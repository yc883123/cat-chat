# -*- coding: utf-8 -*-
"""输入区右键「粘贴」前端接线守门（源码级，与 tests/test_context_menu.py 同风格）。

为什么这类改动要多写源码级断言：这一条链是**四个文件 + 一层原生桥**接起来的，
任何一处"写了但没接上"在单测里都不报错（浏览器里就是"点了没反应"）：

    01-core.js（菜单/动作编排）→ 注入的 driver
    15-bind-events.js（组合根：注册 driver）→
    12-chat-input.js（driver 实现：问桥、图片走 uploadFiles、文件走路径附件）→
    launcher.py（JsApi.naibaClipboardPayload）

其中**桥方法名跨层一致**最容易悄悄改坏（Python 侧改名 → 前端那一行永远 Promise 失败），
所以单独一条断言两边必须出现同一个字面量。

判决性：下面每一条在改动前都是红的（粘贴动作只走 execCommand + readText；没有 driver、
没有自适应文案、空串还会把选区删掉）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BRIDGE_METHOD = "naibaClipboardPayload"


def _source(*parts: str) -> str:
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def _function_body(source: str, marker: str, end_marker: str = "\n}") -> str:
    """截出 marker 之后到 `end_marker` 的一段（源码级断言的通用取法）。"""
    start = source.index(marker)
    end = source.index(end_marker, start)
    return source[start:end]


class PasteActionWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.core = _source("public", "js", "01-core.js")
        self.chat = _source("public", "js", "12-chat-input.js")
        self.bind = _source("public", "js", "15-bind-events.js")
        self.launcher = _source("launcher.py")

    # ---- ① 桥名字跨层一致（Python ↔ 前端）----
    def test_bridge_method_name_matches_on_both_sides(self) -> None:
        self.assertIn(f"def {BRIDGE_METHOD}(", self.launcher, "Python 侧缺桥方法")
        self.assertIn(f"{BRIDGE_METHOD}", self.chat, "前端没调这个桥方法（改名就会静默失效）")

    # ---- ② 粘贴动作必须走原生驱动 ----
    def test_paste_action_consults_the_native_driver(self) -> None:
        body = _function_body(self.core, "export async function runTextContextAction")
        self.assertIn("clipboardPasteDriver", body, "粘贴动作没问原生剪贴板")
        self.assertIn("clipboardPasteDriver.apply()", body)
        self.assertIn("handled", body, "要能区分'原生已处理'与'走文本通道'")

    # ---- ③ 空串不得再插进可编辑区（旧实现会删掉选中文字并误报"已粘贴"）----
    def test_empty_clipboard_text_must_not_replace_the_selection(self) -> None:
        self.assertIn("text !== ''", self.core, "空串必须挡在 insertTextIntoEditable 之前")
        self.assertNotIn(
            "if (text != null) ok = insertTextIntoEditable(text)", self.core,
            "旧写法会把选区替换成空串（=删掉选中内容）并返回 true，导致误报'已粘贴'",
        )

    # ---- ④ 菜单文案自适应 ----
    def test_paste_menu_label_is_probed_and_refreshed(self) -> None:
        self.assertIn("export async function refreshContextMenuPasteLabel", self.core)
        menu_body = _function_body(self.core, "export function showTextContextMenu")
        self.assertIn("refreshContextMenuPasteLabel", menu_body, "显示菜单时没触发文案探测")
        refresh_body = _function_body(self.core, "export async function refreshContextMenuPasteLabel")
        self.assertIn("labelFor", refresh_body, "文案必须由 driver 提供，别在底层模块里重写一份口径")
        self.assertIn("menu.hidden", refresh_body, "等待期间菜单可能已关闭：要重新确认再回填")
        # 文案口径只在 driver 里定义一次
        self.assertIn("粘贴图片并上传", self.chat)
        self.assertIn("个文件", self.chat)

    def test_driver_is_injected_by_the_composition_root(self) -> None:
        self.assertIn("export function setClipboardPasteDriver", self.core)
        self.assertIn("export const clipboardPasteDriver", self.chat)
        self.assertIn("setClipboardPasteDriver(clipboardPasteDriver)", self.bind,
                      "组合根必须把 driver 真正注册进去（写了但没接上=点了没反应）")

    # ---- ⑤ 两条落地路径：图片上传 / 文件路径附件 ----
    def test_image_payload_goes_through_the_existing_upload_pipeline(self) -> None:
        body = _function_body(self.chat, "async function applyNativeClipboardPaste")
        self.assertIn("payload.kind === 'image'", body)
        self.assertIn("uploadFiles([pngFileFromBase64(", body, "图片必须复用既有上传链路")
        self.assertIn("createConversation", body, "没有会话时先建会话（与 handlePasteImage 同口径）")

    def test_file_payload_becomes_a_path_attachment(self) -> None:
        body = _function_body(self.chat, "function addClipboardPathAttachments")
        self.assertIn("state.pendingFiles.push", body)
        self.assertIn("path,", body, "必须带 path：路径附件是零拷贝形态（与拖链接/@引用同一路）")
        self.assertIn("renderPendingFiles()", body, "推完必须重渲染待发送列表")

    def test_files_are_deduplicated_against_pending_list(self) -> None:
        body = _function_body(self.chat, "function addClipboardPathAttachments")
        self.assertIn("existing.has(path)", body, "连点两次不该出现两条一样的附件")

    def test_no_probe_cache_so_the_label_cannot_lie(self) -> None:
        """文案与动作都必须按**此刻**的剪贴板算。

        曾经给探测加了 2 秒缓存，真浏览器冒烟当场抓出"文案说谎"：缓存窗口内先复制图片、
        再复制文本，菜单仍写「粘贴图片并上传」（点击时动作是对的，标签是错的）。
        这条源码守卫让"再加回缓存"在单测层面就红，不必等浏览器冒烟。
        """
        self.assertNotIn("nativePasteCache", self.chat, "探测不许缓存：标签与动作都要看当下")
        self.assertNotIn("NATIVE_PASTE_CACHE_MS", self.chat)
        probe = _function_body(self.chat, "async function probeNativeClipboard")
        self.assertIn("await bridge.naibaClipboardPayload()", probe, "每次都要真的问一次")

    # ---- ⑥ 没有桥时必须退回原有文本通道（手机口径不变）----
    def test_no_bridge_falls_back_to_the_text_channel(self) -> None:
        body = _function_body(self.chat, "async function probeNativeClipboard")
        self.assertIn("if (!bridge) return null", body)
        self.assertIn("navigator.clipboard?.readText", self.core, "文本通道要原样保留")
        self.assertIn("PASTE_UNAVAILABLE_HINT", self.core, "不可用时仍要给可行动作")


class ContextMenuHintTests(unittest.TestCase):
    """原有的手机提示口径不能被这次改动带坏（frozen_mobile_ui_check 也会核对打包资源）。"""

    def test_hint_keeps_actionable_wording(self) -> None:
        core = _source("public", "js", "01-core.js")
        self.assertIn("PASTE_UNAVAILABLE_HINT", core)
        self.assertIn("Ctrl+V", core)
        self.assertIn("长按", core)


if __name__ == "__main__":
    unittest.main()
