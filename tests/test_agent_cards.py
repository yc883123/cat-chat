# -*- coding: utf-8 -*-
"""Agent 设置页（卡片网格 + 点开才弹出的设置弹层）与「下线内置 Agent」的守门。

背景（两条一起做的改动）：
1. 四个内置 Agent 预设（dsh-standard / dsh-code / dsh-minimal / dsh-cordis）按用户要求下线：
   `built_in_agents()` 清单置空，**机制保留**（built_in 标记 + 不可删守卫 + 前端「内置」徽标）；
   用户配置里遗留的旧内置副本由 `_migrate_agent_builtin_flags()` 摘掉标记，变成普通可删 Agent。
2. Agent 管理页改成与 API 供应商页同款：卡片网格（一行最多三张）+ 末尾「新增 Agent」卡片 +
   卡片右上角 × 删除 + 点卡片才弹出顶层 `<dialog id="agentDialog">`（字段/工具集/Skill 选择器
   保持原样）。

关键不变量：
- 内置清单为空 → 不再注入任何内置 Agent，所有 Agent 都可删除，`upsert_agent` 不再打 built_in；
- 遗留副本只摘标记、不动内容（用户改过的名称/提示词/工具集不丢）；
- 卡片容器 `#agentCards`，旧 `#agentList`/`#addAgent`/`.agent-item*` 连绑定一起消失；
- 表单整体搬进 `#agentDialog`，字段 id 与文案逐字不变；
- 网格三列 + 窄屏 2/1 列。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.config import ConfigStore, built_in_agent_ids, built_in_agents  # noqa: E402

LEGACY_SELECTORS = ("#agentList", "#addAgent", "agent-item", "agent-manager", "data-agent-edit")

FORM_FIELD_IDS = (
    "agentFormId",
    "agentName",
    "agentSystemPromptEdit",
    "agentPromptPresetButton",
    "saveAgentPromptPreset",
    "importAgentCharacterCard",
    "agentCharacterCardFileInput",
    "pickAgentAvatar",
    "agentAvatarFileInput",
    "agentAvatarPreview",
    "agentSkillList",
    "agentToolPresetView",
    "agentToolPresetCards",
    "agentToolPresetState",
    "agentToolEditor",
    "agentToolSetName",
    "agentToolEditorBack",
    "agentToolEditorSave",
    "agentToolCount",
    "toggleAllToolGroups",
    "agentToolUnknownHint",
    "agentToolScope",
    "agentError",
    "cancelAgent",
    "saveAgentForm",
)


class BuiltInAgentsRetiredTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _store(self, payload=None) -> ConfigStore:
        if payload is not None:
            self.config_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        return ConfigStore(self.config_path)

    def test_built_in_list_is_empty_but_mechanism_kept(self) -> None:
        self.assertEqual(built_in_agents(), [])
        self.assertEqual(built_in_agent_ids(), set())

    def test_no_built_in_agent_is_injected(self) -> None:
        store = self._store({"agents": [{"id": "mine", "name": "我的 Agent", "system_prompt": "", "skill_ids": []}]})
        ids = [agent.get("id") for agent in store.public_agents()]
        self.assertEqual(ids, ["mine"])

    def test_legacy_built_in_flag_is_stripped_but_content_kept(self) -> None:
        store = self._store({
            "agents": [
                {
                    "id": "dsh-standard",
                    "name": "dsh-standard（全能）",
                    "system_prompt": "用户改过的提示词",
                    "skill_ids": ["s1"],
                    "tool_scope": ["read_file"],
                    "built_in": True,
                }
            ]
        })
        agent = store.public_agents()[0]
        self.assertNotIn("built_in", agent, "遗留副本必须变成普通 Agent（否则前端仍隐藏删除按钮）")
        self.assertEqual(agent["name"], "dsh-standard（全能）")
        self.assertEqual(agent["system_prompt"], "用户改过的提示词")
        self.assertEqual(agent["tool_scope"], ["read_file"])
        # 落盘也要清掉，避免下次启动重复迁移。
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("built_in", saved["agents"][0])

    def test_former_built_in_agent_is_deletable(self) -> None:
        store = self._store({
            "agents": [
                {"id": "dsh-code", "name": "dsh-code（编程）", "system_prompt": "", "skill_ids": [], "built_in": True},
                {"id": "keep", "name": "保留", "system_prompt": "", "skill_ids": []},
            ],
            "default_agent_id": "keep",
        })
        self.assertTrue(store.delete_agent("dsh-code"), "内置清单为空后旧内置 id 必须可删")
        self.assertEqual([agent.get("id") for agent in store.public_agents()], ["keep"])

    def test_upsert_agent_never_marks_built_in(self) -> None:
        store = self._store({})
        saved = store.upsert_agent({"id": "dsh-standard", "name": "再来一个", "system_prompt": "", "skill_ids": []})
        self.assertNotIn("built_in", saved)
        self.assertNotIn("built_in", store.public_agents()[0])


class ToolGroupCatalogTests(unittest.TestCase):
    """工具分类：单一维度 7 组 + 风险徽标 + MCP 按服务器二级分组（Agent 编辑页可读性的地基）。"""

    EXPECTED_GROUPS = (
        "读取与检索", "文件写入与编辑", "命令与脚本执行",
        "联网与外部服务", "视觉与图片", "任务与扩展", "长会话",
    )

    def _catalog(self, extra_schemas=()):
        from naiba.config import tool_catalog_entries
        from naiba.tools.registry import build_tool_registry

        schemas = list(build_tool_registry().schemas())
        schemas.extend(extra_schemas)
        return tool_catalog_entries(schemas)

    def test_group_order_badge_and_desc(self) -> None:
        from naiba.config import TOOL_GROUP_INFO

        infos = list(TOOL_GROUP_INFO)
        names = [info["name"] for info in infos]
        self.assertEqual(names[: len(self.EXPECTED_GROUPS)], list(self.EXPECTED_GROUPS),
                         "分类顺序即前端展示顺序：7 组 = 「作用对象 + 风险」单一维度")
        for info in infos:
            with self.subTest(group=info["name"]):
                self.assertTrue(str(info["desc"]).strip(), "每个分类都要有一句话说明")
                self.assertIn(info["tone"], ("safe", "warn", "danger", "info"))
        self.assertTrue(dict((i["name"], i["badge"]) for i in infos)["视觉与图片"].strip(),
                        "视觉分类必须有风险徽标")
        # 收敛前的旧分类名不得复活：改名漏改会让 preset 的 group: 引用静默失效。
        for retired in ("文件读取/搜索", "文件写入/编辑", "命令执行", "Skill 脚本", "网络",
                        "会话与记忆", "MCP", "后台/Job/子任务", "ComfyUI", "能力/Skill 管理",
                        "文档（PDF）", "视觉"):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, names)

    def test_every_tool_lands_in_exactly_one_group(self) -> None:
        from naiba.config import tool_group_entries

        entries = self._catalog()
        groups = tool_group_entries(entries)
        self.assertEqual([group["name"] for group in groups], list(self.EXPECTED_GROUPS),
                         "没有未归类工具时「其他」不该出现")
        placed = [tool for group in groups for tool in group["tools"]]
        self.assertEqual(sorted(placed), sorted(entry["name"] for entry in entries),
                         "每个工具必须且只落进一个分类")
        self.assertEqual(len(placed), len(set(placed)))
        # 各分类成员固定：合并后的归属一目了然；调整归属必须同步本断言。
        membership = {group["name"]: group["tools"] for group in groups}
        self.assertEqual(membership["读取与检索"], [
            "read_file", "list_directory", "search_files",
            "read_pdf", "pdf_render_pages", "pdf_zoom_region",
            "probe_video", "extract_frames",
        ], "视频抽帧与 PDF 三件套同组（读元信息 + 抽帧，都是「读取与检索」）")
        self.assertEqual(membership["文件写入与编辑"], ["write_file", "edit_file"])
        self.assertEqual(membership["命令与脚本执行"], ["pwsh", "run_skill_script"])
        self.assertEqual(membership["视觉与图片"], ["vision_analyze", "vision_image_ops"])
        self.assertEqual(membership["联网与外部服务"], [
            "http_request", "web_search", "register_mcp",
            "comfyui_prepare_workflow", "comfyui_batch",
        ])
        self.assertEqual(membership["任务与扩展"], [
            "run_in_background", "job_output", "job_status", "job_wait", "job_kill", "subagent",
            "todo_write", "install_skill", "unpack_skill_archive", "inspect_installed_skill",
        ])
        self.assertEqual(membership["长会话"], [
            "find_conversations", "recall_history", "read_conversation", "reset_context",
        ], "长会话工具集：翻历史三件套 + 重置上下文（成组勾选才有意义）")

    def test_mcp_tools_are_subgrouped_by_server(self) -> None:
        from naiba.config import tool_group_entries

        entries = self._catalog((
            {"name": "mcp__comfy-mcp__system_stats", "description": "查看 ComfyUI 状态"},
            {"name": "mcp__comfy-mcp__run_workflow", "description": "运行工作流"},
            {"name": "mcp__other__ping", "description": "探活"},
        ))
        groups = {group["name"]: group for group in tool_group_entries(entries)}
        net = groups["联网与外部服务"]
        self.assertIn("mcp__comfy-mcp__system_stats", net["tools"],
                      "动态 MCP 工具统一归入「联网与外部服务」")
        self.assertNotIn("mcp__comfy-mcp__system_stats", net["direct_tools"],
                         "带服务器名的工具不进平铺区，只在二级分组里出现")
        self.assertIn("http_request", net["direct_tools"])
        subs = {sub["name"]: sub["tools"] for sub in net["subgroups"]}
        self.assertEqual(list(subs), ["comfy-mcp", "other"], "二级分组按服务器聚合，顺序稳定")
        # MCP 工具未登记在 order 表里 → 组内按名字排序（稳定、可预期）。
        self.assertEqual(subs["comfy-mcp"],
                         ["mcp__comfy-mcp__run_workflow", "mcp__comfy-mcp__system_stats"])
        self.assertEqual(subs["other"], ["mcp__other__ping"])

    def test_presets_are_five_and_closure_complete(self) -> None:
        from naiba.config import (
            TOOL_PRESETS, resolve_tool_preset, tool_group_entries, tool_preset_entries,
        )
        from naiba.run.session import JOB_CREATOR_TOOL_DEPS

        entries = self._catalog()
        known = {entry["name"] for entry in entries}
        known_groups = {group["name"] for group in tool_group_entries(entries)}
        items = tool_preset_entries(entries)
        presets = {item["id"]: set(item["tools"]) for item in items}
        self.assertEqual([item["id"] for item in items],
                         ["readonly", "standard", "longsession", "comfyui", "full"],
                         "预设 5 档：只读 / 标准 / 长会话 / ComfyUI 联动 / 全能（按能力从小到大排）")
        # 引用必须都存在（分类名/工具名写错会静默少选，见 resolve_tool_preset 的告警）。
        for preset in TOOL_PRESETS:
            with self.subTest(preset=preset["id"]):
                for raw in list(preset.get("include") or []) + list(preset.get("exclude") or []):
                    ref = str(raw)
                    if ref.startswith("group:"):
                        group = ref[len("group:"):]
                        self.assertTrue(group == "*" or group in known_groups,
                                        f"预设引用了不存在的分类：{ref}")
                    else:
                        self.assertIn(ref, known, "预设引用的工具名必须仍在工具目录里")
                self.assertTrue(resolve_tool_preset(preset, entries), "预设不能展开成空集")
        # 依赖闭包必须已在预设里显式列出：否则「卡片显示的个数」≠「套用后的实际个数」
        # （前端 openAgentToolEditor 会补闭包，而 matchToolPreset 是精确比对 → 载入即变「自定义」）。
        for preset_id, tools in presets.items():
            with self.subTest(preset=preset_id):
                closed = set(tools)
                for creator, deps in JOB_CREATOR_TOOL_DEPS.items():
                    if creator in closed:
                        closed.update(deps)
                self.assertEqual(closed, set(tools), f"{preset_id} 预设缺少依赖闭包")
        self.assertEqual(presets["readonly"],
                         {"read_file", "list_directory", "search_files", "vision_analyze"})
        self.assertEqual(presets["standard"], {
            "read_file", "list_directory", "search_files", "write_file", "edit_file",
            "pwsh", "run_skill_script", "vision_analyze",
        })
        self.assertFalse(presets["standard"] & {"http_request", "web_search", "read_pdf",
                                                "pdf_render_pages", "pdf_zoom_region"},
                         "标准模式不含联网与 PDF 工具")
        self.assertEqual(presets["longsession"], presets["standard"] | {
            "find_conversations", "recall_history", "read_conversation", "reset_context",
        }, "长会话模式 = 标准模式 + 翻历史三件套 + 重置上下文")
        self.assertFalse(presets["longsession"] & {"http_request", "web_search", "read_pdf",
                                                   "pdf_render_pages", "pdf_zoom_region"},
                         "长会话模式也不含联网与 PDF 工具")
        self.assertEqual(presets["comfyui"], presets["standard"] | {
            "comfyui_prepare_workflow", "comfyui_batch",
            "job_output", "job_status", "job_wait",
        }, "ComfyUI 联动 = 标准模式 + ComfyUI 两个工具 + 依赖的 Job 查询工具")
        self.assertFalse([n for n in presets["comfyui"] if n.startswith("mcp__")],
                         "ComfyUI 预设声明不启用 MCP")
        self.assertEqual(len(presets["full"]), len(known), "全能模式覆盖全部工具")

    def test_default_selected_tools_equal_standard_preset(self) -> None:
        from naiba.config import TOOL_PRESETS, _DEFAULT_SELECTED_TOOLS

        standard = next(preset for preset in TOOL_PRESETS if preset["id"] == "standard")
        self.assertEqual(set(standard["include"]), set(_DEFAULT_SELECTED_TOOLS),
                         "新建 Agent 的默认勾选必须等于标准模式，否则打开表单显示「自定义」")

    def test_frontend_dep_rules_match_backend(self) -> None:
        """前端 AGENT_TOOL_DEP_RULES 必须与后端 JOB_CREATOR_TOOL_DEPS 逐项一致（闭包同源）。"""
        from naiba.run.session import JOB_CREATOR_TOOL_DEPS

        settings = (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")
        body = settings[settings.index("export const AGENT_TOOL_DEP_RULES = {"):]
        body = body[: body.index("};")]
        for creator, deps in JOB_CREATOR_TOOL_DEPS.items():
            with self.subTest(creator=creator):
                match = re.search(rf"{creator}: \[([^\]]*)\]", body)
                self.assertIsNotNone(match, f"前端缺少 {creator} 的依赖规则")
                self.assertEqual(set(re.findall(r"'([^']+)'", match.group(1))), set(deps))

    def test_unknown_preset_reference_logs_warning(self) -> None:
        from naiba.config import resolve_tool_preset

        entries = self._catalog()
        with self.assertLogs("naiba.config", level="WARNING") as captured:
            resolved = resolve_tool_preset(
                {"id": "bad", "include": ["group:不存在的分类", "not_a_tool"]}, entries)
        self.assertEqual(resolved, [])
        self.assertIn("不存在的分类", "\n".join(captured.output))
        self.assertIn("not_a_tool", "\n".join(captured.output))

    def test_tool_group_head_renders_title_and_desc_in_one_line(self) -> None:
        settings = (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")
        body = settings[settings.index("function buildGroupBlock(group, toolMap, list) {"):]
        body = body[: body.index("function emptyScopeHint(")]
        self.assertIn("head.append(caret, allCb, title, desc, count)", body,
                      "小字说明必须排在标题之后、计数之前（一行呈现）")
        self.assertIn("title.append(badge)", body, "风险徽标随分类名同行，不额外占列")
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        head_rule = css[css.index(".agent-tool-group-head {"):]
        head_rule = head_rule[: head_rule.index("}")]
        self.assertIn("auto auto max-content minmax(0, 1fr) auto", head_rule)
        desc_rule = css[css.index(".group-desc {"):]
        desc_rule = desc_rule[: desc_rule.index("}")]
        self.assertIn("white-space: nowrap", desc_rule)
        self.assertIn("text-overflow: ellipsis", desc_rule)
        self.assertNotIn("grid-column", desc_rule, "小字不能再独占第二行")

    def test_frontend_renders_badge_subgroups_and_search(self) -> None:
        """前端接线守门：徽标 / 二级分组 / 搜索框三件套缺一不可（改版的核心可见物）。"""
        settings = (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")
        self.assertIn("export function renderToolScopeList()", settings)
        self.assertIn("buildSubgroupBlock(sub, tools, list)", settings)
        self.assertIn("badge.dataset.tone = group.tone || 'info'", settings)
        self.assertIn("renderFilteredScope(list, catalog, filter)", settings)
        self.assertIn("subgroup-select-all", settings, "二级分组要能整组全选")
        index = (ROOT / "public/index.html").read_text(encoding="utf-8")
        self.assertIn('id="agentToolFilter"', index)
        bind = (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")
        self.assertIn("$('#agentToolFilter')?.addEventListener('input'", bind)
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        for rule in ('.group-badge[data-tone="danger"]', ".tool-subgroup-head {",
                     ".agent-tool-group.collapsed .agent-tool-group-body { display: none; }"):
            with self.subTest(rule=rule):
                self.assertIn(rule, css)


class AgentPromptPresetUiTests(unittest.TestCase):
    """快捷提示词从「设置页」搬到 Agent 表单：另存按钮 + 弹窗 + 带 × 的面板。"""

    def _index(self):
        return (ROOT / "public/index.html").read_text(encoding="utf-8")

    def _js(self):
        return (ROOT / "public/js/08-conversations.js").read_text(encoding="utf-8")

    def _bind(self):
        return (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")

    def test_dialog_has_title_and_body(self):
        index = self._index()
        self.assertIn('id="promptPresetDialog"', index)
        self.assertIn('id="promptPresetTitle"', index)
        self.assertIn('id="promptPresetText"', index, "编辑路径要能改正文")
        self.assertIn('id="savePromptPreset"', index)
        dialog = index[index.index('id="promptPresetDialog"'):]
        dialog = dialog[: dialog.index("</dialog>")]
        self.assertIn("textarea", dialog, "弹窗要能改正文（另存时预填当前系统提示词）")
        js = self._js()
        self.assertIn("export function openAgentPromptPresetEditDialog(", js)
        self.assertIn("state.agentPromptPresetEditingId = id;", js)
        self.assertIn("state.agentPromptPresetEditingId = '';", js, "另存路径要清掉编辑中的 id")

    def test_panel_items_have_edit_and_delete(self):
        js = self._js()
        self.assertIn("data-agent-preset-edit=", js, "面板条目要有 ✎ 编辑")
        self.assertIn("data-agent-preset-delete=", js, "面板条目右侧要有 × 删除")
        self.assertIn("export async function removeAgentPromptPreset(", js)
        self.assertIn("export function handleAgentPromptPresetPanelClick(", js)
        bind = self._bind()
        self.assertIn(
            "$('#agentPromptPresetPanel')?.addEventListener('click', handleAgentPromptPresetPanelClick)", bind)
        self.assertIn(
            "$('#saveAgentPromptPreset')?.addEventListener('click', openAgentPromptPresetSaveDialog)", bind)
        self.assertIn("$('#promptPresetForm')?.addEventListener('submit', saveAgentPromptPreset)", bind)

    def test_panel_lives_inside_dialog_and_is_js_positioned(self):
        """模态 <dialog> 在 top layer：body 上的 fixed 浮层会被盖住，面板必须挂在弹层内部。"""
        index = self._index()
        dialog = index[index.index('<dialog id="agentDialog"'):]
        dialog = dialog[: dialog.index("</dialog>")]
        self.assertIn('id="agentPromptPresetPanel"', dialog)
        js = self._js()
        self.assertIn("panel.style.top =", js, "面板必须 JS 定位（fixed + 视口夹紧）")
        bind = self._bind()
        self.assertIn("positionAgentPromptPresetPanel", bind, "resize 时要重新定位")
        self.assertIn("$('#agentDialog')?.addEventListener('cancel'", bind,
                      "Esc 先收面板，不能把整个 Agent 弹层关掉")


class AgentTabsTests(unittest.TestCase):
    """Agent 弹层分区切换：模块不再往尾部堆叠，顶端按钮切换；操作按钮与分区同排。"""

    def _index(self):
        return (ROOT / "public/index.html").read_text(encoding="utf-8")

    def _settings(self):
        return (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")

    def _bind(self):
        return (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")

    def test_three_tabs_and_panels(self):
        index = self._index()
        tabs = index[index.index('class="agent-tabs"'):]
        tabs = tabs[: tabs.index("</nav>")]
        for name in ("basic", "skills", "tools"):
            with self.subTest(tab=name):
                self.assertIn(f'data-agent-tab="{name}"', tabs)
                self.assertIn(f'data-agent-panel="{name}"', index)
        self.assertEqual(index.count("data-agent-panel="), 3, "只有 3 个分区面板")
        self.assertNotIn('data-agent-panel="prompt"', index, "系统提示词已并入「基本」，不再单独分区")
        # 默认只有第一个分区可见，其余带 hidden。
        self.assertIn('<section class="agent-tab-panel" data-agent-panel="basic" role="tabpanel">', index)
        for name in ("skills", "tools"):
            with self.subTest(hidden=name):
                self.assertIn(f'data-agent-panel="{name}" role="tabpanel" hidden', index)
        # 标签带计数：固定 Skill 个数 / 工具已选/总数
        self.assertIn('id="agentSkillTabCount"', index)
        self.assertIn('id="agentToolTabCount"', index)

    def test_basic_tab_holds_name_avatar_and_prompt(self):
        index = self._index()
        basic = index[index.index('data-agent-panel="basic"'):]
        basic = basic[: basic.index('data-agent-panel="skills"')]
        for field in ("agentName", "pickAgentAvatar", "agentSystemPromptEdit",
                      "agentPromptPresetButton", "saveAgentPromptPreset", "importAgentCharacterCard"):
            with self.subTest(field=field):
                self.assertIn(f'id="{field}"', basic, "基本分区应包含名称/头像/系统提示词")

    def test_actions_pinned_to_tabs_row(self):
        """取消/保存移到分区行右侧：不随面板高度上下跳，底部腾给内容。"""
        index = self._index()
        row = index[index.index('class="agent-tabs-row"'):]
        row = row[: row.index("</div>", row.index("agent-form-actions"))]
        self.assertIn('id="cancelAgent"', row)
        self.assertIn('id="saveAgentForm"', row)
        self.assertNotIn('class="agent-form-footer"', index, "旧底栏已移除")
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        dialog_rule = css[css.index(".agent-dialog {"):]
        dialog_rule = dialog_rule[: dialog_rule.index("}")]
        self.assertNotIn("--agent-dialog-h", dialog_rule, "弹层高度固定，不再随分区/状态变")
        self.assertNotIn("transition: height", dialog_rule, "高度不参与过渡（否则卡片态切换会跳）")
        self.assertIn("height: min(760px", dialog_rule)

    def test_switch_resets_and_updates_counts(self):
        settings = self._settings()
        self.assertIn("export function switchAgentTab(name)", settings)
        self.assertIn("switchAgentTab('basic')", settings, "打开表单复位到基本分区")
        self.assertIn("export function updateAgentSkillTabCount()", settings)
        self.assertIn("closeAgentPromptPresetPanel();", settings, "切页要收起快捷提示词面板")
        bind = self._bind()
        self.assertIn("$$('.agent-tabs button[data-agent-tab]').forEach", bind)
        self.assertIn("switchAgentTab(button.dataset.agentTab)", bind)
        self.assertIn("updateAgentSkillTabCount();", bind, "勾选 Skill 后标签计数要刷新")


class AgentToolSetCardsTests(unittest.TestCase):
    """工具集：卡片态（预设 + 我的工具集 + 添加卡）↔ 编辑态（横条 + 工具列表）。"""

    def _index(self):
        return (ROOT / "public/index.html").read_text(encoding="utf-8")

    def _settings(self):
        return (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")

    def _bind(self):
        return (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")

    def _css(self):
        return (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def test_card_view_and_editor_are_separate(self):
        index = self._index()
        self.assertIn('id="agentToolStage"', index, "两态同框叠放在 .tool-stage 里")
        self.assertIn('id="agentToolPresetView"', index)
        self.assertIn('id="agentToolPresetCards"', index)
        self.assertIn('id="agentToolEditor" hidden', index, "编辑态默认隐藏")
        stage = index[index.index('id="agentToolStage"'):]
        stage = stage[: stage.index("</section>")]
        self.assertLess(stage.index('id="agentToolPresetView"'), stage.index('id="agentToolEditor"'))
        # 旧的下拉框与模板芯片行已退役。
        for snippet in ("agentToolPresetSelect", "agentToolTemplateName", "agentToolTemplateSave",
                        "agentToolTemplateRow", "agentToolTemplates"):
            with self.subTest(snippet=snippet):
                self.assertNotIn(snippet, index, "旧的预设下拉/模板芯片行残留")

    def test_cards_markup_and_readonly_presets(self):
        js = self._settings()
        self.assertIn("export function renderAgentToolPresetCards()", js)
        self.assertIn('data-tool-preset-card=', js)
        self.assertIn('data-tool-template-card=', js)
        self.assertIn("data-tool-preset-add", js, "末尾要有「添加自定义工具集」卡")
        self.assertIn("data-tool-template-del=", js, "我的工具集卡片要有 × 删除")
        # 预设卡不给删除入口（只读）。
        body = js[js.index("export function renderAgentToolPresetCards()"):]
        body = body[: body.index("// 勾选变化时刷新")]
        preset_block = body[body.index("const presetCards"): body.index("const templateCards")]
        self.assertNotIn("tool-preset-card-del", preset_block, "内置预设卡不得给删除按钮")

    def test_editor_flow_and_closure(self):
        js = self._settings()
        self.assertIn("export function openAgentToolEditor(", js)
        self.assertIn("export function closeAgentToolEditor()", js)
        self.assertIn("export async function saveAgentToolSet()", js)
        self.assertIn("export function handleAgentToolPresetCardsClick(", js)
        self.assertIn("export function handleAgentToolPresetCardsKeydown(", js)
        # 点卡片必须载入该卡的工具并走依赖闭包（否则卡片显示个数 ≠ 实际放行个数）。
        editor = js[js.index("export function openAgentToolEditor("):]
        editor = editor[: editor.index("\n}")]
        self.assertEqual(editor.count("normalizeToolScope("), 2, "预设卡与「我的工具集」卡都要补闭包")
        self.assertIn("base.tools", editor)
        self.assertIn("usableTemplateTools(template)", editor)
        # 命名栏预填卡片名：预设卡用预设名、「我的工具集」卡用它自己的名字、「添加」卡留空。
        self.assertIn("nameInput.value = preset ? preset.name", editor)
        # 「添加」卡不跟随当前选中项：固定以「标准模式」为起点。
        self.assertIn("DEFAULT_TOOL_SET_PRESET_ID = 'standard'", js)
        self.assertIn("presets.find((item) => item.id === DEFAULT_TOOL_SET_PRESET_ID)", editor)        # 三张入口（预设 / 我的工具集 / 添加）都要进编辑态。
        click = js[js.index("export function handleAgentToolPresetCardsClick("):]
        click = click[: click.index("\n}")]
        self.assertEqual(click.count("openAgentToolEditor("), 3)
        self.assertIn("function markPickedToolCard(", js, "被点的卡要有「按下」状态")
        mark = js[js.index("function markPickedToolCard("):]
        mark = mark[: mark.index("\n}")]
        self.assertIn("is-picked", mark)
        self.assertIn("aria-pressed", mark)
        bind = self._bind()
        self.assertIn("$('#agentToolPresetCards')?.addEventListener('click', handleAgentToolPresetCardsClick)", bind)
        self.assertIn("$('#agentToolEditorBack')?.addEventListener('click', closeAgentToolEditor)", bind)
        self.assertIn("$('#agentToolEditorSave')?.addEventListener('click', saveAgentToolSet)", bind)

    def test_saved_sets_live_in_backend_not_localstorage(self):
        """「我的工具集」存后端 config.json（localStorage 在冻结版每次退出都会被清空）。"""
        js = self._settings()
        save = js[js.index("export async function saveAgentToolSet()"):]
        save = save[: save.index("\n}")]
        self.assertIn("await api('/api/tool_sets'", save)
        self.assertIn("body: { id: editingId, name, tools }", save, "存的是闭包后的工具名列表")
        self.assertNotIn("localStorage", save, "保存不再写 localStorage")
        delete = js[js.index("export async function deleteToolTemplate("):]
        delete = delete[: delete.index("\n}")]
        self.assertIn("method: 'DELETE'", delete)
        self.assertNotIn("localStorage", delete)
        loader = js[js.index("export function loadToolTemplates()"):]
        loader = loader[: loader.index("\n}")]
        self.assertIn("state.toolTemplates", loader)
        self.assertNotIn("localStorage", loader, "读取只认内存里的后端数据")
        self.assertNotIn("persistToolTemplates", js, "旧的 localStorage 写入函数已退役")
        # 老数据一次性搬迁（源码模式/浏览器里存过的），搬完删掉旧键。
        self.assertIn("export async function migrateLegacyToolTemplates()", js)
        migrate = js[js.index("export async function migrateLegacyToolTemplates()"):]
        migrate = migrate[: migrate.index("\n}")]
        self.assertIn("TOOL_TEMPLATE_STORE", migrate)
        self.assertIn("removeItem(TOOL_TEMPLATE_STORE)", migrate)

    def test_swap_animation_keeps_height_fixed(self):
        js = self._settings()
        swap = js[js.index("function swapToolView(editing)"):]
        swap = swap[: swap.index("\n}")]
        self.assertIn("prefers-reduced-motion: reduce", swap)
        self.assertNotIn("--agent-dialog-h", swap, "高度固定，切换时不再改弹层高度")
        self.assertNotIn("--agent-dialog-h", js, "09-settings 里不该再残留高度变量")
        css = self._css()
        for rule in (".tool-stage", ".tool-preset-view.is-leaving", ".tool-editor.is-entering",
                     ".tool-preset-card.is-picked", ".tool-preset-card-add.is-picked",
                     ".tool-preset-card-add", ".tool-preset-card-del"):
            with self.subTest(rule=rule):
                self.assertIn(rule, css)
        # 卡片态贴顶（空白留在下方，正好是列表滑入的位置）。
        view_rule = css[css.index(".tool-preset-view {"):]
        view_rule = view_rule[: view_rule.index("}")]
        self.assertIn("align-content: start", view_rule)
        self.assertIn("transition: opacity .08s", view_rule, "切换过渡已再缩短 1/3")


class AgentCardsMarkupTests(unittest.TestCase):
    def _index(self) -> str:
        return (ROOT / "public/index.html").read_text(encoding="utf-8")

    def _settings(self) -> str:
        return (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")

    def _bind(self) -> str:
        return (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def test_agent_panel_only_renders_cards(self) -> None:
        index = self._index()
        start = index.index('data-settings-panel="agent"')
        end = index.index('data-settings-panel="runtime"')
        panel = index[start:end]
        self.assertIn('id="agentCards"', panel)
        self.assertNotIn('id="agentForm"', panel, "设置表单不应再常驻在设置页里")
        self.assertNotIn('id="addAgent"', panel)

    def test_form_moved_into_dialog_with_same_fields(self) -> None:
        index = self._index()
        self.assertIn('<dialog id="agentDialog" class="agent-dialog">', index)
        self.assertLess(index.index('id="agentDialog"'), index.index('id="agentForm"'))
        for field_id in FORM_FIELD_IDS:
            with self.subTest(field=field_id):
                self.assertIn(f'id="{field_id}"', index)
        for label in ("名称", "系统提示词（预设与规则）", "固定 Skill", "工具集", "添加自定义工具集"):
            with self.subTest(label=label):
                self.assertIn(label, index)

    def test_legacy_structures_removed_everywhere(self) -> None:
        sources = {
            "public/index.html": self._index(),
            "public/js/09-settings.js": self._settings(),
            "public/js/15-bind-events.js": self._bind(),
        }
        for name, source in sources.items():
            for snippet in LEGACY_SELECTORS:
                with self.subTest(file=name, snippet=snippet):
                    self.assertNotIn(snippet, source)

    def test_card_markup_and_add_card_last(self) -> None:
        source = self._settings()
        for snippet in (
            'class="agent-card',
            'data-agent-card="${id}"',
            'data-agent-delete="${id}"',
            "data-agent-add",
            "agent-card-name",
            "agent-card-meta",
            "agent-card-tools",
            "agent-card-prompt",
            "agent-card-tag",
            "新增 Agent",
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, source)
        self.assertLess(
            source.index("agents.map((agent) => agentCardMarkup(agent)).join('')"),
            source.index("agent-card-add"),
            "「新增 Agent」卡片必须排在最后一张",
        )

    def test_card_shows_tool_scope_label(self) -> None:
        """「固定 Skill」下方显示该 Agent 工具集对应的名称（预设名 / 我的工具集 / 未限制 / 自定义）。"""
        source = self._settings()
        self.assertIn("export function toolScopeLabel(", source)
        label = source[source.index("export function toolScopeLabel("):]
        label = label[: label.index("\n}")]
        self.assertIn("未限制（全部工具）", label, "空 tool_scope = 不限制")
        self.assertIn("自定义 · ", label)
        self.assertIn("matchToolScope(tools)", label)
        self.assertIn("knownToolNames()", label, "退役/掉线工具名不参与匹配与计数")
        card = source[source.index("function agentCardMarkup("):]
        card = card[: card.index("\n}")]
        self.assertIn("toolScopeLabel(agent.tool_scope)", card, "卡片用 Agent 的 tool_scope 算标签")
        self.assertLess(card.index("agent-card-meta"), card.index("agent-card-tools"),
                        "工具集标签排在「固定 Skill」下方")
        self.assertLess(card.index("agent-card-tools"), card.index("agent-card-prompt"))
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        self.assertIn(".agent-card-tools", css)

    def test_tool_catalog_cache_keeps_mcp_tools_visible(self) -> None:
        """工具目录只能短时效缓存：MCP 按需连接，启动时的目录可能没有 mcp__* 工具。

        用户实测「MCP 工具消失了」——启动时拉到的目录被永久缓存，面板里再也看不到 MCP 工具。
        """
        source = self._settings()
        self.assertIn("export async function ensureToolCatalog({ maxAgeMs = 5000 } = {})", source)
        self.assertIn("let toolCatalogPromise = null;", source)
        self.assertIn("export function invalidateToolCatalog()", source)
        picker = source[source.index("export async function renderAgentToolPicker()"):]
        picker = picker[: picker.index("\n}")]
        self.assertIn("await ensureToolCatalog({ maxAgeMs: 0 });", picker, "打开面板必须强制取新")
        manager = source[source.index("export async function renderAgentManager()"):]
        manager = manager[: manager.index("\n}")]
        self.assertIn("await ensureToolCatalog();", manager, "渲染卡片前先确保目录已加载")
        poll = source[source.index("export async function pollMcpStatus()"):]
        poll = poll[: poll.index("\n}")]
        self.assertIn("invalidateToolCatalog()", poll, "MCP 连接状态变化要作废目录缓存")
        core = (ROOT / "public/js/01-core.js").read_text(encoding="utf-8")
        self.assertIn("toolCatalogAt: 0,", core, "缓存时间戳登记进全局状态")

    def test_default_agent_has_no_badge_or_highlight(self) -> None:
        """默认 Agent 在卡片上不做任何标记：角标/强调色会被误读成「当前选中」。"""
        source = self._settings()
        card = source[source.index("function agentCardMarkup("):]
        card = card[: card.index("\n}")]
        self.assertNotIn("agent-card-badge", card, "「默认」角标已移除")
        self.assertNotIn("is-default", card, "默认 Agent 不再加高亮类")
        self.assertNotIn("defaultId", card, "卡片渲染不再需要默认项 ID")
        self.assertNotIn("agent-card-badge", source)
        self.assertNotIn("agentCardMarkup(agent, defaultId)", source)
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        self.assertNotIn(".agent-card.is-default", css)
        self.assertNotIn(".agent-card-badge", css, "角标样式一并退役")
        self.assertIn(".agent-card:hover", css, "悬停反馈保留（那不是选中态）")

    def test_built_in_agents_have_no_delete_button(self) -> None:
        source = self._settings()
        self.assertIn("agent.built_in ? '' : `<button class=\"agent-card-delete", source)

    def test_render_does_not_auto_open_form(self) -> None:
        source = self._settings()
        body = source[source.index("export async function renderAgentManager()"):]
        body = body[: body.index("\n}")]
        self.assertNotIn("showAgentForm(", body)

    def test_card_click_opens_dialog_with_that_agent(self) -> None:
        source = self._settings()
        self.assertIn("export function openAgentCard(", source)
        body = source[source.index("export function openAgentCard("):]
        body = body[: body.index("\n}")]
        self.assertIn("state.bootstrap?.agents", body)
        self.assertIn("showAgentForm(agent)", body)

    def test_delete_closes_dialog_when_editing_deleted_agent(self) -> None:
        source = self._settings()
        body = source[source.index("export async function deleteAgent("):]
        body = body[: body.index("\n}")]
        self.assertIn("confirm(", body)
        self.assertIn("if ($('#agentFormId').value === agentId) hideAgentForm();", body)

    def test_bindings_delegate_from_card_container(self) -> None:
        bind = self._bind()
        self.assertIn("$('#agentCards').addEventListener('click'", bind)
        self.assertIn("$('#agentCards').addEventListener('keydown'", bind)
        self.assertIn("data-agent-delete", bind)
        self.assertIn("data-agent-add", bind)
        self.assertIn("data-agent-card", bind)
        self.assertIn("openAgentCard(", bind)
        self.assertIn("$('#agentDialog').addEventListener('close'", bind)
        self.assertIn("$('#saveAgentForm').addEventListener('click'", bind)
        self.assertIn("$('#cancelAgent').addEventListener('click'", bind)

    def test_css_three_columns_and_responsive(self) -> None:
        css = self._css()
        cards = css[css.index(".agent-cards {"):]
        cards = cards[: cards.index("}")]
        self.assertIn("repeat(3, minmax(0, 1fr))", cards, "一行最多三张卡片")
        self.assertIn("repeat(2, minmax(0, 1fr))", css, "窄屏应降级为两列")
        delete_rule = css[css.index(".agent-card-delete {"):]
        delete_rule = delete_rule[: delete_rule.index("}")]
        self.assertIn("position: absolute", delete_rule, "× 必须固定在卡片右上角")
        prompt_rule = css[css.index(".agent-card-prompt {"):]
        prompt_rule = prompt_rule[: prompt_rule.index("}")]
        self.assertIn("-webkit-line-clamp: 2", prompt_rule, "提示词摘要必须两行截断")
        dialog = css[css.index(".agent-dialog {"):]
        dialog = dialog[: dialog.index("}")]
        self.assertIn("max-height", dialog, "弹层内部需可滚动，不能溢出视口")

    def test_no_manual_agent_id_field(self) -> None:
        """用户不再手填 Agent ID：表单里不得再有 ID 输入框，保存走隐藏字段。"""
        index = self._index()
        self.assertNotIn('id="agentId"', index)
        self.assertNotIn("英文、数字、下划线或连字符", index)
        source = self._settings()
        self.assertNotIn("#agentId", source)
        body = source[source.index("export async function saveAgentForm()"):]
        body = body[: body.index("\n}")]
        self.assertIn("id: $('#agentFormId').value.trim()", body)
        self.assertIn("保存后自动分配 ID", source)

    def test_avatar_button_in_basic_tab(self) -> None:
        """「自定义头像」与名称同排（基本分区），选图只做预览、保存才上传。"""
        index = self._index()
        basic = index[index.index('data-agent-panel="basic"'):]
        basic = basic[: basic.index('data-agent-panel="skills"')]
        self.assertIn('id="pickAgentAvatar"', basic)
        self.assertIn('id="agentAvatarPreview"', basic)
        self.assertLess(basic.index('id="agentName"'), basic.index('id="pickAgentAvatar"'))
        source = self._settings()
        for snippet in ("export function pickAgentAvatar(", "export function handleAgentAvatarFile(",
                        "agentAvatarUrl(", "/api/agents/avatar/", "FormData()"):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, source)
        bind = self._bind()
        self.assertIn("$('#pickAgentAvatar')?.addEventListener('click', pickAgentAvatar)", bind)
        self.assertIn("handleAgentAvatarFile(file)", bind)

    def test_avatar_shown_on_cards_and_messages(self) -> None:
        """头像要落到两处：Agent 卡片（小圆图）与助手消息气泡（替换「AI」圆标）。"""
        source = self._settings()
        self.assertIn('class="agent-card-avatar"', source)
        messages = (ROOT / "public/js/04-messages.js").read_text(encoding="utf-8")
        self.assertIn("export function currentAgentAvatarUrl(", messages)
        self.assertIn('class="message-avatar message-avatar-img"', messages)
        self.assertIn('<div class="message-avatar">AI</div>', messages, "没有头像时必须保留默认 AI 圆标")
        css = self._css()
        self.assertIn(".agent-card-avatar {", css)
        self.assertIn(".agent-avatar-preview {", css)
        avatar_rule = css[css.index(".message-avatar-img {"):]
        avatar_rule = avatar_rule[: avatar_rule.index("}")]
        self.assertIn("object-fit: cover", avatar_rule, "头像必须中心裁切填充，不能拉伸变形")

    def test_skill_picker_uses_cards_and_fills_panel(self) -> None:
        """固定 Skill 用卡片网格；列表占满分区高度、内部滚动（不再有灰色标题块与固定高度）。"""
        source = self._settings()
        body = source[source.index("export function renderAgentSkillPicker()"):]
        body = body[: body.index("\n}")]
        self.assertIn('class="skill-card"', body)
        self.assertNotIn('class="skill-item"', body, "技能页的列表样式不该再被 Agent 弹层复用")
        css = self._css()
        skill_list = css[css.index(".agent-tab-panel .skill-list {"):]
        skill_list = skill_list[: skill_list.index("}")]
        self.assertIn("flex: 1", skill_list, "列表占满分区剩余高度")
        self.assertIn("overflow: auto", skill_list)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", skill_list)
        card = css[css.index(".agent-tab-panel .skill-card {"):]
        card = card[: card.index("}")]
        self.assertIn("display: flex", card)
        self.assertIn("border: 1px solid var(--line)", card)
        tool_scope = css[css.index("#agentToolScope {"):]
        tool_scope = tool_scope[: tool_scope.index("}")]
        self.assertIn("flex: 1", tool_scope, "工具列表同样占满分区高度")
        self.assertIn("grid-auto-rows: max-content", tool_scope,
                      "默认 align-content:stretch 会把分组行均摊压扁（实测 19.6px vs 分组头 59px）")

    def test_no_duplicated_panel_title_block(self) -> None:
        """分区按钮已有标题，面板里不再重复灰色标题块，只留一行小字说明。"""
        index = self._index()
        self.assertNotIn('class="agent-skills"', index, "灰色标题块已移除")
        self.assertNotIn('class="agent-skills-head"', index)
        skills = index[index.index('data-agent-panel="skills"'):]
        skills = skills[: skills.index('data-agent-panel="tools"')]
        self.assertIn('class="agent-tab-hint"', skills, "只保留一行小字说明")
        self.assertNotIn("<b>固定 Skill</b>", skills, "面板里不再重复标题")
        tools = index[index.index('data-agent-panel="tools"'):]
        self.assertIn('class="agent-tab-hint"', tools)
        self.assertNotIn("<b>工具集</b>", tools, "面板里不再重复标题")
        # 计数、预设状态与「展开全部」搬进预设/搜索行，功能不丢。
        for field in ("agentToolCount", "toggleAllToolGroups", "agentToolPresetState"):
            with self.subTest(field=field):
                self.assertIn(f'id="{field}"', tools)

    def test_scrollable_lists_are_not_clipped(self) -> None:
        """固定 Skill / 工具集列表是滚动容器：分区面板必须 min-height:0 + 自身可滚，否则被裁掉且点不到。"""
        css = self._css()
        body = css[css.index(".agent-form-body {"):]
        body = body[: body.index("}")]
        self.assertIn("min-height: 0", body, "flex 子项默认 min-height:auto 会被内容顶破")
        panel = css[css.index(".agent-tab-panel {"):]
        panel = panel[: panel.index("}")]
        self.assertIn("min-height: 0", panel)
        self.assertIn("overflow: auto", panel, "面板自身滚动，列表不被裁掉")
        provider_body = css[css.index(".provider-form-body {"):]
        provider_body = provider_body[: provider_body.index("}")]
        self.assertIn("grid-auto-rows: max-content", provider_body)

    def test_expanded_tool_cards_compact_and_distinct_from_parent(self) -> None:
        """展开后的工具卡片：紧凑 + 与父分组区分度（用户实测"太宽松、父子分不清"）。"""
        css = self._css()
        grid = css[css.index(".permission-grid {"):]
        grid = grid[: grid.index("}")]
        self.assertIn("gap: 7px", grid)
        card = css[css.index(".permission-grid > label {"):]
        card = card[: card.index("}")]
        self.assertIn("min-height: 54px", card, "卡片高度要收紧（原 68px）")
        self.assertIn("padding: 8px 10px", card)
        self.assertIn("background: var(--surface)", card, "子卡片白底")
        self.assertIn("border: 1px solid var(--line-strong)", card, "描边要加强，与父区分")
        self.assertIn("box-shadow", card)
        desc = css[css.index(".permission-grid small {"):]
        desc = desc[: desc.index("}")]
        self.assertIn("-webkit-line-clamp: 2", desc, "说明两行截断，保证卡片高度一致")
        expanded = css[css.index(".agent-tool-group .permission-grid {"):]
        expanded = expanded[: expanded.index("}")]
        self.assertIn("background: var(--surface)", expanded, "展开区必须白底")
        head = css[css.index(".agent-tool-group-head {"):]
        head = head[: head.index("}")]
        self.assertNotIn("background:", head, "折叠时分组头保持原样（无灰底）")
        self.assertIn(".agent-tool-group:not(.collapsed) .agent-tool-group-head { background: var(--surface-2); }",
                      css, "只有展开的那一组，分组头才变浅灰条子")
        settings = (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")
        self.assertIn("label.title = `${tool.name}：${tool.description}`", settings,
                      "说明被截断，完整描述要进 title 悬停可见")

    def test_index_html_still_has_no_duplicate_ids(self) -> None:
        ids = re.findall(r'\sid="([^"]+)"', self._index())
        duplicates = {value for value in ids if ids.count(value) > 1}
        self.assertFalse(duplicates, f"index.html 存在重复 id：{sorted(duplicates)}")


if __name__ == "__main__":
    unittest.main()
