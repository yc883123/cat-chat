from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing
import unittest
from pathlib import Path
from unittest.mock import patch

from naiba.storage.store import ChatStorage
from naiba.super_agent.service import SuperAgentService


class SuperAgentP0BoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.service = SuperAgentService(self.storage)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_snapshot_cannot_be_changed_after_creation(self) -> None:
        thread = self.service.create_thread("u", "t", execution_snapshot={"provider_id": "p", "model_key": "m"})
        with self.assertRaises(ValueError):
            self.service.store.update_thread(thread.thread_id, owner_id="u", execution_snapshot={"provider_id": "other"})
        node = self.service.create_node("u", thread.thread_id, "w", idempotency_key="w", execution_snapshot={"model_key": "m"})
        with self.assertRaises(ValueError):
            self.service.store.update_node(node.node_id, owner_id="u", execution_snapshot={"model_key": "other"})

    def test_event_idempotency_and_relations_are_strict(self) -> None:
        thread = self.service.create_thread("u", "t")
        other = self.service.create_thread("u", "other")
        node = self.service.create_node("u", thread.thread_id, "w", idempotency_key="w")
        with self.assertRaises(ValueError):
            self.service.emit_event("u", thread.thread_id, "progress", node_id=node.node_id)
        with self.assertRaises(LookupError):
            self.service.emit_event("u", other.thread_id, "progress", node_id=node.node_id, idempotency_key="bad", attempt=0)
        with self.assertRaises(LookupError):
            self.service.create_artifact("u", other.thread_id, node.node_id, kind="x", path_or_ref="ref", idempotency_key="x")
        with self.assertRaises(LookupError):
            self.service.create_artifact("other-owner", thread.thread_id, node.node_id, kind="x", path_or_ref="ref", idempotency_key="y")

    def test_approval_reject_and_duplicate_resolution(self) -> None:
        thread = self.service.create_thread("u", "t")
        node = self.service.create_node("u", thread.thread_id, "w", idempotency_key="w")
        first = self.service.create_approval("u", thread.thread_id, node.node_id, action="delete", description="x", idempotency_key="approval")
        duplicate = self.service.create_approval("u", thread.thread_id, node.node_id, action="changed", description="changed", idempotency_key="approval")
        self.assertEqual(first.approval_id, duplicate.approval_id)
        resolved = self.service.resolve_approval("u", thread.thread_id, first.approval_id, status="rejected", resolved_by="u", request_id="resolve-1")
        self.assertEqual(resolved.status, "rejected")
        same = self.service.resolve_approval("u", thread.thread_id, first.approval_id, status="rejected", resolved_by="u", request_id="resolve-1")
        self.assertEqual(same.status, "rejected")
        with self.assertRaises(RuntimeError):
            self.service.resolve_approval("u", thread.thread_id, first.approval_id, status="approved", resolved_by="u", request_id="resolve-2")

    def test_control_actions_require_request_ids(self) -> None:
        thread = self.service.create_thread("u", "t")
        with self.assertRaises(ValueError):
            self.service.pause_thread("u", thread.thread_id, request_id="")
        with self.assertRaises(ValueError):
            self.service.resume_thread("u", thread.thread_id, request_id="")
        with self.assertRaises(ValueError):
            self.service.cancel_thread("u", thread.thread_id, request_id="")

    def test_recovery_is_idempotent_and_keeps_waiting_and_paused(self) -> None:
        running_thread = self.service.create_thread("u", "running")
        running = self.service.create_node("u", running_thread.thread_id, "w", idempotency_key="running")
        self.service.start_node("u", running.node_id)
        waiting_thread = self.service.create_thread("u", "waiting")
        waiting = self.service.create_node("u", waiting_thread.thread_id, "w", idempotency_key="waiting")
        self.service.create_approval("u", waiting_thread.thread_id, waiting.node_id, action="publish", description="x", idempotency_key="waiting-approval")
        paused_thread = self.service.create_thread("u", "paused")
        paused = self.service.create_node("u", paused_thread.thread_id, "w", idempotency_key="paused")
        self.service.start_node("u", paused.node_id)
        self.service.pause_thread("u", paused_thread.thread_id, request_id="pause")
        self.service.recover_all()
        self.assertEqual(self.service.get_node("u", running.node_id).status, "interrupted")
        self.assertEqual(self.service.get_thread("u", running_thread.thread_id).status, "needs_recovery")
        self.assertEqual(self.service.get_node("u", waiting.node_id).status, "waiting_user")
        self.assertEqual(self.service.get_thread("u", paused_thread.thread_id).status, "paused")
        before = len(self.service.events("u", running_thread.thread_id))
        self.service.recover_all()
        self.assertEqual(len(self.service.events("u", running_thread.thread_id)), before)

    def test_v29_migration_failure_rolls_back_version_and_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "chat.db"
            storage = ChatStorage(path)
            with closing(sqlite3.connect(path)) as db:
                db.execute("DROP TABLE super_threads")
                db.execute("PRAGMA user_version = 28")
                db.commit()
            import naiba.storage.store as storage_module
            original = storage_module.MIGRATIONS[29]
            storage_module.MIGRATIONS[29] = lambda db: (_ for _ in ()).throw(RuntimeError("boom"))
            try:
                with self.assertRaises(RuntimeError):
                    storage.apply_pending_migrations()
            finally:
                storage_module.MIGRATIONS[29] = original
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 28)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='super_threads'").fetchone())


if __name__ == "__main__":
    unittest.main()
