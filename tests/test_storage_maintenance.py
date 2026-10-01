# -*- coding: utf-8 -*-
"""护栏：历史数据管理——storage_usage 统计与 compact_database 压缩。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.storage.store import ChatStorage  # noqa: E402


class StorageMaintenanceTests(unittest.TestCase):
    def test_storage_usage_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            convo = storage.create_conversation()
            agent = {"id": "general", "name": "通用 Agent"}
            run = storage.create_chat_run(
                convo["id"], "消息", [], agent, {"model_key": "online:demo"}, "craft"
            )
            storage.update_background_task(run["id"], status="completed", finished=True)
            usage = storage.storage_usage()
            self.assertGreater(usage["db_bytes"], 0)
            self.assertGreaterEqual(usage["event_count"], 0)
            self.assertGreaterEqual(usage["task_count"], 1)
            self.assertGreaterEqual(usage["terminal_task_count"], 1)
            self.assertGreaterEqual(usage["snapshot_chars"], 0)
            self.assertGreaterEqual(usage["message_count"], 1)
            self.assertIn("event_payload_chars", usage)

    def test_compact_database_reports_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            convo = storage.create_conversation()
            agent = {"id": "general", "name": "通用 Agent"}
            run = storage.create_chat_run(
                convo["id"], "消息", [], agent, {"model_key": "online:demo"}, "craft"
            )
            storage.update_background_task(run["id"], status="completed", finished=True)
            result = storage.compact_database()
            self.assertIn("before_bytes", result)
            self.assertIn("after_bytes", result)
            self.assertGreater(result["after_bytes"], 0)
            # 压缩后库仍可正常读写
            after = storage.storage_usage()
            self.assertGreaterEqual(after["task_count"], 1)
            self.assertEqual(storage.check_integrity()["ok"], True)


if __name__ == "__main__":
    unittest.main()
