"""Live tool-confirmation projection: real executor, isolated SQLite, no network."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from naiba.app import NaibaChatApp
from naiba.core.contracts import SUPER_AGENT_EXECUTION_KEY
from naiba.core.exceptions import TaskCancelled
from naiba.http import RequestHandler
from naiba.run.manager import ConversationRunManager
from naiba.run.stream import _RunEventSink
from naiba.skills.agent import SkillAgent
from naiba.storage.store import ChatStorage
from naiba.super_agent.domain import DomainCoordinator
from naiba.super_agent.service import SuperAgentService
from naiba.tools.executor import ToolExecutor
from tests.test_run_events import _FakeRuns, _FakeStreamHandler
from tests.tool_testkit import assembled_registry, wired_executor


class LiveConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.storage = ChatStorage(self.root / "chat.db")
        self.conversation = self.storage.create_conversation()
        self.cid = self.conversation["id"]
        self.run = self.storage.create_chat_run(
            self.cid, "Test request", [], {"id": "general", "name": "Test"}, {}, "craft",
        )
        self.rid = self.run["id"]
        self.storage.update_background_task(self.rid, status="waiting")
        self.executor = ToolExecutor(self.root, sys.executable, 5, SimpleNamespace())
        self.app = SimpleNamespace(storage=self.storage, executor=self.executor)
        self.manager = ConversationRunManager(self.app)
        self.app.runs = self.manager
        self.manager._executors[self.rid] = self.executor
        self.manager._events[self.rid] = threading.Event()

    def request(self, tool="list_directory", **context):
        _, result = self.executor.request_confirmation(
            tool, {"path": str(self.root / "private"), "api_key": "secret-value"}, [],
            "private reason " + str(self.root),
            {"run_id": self.rid, "conversation_id": self.cid, **context},
        )
        return result.split(":", 3)[1]

    def test_multiple_pending_are_safe_and_owned_independently(self):
        first, second = self.request(), self.request("read_file")
        snapshot = self.manager.confirmation_snapshot(self.rid)
        self.assertEqual({item["confirm_id"] for item in snapshot}, {first, second})
        for item in snapshot:
            self.assertEqual(set(item), {"confirm_id", "run_id", "conversation_id", "tool", "reason", "status"})
            self.assertEqual(item["status"], "pending")
            self.assertTrue(self.manager.owns_confirmation(self.rid, item["confirm_id"]))
        encoded = json.dumps(snapshot)
        self.assertNotIn("secret-value", encoded)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("arguments", encoded)
        self.assertNotIn("permission", encoded)
        self.assertEqual(self.manager.confirmation_snapshot("other-run"), [])

    def test_async_allow_is_processing_and_executes_once(self):
        confirm_id = self.request()
        entered, release = threading.Event(), threading.Event()
        calls = []

        def execute(*args):
            calls.append(args)
            entered.set()
            self.assertTrue(release.wait(5))
            return True, "test output"

        with patch.object(self.executor, "execute_unchecked", side_effect=execute):
            try:
                self.assertTrue(self.manager.confirm_tool_async(self.rid, confirm_id)[0])
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.manager.confirmation_snapshot(self.rid)[0]["status"], "processing")
                self.assertTrue(self.manager.confirm_tool_async(self.rid, confirm_id)[0])
                self.assertFalse(self.manager.reject_tool(self.rid, confirm_id)[0])
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
                self.assertEqual(self.executor.wait_for_confirmation(confirm_id, timeout=3), (True, "test output"))
        self.assertEqual(self.manager.confirmation_snapshot(self.rid), [])

    def test_reject_timeout_and_cancel_remove_actionable_projection(self):
        rejected = self.request()
        with patch.object(self.executor, "execute_unchecked") as execute:
            self.assertFalse(self.manager.reject_tool(self.rid, rejected)[0])
            self.assertFalse(self.executor.wait_for_confirmation(rejected, timeout=1)[0])
            timed_out = self.request()
            self.assertFalse(self.executor.wait_for_confirmation(timed_out, timeout=0)[0])
            cancelled = self.request()
            signal = self.manager._events[self.rid]
            signal.set()
            self.assertEqual(self.manager.confirmation_snapshot(self.rid), [])
            self.assertIsNone(self.manager.confirm_tool_async(self.rid, cancelled))
            with self.assertRaises(TaskCancelled):
                self.executor.wait_for_confirmation(cancelled, cancel_event=signal)
            execute.assert_not_called()
        self.assertFalse(self.executor.pending_confirmation)

    def test_no_projection_or_authority_after_stopping_terminal_or_restart(self):
        confirm_id = self.request()
        for status in ("stopping", "cancelling", "completed", "failed", "cancelled", "interrupted"):
            with self.subTest(status=status):
                self.storage.update_background_task(self.rid, status=status)
                self.assertEqual(self.manager.confirmation_snapshot(self.rid), [])
                self.assertFalse(self.manager.owns_confirmation(self.rid, confirm_id))
        self.storage.update_background_task(self.rid, status="waiting")
        restarted = ConversationRunManager(self.app)
        self.assertEqual(restarted.confirmation_snapshot(self.rid), [])
        self.assertIsNone(restarted.confirm_tool_async(self.rid, confirm_id))

    def test_context_mismatch_and_invalid_tool_names_do_not_escape(self):
        self.request(run_id="other")
        self.request(conversation_id="other")
        self.request("private/path?token=secret")
        self.assertEqual(self.manager.confirmation_snapshot(self.rid), [])

    def test_snapshot_holds_executor_confirmation_lock(self):
        self.request()
        original = self.executor.pending_confirmation
        lock = self.executor._confirmation_lock

        class CheckedPending(dict):
            def items(self):
                if not lock._is_owned():
                    raise AssertionError("confirmation read outside executor lock")
                return super().items()

        self.executor.pending_confirmation = CheckedPending(original)
        self.assertEqual(len(self.manager.confirmation_snapshot(self.rid)), 1)

    def test_waiting_for_executor_lock_does_not_hold_manager_lock(self):
        self.request()
        original = self.executor._confirmation_lock
        waiting = threading.Event()
        results = []

        class ObservedLock:
            def __enter__(self):
                waiting.set()
                original.acquire()

            def __exit__(self, *_):
                original.release()

        self.executor._confirmation_lock = ObservedLock()
        reader = threading.Thread(target=lambda: results.extend(self.manager.confirmation_snapshot(self.rid)))
        original.acquire()
        try:
            reader.start()
            self.assertTrue(waiting.wait(2))
            # A policy recheck holds this executor lock and may re-enter manager.
            acquired = self.manager._lock.acquire(timeout=1)
            if acquired:
                self.manager._lock.release()
            self.assertTrue(acquired, "snapshot inverted manager/executor lock order")
        finally:
            original.release()
            reader.join(3)
            self.assertFalse(reader.is_alive())
        self.assertEqual(len(results), 1)

    def bind_thread(self):
        self.app.super_agent = SuperAgentService(self.storage)
        self.app.super_agent_domain = DomainCoordinator(self.app)
        self.app._super_agent_owner = lambda: "local"
        service = self.app.super_agent
        thread = service.create_thread("local", "Confirm test", status="active")
        node = service.create_node("local", thread.thread_id, "chat", idempotency_key="node",
            status="running", executor_type="chat_run", executor_id=self.rid,
            checkpoint={"conversation_id": self.cid})
        _, claim, _ = service.store.claim_command("local", thread.thread_id, node.node_id, "start", "request")
        self.storage.update_run_snapshot(self.rid, {SUPER_AGENT_EXECUTION_KEY: claim["correlation"]})
        service.store.update_thread(thread.thread_id, owner_id="local", current_node_id=node.node_id)
        return thread, node

    def test_thread_detail_projects_only_current_attempt_and_owner(self):
        confirm_id = self.request()
        thread, node = self.bind_thread()
        payload, code = NaibaChatApp.api_super_agent_thread(self.app, thread.thread_id)
        self.assertEqual(code, 200)
        self.assertEqual(payload["thread"]["chat_confirmations"][0]["confirm_id"], confirm_id)
        self.assertEqual(payload["thread"]["chat_confirmations"][0]["node_id"], node.node_id)
        with self.assertRaises(LookupError):
            self.app.super_agent_domain.chat_confirmations("other", thread.thread_id)
        self.app.super_agent.store.update_node(node.node_id, owner_id="local", attempt=1)
        self.assertEqual(self.app.super_agent_domain.chat_confirmations("local", thread.thread_id), [])

    def test_thread_projection_rejects_wrong_correlation_and_cancellation(self):
        self.request()
        thread, node = self.bind_thread()
        self.storage.update_run_snapshot(self.rid, {SUPER_AGENT_EXECUTION_KEY: "old-attempt"})
        self.assertEqual(self.app.super_agent_domain.chat_confirmations("local", thread.thread_id), [])
        self.app.super_agent.store.update_node(node.node_id, owner_id="local", cancel_requested=True)
        self.assertEqual(self.app.super_agent_domain.chat_confirmations("local", thread.thread_id), [])

    def test_thread_projection_does_not_reconstruct_from_historical_events(self):
        confirm_id = self.request()
        thread, _ = self.bind_thread()
        self.manager.emit(self.rid, {"type": "tool_confirm", "confirm_id": confirm_id,
                                   "tool_name": "list_directory", "arguments": {"path": "private"}})
        self.manager._executors.clear()
        self.assertEqual(self.app.super_agent_domain.chat_confirmations("local", thread.thread_id), [])

    def run_model_confirmation_loop(self, approve):
        """Local model response -> policy -> UI decision -> tool result -> model resume."""
        workspace = self.root / "workspace"
        workspace.mkdir()
        self.executor = wired_executor(workspace, mode="confirm")
        registry = assembled_registry(workspace)
        registry.bind_executor(self.executor)
        self.manager._executors[self.rid] = self.executor
        thread, node = self.bind_thread()
        adapter = self.app.super_agent_domain.registry.get("chat_run")
        reply = json.dumps({"type": "tool", "tool": "list_directory",
                            "arguments": {"path": str(self.root)}})
        answers = iter([reply, "本地测试完成。"])
        confirmation = threading.Event()
        finished = threading.Event()
        cancel = self.manager._events[self.rid]
        sink = _RunEventSink(self.manager, self.rid, cancel)
        outcomes = {}

        def emit(payload):
            sink(payload)
            if payload.get("type") == "tool_confirm":
                confirmation.set()

        def run_agent():
            try:
                agent = SkillAgent(SimpleNamespace(scan=lambda: []), self.executor,
                    lambda *_: next(answers, "本地测试完成。"), None)
                outcomes["result"] = agent.run(
                    "列出指定目录", [], {"kind": "local", "model": "fake", "context_window": 32768},
                    {"stream": False, "max_tokens": 512, "max_steps": 4},
                    {"mode": "auto", "skill_ids": []}, [], "", ["list_directory"], emit, cancel,
                    tool_registry=registry, run_context={"run_id": self.rid, "conversation_id": self.cid,
                        "executor": self.executor, "workspace_dir": str(workspace), "confirmation_ui": True},
                )
            except Exception as exc:
                outcomes["error"] = exc
            finally:
                sink.close()
                finished.set()

        entered, release = threading.Event(), threading.Event()
        original = self.executor.execute_unchecked

        def real_tool(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release tool")
            return original(*args)

        worker = threading.Thread(target=run_agent, daemon=True)
        with patch.object(self.executor, "execute_unchecked", side_effect=real_tool) as execution:
            worker.start()
            try:
                self.assertTrue(confirmation.wait(3), outcomes)
                adapter.sync("local", thread.thread_id, node.node_id)
                self.assertEqual(self.app.super_agent.get_node("local", node.node_id).status, "waiting_user")
                item = self.app.super_agent_domain.chat_confirmations("local", thread.thread_id)[0]
                self.assertEqual(item["status"], "pending")
                execution.assert_not_called()
                if approve:
                    self.manager.confirm_tool_async(self.rid, item["confirm_id"])
                    self.assertTrue(entered.wait(2))
                    adapter.sync("local", thread.thread_id, node.node_id)
                    self.assertEqual(self.app.super_agent.get_node("local", node.node_id).status, "running")
                    self.assertEqual(self.app.super_agent_domain.chat_confirmations("local", thread.thread_id)[0]["status"], "processing")
                    release.set()
                else:
                    self.manager.reject_tool(self.rid, item["confirm_id"])
                self.assertTrue(finished.wait(3), "Agent did not resume after decision")
                self.assertNotIn("error", outcomes)
                self.assertEqual(execution.call_count, 1 if approve else 0)
                self.assertEqual(outcomes["result"][1][0]["success"], approve)
                adapter.sync("local", thread.thread_id, node.node_id)
                self.assertEqual(self.app.super_agent.get_thread("local", thread.thread_id).status, "active")
                self.assertEqual(self.app.super_agent_domain.chat_confirmations("local", thread.thread_id), [])
            finally:
                release.set()
                cancel.set()
                worker.join(3)
                self.assertFalse(worker.is_alive())

    def test_local_model_and_real_policy_allow_processing_resume(self):
        self.run_model_confirmation_loop(True)

    def test_local_model_and_real_policy_reject_resume(self):
        self.run_model_confirmation_loop(False)


class ConfirmationReplayTests(unittest.TestCase):
    def test_replay_marks_pending_processing_and_resolved_without_changing_history(self):
        for status in ("pending", "processing", "resolved"):
            with self.subTest(status=status):
                events = [{"type": "tool_confirm", "confirm_id": "confirm", "sequence": 1},
                          {"type": "done", "sequence": 2}]
                runs = _FakeRuns(events=events)
                runs.confirmation_snapshot = lambda _: [] if status == "resolved" else [
                    {"confirm_id": "confirm", "status": status},
                ]
                handler = _FakeStreamHandler(runs)
                RequestHandler._stream_run(handler, "run")
                event = handler.wfile.events[0]
                self.assertEqual(event["confirm_processing"], status == "processing")
                self.assertEqual(event["confirm_resolved"], status == "resolved")
                self.assertNotIn("confirm_processing", events[0])


if __name__ == "__main__":
    unittest.main()
