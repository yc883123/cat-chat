# -*- coding: utf-8 -*-
"""护栏：拖文件夹进输入区的「前端 JS → WebView2 原生 → Python」三段接力。

缺陷形态（本文件钉的就是这条）：
前端识别出拖进来的是目录后，去问桌面端"这个文件夹在硬盘哪儿"，等 0.8 秒没人回答，
就弹一句"浏览器里拖文件夹拿不到完整路径"——**在桌面客户端里也弹这句**，用户以为功能坏了。
真实原因在前两步之外：WebView2 的 `chrome.webview.postMessageWithAdditionalObjects`
只要收到不受支持的对象就抛异常、**整条消息（连同紧随其后的 postMessage）作废**
（微软官方文档原话），而 pywebview 注入的 drop 监听正是走这一步 —— 它一抛，
事件消息根本到不了 Python，Python 侧连异常都看不到。

所以现在的分工必须同时成立，本文件逐条钉住：
1. 前端在 drop 事件**同步阶段**自己调原生接口，并把异常文本抓下来（`handFilesToNative`）；
2. Python 侧不再依赖事件回调，而是**直接读** `_dnd_state['paths']`（`JsApi.naibaFolderDrop`）；
3. pywebview 的 drop 监听仍要挂着 —— WebView2 只在"有监听者"时才把真实路径交给 pywebview
   （`_dnd_state['num_listeners'] > 0`），摘掉它就等于把整条链路的前置开关关掉；
4. 每一段都往 `drop-debug.log` 留一行：这条路出问题时前端只会显示一句兜底提示，
   不留痕就只能靠猜（2026-09-21 就是这么返工的）。
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

BIND_EVENTS = ROOT / "public" / "js" / "15-bind-events.js"
LAUNCHER_SRC = ROOT / "launcher.py"

try:  # pywebview 是运行期依赖；缺了就让这几条显式失败，而不是静默跳过
    from webview.dom import _dnd_state
except Exception:  # pragma: no cover - 环境相关
    _dnd_state = None


class FakePathsTests(unittest.TestCase):
    """`JsApi.naibaFolderDrop`：只挑目录、去重、消费后清空队列。"""

    def setUp(self) -> None:
        if _dnd_state is None:  # pragma: no cover
            self.skipTest("pywebview 不可用")
        self._saved = list(_dnd_state.get("paths") or [])
        _dnd_state["paths"] = []
        self._patch_settle = mock.patch.object(launcher, "FOLDER_DROP_SETTLE_SECONDS", 0)
        self._patch_settle.start()
        self.api = launcher.JsApi()
        self.tmp = tempfile.TemporaryDirectory()
        # resolve()：CI runner 的 TEMP 是 8.3 短路径，产品侧会解析成真实长路径，
        # 不解析就会"本地全绿、CI 全红"（见维护说明 §九.53）。
        self.base = Path(self.tmp.name).resolve()

    def tearDown(self) -> None:
        self._patch_settle.stop()
        if _dnd_state is not None:
            _dnd_state["paths"] = self._saved
        self.tmp.cleanup()

    def _dir_named(self, name: str) -> Path:
        target = self.base / name
        target.mkdir()
        return target

    def test_returns_dropped_directory(self) -> None:
        folder = self._dir_named("素材4")
        _dnd_state["paths"] = [("素材4", str(folder))]
        result = self.api.naibaFolderDrop()
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(result["dirs"], [str(folder)])

    def test_skips_plain_files(self) -> None:
        """普通文件仍走前端上传链路，这里不能把它们当成"拖进来的文件夹"。"""
        folder = self._dir_named("素材4")
        plain = self.base / "note.txt"
        plain.write_text("x", encoding="utf-8")
        _dnd_state["paths"] = [("素材4", str(folder)), ("note.txt", str(plain))]
        result = self.api.naibaFolderDrop()
        self.assertEqual(result["dirs"], [str(folder)])
        self.assertNotIn(str(plain), result["dirs"])

    def test_missing_path_is_not_a_directory(self) -> None:
        _dnd_state["paths"] = [("ghost", str(self.base / "不存在的目录"))]
        result = self.api.naibaFolderDrop()
        self.assertEqual(result["dirs"], [])
        self.assertEqual(result["reason"], "no-dir")

    def test_empty_queue_reports_empty(self) -> None:
        """原生那一步就废了（异常/无监听）：队列是空的，前端要能区分"没路径"和"没目录"。"""
        result = self.api.naibaFolderDrop()
        self.assertEqual(result["dirs"], [])
        self.assertEqual(result["reason"], "empty")

    def test_native_error_is_passed_through(self) -> None:
        """前端抓到的原生异常必须带到 Python 侧（否则只知道失败、不知道哪一步失败）。"""
        result = self.api.naibaFolderDrop("native-throw: not a file")
        self.assertIn("native-throw", result["native_error"])

    def test_queue_is_consumed(self) -> None:
        """不消费的话，下一次拖拽会拿到上一次的路径，还会让事件回调按文件名错配对象。"""
        folder = self._dir_named("素材4")
        _dnd_state["paths"] = [("素材4", str(folder))]
        self.api.naibaFolderDrop()
        self.assertEqual(_dnd_state["paths"], [])

    def test_duplicate_entries_collapse(self) -> None:
        folder = self._dir_named("素材4")
        _dnd_state["paths"] = [("素材4", str(folder)), ("素材4", str(folder))]
        result = self.api.naibaFolderDrop()
        self.assertEqual(result["dirs"], [str(folder)])

    def test_never_raises_without_app(self) -> None:
        """诊断日志拿不到数据目录时也不许抛（它永远不该拦住功能）。"""
        with mock.patch.object(launcher.srv, "APP", None, create=True):
            result = self.api.naibaFolderDrop()
        self.assertIsInstance(result["dirs"], list)


class BridgeContractTests(unittest.TestCase):
    """三段接力的接线必须同时存在（少一段就是这次这种"静默失败"）。"""

    def setUp(self) -> None:
        self.js = BIND_EVENTS.read_text(encoding="utf-8")
        self.py = LAUNCHER_SRC.read_text(encoding="utf-8")

    def test_frontend_calls_native_bridge_itself(self) -> None:
        self.assertIn("postMessageWithAdditionalObjects", self.js)
        self.assertIn("'FilesDropped'", self.js)

    def test_frontend_wraps_native_call_and_captures_error(self) -> None:
        """原生调用必须被 try/catch 包住并把异常带回来：它是唯一能定位"哪一步废了"的证据。"""
        self.assertIn("function handFilesToNative", self.js)
        self.assertIn("native-throw:", self.js)

    def test_drop_handler_hands_files_over_synchronously(self) -> None:
        """File 对象出了 drop 事件就失效，所以 handFilesToNative 必须在处理器同步段落里调。"""
        self.assertIn("const nativeError = handFilesToNative(event.dataTransfer?.files);", self.js)
        index = self.js.index("const nativeError = handFilesToNative")
        self.assertLess(index, self.js.index("void handleFolderDrop(event, nativeError)"))

    def test_frontend_asks_python_directly(self) -> None:
        """主通道：直接问 Python，不依赖 pywebview 的事件回调（那条路在 WebView2 上会死）。"""
        self.assertIn("window.pywebview.api.naibaFolderDrop", self.js)

    def test_python_reads_dnd_state_directly(self) -> None:
        self.assertIn("def naibaFolderDrop", self.py)
        self.assertIn("from webview.dom import _dnd_state", self.py)

    def test_drop_listener_is_still_registered(self) -> None:
        """**不能摘掉**：WebView2 只在 num_listeners > 0 时才把真实路径交给 pywebview。"""
        self.assertIn("def _register_drop_listener", self.py)
        self.assertIn("node.events.drop += DOMEventHandler(self._on_composer_drop)", self.py)
        self.assertIn("num_listeners", self.py)

    def test_both_channels_are_deduped(self) -> None:
        """两条通道可能都回交同一批路径，不许长出两个相同的索引 chip。"""
        self.assertIn("isFreshFolderPath", self.js)
        self.assertIn("addFolderPathsOnce", self.js)
        window = self.js[self.js.index("async function handleFolderDrop") :]
        window = window[: window.index("window.naibaHandleDroppedFolders")]
        self.assertIn("addFolderPathsOnce", window)

    def test_each_hop_writes_a_trace(self) -> None:
        self.assertIn("def _drop_debug_log", self.py)
        self.assertIn("drop-debug.log", self.py)
        for needle in ("dropListener", "folderDrop", "dropEvent"):
            self.assertIn(needle, self.py)

    def test_failure_message_no_longer_blames_the_browser_in_desktop(self) -> None:
        """桌面端拿不到路径时不许再说"请在桌面客户端里拖入"（用户就在客户端里）。"""
        block = self.js[self.js.index("async function handleFolderDrop") :]
        block = block[: block.index("window.naibaHandleDroppedFolders")]
        self.assertIn("isPywebview()", block)
        self.assertIn("没能读到这个文件夹的位置", block)


if __name__ == "__main__":
    unittest.main()
