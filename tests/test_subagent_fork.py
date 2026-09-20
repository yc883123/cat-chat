# -*- coding: utf-8 -*-
"""守门：子代理的双上下文模式 = **两个互斥工具**（`subagent` fork / `subagent_spawn` spawn）。

背景（2026-09-20）：naiba-chat 的子代理原本只有 fork 一种形态——``run_subagent_agent``
无条件 ``build_model_history(conversation.messages)``，子代理每步推理都重放父会话全量历史。

两种模式的成本特性完全相反：

- **fork**（``subagent`` 工具）：继承父历史，在前缀缓存命中的供应商（DeepSeek/Kimi 官方 API）
  上按缓存价计费；在无缓存或按 ``max_tokens`` 预扣费的中继上则是全价重放，长会话里每个子代理的
  "起步价" = 父会话体量。
- **spawn**（``subagent_spawn`` 工具）：只带 system + instruction，任何供应商下起步都便宜；
  代价是 instruction 必须自包含、且看不到任何前文。

**模式选择权完全归用户**（第一版设计的两条死结：让模型填参数 = 让它猜环境；让后端定默认 =
替它判断任务。所以模式根本不进运行时决策，而是工具集层面的二选一）：

- 用户：在 Agent 工具页勾哪个工具就是哪个模式（两个互斥，勾一个自动取消另一个）；
- 模型：只看到被启用的那个工具，**不需要知道"模式"这个概念**；
- 后端：`fork` 是注册时固化的常量（``subagent_handler_factory(app, fork=...)``），
  模型传什么"fork"参数都不作数；互斥冲突由 ``run/session.normalize_tool_mutex`` 确定性归一
  （保留 ``subagent`` = fork，错向只是费钱）。

存量行为零变化：``subagent`` 依旧 fork；旧 Job 记录没有 ``fork`` 字段，``_coerce_fork``
缺省回落 true ⇒ resume 行为不变、**无需迁移**。

守门分工：本文件管"模式怎么落地"（runner/handler/schema/常驻区文案），互斥归一见
``tests/test_tool_mutex.py``。全量验证口径见 `项目维护说明（修改代码前必读）.md` §六。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.jobs import JobSpec  # noqa: E402
from naiba.skills.agent import (  # noqa: E402
    SUBAGENT_COMMON_RULES,
    SUBAGENT_FORK_RULE,
    SUBAGENT_SPAWN_RULE,
    SkillAgent,
    subagent_context_rule,
)
from naiba.subagent import (  # noqa: E402
    MAX_CHILDREN_PER_PARENT,
    MAX_SUBAGENT_DEPTH,
    SUBAGENT_ALLOWED_TOOLS,
    _coerce_fork,
    run_subagent_agent,
    subagent_handler_factory,
)

# ---------------------------------------------------------------------------
# 测试替身：run_subagent_agent 需要一整套 app（storage/config/executor/vision…）
# ---------------------------------------------------------------------------


class _FakeStorage:
    def __init__(self, conversation: dict) -> None:
        self.conversation = conversation
        self.results: dict[str, dict] = {}

    def get_conversation(self, conversation_id: str):
        if conversation_id != self.conversation["id"]:
            return None
        return self.conversation

    def update_job(self, job_id: str, *, result=None, **kwargs) -> None:
        if result is not None:
            self.results[job_id] = result


class _FakeConfig:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.data = {"agent_system_prompt": "全局兜底人设"}

    def reasoning_replay_options(self) -> dict:
        return {"reasoning_replay_max_chars": 4000, "reasoning_replay_turn_chars": 16000}

    def profile(self, model_key: str) -> dict:
        return {"id": model_key or "test-model"}

    def generation_options(self, model_key: str | None = None) -> dict:
        return {}

    def get_agent(self, agent_id: str) -> dict:
        return {"id": agent_id, "system_prompt": "Agent 人设"}

    def resolve_workspace_dir(self, value: str | None = None):
        return self.workspace


class _FakeJobs:
    """handler 只用到 list() / start()；start() 不真的起线程。"""

    def __init__(self) -> None:
        self.specs: list[JobSpec] = []
        self.existing: list[dict] = []

    def list(self, owner: str = "", active_only: bool = False):
        return list(self.existing)

    def start(self, spec: JobSpec, owner: str = "") -> str:
        self.specs.append(spec)
        return f"job-{len(self.specs)}"


def _make_app(conversation: dict, workspace: Path) -> SimpleNamespace:
    return SimpleNamespace(
        storage=_FakeStorage(conversation),
        config=_FakeConfig(workspace),
        executor=SimpleNamespace(),  # 无 clone_for_permission ⇒ 回落到自身
        catalog=SimpleNamespace(),
        models=SimpleNamespace(complete=lambda *a, **k: None),
        media_collector=None,
        vision=None,
        tool_registry=None,
        jobs=_FakeJobs(),
    )


def _conversation(messages: list[dict]) -> dict:
    return {
        "id": "conv-1",
        "messages": messages,
        "model_key": "online:test",
        "provider_id": "",
        "stream_enabled": 0,
        "agent_id": "agent-1",
        "workspace_dir": "",
        "permission_mode": "auto",
    }


class SubagentContextModeRunnerTests(unittest.TestCase):
    """运行器分叉：唯一差别是 history 的播种（fork 值来自 JobSpec.params）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.app = _make_app(
            _conversation([
                {"role": "user", "content": "上面聊到的那几个文件整理一下"},
                {"role": "assistant", "content": "好的"},
            ]),
            self.workspace,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, params: dict) -> mock.MagicMock:
        """跑一次运行器，返回被 mock 的 SkillAgent.run。"""
        spec = JobSpec(kind="subagent", conversation_id="conv-1", params=params, label="子任务")
        with mock.patch("naiba.skills.agent.SkillAgent") as agent_cls:
            agent_cls.return_value.run.return_value = ("结果", [], [], {})
            run_subagent_agent(self.app, "job-1", spec, None, lambda event: None)
            return agent_cls.return_value.run

    def test_spawn_starts_with_empty_history(self) -> None:
        """spawn（subagent_spawn 工具）：不播种父历史（对齐 dsh 的 seed 缺省语义）。"""
        run = self._run({"instruction": "把这个 JSON 校验一遍", "fork": False})
        self.assertEqual(run.call_args.args[1], [])

    def test_fork_replays_parent_history(self) -> None:
        run = self._run({"instruction": "整理前文提到的文件", "fork": True})
        history = run.call_args.args[1]
        self.assertEqual([item["content"] for item in history], [
            "上面聊到的那几个文件整理一下",
            "好的",
        ])

    def test_missing_fork_key_defaults_to_fork(self) -> None:
        """旧 Job 记录没有 fork 字段 ⇒ 按 fork 处理，存量行为不变（无需迁移）。"""
        run = self._run({"instruction": "整理前文提到的文件"})
        self.assertTrue(run.call_args.args[1], "缺省必须回放父历史")

    def test_string_false_is_treated_as_spawn(self) -> None:
        """落库的值偶尔是字符串（老数据/手工 jobs 表）："false" 必须真的走 spawn。"""
        run = self._run({"instruction": "校验 JSON", "fork": "false"})
        self.assertEqual(run.call_args.args[1], [])

    def test_system_prompt_kept_in_both_modes(self) -> None:
        """spawn 也有子代理自身身份：Agent 人设两种模式都保留。"""
        for fork in (True, False):
            with self.subTest(fork=fork):
                run = self._run({"instruction": "干活", "fork": fork})
                self.assertEqual(run.call_args.args[6], "Agent 人设")

    def test_spawn_still_shares_session_scope(self) -> None:
        """spawn 只砍历史：工具白名单、工作区、走独立 Job 的隔离语义都不受影响。"""
        run = self._run({"instruction": "干活", "fork": False})
        self.assertTrue(run.call_args.args[0], "instruction 必须照常传入")
        self.assertTrue(run.call_args.args[7], "allowed_tools 不得因 spawn 变空")

    def test_empty_parent_history_makes_both_modes_equal(self) -> None:
        """空历史父会话下 fork/spawn 等价（新会话首轮开子代理的常见形态）。"""
        self.app = _make_app(_conversation([]), self.workspace)
        for fork in (True, False):
            with self.subTest(fork=fork):
                self.assertEqual(self._run({"instruction": "干活", "fork": fork}).call_args.args[1], [])


class SubagentContextModeHandlerTests(unittest.TestCase):
    """handler 层：fork 由**工具注册时固化**，模型传参不作数。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.app = _make_app(_conversation([]), self.workspace)
        self.ctx = {
            "conversation_id": "conv-1",
            "run_id": "parent-run",
            "owner_session_id": "sess-1",
            "depth": 0,
            "allowed_tools": list(SUBAGENT_ALLOWED_TOOLS) + ["subagent_spawn"],
            "skill_policy": {"mode": "auto", "skill_ids": []},
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _handler(self, fork: bool):
        return subagent_handler_factory(self.app, fork=fork)

    def _params(self, fork: bool, arguments: dict) -> dict:
        ok, message = self._handler(fork)(arguments, [], self.ctx)
        self.assertTrue(ok, message)
        return self.app.jobs.specs[-1].params

    def test_fork_handler_pins_true(self) -> None:
        self.assertIs(self._params(True, {"instruction": "干活"})["fork"], True)

    def test_spawn_handler_pins_false(self) -> None:
        self.assertIs(self._params(False, {"instruction": "干活"})["fork"], False)

    def test_model_supplied_fork_argument_is_ignored(self) -> None:
        """模型不该、也不能改模式：传了相反的 fork 也以注册时的固化为准。"""
        self.assertIs(self._params(True, {"instruction": "干活", "fork": False})["fork"], True)
        self.assertIs(self._params(False, {"instruction": "干活", "fork": True})["fork"], False)

    def test_job_kind_stays_subagent(self) -> None:
        """两种工具是同一种 Job（kind 不变）：resume / 面板 / 清理路径零迁移。"""
        self._params(False, {"instruction": "干活"})
        self.assertEqual(self.app.jobs.specs[-1].kind, "subagent")

    def test_allowed_tools_filtering_unaffected(self) -> None:
        """模式只影响上下文播种：工具收窄规则照旧（且两个子代理工具都被禁）。"""
        params = self._params(True, {
            "instruction": "干活",
            "allowed_tools": ["read_file", "subagent", "subagent_spawn", "job_wait", "shell"],
        })
        self.assertEqual(params["allowed_tools"], ["read_file"])

    def test_depth_gate_unaffected(self) -> None:
        deep = dict(self.ctx, depth=MAX_SUBAGENT_DEPTH - 1)
        for fork in (True, False):
            with self.subTest(fork=fork):
                ok, message = self._handler(fork)({"instruction": "干活"}, [], deep)
                self.assertFalse(ok)
                self.assertIn("深度", message)

    def test_children_cap_unaffected(self) -> None:
        self.app.jobs.existing = [
            {"parent_job_id": "parent-run"} for _ in range(MAX_CHILDREN_PER_PARENT)
        ]
        ok, message = self._handler(False)({"instruction": "干活"}, [], self.ctx)
        self.assertFalse(ok)
        self.assertIn("最多", message)


def _job_specs() -> dict:
    from naiba.tools.registry import build_job_tool_specs

    return {item.name: item for item in build_job_tool_specs()}


def _system_prompt_strings() -> list[str]:
    """取出 `naiba/skills/agent.py` 的全部字符串常量（AST：隐式拼接的多行文本算一条）。"""
    import ast

    source = (ROOT / "naiba" / "skills" / "agent.py").read_text(encoding="utf-8")
    return [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


class SubagentToolPairSchemaTests(unittest.TestCase):
    """两个工具必须**同形**：参数一模一样，模式不在参数里（模型零判断）。"""

    EXPECTED_PARAMS = {"instruction", "allowed_tools", "label"}

    def test_both_tools_are_registered(self) -> None:
        specs = _job_specs()
        self.assertIn("subagent", specs)
        self.assertIn("subagent_spawn", specs, "spawn 模式必须是一个独立工具")

    def test_parameters_are_identical_and_carry_no_fork(self) -> None:
        specs = _job_specs()
        for name in ("subagent", "subagent_spawn"):
            with self.subTest(tool=name):
                params = specs[name].parameters
                self.assertEqual(set(params["properties"]), self.EXPECTED_PARAMS)
                self.assertEqual(params["required"], ["instruction"])
                self.assertNotIn("fork", params["properties"],
                                 "模式不是模型参数（第一版设计已废弃，见 §九.116）")

    def test_descriptions_split_the_two_modes_within_budget(self) -> None:
        """描述有 ≤100 字 / ≤3 句预算（§九.23）：分工靠描述，规则正文在常驻区。"""
        specs = _job_specs()
        fork_desc = specs["subagent"].description
        spawn_desc = specs["subagent_spawn"].description
        for desc in (fork_desc, spawn_desc):
            with self.subTest(desc=desc[:16]):
                self.assertLessEqual(len(desc), 100)
                self.assertLessEqual(len([s for s in desc.split("。") if s.strip()]), 3)
        self.assertIn("完整历史", fork_desc)
        self.assertNotIn("完整历史", spawn_desc)
        self.assertIn("不带任何会话历史", spawn_desc)
        self.assertIn("自包含", spawn_desc)


class SubagentResidentRuleTests(unittest.TestCase):
    """完整规则（成本特性 + 用法）必须落在系统提示常驻区，且**只讲启用的那个**。"""

    def test_rule_picker_follows_the_enabled_tool(self) -> None:
        self.assertEqual(subagent_context_rule({"subagent"}), SUBAGENT_FORK_RULE)
        self.assertEqual(subagent_context_rule({"subagent_spawn"}), SUBAGENT_SPAWN_RULE)
        self.assertEqual(subagent_context_rule({"read_file"}), "")
        # 互斥归一后不该出现"两个都在"，真出现了也以 fork 为准（安全方向）。
        self.assertEqual(
            subagent_context_rule({"subagent", "subagent_spawn"}), SUBAGENT_FORK_RULE
        )

    def test_fork_rule_describes_cost_shape(self) -> None:
        for needle in ("重发父会话历史", "缓存", "预扣费", "instruction"):
            with self.subTest(needle=needle):
                self.assertIn(needle, SUBAGENT_FORK_RULE)

    def test_spawn_rule_says_self_contained(self) -> None:
        for needle in ("不带任何会话历史", "自包含", "instruction"):
            with self.subTest(needle=needle):
                self.assertIn(needle, SUBAGENT_SPAWN_RULE)

    def test_rules_are_independent(self) -> None:
        """两条规则不得互相串味：spawn 段里不许再教模型"用默认的 fork"。"""
        self.assertNotIn("fork", SUBAGENT_SPAWN_RULE)
        self.assertNotIn("spawn", SUBAGENT_FORK_RULE)

    def test_common_rules_keep_the_hard_limits(self) -> None:
        self.assertIn("最多 4 个", SUBAGENT_COMMON_RULES)
        self.assertIn("不能再派生", SUBAGENT_COMMON_RULES)

    def test_rules_live_in_the_resident_area(self) -> None:
        """三段文案都必须是 `skills/agent.py` 里的字符串常量（常驻区，不进工具描述）。"""
        strings = _system_prompt_strings()
        for rule in (SUBAGENT_FORK_RULE, SUBAGENT_SPAWN_RULE, SUBAGENT_COMMON_RULES):
            with self.subTest(rule=rule[:12]):
                self.assertIn(rule, strings)


class _EmptyCatalog:
    def scan(self) -> list:
        return []


class SubagentRuleRealPromptTests(unittest.TestCase):
    """真路径：常驻区必须按**实际启用的工具**注入对应那条（源码级断言不算证据）。

    与 §九.92 同一条纪律——配置驱动的东西必须跑真实链路，静态断言只能证明"源码里有这行"。
    """

    PROFILE = {"kind": "online", "base_url": "https://teynex.com", "model": "kimi-k2"}

    def _system_prompt(self, allowed: list[str]) -> str:
        captured: list[dict] = []

        def complete(profile_arg, messages, options, event):
            captured.extend(messages)
            return "完成"

        worker = SkillAgent(_EmptyCatalog(), None, complete, None)
        worker.run(
            "干活",
            [],
            self.PROFILE,
            {"max_steps": 1, "stream": False},
            {"mode": "auto", "skill_ids": []},
            [],
            "",
            allowed,
            lambda payload: None,
            None,
        )
        systems = [item for item in captured if item.get("role") == "system"]
        self.assertTrue(systems, "没有拼出 system 消息，测试桩失效")
        return "\n".join(str(item.get("content") or "") for item in systems)

    def test_fork_tool_gets_the_fork_rule_only(self) -> None:
        prompt = self._system_prompt(["subagent"])
        self.assertIn(SUBAGENT_FORK_RULE, prompt)
        self.assertIn(SUBAGENT_COMMON_RULES, prompt)
        self.assertNotIn(SUBAGENT_SPAWN_RULE, prompt, "开了 fork 就不该出现 spawn 的规则")

    def test_spawn_tool_gets_the_spawn_rule_only(self) -> None:
        prompt = self._system_prompt(["subagent_spawn"])
        self.assertIn(SUBAGENT_SPAWN_RULE, prompt)
        self.assertIn(SUBAGENT_COMMON_RULES, prompt)
        self.assertNotIn(SUBAGENT_FORK_RULE, prompt, "开了 spawn 就不该出现 fork 的规则")

    def test_no_subagent_tool_no_rule(self) -> None:
        """缺工具就不提（与视觉/PDF 段同口径）。"""
        prompt = self._system_prompt(["read_file"])
        for rule in (SUBAGENT_FORK_RULE, SUBAGENT_SPAWN_RULE, SUBAGENT_COMMON_RULES):
            with self.subTest(rule=rule[:12]):
                self.assertNotIn(rule, prompt)


class CoerceForkTests(unittest.TestCase):
    """归一化边界：只有显式假值才 spawn，其余一律 fork。"""

    def test_explicit_and_ambiguous_values(self) -> None:
        cases = [
            (True, True), (False, False),
            ("false", False), ("False", False), (" off ", False), ("0", False), ("no", False),
            ("true", True), ("yes", True), ("1", True),
            (0, False), (1, True),
            (None, True), ("", True), ("随便写的", True), ({}, True), ([], True),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertIs(_coerce_fork(raw), expected)


if __name__ == "__main__":
    unittest.main()
