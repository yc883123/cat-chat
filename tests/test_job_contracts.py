# -*- coding: utf-8 -*-
"""后台任务契约守门（计划 2026-10-22 §3）：创建前校验 / job_wait 语义 / detached 语义 / 单独授权。

四组不变量：

1. `run_in_background` 的 kind 归一与前置校验发生在**创建之前**——不能先报已提交再被未知
   worker 打成失败；shell 的 cwd 冻结当前 Run 的会话工作区。
2. `job_wait` 等待到期且任务仍活动时返回**正常快照**（True + 「仍在运行」），
   failed / cancelled / interrupted 才分别是失败。
3. detached（逐帧抽取等）Job 不占会话互斥位、不进 list_primary、不被聊天收尾等待，
   也不被父任务取消级联。
4. opt-in 工具（`OPT_IN_TOOLS`）只能由 Agent 的 `tool_opt_ins` 授权；
   预设 / 全选 / 旧会话回退 / Plan / 辅助对话一律不得隐式放行。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.config import (  # noqa: E402
    ConfigStore,
    OPT_IN_TOOLS,
    TOOL_PRESETS,
    resolve_tool_preset,
    tool_catalog_entries,
)
from naiba.jobs import JobRegistry, JobSpec  # noqa: E402
from naiba.run.manager import ConversationRunManager  # noqa: E402
from naiba.run.session import (  # noqa: E402
    all_tool_names,
    bake_session_tool_ids,
    resolve_allowed_tools,
)
from naiba.skills.agent import SkillAgent  # noqa: E402
from naiba.subagent import job_tool_handler_factory  # noqa: E402
from naiba.tools.registry import build_tool_registry  # noqa: E402

EXACT_TOOL = "extract_video_frames_exact"


class _FakeJobs:
    def __init__(self, job: dict | None = None) -> None:
        self.started: list[JobSpec] = []
        self._job = job
        self.waited: list[tuple[str, int]] = []

    def start(self, spec: JobSpec, owner: str | None = None) -> str:
        self.started.append(spec)
        return "job_new"

    def get(self, job_id: str, owner: str | None = None):
        return self._job

    def wait(self, job_id: str, timeout: float, owner: str | None = None):
        self.waited.append((job_id, timeout))
        return self._job

    def list(self, owner=None, active_only=False):
        return []


class _FakeApp:
    def __init__(self, jobs, workspace: str = "") -> None:
        self.jobs = jobs
        self.config = SimpleNamespace(resolve_workspace_dir=lambda *a, **k: workspace)
        self.storage = SimpleNamespace(is_job_cleaned=lambda job_id: False)


class RunInBackgroundPreflightTests(unittest.TestCase):
    """创建前校验：kind 归一、未知类型拒绝、shell 必需参数、cwd 冻结。"""

    def setUp(self) -> None:
        self.jobs = _FakeJobs()
        self.app = _FakeApp(self.jobs, workspace="D:/ws")
        self.handler = job_tool_handler_factory(self.app)["run_in_background"]

    def _ctx(self) -> dict:
        return {
            "conversation_id": "conv_1",
            "owner_session_id": "conv_1",
            "run_id": "run_1",
            "workspace_dir": "D:/session-ws",
        }

    def test_agent_alias_normalizes_to_subagent(self) -> None:
        ok, text = self.handler(
            {"spec": {"kind": "agent", "params": {"instruction": "查一下"}}}, [], self._ctx()
        )
        self.assertTrue(ok, text)
        self.assertEqual(self.jobs.started[0].kind, "subagent")

    def test_unknown_kind_is_rejected_before_creation(self) -> None:
        ok, text = self.handler({"spec": {"kind": "video_frames", "params": {}}}, [], self._ctx())
        self.assertFalse(ok)
        self.assertIn("不支持的后台任务类型", text)
        self.assertEqual(self.jobs.started, [], "被拒绝的 kind 不得留下 Job 记录")

    def test_shell_requires_command(self) -> None:
        ok, text = self.handler({"spec": {"kind": "shell", "params": {}}}, [], self._ctx())
        self.assertFalse(ok)
        self.assertIn("command", text)
        self.assertEqual(self.jobs.started, [])

    def test_shell_cwd_defaults_to_run_workspace(self) -> None:
        ok, text = self.handler(
            {"spec": {"kind": "shell", "params": {"command": "echo hi"}}}, [], self._ctx()
        )
        self.assertTrue(ok, text)
        self.assertEqual(self.jobs.started[0].params["cwd"], "D:/session-ws",
                         "cwd 必须冻结当前 Run 的会话工作区，而不是 worker 启动时读全局配置")

    def test_subagent_requires_instruction(self) -> None:
        ok, text = self.handler({"spec": {"kind": "subagent", "params": {}}}, [], self._ctx())
        self.assertFalse(ok)
        self.assertIn("instruction", text)


class JobWaitSemanticsTests(unittest.TestCase):
    """等待到期 ≠ 失败：模型据返回文本决定「继续等」还是「重新提交」。"""

    def _wait(self, job: dict) -> tuple[bool, str]:
        app = _FakeApp(_FakeJobs(job))
        handler = job_tool_handler_factory(app)["job_wait"]
        return handler({"job_id": "job_1", "timeout": 5}, [], None)

    def test_timeout_while_running_is_a_normal_snapshot(self) -> None:
        ok, text = self._wait({"id": "job_1", "status": "running", "progress": 42,
                               "current_step": "已抽取 42", "attempt": 0, "result": {}})
        self.assertTrue(ok, text)
        self.assertIn("仍在运行", text)
        self.assertIn("不要重复创建同一任务", text)

    def test_failed_cancelled_interrupted_are_failures(self) -> None:
        cases = {
            "failed": "已失败",
            "cancelled": "已被取消",
            "interrupted": "已中断",
        }
        for status, marker in cases.items():
            with self.subTest(status=status):
                ok, text = self._wait({"id": "job_1", "status": status, "error": "x", "result": {}})
                self.assertFalse(ok, text)
                self.assertIn(marker, text)

    def test_completed_is_success(self) -> None:
        ok, text = self._wait({"id": "job_1", "status": "completed", "result": {"ok": True}})
        self.assertTrue(ok)
        self.assertIn("已完成", text)


class DetachedJobSemanticsTests(unittest.TestCase):
    """detached Job 与主聊天并行：不占互斥位、不进 list_primary、不被收尾等待、不级联取消。"""

    def setUp(self) -> None:
        from naiba.storage.store import ChatStorage

        self.tmp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.conv = self.storage.create_conversation(agent_id="master")
        self.agent = {"id": "master", "name": "Job", "system_prompt": "", "skill_ids": []}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_detached_is_excluded_from_active_run(self) -> None:
        detached = self.storage.create_run(
            self.conv["id"], "逐帧", self.agent, {}, kind="video_frames", detached=True,
            origin_run_id="run_origin",
        )
        self.assertIsNone(self.storage.active_run(self.conv["id"]), "detached 不占会话互斥位")
        row = self.storage.get_background_task(str(detached["id"]))
        self.assertTrue(row["detached"])
        self.assertEqual(row["origin_run_id"], "run_origin")

    def test_detached_is_excluded_from_list_primary(self) -> None:
        rows = [
            {"id": "run_1", "kind": "chat", "parent_job_id": "", "detached": False},
            {"id": "job_x", "kind": "video_frames", "parent_job_id": "", "detached": True},
            {"id": "job_child", "kind": "shell", "parent_job_id": "run_1", "detached": False},
        ]
        stub = SimpleNamespace(list=lambda conversation_id="", active_only=False: rows)
        primary = ConversationRunManager.list_primary(stub, self.conv["id"])
        self.assertEqual([item["id"] for item in primary], ["run_1"])

    def test_detached_is_not_awaited_at_chat_turn_end(self) -> None:
        """即使误带上 parent_job_id，detached 也不参与本轮收尾等待。"""
        registry = SimpleNamespace(list=lambda owner=None, active_only=False: [
            {"id": "job_detached", "parent_job_id": "run_1", "status": "running", "detached": True},
            {"id": "job_child", "parent_job_id": "run_1", "status": "running", "detached": False},
        ])
        pending = SkillAgent._pending_background_jobs(
            {"run_id": "run_1", "owner_session_id": "conv_1", "job_registry": registry}
        )
        self.assertEqual(pending, ["job_child"])

    def test_detached_is_not_cancelled_with_parent(self) -> None:
        cancelled: list[str] = []
        stub = SimpleNamespace(
            list=lambda owner=None: [
                {"id": "job_detached", "parent_job_id": "", "detached": True},
            ],
            cancel=lambda job_id, owner=None, reason=None: cancelled.append(job_id),
        )
        JobRegistry._cancel_children(stub, "run_1", "conv_1", None)
        self.assertEqual(cancelled, [], "detached 没有父 ID，聊天取消不得波及它")


class _StubStorage:
    def __init__(self, message_count: int = 0) -> None:
        self._count = message_count
        self.saved: list[str] | None = None

    def message_count(self, conversation_id: str) -> int:
        return self._count

    def set_enabled_tool_ids(self, conversation_id: str, ids: list[str]) -> None:
        self.saved = list(ids)


class ToolOptInAuthorizationTests(unittest.TestCase):
    """opt-in 工具只能由 Agent 的 tool_opt_ins 打开（预设/全选/回退/Plan/辅助对话都不行）。"""

    def setUp(self) -> None:
        self.registry = build_tool_registry()
        self.entries = tool_catalog_entries(self.registry.schemas())

    def _app(self, storage: _StubStorage | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            config=SimpleNamespace(data={"agent_tools": ["read_file"]}),
            tool_registry=self.registry,
            web_search=SimpleNamespace(is_available=lambda: False),
            storage=storage or _StubStorage(),
        )

    def test_catalog_marks_opt_in_and_never_default_selected(self) -> None:
        row = next(item for item in self.entries if item["name"] == EXACT_TOOL)
        self.assertTrue(row["opt_in"])
        self.assertFalse(row["default_selected"])

    def test_no_preset_expands_opt_in_tool(self) -> None:
        for preset in TOOL_PRESETS:
            with self.subTest(preset=preset["id"]):
                self.assertNotIn(EXACT_TOOL, resolve_tool_preset(preset, self.entries))

    def test_all_tool_names_excludes_opt_in(self) -> None:
        names = all_tool_names(self._app())
        self.assertIn("read_file", names)
        self.assertNotIn(EXACT_TOOL, names)

    def test_new_session_without_opt_in_does_not_get_the_tool(self) -> None:
        app = self._app()
        baked = bake_session_tool_ids(app, {"id": "conv_1"}, {"tool_scope": [], "tool_opt_ins": []})
        self.assertNotIn(EXACT_TOOL, baked, "不限制 ≠ 放开逐帧落盘")

    def test_new_session_with_opt_in_gets_the_tool(self) -> None:
        app = self._app()
        baked = bake_session_tool_ids(
            app, {"id": "conv_1"}, {"tool_scope": ["read_file"], "tool_opt_ins": [EXACT_TOOL]}
        )
        self.assertIn(EXACT_TOOL, baked)

    def test_tool_scope_mentioning_the_tool_is_not_authorization(self) -> None:
        app = self._app()
        baked = bake_session_tool_ids(
            app, {"id": "conv_1"}, {"tool_scope": [EXACT_TOOL], "tool_opt_ins": []}
        )
        self.assertNotIn(EXACT_TOOL, baked, "写进 tool_scope 不构成授权")

    def test_old_session_fallback_excludes_opt_in(self) -> None:
        app = self._app(_StubStorage(message_count=3))
        baked = bake_session_tool_ids(app, {"id": "conv_old"}, {"tool_scope": []})
        self.assertIn("read_file", baked)
        self.assertNotIn(EXACT_TOOL, baked, "旧会话全量回退不得隐式授权")

    def test_plan_and_aux_never_get_opt_in(self) -> None:
        app = self._app()
        snapshot = ["read_file", EXACT_TOOL, "show_media"]
        plan = resolve_allowed_tools(app, "plan", {"tool_scope": []}, False, enabled_tool_ids=snapshot)
        self.assertNotIn(EXACT_TOOL, plan, "Plan 阶段不产生副作用")
        aux = resolve_allowed_tools(app, "craft", {"tool_scope": []}, False, enabled_tool_ids=snapshot,
                                    conv_kind="aux")
        self.assertNotIn(EXACT_TOOL, aux, "辅助对话只读锁死")
        craft = resolve_allowed_tools(app, "craft", {"tool_scope": []}, False, enabled_tool_ids=snapshot)
        self.assertIn(EXACT_TOOL, craft, "授权快照里带着就可以用（新会话已含 opt-in）")


class AgentOptInPersistenceTests(unittest.TestCase):
    """upsert_agent 对 tool_opt_ins 的读写口径：白名单、缺省保留、非数组拒绝。"""
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmp.name) / "config.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_round_trip_and_whitelist(self) -> None:
        store = ConfigStore(self.config_path)
        saved = store.upsert_agent({
            "name": "逐帧 Agent",
            "tool_scope": ["read_file"],
            "tool_opt_ins": [EXACT_TOOL, "read_file", "no_such_tool"],
        })
        self.assertEqual(saved["tool_opt_ins"], [EXACT_TOOL], "只保留白名单内的值")
        again = store.get_agent(str(saved["id"]))
        self.assertEqual(again["tool_opt_ins"], [EXACT_TOOL])

    def test_missing_key_preserves_stored_value(self) -> None:
        store = ConfigStore(self.config_path)
        saved = store.upsert_agent({
            "name": "逐帧 Agent", "tool_scope": ["read_file"], "tool_opt_ins": [EXACT_TOOL],
        })
        # 其它保存路径（不带这个键）不能顺手把已授权项抹掉。
        store.upsert_agent({"id": saved["id"], "name": "改名后", "tool_scope": ["read_file"]})
        again = store.get_agent(str(saved["id"]))
        self.assertEqual(again["tool_opt_ins"], [EXACT_TOOL])
        self.assertEqual(again["name"], "改名后")

    def test_non_array_is_rejected(self) -> None:
        store = ConfigStore(self.config_path)
        with self.assertRaises(ValueError):
            store.upsert_agent({"name": "x", "tool_opt_ins": EXACT_TOOL})

    def test_default_is_empty(self) -> None:
        store = ConfigStore(self.config_path)
        saved = store.upsert_agent({"name": "普通", "tool_scope": ["read_file"]})
        self.assertEqual(saved["tool_opt_ins"], [])


class FrontendOptInContractTests(unittest.TestCase):
    """设置页「单独授权」区的静态契约（沿用仓库的源码断言风格，不跑浏览器）。"""

    def setUp(self) -> None:
        self.settings_js = (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")
        self.index_html = (ROOT / "public/index.html").read_text(encoding="utf-8")

    def test_block_exists_in_form(self) -> None:
        self.assertIn('id="agentToolOptIns"', self.index_html)
        self.assertIn("agent-tool-optins", self.index_html)

    def test_save_and_echo_payload(self) -> None:
        self.assertIn("tool_opt_ins: [...new Set(state.agentFormToolOptIns || [])]", self.settings_js,
                      "保存 Agent 必须带上单独授权（独立于 tool_scope）")
        self.assertIn("agent?.tool_opt_ins", self.settings_js, "打开表单要回显已存的单独授权")

    def test_renderer_filters_by_opt_in_flag(self) -> None:
        self.assertIn("tool.opt_in", self.settings_js,
                      "只渲染目录里标记为 opt_in 的工具，不写死名字")
        self.assertIn("renderAgentToolOptIns", self.settings_js)

    def test_presets_and_dependency_rules_never_touch_opt_ins(self) -> None:
        # 依赖联动只作用于 tool_scope：出现 agentFormToolOptIns 就算串了通道。
        body = self.settings_js[
            self.settings_js.index("export const AGENT_TOOL_DEP_RULES = {"):
        ]
        body = body[: body.index("};")]
        self.assertNotIn("agentFormToolOptIns", body)
        self.assertNotIn(EXACT_TOOL, body, "opt-in 工具不得进入依赖联动表")

    def test_task_panel_has_a_readable_label(self) -> None:
        tasks_js = (ROOT / "public/js/06-tasks-plans.js").read_text(encoding="utf-8")
        self.assertIn("video_frames:", tasks_js, "任务面板要给逐帧 Job 一个可读类型名")

    def test_submit_path_keeps_opt_ins_when_narrowing_agent(self) -> None:
        """提交入口会把 Agent 收窄成固定字段再固化工具集：漏掉 tool_opt_ins 等于授权永远不生效。"""
        chat_py = (ROOT / "naiba/run/chat.py").read_text(encoding="utf-8")
        self.assertIn(
            '"tool_opt_ins": [str(item) for item in agent.get("tool_opt_ins") or []]',
            chat_py,
            "run/chat.py 收窄 Agent 字典时必须保留 tool_opt_ins",
        )
        self.assertGreaterEqual(
            chat_py.count('"tool_opt_ins": [str(item) for item in agent.get("tool_opt_ins") or []]'),
            2,
            "提交路径与计划路径两处收窄都要带上它",
        )


if __name__ == "__main__":
    unittest.main()
