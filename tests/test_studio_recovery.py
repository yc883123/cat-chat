"""Studio 重启收敛保留产出、完成时间与可重试输入。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from naiba.studio.jobs import StudioJobManager
from naiba.studio.runner import StudioRunner
from naiba.studio.store import StudioStore


class StudioRecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = StudioStore(Path(folder.name))
        self.app = Mock()
        self.runner = StudioRunner(self.app, self.store)
        self.addCleanup(self.runner.shutdown)
        self.jobs = StudioJobManager(self.app, self.store, self.runner)

    def task(self, **fields):
        task = self.store.create_task({
            "content": "恢复后重试原始输入", "model_key": "online:fixture",
            "model_name": "fixture-model", "user_request": "甘雨",
        })
        if fields:
            task = self.store.update_task(task["id"], **fields)
        return task

    def test_recovery_records_terminal_time_and_preserves_streamed_partial(self):
        task = self.task(status="running", started_at=1000, reasoning=["已有思考"])
        self.store.append_event(task["id"], {"type": "reasoning_delta", "content": "思考不混入正文"})
        self.store.append_event(task["id"], {"type": "delta", "content": "已生成"})
        self.store.append_event(task["id"], {"type": "delta", "content": "的正文"})
        with patch("naiba.studio.jobs.now_ms", return_value=5000):
            self.assertEqual(self.jobs.recover_interrupted(), 1)
        recovered = self.store.get_task(task["id"])
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["started_at"], 1000)
        self.assertEqual(recovered["finished_at"], 5000)
        self.assertEqual(recovered["result_partial"], "已生成的正文")
        self.assertEqual(recovered["reasoning"], ["已有思考"])
        self.assertEqual(recovered["input"], task["input"])
        self.assertEqual(self.store.read_events(task["id"])[-1]["type"], "error")

    def test_queued_recovery_has_completion_time_without_start_time(self):
        task = self.task()
        with patch("naiba.studio.jobs.now_ms", return_value=5000):
            self.assertEqual(self.jobs.recover_interrupted(), 1)
        recovered = self.store.get_task(task["id"])
        self.assertEqual(recovered["status"], "interrupted")
        self.assertIsNone(recovered["started_at"])
        self.assertEqual(recovered["finished_at"], 5000)
        self.assertEqual(recovered["result_partial"], "")

    def test_recovery_is_idempotent_and_does_not_rewrite_existing_terminal_tasks(self):
        running = self.task(status="running")
        for status in ("done", "failed", "cancelled", "interrupted"):
            self.task(status=status, finished_at=2000, result="已有完整结果")
        original_terminal = {
            task["id"]: task for task in self.store.list_tasks()
            if task["id"] != running["id"]
        }
        self.assertEqual(self.jobs.recover_interrupted(), 1)
        recovered = self.store.get_task(running["id"])
        events = self.store.read_events(running["id"])
        self.assertEqual(self.jobs.recover_interrupted(), 0)
        self.assertEqual(self.store.get_task(running["id"]), recovered)
        self.assertEqual(self.store.read_events(running["id"]), events)
        for task_id, original in original_terminal.items():
            self.assertEqual(self.store.get_task(task_id), original)

    def test_recovery_keeps_saved_partial_when_event_log_has_no_body(self):
        task = self.task(status="running", result_partial="已保存的部分正文")
        self.store.append_event(task["id"], {"type": "reasoning_delta", "content": "思考"})
        self.jobs.recover_interrupted()
        self.assertEqual(self.store.get_task(task["id"])["result_partial"], "已保存的部分正文")

    def test_retry_uses_frozen_input_and_preserves_interrupted_record(self):
        task = self.task(status="running")
        self.store.append_event(task["id"], {"type": "delta", "content": "上次部分正文"})
        self.jobs.recover_interrupted()
        source = self.store.get_task(task["id"])
        with patch.object(self.runner, "submit") as submit:
            retried = self.jobs.retry(task["id"])
        self.assertNotEqual(retried["id"], source["id"])
        self.assertEqual(retried["status"], "queued")
        self.assertEqual(retried["source_task_id"], source["id"])
        self.assertEqual(retried["input"], source["input"])
        self.assertIsNone(retried["finished_at"])
        submit.assert_called_once_with(retried["id"])
        self.assertEqual(self.store.get_task(source["id"]), source)


if __name__ == "__main__":
    unittest.main()
