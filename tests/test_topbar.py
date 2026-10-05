# -*- coding: utf-8 -*-
"""顶栏样式守门（任务计数 / Skill 标签 / MCP 指示灯 / 刷新按钮归位）。

背景：用户走查提出四处——①「任务」前的数字是圆角胶囊，像奇怪徽标；②按钮显示
「Skill 8 Skill」（计数里又拼了一次 Skill）；③MCP 后面拖「· 已就绪」等文字；
④「刷新」被藏进「⋯」溢出菜单。本测试把四条不变量钉在源码上。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TopbarStyleTests(unittest.TestCase):
    def _index(self) -> str:
        return (ROOT / "public/index.html").read_text(encoding="utf-8")

    def _settings(self) -> str:
        return (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def _bind(self) -> str:
        return (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")

    def test_overflow_menu_removed_and_reload_moved_back(self) -> None:
        index = self._index()
        self.assertNotIn("topbarMoreButton", index, "「⋯」按钮又回来了")
        self.assertNotIn("topbarOverflowMenu", index, "溢出菜单又回来了")
        actions = index[index.index('class="topbar-actions"'):]
        actions = actions[: actions.index("</header>")]
        self.assertIn('id="reloadPage"', actions, "刷新按钮不在顶栏操作区")
        bind = self._bind()
        self.assertNotIn("topbarMoreButton", bind, "溢出菜单的绑定未清理")
        self.assertIn("$('#reloadPage')?.addEventListener", bind)

    def test_task_count_is_plain_text(self) -> None:
        css = self._css()
        rule = css[css.index("#taskCount {"):]
        rule = rule[: rule.index("}")]
        self.assertNotIn("border-radius", rule, "计数又变回胶囊")
        self.assertNotIn("background", rule, "计数不应有胶囊底色")
        self.assertIn("font-variant-numeric", rule)

    def test_skill_button_has_no_duplicate_prefix(self) -> None:
        self.assertIn("$('#skillCount').textContent = String(state.bootstrap.skills.length);", self._settings())
        self.assertNotIn("`Skill ${state.bootstrap.skills.length}`", self._settings())
        index = self._index()
        self.assertIn('<span id="skillCount">0</span>', index)

    def test_mcp_status_is_color_only(self) -> None:
        source = self._settings()
        render = source[source.index("export function renderMcp()"):]
        render = render[: render.index("\n}\n")]
        self.assertIn("label.textContent = 'MCP';", render, "MCP 文字应恒为「MCP」")
        self.assertNotIn("dot.style.background", render, "状态不应再用内联颜色（走 CSS 类）")
        self.assertIn("disconnected", render, "未连接状态应走 disconnected 类")
        css = self._css()
        self.assertIn(".mcp-button.disconnected i", css)
        self.assertIn(".mcp-button.connected i", css)
        self.assertIn(".mcp-button.error i", css)

    def test_index_html_has_no_duplicate_ids(self) -> None:
        """index.html 不得有重复 id——`$('#x')` 只命中第一个，重复会让另一处被写错。

        真实事故：顶栏任务徽标与「历史数据管理」的运行记录都叫 `taskCount`，
        打开设置页时 `loadStorageStats` 把顶栏按钮写成了「0 条（已结束 0）」。
        """
        import collections
        import re

        html = self._index()
        ids = re.findall(r'\bid="([^"]+)"', html)
        duplicates = {key: count for key, count in collections.Counter(ids).items() if count > 1}
        self.assertEqual(duplicates, {}, f"index.html 存在重复 id：{duplicates}")
        self.assertIn('id="storageTaskCount"', html, "历史数据管理的运行记录应使用独立 id")

    def test_topbar_unload_button_removed(self) -> None:
        """顶栏「卸载模型」按钮已删（本地模型卸载统一在设置页）。"""
        index = self._index()
        self.assertNotIn('id="unloadModel"', index)
        bind = self._bind()
        self.assertNotIn("unloadCurrentModel", bind)
        models = (ROOT / "public/js/07-models-agents.js").read_text(encoding="utf-8")
        self.assertNotIn("unloadCurrentModel", models)
        self.assertNotIn("$('#unloadModel')", models)

    def test_file_button_shares_topbar_style(self) -> None:
        """文件面板按钮必须与其它顶栏按钮同款（此前用侧栏深色底，与浅色主题格格不入）。"""
        css = self._css()
        rule = css[css.index(".file-reopen-button:not([hidden]) {"):]
        rule = rule[: rule.index("}")]
        self.assertNotIn("var(--sidebar)", rule, "又用上侧栏深色底")
        self.assertNotIn("color: var(--sidebar-text)", rule)

    def test_file_button_hidden_really_hides(self) -> None:
        """`[hidden]` 必须显式 display:none。

        真实事故：`.topbar-actions .control-button { display: inline-flex }`（作者样式）会压过
        UA 的 `[hidden] { display: none }`，导致 `updateFileTabsButton()` 里 `button.hidden = true`
        没生效——空会话也常驻一个 78px 的「文件」按钮，在窄桌面顶栏白占位、把轮次下拉挤出左段。
        """
        css = self._css()
        self.assertIn(".file-reopen-button[hidden] { display: none; }", css)
        # 反例护栏：不能只有 :not([hidden]) 那条。
        self.assertIn(".file-reopen-button:not([hidden]) {", css)

    def test_topbar_buttons_do_not_wrap_or_shrink(self) -> None:
        """顶栏操作区吃左段余量且可被压缩，但压缩量只由「轮次下拉」吸收——
        按钮一律 flex:none、文字不换行（否则窗口偏窄时会被挤成竖排文字）。"""
        css = self._css()
        actions = css[css.index(".topbar-actions {"):]
        actions = actions[: actions.index("}")]
        self.assertIn("flex: 1 1 auto", actions, "操作区吃余量、可压缩；压缩量由内部轮次下拉吸收")
        self.assertIn("min-width: 0", actions, "min-width:0 否则操作区不收缩、顶栏溢出右缘")
        self.assertIn(".topbar-actions > * { flex: none; }", css, "按钮默认不被压缩（保形、贴右缘）")
        # 唯一例外：轮次下拉在桌面端显式允许收缩（shrink:100），先截断它、不动按钮。
        self.assertIn(
            ".topbar-actions > .turn-jump-select { flex: 0 100 260px; }",
            css,
            "桌面轮次下拉显式收缩，且 flex-basis 写死（auto 会触发 Chromium 收缩不回收）",
        )
        base = css[css.index(".control-button, .mcp-button, .text-button {"):]
        base = base[: base.index("}")]
        self.assertIn("white-space: nowrap", base)
        self.assertNotIn("#openSkills .button-label", css, "窄屏不应再隐藏 Skill/任务文字标签")
        self.assertNotIn("#openTasks .button-label", css)
        self.assertNotIn(".mcp-button span { display: none; }", css)

    def test_topbar_control_button_icons_are_outlined(self) -> None:
        """搬回操作区的按钮必须有线性 SVG 样式，否则裸 SVG 会渲染成黑色实心块。"""
        css = self._css()
        self.assertIn(".topbar-actions .control-button svg", css)
        rule = css[css.index(".topbar-actions .control-button svg {"):]
        rule = rule[: rule.index("}")]
        self.assertIn("fill: none", rule)
        self.assertIn("stroke: currentColor", rule)

    def test_api_stays_in_topbar_and_model_selector_stays_by_composer(self) -> None:
        """API 负责选连接配置，实际模型在输入区从「当前 API 的模型目录」里选，不再手动填写。"""
        index = self._index()
        topbar = index[index.index('<header class="topbar">'):index.index('</header>')]
        self.assertIn('<span>API</span>', topbar)
        self.assertIn('id="modelSelect"', topbar)
        composer = index[index.index('<div class="composer-meta">'):index.index('</section>', index.index('<div class="composer-meta">'))]
        self.assertIn('id="composerModelSelect"', composer)
        self.assertNotIn('id="composerModelCustom"', index, "手动输入模型名称已移除")
        self.assertNotIn("手动输入模型名称", index)
        bind = self._bind()
        self.assertIn("$('#composerModelSelect').addEventListener('change', saveComposerModelSelection)", bind)
        self.assertNotIn("saveCustomComposerModel", bind, "手动输入的绑定未清理")
        models = (ROOT / "public/js/07-models-agents.js").read_text(encoding="utf-8")
        self.assertNotIn("saveCustomComposerModel", models)
        self.assertNotIn("__custom__", models, "下拉里不应再有「手动输入」分支")
        self.assertIn("已保存：", models, "历史模型不在目录时置顶「已保存：X」，不能被静默换掉")
        self.assertIn("请重新检查模型", models, "历史模型不在最新目录时提示重新检查，不能被静默换掉")
        self.assertIn("composerModelIsValidated", models, "发送前校验模型来自检测目录")
        stream = (ROOT / "public/js/11-run-stream.js").read_text(encoding="utf-8")
        self.assertIn(
            "model_name: composerModelChoice()",
            stream,
            "提交必须发送原始选择（'' = 跟随），否则跟随会被静默固化成覆盖",
        )

    def test_turn_jump_select_lives_in_topbar_actions(self) -> None:
        """轮次下拉在顶栏操作区（手机 / 桌面共用一个元素，桌面落在左段中段），细节见 test_turn_jump.py。"""
        index = self._index()
        actions = index[index.index('class="topbar-actions"'):]
        actions = actions[: actions.index("</header>")]
        self.assertIn('id="turnJumpSelect"', actions, "轮次下拉不在顶栏操作区")


if __name__ == "__main__":
    unittest.main()
