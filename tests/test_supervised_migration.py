"""v31 preserves old Chat/task rows and rolls back failed supervision DDL."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from naiba.storage import store as storage_module
from naiba.storage.store import ChatStorage
from naiba.super_agent.service import SuperAgentService


class SupervisedMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "chat.db"
        self.storage = ChatStorage(self.path)
        self.cid = self.storage.create_conversation(title="原有对话")["id"]
        self.storage.create_chat_run(self.cid, "原有人工消息", [], {}, {}, "craft")
        service = SuperAgentService(self.storage)
        self.addCleanup(service.shutdown)
        self.thread = service.create_thread("local", "旧单次任务")
        self.node = service.create_node("local", self.thread.thread_id, "chat", idempotency_key="old", executor_type="chat_run", status="ready")
        with self.storage._connect() as db:
            db.execute("DROP TABLE super_supervision_rounds")
            db.execute("DROP TABLE super_supervisions")
            db.execute("PRAGMA user_version=30")

    def test_upgrade_and_repeated_open_preserve_ordinary_chat_and_tasks(self):
        self.storage.apply_pending_migrations()
        first = ChatStorage(self.path)
        second = ChatStorage(self.path)
        for storage in (first, second):
            self.assertEqual(storage.get_user_version(), 31)
            messages = storage.get_conversation(self.cid)["messages"]
            self.assertEqual(messages[0]["content"], "原有人工消息")
            self.assertNotIn("supervised_source", messages[0]["metadata"])
            with storage._connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM super_supervisions").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT executor_type FROM super_nodes WHERE node_id=?", (self.node.node_id,)).fetchone()[0], "chat_run")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM super_threads WHERE thread_id=?", (self.thread.thread_id,)).fetchone()[0], 1)

    def test_failed_migration_rolls_back_schema_and_version_then_can_retry(self):
        original = storage_module.MIGRATIONS[31]
        def fail_after_ddl(db):
            original(db)
            raise sqlite3.OperationalError("injected nontransient migration failure")
        with patch.dict(storage_module.MIGRATIONS, {31: fail_after_ddl}):
            with self.assertRaisesRegex(sqlite3.OperationalError, "injected"):
                self.storage.apply_pending_migrations()
        self.assertEqual(self.storage.get_user_version(), 30)
        with self.storage._connect() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='super_supervisions'").fetchone())
            self.assertEqual(db.execute("SELECT content FROM messages WHERE conversation_id=?", (self.cid,)).fetchone()[0], "原有人工消息")
        self.storage.apply_pending_migrations()
        self.assertEqual(self.storage.get_user_version(), 31)


if __name__ == "__main__":
    unittest.main()
