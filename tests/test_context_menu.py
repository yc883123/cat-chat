# -*- coding: utf-8 -*-
"""文本框右键菜单的守门：弹层内可见（top layer）+ 不误弹底层会话页菜单。

背景（用户实测两个症状）：
1. **设置页文本框右键"菜单被禁用"**：设置页是模态 `<dialog>`，处于浏览器 **top layer**；
   而 `#textContextMenu` 是 `body` 上的 `position: fixed; z-index: 1000`——弹层永远盖在它
   上面（实测 `elementFromPoint` 命中的是弹层内容，菜单看不见）。
2. **弹层里右键误弹会话页菜单**：模态弹层**不会**清除 `window.getSelection()`，弹层里右键会
   拿着底层会话的残留 `.message-body` 选区，弹出「复制选中 / 快速发送」。

不变量：
- `showTextContextMenu` 必须把菜单挂进 `topLayerContainer()`（最上层模态 dialog，无则 body）；
- `topLayerContainer()` 只认模态 dialog（`:modal`），要排除 toast（它是 `<dialog>` 但用 `show()`）；
- 选区菜单分支必须把选区限制在同一弹层内；
- 编辑菜单条目齐全（撤销/重做/剪切/复制/粘贴/删除/全选）；
- number/email 输入框不暴露 `selectionStart` → 「复制」退化为整值复制；
- **触摸 / 手写笔长按不得被拦截**（§九.85）：手机经局域网 http:// 打开时是**非安全上下文**，
  网页既没有 `navigator.clipboard`、`execCommand('paste')` 也被浏览器禁用——系统长按菜单是手机上
  唯一可行的粘贴入口。旧实现在 `contextmenu` 上无条件 `preventDefault()`，把它一起关掉了，
  用户实测「粘贴失败：浏览器未授权」；
- 「粘贴」按钮在两条程序化通道都不可用时必须给**可行动作**（Ctrl+V / 长按系统菜单），
  不许只说「未授权」。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TextContextMenuTests(unittest.TestCase):
    def _core(self) -> str:
        return (ROOT / "public/js/01-core.js").read_text(encoding="utf-8")

    def _bind(self) -> str:
        return (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def test_menu_is_reparented_into_top_layer_container(self) -> None:
        core = self._core()
        self.assertIn("export function topLayerContainer()", core)
        body = core[core.index("export function showTextContextMenu("):]
        body = body[: body.index("\n}")]
        self.assertIn("topLayerContainer()", body, "菜单必须挂进最上层模态弹层，否则被 top layer 盖住")
        self.assertIn("container.append(menu)", body)

    def test_top_layer_container_only_matches_modal_dialogs(self) -> None:
        core = self._core()
        body = core[core.index("export function topLayerContainer()"):]
        body = body[: body.index("\n}")]
        self.assertIn("dialog[open]", body)
        self.assertIn(":modal", body, "只认 showModal() 打开的对话框")
        self.assertIn("toast", body, ":modal 不可用时必须排除非模态的 toast")

    def test_toast_is_reparented_into_top_layer_container(self) -> None:
        """底部提示框（#toast）也必须挂进最上层模态弹层，否则在设置/Agent 弹层里看不见。"""
        core = self._core()
        body = core[core.index("export function toast("):]
        body = body[: body.index("\n}")]
        self.assertIn("topLayerContainer()", body, "toast 必须挂进最上层模态弹层")
        self.assertIn("container.append(element)", body)
        self.assertIn("element.parentElement !== container", body, "无模态时回到 body")

    def test_selection_menu_scoped_to_same_dialog(self) -> None:
        bind = self._bind()
        body = bind[bind.index("document.addEventListener('contextmenu'"):]
        body = body[: body.index("document.addEventListener('pointerdown'")]
        self.assertIn("editableElement(event.target)", body)
        self.assertIn("dialog.contains(messageBody)", body,
                      "弹层里右键不能拿底层会话的残留选区弹菜单")
        self.assertIn("topLayerContainer() !== document.body", body)
        self.assertIn("topLayerContainer", bind.split("\n")[4], "需要在 import 里引入 topLayerContainer")

    def test_edit_menu_has_all_text_actions(self) -> None:
        core = self._core()
        edit_branch = core[core.index("if (contextMenuMode === 'edit')"):]
        edit_branch = edit_branch[: edit_branch.index("} else {")]
        for action in ("undo", "redo", "cut", "copy", "paste", "delete", "select-all"):
            with self.subTest(action=action):
                self.assertIn(f'data-context-action="{action}"', edit_branch)

    def test_copy_falls_back_to_whole_value_without_selection_api(self) -> None:
        core = self._core()
        body = core[core.index("export function editableSelectedText()"):]
        body = body[: body.index("\n}")]
        self.assertIn("typeof el.selectionStart === 'number'", body)
        self.assertIn("return el.value;", body,
                      "number/email 输入框没有 selectionStart，复制要退化为整值")

    def test_menu_css_stays_fixed_overlay(self) -> None:
        css = self._css()
        rule = css[css.index(".text-context-menu {"):]
        rule = rule[: rule.index("}")]
        self.assertIn("position: fixed", rule)
        self.assertIn("z-index", rule)

    def test_touch_long_press_is_left_to_the_system_menu(self) -> None:
        """手机长按不拦截：系统菜单才是手机上唯一能用的粘贴入口（§九.85）。"""
        bind = self._bind()
        self.assertIn("let lastPointerType = 'mouse';", bind, "没有记录指针类型，长按判定无从谈起")
        self.assertIn("lastPointerType = event.pointerType || 'mouse';", bind)
        self.assertRegex(bind, r"\}, true\);", "指针类型要在捕获阶段记，别被后续 stopPropagation 吞掉")
        body = bind[bind.index("document.addEventListener('contextmenu'"):]
        body = body[: body.index("document.addEventListener('pointerdown'")]
        self.assertIn("isLongPressPointer()", body)
        self.assertLess(
            body.index("isLongPressPointer()"),
            body.index("event.preventDefault()"),
            "长按判定必须排在 preventDefault 之前，否则照样把系统菜单关掉",
        )
        helper = bind[bind.index("function isLongPressPointer()"):]
        helper = helper[: helper.index("\n}")]
        self.assertIn("'touch'", helper)
        self.assertIn("'pen'", helper)

    def test_paste_falls_back_to_an_actionable_hint(self) -> None:
        """两条程序化通道都不可用时，提示必须给出下一步动作。"""
        core = self._core()
        self.assertNotIn("粘贴失败：浏览器未授权", core, "又回到只说「未授权」、不给下一步的老文案")
        self.assertIn("PASTE_UNAVAILABLE_HINT", core)
        hint = core[core.index("const PASTE_UNAVAILABLE_HINT"):]
        hint = hint[: hint.index(";")]
        self.assertIn("Ctrl+V", hint, "桌面通路：要告诉用户按 Ctrl+V")
        self.assertIn("长按", hint, "手机通路：要告诉用户长按用系统菜单")
        paste = core[core.index("} else if (action === 'paste') {"):]
        paste = paste[: paste.index("} else if (action === 'delete')")]
        self.assertIn("toast(ok ? '已粘贴' : PASTE_UNAVAILABLE_HINT);", paste,
                      "成功与失败都要出声，不许出现「点了没反应」")
        self.assertIn("insertTextIntoEditable(text)", paste)


if __name__ == "__main__":
    unittest.main()
