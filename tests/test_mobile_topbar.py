# -*- coding: utf-8 -*-
"""手机端顶栏折叠 + 关闭态弹层幽灵面板守门。

背景（两条一起改的）：

1. **幽灵面板**：`styles.css` 的 `.agent-dialog { … display: flex; … }` 是**作者样式**，按级联的
   origin 规则无条件盖过 UA 样式表的 `dialog:not([open]) { display: none; }`，于是 `#agentDialog`
   在关闭态（无 `[open]`）照样被渲染 —— 非模态 `<dialog>` 走 UA 的 `position: absolute`，静态位置
   落在 `.app-shell`（100dvh、overflow:hidden）之后，成为悬在页面底部的"幽灵"。用户手机端截图：
   浏览器底栏上方露出一条只有「Agent 设置」标题栏的白条；点过 Agent 卡片后
   `showAgentForm()` 的 `focus()` 把文档滚到弹层位置，"偶发"露出来。修法：作者级
   `dialog:not([open]) { display: none; }`（(0,1,1) 高于 `.agent-dialog` 的 (0,1,0)，无需 `!important`）。

2. **顶栏折叠**：手机顶栏在 ≤760px 下要占三行（API / Agent / 操作区 + 折叠条），会话区只剩半屏。
   细条按钮 `#toggleTopbarCompact` 收起后**整条顶栏只剩它自己**（API / Agent 两个下拉、操作区、
   侧栏按钮全部 `display: none`），并把「展开顶栏」文字亮出来。曾经的实现只隐藏 `.topbar-actions`
   并把两个下拉收拢到第 1 行，用户看到的仍是「一行 API + 一行 Agent + 一条箭头」——收起等于没收
   （用户手机截图报障，§九.86）。

关键不变量（改这些地方前先读本文件）：
- 关闭态弹层必须 `display: none`，且该规则**不在任何媒体查询里**、不带 `!important`；
- 折叠条只在手机形态出现（基态 `display: none`），规则全部写在 760 块内、靠类名门控，
  **不得**用 `!important` / `pointer-events` / 隐藏按钮文字来省空间（§九.44 + test_mobile_parity）；
- 折叠条**不在** `.topbar-actions` 内、不带 `.control-button` / `.mcp-button` 类
  （否则 `verify/topbar_smoke.cjs` 会量到收起后 0 宽而报红）；
- 收起是「只留一个入口」而不是「删掉能力」：那个入口必须带可见文字、且点一次全部恢复；
- 折叠默认展开（干净浏览器无 localStorage），状态记在 `naibaChatTopbarCompact`。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MOBILE_MEDIA = "@media (max-width: 760px) {"


class GhostDialogTests(unittest.TestCase):
    """关闭的 <dialog> 必须真的消失（作者级 display 会盖掉 UA 的 dialog:not([open])）。"""

    def _css(self) -> str:
        return (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def test_closed_dialog_is_display_none(self) -> None:
        css = self._css()
        self.assertIn(
            "dialog:not([open]) { display: none; }",
            css,
            "关闭态弹层又会被作者级 display 规则渲染成幽灵面板（§手机端幽灵面板）",
        )

    def test_closed_dialog_rule_is_unconditional(self) -> None:
        """这条规则必须在任何媒体查询之外，否则某些视口下幽灵照样出现。"""
        css = self._css()
        self.assertLess(
            css.index("dialog:not([open])"),
            css.index(MOBILE_MEDIA),
            "关闭态规则被放进媒体查询了（应无条件生效）",
        )
        rule = css[css.index("dialog:not([open]) {"):]
        rule = rule[: rule.index("}")]
        self.assertNotIn("!important", rule)

    def test_no_dialog_display_rule_uses_important(self) -> None:
        """`!important` 会反过来压住关闭态规则（origin 相同、特异性更高的反而输）——一律不许用。"""
        css = self._css()
        for match in re.finditer(r"(?m)^[^\n{}]*\bdialog[^\n{}]*\{[^}]*\}", css):
            rule = match.group(0)
            if re.search(r"(?<![\w-])display\s*:", rule):
                self.assertNotIn(
                    "!important", rule,
                    f"弹层 display 不得用 !important（会盖掉 dialog:not([open])）：{rule[:120]}",
                )

    def test_agent_dialog_keeps_its_open_state_layout(self) -> None:
        """修复不得顺手删掉打开态需要的 display: flex / 固定高度。"""
        css = self._css()
        rule = css[css.index(".agent-dialog {"):]
        rule = rule[: rule.index("}")]
        self.assertIn("display: flex", rule, "打开态仍需要纵向 flex 布局")
        self.assertIn("height: min(760px", rule)
        self.assertIn("max-height", rule)

    def test_agent_form_only_focuses_an_open_dialog(self) -> None:
        """往关闭的弹层里 focus() 会把文档滚到弹层位置（幽灵露出的触发条件）。"""
        settings = (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")
        body = settings[settings.index("export function showAgentForm("):]
        body = body[: body.index("\n}")]
        self.assertIn("if (!dialog || dialog.open) $('#agentName').focus();", body)
        self.assertNotIn(
            "\n  $('#agentName').focus();",
            body,
            "无条件 focus 又回来了",
        )


class TopbarCollapseMarkupTests(unittest.TestCase):
    """折叠条的位置与类名（不得进操作区、不得带会被冒烟测量的类）。"""

    def _index(self) -> str:
        return (ROOT / "public/index.html").read_text(encoding="utf-8")

    def _header(self) -> str:
        index = self._index()
        # 字面量必须保留：tests/test_topbar.py 靠它定位顶栏。
        self.assertIn('<header class="topbar">', index, "顶栏 header 字面量被改（test_topbar 依赖它）")
        return index[index.index('<header class="topbar">'): index.index("</header>")]

    def test_toggle_lives_in_topbar_after_actions(self) -> None:
        header = self._header()
        self.assertIn('id="toggleTopbarCompact"', header, "折叠条不在顶栏里")
        actions_start = header.index('class="topbar-actions"')
        actions_end = header.index("</div>", actions_start)
        self.assertNotIn('id="toggleTopbarCompact"', header[actions_start:actions_end],
                         "折叠条不能放进 .topbar-actions（收起后该容器 display:none，冒烟会量到 0 宽）")
        self.assertGreater(header.index('id="toggleTopbarCompact"'), actions_end,
                           "折叠条要排在操作区之后（= 顶栏最后一行）")

    def test_toggle_has_chevron_and_no_button_classes(self) -> None:
        header = self._header()
        start = header.index('<button class="topbar-collapse-toggle"')
        tag = header[start: header.index(">", start) + 1]
        self.assertIn('id="toggleTopbarCompact"', tag)
        self.assertIn('type="button"', tag)
        self.assertIn('aria-expanded="true"', tag, "默认必须是展开态")
        self.assertNotIn("control-button", tag)
        self.assertNotIn("mcp-button", tag)
        self.assertNotIn("mobile-only", tag, "显隐由 .topbar-collapse-toggle 自己的基态规则控制")
        body = header[header.index(">", start): header.index("</button>", start)]
        self.assertIn('M6 15l6-6 6 6', body, "chevron（^）图标缺失")

    def test_no_duplicate_ids(self) -> None:
        ids = re.findall(r'\bid="([^"]+)"', self._index())
        duplicates = {value for value in ids if ids.count(value) > 1}
        self.assertFalse(duplicates, f"index.html 存在重复 id：{sorted(duplicates)}")


class TopbarCollapseStyleTests(unittest.TestCase):
    """折叠规则：手机形态内、类名门控、不按视口删能力。"""

    def _css(self) -> str:
        return (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def _mobile_block(self) -> str:
        css = self._css()
        block = css[css.index(MOBILE_MEDIA):]
        return block[: block.index("\n}")]

    def test_hidden_on_desktop(self) -> None:
        self.assertIn(
            ".topbar-collapse-toggle { display: none; }",
            self._css(),
            "折叠条必须默认隐藏（桌面端顶栏形态完全不变）",
        )

    def test_collapse_rules_live_in_the_mobile_block(self) -> None:
        mobile = self._mobile_block()
        for snippet in (
            ".topbar-collapse-toggle {",
            ".topbar-collapse-toggle svg {",
            ".topbar.compact > *:not(.topbar-collapse-toggle) { display: none; }",
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, mobile, f"折叠规则不在 760 块内：{snippet}")

    def test_compact_hides_every_child_but_the_toggle(self) -> None:
        """收起 = 整条顶栏只剩折叠条（API / Agent / 操作区 / 侧栏按钮一起让位）。

        只隐藏 `.topbar-actions` 是**不够的**：用户看到的仍是「API 栏 + Agent 栏 + 箭头」，
        收起等于没收（手机截图报障）。这条断言就是钉住「一个都不许剩」。
        """
        mobile = self._mobile_block()
        self.assertIn(".topbar.compact > *:not(.topbar-collapse-toggle) { display: none; }", mobile)
        # 收起后单列一行：两个下拉不再需要并排，也不许留下 380 块的三行模板。
        self.assertIn(".topbar.compact { grid-template-columns: 1fr; grid-template-rows: auto; }", mobile)
        self.assertNotIn(".topbar.compact .model-control", mobile, "收起态不得再给下拉单独排格")
        self.assertNotIn(".topbar.compact .agent-control", mobile, "收起态不得再给下拉单独排格")
        self.assertNotIn(".topbar.compact .topbar-actions { display: none; }", mobile)

    def test_toggle_label_only_shows_when_collapsed(self) -> None:
        """收起后顶栏只剩这一个入口，必须靠文字说明「点它能展开」（裸箭头认不出来）。"""
        mobile = self._mobile_block()
        label = mobile[mobile.index(".topbar-toggle-label {"):]
        label = label[: label.index("}")]
        self.assertIn("display: none", label, "展开态要保持细条，不显示文字")
        self.assertIn(".topbar.compact .topbar-toggle-label { display: inline; }", mobile)
        index = (ROOT / "public/index.html").read_text(encoding="utf-8")
        self.assertIn('<span class="topbar-toggle-label">展开顶栏</span>', index)

    def test_toggle_is_full_width_row(self) -> None:
        mobile = self._mobile_block()
        rule = mobile[mobile.index(".topbar-collapse-toggle {"):]
        rule = rule[: rule.index("}")]
        self.assertIn("grid-column: 1 / -1", rule, "要占满一行，不能挤进下拉那一格")
        self.assertNotIn("grid-row", rule, "不要写死 grid-row：760/380 两块都要自然落到最后一行")
        self.assertIn("min-height", rule)

    def test_collapse_does_not_delete_capabilities(self) -> None:
        """与 tests/test_mobile_parity.py 同源：收起是形态变化，不是按视口删能力。"""
        mobile = self._mobile_block()
        self.assertNotIn("display: none !important", mobile)
        self.assertNotIn("pointer-events: none", mobile)

    def test_no_button_label_is_hidden_to_save_space(self) -> None:
        """§九.44：宁可整行换行，也不隐藏按钮文字（含折叠态）。"""
        css = self._css()
        self.assertNotIn("#openSkills .button-label", css)
        self.assertNotIn("#openTasks .button-label", css)
        self.assertNotIn(".mcp-button span { display: none; }", css)
        self.assertNotIn(".button-label", self._mobile_block(), "760 块里不得出现任何按钮文字隐藏规则")

    def test_topbar_button_rules_untouched(self) -> None:
        """新按钮不得动摇顶栏按钮的既有不变量（test_topbar 亦守门，这里就近再钉一次）。"""
        css = self._css()
        actions = css[css.index(".topbar-actions {"):]
        actions = actions[: actions.index("}")]
        self.assertIn("flex: none", actions)
        self.assertIn(".topbar-actions > * { flex: none; }", css)
        buttons = css[css.index(".control-button, .mcp-button, .text-button {"):]
        buttons = buttons[: buttons.index("}")]
        self.assertIn("white-space: nowrap", buttons)


class TopbarCollapseWiringTests(unittest.TestCase):
    """JS 接线：状态读写 + 首次渲染恢复。"""

    def _core(self) -> str:
        return (ROOT / "public/js/01-core.js").read_text(encoding="utf-8")

    def _bind(self) -> str:
        return (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")

    def test_state_api_lives_in_core(self) -> None:
        core = self._core()
        self.assertIn("const TOPBAR_COMPACT_KEY = 'naibaChatTopbarCompact';", core)
        self.assertIn("export function setTopbarCompact(compact)", core)
        self.assertIn("export function restoreTopbarCompact()", core)
        self.assertIn("topbar.classList.toggle('compact', collapsed)", core)
        # 与 setLeftSidebarCollapsed 同一套路：收起写 1、展开删键。
        self.assertIn("localStorage.setItem(TOPBAR_COMPACT_KEY, '1')", core)
        self.assertIn("localStorage.removeItem(TOPBAR_COMPACT_KEY)", core)
        # 存储不可用（隐私模式）不能让折叠本身失效。
        self.assertIn("catch (_error)", core)

    def test_click_binding_and_initial_restore(self) -> None:
        bind = self._bind()
        self.assertIn(
            "$('#toggleTopbarCompact')?.addEventListener('click'", bind,
            "折叠条没有绑定（点击无反应）",
        )
        self.assertIn("setTopbarCompact(!", bind, "点击要按当前状态取反")
        self.assertIn("restoreTopbarCompact", bind.split("from \"./01-core.js\"")[0], "import 清单缺 restoreTopbarCompact")
        tail = bind[bind.index("bindEvents();"):]
        self.assertIn("restoreTopbarCompact();", tail, "首屏没有恢复折叠状态")


if __name__ == "__main__":
    unittest.main()
