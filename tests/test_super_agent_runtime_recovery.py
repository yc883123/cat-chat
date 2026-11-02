"""Isolated regression cases for V1 launch/linkage and live Chat waiting."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from naiba.core.contracts import SUPER_AGENT_CLAIMS_KEY, SUPER_AGENT_EXECUTION_KEY
from naiba.storage.store import ChatStorage
from naiba.super_agent.adapters import AdapterError
from naiba.super_agent.router import AdapterRegistry
from naiba.super_agent.runtime import ExecutionCoordinator, current_execution_claims
from naiba.super_agent.service import SuperAgentService


class RuntimeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        # These fixtures exercise linkage, not configuration resolution. Real
        # configuration/runner coverage lives in test_super_agent_subagent_chain.
        validator = patch("naiba.super_agent.runtime.validate_execution_baseline", return_value="")
        validator.start()
        self.addCleanup(validator.stop)
        self.service = SuperAgentService(self.storage)
        self.cid = self.storage.create_conversation(permission_mode="confirm")["id"]
        self.submits = []
        self.job_starts = []
        self.confirmations = []
        self.app = SimpleNamespace(storage=self.storage, runs=SimpleNamespace(
            submit_chat=self.submit_chat, confirmation_snapshot=lambda _: list(self.confirmations),
            cancel=lambda rid: self.storage.update_background_task(rid, status="stopping")),
            jobs=SimpleNamespace(start=self.start_job, cancel=lambda rid, **_: self.storage.update_background_task(rid, status="stopping")))
        self.domain = SimpleNamespace(app=self.app, service=self.service, registry=AdapterRegistry())
        self.runtime = ExecutionCoordinator(self.domain)
        self.addCleanup(self.runtime.shutdown)
        self.addCleanup(self.service.shutdown)

    def submit_chat(self, body, *, execution_key):
        self.submits.append(execution_key)
        return self.record("chat", execution_key)

    def start_job(self, spec, *, owner):
        self.job_starts.append(spec)
        return self.record(spec.kind, spec.params[SUPER_AGENT_EXECUTION_KEY], params=True)["id"]

    def record(self, kind, correlation="", *, params=False):
        snapshot = {SUPER_AGENT_EXECUTION_KEY: correlation}
        if params:
            snapshot = {"params": snapshot}
        row = self.storage.create_run(self.cid, "offline fixture", {}, snapshot, kind=kind,
            owner_session_id=self.cid, detached=True)
        self.storage.update_background_task(row["id"], status="running")
        return self.storage.get_background_task(row["id"])

    def node(self, kind="chat_run", **fields):
        thread = self.service.create_thread("local", "runtime fixture", status="active")
        checkpoint = {"conversation_id": self.cid, "start_request": {"message": "offline"}}
        if kind == "subagent":
            checkpoint["configuration_fingerprint"] = {"fixture": True}
        checkpoint.update(fields.pop("checkpoint", {}))
        node = self.service.create_node("local", thread.thread_id, kind, idempotency_key="fixture",
            status="ready", executor_type=kind, checkpoint=checkpoint, **fields)
        return thread, node

    def command(self, thread, node, action="start", request="fixture", **kwargs):
        return self.runtime.command("local", thread.thread_id, node.node_id, action, request, **kwargs)

    def current(self, node):
        return self.service.get_node("local", node.node_id)

    def test_new_container_defaults_are_typed(self):
        thread = self.service.create_thread("local", "defaults")
        for name in ("linked_conversation_ids", "linked_run_ids", "linked_job_ids", "linked_domain_task_ids"):
            self.assertEqual(getattr(thread, name), [])
        node = self.service.create_node("local", thread.thread_id, "fixture", idempotency_key="default")
        for name in ("dependencies", "input_refs", "output_refs"):
            self.assertEqual(getattr(node, name), [])
        for name in ("checkpoint", "retry_policy", "promotion_policy", "execution_snapshot"):
            self.assertEqual(getattr(node, name), {})

    def test_legacy_null_and_wrong_container_reads_do_not_rewrite_database(self):
        thread, node = self.node()
        with self.storage._connect() as db:
            db.execute("UPDATE super_threads SET linked_run_ids='null',linked_job_ids='{}' WHERE thread_id=?", (thread.thread_id,))
            db.execute("UPDATE super_nodes SET checkpoint='null',output_refs='{}',dependencies='null' WHERE node_id=?", (node.node_id,))
        self.assertEqual(self.service.get_thread("local", thread.thread_id).linked_run_ids, [])
        self.assertEqual(self.service.get_thread("local", thread.thread_id).linked_job_ids, [])
        self.assertEqual(self.current(node).checkpoint, {})
        self.assertEqual(self.current(node).output_refs, [])
        self.assertEqual(self.current(node).dependencies, [])
        with self.storage._connect() as db:
            self.assertEqual(db.execute("SELECT linked_run_ids FROM super_threads WHERE thread_id=?", (thread.thread_id,)).fetchone()[0], "null")

    def test_container_updates_use_distinct_defaults(self):
        thread, node = self.node()
        result = self.service.store.update_node(node.node_id, owner_id="local", checkpoint=None, output_refs=None)
        self.assertEqual(result.checkpoint, {})
        self.assertEqual(result.output_refs, [])
        with self.storage._connect() as db:
            row = db.execute("SELECT checkpoint,output_refs FROM super_nodes WHERE node_id=?", (node.node_id,)).fetchone()
            self.assertEqual(tuple(row), ("{}", "[]"))

    def test_chat_start_replay_links_and_submits_once(self):
        thread, node = self.node()
        result = self.command(thread, node)
        again = self.command(thread, node, request="another-button-click")
        self.assertEqual(result.executor_id, again.executor_id)
        self.assertEqual(len(self.submits), 1)
        self.assertEqual(self.service.get_thread("local", thread.thread_id).linked_run_ids, [result.executor_id])
        self.assertEqual(self.current(node).checkpoint[SUPER_AGENT_CLAIMS_KEY]["start:0"]["state"], "done")
        self.assertEqual(len([e for e in self.service.events("local", thread.thread_id) if e.event_type == "run_started"]), 1)

    def test_subagent_and_existing_job_binding_are_idempotent(self):
        for kind in ("subagent", "job"):
            with self.subTest(kind=kind):
                checkpoint = {"job_id": self.record("check")["id"]} if kind == "job" else {}
                thread, node = self.node(kind, checkpoint=checkpoint)
                result = self.command(thread, node)
                self.command(thread, node)
                self.assertEqual(self.service.get_thread("local", thread.thread_id).linked_job_ids, [result.executor_id])
                self.assertEqual(self.current(node).status, "running")
        self.assertEqual(len(self.job_starts), 1)
        self.assertEqual(len(self.submits), 0)

    def test_real_legacy_null_links_bind_all_runtime_kinds(self):
        for kind in ("chat_run", "subagent", "job"):
            with self.subTest(kind=kind):
                checkpoint = {"job_id": self.record("check")["id"]} if kind == "job" else {}
                thread, node = self.node(kind, checkpoint=checkpoint)
                with self.storage._connect() as db:
                    db.execute("UPDATE super_threads SET linked_run_ids='null',linked_job_ids='null' WHERE thread_id=?", (thread.thread_id,))
                result = self.command(thread, node)
                self.command(thread, node)
                linked = self.service.get_thread("local", thread.thread_id)
                self.assertIn(result.executor_id, linked.linked_run_ids if kind == "chat_run" else linked.linked_job_ids)
        self.assertEqual(len(self.submits), 1)
        self.assertEqual(len(self.job_starts), 1)

    def test_stale_adapter_callback_cannot_bind_any_new_attempt(self):
        for kind in ("chat_run", "subagent", "job"):
            with self.subTest(kind=kind):
                checkpoint = {"job_id": self.record("check")["id"]} if kind == "job" else {}
                thread, node = self.node(kind, checkpoint=checkpoint)
                result = self.command(thread, node)
                old = self.current(node)
                self.service.store.update_node(node.node_id, owner_id="local", status="interrupted", expected_attempt=0)
                self.service.retry_node("local", node.node_id)
                with self.assertRaisesRegex(AdapterError, "attempt"):
                    self.domain.registry.get(kind)._bind(old, result.executor_id)
                self.assertEqual(self.current(node).attempt, 1)

    def test_linkage_failure_recovers_same_executor_and_settles_claim(self):
        for recovery in ("command", "tick"):
            with self.subTest(recovery=recovery):
                thread, node = self.node()
                original = self.service.store.update_thread
                def fail_link(tid, **kwargs):
                    if "linked_run_ids" in kwargs:
                        raise TypeError("'NoneType' object is not iterable")
                    return original(tid, **kwargs)
                before = len(self.submits)
                with patch.object(self.service.store, "update_thread", side_effect=fail_link):
                    with self.assertRaises(TypeError):
                        self.command(thread, node)
                interrupted = self.current(node)
                self.assertTrue(interrupted.executor_id)
                self.assertEqual(interrupted.status, "interrupted")
                self.assertEqual(self.service.get_thread("local", thread.thread_id).status, "needs_recovery")
                if recovery == "command":
                    self.command(thread, node)
                else:
                    self.runtime._tick_thread(self.service.get_thread("local", thread.thread_id))
                current = self.current(node)
                self.assertEqual(current.executor_id, interrupted.executor_id)
                self.assertEqual(current.status, "running")
                self.assertEqual(current.error, "")
                self.assertEqual(current.checkpoint[SUPER_AGENT_CLAIMS_KEY]["start:0"]["state"], "done")
                self.assertEqual(self.service.get_thread("local", thread.thread_id).linked_run_ids, [current.executor_id])
                self.assertEqual(self.service.get_thread("local", thread.thread_id).status, "active")
                self.assertEqual(len(self.submits), before + 1)

    def test_submit_succeeded_before_executor_id_persisted_is_reconciled(self):
        thread, node = self.node()
        adapter = self.domain.registry.get("chat_run")
        with patch.object(adapter, "_bind", side_effect=RuntimeError("crash before binding")):
            with self.assertRaises(RuntimeError):
                self.command(thread, node)
        self.assertEqual(self.current(node).executor_id, "")
        self.runtime._tick_thread(self.service.get_thread("local", thread.thread_id))
        self.assertTrue(self.current(node).executor_id)
        self.assertEqual(self.current(node).status, "running")
        self.assertEqual(len(self.submits), 1)

    def test_retry_button_recovers_missing_handle_before_rejecting_live_retry(self):
        thread, node = self.node()
        adapter = self.domain.registry.get("chat_run")
        with patch.object(adapter, "_bind", side_effect=RuntimeError("crash before binding")):
            with self.assertRaises(RuntimeError):
                self.command(thread, node)
        with self.assertRaisesRegex(AdapterError, "只有失败"):
            self.command(thread, node, action="retry", request="retry-click")
        self.assertEqual(len(self.submits), 1)
        self.assertTrue(self.current(node).executor_id)
        self.assertEqual(self.current(node).status, "running")
        self.assertEqual(self.current(node).error, "")

    def test_unknown_launch_cannot_be_reported_cancelled_or_retried(self):
        thread, node = self.node()
        with patch.object(self.app.runs, "submit_chat", side_effect=RuntimeError("unknown")):
            with self.assertRaises(RuntimeError):
                self.command(thread, node)
        for action in ("retry", "cancel"):
            with self.assertRaisesRegex(AdapterError, "原执行尚未对账"):
                self.command(thread, node, action=action, request=action)
        self.runtime.control("local", thread.thread_id, "cancel", "cancel-thread")
        self.assertEqual(self.current(node).status, "interrupted")
        self.assertEqual(self.service.get_thread("local", thread.thread_id).status, "needs_recovery")
        self.assertEqual(len(self.submits), 0)

    def test_start_reconciles_retry_claim_even_before_start_claim_exists(self):
        thread, node = self.node()
        first = self.command(thread, node)
        self.storage.update_background_task(first.executor_id, status="failed")
        self.command(thread, node, action="sync", request="")
        adapter = self.domain.registry.get("chat_run")
        with patch.object(adapter, "_bind", side_effect=RuntimeError("retry binding lost")):
            with self.assertRaises(RuntimeError):
                self.command(thread, node, action="retry", request="retry-1")
        self.assertEqual(self.current(node).attempt, 1)
        self.assertEqual(self.current(node).executor_id, "")
        result = self.command(thread, node, action="start", request="start-after-retry")
        self.assertTrue(result.executor_id)
        self.assertEqual(len(self.submits), 2)
        self.assertNotIn("start:1", self.current(node).checkpoint[SUPER_AGENT_CLAIMS_KEY])

    def test_unknown_launch_result_is_never_automatically_replayed(self):
        thread, node = self.node()
        with patch.object(self.app.runs, "submit_chat", side_effect=RuntimeError("unknown outcome")) as submit:
            with self.assertRaises(RuntimeError):
                self.command(thread, node)
            self.runtime._tick_thread(self.service.get_thread("local", thread.thread_id))
            with self.assertRaisesRegex(AdapterError, "不会自动重放"):
                self.command(thread, node, request="second-click")
            self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.current(node).status, "interrupted")
        self.assertEqual(self.current(node).executor_id, "")

    def test_old_attempt_cannot_rebind_or_pollute_new_execution(self):
        thread, node = self.node()
        first = self.command(thread, node)
        self.storage.update_background_task(first.executor_id, status="failed")
        self.command(thread, node, action="sync", request="")
        second = self.command(thread, node, action="retry", request="retry-1")
        self.assertNotEqual(first.executor_id, second.executor_id)
        self.assertEqual(second.attempt, 1)
        replay = self.command(thread, node, action="retry", request="retry-1")
        self.assertEqual(replay.executor_id, second.executor_id)
        self.assertEqual(len(self.submits), 2)
        self.assertEqual(set(current_execution_claims(self.current(node))), {"retry:retry-1"})
        self.storage.update_background_task(first.executor_id, status="completed")
        self.service.emit_event("local", thread.thread_id, "run_completed", node_id=node.node_id,
            attempt=0, idempotency_key="late-old", payload={"output_refs": ["stale"]})
        self.assertEqual(self.current(node).status, "running")
        self.assertEqual(self.current(node).output_refs, [])
        self.service.store.update_node(node.node_id, owner_id="local", executor_id="", expected_attempt=1)
        with self.assertRaisesRegex(LookupError, "attempt"):
            self.domain.registry.get("chat_run")._bind(self.current(node), first.executor_id)
        self.runtime._tick_thread(self.service.get_thread("local", thread.thread_id))
        self.assertEqual(self.current(node).executor_id, second.executor_id)
        self.assertEqual(len(self.submits), 2)

    def test_duplicate_correlation_fails_closed(self):
        thread, node = self.node()
        _, claim, _ = self.service.store.claim_command("local", thread.thread_id, node.node_id, "start", "fixture")
        self.record("chat", claim["correlation"])
        self.record("chat", claim["correlation"])
        with self.assertRaisesRegex(AdapterError, "多条记录"):
            self.command(thread, node)
        self.assertEqual(self.current(node).executor_id, "")
        self.assertEqual(len(self.submits), 0)

    def test_pending_processing_and_completion_project_real_states(self):
        thread, node = self.node()
        result = self.command(thread, node)
        self.storage.update_background_task(result.executor_id, status="waiting")
        self.confirmations = [{"status": "pending"}]
        self.command(thread, node, action="sync", request="")
        self.assertEqual(self.current(node).status, "waiting_user")
        self.assertEqual(self.service.get_thread("local", thread.thread_id).status, "waiting_user")
        self.confirmations = [{"status": "processing"}]
        self.command(thread, node, action="sync", request="")
        self.assertEqual(self.current(node).status, "running")
        self.assertEqual(self.service.get_thread("local", thread.thread_id).status, "active")
        self.confirmations = [{"status": "pending"}]
        self.command(thread, node, action="sync", request="")
        self.confirmations = []
        self.storage.update_background_task(result.executor_id, status="completed")
        self.command(thread, node, action="sync", request="")
        self.assertEqual(self.current(node).status, "succeeded")
        self.assertEqual(self.service.get_thread("local", thread.thread_id).status, "completed")

    def test_waiting_cancel_waits_for_executor_and_interruption_is_legal(self):
        for action in ("cancel", "interrupt"):
            with self.subTest(action=action):
                thread, node = self.node()
                result = self.command(thread, node)
                self.confirmations = [{"status": "pending"}]
                self.command(thread, node, action="sync", request="")
                if action == "cancel":
                    self.command(thread, node, action="cancel", request="cancel")
                    self.assertEqual(self.current(node).status, "stopping")
                    self.assertTrue(self.current(node).cancel_requested)
                    self.storage.update_background_task(result.executor_id, status="cancelled")
                    self.command(thread, node, action="sync", request="")
                    self.assertEqual(self.current(node).status, "cancelled")
                else:
                    self.storage.update_background_task(result.executor_id, status="interrupted")
                    self.command(thread, node, action="sync", request="")
                    self.assertEqual(self.current(node).status, "interrupted")
                    self.assertEqual(self.service.get_thread("local", thread.thread_id).status, "needs_recovery")

    def test_stale_settlement_cannot_change_current_attempt(self):
        thread, node = self.node()
        self.service.store.claim_command("local", thread.thread_id, node.node_id, "start", "fixture")
        self.service.store.update_node(node.node_id, owner_id="local", attempt=1)
        with self.assertRaisesRegex(RuntimeError, "attempt"):
            self.service.store.settle_command("local", node.node_id, "start:0", {}, expected_attempt=0)
        self.assertEqual(self.current(node).checkpoint[SUPER_AGENT_CLAIMS_KEY]["start:0"]["state"], "pending")


if __name__ == "__main__":
    unittest.main()
