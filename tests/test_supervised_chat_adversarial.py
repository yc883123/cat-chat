"""Independent fault-boundary gates for supervisor cancellation and deadlines."""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from tests import test_supervised_chat as fixtures


class SupervisedBoundaryTests(unittest.TestCase):
    setUp = fixtures.SupervisedAppTests.setUp
    complete = fixtures.SupervisedAppTests.complete
    create = fixtures.SupervisedAppTests.create
    start = fixtures.SupervisedAppTests.start
    wait_phase = fixtures.SupervisedAppTests.wait_phase

    def hold_execution(self):
        gate = threading.Event()
        entered = threading.Event()
        original = self.complete

        def held(profile, messages, options, status=None):
            if not messages[0].get("content", "").startswith("你是任务结果审查器"):
                entered.set()
                gate.wait(4)
            return original(profile, messages, options, status)

        self.app.models.complete = held
        self.addCleanup(gate.set)
        return gate, entered

    def test_cancel_between_run_registration_and_node_binding_cancels_real_run(self):
        gate, entered = self.hold_execution()
        self.create()
        submit = self.app.runs.submit_chat

        def cancel_after_registration(*args, **kwargs):
            run = submit(*args, **kwargs)
            self.assertTrue(entered.wait(2))
            self.adapter.control("local", self.tid, "cancel")
            return run

        try:
            with patch.object(self.app.runs, "submit_chat", side_effect=cancel_after_registration), \
                    patch.object(self.app.runs, "cancel", wraps=self.app.runs.cancel) as cancel:
                self.start()
                self.assertGreaterEqual(cancel.call_count, 1, "A registered Run must be cancelled even before node binding")
                record = self.adapter.store.get(self.tid)
                self.assertNotEqual(record["state"]["phase"], "executing")
        finally:
            gate.set()

    def test_paused_task_does_not_extend_deadline_for_inflight_run(self):
        gate, entered = self.hold_execution()
        self.create()
        self.start()
        self.assertTrue(entered.wait(2))
        self.adapter.control("local", self.tid, "pause")
        record = self.adapter.store.get(self.tid)
        deadline = record["state"]["deadline_at"]
        try:
            with patch("naiba.super_agent.supervised._now", return_value=deadline + 1), \
                    patch.object(self.app.runs, "cancel", wraps=self.app.runs.cancel) as cancel:
                self.adapter.sync("local", self.tid, self.nid)
                self.assertGreaterEqual(cancel.call_count, 1, "Pause cannot hide deadline cancellation")
                self.assertNotEqual(self.adapter.detail("local", self.tid)["phase"], "completed")
        finally:
            gate.set()

    def test_late_review_after_deadline_cannot_complete_without_a_poll(self):
        self.gate = threading.Event()
        self.create()
        self.start()
        state = self.wait_phase("reviewing")
        deadline = state["deadline_at"]
        with patch("naiba.super_agent.supervised._now", return_value=deadline + 1):
            self.gate.set()
            until = time.monotonic() + 3
            while time.monotonic() < until:
                with self.adapter.workers_lock:
                    active = self.tid in self.adapter.workers
                if not active:
                    break
                time.sleep(.01)
            self.assertFalse(active, "review worker did not return")
            self.assertNotEqual(self.adapter.detail("local", self.tid)["phase"], "completed",
                                "A late review must recheck deadline at commit, not rely on the next poll")
            self.assertNotEqual(self.app.super_agent.get_thread("local", self.tid).status, "completed")


if __name__ == "__main__":
    unittest.main()
