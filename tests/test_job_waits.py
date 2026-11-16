# -*- coding: utf-8 -*-
"""普通等待（job_wait notify_on_completion）与后台自动续答守门（计划 2026-11-16）。

冻结的不变量：

1. 普通 `job_wait` 保持同步语义；等待到期仍运行是**正常快照**（True），只有
   failed / cancelled / interrupted 是失败；`timeout` 严格校验，0 不再暗改成 600。
2. 只有「确实等完期限、任务仍在运行」这一种结构化结果不累计无进展熔断；
   `job_status`、重复读取终态、普通工具仍熔断。
3. `notify_on_completion=true` 仅普通主对话顶层 Run、本会话自己的 Job 可登记；
   辅助对话 / 子 Agent / 监督续办 / 依赖续办 / 跨会话一律拒绝。
4. 登记成功才走交接出口：本轮以固定提示正常收尾，不调用下一次模型请求。
5. 等待表：每会话最多一条有效等待；重复登记同来源同目标复用；上限集中成常量。
6. 续答派发在同一事务里核对等待状态、人工代次与会话运行位；重复派发被拒绝。
7. 用户新输入 / 插话撤销等待；服务重启一律暂停；「继续查看结果」只重置等待，不重新生成。
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.core.contracts import JOB_WAIT_OUTCOME_KEY  # noqa: E402
from naiba.core.messages import MetadataKeys  # noqa: E402
from naiba.jobs import JobRegistry  # noqa: E402
from naiba.run.job_waits import JobWaitCoordinator, build_resume_message, summarize_job_result  # noqa: E402
from naiba.skills.agent import SkillAgent  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402
from naiba.subagent import job_tool_handler_factory  # noqa: E402

AGENT = {"id": "agent_1", "name": "Agent", "system_prompt": "", "skill_ids": []}


def _storage() -> tuple[ChatStorage, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="naiba_job_waits_"))
    return ChatStorage(tmp / "chat.db"), tmp


def _conversation(storage: ChatStorage) -> str:
    return str(storage.create_conversation("等待测试")["id"])


def _make_job(storage: ChatStorage, conversation_id: str, *, status: str = "running",
              parent: str = "", kind: str = "shell", result: dict | None = None) -> str:
    run = storage.create_run(
        conversation_id, f"Job({kind})", AGENT, {"job_spec": {"kind": kind}},
        kind=kind, parent_job_id=parent, owner_session_id=conversation_id,
    )
    job_id = str(run["id"])
    storage.update_job(
        job_id, status=status, result=result or {}, finished=status in {"completed", "failed", "cancelled"},
    )
    return job_id


class _Jobs:
    """job_wait 处理器的最小 Job 视图（按需可配 running / completed / 多目标）。"""

    def __init__(self, job: dict, children: list[dict] | None = None,
                 wait_job: dict | None = None, finish_after: int | None = None) -> None:
        self.job = job
        self.child_rows = list(children or [])
        self.wait_job = wait_job or job
        self.finish_after = finish_after
        self.get_calls = 0
        self.waited: list[tuple[str, float]] = []

    def get(self, job_id, owner=None):
        self.get_calls += 1
        if job_id == self.job["id"]:
            if self.finish_after is not None and self.get_calls > self.finish_after:
                return dict(self.wait_job)
            return dict(self.job)
        for item in self.child_rows:
            if item["id"] == job_id:
                return dict(item)
        return None

    def wait(self, job_id, timeout, owner=None, *, cancel_event=None):
        self.waited.append((job_id, timeout))
        if cancel_event is not None and cancel_event.is_set():
            from naiba.core.exceptions import TaskCancelled

            raise TaskCancelled("等待已停止")
        # 短等期间完成任务：后续 get() 必须看到终态，否则 _wait_for_targets 会空转到超时。
        if str(self.wait_job.get("status") or "") != "running":
            self.job = dict(self.wait_job)
        return dict(self.wait_job)

    def children(self, parent_job_id, active_only=False):
        return [dict(item) for item in self.child_rows]

    def list(self, owner=None, active_only=False):
        return []


class _Coordinator:
    def __init__(self) -> None:
        self.registered: list[tuple[str, str, list[str]]] = []
        self.cancelled: list[tuple[str, str]] = []
        self.fail = ""

    def register(self, *, conversation_id, origin_run_id, target_job_ids, snapshot):
        if self.fail:
            return None, self.fail
        self.registered.append((conversation_id, origin_run_id, list(target_job_ids)))
        return {"id": "wait_1", "target_job_ids": list(target_job_ids)}, ""

    def cancel_wait(self, wait_id, reason=""):
        self.cancelled.append((wait_id, reason))
        return {"id": wait_id, "status": "cancelled"}


class _App:
    def __init__(self, jobs, coordinator=None) -> None:
        self.jobs = jobs
        self.job_waits = coordinator
        self.storage = type("S", (), {"is_job_cleaned": staticmethod(lambda job_id: False)})()


class JobWaitValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.job = {"id": "job_1", "status": "running", "conversation_id": "conv_1",
                    "owner_session_id": "conv_1", "progress": 10, "current_step": "跑", "result": {}}
        self.handler = job_tool_handler_factory(_App(_Jobs(self.job)))["job_wait"]

    def _ctx(self, **extra) -> dict:
        base = {
            "conversation_id": "conv_1", "owner_session_id": "conv_1", "run_id": "run_1",
            "depth": 0, "conv_kind": "main", "run_origin": "user",
            "allowed_tools": ["job_wait", "job_output"], "tool_call_slot": {},
        }
        base.update(extra)
        return base

    def test_timeout_zero_and_negative_are_rejected(self) -> None:
        for value in (0, -5, 1.5, "abc", True):
            ok, text = self.handler({"job_id": "job_1", "timeout": value}, [], self._ctx())
            self.assertFalse(ok, f"timeout={value!r} 必须拒绝")
            self.assertIn("timeout", text)

    def test_timeout_uppercase_is_not_coerced_to_default(self) -> None:
        ok, text = self.handler({"job_id": "job_1", "timeout": 0}, [], self._ctx())
        self.assertFalse(ok)
        self.assertNotIn("仍在运行", text)

    def test_notify_must_be_json_boolean(self) -> None:
        for value in ("true", 1, None):
            ok, text = self.handler(
                {"job_id": "job_1", "notify_on_completion": value}, [], self._ctx()
            )
            self.assertFalse(ok, f"{value!r} 不是 JSON 布尔值，必须拒绝")
            self.assertIn("布尔", text)

    def test_aux_subagent_and_supervised_cannot_register(self) -> None:
        coordinator = _Coordinator()
        handler = job_tool_handler_factory(_App(_Jobs(self.job), coordinator))["job_wait"]
        cases = (
            {"conv_kind": "aux"},
            {"depth": 1},
            {"run_origin": "supervised"},
            {"run_origin": "dependency"},
        )
        for extra in cases:
            ok, text = handler(
                {"job_id": "job_1", "notify_on_completion": True}, [], self._ctx(**extra)
            )
            self.assertFalse(ok, f"{extra} 不应登记成功")
            self.assertTrue("登记" in text, text)
        self.assertEqual(coordinator.registered, [])

    def test_cross_conversation_job_is_rejected(self) -> None:
        coordinator = _Coordinator()
        job = {**self.job, "conversation_id": "conv_other", "owner_session_id": "conv_other"}
        handler = job_tool_handler_factory(_App(_Jobs(job), coordinator))["job_wait"]
        ok, text = handler({"job_id": "job_1", "notify_on_completion": True}, [], self._ctx())
        self.assertFalse(ok)
        self.assertIn("不属于当前会话", text)
        self.assertEqual(coordinator.registered, [])

    def test_pending_timeout_is_recorded_as_trusted_outcome(self) -> None:
        ctx = self._ctx()
        ok, text = self.handler({"job_id": "job_1", "timeout": 1}, [], ctx)
        self.assertTrue(ok, text)
        self.assertEqual(ctx["tool_call_slot"][JOB_WAIT_OUTCOME_KEY]["kind"], "pending_timeout")
        self.assertTrue(ctx["tool_call_slot"][JOB_WAIT_OUTCOME_KEY]["waited"])

    def test_completed_job_does_not_register(self) -> None:
        coordinator = _Coordinator()
        job = {**self.job, "status": "completed", "result": {"response": "ok"}}
        handler = job_tool_handler_factory(_App(_Jobs(job), coordinator))["job_wait"]
        ok, text = handler({"job_id": "job_1", "notify_on_completion": True}, [], self._ctx())
        self.assertTrue(ok, text)
        self.assertIn("已完成", text)
        self.assertEqual(coordinator.registered, [], "已终态不得创建无用续答")

    def test_registration_failure_does_not_claim_auto_reply(self) -> None:
        coordinator = _Coordinator()
        coordinator.fail = "已达上限"
        handler = job_tool_handler_factory(_App(_Jobs(self.job), coordinator))["job_wait"]
        ctx = self._ctx()
        ok, text = handler({"job_id": "job_1", "notify_on_completion": True}, [], ctx)
        self.assertFalse(ok)
        self.assertIn("未能登记", text)
        self.assertNotIn("handoff", ctx["tool_call_slot"].get(JOB_WAIT_OUTCOME_KEY, {}).get("kind", ""))

    def test_registered_still_running_hands_off(self) -> None:
        coordinator = _Coordinator()
        handler = job_tool_handler_factory(_App(_Jobs(self.job), coordinator))["job_wait"]
        ctx = self._ctx()
        with mock.patch("naiba.subagent.JOB_WAIT_SHORT_WAIT_SECONDS", 0.2):
            ok, text = handler(
                {"job_id": "job_1", "notify_on_completion": True, "timeout": 120}, [], ctx
            )
        self.assertTrue(ok, text)
        self.assertEqual(ctx["tool_call_slot"][JOB_WAIT_OUTCOME_KEY]["kind"], "handoff")
        self.assertEqual(ctx["tool_call_slot"][JOB_WAIT_OUTCOME_KEY]["wait_id"], "wait_1")
        self.assertIn("已登记", text)
        self.assertEqual(len(coordinator.registered), 1)

    def test_registered_job_completes_during_short_wait_without_handoff(self) -> None:
        coordinator = _Coordinator()
        finished = {**self.job, "status": "completed", "result": {"response": "done"}}
        handler = job_tool_handler_factory(
            _App(_Jobs(self.job, wait_job=finished, finish_after=3), coordinator)
        )["job_wait"]
        ctx = self._ctx()
        ok, text = handler({"job_id": "job_1", "notify_on_completion": True}, [], ctx)
        self.assertTrue(ok, text)
        self.assertIn("已完成", text)
        self.assertEqual(coordinator.cancelled[0][0], "wait_1")
        self.assertNotEqual(ctx["tool_call_slot"][JOB_WAIT_OUTCOME_KEY]["kind"], "handoff")

    def test_child_jobs_are_included_in_targets(self) -> None:
        coordinator = _Coordinator()
        child = {"id": "child_1", "status": "running", "conversation_id": "conv_1",
                 "owner_session_id": "conv_1", "detached": False}
        handler = job_tool_handler_factory(
            _App(_Jobs(self.job, children=[child]), coordinator)
        )["job_wait"]
        with mock.patch("naiba.subagent.JOB_WAIT_SHORT_WAIT_SECONDS", 0.2):
            ok, text = handler({"job_id": "job_1", "notify_on_completion": True}, [], self._ctx())
        self.assertTrue(ok, text)
        self.assertEqual(coordinator.registered[0][2], ["child_1", "job_1"])


class _LoopCatalog:
    def scan(self) -> list:
        return []

    def read_skill_content(self, path: str) -> str:  # pragma: no cover
        return ""


class _LoopRegistry:
    """把工具调用转给真实 job_wait 处理器；结果始终相同以触发无进展熔断。"""

    def __init__(self, handler, outcome: dict | None = None) -> None:
        self.handler = handler
        self.outcome = outcome
        self.calls = 0

    def schemas(self) -> list:
        return []

    def side_effect(self, name: str) -> bool:
        return False

    def retryable(self, name: str) -> bool:
        return False

    def media_declaration(self, name: str) -> dict:
        return {"extract": "none", "policy": "never"}

    def execute(self, tool: str, arguments: dict, active: list, run_context: object):
        self.calls += 1
        return self.handler(arguments, active, run_context)


class PendingWaitBreakerTests(unittest.TestCase):
    """切片 A：真实等待到期快照不得触发「连续相同结果 = 无进展」熔断。"""

    def _run(self, handler, max_steps: int) -> tuple[str, list[dict]]:
        events: list[dict] = []
        registry = _LoopRegistry(handler)
        worker = SkillAgent(
            _LoopCatalog(), None,
            lambda profile, messages, options, event: '{"type":"tool","tool":"job_wait","arguments":{"job_id":"job_1","timeout":1}}',
            None,
        )
        ctx = {
            "conversation_id": "conv_1", "owner_session_id": "conv_1", "run_id": "run_1",
            "depth": 0, "conv_kind": "main", "run_origin": "user",
            "allowed_tools": ["job_wait"], "tool_call_slot": {},
        }
        response, _runs, _reasonings, _usage = worker.run(
            "等结果", [], {"kind": "online", "model": "m", "context_window": 128000},
            {"max_steps": max_steps, "stream": False}, {"mode": "auto", "skill_ids": []},
            [], "", ["job_wait"], lambda payload: events.append(payload), None,
            tool_registry=registry, run_context=ctx,
        )
        return response, events

    def test_repeated_pending_timeout_is_not_a_no_progress_breaker(self) -> None:
        job = {"id": "job_1", "status": "running", "conversation_id": "conv_1",
               "owner_session_id": "conv_1", "progress": 5, "current_step": "跑", "result": {}}
        handler = job_tool_handler_factory(_App(_Jobs(job)))["job_wait"]
        response, events = self._run(handler, 5)
        self.assertNotIn("任务没有进展", response, response)
        self.assertFalse(
            [item for item in events
             if item.get("type") == "run_failed" and "没有进展" in str(item.get("error") or "")],
            "正常等待到期不能被当成失败熔断",
        )
        self.assertIn("步数上限", response)

    def test_handoff_finishes_the_turn_without_another_model_request(self) -> None:
        job = {"id": "job_1", "status": "running", "conversation_id": "conv_1",
               "owner_session_id": "conv_1", "progress": 5, "current_step": "跑", "result": {}}
        coordinator = _Coordinator()
        handler = job_tool_handler_factory(_App(_Jobs(job), coordinator))["job_wait"]

        prompts: list[int] = []

        def complete(profile, messages, options, event):
            prompts.append(1)
            return ('{"type":"tool","tool":"job_wait","arguments":'
                    '{"job_id":"job_1","notify_on_completion":true,"timeout":120}}')

        worker = SkillAgent(_LoopCatalog(), None, complete, None)
        ctx = {
            "conversation_id": "conv_1", "owner_session_id": "conv_1", "run_id": "run_1",
            "depth": 0, "conv_kind": "main", "run_origin": "user",
            "allowed_tools": ["job_wait"], "tool_call_slot": {},
        }
        with mock.patch("naiba.subagent.JOB_WAIT_SHORT_WAIT_SECONDS", 0.2):
            response, _runs, _reasonings, _usage = worker.run(
                "等结果", [], {"kind": "online", "model": "m", "context_window": 128000},
                {"max_steps": 8, "stream": False}, {"mode": "auto", "skill_ids": []},
                [], "", ["job_wait"], lambda payload: None, None,
                tool_registry=_LoopRegistry(handler), run_context=ctx,
            )
        self.assertEqual(len(prompts), 1, "交接后不得再请求模型写承诺")
        self.assertEqual(response, "已登记，后台任务结束后会在这里回复。")
        self.assertEqual(ctx["job_wait_handoff"]["wait_id"], "wait_1")


class JobWaitStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage, self.tmp = _storage()
        self.conversation_id = _conversation(self.storage)
        self.storage.add_message(self.conversation_id, "user", "生成")
        self.origin = _make_job(self.storage, self.conversation_id, status="completed", kind="chat")
        self.target = _make_job(self.storage, self.conversation_id, status="running", parent=self.origin)
        self.snapshot = {"model_key": "online:p1", "allowed_tools": ["job_wait", "job_output"]}

    def _register(self, **extra):
        return self.storage.create_job_wait(
            conversation_id=self.conversation_id,
            origin_run_id=self.origin if "origin_run_id" not in extra else extra["origin_run_id"],
            target_job_ids=extra.get("targets", [self.target]),
            snapshot=self.snapshot,
        )

    def _attach_card(self, wait_id: str, state: str = "waiting") -> dict:
        message = self.storage.add_message(
            self.conversation_id, "assistant", "已登记",
            {
                MetadataKeys.SESSION_START: {"note": "保留会话边界"},
                MetadataKeys.ATTACHMENTS: [{"name": "result.png"}],
                MetadataKeys.LOCAL_IMAGES_CAPPED: {"names": ["old.png"]},
                MetadataKeys.JOB_WAIT: {"wait_id": wait_id, "state": state},
            },
        )
        self.assertTrue(self.storage.update_job_wait(
            wait_id, status=state, origin_message_id=message["id"],
        ))
        return message

    def _assert_card(self, wait_id: str, message: dict, state: str) -> dict:
        conversation = self.storage.get_conversation(self.conversation_id)
        stored = next(item for item in conversation["messages"] if item["id"] == message["id"])
        metadata = stored["metadata"]
        for key, value in message["metadata"].items():
            if key != MetadataKeys.JOB_WAIT:
                self.assertEqual(metadata[key], value, f"共享键 {key} 不得丢失")
        card = metadata[MetadataKeys.JOB_WAIT]
        wait = self.storage.get_job_wait(wait_id)
        self.assertEqual(card["wait_id"], wait_id)
        self.assertEqual(card["state"], state)
        self.assertEqual(card["state"], wait["status"])
        self.assertEqual(card["reason"], wait["reason"])
        self.assertEqual(card["targets"], wait["target_job_ids"])
        return card

    def test_register_reuse_and_one_active_per_conversation(self) -> None:
        record, note = self._register()
        self.assertIsNotNone(record)
        self.assertEqual(note, "")
        reused, reuse_note = self._register()
        self.assertEqual(reuse_note, "reuse")
        self.assertEqual(reused["id"], record["id"])
        other, other_note = self.storage.create_job_wait(
            conversation_id=self.conversation_id,
            origin_run_id="other_run",
            target_job_ids=["other_job"],
            snapshot=self.snapshot,
        )
        self.assertIsNone(other, "每会话最多一条有效等待")
        self.assertIn("已有一条等待", other_note)

    def test_target_limit_is_rejected(self) -> None:
        record, note = self.storage.create_job_wait(
            conversation_id=self.conversation_id,
            origin_run_id=self.origin,
            target_job_ids=[f"job_{i}" for i in range(101)],
            snapshot=self.snapshot,
        )
        self.assertIsNone(record)
        self.assertIn("上限", note)

    def test_dispatch_is_transactional_and_not_repeatable(self) -> None:
        record, _ = self._register()
        wait_id = record["id"]
        message = self._attach_card(wait_id)
        self.assertTrue(self.storage.update_job_wait(
            wait_id, status="ready", expected_statuses=("waiting",)
        ))
        run = self.storage.create_chat_run(
            self.conversation_id, "结果摘要", [], AGENT, self.snapshot, "craft",
            job_wait_dispatch={
                "wait_id": wait_id, "dispatch_key": "d1",
                "source": {"wait_id": wait_id, "targets": [self.target]},
            },
        )
        wait = self.storage.get_job_wait(wait_id)
        self.assertEqual(wait["status"], "dispatched")
        self.assertEqual(wait["resume_run_id"], run["id"])
        card = self._assert_card(wait_id, message, "dispatched")
        self.assertEqual(card["resume_run_id"], run["id"])
        with self.assertRaises(RuntimeError):
            self.storage.create_chat_run(
                self.conversation_id, "重复摘要", [], AGENT, self.snapshot, "craft",
                job_wait_dispatch={
                    "wait_id": wait_id, "dispatch_key": "d2", "source": {},
                },
            )

    def test_dispatch_refuses_after_new_human_message(self) -> None:
        record, _ = self._register()
        wait_id = record["id"]
        self.storage.update_job_wait(wait_id, status="ready", expected_statuses=("registered",))
        self.storage.add_message(self.conversation_id, "user", "新指令")
        with self.assertRaises(RuntimeError) as raised:
            self.storage.create_chat_run(
                self.conversation_id, "结果摘要", [], AGENT, self.snapshot, "craft",
                job_wait_dispatch={"wait_id": wait_id, "dispatch_key": "d1", "source": {}},
            )
        self.assertIn("新的人工输入", str(raised.exception))

    def test_dispatch_refuses_while_another_top_level_run_is_active(self) -> None:
        record, _ = self._register()
        self.storage.update_job_wait(record["id"], status="ready", expected_statuses=("registered",))
        _make_job(self.storage, self.conversation_id, status="running", kind="chat")
        with self.assertRaises(RuntimeError) as raised:
            self.storage.create_chat_run(
                self.conversation_id, "结果摘要", [], AGENT, self.snapshot, "craft",
                job_wait_dispatch={"wait_id": record["id"], "dispatch_key": "d1", "source": {}},
            )
        self.assertIn("ACTIVE_RUN", str(raised.exception))

    def test_restart_pauses_without_dispatch(self) -> None:
        for state in ("registered", "waiting", "ready", "dispatched"):
            with self.subTest(state=state):
                record, _ = self._register()
                message = self._attach_card(record["id"], state)
                if state == "dispatched":
                    self.storage.update_job_wait(record["id"], resume_run_id="resume_old")
                # 重新打开同一个库，复现进程重启后原消息仍在的实际路径。
                self.storage = ChatStorage(self.tmp / "chat.db")
                self.assertEqual(self.storage.pause_job_waits_after_restart(), 1)
                wait = self.storage.get_job_wait(record["id"])
                self.assertIn("服务重启", wait["reason"])
                card = self._assert_card(record["id"], message, "needs_attention")
                self.assertEqual(card["resume_run_id"], wait["resume_run_id"])
                self.assertEqual(self.storage.pause_job_waits_after_restart(), 0)

    def test_continue_resets_epoch_and_deadline(self) -> None:
        for state in ("needs_attention", "expired"):
            with self.subTest(state=state):
                record, _ = self._register()
                message = self._attach_card(record["id"], state)
                self.storage.update_job_wait(
                    record["id"], reason="已暂停", resume_run_id="resume_old", dispatch_key="old",
                )
                self.assertTrue(self.storage.prepare_job_wait_continue(
                    record["id"], human_epoch=7, deadline_at=123,
                ))
                wait = self.storage.get_job_wait(record["id"])
                self.assertEqual(wait["human_epoch"], 7)
                self.assertEqual(wait["deadline_at"], 123)
                card = self._assert_card(record["id"], message, "ready")
                self.assertEqual(card["reason"], "")
                self.assertEqual(card["resume_run_id"], "")
                self.assertFalse(self.storage.prepare_job_wait_continue(
                    record["id"], human_epoch=8, deadline_at=456,
                ))
                self._assert_card(record["id"], message, "ready")
                self.storage.update_job_wait(record["id"], status="cancelled")

    def test_continue_conflict_preserves_paused_card(self) -> None:
        record, _ = self._register()
        message = self._attach_card(record["id"], "expired")
        self.storage.update_job_wait(record["id"], reason="到期")
        newer, note = self._register(origin_run_id="new_run")
        self.assertIsNotNone(newer, note)
        self.assertFalse(self.storage.prepare_job_wait_continue(
            record["id"], human_epoch=7, deadline_at=123,
        ))
        self._assert_card(record["id"], message, "expired")

    def test_card_write_failure_rolls_back_wait_transition(self) -> None:
        record, _ = self._register()
        message = self._attach_card(record["id"])
        with mock.patch.object(
            self.storage, "_sync_job_wait_message", side_effect=RuntimeError("卡片写回失败"),
        ):
            with self.assertRaisesRegex(RuntimeError, "卡片写回失败"):
                self.storage.pause_job_waits_after_restart()
        self._assert_card(record["id"], message, "waiting")
        self.storage.pause_job_waits_after_restart()
        with mock.patch.object(
            self.storage, "_sync_job_wait_message", side_effect=RuntimeError("卡片写回失败"),
        ):
            with self.assertRaisesRegex(RuntimeError, "卡片写回失败"):
                self.storage.prepare_job_wait_continue(
                    record["id"], human_epoch=7, deadline_at=123,
                )
        self._assert_card(record["id"], message, "needs_attention")


class _CoordinatorJobs:
    def __init__(self, statuses: dict[str, str]) -> None:
        self.statuses = statuses

    def get(self, job_id, owner=None):
        status = self.statuses.get(job_id)
        if status is None:
            return None
        return {"id": job_id, "status": status, "kind": "shell", "result": {"text": "ok"}}

    def list(self, owner=None, active_only=False):
        return []


class _CoordinatorRuns:
    def __init__(self) -> None:
        self.dispatched: list[str] = []
        self.cancelled: list[str] = []

    def cancel(self, run_id: str):
        self.cancelled.append(run_id)

    def dispatch_job_wait(self, wait_id: str):
        self.dispatched.append(wait_id)
        return {"id": "resume"}


class _AppStub:
    def __init__(self, storage, jobs, runs) -> None:
        self.storage = storage
        self.jobs = jobs
        self.runs = runs
        self.job_media_writer = None


class CoordinatorStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage, self.tmp = _storage()
        self.conversation_id = _conversation(self.storage)
        self.storage.add_message(self.conversation_id, "user", "生成")
        self.origin = _make_job(self.storage, self.conversation_id, status="running", kind="chat")
        self.target = _make_job(self.storage, self.conversation_id, status="running", parent=self.origin)
        record, _ = self.storage.create_job_wait(
            conversation_id=self.conversation_id, origin_run_id=self.origin,
            target_job_ids=[self.target], snapshot={"model_key": "online:p1"},
        )
        self.wait_id = record["id"]
        self.jobs = _CoordinatorJobs({self.target: "running"})
        self.runs = _CoordinatorRuns()
        self.app = _AppStub(self.storage, self.jobs, self.runs)
        self.coordinator = JobWaitCoordinator(self.app)

    def test_origin_completion_moves_to_waiting_and_marks_message(self) -> None:
        message = self.storage.add_message(self.conversation_id, "assistant", "已登记")
        self.coordinator.on_run_finished(self.origin, message["id"], "completed")
        wait = self.storage.get_job_wait(self.wait_id)
        self.assertEqual(wait["status"], "waiting")
        self.assertEqual(wait["origin_message_id"], message["id"])
        conversation = self.storage.get_conversation(self.conversation_id)
        stored = next(item for item in conversation["messages"] if item["id"] == message["id"])
        self.assertEqual(stored["metadata"][MetadataKeys.JOB_WAIT]["state"], "waiting")

    def test_all_targets_terminal_dispatches_exactly_once(self) -> None:
        message = self.storage.add_message(self.conversation_id, "assistant", "已登记")
        self.coordinator.on_run_finished(self.origin, message["id"], "completed")
        self.jobs.statuses[self.target] = "completed"
        self.coordinator._reconcile()
        self.assertEqual(self.runs.dispatched, [self.wait_id])
        self.assertEqual(self.storage.get_job_wait(self.wait_id)["status"], "ready")

    def test_resume_completion_marks_wait_completed(self) -> None:
        message = self.storage.add_message(self.conversation_id, "assistant", "已登记")
        self.coordinator.on_run_finished(self.origin, message["id"], "completed")
        result = self.storage.add_message(self.conversation_id, "assistant", "结果")
        self.storage.update_job_wait(self.wait_id, status="ready", expected_statuses=("waiting",))
        self.storage.update_job_wait(
            self.wait_id, status="dispatched", resume_run_id="resume_1",
            expected_statuses=("ready",),
        )
        self.coordinator.on_run_finished("resume_1", result["id"], "completed")
        wait = self.storage.get_job_wait(self.wait_id)
        self.assertEqual(wait["status"], "completed")
        self.assertEqual(wait["result_message_id"], result["id"])

    def test_human_input_cancels_waiting(self) -> None:
        message = self.storage.add_message(self.conversation_id, "assistant", "已登记")
        self.coordinator.on_run_finished(self.origin, message["id"], "completed")
        self.coordinator.human_input(self.conversation_id)
        self.assertEqual(self.storage.get_job_wait(self.wait_id)["status"], "cancelled")

    def test_cancel_wait_marks_cancelled(self) -> None:
        message = self.storage.add_message(self.conversation_id, "assistant", "已登记")
        self.coordinator.on_run_finished(self.origin, message["id"], "completed")
        record = self.coordinator.cancel_wait(self.wait_id)
        self.assertEqual(record["status"], "cancelled")
        conversation = self.storage.get_conversation(self.conversation_id)
        stored = next(item for item in conversation["messages"] if item["id"] == message["id"])
        self.assertEqual(stored["metadata"][MetadataKeys.JOB_WAIT]["state"], "cancelled")

    def test_cancel_stops_run_dispatched_after_initial_read(self) -> None:
        message = self.storage.add_message(self.conversation_id, "assistant", "已登记")
        self.coordinator.on_run_finished(self.origin, message["id"], "completed")
        self.storage.update_job(self.origin, status="completed", finished=True)
        self.storage.update_job_wait(self.wait_id, status="ready")
        original_update = self.storage.update_job_wait
        resumed = []

        def dispatch_then_cancel(wait_id, **kwargs):
            # cancel_wait 已读到 ready；在条件更新前让派发事务抢先提交。
            if kwargs.get("status") == "cancelled":
                run = self.storage.create_chat_run(
                    self.conversation_id, "结果摘要", [], AGENT, {}, "craft",
                    job_wait_dispatch={"wait_id": wait_id, "dispatch_key": "race", "source": {}},
                )
                resumed.append(run["id"])
            return original_update(wait_id, **kwargs)

        with mock.patch.object(self.storage, "update_job_wait", side_effect=dispatch_then_cancel):
            wait = self.coordinator.cancel_wait(self.wait_id)
        self.assertEqual(wait["status"], "cancelled")
        self.assertEqual(len(resumed), 1)
        self.assertEqual(self.runs.cancelled, resumed, "必须停止刚提交的结果回复 Run")
        stored = self.storage.get_conversation(self.conversation_id)["messages"]
        card = next(item for item in stored if item["id"] == message["id"])["metadata"][MetadataKeys.JOB_WAIT]
        self.assertEqual(card["state"], "cancelled")
        self.assertEqual(card["resume_run_id"], resumed[0])

    def test_late_cancel_and_finish_do_not_overwrite_completed_card(self) -> None:
        message = self.storage.add_message(self.conversation_id, "assistant", "已登记")
        self.coordinator.on_run_finished(self.origin, message["id"], "completed")
        self.storage.update_job_wait(self.wait_id, status="dispatched", resume_run_id="resume_1")
        stale = self.storage.get_job_wait(self.wait_id)
        self.coordinator.on_run_finished("resume_1", "result_1", "completed")
        self.coordinator._cancel(stale, "迟到取消")
        self.coordinator._finish(stale, "needs_attention", "迟到失败")
        self.assertEqual(self.runs.cancelled, [])
        self.assertEqual(self.storage.get_job_wait(self.wait_id)["status"], "completed")
        stored = self.storage.get_conversation(self.conversation_id)["messages"]
        card = next(item for item in stored if item["id"] == message["id"])["metadata"][MetadataKeys.JOB_WAIT]
        self.assertEqual(card["state"], "completed")
        self.assertEqual(card["result_message_id"], "result_1")

    def test_continue_keeps_card_current_while_target_still_running(self) -> None:
        message = self.storage.add_message(self.conversation_id, "assistant", "已登记")
        self.coordinator.on_run_finished(self.origin, message["id"], "completed")
        self.storage.pause_job_waits_after_restart()
        with mock.patch.object(self.coordinator, "start"):
            wait, note = self.coordinator.continue_wait(self.wait_id)
        self.assertEqual(note, "")
        self.assertEqual(wait["status"], "ready")
        self.assertEqual(self.runs.dispatched, [])
        stored = self.storage.get_conversation(self.conversation_id)["messages"]
        card = next(item for item in stored if item["id"] == message["id"])["metadata"][MetadataKeys.JOB_WAIT]
        self.assertEqual(card["state"], "ready")
        self.assertEqual(card["reason"], "")

    def test_missing_target_does_not_block_forever(self) -> None:
        message = self.storage.add_message(self.conversation_id, "assistant", "已登记")
        self.coordinator.on_run_finished(self.origin, message["id"], "completed")
        self.jobs.statuses.pop(self.target)
        self.coordinator._reconcile()
        self.assertEqual(self.runs.dispatched, [self.wait_id], "被清理的目标记为无法核实，不得永久等待")

    def test_expired_wait_is_not_dispatched(self) -> None:
        message = self.storage.add_message(self.conversation_id, "assistant", "已登记")
        self.coordinator.on_run_finished(self.origin, message["id"], "completed")
        with self.storage._connect() as db:
            db.execute("UPDATE job_waits SET deadline_at=1 WHERE id=?", (self.wait_id,))
        self.jobs.statuses[self.target] = "completed"
        self.coordinator._reconcile()
        self.assertEqual(self.runs.dispatched, [])
        self.assertEqual(self.storage.get_job_wait(self.wait_id)["status"], "expired")


class RegistryWaitCancelTests(unittest.TestCase):
    def test_cancel_event_interrupts_wait_promptly(self) -> None:
        storage, _tmp = _storage()
        conversation_id = _conversation(storage)
        job_id = _make_job(storage, conversation_id, status="running")

        app = type("A", (), {"storage": storage})()
        registry = JobRegistry(app)
        cancel = threading.Event()
        caught: list[BaseException] = []

        def waiter() -> None:
            try:
                registry.wait(job_id, 30, cancel_event=cancel)
            except BaseException as exc:  # noqa: BLE001 - 断言异常类型
                caught.append(exc)

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        time.sleep(0.2)
        cancel.set()
        thread.join(2.0)
        self.assertFalse(thread.is_alive(), "停止后两秒内必须退出等待")
        self.assertEqual(len(caught), 1)
        self.assertEqual(type(caught[0]).__name__, "TaskCancelled")

    def test_wait_timeout_returns_running_snapshot(self) -> None:
        storage, _tmp = _storage()
        conversation_id = _conversation(storage)
        job_id = _make_job(storage, conversation_id, status="running")
        app = type("A", (), {"storage": storage})()
        registry = JobRegistry(app)
        snapshot = registry.wait(job_id, 0.1)
        self.assertEqual(snapshot["status"], "running")


class ResumeMessageTests(unittest.TestCase):
    def test_summary_does_not_require_fixed_fields(self) -> None:
        text, truncated = summarize_job_result({
            "id": "j1", "kind": "comfyui", "status": "completed", "progress": 100,
            "result": {"completed_shots": [{"files": ["a.png"]}], "errors": ["第2段失败"]},
        })
        self.assertIn("completed_shots", text)
        self.assertIn("partial_errors", text, "completed 带部分失败必须报告数量")
        self.assertFalse(truncated)

    def test_long_result_states_total_and_shown_volume(self) -> None:
        text, truncated = summarize_job_result({
            "id": "j1", "kind": "shell", "status": "completed", "progress": 100,
            "result": {"stdout": "x" * 20000},
        })
        self.assertTrue(truncated)
        self.assertIn("结果共", text)
        self.assertIn("job_output", text)

    def test_resume_message_counts_each_status(self) -> None:
        wait = {"id": "w1", "target_job_ids": ["j1", "j2", "j3"]}
        jobs = [
            {"id": "j1", "kind": "shell", "status": "completed", "progress": 100, "result": {"ok": 1}},
            {"id": "j2", "kind": "shell", "status": "failed", "progress": 0, "error": "boom"},
            {"id": "j3", "kind": "shell", "status": "cancelled", "progress": 0},
        ]
        text = build_resume_message(wait, jobs, ["j9"])
        self.assertIn("完成 1", text)
        self.assertIn("失败 1", text)
        self.assertIn("取消 1", text)
        self.assertIn("无法核实", text)
        self.assertIn("不要重新执行", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
