"""Tests for GOAL continuous execution, unified session permissions, cancel propagation,
todo_write DB snapshots, and subagent goal_id linkage."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from naiba.jobs import JobSpec
from naiba.storage.goals import GoalConflict, goal_json
from naiba.tools.providers.jobs import _todo_write_handler
from tests import test_goals as fixture


class GoalContinuousAndStatusTests(unittest.TestCase):
    def setUp(self):
        fixture.GoalTests.setUp(self)

    transport = fixture.GoalTests.transport
    text = staticmethod(fixture.GoalTests.text)
    call = staticmethod(fixture.GoalTests.call)
    submit = fixture.GoalTests.submit
    wait = fixture.GoalTests.wait

    def test_new_goal_defaults_to_continuous_and_ignores_default_caps(self):
        """New GOAL uses continuous policy (deadline_at=0) and crosses stored bounded limits."""
        from naiba.run import goals as goal_runtime

        counter = {"n": 0}
        for i in range(1, 4):
            (self.workspace / f"note-{i}.txt").write_text(f"hello continuous {i}", encoding="utf-8")

        def produce(body):
            counter["n"] += 1
            if counter["n"] <= 3:
                return self.call("read_file", {"path": f"note-{counter['n']}.txt"}, f"read-{counter['n']}")
            return self.text("已完成。\nGOAL: completed")

        self.reply = produce
        with patch.object(self.app.goals, "start"):
            goal = self.submit(criteria=[{"path": "note-1.txt"}])
        self.assertEqual(goal["execution_policy"], "continuous")
        self.assertEqual(goal["deadline_at"], 0)
        # Shrink stored bounded limits artificially below actual usage — continuous policy must NOT stop.
        with self.app.storage._connect() as db:
            db.execute(
                "UPDATE chat_goals SET limits=? WHERE id=?",
                (goal_json({"model_requests": 1, "tool_actions": 1, "rounds": 1}), goal["id"]),
            )
        with patch.object(goal_runtime, "_QUANTUM", 2):
            self.app.goals.start()
            result = self.wait(goal["id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertGreaterEqual(result["generation"], 2)
        self.assertEqual(result["model_requests"], 4)
        self.assertEqual(result["tool_actions"], 3)

    def test_read_only_multi_round_progress_does_not_false_trigger_no_progress(self):
        """Multiple read-only / todo rounds count as real progress and do not trigger false no_progress."""
        from naiba.run import goals as goal_runtime

        self.app.storage.set_enabled_tool_ids(self.cid, ["read_file", "todo_write", "write_file", "edit_file"])
        (self.workspace / "info.txt").write_text("details", encoding="utf-8")
        step = {"n": 0}

        def produce(body):
            step["n"] += 1
            if step["n"] == 1:
                return self.call("todo_write", {"todos": [
                    {"id": "1", "content": "阅读资料", "status": "in_progress"},
                    {"id": "2", "content": "输出结论", "status": "pending"},
                ]}, "todo-1")
            if step["n"] in {2, 3}:
                return self.call("read_file", {"path": "info.txt"}, f"read-{step['n']}")
            if step["n"] == 4:
                return self.call("todo_write", {"todos": [
                    {"id": "1", "content": "阅读资料", "status": "completed"},
                    {"id": "2", "content": "输出结论", "status": "completed"},
                ]}, "todo-2")
            return self.text("分析完成。\nGOAL: completed")

        self.reply = produce
        # Force a round boundary after every tool call so 4 consecutive write-free rounds run.
        with patch.object(goal_runtime, "_QUANTUM", 2):
            goal = self.submit()
            result = self.wait(goal["id"])
        self.assertEqual(result["status"], "awaiting_review", result)
        self.assertGreaterEqual(result["generation"], 3)
        conv = self.app.storage.get_conversation(self.cid, include_messages=False)
        self.assertEqual(len(conv["todos"]), 2)
        self.assertTrue(all(t["status"] == "completed" for t in conv["todos"]))

    def test_repeated_error_loop_breaks_into_paused_and_allows_resume(self):
        """3 consecutive rounds ending with the same tool error auto-pause with loop_detected."""
        from naiba.run import goals as goal_runtime

        step = {"n": 0}

        def produce(body):
            step["n"] += 1
            if step["n"] <= 3:
                return self.call("read_file", {"path": "missing.txt"}, f"missing-{step['n']}")
            return self.text("已恢复并完成。\nGOAL: completed")

        self.reply = produce
        with patch.object(goal_runtime, "_QUANTUM", 2):
            goal = self.submit()
            paused = self.wait(goal["id"], ("paused", "blocked", "completed", "awaiting_review"))
            self.assertEqual(paused["status"], "paused", paused)
            self.assertEqual(paused["stop_code"], "loop_detected")
            resumed = self.app.goals.control(goal["id"], "resume", paused["version"])
            self.assertEqual(resumed["status"], "queued")
            final = self.wait(goal["id"], ("awaiting_review", "completed", "paused", "blocked"))
            self.assertEqual(final["status"], "awaiting_review", final)

    def test_full_permission_allows_external_paths_but_blocks_sensitive_and_data_dir(self):
        """Full permission mode allows normal outside paths while keeping sensitive/data_dir blocked."""
        ext_tmp = tempfile.TemporaryDirectory(prefix="naiba-ext-")
        self.addCleanup(ext_tmp.cleanup)
        ext_dir = Path(ext_tmp.name).resolve()
        ext_file = ext_dir / "external.txt"
        ext_file.write_text("outside ok", encoding="utf-8")
        env_file = ext_dir / ".env"
        env_file.write_text("SECRET=1", encoding="utf-8")

        with patch.object(self.app.goals, "start"):
            goal = self.submit()
        self.assertTrue(self.app.goals.store.claim(goal["id"], goal["run_id"]))
        snapshot = self.app.storage.get_run_snapshot(goal["run_id"])
        cycle = self.app.goals.cycle(goal["id"], goal["run_id"], snapshot)
        # Full permission allows external read & write.
        self.assertEqual(cycle.path(ext_file, writing=False), ext_file)
        self.assertEqual(cycle.path(ext_dir / "new-ext.txt", writing=True), (ext_dir / "new-ext.txt").resolve())
        # Sensitive files & data_dir remain blocked even in full mode.
        with self.assertRaises(GoalConflict):
            cycle.path(env_file, writing=False)
        with self.assertRaises(GoalConflict):
            cycle.path(self.paths.data_dir / "chat.db", writing=False)

        # Auto permission blocks external paths.
        auto_snapshot = {**snapshot, "permission_mode": "auto"}
        auto_cycle = self.app.goals.cycle(goal["id"], goal["run_id"], auto_snapshot)
        with self.assertRaises(GoalConflict):
            auto_cycle.path(ext_file, writing=False)

    def test_goal_cancel_propagates_to_linked_subagents_and_jobs(self):
        """Cancelling a GOAL also cancels active subagents/jobs linked to that goal_id."""
        started = threading.Event()
        released = threading.Event()

        def slow_subagent(job_id, spec, cancel, emit):
            started.set()
            while not cancel.is_set() and not released.is_set():
                time.sleep(0.02)

        self.app.jobs.agent_runner = slow_subagent
        self.addCleanup(released.set)

        with patch.object(self.app.goals, "start"):
            goal = self.submit()
        job_id = self.app.jobs.start(JobSpec(
            kind="subagent",
            conversation_id=self.cid,
            params={"instruction": "长任务"},
            label="子任务：测试级联取消",
            parent_job_id=goal["run_id"],
            owner_session_id=self.cid,
            goal_id=goal["id"],
        ), owner=self.cid)
        self.assertTrue(started.wait(5), "subagent never started")

        # Verify goal_id is persisted and exposed on task listings.
        listed = self.app.storage.list_background_tasks(self.cid)
        sub_row = next((t for t in listed if t["id"] == job_id), None)
        self.assertIsNotNone(sub_row)
        self.assertEqual(sub_row["goal_id"], goal["id"])

        goal_tasks = self.app.storage.list_goal_tasks(goal["id"])
        self.assertTrue(any(t["id"] == job_id for t in goal_tasks))

        self.app.goals.control(goal["id"], "cancel", goal["version"])
        final = self.wait(goal["id"], ("cancelled",))
        self.assertEqual(final["status"], "cancelled")

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = self.app.storage.get_background_task(job_id)
            if task and task["status"] in {"cancelled", "completed", "failed"}:
                break
            time.sleep(0.03)
        task = self.app.storage.get_background_task(job_id)
        self.assertIn(task["status"], {"cancelled", "stopping"})
        self.assertTrue(task["cancel_requested"])

    def test_rebaseline_resume_adopts_updated_conversation_settings(self):
        """When config drifts to needs_user(configuration), resume(rebaseline=True) updates baseline and continues."""
        with patch.object(self.app.goals, "start"):
            goal = self.submit()
        self.app.storage.update_conversation_settings(self.cid, permission_mode="auto")
        self.app.goals.start()
        needs = self.wait(goal["id"], ("needs_user", "blocked"))
        self.assertEqual(needs["status"], "needs_user", needs)
        self.assertEqual(needs["stop_code"], "configuration")
        self.app.goals.control(goal["id"], "resume", needs["version"], extras={"rebaseline": True})
        done = self.wait(goal["id"], ("awaiting_review", "completed", "blocked", "needs_user"))
        self.assertEqual(done["status"], "awaiting_review", done)

    def test_todo_write_persists_snapshot_and_clears_on_clear_messages(self):
        ok, payload = _todo_write_handler(
            self.app,
            {"todos": [
                {"id": "t1", "content": "第一步", "status": "completed"},
                {"id": "t2", "content": "第二步", "status": "in_progress"},
            ]},
            [],
            {"run_id": "run-1", "conversation_id": self.cid},
        )
        self.assertTrue(ok, payload)
        conv = self.app.storage.get_conversation(self.cid, include_messages=False)
        self.assertEqual(len(conv["todos"]), 2)
        self.assertEqual(conv["todos"][1]["status"], "in_progress")

        self.app.storage.clear_conversation_messages(self.cid)
        cleared = self.app.storage.get_conversation(self.cid, include_messages=False)
        self.assertEqual(cleared["todos"], [])

    def test_confirm_mode_pauses_write_and_discards_unexecuted_generic_receipt(self):
        """Confirm mode pauses write_file/pwsh into needs_user without leaving stale pending receipts."""
        # 1. Confirm mode blocks write_file before writing anything.
        self.app.storage.update_conversation_settings(self.cid, permission_mode="confirm")
        self.reply = lambda body: self.call("write_file", {"path": "blocked.txt", "content": "nope"}, "w-1")
        goal1 = self.submit(request_id="confirm-write")
        res1 = self.wait(goal1["id"], ("needs_user", "blocked", "completed"))
        self.assertEqual(res1["status"], "needs_user", res1)
        self.assertFalse((self.workspace / "blocked.txt").exists())
        self.assertEqual(self.app.goals.store.pending_actions(goal1["id"]), [])
        self.app.goals.control(goal1["id"], "cancel", res1["version"])
        self.wait(goal1["id"], ("cancelled",))

        # 2. Confirm mode on side-effect generic tool (pwsh) discards pending receipt on NEED_CONFIRM,
        # allowing clean rebaseline + resume once permission is upgraded to full.
        self.app.storage.set_enabled_tool_ids(self.cid, ["read_file", "write_file", "pwsh"])
        step = {"n": 0}

        def produce(body):
            step["n"] += 1
            if step["n"] == 1:
                return self.call("pwsh", {"command": "Write-Output ok"}, "pw-1")
            return self.text("已按新权限完成。\nGOAL: completed")

        self.reply = produce
        goal2 = self.submit(request_id="confirm-pwsh")
        res2 = self.wait(goal2["id"], ("needs_user", "blocked", "completed"))
        self.assertEqual(res2["status"], "needs_user", res2)
        self.assertEqual(self.app.goals.store.pending_actions(goal2["id"]), [])
        self.app.storage.update_conversation_settings(self.cid, permission_mode="full")
        self.app.goals.control(goal2["id"], "resume", res2["version"], extras={"rebaseline": True})
        snap = self.app.storage.get_run_snapshot(res2["run_id"])
        self.assertIn("pwsh", snap.get("allowed_tools") or [])
        final2 = self.wait(goal2["id"], ("awaiting_review", "completed", "blocked", "needs_user"))
        self.assertEqual(final2["status"], "awaiting_review", final2)


if __name__ == "__main__":
    unittest.main()
