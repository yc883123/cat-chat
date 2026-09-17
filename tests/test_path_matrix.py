# -*- coding: utf-8 -*-
"""护栏：路径判定矩阵（core/paths）——判定一律"以解析后的实际目标为准"。

覆盖：
- 方向性（自身/子/兄弟/父）与多允许根；
- Windows 大小写语义（不区分大小写，不应因此误判越界）；
- 工作区"根"本身是链接：界内文件仍判界内；
- 工作区内的链接指向外部：**仍判越界**（安全边界不得放宽——这是"词法路径也算命中"
  这类设计的反例护栏）。

链接不可创建时安全跳过（不依赖特权 CI / 开发者模式）。
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.core.paths import path_within, path_within_any, within_detail  # noqa: E402


def _make_dir_link(link: Path, target: Path) -> None:
    """创建目录链接：优先符号链接，Windows 回退 junction；不可用则跳过测试。

    注意 `os.symlink` **不抛异常也可能没建成**（受限环境/沙箱里写入被静默拦掉，实测
    `link.exists()` 为 False、`resolve()` 原样返回链接路径）——所以每次都要**验活**，
    否则本文件会以一条 `AssertionError` 假红，而不是老老实实 SkipTest。
    """
    target.mkdir(parents=True, exist_ok=True)

    def usable() -> bool:
        """链接真的可用 = 解析结果就是目标目录本身（静默失败时 resolve() 会返回链接自身）。"""
        try:
            return link.is_dir() and link.resolve() == target.resolve()
        except OSError:
            return False

    try:
        os.symlink(target, link, target_is_directory=True)
        if usable():
            return
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            # 中文 Windows 的 mklink 输出是 GBK：按 UTF-8 硬解会在读线程里抛
            # UnicodeDecodeError（一串看着像失败的堆栈，其实只影响读输出），这里只关心
            # returncode，解码用 replace 兜住即可。
            errors="replace",
        )
        if completed.returncode == 0 and usable():
            return
    raise unittest.SkipTest("当前环境不允许创建目录链接（符号链接/junction）")


class PathWithinMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_direction(self):
        child = self.root / "a" / "b.txt"
        sibling = self.root / "other" / "b.txt"
        outside = self.root.parent / "elsewhere.txt"
        self.assertTrue(path_within(child, self.root))
        self.assertTrue(path_within(self.root, self.root))
        self.assertFalse(path_within(sibling, self.root / "a"))
        self.assertFalse(path_within(outside, self.root))
        # 反向不成立：根不在子路径内
        self.assertFalse(path_within(self.root, child))

    def test_multi_roots_and_detail(self):
        child = self.root / "a" / "b.txt"
        other = self.root / "other"
        detail = within_detail(child, [other, self.root], raw="a/b.txt", workspace=self.root)
        self.assertTrue(detail["inside"])
        self.assertEqual(1, detail["hit"], detail)
        self.assertEqual("a/b.txt", detail["raw"])
        self.assertFalse(detail["roots"][0]["hit"])
        self.assertTrue(detail["roots"][1]["hit"])
        self.assertTrue(path_within_any(child, [other, self.root]))
        self.assertFalse(path_within_any(child, [other]))

    @unittest.skipUnless(os.name == "nt", "Windows 路径大小写语义")
    def test_case_insensitive_on_windows(self):
        child = self.root / "Sub" / "B.TXT"
        child.parent.mkdir(parents=True, exist_ok=True)
        child.write_text("x", encoding="utf-8")
        self.assertTrue(path_within(child, Path(str(self.root).upper())))
        self.assertTrue(path_within(Path(str(child).lower()), self.root))

    def test_workspace_root_is_link(self):
        real = self.root / "real-ws"
        link = self.root / "ws-link"
        _make_dir_link(link, real)
        target = real / "inner.txt"
        target.write_text("x", encoding="utf-8")
        # 根的两种形态（链接路径 / 真实路径）都必须把界内文件判为界内
        self.assertTrue(path_within(target, real.resolve()))
        self.assertTrue(path_within(target, link.resolve()))
        self.assertTrue(path_within((link / "inner.txt").resolve(), link.resolve()))

    def test_link_inside_workspace_escaping_outside_still_outside(self):
        """工作区内链接指向外部：实际目标仍属越界（不得因词法路径在界内而放行）。"""
        outside = self.root / "outside-dir"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "secret.txt").write_text("s", encoding="utf-8")
        workspace = self.root / "ws"
        workspace.mkdir()
        _make_dir_link(workspace / "link", outside)
        lexical = workspace / "link" / "secret.txt"
        real = lexical.resolve()
        if real == lexical:
            self.skipTest("当前平台 resolve() 不跟随目录链接，链接逃逸场景不适用")
        self.assertFalse(
            path_within(real, workspace.resolve()),
            "链接逃逸必须判越界（安全边界不得放宽）",
        )
        detail = within_detail(real, [workspace.resolve()], raw=str(lexical), workspace=workspace)
        self.assertFalse(detail["inside"])
        self.assertEqual(-1, detail["hit"])


if __name__ == "__main__":
    unittest.main()
