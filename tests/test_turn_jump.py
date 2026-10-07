# -*- coding: utf-8 -*-
"""手机端轮次下拉守门（桌面刻度轨的等价形态）。

需求（用户原话归纳）：电脑端消息区右缘那道「每个用户轮次一条小横杠」的刻度轨，手机上换成
顶栏一个轮次下拉——列出每一轮（「第 N 轮 · 用户消息摘要」），选中即滚到该轮；滚动时下拉
自动显示当前所在轮次。电脑端刻度轨原样保留；**电脑端同样提供这个下拉**（方案 A：落在顶栏
左段中段，Agent 之后、[文件] 之前），两处共用同一个 `#turnJumpSelect` 元素。

不变量：
1. `#turnJumpSelect` 在 `.topbar-actions` 内、初始 `hidden`，手机 / 桌面共用一个元素：
   手机端显示规则写在既有 760 块内（顶到操作区行首），桌面端由基态规则 + `margin-right:auto`
   落在左段中段（视口 ≥1000px 时显示，761–999px 顶栏放不下则收起、只留刻度轨）；
   不得再挂 `.mobile-only`（那会让桌面端也藏起来）；
2. 实现落在 `04-messages.js` **内部**，复用既有 `collectTurns()` / `turnRailTurns` /
   `turnRailActive` / `scrollToTurn()`——不复制算法、不导出内部状态、不新开模块；
3. options 只在**总轮数**变化时重建（教训 §九.37），轮数没变只就地改文案；
4. 跳转后的平滑滚动期间不回写选中项（否则会被「中途轮次」抢走），落点再同步一次；
5. 跳转到未渲染轮次必须走 `scrollToTurn()`（内部先扩懒加载窗口），不得自算 scrollTop（§九.56）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TurnJumpTests(unittest.TestCase):
    def _index(self) -> str:
        return (ROOT / "public/index.html").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def _messages(self) -> str:
        return (ROOT / "public/js/04-messages.js").read_text(encoding="utf-8")

    def test_select_lives_in_topbar_actions_and_shows_on_both_layouts(self) -> None:
        index = self._index()
        self.assertIn(
            '<select class="turn-jump-select" id="turnJumpSelect"',
            index,
            "手机 / 桌面共用同一个轮次下拉元素",
        )
        self.assertNotIn(
            'class="mobile-only turn-jump-select"',
            index,
            "桌面端也要显示轮次下拉，不能再挂 .mobile-only",
        )
        actions = index[index.index('class="topbar-actions"'):]
        actions = actions[: actions.index("</header>")]
        self.assertIn('id="turnJumpSelect"', actions, "轮次下拉要放进顶栏操作区")
        start = index.index('<select class="turn-jump-select"')
        markup = index[start: index.index(">", start) + 1]
        self.assertIn("hidden", markup, "初始 hidden（不足 2 轮时不显示）")
        css = self._css()
        self.assertIn(".turn-jump-select[hidden] { display: none; }", css, "hidden 必须能压过显示规则")
        # 桌面端（第一个手机块之外）必须有显示规则，且靠 margin-right:auto 落在左段中段。
        base = css[: css.index("@media (max-width: 760px) {")]
        self.assertIn(".turn-jump-select {", base, "桌面端要有自己的显示规则")
        self.assertIn("margin-right: auto", base, "桌面端靠 margin-right:auto 顶到操作区行首（左段中段）")
        # 桌面端收缩规则：flex-basis 必须写死 260px，不能 auto——Chromium 下 <select> 的 auto 基准
        # 会取「上次渲染宽度」，窗口一缩被压小后即使再放大也回不来（实测 1440→1100→1440 恒为 0）。
        self.assertIn(".topbar-actions > .turn-jump-select { flex: 0 100 260px; }", css)
        # 窄桌面（761–999px）：顶栏被 API + Agent + 4 个按钮占满，下拉会被压成废桩 → 收掉只留刻度轨。
        narrow = css[css.index("@media (min-width: 761px) and (max-width: 999px) {"):]
        narrow = narrow[: narrow.index("\n}")]
        self.assertIn(".turn-jump-select { display: none; }", narrow, "窄桌面收掉下拉，让位刻度轨")
        # 手机端形态不动：显示规则仍写在既有 760 块内。
        mobile = css[css.index("@media (max-width: 760px) {"):]
        mobile = mobile[: mobile.index("\n}")]
        self.assertIn(".turn-jump-select {", mobile, "显示规则写在既有 760 块内")

    def test_logic_stays_inside_messages_module(self) -> None:
        source = self._messages()
        for name in ("function renderTurnJump(", "function syncTurnJump(", "function applyTurnJump("):
            with self.subTest(function=name):
                self.assertIn(name, source)
                self.assertNotIn(f"export {name}", source, "内部状态不外泄")
        self.assertIn("renderTurnJump(turnRailTurns, active)", source, "共用刻度轨的 turnRailTurns / active")
        apply_body = source[source.index("function applyTurnJump("):]
        apply_body = apply_body[: apply_body.index("\n}")]
        self.assertIn("scrollToTurn(index)", apply_body, "必须复用 scrollToTurn（内含懒加载扩窗口）")
        self.assertNotIn("scrollTop", apply_body, "不得自算滚动位置")

    def test_options_rebuilt_only_when_turn_count_changes(self) -> None:
        source = self._messages()
        render = source[source.index("function renderTurnJump("):]
        render = render[: render.index("\n// 只挪选中项")]
        self.assertIn("select.options.length !== turns.length", render, "轮数没变就不重建 DOM")
        self.assertIn("select.replaceChildren()", render)
        self.assertIn("第 ${index + 1} 轮", source, "选项文案 = 第 N 轮 · 用户消息摘要")

    def test_option_label_is_truncated_summary(self) -> None:
        # 原生 <select> 弹层宽度=最长 option 且无法 CSS 限宽：整段原文会把所有条目撑成
        # 超宽行（2026-10-07 本人实测截图）。选项文案必须在 turnJumpLabel 里截断。
        source = self._messages()
        self.assertIn("const TURN_JUMP_TEXT_LIMIT = 24", source, "摘要上限常量（要调只改一处）")
        label = source[source.index("function turnJumpLabel("):]
        label = label[: label.index("\n}")]
        self.assertIn("replace(/\\s+/g, ' ')", label, "换行/连续空白先归一，不浪费摘要字数")
        self.assertIn("[...raw]", label, "按码点截断（emoji 不被拦腰切碎）")
        self.assertIn("TURN_JUMP_TEXT_LIMIT", label, "超上限必须截断加省略号")
        self.assertIn("'…'", label)

    def test_hidden_below_two_turns_and_suppressed_while_jumping(self) -> None:
        source = self._messages()
        self.assertIn("function hideTurnJump()", source)
        rail = source[source.index("function renderTurnRail()"):]
        rail = rail[: rail.index("\n}")]
        self.assertIn("hideTurnJump();", rail, "只有一轮时不显示（与刻度轨一致）")
        sync = source[source.index("function syncTurnJump("):]
        sync = sync[: sync.index("\n}")]
        self.assertIn("turnJumpSuppressUntil", sync, "跳转滚动期间不回写选中项")
        init = source[source.index("export function initTurnRail()"):]
        init = init[: init.index("\n}")]
        self.assertIn("addEventListener('change'", init, "change 绑在 initTurnRail 里")
        self.assertNotIn("turnJumpSelect().addEventListener", init, "不要重复取 DOM 各绑一次")


if __name__ == "__main__":
    unittest.main()
