# -*- coding: utf-8 -*-
"""守门：互斥工具组（子代理两种上下文模式；模式选择权完全归用户的工具集）。

背景（2026-09-20）：子代理的 fork / spawn 是两种**互斥**的上下文模式。第一版把它做成
`subagent` 工具的 `fork` 参数（让模型自选），第二版让后端按计费形态定默认——两条路都有死结：
「任务要不要前文」只有模型知道，「当前会话在哪个供应商上」只有后端知道，谁都判不全。

最终取向：**模式不进运行时决策**，直接变成工具集层面的用户配置 —— `subagent`（fork）与
`subagent_spawn`（spawn）两个工具互斥，勾一个自动取消另一个；模型只看到被启用的那个。
决策权责：用哪个模式 = 用户；什么时候开子代理 = 模型；冲突归一 = 后端（确定性）。

本文件守住三件事：

1. **互斥归一只有一份权威**：``run/session.normalize_tool_mutex``（保留组内排前者 = fork =
   安全方向：选错只是费钱，反过来丢上下文是质量事故），且三个入口全覆盖——
   ``resolve_allowed_tools``（每轮 Run 的最终闸门）/ ``bake_session_tool_ids``（落库即归一）/
   ``enable_conversation_tools``（只加不删的注入路径）；
2. **前端镜像**：``public/js/09-settings.js`` 的 ``AGENT_TOOL_MUTEX_GROUPS`` 必须与后端
   ``MUTUALLY_EXCLUSIVE_TOOL_GROUPS`` 逐字一致（与 AGENT_TOOL_DEP_RULES / JOB_CREATOR_TOOL_DEPS
   同款前后端镜像，改一边必须改另一边）；
3. **预设不留洞**：full 预设走 ``group:*`` 会把互斥的两个同时展开 ⇒ 必须显式 exclude 后位者。

模式本身怎么落地（runner/handler/schema/常驻区文案）归 ``tests/test_subagent_fork.py``。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.config import (  # noqa: E402
    TOOL_PRESETS,
    _clean_tool_set_tools,
    resolve_tool_preset,
    tool_catalog_entries,
)
from naiba.run.session import (  # noqa: E402
    JOB_CREATOR_TOOL_DEPS,
    JOB_TOOLS,
    MUTUALLY_EXCLUSIVE_TOOL_GROUPS,
    bake_session_tool_ids,
    enable_conversation_tools,
    normalize_tool_mutex,
    resolve_allowed_tools,
)

SETTINGS_JS = (ROOT / "public" / "js" / "09-settings.js").read_text(encoding="utf-8")
PAIR = ("subagent", "subagent_spawn")


# ---------------------------------------------------------------------------
# 后端：归一函数本身
# ---------------------------------------------------------------------------


class NormalizeToolMutexTests(unittest.TestCase):
    """同组多个 → 只留组内排前者；只含单个 → 恒等。"""

    def test_both_present_keeps_the_first(self) -> None:
        self.assertEqual(normalize_tool_mutex(list(PAIR)), [PAIR[0]])
        self.assertEqual(normalize_tool_mutex(list(reversed(PAIR))), [PAIR[0]])

    def test_single_member_is_identity(self) -> None:
        for scope in ([PAIR[0]], [PAIR[1]], ["read_file", PAIR[1]]):
            with self.subTest(scope=scope):
                self.assertEqual(normalize_tool_mutex(scope), scope)

    def test_order_of_the_rest_is_preserved(self) -> None:
        scope = ["read_file", PAIR[1], "pwsh", PAIR[0], "write_file"]
        self.assertEqual(
            normalize_tool_mutex(scope),
            ["read_file", "pwsh", PAIR[0], "write_file"],
            "只删互斥的后位者，其余顺序逐字不变（前缀缓存与显示顺序都靠它）",
        )

    def test_empty_and_dirty_inputs(self) -> None:
        for raw in ([], None, ()):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_tool_mutex(raw), [])

    def test_duplicates_do_not_eat_the_keeper(self) -> None:
        """重复项不能把自己当"后位者"删掉（按值删，必须保证 keeper 还在）。"""
        self.assertEqual(
            normalize_tool_mutex([PAIR[0], PAIR[0], PAIR[1]]), [PAIR[0], PAIR[0]]
        )

    def test_unknown_names_untouched(self) -> None:
        scope = ["mcp__x__y", "subagent_spawn"]
        self.assertEqual(normalize_tool_mutex(scope), scope)


# ---------------------------------------------------------------------------
# 后端：三个入口全覆盖
# ---------------------------------------------------------------------------


class _FakeStorage:
    def __init__(self, conversation: dict, message_count: int = 0) -> None:
        self.conversation = conversation
        self.count = message_count
        self.writes: list[list[str]] = []

    def get_conversation(self, conversation_id: str):
        return self.conversation

    def message_count(self, conversation_id: str) -> int:
        return self.count

    def set_enabled_tool_ids(self, conversation_id: str, ids: list[str]) -> None:
        self.writes.append(list(ids))
        self.conversation["enabled_tool_ids"] = list(ids)


def _fake_app(tool_names: tuple[str, ...], conversation: dict, message_count: int = 0):
    return SimpleNamespace(
        storage=_FakeStorage(conversation, message_count),
        config=SimpleNamespace(
            data={"agent_tools": []},
            get_agent=lambda agent_id: {"id": agent_id, "tool_scope": []},
            default_agent_id=lambda: "agent-1",
        ),
        tool_registry=SimpleNamespace(
            schemas=lambda: [{"name": name} for name in tool_names],
            readonly_mcp_tools=lambda: [],
        ),
        web_search=SimpleNamespace(is_available=lambda: False),
    )


class ResolveAllowedToolsGateTests(unittest.TestCase):
    """每轮 Run 的最终闸门：固化集里两个都在 → 放行集里只剩 fork 那个。"""

    ALL = ("read_file", "pwsh", "subagent", "subagent_spawn", "job_output")

    def test_baked_set_with_both_keeps_fork(self) -> None:
        app = _fake_app(self.ALL, {"id": "conv-1"})
        allowed = resolve_allowed_tools(app, "craft", {}, False, "", list(self.ALL))
        self.assertIn("subagent", allowed)
        self.assertNotIn("subagent_spawn", allowed)

    def test_spawn_only_is_kept_and_drags_job_output(self) -> None:
        """互斥归一不能误伤单开的 spawn：它自己与依赖闭包都必须在。"""
        app = _fake_app(self.ALL, {"id": "conv-1"})
        allowed = resolve_allowed_tools(app, "craft", {}, False, "", ["subagent_spawn"])
        self.assertIn("subagent_spawn", allowed)
        self.assertIn("job_output", allowed, "spawn 也是 Job 创建者，必须带出查询工具")
        self.assertNotIn("subagent", allowed)

    def test_agent_scope_path_also_normalized(self) -> None:
        """未固化的旧会话走 Agent scope 分支，同样不得双开。"""
        app = _fake_app(self.ALL, {"id": "conv-1"})
        agent = {"tool_scope": ["subagent", "subagent_spawn", "read_file"]}
        allowed = resolve_allowed_tools(app, "craft", agent, False, "")
        self.assertIn("subagent", allowed)
        self.assertNotIn("subagent_spawn", allowed)


class BakeSessionToolIdsTests(unittest.TestCase):
    """落库的就必须是归一后的集合：前端显示与实际放行同源。"""

    def test_scope_with_both_is_baked_normalized(self) -> None:
        conversation = {"id": "conv-1", "enabled_tool_ids": []}
        app = _fake_app(("read_file", *PAIR), conversation)
        baked = bake_session_tool_ids(app, conversation, {"tool_scope": ["read_file", *PAIR]})
        self.assertNotIn("subagent_spawn", baked)
        self.assertEqual(app.storage.writes[-1], baked, "落库值必须与返回值一致")

    def test_existing_baked_set_is_normalized_on_read(self) -> None:
        conversation = {"id": "conv-1", "enabled_tool_ids": ["subagent", "subagent_spawn"]}
        app = _fake_app(("read_file", *PAIR), conversation)
        baked = bake_session_tool_ids(app, conversation, {})
        self.assertEqual(baked, ["subagent"])
        self.assertEqual(app.storage.writes, [], "已固化的会话不该被顺手改写")

    def test_legacy_all_tools_bake_drops_the_loser(self) -> None:
        """旧会话（已有消息、未固化）= 全部工具激活：同样过归一，否则一启动就双开。"""
        conversation = {"id": "conv-1", "enabled_tool_ids": []}
        app = _fake_app(("read_file", *PAIR), conversation, message_count=3)
        baked = bake_session_tool_ids(app, conversation, {})
        self.assertIn("read_file", baked)
        self.assertNotIn("subagent_spawn", baked)


class EnableConversationToolsTests(unittest.TestCase):
    """"只加不删"的注入路径也不得把集合变成双开。"""

    def test_adding_the_rival_cannot_create_a_double_open(self) -> None:
        conversation = {"id": "conv-1", "enabled_tool_ids": ["read_file", "subagent"]}
        app = _fake_app(("read_file", *PAIR), conversation)
        result = enable_conversation_tools(app, "conv-1", ["subagent_spawn"])
        self.assertIn("read_file", result["enabled_tool_ids"])
        self.assertIn("subagent", result["enabled_tool_ids"])
        self.assertNotIn("subagent_spawn", result["enabled_tool_ids"])
        self.assertEqual(result["added"], ["subagent_spawn"], "add 语义照旧返回被请求的工具名")

    def test_adding_the_keeper_onto_a_spawn_session_keeps_the_keeper(self) -> None:
        conversation = {"id": "conv-1", "enabled_tool_ids": ["subagent_spawn"]}
        app = _fake_app(("read_file", *PAIR), conversation)
        result = enable_conversation_tools(app, "conv-1", ["subagent"])
        self.assertIn("subagent", result["enabled_tool_ids"])
        self.assertNotIn("subagent_spawn", result["enabled_tool_ids"])


class JobToolRegistrationTests(unittest.TestCase):
    def test_both_names_are_craft_system_tools(self) -> None:
        for name in PAIR:
            with self.subTest(tool=name):
                self.assertIn(name, JOB_TOOLS)

    def test_dependency_closure_covers_the_new_tool(self) -> None:
        self.assertEqual(JOB_CREATOR_TOOL_DEPS["subagent_spawn"], ("job_output",))


# ---------------------------------------------------------------------------
# 预设与工具集落库
# ---------------------------------------------------------------------------


class PresetAndToolSetGateTests(unittest.TestCase):
    def _entries(self):
        from naiba.tools.registry import build_tool_registry

        return tool_catalog_entries(list(build_tool_registry().schemas()))

    def test_full_preset_expands_everything_except_the_loser(self) -> None:
        """full 走 group:* ⇒ 必须显式 exclude 后位者，否则"一键全能"就是双开。"""
        entries = self._entries()
        known = {entry["name"] for entry in entries}
        full = next(preset for preset in TOOL_PRESETS if preset["id"] == "full")
        self.assertIn("subagent_spawn", list(full.get("exclude") or []))
        tools = set(resolve_tool_preset(full, entries))
        self.assertEqual(tools, known - {PAIR[1]})
        self.assertIn(PAIR[0], tools)

    def test_other_presets_carry_neither_subagent_tool(self) -> None:
        entries = self._entries()
        for preset in TOOL_PRESETS:
            if preset["id"] == "full":
                continue
            with self.subTest(preset=preset["id"]):
                tools = set(resolve_tool_preset(preset, entries))
                self.assertEqual(tools & set(PAIR), set(),
                                 "常规预设不预设子代理模式：由用户在工具页自己选")

    def test_tool_sets_are_cleaned_by_the_backend_too(self) -> None:
        """手攒/直写 API 的工具集也过归一（前端已归一，后端是兜底）。"""
        self.assertEqual(_clean_tool_set_tools(list(PAIR)), [PAIR[0]])
        self.assertEqual(
            _clean_tool_set_tools(["read_file", PAIR[1], "read_file"]),
            ["read_file", PAIR[1]],
            "既有语义（去空去重保序）不受影响",
        )


# ---------------------------------------------------------------------------
# 前端镜像（与 AGENT_TOOL_DEP_RULES / JOB_CREATOR_TOOL_DEPS 同款：改一边必须改另一边）
# ---------------------------------------------------------------------------


class FrontendMirrorTests(unittest.TestCase):
    def test_mutex_groups_mirror_the_backend(self) -> None:
        line = next(
            (row for row in SETTINGS_JS.splitlines()
             if row.startswith("export const AGENT_TOOL_MUTEX_GROUPS = ")),
            "",
        )
        self.assertTrue(line, "前端缺少 AGENT_TOOL_MUTEX_GROUPS")
        groups = tuple(re.findall(r"'([^']+)'\s*,\s*'([^']+)'", line))
        self.assertEqual(groups, MUTUALLY_EXCLUSIVE_TOOL_GROUPS,
                         "前后端互斥组必须逐字一致（镜像契约）")

    def test_normalize_helper_exists_and_reuses_the_groups(self) -> None:
        self.assertIn("export function normalizeToolMutex(", SETTINGS_JS)
        body = SETTINGS_JS[SETTINGS_JS.index("export function normalizeToolMutex("):]
        body = body[: body.index("\n}")]
        self.assertIn("AGENT_TOOL_MUTEX_GROUPS", body, "归一函数必须读同一份组定义")

    def test_scope_normalization_and_saving_go_through_it(self) -> None:
        """加载（预设/模板/表单）与保存（我的工具集）两条路径都必须过归一。"""
        for marker in (
            "return normalizeToolMutex([...result]);",       # normalizeToolScope
            "const tools = normalizeToolMutex(",             # saveAgentToolSet
            "return normalizeToolMutex((template.tools || [])",  # usableTemplateTools
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, SETTINGS_JS)

    def test_bulk_selection_defers_to_the_keeper(self) -> None:
        """分类/二级分组全选没有"用户想选哪个"的语义 ⇒ 逐项互斥关掉，末尾按组内排前者归一。"""
        self.assertEqual(SETTINGS_JS.count("{ applyMutex: false }"), 2,
                         "两个批量勾选入口（二级分组 + 分类全选）都要关掉逐项互斥")
        self.assertEqual(SETTINGS_JS.count("notifyToolMutexDrop("), 3,
                         "一次函数定义 + 两处批量勾选提示")

    def test_single_click_lets_the_user_win(self) -> None:
        """单点勾选必须听用户的：勾哪个留哪个（不是按组内排前者保留）。"""
        body = SETTINGS_JS[SETTINGS_JS.index("export function applyAgentToolDependency("):]
        body = body[: body.index("\n}")]
        self.assertIn("MUTEX_RIVALS.get(changedTool)", body)
        self.assertIn("if (applyMutex)", body)


if __name__ == "__main__":
    unittest.main()
