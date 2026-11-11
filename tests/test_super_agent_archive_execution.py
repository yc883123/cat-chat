"""Archive uses persisted execution state, including interrupted submissions."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from naiba.core.contracts import SUPER_AGENT_CLAIMS_KEY, SUPER_AGENT_EXECUTION_KEY
from naiba.storage.store import ChatStorage
from naiba.super_agent.router import AdapterRegistry
from naiba.super_agent.runtime import ExecutionCoordinator
from naiba.super_agent.service import SuperAgentService


class ArchiveExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.service = SuperAgentService(self.storage)
        self.addCleanup(self.service.shutdown)
        self.cid = self.storage.create_conversation(permission_mode="confirm")["id"]
        self.submits = []
        self.app = SimpleNamespace(storage=self.storage,
            runs=SimpleNamespace(submit_chat=self.submit, confirmation_snapshot=lambda _: []),
            jobs=SimpleNamespace())
        self.domain = SimpleNamespace(app=self.app, service=self.service, registry=AdapterRegistry())
        self.runtime = ExecutionCoordinator(self.domain)
        self.addCleanup(self.runtime.shutdown)

    def submit(self, body, *, execution_key):
        self.submits.append(execution_key)
        row = self.storage.create_run(self.cid, "archive fixture", {},
            {SUPER_AGENT_EXECUTION_KEY: execution_key}, kind="chat", owner_session_id=self.cid)
        self.storage.update_background_task(row["id"], status="running")
        return self.storage.get_background_task(row["id"])

    def node(self, *, start=False):
        thread = self.service.create_thread("local", "archive fixture", status="active")
        node = self.service.create_node("local", thread.thread_id, "chat", idempotency_key="first",
            status="ready", executor_type="chat_run",
            checkpoint={"conversation_id": self.cid, "start_request": {"message": "fixture"}})
        if start:
            self.runtime.command("local", thread.thread_id, node.node_id, "start", "first")
            node = self.service.get_node("local", node.node_id)
        self.service.store.update_thread(thread.thread_id, owner_id="local", status="paused")
        return self.service.get_thread("local", thread.thread_id), node

    def archive(self, thread):
        return self.service.archive_thread("local", thread.thread_id, request_id="archive")

    def force_legacy_archive(self, thread):
        # Simulate data written by the buggy release, bypassing today's gate.
        with self.storage._connect() as db:
            db.execute("UPDATE super_threads SET status='archived', archived_at=1 WHERE thread_id=?", (thread.thread_id,))

    def test_paused_running_task_cannot_be_hidden_then_terminal_can_archive(self):
        thread, node = self.node(start=True)
        with self.assertRaisesRegex(RuntimeError, "尚未确认结果"):
            self.archive(thread)
        self.assertIn(thread.thread_id, [t.thread_id for t in self.service.list_threads("local")])
        self.storage.update_background_task(node.executor_id, status="completed")
        self.runtime.control("local", thread.thread_id, "continue", "continue")
        self.runtime.tick()
        self.assertEqual(self.service.get_node("local", node.node_id).status, "succeeded")
        self.assertEqual(self.archive(thread).status, "archived")

    def test_launch_receipt_without_handle_blocks_archive(self):
        thread, node = self.node()
        self.service.store.claim_command("local", thread.thread_id, node.node_id, "start", "unknown")
        with self.assertRaisesRegex(RuntimeError, "尚未确认结果"):
            self.archive(thread)
        self.assertEqual(self.submits, [])

    def test_old_attempt_and_explicitly_unsubmitted_receipts_do_not_block(self):
        for claim in (
            {"attempt": -1, "state": "pending", "correlation": "old"},
            {"attempt": 0, "state": "done", "correlation": "rejected", "result": {"submission": "not_submitted"}},
        ):
            with self.subTest(claim=claim):
                thread, node = self.node()
                self.service.store.update_node(node.node_id, owner_id="local",
                    checkpoint={SUPER_AGENT_CLAIMS_KEY: {"start:0": claim}})
                self.assertEqual(self.archive(thread).status, "archived")

    def test_pending_retry_reservation_blocks_archive_before_attempt_increment(self):
        thread, node = self.node(start=True)
        self.service.store.update_node(node.node_id, owner_id="local", status="failed")
        self.service.store.claim_command("local", thread.thread_id, node.node_id, "retry", "retry")
        with self.assertRaisesRegex(RuntimeError, "尚未确认结果"):
            self.archive(thread)

    def test_stopping_and_interrupted_handles_are_not_archiveable(self):
        for status in ("stopping", "interrupted", "waiting_user"):
            with self.subTest(status=status):
                thread, node = self.node(start=True)
                self.service.store.update_node(node.node_id, owner_id="local", status=status)
                with self.assertRaisesRegex(RuntimeError, "尚未确认结果"):
                    self.archive(thread)
                self.storage.update_background_task(node.executor_id, status="cancelled")

    def test_archive_and_new_claim_are_serialized_in_the_database(self):
        thread, node = self.node()
        inside_archive, release_archive, claim_finished = threading.Event(), threading.Event(), threading.Event()
        failures, claims = [], []
        connect = self.service.store._connect

        class Connection:
            def __init__(self, db):
                self.db = db

            def __getattr__(self, key):
                return getattr(self.db, key)

            def execute(self, sql, *args):
                if (threading.current_thread().name == "archive-race"
                        and sql.startswith("SELECT * FROM super_nodes WHERE thread_id=")):
                    inside_archive.set()
                    if not release_archive.wait(5):
                        raise AssertionError("archive gate not released")
                return self.db.execute(sql, *args)

        def archive():
            try:
                self.archive(thread)
            except Exception as exc:
                failures.append(exc)

        def claim():
            try:
                self.service.store.claim_command("local", thread.thread_id, node.node_id, "start", "late")
            except RuntimeError as exc:
                claims.append(str(exc))
            finally:
                claim_finished.set()

        with patch.object(self.service.store, "_connect", side_effect=lambda: Connection(connect())):
            first = threading.Thread(target=archive, name="archive-race")
            second = threading.Thread(target=claim)
            first.start()
            try:
                self.assertTrue(inside_archive.wait(2))
                second.start()
                self.assertFalse(claim_finished.wait(0.05))
            finally:
                release_archive.set()
                first.join(5)
                if second.ident is not None:
                    second.join(5)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(claims, ["已归档任务不能派发新执行"])
        self.assertNotIn(SUPER_AGENT_CLAIMS_KEY, self.service.get_node("local", node.node_id).checkpoint)

    def test_legacy_archived_execution_collects_result_without_promoting_successor(self):
        thread, node = self.node(start=True)
        successor = self.service.create_node("local", thread.thread_id, "chat", idempotency_key="next",
            status="pending", dependencies=[node.node_id], executor_type="chat_run")
        self.force_legacy_archive(thread)
        self.storage.update_background_task(node.executor_id, status="completed")
        self.service.recover_all()
        self.runtime.tick()
        self.assertEqual(self.service.get_thread("local", thread.thread_id).status, "archived")
        self.assertEqual(self.service.get_node("local", node.node_id).status, "succeeded")
        self.assertEqual(self.service.get_node("local", successor.node_id).status, "pending")
        self.assertEqual(len(self.service.artifacts("local", thread.thread_id)), 1)
        self.assertEqual(len(self.submits), 1)
        self.assertNotIn(thread.thread_id, [t.thread_id for t in self.service.store.reconciliation_threads()])

    def test_legacy_archived_missing_handle_is_rebound_without_resubmission(self):
        thread, node = self.node(start=True)
        self.service.store.update_node(node.node_id, owner_id="local", executor_id="", status="interrupted")
        self.force_legacy_archive(thread)
        self.storage.update_background_task(node.executor_id, status="failed", error="source failed")
        self.runtime.tick()
        current = self.service.get_node("local", node.node_id)
        self.assertEqual(current.executor_id, node.executor_id)
        self.assertEqual(current.status, "failed")
        self.assertEqual(self.service.get_thread("local", thread.thread_id).status, "archived")
        self.assertEqual(len(self.submits), 1)

    def test_archived_ready_node_cannot_be_started_without_a_prior_execution(self):
        thread, node = self.node()
        self.archive(thread)
        with self.assertRaisesRegex(RuntimeError, "不能派发"):
            self.service.store.update_node(node.node_id, owner_id="local", status="running")

    def test_legacy_writer_result_is_read_without_ticking_or_promoting(self):
        from naiba.super_agent.domain_adapters import WriterPipelineAdapter
        thread = self.service.create_thread("local", "writer", status="active")
        node = self.service.create_node("local", thread.thread_id, "writer", idempotency_key="writer",
            status="running", executor_type="writer_pipeline", executor_id="task-old",
            checkpoint={"pipeline_id": "pipeline-old",
                        "domain_ref": {"domain": "writer", "book_id": "book", "stage": "draft", "domain_task_id": "task-old"}})
        self.app.writer = SimpleNamespace(store=SimpleNamespace(get_active_pipeline=lambda _: {"id": "pipeline-old", "status": "done"}))
        self.domain.registry.register(WriterPipelineAdapter(self.service, self.app))
        self.force_legacy_archive(thread)
        self.runtime.tick()
        self.assertEqual(self.service.get_node("local", node.node_id).status, "succeeded")
        self.assertEqual(self.service.get_thread("local", thread.thread_id).status, "archived")
        self.assertEqual(len(self.service.artifacts("local", thread.thread_id)), 1)

    def test_archived_writer_status_and_sync_use_only_readonly_reconciliation(self):
        from naiba.super_agent.domain_adapters import WriterPipelineAdapter
        self.domain.registry.register(WriterPipelineAdapter(self.service, self.app))
        for action in ("status", "sync"):
            with self.subTest(action=action):
                thread = self.service.create_thread("local", "writer", status="active")
                node = self.service.create_node("local", thread.thread_id, "writer", idempotency_key="writer",
                    status="running", executor_type="writer_pipeline", executor_id="task-old",
                    checkpoint={"pipeline_id": "pipeline-old",
                                "domain_ref": {"domain": "writer", "book_id": "book", "stage": "draft", "domain_task_id": "task-old"}})
                forbidden = Mock(side_effect=AssertionError("ordinary Writer access can advance execution"))
                self.app.writer = SimpleNamespace(
                    store=SimpleNamespace(get_active_pipeline=lambda _: {"id": "pipeline-old", "status": "done"}),
                    pipeline=SimpleNamespace(tick=forbidden), api_get_pipeline=forbidden)
                self.force_legacy_archive(thread)
                result = self.runtime.command("local", thread.thread_id, node.node_id, action, "")
                self.assertEqual(result.node_status, "succeeded")
                self.assertEqual(self.service.get_thread("local", thread.thread_id).status, "archived")
                self.assertEqual(len(self.service.artifacts("local", thread.thread_id)), 1)
                for control in ("start", "retry", "resume", "cancel"):
                    with self.assertRaisesRegex(RuntimeError, "已归档任务不能控制"):
                        self.runtime.command("local", thread.thread_id, node.node_id, control, "late-control")
                forbidden.assert_not_called()

    def test_archived_writer_does_not_collect_a_replacement_pipeline(self):
        from naiba.super_agent.domain_adapters import WriterPipelineAdapter
        thread = self.service.create_thread("local", "writer", status="active")
        node = self.service.create_node("local", thread.thread_id, "writer", idempotency_key="writer",
            status="running", executor_type="writer_pipeline", executor_id="task-old",
            checkpoint={"pipeline_id": "pipeline-old",
                        "domain_ref": {"domain": "writer", "book_id": "book", "stage": "draft"}})
        self.app.writer = SimpleNamespace(store=SimpleNamespace(get_active_pipeline=lambda _: {"id": "pipeline-new", "status": "done"}))
        self.domain.registry.register(WriterPipelineAdapter(self.service, self.app))
        self.force_legacy_archive(thread)
        self.runtime.tick()
        current = self.service.get_node("local", node.node_id)
        self.assertEqual(current.status, "running")
        self.assertIn("已被替换", current.error)
        self.assertEqual(self.service.artifacts("local", thread.thread_id), [])

    def test_legacy_novel_result_waits_for_review_without_creating_approval(self):
        from naiba.super_agent.domain_adapters import NovelStageAdapter
        thread = self.service.create_thread("local", "novel", status="active")
        node = self.service.create_node("local", thread.thread_id, "novel", idempotency_key="novel",
            status="running", executor_type="novel_stage", executor_id="task-old",
            checkpoint={"domain_ref": {"domain": "novel", "book_id": "book", "stage": "script", "domain_task_id": "task-old"}})
        self.app.studio = SimpleNamespace(jobs=SimpleNamespace(task=lambda _: {"id": "task-old", "status": "done"}))
        self.domain.registry.register(NovelStageAdapter(self.service, self.app))
        self.force_legacy_archive(thread)
        self.runtime.tick()
        self.assertEqual(self.service.get_node("local", node.node_id).status, "waiting_user")
        self.assertEqual(self.service.get_thread("local", thread.thread_id).status, "archived")
        self.assertEqual(self.service.approvals("local", thread.thread_id), [])
        self.assertEqual(len(self.service.artifacts("local", thread.thread_id)), 1)


if __name__ == "__main__":
    unittest.main()
