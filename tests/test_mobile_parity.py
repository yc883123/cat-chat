# -*- coding: utf-8 -*-
"""手机端「功能对等」守门：手机不是「删掉能力的缩小版电脑」。

需求（用户原话归纳）：手机经局域网打开后，电脑端能做的事手机都能做，只是换形态——
侧栏变抽屉、顶栏精简、输入区单行、文件面板变全屏抽屉、弹层近全屏。

本次修掉的根因（三处「按视口删能力」）：
1. `styles.css` 760 块里 `.file-panel … { display: none !important; }` 把整个文件面板关掉；
2. 同一块里 `.file-change-chip { pointer-events: none; }` 让消息末尾的文件条目点不动；
3. `14-file-panel.js` 的 `filePanelUsable()` 按 `window.innerWidth > 760` 直接判死。

不变量：
- 760 块里不得再有「删能力」规则，必须换成形态（全屏抽屉 + 可点文件条目）；
- 触摸目标不小于 44px（顶栏按钮 / 抽屉开关 / 输入区图标与发送 / 文件面板关闭）；
- 视口边界只有一个来源：JS 侧统一取 `NARROW_VIEWPORT_MAX`，与 CSS 的 760px 一致；
- 顶栏操作区的文字标签一律保留（§九.44：宁可整行换行，也不隐藏标签）；
- 设置弹窗的布局列只许写在媒体查询里（见 test_settings_layout_columns_stay_breakpoint_scoped）。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class MobileParityTests(unittest.TestCase):
    def _css(self) -> str:
        return (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def _file_panel(self) -> str:
        return (ROOT / "public/js/14-file-panel.js").read_text(encoding="utf-8")

    def _iter_rules(self, css: str):
        """逐条吐出 (selector, body, in_media)。

        只做 CSS 解析里最必要的两件事：花括号配对 + 记住自己是否落在 @media 里。
        注释当空白，够本文件用。
        """
        stack: list = []
        buf: list = []
        index = 0
        while index < len(css):
            if css.startswith("/*", index):
                index = css.index("*/", index) + 2
                buf.append(" ")
                continue
            char = css[index]
            if char == "{":
                selector = "".join(buf).strip()
                outer = stack[-1]["media"] if stack else False
                stack.append({
                    "selector": selector,
                    "body": [],
                    "media": outer or selector.startswith("@media"),
                })
                buf = []
            elif char == "}":
                frame = stack.pop() if stack else None
                if frame is not None and not frame["selector"].startswith("@"):
                    yield frame["selector"], "".join(frame["body"]), frame["media"]
                buf = []
            else:
                if stack:
                    stack[-1]["body"].append(char)
                buf.append(char)
            index += 1

    def _mobile_block(self) -> str:
        css = self._css()
        block = css[css.index("@media (max-width: 760px) {"):]
        return block[: block.index("\n}")]

    def _rule(self, selector: str) -> str:
        """取该选择器在 760 块里的第一条规则。"""
        return self._rules(selector)[0]

    def _rules(self, selector: str) -> list:
        """同一个选择器在 760 块里可能有多条（例如 #openSidebar 先定位、后补触摸尺寸）。"""
        mobile = self._mobile_block()
        found = []
        start = 0
        while True:
            index = mobile.find(selector, start)
            if index < 0:
                return found
            end = mobile.index("}", index)
            found.append(mobile[index:end])
            start = end + 1

    def test_styles_css_braces_are_balanced(self) -> None:
        """整份 styles.css 必须花括号配平，且第一个 760 块必须真的在本文件内闭合。

        教训（§九.135）：曾经在注释里写了「… 第一个 @media (max-width: 760px) { …」，
        而几个守门是按**花括号配平**找块尾的、**不认注释** ⇒ 那个裸 `{` 让 1844 行之后的
        全部内容都被吞进了第一个手机块；真正报出来的却是另一个用例
        （`test_interjection_edit_actions` 的「所在的 @media 块没有闭合」），定位成本极高。
        这条把根因直接钉死：配平 + 块尾位置，报错就能一眼指到成因。
        """
        css = self._css()
        opens, closes = css.count("{"), css.count("}")
        self.assertEqual(
            opens,
            closes,
            "styles.css 花括号不配平：%d 个 { / %d 个 }（注释里写了裸 { 也会被算进来）"
            % (opens, closes),
        )
        depth = 0
        start = css.index("@media (max-width: 760px) {")
        closed_at = None
        for index in range(css.index("{", start), len(css)):
            char = css[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    closed_at = index
                    break
        self.assertIsNotNone(closed_at, "第一个手机块在本文件内没有闭合（多半是注释里多了个裸 {）")
        self.assertLess(
            css[:closed_at].count("\n") + 1,
            len(css.split("\n")),
            "第一个手机块不该一直拖到文件末尾",
        )

    def test_mobile_block_no_longer_removes_capabilities(self) -> None:
        mobile = self._mobile_block()
        self.assertNotIn("display: none !important", mobile, "又用 !important 把能力关掉了")
        self.assertNotIn("pointer-events: none", mobile, "又用 pointer-events 把能力关掉了")

    def test_settings_layout_columns_stay_breakpoint_scoped(self) -> None:
        """`.settings-layout` 的列定义只许「裸写且只有一个类名」，或住在媒体查询里。

        教训（2026-09-24，设置页视觉重设计）：桌面段新写了一条
        `.settings-dialog .settings-layout { grid-template-columns: 212px minmax(0, 1fr); }`，
        权重高过手机块里的 `.settings-layout { grid-template-columns: minmax(0, 1fr); }`。
        **媒体查询不加权重**，所以 760px 以下仍是「212px 侧栏 + 被挤扁的内容列」：
        390px 竖屏下字号滑杆只剩 22px 宽、字体下拉只剩 112px，人和守门都拖不动。

        判据用「权重」而不是「顺序」：基础规则 `.settings-layout`（1 个类名）能靠
        「手机块更靠后」稳定取胜，是允许的；一旦叠了 `.settings-dialog` 前缀变成 2 个类名，
        顺序就救不回来了，必须显式套 min-width。
        """
        css = self._css()
        rules = list(self._iter_rules(css))
        targets = [
            (selector, body, in_media)
            for selector, body, in_media in rules
            if ".settings-layout" in selector and "grid-template-columns" in body
        ]
        # 先断言前提成立：基础那条与桌面那条都得真在，否则这条守门等于空转
        self.assertTrue(
            any(not in_media and "200px" in body for _, body, in_media in targets),
            "没找到基础 `.settings-layout` 列定义，守门前提不成立",
        )
        self.assertTrue(
            any("212px" in body for _, body, in_media in targets),
            "没找到桌面端 `.settings-layout` 列定义，守门前提不成立",
        )

        mobile_start = css.index("@media (max-width: 760px) {")
        offenders = []
        for selector, _body, in_media in targets:
            if in_media:
                continue
            if self._selector_weight(selector) >= 2:
                offenders.append(selector)
            elif css.find(selector + " {") > mobile_start:
                offenders.append("%s（裸写在手机块之后，同样会压过去）" % selector)
        self.assertEqual(
            offenders,
            [],
            "`.settings-layout` 的列定义压过了手机块的单列写法，760px 以下内容列会被挤扁：%s"
            % offenders,
        )

    @staticmethod
    def _selector_weight(selector: str) -> int:
        """粗略权重：id 记 2，类 / 属性 / 伪类各记 1。这里只比较「谁更具体」，够用。"""
        weight = 0
        for token in re.findall(r"#[-\w]+|\.[-\w]+|\[[^\]]*\]|:{1,2}[-\w]+", selector):
            weight += 2 if token.startswith("#") else 1
        return weight

    def test_file_panel_becomes_fullscreen_drawer(self) -> None:
        panel = self._rule(".file-panel, .app-shell.file-panel-open .file-panel {")
        self.assertIn("position: fixed", panel, "手机端文件面板必须是浮层形态")
        self.assertIn("inset: 0", panel, "全屏")
        self.assertIn("width: 100%", panel)
        self.assertNotIn("!important", panel)
        mobile = self._mobile_block()
        self.assertIn(".app-shell.file-panel-open .file-panel { display: flex; }", mobile, "打开时要能显示")
        self.assertIn(".file-panel-resizer { display: none; }", mobile, "手机上不拖拽调宽")

    def test_file_change_chip_is_clickable_again(self) -> None:
        chip = self._rule(".file-change-chip {")
        self.assertNotIn("pointer-events", chip)
        self.assertIn("min-height", chip, "要给它可点的触摸高度，不能再当纯文本")

    def test_touch_targets_are_at_least_44px(self) -> None:
        for selector in (
            ".mcp-button, .control-button {",
            "#openSidebar {",
            ".composer .icon-button, .composer .send-button {",
            ".file-panel-close {",
        ):
            with self.subTest(selector=selector):
                rules = self._rules(selector)
                self.assertTrue(rules, f"{selector} 在 760 块里不存在")
                self.assertTrue(
                    any("44px" in rule for rule in rules),
                    f"{selector} 的触摸目标小于 44px：{rules}",
                )

    def test_viewport_boundary_has_a_single_source(self) -> None:
        source = self._file_panel()
        self.assertIn("export const NARROW_VIEWPORT_MAX = 760;", source)
        self.assertNotIn("innerWidth > 760", source, "边界必须走 NARROW_VIEWPORT_MAX（与 CSS 的 760 只有一个来源）")
        usable = source[source.index("export function filePanelUsable()"):]
        usable = usable[: usable.index("\n}")]
        self.assertIn("return !!$('#filePanel')", usable, "能不能用只看面板是否存在")
        sidebar = source[source.index("export function sidebarDesktop()"):]
        sidebar = sidebar[: sidebar.index("\n}")]
        self.assertIn("NARROW_VIEWPORT_MAX", sidebar)

    def test_labels_stay_visible_on_mobile(self) -> None:
        """§九.44：顶栏操作区宁可整行换行，也不隐藏文字标签。"""
        css = self._css()
        self.assertNotIn("#openSkills .button-label", css)
        self.assertNotIn("#openTasks .button-label", css)
        self.assertNotIn(".mcp-button span { display: none; }", css)

    def test_file_panel_is_not_closed_when_crossing_the_boundary(self) -> None:
        """跨 760px 不再顺手关掉文件面板（那正是手机端「没有打开文件能力」的来源）。"""
        bind = (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")
        self.assertIn("if (filePanelState.open) applyFilePanelOpenClass();", bind)
        self.assertNotIn("else if (filePanelState.open)", bind, "窄屏不再收起右侧栏")
        self.assertNotIn("closeFilePanel(); // 窄屏收起右侧栏", bind, "跨边界不再把面板关掉")


if __name__ == "__main__":
    unittest.main()
