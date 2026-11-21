"""GOAL v34 migration and storage boundaries, using only isolated SQLite files."""
from __future__ import annotations

import base64
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from naiba.core.contracts import GOAL_DEFAULT_LIMITS, GOAL_MAX_LIMITS
from naiba.storage.goals import (GoalConflict, GoalStore, goal_json, goal_now, insert_goal,
                                 migrate_goal_extensions, migrate_goals)
from naiba.storage import store as storage_module
from naiba.storage.store import ChatStorage


class IsolatedStorage:
    def __init__(self, path):
        self.db_path = path
        self.statements = []

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.set_trace_callback(self.statements.append)
        try:
            with db:
                yield db
        finally:
            db.close()


class GoalExtensionsMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="goal-v34-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "chat.db"
        # A real v33 database, including conversations/messages/background tasks.
        with patch.object(storage_module, "CURRENT_SCHEMA_VERSION", 33):
            self.storage = ChatStorage(self.path)
        self.assertEqual(self.storage.get_user_version(), 33)
        self.goals = GoalStore(self.storage)
        self.now = goal_now()
        self.cids = []
        for index, criteria in enumerate(([], [{"path": "result.txt"}])):
            conv = self.storage.create_conversation(title=f"old-{index}")
            message = self.storage.add_message(conv["id"], "user", "old input")
            run = self.storage.create_run(conv["id"], "old input", {"id": "master"}, {})
            self.cids.append(conv["id"])
            with self.storage._connect() as db:
                insert_goal(db, conv["id"], message["id"], run["id"], {
                    "id": f"old-{index}", "request_id": "old", "request_hash": "hash",
                    "message": "old input", "duration_ms": 10800000, "criteria": criteria}, self.now)
                db.execute("UPDATE chat_goals SET model_requests=71,tool_actions=19,version=8,"
                           "status='paused',checkpoint=?,evidence=? WHERE id=?",
                           ('{"messages":["private"]}', '[{"path":"result.txt","passed":true,"raw":"secret"}]', f"old-{index}"))
                db.execute("INSERT INTO chat_goal_actions VALUES (?,?,?,?,?)",
                           (f"old-{index}", "write", run["id"], "confirmed", '{"after":"hash"}'))
        self.history = self.snapshot(("conversations", "messages", "background_tasks", "chat_goal_actions"))

    def snapshot(self, tables):
        with self.storage._connect() as db:
            return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in tables}

    def upgrade(self, migration=migrate_goal_extensions):
        with patch.object(storage_module, "CURRENT_SCHEMA_VERSION", 34), patch.dict(
                storage_module.MIGRATIONS, {34: migration}):
            self.storage.apply_pending_migrations()

    def test_real_v33_upgrade_preserves_usage_deadline_identity_and_history(self):
        old = self.snapshot(("chat_goals", "chat_goal_rounds"))
        self.upgrade()
        self.assertEqual(self.storage.get_user_version(), 34)
        self.assertEqual(self.snapshot(tuple(self.history)), self.history)
        for index, expected in enumerate(("manual", "machine_all")):
            row = self.goals.get(f"old-{index}")
            self.assertEqual(row["completion_policy"], expected)
            self.assertEqual(row["limits"], GOAL_DEFAULT_LIMITS)
            self.assertEqual((row["model_requests"], row["tool_actions"], row["version"]), (71, 19, 8))
            self.assertEqual(row["deadline_at"], self.now + 10800000)
            self.assertEqual(row["checkpoint"], {"messages": ["private"]})
            self.assertEqual((row["sources"], row["stages"], row["manual_items"], row["wait_binding"]), ([], [], [], {}))
            round_row = self.goals.rounds(row["id"])["rounds"][0]
            self.assertIsNone(round_row["model_requests_start"], "unknown v33 samples must not become zero")
            self.assertEqual(round_row["model_requests_end"], 71)
            self.assertEqual(round_row["evidence"], [{"path": "result.txt", "passed": True}])
        with self.storage._connect() as db:
            cols = [row[1] for row in db.execute("PRAGMA table_info(chat_goals)")]
            for index, source in enumerate(old["chat_goals"]):
                actual = tuple(db.execute("SELECT * FROM chat_goals WHERE id=?", (f"old-{index}",)).fetchone())
                self.assertEqual(actual[:len(source)], source)
            self.assertIn("limits", cols)
        self.assertTrue(self.storage.check_integrity()["ok"])

    def test_repeated_migration_preserves_extension_state_and_budget_receipt(self):
        self.upgrade()
        row = self.goals.extend("old-0", 8, "append", {"rounds": 1}, 10)
        self.goals.transition(row, "paused", stages=[{"id": "s", "status": "done"}], stop_code="budget_rounds")
        tables = ("chat_goals", "chat_goal_rounds", "chat_goal_actions", "chat_goal_budget_receipts")
        before = self.snapshot(tables)
        with self.storage._connect() as db:
            migrate_goal_extensions(db)
            migrate_goal_extensions(db)
        self.upgrade()
        self.assertEqual(self.snapshot(tables), before)

    def test_failure_after_ddl_rolls_back_schema_version_and_rows_then_retries(self):
        before = self.snapshot(("chat_goals", "chat_goal_rounds"))
        with self.storage._connect() as db:
            schema = [tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master ORDER BY name")]

        def fail(db):
            migrate_goal_extensions(db)
            raise RuntimeError("v34 interrupted")

        with self.assertRaisesRegex(RuntimeError, "v34 interrupted"):
            self.upgrade(fail)
        self.assertEqual(self.storage.get_user_version(), 33)
        self.assertEqual(self.snapshot(tuple(before)), before)
        self.assertEqual(self.snapshot(tuple(self.history)), self.history)
        with self.storage._connect() as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master ORDER BY name")], schema)
        self.upgrade()
        self.assertEqual(self.storage.get_user_version(), 34)

    def test_internal_failure_rolls_back_ddl_even_without_outer_begin(self):
        with self.storage._connect() as db:
            def deny_index(action, arg1, arg2, database, source):
                return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_INDEX else sqlite3.SQLITE_OK
            db.set_authorizer(deny_index)
            with self.assertRaises(sqlite3.DatabaseError):
                migrate_goal_extensions(db)
            db.set_authorizer(None)
            self.assertNotIn("limits", {row[1] for row in db.execute("PRAGMA table_info(chat_goals)")})
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='chat_goal_budget_receipts'").fetchone())
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 33)
        self.upgrade()


class GoalStoreExtensionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="goal-store-")
        self.addCleanup(self.tmp.cleanup)
        self.storage = IsolatedStorage(Path(self.tmp.name) / "chat.db")
        self.store = GoalStore(self.storage)
        self.now = goal_now()
        with self.storage._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE conversations(id TEXT PRIMARY KEY)")
            migrate_goals(db)
            migrate_goal_extensions(db)
        self.create()

    def create(self, goal_id="goal", **extra):
        with self.storage._connect() as db:
            db.execute("INSERT INTO conversations VALUES (?)", (goal_id,))
            insert_goal(db, goal_id, "message", f"run-{goal_id}", {
                "id": goal_id, "request_id": "first", "request_hash": "hash", "message": "goal",
                "duration_ms": 10800000, "criteria": [], **extra}, self.now)
        return self.store.get(goal_id)

    def claim(self):
        self.assertTrue(self.store.claim("goal", "run-goal"))
        return self.store.get("goal")

    def receipts(self):
        with self.storage._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM chat_goal_budget_receipts")]

    def test_insert_old_and_new_spec_decode_every_json_field(self):
        row = self.store.get("goal")
        self.assertEqual(row["limits"], GOAL_DEFAULT_LIMITS)
        self.assertEqual(row["completion_policy"], "manual")
        row = self.create("new", criteria=[{"path": "done.txt"}], sources=[{"path": "plan.md", "sha256": "hash"}],
            stages=[{"id": "s1"}], manual_items=[{"label": "review"}], completion_policy="mixed",
            limits={"model_requests": 12, "tool_actions": 9, "rounds": 3})
        self.assertEqual(row["completion_policy"], "mixed")
        self.assertEqual(row["limits"], {"model_requests": 12, "tool_actions": 9, "rounds": 3})
        for key in ("sources", "stages", "manual_items", "criteria"):
            self.assertIsInstance(row[key], list)
            self.assertTrue(row[key])
        self.assertEqual(self.store.request("new", "first"), row)
        self.assertEqual(self.store.latest("new"), row)
        self.assertEqual(self.create("machine", criteria=[{"path": "done.txt"}])["completion_policy"], "machine_all")
        self.assertEqual(self.create("mixed-default", criteria=[{"path": "done.txt"}],
                                     manual_items=[{"label": "manual"}], stop_code="source_changed")["completion_policy"], "mixed")
        self.assertEqual(self.store.get("mixed-default")["stop_code"], "source_changed")

    def test_reserve_enforces_database_limit_and_optional_maximum_only_tightens(self):
        self.claim()
        with self.storage._connect() as db:
            db.execute("UPDATE chat_goals SET limits=? WHERE id='goal'", (goal_json({**GOAL_DEFAULT_LIMITS, "model_requests": 3}),))
        self.assertTrue(self.store.reserve("goal", "run-goal", "model_requests", 2, 999))
        self.assertFalse(self.store.reserve("goal", "run-goal", "model_requests", 1, 2))
        self.assertTrue(self.store.reserve("goal", "run-goal", "model_requests", 1))
        self.assertFalse(self.store.reserve("goal", "run-goal", "model_requests", 1, 999))
        self.assertFalse(self.store.reserve("goal", "wrong", "tool_actions", 1))
        self.assertTrue(any("json_extract(limits,'$.model_requests')" in sql for sql in self.storage.statements))

    def test_reservation_concurrency_cannot_overspend(self):
        self.claim()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.store.reserve("goal", "run-goal", "model_requests", 1, 7), range(30)))
        self.assertEqual(sum(results), 7)
        self.assertEqual(self.store.get("goal")["model_requests"], 7)

    def test_strict_integer_validation_for_increments_and_reservations(self):
        row = self.claim()
        for value in (True, False, 0, -1, 1.5, "1", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.extend("goal", row["version"], "invalid", {"rounds": value})
            with self.subTest(reserve=value), self.assertRaises(ValueError):
                self.store.reserve("goal", "run-goal", "model_requests", value)
        for value in (True, -1, 181, 1.0, "1"):
            with self.subTest(minutes=value), self.assertRaises(ValueError):
                self.store.extend("goal", row["version"], "invalid", {}, value)
        for increments in ({"unknown": 1}, [], None, {}, {"rounds": 33}, {"tool_actions": 201}, {"model_requests": 257}):
            with self.subTest(increments=increments), self.assertRaises(ValueError):
                self.store.extend("goal", row["version"], "invalid", increments)
        for maximum in (True, -1, "3", 3.0):
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                self.store.reserve("goal", "run-goal", "model_requests", 1, maximum)
        self.assertEqual(self.receipts(), [])

    def test_extend_is_idempotent_preserves_usage_checkpoint_state_and_wait(self):
        row = self.claim()
        self.store.reserve("goal", "run-goal", "model_requests", 2)
        self.store.reserve("goal", "run-goal", "tool_actions", 1)
        binding = {"kind": "job", "target_id": "job1"}
        self.store.transition(row, "blocked", "budget", stop_code="budget_model", checkpoint={"messages": ["private"]},
                              wait_binding=binding, settle=True)
        row = self.store.get("goal")
        changed = self.store.extend("goal", row["version"], "extend", {"model_requests": 3, "rounds": 2}, 15)
        for key in ("model_requests", "tool_actions", "checkpoint", "status", "stop_code", "wait_binding", "run_id", "generation"):
            self.assertEqual(changed[key], row[key], key)
        self.assertEqual(changed["version"], row["version"] + 1)
        self.assertEqual(changed["limits"]["model_requests"], GOAL_DEFAULT_LIMITS["model_requests"] + 3)
        self.assertEqual(changed["deadline_at"], row["deadline_at"] + 15 * 60000)
        self.assertEqual(self.store.extend("goal", row["version"], "extend", {"rounds": 2, "model_requests": 3}, 15), changed)
        with self.assertRaises(GoalConflict):
            self.store.extend("goal", changed["version"], "extend", {"model_requests": 4}, 15)
        self.assertEqual(len(self.receipts()), 1)
        with self.assertRaises(GoalConflict):
            self.store.extend("goal", row["version"], "other", {"rounds": 1})

    def test_expired_deadline_uses_now_and_total_time_cap_is_atomic(self):
        row = self.store.get("goal")
        self.store.transition(row, "blocked", deadline_at=self.now - 1)
        row = self.store.get("goal")
        with patch("naiba.storage.goals.goal_now", return_value=self.now + 1000):
            row = self.store.extend("goal", row["version"], "time", {}, 10)
        self.assertEqual(row["deadline_at"], self.now + 1000 + 600000)
        self.assertEqual(row["status"], "blocked")
        with self.storage._connect() as db:
            db.execute("UPDATE chat_goals SET deadline_at=? WHERE id='goal'", (self.now + 24 * 3600000 - 60000,))
        row = self.store.get("goal")
        before = len(self.receipts())
        with self.assertRaises(ValueError):
            self.store.extend("goal", row["version"], "over-time", {"rounds": 1}, 2)
        self.assertEqual(self.store.get("goal"), row)
        self.assertEqual(len(self.receipts()), before)
        extended = self.store.extend("goal", row["version"], "exact-time", {}, 1)
        self.assertEqual(extended["deadline_at"], self.now + 24 * 3600000)

    def test_total_budget_cap_no_partial_changes(self):
        with self.storage._connect() as db:
            db.execute("UPDATE chat_goals SET limits=? WHERE id='goal'", (goal_json(GOAL_MAX_LIMITS),))
        before = self.store.get("goal")
        for key in GOAL_MAX_LIMITS:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.store.extend("goal", before["version"], "over", {key: 1}, 1)
            self.assertEqual(self.store.get("goal"), before)
        self.assertEqual(self.receipts(), [])

    def test_receipt_insert_failure_rolls_back_budget_and_deadline(self):
        row = self.store.get("goal")
        with self.storage._connect() as db:
            db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON chat_goal_budget_receipts "
                       "BEGIN SELECT RAISE(ABORT,'receipt failed'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "receipt failed"):
            self.store.extend("goal", row["version"], "failure", {"rounds": 1}, 1)
        self.assertEqual(self.store.get("goal"), row)
        self.assertEqual(self.receipts(), [])

    def test_concurrent_identical_append_only_charges_once(self):
        row = self.store.get("goal")
        barrier = threading.Barrier(8)
        def append(_):
            barrier.wait(10)
            return self.store.extend("goal", row["version"], "shared", {"rounds": 1}, 1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(append, range(8)))
        self.assertTrue(all(r == rows[0] for r in rows))
        self.assertEqual(rows[0]["limits"]["rounds"], GOAL_DEFAULT_LIMITS["rounds"] + 1)
        self.assertEqual(len(self.receipts()), 1)

    def test_concurrent_distinct_appends_require_cas_retry_without_lost_update(self):
        row = self.store.get("goal")
        barrier = threading.Barrier(2)
        def append(request_id):
            barrier.wait(10)
            try:
                self.store.extend("goal", row["version"], request_id, {"rounds": 1}, 1)
                return request_id, True
            except GoalConflict:
                return request_id, False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(append, ("a", "b")))
        self.assertEqual(sum(ok for _, ok in results), 1)
        loser = next(key for key, ok in results if not ok)
        current = self.store.get("goal")
        self.store.extend("goal", current["version"], loser, {"rounds": 1}, 1)
        current = self.store.get("goal")
        self.assertEqual(current["limits"]["rounds"], GOAL_DEFAULT_LIMITS["rounds"] + 2)
        self.assertEqual(current["deadline_at"], row["deadline_at"] + 120000)
        self.assertEqual(len(self.receipts()), 2)

    def test_terminal_goals_never_revive_and_old_receipt_can_be_replayed(self):
        row = self.store.get("goal")
        appended = self.store.extend("goal", row["version"], "before", {"rounds": 1})
        self.assertTrue(self.store.transition(appended, "completed", settle=True))
        terminal = self.store.get("goal")
        with self.assertRaises(GoalConflict):
            self.store.extend("goal", terminal["version"], "after", {"rounds": 1})
        self.assertEqual(self.store.extend("goal", row["version"], "before", {"rounds": 1}), terminal)
        self.assertFalse(self.store.transition(terminal, "queued"))
        self.assertFalse(self.store.claim("goal", "run-goal"))
        self.assertEqual(self.store.get("goal"), terminal)

    def test_round_limit_and_deadline_block_claim_and_bind_next(self):
        with self.storage._connect() as db:
            db.execute("UPDATE chat_goals SET limits=? WHERE id='goal'", (goal_json({**GOAL_DEFAULT_LIMITS, "rounds": 1}),))
        row = self.claim()  # Last available round can still be claimed.
        self.assertTrue(self.store.transition(row, "queued", settle=True))
        row = self.store.get("goal")
        with self.assertRaises(GoalConflict), self.storage._connect() as db:
            self.store.bind_next(db, "goal", "next", row["version"])
        self.assertEqual(self.store.get("goal"), row)
        with self.storage._connect() as db:
            db.execute("UPDATE chat_goals SET limits=?,generation=2 WHERE id='goal'", (goal_json({**GOAL_DEFAULT_LIMITS, "rounds": 1}),))
        self.assertFalse(self.store.claim("goal", "run-goal"))
        row = self.store.get("goal")
        self.store.transition(row, "queued", deadline_at=1)
        row = self.store.get("goal")
        self.assertFalse(self.store.claim("goal", "run-goal"))
        with self.assertRaises(GoalConflict), self.storage._connect() as db:
            self.store.bind_next(db, "goal", "expired", row["version"])

    def test_transition_cas_new_fields_round_samples_and_safe_projection(self):
        row = self.claim()
        self.store.reserve("goal", "run-goal", "model_requests", 4)
        self.store.reserve("goal", "run-goal", "tool_actions", 2)
        evidence = [{"id": "item", "passed": True, "sha256": "hash", "raw": "secret", "result": {"raw": "secret"}}]
        self.assertTrue(self.store.transition(row, "waiting_tool", "checking", evidence=evidence, settle=True,
            stop_code="", wait_binding={"kind": "job", "target_id": "job-1"}, stages=[{"id": "stage", "status": "done"}]))
        self.assertFalse(self.store.transition(row, "blocked", wait_binding={}, stages=[]))
        current = self.store.get("goal")
        self.assertEqual(current["wait_binding"]["run_id"], "run-goal")
        self.assertEqual(current["stages"], [{"id": "stage", "status": "done"}])
        self.assertEqual(self.store.waiting("job", "job-1"), [current])
        self.assertEqual(self.store.find_wait_bindings("job", "wrong"), [])
        self.assertIn(("goal", "waiting_tool"), self.store.pending())
        summary = self.store.rounds("goal")["rounds"][0]
        self.assertEqual((summary["model_requests_start"], summary["model_requests_end"],
                          summary["tool_actions_start"], summary["tool_actions_end"]), (0, 4, 0, 2))
        self.assertEqual(summary["reason"], "checking")
        self.assertEqual(summary["evidence"], [{"id": "item", "passed": True, "sha256": "hash"}])
        self.assertNotIn("checkpoint", summary)
        self.assertIsInstance(summary["finished_at"], int)
        self.assertTrue(self.store.transition(current, "queued", wait_binding={}))
        current = self.store.get("goal")
        with self.storage._connect() as db:
            self.store.bind_next(db, "goal", "next", current["version"])
        next_summary = self.store.rounds("goal")["rounds"][0]
        self.assertEqual((next_summary["model_requests_start"], next_summary["tool_actions_start"]), (4, 2))
        self.assertEqual(self.store.waiting(), [])

    def test_bind_check_in_job_transaction_is_atomic_and_owner_checked(self):
        row = self.claim()
        with self.storage._connect() as db:
            db.execute("CREATE TABLE test_jobs(id TEXT PRIMARY KEY)")
        def create_bound_job(job_id, current):
            with self.storage._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO test_jobs VALUES (?)", (job_id,))
                if not self.store.bind_check(db, current, job_id, {"kind": "check", "check_version": 1}):
                    raise GoalConflict("stale")
        create_bound_job("check-1", row)
        current = self.store.get("goal")
        self.assertEqual(current["status"], "waiting_tool")
        self.assertEqual(current["wait_binding"]["target_id"], "check-1")
        self.assertEqual(self.store.waiting("check", "check-1"), [current])
        with self.assertRaises(GoalConflict):
            create_bound_job("check-2", row)
        with self.storage._connect() as db:
            self.assertEqual([r[0] for r in db.execute("SELECT id FROM test_jobs")], ["check-1"])
        self.assertEqual(self.store.get("goal"), current)
        self.assertEqual(self.store.rounds("goal")["rounds"][0]["status"], "waiting_tool")

    def test_append_and_cancel_race_never_revives_or_erases_usage(self):
        row = self.claim()
        self.store.reserve("goal", "run-goal", "model_requests", 3)
        barrier = threading.Barrier(2)
        def append():
            barrier.wait(10)
            try:
                return self.store.extend("goal", row["version"], "race", {"rounds": 1})
            except GoalConflict:
                return None
        def cancel():
            barrier.wait(10)
            return self.store.transition(row, "cancelled", settle=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(append), pool.submit(cancel)
            appended, cancelled = first.result(), second.result()
        self.assertNotEqual(appended is not None, cancelled)
        current = self.store.get("goal")
        if not cancelled:
            self.assertTrue(self.store.transition(current, "cancelled", settle=True))
        final = self.store.get("goal")
        self.assertEqual(final["status"], "cancelled")
        self.assertEqual(final["model_requests"], 3)
        self.assertFalse(self.store.claim("goal", "run-goal"))
        self.assertEqual(len(self.receipts()), int(appended is not None))

    def test_valid_next_round_bind_rolls_back_if_outer_job_creation_fails(self):
        row = self.claim()
        self.assertTrue(self.store.transition(row, "queued", settle=True))
        current = self.store.get("goal")
        with self.assertRaisesRegex(RuntimeError, "after bind"), self.storage._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.store.bind_next(db, "goal", "rollback-next", current["version"])
            raise RuntimeError("after bind")
        self.assertEqual(self.store.get("goal"), current)
        self.assertEqual(self.store.rounds("goal")["count"], 1)

    def test_wait_bind_rejects_foreign_owner_and_stale_same_version_run(self):
        row = self.claim()
        with self.assertRaises(GoalConflict):
            self.store.transition(row, "waiting_tool", wait_binding={"kind": "job", "target_id": "j", "run_id": "foreign"})
        self.assertFalse(self.store.transition({**row, "run_id": "foreign"}, "waiting_tool",
                                              wait_binding={"kind": "job", "target_id": "j"}))
        self.assertEqual(self.store.get("goal"), row)
        with self.storage._connect() as db, self.assertRaises(GoalConflict):
            self.store.bind_check(db, row, "one", {"kind": "check", "target_id": "two"})
        self.assertEqual(self.store.get("goal"), row)

    def test_settled_wait_round_can_receive_final_result_without_changing_finish_time(self):
        row = self.claim()
        with self.storage._connect() as db:
            self.assertTrue(self.store.bind_check(db, row, "job", {"kind": "check"}))
        waiting = self.store.get("goal")
        summary = self.store.rounds("goal")["rounds"][0]
        self.assertTrue(self.store.transition(waiting, "completed", "passed", settle=True, wait_binding={},
                                              evidence=[{"id": "check", "passed": True}]))
        final = self.store.rounds("goal")["rounds"][0]
        self.assertEqual(final["finished_at"], summary["finished_at"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["evidence"], [{"id": "check", "passed": True}])

    def test_check_bind_rolls_back_goal_and_job_on_later_transaction_failure(self):
        row = self.claim()
        with self.storage._connect() as db:
            db.execute("CREATE TABLE test_jobs(id TEXT PRIMARY KEY)")
        with self.assertRaisesRegex(RuntimeError, "after check"), self.storage._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO test_jobs VALUES ('check')")
            self.assertTrue(self.store.bind_check(db, row, "check", {"kind": "check"}))
            raise RuntimeError("after check")
        self.assertEqual(self.store.get("goal"), row)
        self.assertIsNone(self.store.rounds("goal")["rounds"][0]["finished_at"])
        with self.storage._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM test_jobs").fetchone()[0], 0)

    def test_reserve_rejects_expired_running_goal_without_charging(self):
        row = self.claim()
        self.assertTrue(self.store.transition(row, "running", deadline_at=1))
        self.assertFalse(self.store.reserve("goal", "run-goal", "model_requests", 1))
        self.assertFalse(self.store.reserve("goal", "run-goal", "tool_actions", 1))
        self.assertEqual(self.store.get("goal")["model_requests"], 0)
        self.assertEqual(self.store.get("goal")["tool_actions"], 0)

    def test_rounds_keyset_paging_scoped_cursor_and_no_history_cap(self):
        self.create("other")
        with self.storage._connect() as db:
            for generation in range(2, 602):
                db.execute("INSERT INTO chat_goal_rounds(goal_id,generation,run_id,status,checkpoint) VALUES (?,?,?,?,?)",
                    ("goal", generation, f"page-{generation}", "completed", '{"messages":["private"]}'))
        cursor, found = "", []
        self.storage.statements.clear()
        first_cursor = ""
        while True:
            page = self.store.rounds("goal", 20, cursor)
            self.assertEqual(page["count"], len(page["rounds"]))
            found.extend(item["generation"] for item in page["rounds"])
            self.assertTrue(all("checkpoint" not in item for item in page["rounds"]))
            if not page["has_more"]:
                self.assertEqual(page["next_cursor"], "")
                break
            cursor = page["next_cursor"]
            first_cursor = first_cursor or cursor
        self.assertEqual(found, list(range(601, 0, -1)))
        selects = [sql.lower() for sql in self.storage.statements if "from chat_goal_rounds" in sql.lower()]
        self.assertTrue(selects)
        self.assertTrue(all("checkpoint" not in sql and "offset" not in sql and "count(" not in sql for sql in selects))
        with self.assertRaises(ValueError):
            self.store.rounds("other", cursor=first_cursor)
        for cursor in ("!invalid", base64.urlsafe_b64encode(b'{"v":1,"scope":"goal","before":true}').decode(),
                       base64.urlsafe_b64encode(b'{"v":1,"scope":"goal","before":99999999999999999999999}').decode()):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                self.store.rounds("goal", cursor=cursor)
        for size in (True, 0, 101, "20", 1.5):
            with self.subTest(size=size), self.assertRaises(ValueError):
                self.store.rounds("goal", page_size=size)
        with self.assertRaises(LookupError):
            self.store.rounds("missing")


if __name__ == "__main__":
    unittest.main()
