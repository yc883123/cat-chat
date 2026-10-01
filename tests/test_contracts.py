# -*- coding: utf-8 -*-
"""契约守卫：RunContext / EventType / MetadataKeys 的一致性校验。

1. EventType 必须覆盖前端可处置的全部事件 type（从 public/app.js handleChatEvent 反查），
   防止前后端事件协议漂移（wire 兼容由本测试钉死，后端新增事件必须先入枚举）。
2. RunContext 契约键存在性 + 构造样本可运行（TypedDict 运行时即 dict）。
3. MetadataKeys 与 core/history.py 读取的 key 保持一致（读者已常量化的部分）。
"""

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core.contracts import (  # noqa: E402
    AppContext, ConfigView, EventPayload, EventType, MetadataKeys, RunContext,
    RUN_CONTEXT_KEYS, default_run_context, validate_run_context,
)


class EventContractTests(unittest.TestCase):
    def test_frontend_handled_events_subset_of_enum(self):
        # 前端已按域拆分为 public/js/*.js（阶段 5.1），handleChatEvent 已路由表化
        # （阶段 3：CHAT_EVENT_HANDLERS 的键即前端可处置事件全集），从路由表反查。
        js_dir = ROOT / "public" / "js"
        chunks = []
        for path in sorted(js_dir.glob("*.js")):
            chunks.append(path.read_text(encoding="utf-8"))
        js = "\n".join(chunks)
        table = js[js.index("const CHAT_EVENT_HANDLERS = {"):]
        frontend_types = set(re.findall(r"""^\s{2}([a-z_]+):\s*handle[A-Za-z]+Event,?$""", table, re.M))
        self.assertGreaterEqual(len(frontend_types), 20, "前端事件表解析异常，请检查 12-chat-input.js 路由表")
        missing = sorted(t for t in frontend_types if t not in EventType.__members__.values())
        self.assertEqual(missing, [], f"前端可处置的事件 type 不在 EventType 枚举中：{missing}")

    def test_run_context_contract_keys(self):
        canonical = {
            "run_id", "job_id", "conversation_id", "owner_session_id", "parent_job_id",
            "depth", "allowed_tools", "skill_policy", "job_registry", "executor",
            "cancel_event", "vision_budget", "interaction_mode", "routing_message",
        }
        annotations = set(RunContext.__annotations__)
        self.assertTrue(canonical <= annotations, f"RunContext 缺失契约键：{canonical - annotations}")

    def test_run_context_sample_is_runtime_dict(self):
        ctx: RunContext = {
            "run_id": "r1",
            "conversation_id": "c1",
            "owner_session_id": "c1",
            "depth": 0,
            "allowed_tools": ["read_file"],
            "interaction_mode": "craft",
            "routing_message": "hello",
        }
        # TypedDict 运行时就是 dict：消费方现有 .get 语义不变。
        self.assertIsInstance(ctx, dict)
        self.assertEqual(ctx["run_id"], "r1")

    def test_metadata_keys_readers_use_constants(self):
        history_src = (ROOT / "naiba" / "core" / "history.py").read_text(encoding="utf-8")
        self.assertIn("MetadataKeys.ATTACHMENTS", history_src)
        self.assertIn("MetadataKeys.TRACE", history_src)
        self.assertIn("MetadataKeys.TOOL_RUNS", history_src)
        self.assertIn("MetadataKeys.REASONING", history_src)
        # 键值必须与契约一致（若改契约值，此处立即失败）。
        self.assertEqual(MetadataKeys.ATTACHMENTS, "attachments")
        self.assertEqual(MetadataKeys.TRACE, "trace")

    def test_run_context_default_factory_and_validation(self):
        default = default_run_context()
        self.assertEqual(default["interaction_mode"], "craft")
        self.assertEqual(default["depth"], 0)
        self.assertEqual(validate_run_context(default), [])
        self.assertEqual(validate_run_context({}), [])
        # 非法键必须被标记（防新增裸键逃逸）。
        self.assertIn("typo_key", validate_run_context({"typo_key": 1}))
        self.assertGreater(len(validate_run_context("not-a-dict")), 0)
        self.assertEqual(set(RUN_CONTEXT_KEYS), set(RunContext.__annotations__))

    def test_run_context_bare_keys_are_registered(self):
        """源码扫描：以 `run_context` / `run_ctx` 名义取用的**字面量键**必须登记进契约（§九.148）。

        `validate_run_context` 只在测试里跑，生产侧的 run_context 就是普通 dict —— 真正防漂移的是
        这条扫描：写侧 `run_context[k] = …`、读侧 `run_context.get(k)` / `(run_context or {}).get(k)`
        里出现的每个字面量键都要在 `RUN_CONTEXT_KEYS` 里。它当场逮到两枚裸键——
        `context_reset`（`tools/providers/core.py` 写，`run/chat.py` 与 `skills/agent.py` 两处读，
        正是「模型划分割线」这条链）与 `trace_system`（agent 写、run/chat 读，first_turn 落盘用）。
        口径限定在「变量名为 run_context/run_ctx 的字面量取用」：经 snapshot/ctx/frozen 等别名转发的
        读法不在判据内（否则会误伤无关 dict），那类键由下面那条点名钉桩兜住。
        """
        import ast

        names = {"run_context", "run_ctx"}

        def mentions(node) -> bool:
            return any(isinstance(sub, ast.Name) and sub.id in names for sub in ast.walk(node))

        found: dict[str, list[str]] = {}
        for path in sorted((ROOT / "naiba").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel = path.relative_to(ROOT).as_posix()
            for node in ast.walk(tree):
                key = None
                if isinstance(node, ast.Subscript) and mentions(node.value):
                    if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                        key = node.slice.value
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in ("get", "pop", "setdefault", "update") and mentions(node.func.value):
                        if node.args and isinstance(node.args[0], ast.Constant):
                            if isinstance(node.args[0].value, str):
                                key = node.args[0].value
                if key is None:
                    continue
                found.setdefault(key, []).append(f"{rel}:{getattr(node, 'lineno', 0)}")
        # 早停判据：扫到的键太少说明解析口径失效，不能让守门空转。
        self.assertGreaterEqual(len(found), 15, f"扫描到的 run_context 字面量键过少（{len(found)}），判据失效")
        escaped = {k: v for k, v in found.items() if k not in set(RUN_CONTEXT_KEYS)}
        self.assertFalse(
            escaped,
            "run_context 裸键未登记进契约：" + "；".join(f"{k} ← {v[0]}" for k, v in sorted(escaped.items())),
        )

    def test_cross_layer_run_context_keys_are_pinned(self):
        """两枚曾逃逸的跨层键点名钉桩（别名转发的读法扫描够不着，这里兜住）。"""
        for key in ("context_reset", "trace_system"):
            self.assertIn(key, RUN_CONTEXT_KEYS, f"{key} 必须登记进 RUN_CONTEXT_KEYS（§九.148）")
            self.assertIn(key, RunContext.__annotations__, f"{key} 必须在 RunContext 注解里")
            self.assertIn(key, default_run_context(), f"{key} 必须在 default_run_context() 里有默认值")
        self.assertFalse(default_run_context()["context_reset"], "缺省必须是假值：空 dict = 本轮无重置请求")
        self.assertEqual(default_run_context()["trace_system"], "")

    def test_message_order_contract_is_backend_authority(self):
        # 历史消息排序契约：后端唯一决定顺序（(created_at, rowid)），前端按 API
        # 返回数组顺序渲染、不自行排序。存储层 SQL 与契约常量必须一致。
        from naiba.core.contracts import MESSAGE_ORDER_KEYS

        store_src = (ROOT / "naiba" / "storage" / "store.py").read_text(encoding="utf-8")
        self.assertIn("ORDER BY " + ", ".join(MESSAGE_ORDER_KEYS), store_src, "会话查询未按契约排序（消息顺序权威在后端）")

    def test_config_view_and_app_context_protocols(self):
        # runtime_checkable：ConfigStore / NaibaChatApp 真实实例必须满足注入协议。
        import tempfile
        from pathlib import Path as P

        from naiba.app import NaibaChatApp
        from naiba.config import ConfigStore
        from naiba.paths import PathContext

        with tempfile.TemporaryDirectory() as tmp:
            root = P(tmp)
            paths = PathContext.local(root, root / "config.json")
            store = ConfigStore(root / "config.json", paths=paths)
            self.assertIsInstance(store, ConfigView)
            app = NaibaChatApp(paths=paths)
            self.assertIsInstance(app, AppContext)
            self.assertIsInstance(app.config, ConfigView)


class EventPayloadContractTests(unittest.TestCase):
    """事件负载契约（收官线 ③ EventPayload）：golden 反查 + 核心 type→键映射。"""

    EVENT_KEYS = set(EventPayload.__annotations__)
    # 核心事件 type → 必备负载键（从实际 emit 点固化；新增字段先入 EventPayload 再发事件）
    CORE_TYPES = {
        "status": {"type", "message"},
        "delta": {"type", "content"},
        "reasoning_delta": {"type", "content"},
        "tool_start": {"type", "tool"},
        "tool_confirm": {"type", "tool_name", "tool_desc", "arguments", "confirm_id"},
        "tool_result": {"type", "tool"},
        "skills": {"type", "skills"},
        "cancelled": {"type", "message"},
        "error": {"type", "message"},
        "choice": {"type", "choices", "choice_groups"},
        "context_full": {"type", "limit", "used", "budget"},
    }

    def _walk_event_dicts(self, node, path="$"):
        if isinstance(node, dict):
            if isinstance(node.get("type"), str):
                # metadata.activity 条目（reasoning/tool/prose）是消息展示结构，不是
                # 事件流负载——其键（ts/request_index 等）由活动时间线契约管理，跳过。
                if ".metadata.activity[" not in path:
                    yield node
            for key, value in node.items():
                yield from self._walk_event_dicts(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                yield from self._walk_event_dicts(item, f"{path}[{index}]")

    def test_golden_payload_keys_subset_of_contract(self):
        golden_dir = ROOT / "tests" / "golden"
        collected = 0
        for baseline in sorted(golden_dir.glob("*.json")):
            data = json.loads(baseline.read_text(encoding="utf-8"))
            for event in self._walk_event_dicts(data):
                event_type = event["type"]
                if event_type not in EventType.__members__.values():
                    continue  # 非事件流 dict（如 {"type": "text"} 内容块）
                collected += 1
                keys = set(event.keys())
                extra = keys - self.EVENT_KEYS
                self.assertFalse(extra, f"事件 {event_type} 负载键未登记进 EventPayload：{sorted(extra)}")
        self.assertGreaterEqual(collected, 12, f"golden 基线中事件负载过少（{collected}）")

    def test_core_type_key_mapping_in_contract(self):
        for event_type, keys in self.CORE_TYPES.items():
            missing = keys - self.EVENT_KEYS
            self.assertFalse(missing, f"核心事件 {event_type} 的负载键未登记：{sorted(missing)}")

    def test_metadata_keys_moved_to_messages_with_reexport(self):
        from naiba.core import contracts as contract_mod
        from naiba.core import messages as messages_mod

        self.assertIs(contract_mod.MetadataKeys, messages_mod.MetadataKeys)
        self.assertIs(contract_mod.MESSAGE_METADATA_KEYS, messages_mod.MESSAGE_METADATA_KEYS)
        # 权威清单 == 类属性全集（防新增键漏登记清单）
        self.assertEqual(
            set(messages_mod.MESSAGE_METADATA_KEYS),
            {value for name, value in vars(messages_mod.MetadataKeys).items() if not name.startswith("_")},
        )
        self.assertEqual(messages_mod.MetadataKeys.ATTACHMENTS, "attachments")


if __name__ == "__main__":
    unittest.main()
