# -*- coding: utf-8 -*-
"""护栏：工具设计修正（read_file 双预算/失败语义/确定性枚举/搜索语义/游标闭环）。

保护对象：
- read_file 按行双预算（50 行 × 30000 字符）截断、截断标记（含行区间与续读起点）、
  with_line_numbers / end_line；max_chars 对模型隐藏、执行层硬上限；
- pwsh / run_skill_script 非零退出码、http_request >=400 → success=False（原恒 True）；
- list_directory 单入口（glob_files 已并入）：pattern/files_only/recursive/start_after 续枚举；
- search_files ignore_case 统一生效、超大文件跳过计数、命中总数汇总；
- job_output 增量游标闭环（推进 cursor 回传）。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.mcp import MCPRegistry  # noqa: E402
from naiba.tools.providers import core as core_provider  # noqa: E402
from naiba.tools.providers.core import _result_success  # noqa: E402


def _ctx(workspace: Path):
    return core_provider.ToolContext(
        workspace=workspace,
        python_executable=sys.executable,
        command_timeout=60,
        mcp_registry=MCPRegistry([]),
        mcp_register=None,
    )


class ReadFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba-readfile-"))
        self.ctx = _ctx(self.tmp)

    def _make(self, lines: list[str]) -> Path:
        target = self.tmp / "sample.txt"
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return target

    def test_many_lines_within_char_budget_reads_all(self) -> None:
        """行数不设默认上限：内容 ≤ 30000 字符时全量返回（用户实测：>50 行的小文件
        曾被行数上限截断、模型被迫多次续读）。"""
        target = self._make([f"line-{i:03d}" for i in range(1, 61)])
        out = core_provider._tool_read_file(self.ctx, {"path": str(target)}, None)
        self.assertIn("line-001", out)
        self.assertIn("line-060", out)
        self.assertNotIn("已达读取上限", out)

    def test_explicit_max_lines_still_limits(self) -> None:
        """显式传 max_lines 仍生效（兼容旧调用）；截断标记行区间 + 续读起点。"""
        target = self._make([f"line-{i:03d}" for i in range(1, 61)])
        out = core_provider._tool_read_file(self.ctx, {"path": str(target), "max_lines": 50}, None)
        self.assertIn("line-001", out)
        self.assertIn("line-050", out)
        self.assertNotIn("line-051", out)
        self.assertIn("已返回第 1-50 行", out)
        self.assertIn("start_line=51", out)

    def test_resume_from_marker_returns_remaining(self) -> None:
        target = self._make([f"line-{i:03d}" for i in range(1, 61)])
        first = core_provider._tool_read_file(self.ctx, {"path": str(target), "max_lines": 50}, None)
        resume = int(first.rsplit("start_line=", 1)[1].split()[0].rstrip("）"))
        out = core_provider._tool_read_file(self.ctx, {"path": str(target), "start_line": resume}, None)
        self.assertIn("line-051", out)
        self.assertIn("line-060", out)
        self.assertNotIn("截图", out)  # 无截断标记（已到文件尾）

    def test_char_budget_truncates_midline_and_resume_at_cut_line(self) -> None:
        # 单行远超 30000 字符：按字符截断该行，resume 指向该行本身。
        target = self.tmp / "big.txt"
        target.write_text(("X" * 40000) + "\n", encoding="utf-8")
        out = core_provider._tool_read_file(self.ctx, {"path": str(target)}, None)
        self.assertEqual(out.count("X"), 30000)
        self.assertIn("第 1 行按字符预算截断", out)
        self.assertIn("start_line=1", out)

    def test_end_line_and_with_line_numbers(self) -> None:
        target = self._make([f"row-{i:02d}" for i in range(1, 20)])
        out = core_provider._tool_read_file(
            self.ctx, {"path": str(target), "start_line": 5, "end_line": 7, "with_line_numbers": True}, None
        )
        self.assertIn("5: row-05", out)
        self.assertIn("7: row-07", out)
        self.assertNotIn("row-08", out)
        self.assertNotIn("已达读取上限", out)

    def test_start_line_beyond_end_returns_explicit_note(self) -> None:
        target = self._make(["a", "b"])
        out = core_provider._tool_read_file(self.ctx, {"path": str(target), "start_line": 9}, None)
        self.assertIn("文件共 2 行", out)

    def test_max_chars_hidden_from_schema_and_capped(self) -> None:
        """max_chars 不得对模型暴露；执行层兼容旧调用但硬上限 30000 字符（防浪费）。"""
        from naiba.tools.registry import build_core_tool_specs

        spec = next(item for item in build_core_tool_specs() if item.name == "read_file")
        self.assertNotIn("max_chars", (spec.parameters or {}).get("properties", {}))
        target = self.tmp / "huge.txt"
        target.write_text(("X" * 40000) + "\n", encoding="utf-8")
        out = core_provider._tool_read_file(
            self.ctx, {"path": str(target), "max_chars": 100000}, None
        )
        self.assertEqual(out.count("X"), 30000, "执行层必须把 max_chars 封顶在 30000")

    def test_empty_file(self) -> None:
        target = self.tmp / "empty.txt"
        target.write_text("", encoding="utf-8")
        out = core_provider._tool_read_file(self.ctx, {"path": str(target)}, None)
        self.assertIn("文件为空", out)


class FailureSemanticsTests(unittest.TestCase):
    def test_exit_code_tools_fail_on_nonzero(self) -> None:
        self.assertTrue(_result_success("pwsh", "exit_code=0\nok"))
        self.assertFalse(_result_success("pwsh", "exit_code=1\nboom"))
        self.assertFalse(_result_success("run_skill_script", "exit_code=-1073741510\n"))
        self.assertTrue(_result_success("read_file", "内容") and _result_success("edit_file", "已修改"))

    def test_http_status_ge_400_fails(self) -> None:
        self.assertTrue(_result_success("http_request", "HTTP 200\nok"))
        self.assertFalse(_result_success("http_request", "HTTP 404\nnot found"))
        self.assertFalse(_result_success("http_request", "HTTP 500\nboom"))


class DeterministicEnumerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba-enum-"))
        self.ctx = _ctx(self.tmp)
        for name in ("b.txt", "a.txt", "c.txt", "d.txt"):
            (self.tmp / name).write_text(name, encoding="utf-8")

    def test_list_sorted_pattern_and_resume_marker(self) -> None:
        out = core_provider._tool_list_directory(
            self.ctx,
            {"path": str(self.tmp), "pattern": "*.txt", "files_only": True, "limit": 2},
            None,
        )
        lines = out.splitlines()
        self.assertIn("a.txt", lines[0])
        self.assertIn("b.txt", lines[1])
        self.assertIn("共 4 项，已列出前 2 项", out)
        self.assertIn("start_after=", out)
        last = lines[1].split(" ", 1)[-1]  # 去掉 "FILE "/"DIR " 前缀，取干净路径
        resumed = core_provider._tool_list_directory(
            self.ctx,
            {"path": str(self.tmp), "pattern": "*.txt", "files_only": True, "limit": 2, "start_after": last},
            None,
        )
        self.assertIn("c.txt", resumed)
        self.assertIn("d.txt", resumed)

    def test_recursive_pattern_finds_nested(self) -> None:
        sub = self.tmp / "sub"
        sub.mkdir()
        (sub / "deep.png").write_text("x", encoding="utf-8")
        out = core_provider._tool_list_directory(
            self.ctx,
            {"path": str(self.tmp), "recursive": True, "pattern": "**/*.png"},
            None,
        )
        self.assertIn("deep.png", out)

    def test_list_directory_absolute_sorted(self) -> None:
        out = core_provider._tool_list_directory(self.ctx, {"path": str(self.tmp)}, None)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("FILE "))
        # 生产侧对工作区路径做过 resolve()：CI runner 的 TEMP 是 8.3 短路径
        # （形如 RUNNER~1），必须同样解析后再比，否则本机过、CI 挂。
        self.assertIn(str((self.tmp / "a.txt").resolve()), lines[0])
        self.assertIn("\\d.txt", lines[-1])


class SearchSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        # **两侧都要 resolve()**（§9.53）：生产侧对工作区路径做过 resolve()，而 CI runner 的
        # TEMP 是 8.3 短路径（`NAIBA-S~1`），不 resolve 就会「本机全绿、流水线必挂」——
        # 断言里比较的是工具输出的绝对路径，必须与生产侧同一形态。
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba-search-")).resolve()
        self.ctx = _ctx(self.tmp)
        self.target = self.tmp / "code.txt"
        self.target.write_text("foo\nFOO\nbar\n", encoding="utf-8")

    def test_substring_default_case_sensitive(self) -> None:
        out = core_provider._tool_search_files(self.ctx, {
            "path": str(self.tmp), "query": "foo", "pattern": "*.txt",
        }, None)
        self.assertIn("共 1 处命中，已显示前 1 组", out)

    def test_ignore_case_applies_to_substring(self) -> None:
        out = core_provider._tool_search_files(self.ctx, {
            "path": str(self.tmp), "query": "foo", "pattern": "*.txt", "ignore_case": True,
        }, None)
        self.assertIn("共 2 处命中", out)

    def test_large_files_skipped_are_reported(self) -> None:
        big = self.tmp / "big.bin"
        big.write_bytes(b"x" * 2048)
        out = core_provider._tool_search_files(self.ctx, {
            "path": str(self.tmp), "query": "x", "max_file_size": 1024,
        }, None)
        self.assertIn("已跳过 1 个超过 1024 字节的文件", out)

    def test_path_as_single_file_searches_inside_it(self) -> None:
        """模型直觉用法：path 传文件路径 → 直接在该文件内搜索（不再误为空目录）。"""
        target = self.tmp / "k1.json"
        target.write_text('{"k1": "xxx", "k2": "yyy"}', encoding="utf-8")
        out = core_provider._tool_search_files(self.ctx, {
            "path": str(target), "query": "k1",
        }, None)
        self.assertIn("共 1 处命中", out)
        self.assertIn("k1", out)

    def test_single_file_pattern_ignored_and_miss_reported(self) -> None:
        target = self.tmp / "k1.json"
        target.write_text('{"k1": "xxx"}', encoding="utf-8")
        out = core_provider._tool_search_files(self.ctx, {
            "path": str(target), "query": "k1", "pattern": "*.txt",
        }, None)
        self.assertIn("共 1 处命中", out, "单文件搜索时 pattern 应被忽略")
        miss = core_provider._tool_search_files(self.ctx, {
            "path": str(target), "query": "zzz_not_exists",
        }, None)
        self.assertEqual(miss, "未找到匹配内容")

    def test_missing_path_reports_explicit_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            core_provider._tool_search_files(self.ctx, {
                "path": str(self.tmp / "nope"), "query": "x",
            }, None)
        self.assertIn("路径不存在", str(ctx.exception))

    # ---- 输出去重：表头独占一行印路径，行号行不再重复绝对路径 ----

    def test_default_output_header_once_and_bare_line_numbers(self) -> None:
        target = self.tmp / "dup.py"
        target.write_text("import os\nx = 1\nimport os\n", encoding="utf-8")
        out = core_provider._tool_search_files(self.ctx, {
            "path": str(target), "query": "import os",
        }, None)
        rows = out.splitlines()
        self.assertEqual(rows[0], f"{target}:", f"表头应独占一行：{rows[:3]}")
        self.assertEqual(rows[1], "1: import os")
        self.assertEqual(rows[2], "3: import os")
        self.assertEqual(out.count(str(target)), 1, "同一文件的路径只能出现一次")
        for row in rows[1:]:
            self.assertNotIn(str(self.tmp), row, f"行号行不得再带绝对路径：{row}")

    def test_context_output_prints_header_once_and_numbers_are_one_based(self) -> None:
        target = self.tmp / "ctx.py"
        target.write_text("a\nb\nMATCH\nd\ne\n\nMATCH\nh\n", encoding="utf-8")
        out = core_provider._tool_search_files(self.ctx, {
            "path": str(target), "query": "MATCH", "context_lines": 1,
        }, None)
        self.assertEqual(out.splitlines()[0], f"{target}:")
        self.assertEqual(out.count(str(target)), 1, "上下文块再多也只印一次表头")
        # 表头必须**紧跟**首个上下文块：把表头也塞进 "\n\n".join 会在路径与首个块之间
        # 多出一个空行，与无上下文分支的排版不一致（同一个函数两条分支两种口径）。
        self.assertEqual(out.splitlines()[1], "2: b", f"表头后不得插空行：{out.splitlines()[:3]}")
        # 1 基行号：与无上下文分支、read_file 同口径（历史上这一支印的是 0 基下标，
        # 模型照它传 start_line 会读错一行）。
        self.assertIn("2: b", out)
        self.assertIn("3: MATCH", out)
        self.assertIn("7: MATCH", out)
        self.assertNotIn("0: ", out)
        self.assertIn("\n\n", out, "相邻命中块之间仍然空行分隔（可读性靠它）")

    def test_multiline_regex_header_carries_hit_count(self) -> None:
        target = self.tmp / "multi.py"
        target.write_text("import os\nx = 1\nimport os", encoding="utf-8")
        out = core_provider._tool_search_files(self.ctx, {
            "path": str(target), "query": r"import\s+os", "regex": True, "multiline": True,
        }, None)
        self.assertEqual(out.splitlines()[0], f"{target}:（2 处命中）")
        self.assertEqual(out.count(str(target)), 1)
        self.assertIn("match 1-1 行:", out)

    def test_multi_file_output_repeats_each_path_once_and_blanks_between_files(self) -> None:
        (self.tmp / "one.txt").write_text("NEEDLE\nfiller\n", encoding="utf-8")
        (self.tmp / "two.txt").write_text("filler\nNEEDLE\n", encoding="utf-8")
        out = core_provider._tool_search_files(self.ctx, {
            "path": str(self.tmp), "query": "NEEDLE", "pattern": "*.txt",
        }, None)
        for name in ("one.txt", "two.txt"):
            path = str(self.tmp / name)
            self.assertEqual(out.count(path), 1, f"{name} 的路径只应出现在表头一次")
            self.assertIn(f"{path}:", out)
        self.assertIn("\n\n", out, "文件块之间必须空行分隔")
        self.assertIn("共 2 处命中，已显示前 2 组", out)


class JobOutputCursorTests(unittest.TestCase):
    def test_cursor_marker_roundtrip(self) -> None:
        from naiba.subagent import job_tool_handler_factory

        fake_jobs = SimpleNamespace(
            get=lambda job_id: {"status": "running"},
            read=lambda job_id, cursor=0: {"events": [{"line": "hello"}], "cursor": 7},
        )
        app = SimpleNamespace(jobs=fake_jobs)
        handler = job_tool_handler_factory(app)["job_output"]
        ok, out = handler({"job_id": "j1"}, [], None)
        self.assertTrue(ok)
        self.assertIn("hello", out)
        self.assertIn("cursor=7", out)


class RunSkillScriptEncodingTests(unittest.TestCase):
    def test_chinese_arg_preserved(self) -> None:
        """run_skill_script 传中文路径参数必须原样传给子进程；子进程以 UTF-8 运行时
        输出（PYTHONIOENCODING/PYTHONUTF8），父进程 UTF-8 解码不再乱码（Windows 实测）。"""
        tmp = Path(tempfile.mkdtemp(prefix="naiba-skill-"))
        (tmp / "scripts").mkdir()
        script = tmp / "scripts" / "echo_arg.py"
        script.write_text("import sys\nprint(sys.argv[1])\n", encoding="utf-8")
        ctx = _ctx(tmp)
        active = [{"name": "批量生成提示词", "id": "skill-x", "root": str(tmp), "path": str(tmp)}]
        result = core_provider._tool_run_skill_script(
            ctx,
            {
                "skill": "批量生成提示词",
                "script": "scripts/echo_arg.py",
                "args": [r"C:\Users\user\Desktop\临时提示词\_pending_entry.json"],
            },
            active,
        )
        self.assertIn("exit_code=0", result)
        self.assertIn("临时提示词", result)
        self.assertIn("_pending_entry.json", result)


if __name__ == "__main__":
    unittest.main()
