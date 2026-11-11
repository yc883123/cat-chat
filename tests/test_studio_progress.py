"""Studio 静默等待提示、取消尾块与终态后的回调屏障。"""

from __future__ import annotations

import contextlib
import io
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from naiba.core.exceptions import TaskCancelled
from naiba.studio.runner import StudioRunner
from naiba.studio.store import StudioStore


class StudioProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = StudioStore(Path(temporary.name))
        self.models = SimpleNamespace(complete=lambda *_: "完成")
        config = SimpleNamespace(
            data={},
            profile=lambda _: {"kind": "online", "model": "stub", "context_window": 8000},
            runtime_guard_options=lambda: {},
        )
        self.runner = StudioRunner(SimpleNamespace(config=config, models=self.models), self.store)
        self.addCleanup(self.runner.shutdown)
        self.task = self.store.create_task({
            "content": "生成内容", "model_key": "online:stub", "model_name": "stub",
        })

    def run_task(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.runner._run_task(self.task["id"])
        return self.store.get_task(self.task["id"])

    def test_silence_flushes_thinking_then_reports_wait_without_late_events(self):
        waiting = threading.Event()
        callbacks = []
        append = self.store.append_event

        def watch(task_id, payload):
            result = append(task_id, payload)
            if "无新内容" in payload.get("message", ""):
                waiting.set()
            return result

        def complete(profile, messages, options, callback):
            callbacks.append(callback)
            callback({"type": "reasoning_start"})
            callback({"type": "reasoning_delta", "content": "已收到的思考"})
            self.assertTrue(waiting.wait(2), "静默期间没有发出等待提示")
            callback({"type": "reasoning_end"})
            callback({"type": "delta", "content": "恢复后的正文"})
            return "恢复后的正文"

        self.models.complete = complete
        with patch("naiba.studio.runner.HEARTBEAT_SECONDS", 0.02), \
                patch("naiba.studio.runner.FLUSH_SECONDS", 60), \
                patch.object(self.store, "append_event", side_effect=watch):
            state = self.run_task()
        self.assertEqual(state["status"], "done")
        events = self.store.read_events(self.task["id"])
        reasoning_index = next(i for i, row in enumerate(events) if row["type"] == "reasoning_delta")
        waiting_index = next(i for i, row in enumerate(events) if "无新内容" in row.get("message", ""))
        self.assertLess(reasoning_index, waiting_index)
        self.assertIn("等待模型继续输出", events[waiting_index]["message"])
        self.assertEqual(events[-1]["type"], "done")
        callbacks[0]({"type": "delta", "content": "不得写进终态的迟到内容"})
        callbacks[0]({"type": "status", "message": "不得覆盖终态"})
        self.assertEqual(self.store.read_events(self.task["id"]), events)
        self.assertFalse(any(t.name == "naiba-studio-progress" for t in threading.enumerate()))

    def test_cancel_preserves_buffered_tail_and_discards_late_callback(self):
        def complete(profile, messages, options, callback):
            callback({"type": "reasoning_delta", "content": "短思考"})
            callback({"type": "delta", "content": "尚未达到合批阈值的短尾"})
            options["cancel_event"].set()
            callback({"type": "delta", "content": "取消后的额外内容"})
            raise TaskCancelled("任务已取消")

        self.models.complete = complete
        with patch("naiba.studio.runner.FLUSH_SECONDS", 60):
            state = self.run_task()
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["result_partial"], "尚未达到合批阈值的短尾")
        events = self.store.read_events(self.task["id"])
        self.assertEqual("".join(row.get("content", "") for row in events if row["type"] == "reasoning_delta"), "短思考")
        self.assertEqual(events[-1]["type"], "cancelled")

    def test_completion_cannot_override_cancel(self):
        def complete(profile, messages, options, callback):
            callback({"type": "delta", "content": "已有内容"})
            options["cancel_event"].set()
            return "迟到的成功"

        self.models.complete = complete
        state = self.run_task()
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["result_partial"], "已有内容")

    def test_shutdown_before_cancel_registration_does_not_start_model(self):
        self.runner.shutdown()
        with patch.object(self.models, "complete") as complete:
            state = self.run_task()
        complete.assert_not_called()
        self.assertEqual(state["status"], "cancelled")
        self.assertIsNotNone(state["finished_at"])


if __name__ == "__main__":
    unittest.main()
