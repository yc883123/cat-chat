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
        """顶栏操作区吃左段余量，但**不得被压到内容宽度以下**——
        按钮一律 flex:none、文字不换行（否则窗口偏窄时会被挤成竖排文字）。

        3.3.2 修正：这里的 min-width 从 `0` 改成了 `min-content`。旧的 `0` 让操作区可以
        被压到比内容还窄，`justify-content:flex-end` 于是把整组按钮**向左溢出**、盖住
        API / Agent（右侧面板一开就复现：1200 实测重叠 77px）。`min-content` 保证
        「按钮一个都不许被压掉」，放不下时由 `.topbar[data-tb]` 两档降级去收内容。
        """
        css = self._css()
        actions = css[css.index(".topbar-actions {"):]
        actions = actions[: actions.index("}")]
        self.assertIn("flex: 1 1 auto", actions, "操作区吃余量")
        self.assertIn(
            "min-width: min-content", actions,
            "操作区的最小宽度必须是 min-content：改成 0 会让按钮组向左溢出盖住 API/Agent",
        )
        self.assertNotIn(
            "min-width: 0", actions,
            "回退到 min-width:0 就是 3.3.1 的挤压 bug（面板一开就重叠）",
        )
        self.assertIn(".topbar-actions > * { flex: none; }", css, "按钮默认不被压缩（保形、贴右缘）")
        # 轮次下拉仍显式写死 flex-basis（auto 会触发 Chromium 收缩不回收），
        # 窄容器档位由 .topbar[data-tb] 覆盖成 96 / 64px。
        self.assertIn(
            ".topbar-actions > .turn-jump-select { flex: 0 100 260px; }",
            css,
            "桌面轮次下拉显式收缩，且 flex-basis 写死（auto 会触发 Chromium 收缩不回收）",
        )
        self.assertIn(
            '.topbar[data-tb="tight"] .topbar-actions > .turn-jump-select { flex: 0 0 96px;',
            css,
            "窄容器第一档必须把轮次下拉收窄，且 flex-shrink 写 0（否则又被压成一根箭头）",
        )
        self.assertIn(
            '.topbar[data-tb="tightest"] .topbar-actions > .turn-jump-select { flex: 0 0 64px;',
            css,
            "窄容器第二档轮次下拉收到 64px（仍要能认出「第 N 轮」）",
        )
        base = css[css.index(".control-button, .mcp-button, .text-button {"):]
        base = base[: base.index("}")]
        self.assertIn("white-space: nowrap", base)
        self.assertNotIn("#openSkills .button-label", css, "不得按 id 单独隐藏 Skill 文字")
        self.assertNotIn("#openTasks .button-label", css)
        self.assertNotIn(".mcp-button span { display: none; }", css)

    def test_narrow_container_tiers_keep_numbers_readable(self) -> None:
        """窄容器降级只许收「有图标」的按钮文字。

        任务 / Skill 是纯数字按钮（`13 任务` / `19 Skill`，没有 svg），一刀切收掉文字后
        只剩裸数字「13」「19」，用户认不出是什么——所以它们带 `.keep` 类。
        """
        css = self._css()
        self.assertIn(
            '.topbar[data-tb="tight"] .topbar-actions .button-label:not(.keep),',
            css,
            "降级规则必须带 :not(.keep)，否则裸数字按钮失去语义",
        )
        index = (ROOT / "public/index.html").read_text(encoding="utf-8")
        for button_id in ('id="openTasks"', 'id="openSkills"'):
            with self.subTest(button=button_id):
                start = index.index(button_id)
                snippet = index[start: start + 260]
                self.assertIn(
                    'class="button-label keep"', snippet,
                    f"{button_id} 的文字标签必须带 keep 类（纯数字按钮，收掉文字就只剩数字）",
                )

    def test_container_tiers_are_container_driven_not_viewport(self) -> None:
        """降级必须按**容器**宽度判（ResizeObserver → data-tb），不能只按视口。

        右侧面板一开，视口没变、主区却被吃掉 280-420px，媒体查询一条都不命中。
        """
        core = (ROOT / "public/js/01-core.js").read_text(encoding="utf-8")
        self.assertIn("export function watchTopbarWidth()", core)
        self.assertIn("export function applyTopbarWidth(width)", core)
        self.assertIn("new ResizeObserver(", core)
        self.assertIn('$(".chat-main")', core.replace("'", '"'), "观察对象必须是主区那一格")
        self.assertIn("TOPBAR_TIGHT_MAX = 800", core)
        self.assertIn("TOPBAR_TIGHTEST_MAX = 700", core)
        bind = self._bind()
        self.assertIn("watchTopbarWidth();", bind, "初始化时必须接线，否则首屏不降级")

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


class StudioEntryTests(unittest.TestCase):
    """工作台入口在侧栏模式切换里，不在顶栏会话操作区。

    工作台是应用级视图：它替换整个主区、侧栏也跟着换导航，与会话操作（文件 / 辅助 /
    任务 / Skill / MCP / 刷新）不是同类，挤在顶栏既占宽度又语义不清。
    """

    def _index(self) -> str:
        return (ROOT / "public/index.html").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def test_entry_is_unique_and_lives_in_sidebar(self) -> None:
        index = self._index()
        self.assertEqual(index.count('id="openStudio"'), 1, "全页只能有一个工作台入口（不保留隐藏旧按钮）")
        header = index[index.index('<header class="topbar">'):index.index("</header>")]
        self.assertNotIn('id="openStudio"', header, "工作台入口不能留在顶栏")
        sidebar = index[index.index('<aside class="sidebar"'):index.index("</aside>")]
        self.assertIn('id="openStudio"', sidebar, "工作台入口在侧栏")
        self.assertIn('id="openChatMode"', sidebar, "侧栏必须有独立的「对话」返回按钮")

    def test_studio_mode_swaps_sidebar_navigation(self) -> None:
        index = self._index()
        sidebar = index[index.index('<aside class="sidebar"'):index.index("</aside>")]
        self.assertIn('id="sidebarStudioNav"', sidebar, "工作台态需要独立侧栏导航")
        self.assertIn('id="studioPages"', sidebar, "左侧栏承担功能分页导航（当前只有提词库）")
        self.assertIn('data-studio-page="library"', sidebar, "分页「提词库」在左侧栏")
        self.assertIn("提词库", sidebar)
        self.assertNotIn('id="studioNavHistory"', sidebar,
                         "历史产出已改成结果区图标按钮，不再占侧栏导航")
        self.assertNotIn('id="studioNavTemplates"', sidebar, "「我的模板」快捷入口已按用户要求去掉")
        self.assertIn('body[data-studio="on"] #sidebarWorkspaceTree { display: none; }', self._css(),
                      "工作台态必须隐藏会话树")

    def test_studio_is_an_independent_module_shell(self) -> None:
        """工作台是独立模块：进入后整个对话外壳退场，由模块自己的顶栏接管。

        之前把工作台塞进 chat-main 的第 2-4 行，对话的 API / Agent / 文件 / 辅助 /
        任务 / Skill / MCP 就会一直悬在工作台上方，不符合独立模块的分层，
        也让后续在工作台里加分页时无处安放。
        """
        css = self._css()
        rule = css[css.index('body[data-studio="on"] .chat-backdrop'):]
        rule = rule[: rule.index("}") + 1]
        self.assertIn('body[data-studio="on"] .topbar', rule, "工作台态必须让对话顶栏整体退场")
        self.assertIn("display: none", rule)
        self.assertIn("body[data-studio=\"on\"] .chat-main { grid-template-rows: minmax(0, 1fr); }", css,
                      "对话顶栏退场后主区只占一格，工作台铺满")
        index = self._index()
        studio = index[index.index('<section class="studio-view"'):index.index('<section class="composer-wrap"')]
        sidebar = index[index.index('<aside class="sidebar"'):index.index("</aside>")]
        self.assertIn('id="studioPages"', sidebar, "分页导航已移到左侧栏（模块级顶栏只留模块名与工具）")
        self.assertIn('data-studio-page="library"', sidebar, "分页「提词库」缺失")
        self.assertIn("提词库", sidebar)
        self.assertNotIn('id="studioPages"', studio, "工作台顶栏不再堆分页按钮")
        self.assertNotIn('id="modelSelect"', studio, "工作台顶栏不放对话模型选择")
        self.assertNotIn('id="agentSelect"', studio, "工作台顶栏不放对话 Agent 选择")
        # 历史产出：图标按钮在结果区动作行（紧接着「追问」「定位」两枚文字按钮），不是分页。
        self.assertNotIn('data-studio-page="history"', studio, "历史产出不该是工作台分页")
        self.assertIn('id="studioHistoryList"', studio, "历史列表仍在结果栏")
        self.assertIn('id="studioOpenHistory"', studio, "历史产出入口在结果区动作行")
        self.assertIn(">追问<", studio, "继续追问按钮文案已简化为「追问」")
        self.assertIn(">定位<", studio, "定位模板按钮文案已简化为「定位」")
        # 这些控件只是互斥选择，不是 tab；不要再声明需要方向键/roving tabindex 的 tabs 语义。
        self.assertNotIn('role="tablist"', studio, "工作台控件不能声明未实现的 tabs 语义")
        self.assertNotIn('role="tab"', studio, "工作台控件不能声明未实现的 tabs 语义")

    def test_studio_keeps_a_desktop_sidebar_recovery_path(self) -> None:
        """工作台隐藏对话顶栏后，桌面侧栏收起必须在工作台内恢复。

        对话顶栏的 #expandSidebar 随顶栏一起退场；若工作台不补入口，收起侧栏后
        就只剩刷新页面一条路，用户无法回到对话或历史产出。
        """
        index = self._index()
        studio = index[index.index('<section class="studio-view"'):index.index('<section class="composer-wrap"')]
        self.assertIn('id="studioExpandSidebar"', studio, "工作台顶栏必须有桌面侧栏展开入口")
        js = (ROOT / "public/js/22-studio.js").read_text(encoding="utf-8")
        self.assertIn('$("#studioExpandSidebar")?.addEventListener', js, "展开入口必须接线")
        self.assertIn("setLeftSidebarCollapsed(false)", js, "展开入口必须真正取消侧栏折叠")

    def test_studio_button_is_not_a_toggle(self) -> None:
        """再点「工作台」不退出；返回对话只由「对话」按钮负责。"""
        studio = (ROOT / "public/js/22-studio.js").read_text(encoding="utf-8")
        self.assertIn('export function openStudioView()', studio)
        self.assertIn("if (studio.open) return;", studio, "重复进入不得反向退出")
        self.assertIn('$("#openChatMode")?.addEventListener', studio, "「对话」按钮必须接线")


if __name__ == "__main__":
    unittest.main()
