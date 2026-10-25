# -*- coding: utf-8 -*-
"""护栏：拆书匠命令行脚本（《小说改编接入工作台实施计划》§三.4、§七）。

两条硬保证：
1. **零 AI**：AST 扫 `scripts/split_novel.py` 的 import，出现 app / llm / run / jobs
   或任何会话、模型模块即判红。脚本必须能在不启动服务、不抢实例锁的前提下跑完。
2. **脚本与工作台结果一致**：同一本书、同一参数，章数/标题/字数逐项相同——
   两边共用 `naiba.core.novel_split` 与同一份编码嗅探，不各写一套。

另外覆盖退出码：文件不存在 2、编码无法识别 3、无内容 4、拒绝覆盖 5。
"""

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core import novel_split  # noqa: E402
from naiba.novel.service import NovelStore  # noqa: E402
from naiba.tools.providers.novel import read_text_auto  # noqa: E402

SCRIPT = ROOT / "scripts" / "split_novel.py"
# 零 AI 边界：这些前缀一旦被 import，「不启动服务就能跑」就不成立了。
FORBIDDEN_PREFIXES = (
    "naiba.app",
    "naiba.llm",
    "naiba.run",
    "naiba.jobs",
    "naiba.storage",
    "naiba.http",
    "naiba.studio",
    "naiba.labeler",
    "naiba.comfyui",
    "naiba.tools.registry",
    "naiba.tools.executor",
)


def _book_text(chapters: int = 5, body: str = "这是一段测试正文内容。" * 40) -> str:
    return "".join(f"第{i}章 测试标题{i}\n{body}\n" for i in range(1, chapters + 1))


class ScriptImportGuardTests(unittest.TestCase):
    def test_no_ai_modules_imported(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                # 相对 import（level>0）不会出现在脚本里，出现也归到「不明来源」。
                imported.append(module)
        bad = [name for name in imported
               if any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN_PREFIXES)]
        self.assertEqual(bad, [], f"脚本 import 了会话/模型模块，零 AI 边界被破坏：{bad}")

    def test_only_allowed_naiba_modules(self):
        """正向断言：脚本只许碰算法层与编码嗅探这两个 naiba 模块。"""
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        naiba_imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("naiba"):
                        naiba_imports.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("naiba"):
                naiba_imports.add(node.module or "")
        self.assertEqual(
            naiba_imports,
            {"naiba.core", "naiba.tools.providers.novel"},
            f"脚本的 naiba 依赖超出白名单：{sorted(naiba_imports)}",
        )


class ScriptRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _write(self, name: str, text: str, encoding: str = "utf-8") -> Path:
        path = self.dir / name
        path.write_bytes(text.encode(encoding))
        return path

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(ROOT), timeout=120,
        )

    def test_dry_run_reports_summary_and_level(self):
        path = self._write("book.txt", _book_text(6))
        result = self._run(str(path), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("章数：6", result.stdout)
        self.assertIn("拆分层级：heading", result.stdout)
        # 编码口径必须诚实：这是「实际选中的解码方式」，不是源编码判定。
        self.assertIn("实际选中的解码方式", result.stdout)
        self.assertFalse((self.dir / "book_chapters").exists(), "--dry-run 不能写盘")

    def test_writes_one_file_per_chapter(self):
        path = self._write("book.txt", _book_text(4))
        out = self.dir / "out"
        result = self._run(str(path), "--out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        files = sorted(p.name for p in out.iterdir())
        self.assertEqual(len(files), 4)
        self.assertTrue(files[0].startswith("0001_"), files)

    def test_format_md_and_json(self):
        path = self._write("book.txt", _book_text(3))
        md = self._run(str(path), "--format", "md", "--out", str(self.dir / "md"))
        self.assertEqual(md.returncode, 0, md.stderr)
        self.assertTrue((self.dir / "md" / "book.md").is_file())
        js = self._run(str(path), "--format", "json", "--out", str(self.dir / "js"))
        self.assertEqual(js.returncode, 0, js.stderr)
        payload = json.loads((self.dir / "js" / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["total"], 3)
        for row in payload["chapters"]:
            self.assertNotIn("content", row, "json 索引不能带正文")

    def test_encodings(self):
        cases = {
            "gbk.txt": ("gb18030", "测试标题"),
            "utf8.txt": ("utf-8-sig", "测试标题"),
            "bom.txt": ("utf-8-sig", "测试标题"),
        }
        for name, (encoding, marker) in cases.items():
            path = self._write(name, _book_text(3), encoding=encoding)
            result = self._run(str(path), "--dry-run")
            self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")
            self.assertIn(marker, result.stdout, name)

    def test_explicit_encoding_overrides_sniffing(self):
        big5 = "第一章 開端\n" + ("這是一段測試正文。" * 40) + "\n"
        path = self.dir / "big5.txt"
        path.write_bytes(big5.encode("big5"))
        # 自动嗅探会落到 gb18030（能解出字节但内容是乱码式字符），显式指定才拿到真标题。
        auto = self._run(str(path), "--dry-run")
        self.assertEqual(auto.returncode, 0, auto.stderr)
        explicit = self._run(str(path), "--encoding", "big5", "--dry-run")
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertIn("第一章 開端", explicit.stdout)

    def test_exit_code_missing_file(self):
        result = self._run(str(self.dir / "nope.txt"))
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_exit_code_bad_encoding(self):
        path = self._write("book.txt", _book_text(2))
        result = self._run(str(path), "--encoding", "ascii")
        self.assertEqual(result.returncode, 3, result.stderr)

    def test_exit_code_empty_content(self):
        path = self._write("blank.txt", "   \n\n   \n")
        result = self._run(str(path))
        self.assertEqual(result.returncode, 4, result.stderr)

    def test_refuses_to_overwrite_without_force(self):
        path = self._write("book.txt", _book_text(2))
        out = self.dir / "out"
        self.assertEqual(self._run(str(path), "--out", str(out)).returncode, 0)
        refused = self._run(str(path), "--out", str(out))
        self.assertEqual(refused.returncode, 5, refused.stderr)
        self.assertIn("--force", refused.stderr)
        forced = self._run(str(path), "--out", str(out), "--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)


class ScriptMatchesWorkbenchTests(unittest.TestCase):
    """脚本路径与工作台路径必须给出同一份章节表（同书、同参数）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_same_chapters_from_both_paths(self):
        text = _book_text(8)
        path = self.dir / "book.txt"
        path.write_bytes(text.encode("gb18030"))

        # 工作台路径：导入暂存 → 拆章索引。
        store = NovelStore(self.dir / "data")
        staged = store.stage_import(filename="book.txt", data=path.read_bytes(), max_len=6000)

        # 脚本路径：直接调脚本写的同一批函数（子进程实跑另由 RuntimeTests 覆盖）。
        decoded, _encoding = read_text_auto(path)
        rows = novel_split.split_chapters(decoded, 6000)

        self.assertEqual(staged["total"], len(rows))
        for index, row in enumerate(rows, start=1):
            self.assertEqual(staged["chapters"][index - 1]["title"], str(row["title"]))
            self.assertEqual(staged["chapters"][index - 1]["len"], len(str(row["body"])))
        self.assertEqual(staged["split_level"], novel_split.detect_level(decoded, 6000))


if __name__ == "__main__":
    unittest.main()
