"""GOAL crash/race/budget tests using isolated real workers and fake transport only."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from naiba.app import NaibaChatApp
from naiba.run import goals as goal_runtime
from naiba.storage.goals import GoalConflict
from tests import test_goals as fixture
from tests.test_compaction import GOOD_SUMMARY


class GoalRecoveryTests(unittest.TestCase):
    # Reuse setup and wire helpers without inheriting/re-running its test methods.
    def setUp(self):
        fixture.GoalTests.setUp(self)
        (self.paths.config_path.parent / "errors").mkdir(exist_ok=True)

    transport = fixture.GoalTests.transport
    text = staticmethod(fixture.GoalTests.text)
    call = staticmethod(fixture.GoalTests.call)
    submit = fixture.GoalTests.submit
    wait = fixture.GoalTests.wait

    def queued(self, **kwargs):
        with patch.object(self.app.goals, "start"):
            goal = self.submit(**kwargs)
        self.assertEqual(self.app.goals.store.get(goal["id"])["status"], "queued")
        return goal

    def drive(self, goal_id, *, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.app.goals.store.get(goal_id)
            if row["status"] not in {"queued", "running", "pausing", "cancelling"}:
                return row
            with self.app.goals._lock:
                self.app.goals._tick(goal_id)
            time.sleep(.005)
        self.fail(f"scheduler timed out: {self.app.goals.store.get(goal_id)}")

    def restart(self):
        self.app.stop()
        with patch.object(goal_runtime.GoalService, "start"):
            self.app = NaibaChatApp(paths=self.paths)
        self.addCleanup(self.app.stop)

    def rounds(self, goal_id):
        with self.app.storage._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM chat_goal_rounds WHERE goal_id=? ORDER BY generation", (goal_id,))]

    def test_oversized_checkpoint_compacts_before_resume_without_empty_run_spin(self):
        from naiba.core import compaction

        self.enterContext(patch.object(goal_runtime, "_QUANTUM", 8))
        self.app.config.data["providers"][0]["context_window"] = 12000
        self.app.config.data["auto_compaction"] = {"mode": "auto", "threshold_percent": 30}
        self.app.config.save()
        for number in range(3):
            (self.workspace / f"large-{number}.txt").write_text(
                f"独立资料 {number}。\n" + "事实。" * 850, encoding="utf-8")
        formal, hidden = [], []

        def produce(body):
            if any(str(m.get("content") or "").startswith(compaction.SUMMARIZE_INSTRUCTION)
                   for m in body["messages"]):
                hidden.append(body)
                return self.text(GOOD_SUMMARY)
            formal.append(body)
            index = len(formal) - 1
            if index == 0:
                return self.call("write_file", {"path": "result.txt", "content": "verified-once"}, "write-once")
            if index <= 3:
                return self.call("read_file", {"path": f"large-{index - 1}.txt"}, f"read-{index}")
            return self.text("产物已核对。\nGOAL: completed")

        self.reply = produce
        goal = self.queued(message="写入产物后核对三份独立资料，再验收完成。",
                           criteria=[{"path": "result.txt", "contains": "verified-once"}])
        self.app.goals._tick(goal["id"])
        with self.app.runs._lock:
            worker = self.app.runs._threads.get(goal["run_id"])
        if worker:
            worker.join(5)
            self.assertFalse(worker.is_alive(), "first quantum did not finish")
        first = self.app.goals.store.get(goal["id"])
        self.assertEqual(len(formal), 4, first)
        self.assertGreater(compaction.estimate_messages_tokens(first["checkpoint"]["messages"]), 7147,
                           "fixture must create an actually oversized checkpoint")
        result = self.drive(goal["id"], timeout=15)
        rounds = self.rounds(goal["id"])
        evidence = {"status": result["status"], "reason": result["reason"], "runs": len(rounds),
                    "formal_sends": len(formal), "summary_sends": len(hidden),
                    "compaction_generation": result["checkpoint"].get("compaction_generation", 0)}
        self.assertEqual(result["status"], "completed", evidence)
        self.assertGreaterEqual(len(hidden), 1, evidence)
        self.assertGreaterEqual(result["checkpoint"].get("compaction_generation", 0), 1, evidence)
        self.assertLessEqual(len(rounds), 3, evidence)
        self.assertEqual(len(formal), 5, evidence)
        self.assertEqual(result["model_requests"], len(formal) + len(hidden))
        self.assertEqual(result["tool_actions"], 4)
        self.assertEqual((self.workspace / "result.txt").read_text(), "verified-once")
        with self.app.storage._connect() as db:
            actions = db.execute("SELECT COUNT(*) FROM chat_goal_actions WHERE goal_id=?", (goal["id"],)).fetchone()[0]
        self.assertEqual(actions, 1, "recovery repeated the completed write")
        self.assertEqual(sum(m["role"] == "user" for m in self.app.storage.get_conversation(self.cid)["messages"]), 1)

    def test_slow_goal_a_does_not_starve_goal_b_cancellation(self):
        entered, release = threading.Event(), threading.Event()

        def slow(body):
            entered.set()
            if not release.wait(10):
                raise AssertionError("slow transport was not released during cleanup")
            return self.text("交付完成。\nGOAL: completed")

        self.reply = slow
        first = self.queued()
        try:
            cid = self.app.storage.create_conversation("Goal B cancellation fixture",
                model_key="online:goal-fixture", model_name="goal-fixture", permission_mode="full",
                workspace_dir=str(self.workspace), reasoning_effort="off")["id"]
            self.app.storage.set_enabled_tool_ids(cid, ["read_file", "write_file", "edit_file", "reset_context"])
            self.app.storage.set_conversation_chat_supports_images(cid, False)
            self.app.storage.update_conversation_settings(cid, stream_enabled=False)
            with patch.object(self.app.goals, "start"):
                second = self.app.goals.submit({"conversation_id": cid, "message": "独立目标 B",
                                               "request_id": "second"})
                self.app.goals.control(second["id"], "cancel", second["version"])
            self.app.goals._tick(first["id"])
            self.assertTrue(entered.wait(5), "goal A never entered transport")
            self.assertEqual(self.app.goals.store.pending()[0][0], first["id"],
                             "fixture must put slow A before cancelling B")
            # Exercise the real scheduler, not a direct _tick(B) that bypasses
            # the starvation bug. Bound scans rather than waiting for watchdogs.
            scans = 0
            def next_scan(*args, **kwargs):
                nonlocal scans
                scans += 1
                if scans > 4:
                    self.app.goals._stop.set()
                return True

            with patch.object(self.app.goals._wake, "wait", side_effect=next_scan):
                self.app.goals._loop()
            row = self.app.goals.store.get(second["id"])
            self.assertEqual(row["status"], "cancelled", {
                "first_goal": self.app.goals.store.get(first["id"])["status"],
                "second_goal": row["status"], "second_run": self.app.runs.get(second["run_id"])["status"],
                "scheduler_scans": scans - 1, "model_opens": len(self.wire)})
            self.assertFalse(release.is_set(), "B must settle while A remains blocked")
            self.assertEqual(len(self.wire), 1)
            self.assertNotIn(self.app.runs.get(second["run_id"])["status"], self.app.runs.ACTIVE)
            self.assertIsNone(self.app.storage.active_run(cid))
        finally:
            release.set()
            with self.app.runs._lock:
                worker = self.app.runs._threads.get(first["run_id"])
            if worker:
                worker.join(5)
                self.assertFalse(worker.is_alive(), "goal A worker leaked from test")

    def test_pause_and_cancel_inflight_prevent_late_tool_side_effects(self):
        for action, terminal in (("pause", "paused"), ("cancel", "cancelled")):
            with self.subTest(action=action):
                entered, release = threading.Event(), threading.Event()
                def late_tool(body):
                    entered.set()
                    self.assertTrue(release.wait(5))
                    return self.call("write_file", {"path": f"late-{action}.txt", "content": "forbidden"}, "late")
                self.reply = late_tool
                goal = self.queued(request_id=action)
                try:
                    self.app.goals._tick(goal["id"])
                    self.assertTrue(entered.wait(5))
                    row = self.app.goals.store.get(goal["id"])
                    with patch.object(self.app.goals, "start"):
                        self.app.goals.control(goal["id"], action, row["version"])
                finally:
                    release.set()
                result = self.drive(goal["id"])
                self.assertEqual(result["status"], terminal, result)
                self.assertFalse((self.workspace / f"late-{action}.txt").exists())
                self.assertEqual(result["tool_actions"], 0)
                self.assertEqual(len(self.rounds(goal["id"])), 1)
                if terminal == "paused":
                    with patch.object(self.app.goals, "start"):
                        self.app.goals.control(goal["id"], "cancel", result["version"])
                    self.drive(goal["id"])

    def test_pause_resume_retains_goal_and_original_input(self):
        goal = self.queued()
        with patch.object(self.app.goals, "start"):
            self.app.goals.control(goal["id"], "pause", goal["version"])
        paused = self.drive(goal["id"])
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(len(self.wire), 0)
        run = self.app.runs.get(goal["run_id"])
        self.assertNotIn(run["status"], self.app.runs.ACTIVE,
            f"paused GOAL still holds active original Run: goal={paused['status']} run={run['status']}")
        self.assertIsNone(self.app.storage.active_run(self.cid))
        self.reply = lambda body: self.text("交付完成。\nGOAL: completed")
        with patch.object(self.app.goals, "start"):
            self.app.goals.control(goal["id"], "resume", paused["version"])
        result = self.drive(goal["id"])
        self.assertEqual(result["status"], "awaiting_review", result)
        self.assertEqual(result["id"], goal["id"])
        self.assertEqual(len(self.wire), 1)
        self.assertEqual(sum(m["role"] == "user" for m in self.app.storage.get_conversation(self.cid)["messages"]), 1)
        self.assertEqual(len(self.rounds(goal["id"])), 2)

    def test_queued_cancel_survives_reinstantiation_without_dispatch(self):
        goal = self.queued()
        with patch.object(self.app.goals, "start"):
            self.app.goals.control(goal["id"], "cancel", goal["version"])
        cancelled = self.drive(goal["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.restart()
        self.app.goals._tick(goal["id"])
        self.assertEqual(self.app.goals.store.get(goal["id"])["status"], "cancelled")
        self.assertEqual(len(self.wire), 0)
        with self.assertRaises(GoalConflict):
            self.app.goals.control(goal["id"], "resume", cancelled["version"])

    def test_concurrent_controls_same_version_only_one_wins(self):
        goal = self.queued()
        barrier = threading.Barrier(2)
        def control(action):
            barrier.wait(5)
            try:
                return self.app.goals.control(goal["id"], action, goal["version"])["status"]
            except GoalConflict:
                return "conflict"
        with patch.object(self.app.goals, "start"), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(control, ("pause", "cancel")))
        self.assertEqual(results.count("conflict"), 1, results)
        row = self.drive(goal["id"])
        self.assertIn(row["status"], {"paused", "cancelled"})
        self.assertEqual(len(self.wire), 0)
        self.assertFalse(self.app.goals.store.transition(goal, "queued"), "stale version resurrected goal")

    def test_claim_before_start_crash_recovers_on_new_app(self):
        goal = self.queued()
        self.assertTrue(self.app.goals.store.claim(goal["id"], goal["run_id"]))
        self.assertFalse(self.app.goals.store.claim(goal["id"], goal["run_id"]))
        self.assertEqual(len(self.wire), 0)
        self.restart()
        result = self.drive(goal["id"])
        self.assertEqual(result["status"], "awaiting_review", result)
        self.assertEqual(result["model_requests"], 1)
        rounds = self.rounds(goal["id"])
        self.assertEqual([r["generation"] for r in rounds], list(range(1, len(rounds) + 1)))
        self.assertEqual(len({r["run_id"] for r in rounds}), len(rounds))
        self.assertEqual(len(self.wire), 1)
        messages = self.app.storage.get_conversation(self.cid)["messages"]
        self.assertEqual(sum(m["role"] == "user" for m in messages), 1)
        self.assertIsNone(self.app.storage.active_run(self.cid))

    def test_reinstantiate_queued_checkpoint_preserves_prefix_without_new_user(self):
        self.enterContext(patch.object(goal_runtime, "_QUANTUM", 2))
        self.reply = lambda body: self.call("write_file", {"path": "result.txt", "content": "once"}, "write")
        goal = self.queued(criteria=[{"path": "result.txt", "contains": "once"}])
        self.app.goals._tick(goal["id"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.app.runs._lock:
                live = bool(self.app.runs._threads)
            if not live:
                break
            time.sleep(.005)
        self.app.goals._tick(goal["id"])
        before = self.app.goals.store.get(goal["id"])
        self.assertEqual(before["status"], "queued", before)
        checkpoint = before["checkpoint"]["messages"]
        self.assertTrue((self.workspace / "result.txt").exists())
        self.reply = lambda body: self.text("复核完成。\nGOAL: completed")
        self.restart()
        result = self.drive(goal["id"])
        self.assertEqual(result["status"], "completed", result)
        from naiba.llm.runtime import ModelRuntime
        self.assertEqual(self.wire[-1]["messages"][:len(checkpoint)], ModelRuntime._openai_messages(checkpoint))
        self.assertEqual(result["tool_actions"], 1)
        self.assertEqual((self.workspace / "result.txt").read_text(), "once")
        self.assertEqual(sum(m["role"] == "user" for m in self.app.storage.get_conversation(self.cid)["messages"]), 1)

    def test_write_without_full_checkpoint_is_not_replayed_after_restart(self):
        for receipt_state in ("pending", "confirmed"):
            with self.subTest(receipt_state=receipt_state):
                goal = self.queued(request_id=receipt_state)
                self.assertTrue(self.app.goals.store.claim(goal["id"], goal["run_id"]))
                snapshot = self.app.storage.get_run_snapshot(goal["run_id"])
                cycle = self.app.goals.cycle(goal["id"], goal["run_id"], snapshot)
                path = self.workspace / f"written-{receipt_state}.txt"
                if receipt_state == "pending":
                    with patch.object(cycle.service.store, "finish_action", side_effect=OSError("crash after fsync")):
                        with self.assertRaisesRegex(OSError, "crash after fsync"):
                            cycle._write("write_file", path, {"content": "durable-once"}, {})
                else:
                    self.assertTrue(cycle._write("write_file", path, {"content": "durable-once"}, {})[0])
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                self.assertEqual(len(self.app.goals.store.pending_actions(goal["id"])), 1)
                self.restart()
                result = self.drive(goal["id"])
                self.assertEqual(result["status"], "needs_user", result)
                self.assertEqual(len(self.wire), 0, "unknown write completion must not send another model request")
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
                with patch.object(self.app.goals, "start"):
                    with self.assertRaises(GoalConflict):
                        self.app.goals.control(goal["id"], "resume", result["version"])
                    self.app.goals.control(goal["id"], "cancel", result["version"])
                self.drive(goal["id"])

    def test_pause_between_write_receipt_and_commit_prevents_mutation(self):
        goal = self.queued()
        self.app.goals.store.claim(goal["id"], goal["run_id"])
        snapshot = self.app.storage.get_run_snapshot(goal["run_id"])
        cycle = self.app.goals.cycle(goal["id"], goal["run_id"], snapshot)
        original_start = self.app.goals.store.start_action

        def pause_after_receipt(*args, **kwargs):
            original_start(*args, **kwargs)
            current = self.app.goals.store.get(goal["id"])
            self.assertTrue(self.app.goals.store.transition(current, "paused"))

        path = self.workspace / "must-not-commit.txt"
        with patch.object(self.app.goals.store, "start_action", side_effect=pause_after_receipt):
            success, reason = cycle._write("write_file", path, {"content": "forbidden"}, {})
        self.assertFalse(success, reason)
        self.assertFalse(path.exists())
        self.assertEqual(len(self.app.goals.store.pending_actions(goal["id"])), 1)
        self.assertEqual(len(self.wire), 0)

    def test_changed_file_after_incomplete_write_is_never_overwritten(self):
        goal = self.queued()
        self.app.goals.store.claim(goal["id"], goal["run_id"])
        cycle = self.app.goals.cycle(goal["id"], goal["run_id"], self.app.storage.get_run_snapshot(goal["run_id"]))
        path = self.workspace / "result.txt"
        cycle._write("write_file", path, {"content": "original"}, {})
        path.write_text("user-edit", encoding="utf-8")
        self.restart()
        result = self.drive(goal["id"])
        self.assertEqual(result["status"], "needs_user", result)
        self.assertEqual(path.read_text(), "user-edit")
        self.assertEqual(len(self.wire), 0)

    def test_model_quota_counts_actual_sends_and_rejection_never_retries(self):
        self.enterContext(patch.object(goal_runtime, "_MAX_MODEL_REQUESTS", 1))
        self.reply = lambda body: self.call("read_file", {"path": "source.txt"}, "read")
        (self.workspace / "source.txt").write_text("source", encoding="utf-8")
        goal = self.queued()
        result = self.drive(goal["id"])
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["model_requests"], 1)
        self.assertEqual(len(self.wire), 1, "quota refusal was retried or dispatched")
        with self.assertRaises(GoalConflict):
            self.app.goals.control(goal["id"], "resume", result["version"])

    def test_zero_model_quota_rejects_first_send(self):
        self.enterContext(patch.object(goal_runtime, "_MAX_MODEL_REQUESTS", 0))
        goal = self.queued()
        result = self.drive(goal["id"])
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["model_requests"], 0)
        self.assertEqual(result["tool_actions"], 0)
        self.assertEqual(len(self.wire), 0)

    def test_transport_retry_is_charged_and_second_send_is_rejected(self):
        from tests.test_llm_runtime_retry import http_error

        self.enterContext(patch.object(goal_runtime, "_MAX_MODEL_REQUESTS", 1))
        def fail_once(body):
            raise http_error(503, "synthetic transient GOAL transport failure")
        self.reply = fail_once
        goal = self.queued()
        result = self.drive(goal["id"])
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["model_requests"], 1)
        self.assertEqual(len(self.wire), 1, "transport retry bypassed quota gate")
        self.assertEqual(result["tool_actions"], 0)
        self.assertEqual(len(self.rounds(goal["id"])), 1)

    def test_tool_quota_stops_before_second_side_effect(self):
        self.enterContext(patch.object(goal_runtime, "_MAX_TOOL_ACTIONS", 1))
        self.reply = lambda body: self.call("write_file", {"path": f"file-{len(self.wire)}.txt", "content": "one"}, str(len(self.wire)))
        goal = self.queued()
        result = self.drive(goal["id"])
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["tool_actions"], 1)
        self.assertEqual(sorted(p.name for p in self.workspace.glob("file-*.txt")), ["file-1.txt"])

    def test_atomic_quota_reservations_do_not_overspend(self):
        goal = self.queued()
        self.app.goals.store.claim(goal["id"], goal["run_id"])
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.app.goals.store.reserve(
                goal["id"], goal["run_id"], "model_requests", 1, 3), range(16)))
        self.assertEqual(sum(results), 3)
        self.assertEqual(self.app.goals.store.get(goal["id"])["model_requests"], 3)
        self.assertFalse(self.app.goals.store.reserve(goal["id"], "wrong-owner", "model_requests", 1, 100))

    def test_permission_change_before_dispatch_never_sends(self):
        goal = self.queued()
        self.app.storage.update_conversation_settings(self.cid, permission_mode="confirm")
        result = self.drive(goal["id"])
        self.assertEqual(result["status"], "needs_user", result)
        self.assertEqual(len(self.wire), 0)
        self.assertEqual(result["model_requests"], 0)

    def test_permission_change_after_model_before_tool_blocks_write(self):
        def changed(body):
            self.app.storage.update_conversation_settings(self.cid, permission_mode="confirm")
            return self.call("write_file", {"path": "forbidden.txt", "content": "forbidden"}, "write")
        self.reply = changed
        goal = self.queued()
        result = self.drive(goal["id"])
        self.assertIn(result["status"], {"blocked", "needs_user"}, result)
        self.assertFalse((self.workspace / "forbidden.txt").exists())
        self.assertEqual(result["tool_actions"], 0)
        self.assertEqual(len(self.wire), 1)

    def test_expired_goal_does_not_dispatch_or_resume(self):
        goal = self.queued()
        row = self.app.goals.store.get(goal["id"])
        self.assertTrue(self.app.goals.store.transition(row, "queued", deadline_at=1))
        result = self.drive(goal["id"])
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(len(self.wire), 0)
        self.assertEqual(len(self.rounds(goal["id"])), 1)
        with self.assertRaises(GoalConflict):
            self.app.goals.control(goal["id"], "resume", result["version"])

    def test_paused_stale_owner_cannot_checkpoint_reserve_or_start_action(self):
        goal = self.queued()
        self.assertTrue(self.app.goals.store.claim(goal["id"], goal["run_id"]))
        row = self.app.goals.store.get(goal["id"])
        self.assertTrue(self.app.goals.store.transition(row, "paused"))
        snapshot = self.app.storage.get_run_snapshot(goal["run_id"])
        cycle = goal_runtime.GoalCycle(self.app.goals, row, snapshot)
        self.assertFalse(cycle.before_send())
        with self.assertRaises(GoalConflict):
            cycle.save([{"role": "user", "content": "stale overwrite"}])
        with self.assertRaises(GoalConflict):
            self.app.goals.store.start_action(goal["id"], goal["run_id"], "stale", {
                "path": str(self.workspace / "stale.txt"), "before": "", "after": "hash", "result": "stale"})
        self.assertFalse(self.app.goals.store.reserve(goal["id"], goal["run_id"], "tool_actions", 1, 10))
        self.assertEqual(self.app.goals.store.get(goal["id"])["checkpoint"], {})

    def test_round_limit_prevents_new_run(self):
        self.enterContext(patch.object(goal_runtime, "_MAX_ROUNDS", 1))
        self.enterContext(patch.object(goal_runtime, "_QUANTUM", 2))
        self.reply = lambda body: self.call("write_file", {"path": "once.txt", "content": "once"}, "once")
        goal = self.queued()
        result = self.drive(goal["id"])
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(len(self.rounds(goal["id"])), 1)
        self.assertEqual(result["generation"], 1)
        self.assertEqual(len(self.wire), 1)


class GoalB4PressureTests(unittest.TestCase):
    def setUp(self):
        fixture.GoalTests.setUp(self)
        (self.paths.config_path.parent / "errors").mkdir(exist_ok=True)

    transport = fixture.GoalTests.transport
    text = staticmethod(fixture.GoalTests.text)
    call = staticmethod(fixture.GoalTests.call)
    submit = fixture.GoalTests.submit
    queued = GoalRecoveryTests.queued
    drive = GoalRecoveryTests.drive
    rounds = GoalRecoveryTests.rounds

    def test_b4_one_input_fifteen_real_compactions_across_real_runs(self):
        from naiba.core import compaction
        from naiba.core.trace_view import is_summary_message

        self.enterContext(patch.object(goal_runtime, "_QUANTUM", 8))
        self.app.config.data["auto_compaction"] = {"mode": "auto", "threshold_percent": 30}
        self.app.config.save()
        source = "资料内容与执行状态均应保留。" * 300
        for number in range(64):
            (self.workspace / f"source-{number:03}.txt").write_text(
                f"独立资料编号 {number:03}，本条事实不得重复计为另一阶段。\n" + source, encoding="utf-8")
        formal, hidden, compressions = [], [], []
        original_compact = goal_runtime.GoalCycle.compact

        def observe_compaction(cycle, *args, **kwargs):
            previous = int(cycle.checkpoint.get("compaction_generation") or 0)
            result = original_compact(cycle, *args, **kwargs)
            current = int(cycle.checkpoint.get("compaction_generation") or 0)
            if current != previous:
                durable = cycle.service.store.get(cycle.row["id"])["checkpoint"]
                compressions.append({"run_id": cycle.row["run_id"], "generation": current,
                    "previous": previous, "durable_generation": durable.get("compaction_generation"),
                    **durable["last_compaction"]})
            return result

        self.enterContext(patch.object(goal_runtime.GoalCycle, "compact", observe_compaction))

        def produce(body):
            if any(str(m.get("content") or "").startswith(compaction.SUMMARIZE_INSTRUCTION)
                   for m in body["messages"]):
                hidden.append(body)
                return self.text(GOOD_SUMMARY)
            formal.append(body)
            index = len(formal) - 1
            if index >= 64:
                return self.text("16 份产物已逐个登记并复核。\nGOAL: completed")
            if index % 4 == 0:
                number = index // 4 + 1
                return self.call("write_file", {"path": f"result-{number:02}.txt",
                    "content": f"verified-{number:02}"}, f"write-{number:02}")
            return self.call("read_file", {"path": f"source-{index:03}.txt"}, f"read-{index:03}")

        self.reply = produce
        criteria = [{"path": f"result-{n:02}.txt", "contains": f"verified-{n:02}",
                     "sha256": hashlib.sha256(f"verified-{n:02}".encode()).hexdigest()} for n in range(1, 17)]
        goal = self.queued(message="顺序创建16份独立产物，每份写完读取资料三次复核，全部通过文件验收后完成。", criteria=criteria)
        result = self.drive(goal["id"], timeout=60)
        rounds = self.rounds(goal["id"])
        events = [e for r in rounds for e in self.app.runs._all_run_events(r["run_id"])]
        actual_compactions = [e for e in events if e["type"] == "compaction" and e.get("action") == "compacted"]
        evidence = {"status": result["status"], "reason": result["reason"], "runs": len(rounds),
            "formal_sends": len(formal), "summary_sends": len(hidden), "model_requests": result["model_requests"],
            "tool_actions": result["tool_actions"], "compactions": len(compressions),
            "compaction_run_ids": len({c["run_id"] for c in compressions}),
            "event_compactions": len(actual_compactions), "generation": result["generation"],
            "checkpoint_compaction_generation": result["checkpoint"].get("compaction_generation"),
            "run_states": [{"status": self.app.runs.get(r["run_id"])["status"],
                            "detail": self.app.runs.get(r["run_id"]).get("detail")} for r in rounds],
            "failure_events": [e for e in events if e["type"] in {"error", "run_failed", "context_full"}],
            "checkpoint_outcome": result["checkpoint"].get("outcome")}
        print("B4_EVIDENCE " + json.dumps(evidence, ensure_ascii=False), flush=True)
        self.assertEqual(result["status"], "completed", evidence)
        self.assertGreaterEqual(len(rounds), 15, evidence)
        self.assertGreaterEqual(len(compressions), 15, evidence)
        self.assertGreaterEqual(len({c["run_id"] for c in compressions}), 15, evidence)
        self.assertEqual(len(actual_compactions), len(compressions), evidence)
        self.assertEqual([r["generation"] for r in rounds], list(range(1, len(rounds) + 1)))
        self.assertEqual(len({r["run_id"] for r in rounds}), len(rounds))
        self.assertEqual([c["generation"] for c in compressions], list(range(1, len(compressions) + 1)))
        for item in compressions:
            self.assertEqual(item["generation"], item["durable_generation"])
            self.assertLess(item["after"], item["before"])
            self.assertEqual(sum(a[compaction.NETWORK_SEND_COUNT_KEY] for a in item["attempts"]), 1)
        self.assertEqual(result["model_requests"], len(formal) + len(hidden))
        self.assertEqual(result["tool_actions"], 64)
        self.assertEqual(result["checkpoint"]["compaction_generation"], len(compressions))
        self.assertTrue(all(e["passed"] for e in result["evidence"]))
        self.assertEqual(len(result["evidence"]), 16)
        for criterion in criteria:
            self.assertEqual(hashlib.sha256((self.workspace / criterion["path"]).read_bytes()).hexdigest(), criterion["sha256"])
        self.assertTrue(any(is_summary_message(m) for body in formal for m in body["messages"]))
        self.assertTrue(all("tools" not in body for body in hidden))
        messages = self.app.storage.get_conversation(self.cid)["messages"]
        self.assertEqual(sum(m["role"] == "user" for m in messages), 1)
        with self.app.storage._connect() as db:
            actions = db.execute("SELECT state,receipt FROM chat_goal_actions WHERE goal_id=?", (goal["id"],)).fetchall()
        self.assertEqual(len(actions), 16, "side-effect actions were duplicated across runs")
        self.assertTrue(all(a["state"] == "checkpointed" for a in actions))
        self.assertIsNone(self.app.storage.active_run(self.cid))


_PROCESS_EVENT_PREFIX = "GOAL_PROCESS "


def _goal_recovery_process(root_text, endpoint, scenario, phase):
    """Child entry point: patch crash windows, never patch recovery decisions."""
    from naiba import net as net_io
    from naiba.config import ConfigStore
    from naiba.paths import PathContext
    from naiba.storage.goals import GoalStore
    from naiba.storage.store import ChatStorage

    root = Path(root_text).resolve()
    workspace = root / "workspace"
    target = workspace / "result.txt"
    expected_url = endpoint + "/chat/completions"
    port = urlsplit(endpoint).port
    observations = {"write_calls": 0, "file_mutations": [], "digest_checks": [], "network_denials": []}

    def emit(kind, **payload):
        print(_PROCESS_EVENT_PREFIX + json.dumps({"kind": kind, **payload}, ensure_ascii=False), flush=True)

    def barrier(**payload):
        emit("barrier", **payload)
        threading.Event().wait()  # Only the parent's Popen.kill() releases this process.

    def same_target(raw):
        return isinstance(raw, (str, bytes, os.PathLike)) and Path(os.fsdecode(raw)).resolve() == target

    def audit(event, args):
        if event == "open" and same_target(args[0]):
            mode, flags = args[1:3]
            if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            ):
                observations["file_mutations"].append(event)
        elif event in {"os.link", "os.rename", "os.remove"} and any(same_target(p) for p in args[:2]):
            observations["file_mutations"].append(event)

    sys.addaudithook(audit)
    original_open = net_io.open
    original_connect = socket.socket.connect
    original_getaddrinfo = socket.getaddrinfo
    original_write = goal_runtime.GoalCycle._write
    original_digest = goal_runtime._file_digest
    original_create = ChatStorage.create_run
    original_finish = GoalStore.finish_action

    def deny_network(value):
        observations["network_denials"].append(str(value))
        raise AssertionError(f"GOAL process fixture refuses non-fake network: {value}")

    def local_open(request, *args, **kwargs):
        if getattr(request, "full_url", request) != expected_url:
            deny_network(getattr(request, "full_url", request))
        return original_open(request, *args, **kwargs)

    def local_connect(sock, address):
        if address != ("127.0.0.1", port):
            deny_network(address)
        return original_connect(sock, address)

    def local_getaddrinfo(host, service, *args, **kwargs):
        if host != "127.0.0.1" or int(service) != port:
            deny_network((host, service))
        return original_getaddrinfo(host, service, *args, **kwargs)

    def observe_write(cycle, *args, **kwargs):
        observations["write_calls"] += 1
        return original_write(cycle, *args, **kwargs)

    def observe_digest(path):
        value = original_digest(path)
        if same_target(path):
            observations["digest_checks"].append(value)
        return value

    def create_then_block(storage, *args, **kwargs):
        run = original_create(storage, *args, **kwargs)
        if phase == "crash" and scenario == "next_round" and kwargs.get("goal_bind") is not None:
            # create_run has returned, so bind_next AND the new Run are committed.
            barrier(run_id=run["id"], window="next_round")
        return run

    def finish_then_block(store, goal_id, action_id):
        if phase == "crash" and scenario == "pending":
            barrier(goal_id=goal_id, action_id=action_id, window="pending")
        original_finish(store, goal_id, action_id)
        if phase == "crash" and scenario == "confirmed":
            barrier(goal_id=goal_id, action_id=action_id, window="confirmed")

    app = None
    with ExitStack() as stack:
        stack.enter_context(patch.object(net_io, "open", local_open))
        stack.enter_context(patch.object(socket.socket, "connect", local_connect))
        stack.enter_context(patch.object(socket, "getaddrinfo", local_getaddrinfo))
        stack.enter_context(patch.object(goal_runtime, "_QUANTUM", 2))
        stack.enter_context(patch.object(goal_runtime.GoalCycle, "_write", observe_write))
        stack.enter_context(patch.object(goal_runtime, "_file_digest", observe_digest))
        stack.enter_context(patch.object(ChatStorage, "create_run", create_then_block))
        stack.enter_context(patch.object(GoalStore, "finish_action", finish_then_block))
        paths = PathContext.local(root, root / "config.json")
        if phase == "crash":
            workspace.mkdir()
            (root / "errors").mkdir()
            config = ConfigStore(paths.config_path, paths=paths)
            config.data.update({"data_dir": str(root / "data"), "workspace_dir": str(workspace),
                "mcp_servers": [], "onebot": {"enabled": False}, "auto_compaction": {"mode": "off"},
                "proxy": {"enabled": False, "url": "", "use_system_fallback": False},
                "providers": [{"id": "goal-fixture", "kind": "online", "name": "Process fixture",
                    "api_key": "", "base_url": endpoint, "request_format": "openai_chat",
                    "model": "goal-fixture", "context_window": 32000, "max_output_tokens": 2048}]})
            config.save()
        try:
            app = NaibaChatApp(paths=paths)  # Restart must auto-start pending GOALs here.
            if phase == "crash":
                cid = app.storage.create_conversation("Process recovery fixture", model_key="online:goal-fixture",
                    model_name="goal-fixture", permission_mode="full", workspace_dir=str(workspace),
                    reasoning_effort="off")["id"]
                app.storage.set_enabled_tool_ids(cid, ["read_file", "write_file", "edit_file", "reset_context"])
                app.storage.set_conversation_chat_supports_images(cid, False)
                app.storage.update_conversation_settings(cid, stream_enabled=False)
                app.goals.submit({"conversation_id": cid, "message": "Create result.txt once, then verify completion.",
                    "request_id": "external-process-once", "criteria": [{"path": "result.txt",
                        "contains": "verified-once", "sha256": hashlib.sha256(b"verified-once").hexdigest()}]})
                threading.Event().wait()
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                with app.storage._connect() as db:
                    row = dict(db.execute("SELECT * FROM chat_goals").fetchone())
                if row["status"] not in {"queued", "running", "pausing", "cancelling"}:
                    break
                time.sleep(.02)
            else:
                raise AssertionError(f"Restart did not settle: {row}")
            # Leave the real scheduler running for multiple scans; catch late sends/writes.
            time.sleep(.7)
            resume_rejected = False
            if scenario != "next_round":
                current = app.goals.store.get(row["id"])
                try:
                    app.goals.control(row["id"], "resume", current["version"])
                except GoalConflict:
                    resume_rejected = True
            emit("settled", observations=observations, resume_rejected=resume_rejected,
                 module_path=str(Path(goal_runtime.__file__).resolve()), pid=os.getpid())
        finally:
            if app is not None:
                app.stop()


class GoalExternalProcessRecoveryTests(unittest.TestCase):
    """OS-level kill/restart against real SQLite and a loopback HTTP model."""

    def setUp(self):
        self.repo = Path(__file__).resolve().parents[1]
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba-goal-process-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.requests = []
        self.http_errors = []
        self.http_lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with owner.http_lock:
                    owner.requests.append(body)
                    number = len(owner.requests)
                    if self.path != "/v1/chat/completions" or number > 2:
                        owner.http_errors.append({"path": self.path, "number": number})
                response = (fixture.GoalTests.call("write_file", {"path": "result.txt", "content": "verified-once"},
                                                  "external-write-once") if number == 1 else
                            fixture.GoalTests.text("Verified.\nGOAL: completed"))
                data = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        self.addCleanup(self.close_http)
        self.endpoint = f"http://127.0.0.1:{self.http.server_port}/v1"

    def close_http(self):
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join(5)
        self.assertFalse(self.http_thread.is_alive(), "fake HTTP server leaked")

    def launch(self, root, scenario, phase):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(self.repo), str(self.repo / "tests"))),
                   PYTHONUTF8="1", PYTHONIOENCODING="utf-8", NAIBA_ERROR_DUMP_DIR=str(root / "errors"))
        command = "from tests.test_goal_recovery import _goal_recovery_process; import sys; _goal_recovery_process(*sys.argv[1:])"
        process = subprocess.Popen([sys.executable, "-B", "-u", "-c", command, str(root), self.endpoint, scenario, phase],
            cwd=self.repo, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8")
        events, output = queue.Queue(), []

        def read_output():
            try:
                for line in process.stdout:
                    output.append(line.rstrip())
                    if line.startswith(_PROCESS_EVENT_PREFIX):
                        events.put(json.loads(line[len(_PROCESS_EVENT_PREFIX):]))
            finally:
                events.put({"kind": "eof"})

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            reader.join(5)
            process.stdout.close()
            self.assertFalse(reader.is_alive(), "child output reader leaked")

        self.addCleanup(cleanup)
        return process, events, output

    def event(self, process, events, output, expected):
        try:
            event = events.get(timeout=40)
        except queue.Empty:
            self.fail(f"Child timed out waiting for {expected}, pid={process.pid}: {output}")
        self.assertEqual(event["kind"], expected, "\n".join(output))
        return event

    def snapshot(self, root):
        # Never construct ChatStorage here: its initializer mutates interrupted Runs.
        connection = sqlite3.connect((root / "data" / "chat.db").as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                goal = dict(connection.execute("SELECT * FROM chat_goals").fetchone())
                rows = lambda sql: [dict(row) for row in connection.execute(sql)]
                return {"goal": goal, "rounds": rows("SELECT * FROM chat_goal_rounds ORDER BY generation"),
                    "actions": rows("SELECT * FROM chat_goal_actions"),
                    "users": rows("SELECT id,role,content FROM messages WHERE role='user'"),
                    "runs": rows("SELECT id,status,input_message_id,origin_run_id FROM background_tasks"),
                    "started": rows("SELECT run_id FROM run_events WHERE event_type='run_started'")}
        finally:
            connection.close()

    def kill_at_barrier(self, root, scenario):
        process, events, output = self.launch(root, scenario, "crash")
        event = self.event(process, events, output, "barrier")
        self.assertEqual(event["window"], scenario)
        before = self.snapshot(root)
        self.assertEqual(len(self.requests), 1, output)
        self.assertEqual(len(before["users"]), 1)
        self.assertEqual(before["users"][0]["id"], before["goal"]["input_message_id"])
        self.assertEqual((before["goal"]["model_requests"], before["goal"]["tool_actions"]), (1, 1))
        self.assertEqual(len(before["actions"]), 1)
        target = root / "workspace" / "result.txt"
        self.assertEqual(target.read_bytes(), b"verified-once")
        receipt = json.loads(before["actions"][0]["receipt"])
        self.assertEqual(Path(receipt["path"]), target)
        self.assertEqual(receipt["after"], hashlib.sha256(target.read_bytes()).hexdigest())
        before["file_stat"] = target.stat()
        self.assertIsNone(process.poll(), output)
        process.kill()  # TerminateProcess on Windows, SIGKILL on POSIX; no graceful app.stop().
        process.wait(timeout=10)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(self.snapshot(root), {k: v for k, v in before.items() if k != "file_stat"},
                         "killed child unexpectedly settled its Run")
        return before, process.pid

    def restart(self, root, scenario, before, killed_pid):
        process, events, output = self.launch(root, scenario, "restart")
        report = self.event(process, events, output, "settled")
        process.wait(timeout=15)
        self.assertEqual(process.returncode, 0, "\n".join(output))
        self.assertNotEqual(report["pid"], killed_pid)
        self.assertEqual(Path(report["module_path"]), self.repo / "naiba" / "run" / "goals.py")
        after = self.snapshot(root)
        self.assertEqual(after["users"], before["users"])
        for key in ("id", "input_message_id", "request_id", "request_hash", "deadline_at"):
            self.assertEqual(after["goal"][key], before["goal"][key], key)
        self.assertTrue(all(run["input_message_id"] == before["goal"]["input_message_id"] for run in after["runs"]))
        self.assertFalse(any(run["status"] in {"queued", "running", "waiting", "stopping", "cancelling"}
                             for run in after["runs"]))
        observations = report["observations"]
        self.assertEqual(observations["write_calls"], 0, report)
        self.assertEqual(observations["file_mutations"], [], report)
        self.assertEqual(observations["network_denials"], [], report)
        self.assertEqual(self.http_errors, [])
        target = root / "workspace" / "result.txt"
        self.assertEqual(target.read_bytes(), b"verified-once")
        self.assertEqual(target.stat().st_mtime_ns, before["file_stat"].st_mtime_ns)
        return after, report

    def test_next_round_committed_before_dispatch_recovers_without_duplicate_input(self):
        before, killed_pid = self.kill_at_barrier(self.root, "next_round")
        row = before["goal"]
        self.assertEqual((row["status"], row["generation"]), ("queued", 2))
        self.assertEqual([r["generation"] for r in before["rounds"]], [1, 2])
        self.assertEqual(before["actions"][0]["state"], "checkpointed")
        cp = json.loads(row["checkpoint"])
        self.assertEqual(cp["outcome"], "continue")
        self.assertEqual(cp["owner_run_id"], before["rounds"][0]["run_id"])
        self.assertTrue(any(m["role"] == "tool" for m in cp["messages"]))
        self.assertEqual(next(r for r in before["runs"] if r["id"] == row["run_id"])["status"], "queued")
        self.assertNotIn(row["run_id"], [r["run_id"] for r in before["started"]])
        after, report = self.restart(self.root, "next_round", before, killed_pid)
        self.assertEqual(after["goal"]["status"], "completed", after)
        self.assertEqual((after["goal"]["model_requests"], after["goal"]["tool_actions"]), (2, 1))
        self.assertEqual(len(self.requests), 2)
        from naiba.llm.runtime import ModelRuntime
        self.assertEqual(self.requests[1]["messages"], ModelRuntime._openai_messages(cp["messages"]))
        self.assertEqual(self.requests[1]["tools"], self.requests[0]["tools"])
        self.assertEqual(after["actions"], before["actions"])
        self.assertTrue(all(e["passed"] for e in json.loads(after["goal"]["evidence"])))
        self.assertEqual([r["generation"] for r in after["rounds"]], [1, 2, 3])
        self.assertEqual(len({r["run_id"] for r in after["rounds"]}), 3)
        self.assertEqual(next(r for r in after["runs"] if r["id"] == row["run_id"])["status"], "interrupted")
        self.assertNotIn(row["run_id"], [r["run_id"] for r in after["started"]])
        self.assertEqual(after["goal"]["generation"], 3)
        self.assertEqual([r for r in after["runs"] if r["id"] == after["goal"]["run_id"]][0]["origin_run_id"], row["run_id"])

    def test_incomplete_write_receipt_is_verified_and_stopped_without_rewrite(self):
        for state in ("pending", "confirmed"):
            with self.subTest(receipt_state=state):
                root = self.root / state
                root.mkdir()
                with self.http_lock:
                    self.requests.clear()
                before, killed_pid = self.kill_at_barrier(root, state)
                self.assertEqual((before["goal"]["status"], before["goal"]["generation"]), ("running", 1))
                self.assertEqual(before["actions"][0]["state"], state)
                cp = json.loads(before["goal"]["checkpoint"])
                self.assertNotIn(cp.get("outcome"), {"completed", "review"})
                self.assertFalse(any(m["role"] == "tool" for m in cp["messages"]))
                after, report = self.restart(root, state, before, killed_pid)
                self.assertEqual(after["goal"]["status"], "needs_user", after)
                self.assertEqual(after["goal"]["reason"], "上一次写入已核对，缺少完整工具结果，未自动重做")
                self.assertEqual((after["goal"]["model_requests"], after["goal"]["tool_actions"]), (1, 1))
                self.assertEqual(len(self.requests), 1, "restart dispatched another model request")
                self.assertEqual(after["goal"]["generation"], 1)
                self.assertEqual(after["goal"]["checkpoint"], before["goal"]["checkpoint"])
                self.assertEqual(len(after["rounds"]), 1)
                self.assertEqual(after["actions"], [{**before["actions"][0], "state": "confirmed"}])
                self.assertIn(hashlib.sha256(b"verified-once").hexdigest(), report["observations"]["digest_checks"])
                self.assertTrue(report["resume_rejected"], report)


if __name__ == "__main__":
    unittest.main()
