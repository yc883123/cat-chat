# -*- coding: utf-8 -*-
"""护栏：消息里文件 chip 的三个桌面动作（打开 / 打开文件夹 / 另存为）。

缺陷形态（本文件钉的就是这条）：
消息里的文件 chip 在浏览器里只能 `<a target="_blank">` 预览，在 WebView2 里 `<a download>`
又不弹保存框 —— 桌面端**没有任何一条**「用系统程序打开 / 在资源管理器里定位 / 另存到别处」的路径。
三个 JsApi 补的正是这条，本文件逐条钉住它们的成功与失败分支：

1. `naibaOpenFile`    → `os.startfile`（系统默认关联程序）；
2. `naibaRevealInFolder` → `explorer /select,<path>`，**路径与 /select, 必须同一个 token**
   （拆开会变成"打开两个位置"），且不能 wait（explorer 会转交给已有进程后立刻退出）；
3. `naibaSaveFileAs`  → `create_file_dialog(SAVE_DIALOG)` 后 `shutil.copy2`；**取消不算失败**
   （用户明明点了取消，不该收到"保存失败"）。

三条都共用一个路径校验：目录、空串、不存在的路径都必须**拒绝**且带可读原因；
把目录当文件交给 `os.startfile` 会弹出资源管理器，看起来像"打开成功了"。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import launcher  # noqa: E402


class _BridgeBase(unittest.TestCase):
    def setUp(self) -> None:
        self.api = launcher.JsApi()
        self.tmp = tempfile.TemporaryDirectory()
        # resolve()：CI runner 的 TEMP 是 8.3 短路径，产品侧解析成真实长路径，
        # 不解析就会"本地全绿、CI 全红"（见维护说明 §九.53）。
        self.base = Path(self.tmp.name).resolve()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _file_named(self, name: str, text: str = "正文") -> Path:
        target = self.base / name
        target.write_text(text, encoding="utf-8")
        return target


class OpenFileTests(_BridgeBase):
    def test_calls_startfile_with_resolved_path(self) -> None:
        target = self._file_named("生成结果.md")
        with mock.patch.object(launcher.os, "startfile", create=True) as start:
            result = self.api.naibaOpenFile(str(target))
        self.assertEqual(result, {"ok": True})
        start.assert_called_once_with(str(target))

    def test_rejects_directory(self) -> None:
        folder = self.base / "素材4"
        folder.mkdir()
        with mock.patch.object(launcher.os, "startfile", create=True) as start:
            result = self.api.naibaOpenFile(str(folder))
        self.assertFalse(result["ok"])
        self.assertIn("文件夹", result["error"])
        start.assert_not_called()

    def test_rejects_missing_path(self) -> None:
        result = self.api.naibaOpenFile(str(self.base / "没这个文件.txt"))
        self.assertFalse(result["ok"])
        self.assertIn("不存在", result["error"])

    def test_rejects_empty_path(self) -> None:
        result = self.api.naibaOpenFile("   ")
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_startfile_failure_is_reported(self) -> None:
        target = self._file_named("a.txt")
        with mock.patch.object(launcher.os, "startfile", create=True, side_effect=OSError("boom")):
            result = self.api.naibaOpenFile(str(target))
        self.assertFalse(result["ok"])
        self.assertIn("无法打开文件", result["error"])


class RevealInFolderTests(_BridgeBase):
    def test_explorer_select_is_a_single_token(self) -> None:
        target = self._file_named("大BOSS剧本_完整消息.txt")
        with mock.patch("subprocess.Popen") as popen:
            result = self.api.naibaRevealInFolder(str(target))
        self.assertEqual(result, {"ok": True})
        args = popen.call_args[0][0]
        # 拆成 ["explorer", "/select,", path] 的话 explorer 会以为要打开两个位置。
        self.assertEqual(args, ["explorer", f"/select,{target}"])

    def test_rejects_directory_and_missing(self) -> None:
        folder = self.base / "素材4"
        folder.mkdir()
        with mock.patch("subprocess.Popen") as popen:
            self.assertFalse(self.api.naibaRevealInFolder(str(folder))["ok"])
            self.assertFalse(self.api.naibaRevealInFolder(str(self.base / "x.txt"))["ok"])
        popen.assert_not_called()

    def test_popen_failure_is_reported(self) -> None:
        target = self._file_named("a.txt")
        with mock.patch("subprocess.Popen", side_effect=OSError("no explorer")):
            result = self.api.naibaRevealInFolder(str(target))
        self.assertFalse(result["ok"])
        self.assertIn("无法打开文件夹", result["error"])


class SaveFileAsTests(_BridgeBase):
    def _window(self, chosen):
        window = mock.MagicMock()
        window.create_file_dialog.return_value = chosen
        return window

    def test_copies_to_chosen_path(self) -> None:
        source = self._file_named("extract_conversation.py", "print(1)\n")
        dest = self.base / "另存到这里.py"
        window = self._window([str(dest)])
        with mock.patch("webview.windows", [window]):
            result = self.api.naibaSaveFileAs(str(source), "extract_conversation.py")
        self.assertEqual(result, {"ok": True, "path": str(dest)})
        self.assertEqual(dest.read_text(encoding="utf-8"), "print(1)\n")
        # 默认文件名必须交到对话框上，否则用户面对的是一个空白文件名框。
        kwargs = window.create_file_dialog.call_args[1]
        self.assertEqual(kwargs.get("save_filename"), "extract_conversation.py")

    def test_accepts_plain_string_return(self) -> None:
        """pywebview 各版本返回 str / list / None 三种形态，都要收敛成同一个结果。"""
        source = self._file_named("a.txt")
        dest = self.base / "b.txt"
        with mock.patch("webview.windows", [self._window(str(dest))]):
            result = self.api.naibaSaveFileAs(str(source))
        self.assertTrue(result["ok"])
        self.assertTrue(dest.exists())

    def test_falls_back_to_source_name(self) -> None:
        """调用方没给（或给了空白）默认名时，用源文件名——否则用户面对空白文件名框。"""
        source = self._file_named("封面.png")
        window = self._window(None)
        with mock.patch("webview.windows", [window]):
            self.api.naibaSaveFileAs(str(source), "   ")
        self.assertEqual(window.create_file_dialog.call_args[1].get("save_filename"), "封面.png")

        window2 = self._window(None)
        with mock.patch("webview.windows", [window2]):
            self.api.naibaSaveFileAs(str(source), "")
        self.assertEqual(window2.create_file_dialog.call_args[1].get("save_filename"), "封面.png")

    def test_cancel_is_not_a_failure(self) -> None:
        source = self._file_named("a.txt")
        with mock.patch("webview.windows", [self._window(None)]):
            result = self.api.naibaSaveFileAs(str(source))
        self.assertFalse(result["ok"])
        self.assertTrue(result["cancelled"])
        self.assertNotIn("保存失败", result["error"])

    def test_empty_list_counts_as_cancel(self) -> None:
        source = self._file_named("a.txt")
        with mock.patch("webview.windows", [self._window([])]):
            result = self.api.naibaSaveFileAs(str(source))
        self.assertFalse(result["ok"])
        self.assertTrue(result["cancelled"])

    def test_no_window_is_reported(self) -> None:
        source = self._file_named("a.txt")
        with mock.patch("webview.windows", []):
            result = self.api.naibaSaveFileAs(str(source))
        self.assertFalse(result["ok"])
        self.assertFalse(result.get("cancelled", False))
        self.assertIn("窗口", result["error"])

    def test_rejects_missing_source(self) -> None:
        window = self._window(None)
        with mock.patch("webview.windows", [window]):
            result = self.api.naibaSaveFileAs(str(self.base / "没有.txt"))
        self.assertFalse(result["ok"])
        self.assertIn("不存在", result["error"])
        window.create_file_dialog.assert_not_called()

    def test_copy_failure_is_reported(self) -> None:
        source = self._file_named("a.txt")
        dest = self.base / "锁定" / "b.txt"  # 父目录不存在 → copy2 必然失败
        with mock.patch("webview.windows", [self._window([str(dest)])]):
            result = self.api.naibaSaveFileAs(str(source))
        self.assertFalse(result["ok"])
        self.assertIn("保存失败", result["error"])


class FrontendWiringTests(unittest.TestCase):
    """接线检查：JsApi 方法名与前端调用名必须逐字一致（改一处的经典漏改）。

    前端是 `bridge[method](...)` 的**字符串**调用（三个动作只有一条执行路径），所以这里钉的是
    「方法名字符串在 JS 里出现且 launcher 上真有同名方法」——两处各改一半必然漏一个。
    """

    JS = ROOT / "public" / "js" / "15-bind-events.js"

    def test_frontend_calls_match_bridge_methods(self) -> None:
        js = self.JS.read_text(encoding="utf-8")
        api = launcher.JsApi()
        for name in ("naibaOpenFile", "naibaRevealInFolder", "naibaSaveFileAs"):
            self.assertTrue(hasattr(api, name), f"launcher.JsApi 缺少 {name}")
            self.assertIn(f"'{name}'", js, f"前端没有调用 {name}")

    def test_reveal_button_only_rendered_for_desktop(self) -> None:
        """「打开文件夹」是桌面专属：手机浏览器点了只会在 PC 上弹窗口，用户面前什么都没有。"""
        media = (ROOT / "public" / "js" / "03-media.js").read_text(encoding="utf-8")
        start = media.index("export function fileActionBarMarkup")
        end = media.index("export function", start + 10)
        body = media[start:end]
        self.assertIn("isPywebview()", body)
        self.assertIn("data-file-reveal", body)


if __name__ == "__main__":
    unittest.main()
