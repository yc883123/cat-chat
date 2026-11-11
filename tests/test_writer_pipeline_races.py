"""Deterministic Writer submission/cancellation/polling interleavings."""

import threading
from unittest import mock

from tests.test_writer_pipeline import PipelineTestCase
from naiba.writer.pipeline import WriterPipelineManager
from naiba.writer.store import WriterStore


class WriterPipelineRaceTests(PipelineTestCase):
    def setUp(self):
        super().setUp()
        self.store.save_setup(self.book_id, content={"idea": "并发测试"})

    def run_blocked(self, method, *, task_after_submit=True):
        """Hold submit at a known boundary and always join the worker on cleanup."""
        entered = threading.Event()
        release = threading.Event()
        results, errors = [], []
        submit = self.app.studio.jobs.submit

        def blocked(data):
            task = submit(data) if task_after_submit else None
            entered.set()
            if not release.wait(5):
                raise AssertionError("submit gate was not released")
            return task if task is not None else submit(data)

        self.app.studio.jobs.submit = blocked

        def worker():
            try:
                results.append(method())
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker, name="writer-race-submit")
        thread.start()

        def finish():
            release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive(), "submit worker did not stop")

        self.addCleanup(finish)
        self.assertTrue(entered.wait(5), "submit worker did not enter the gate")
        return finish, results, errors

    def test_poll_during_first_submit_does_not_fail_pipeline(self):
        finish, _, errors = self.run_blocked(self.start)
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "running")
        finish()
        self.assertEqual(errors, [])
        self.assertEqual(self.current_pipeline()["current_task_id"], self.last_task()["id"])
        self.assertEqual(len(self.app.studio.jobs.submitted), 1)

    def test_poll_during_retry_submit_does_not_fail_pipeline(self):
        self.start()
        self.app.studio.jobs.finish(self.last_task()["id"], "", status="failed")
        self.tick()
        finish, _, errors = self.run_blocked(lambda: self.manager.retry_pipeline(self.book_id))
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "running")
        finish()
        self.assertEqual(errors, [])
        self.assertEqual(self.current_pipeline()["attempt"], 2)

    def test_cancel_during_submit_waits_and_cancels_returned_task(self):
        finish, _, errors = self.run_blocked(self.start)
        stopped = self.manager.cancel_pipeline(self.book_id)
        self.assertEqual(stopped["status"], "stopping")
        self.assertIn("停止尚未确认", stopped["error"])
        finish()
        self.assertEqual(errors, [])
        task_id = self.last_task()["id"]
        self.assertIn(task_id, self.app.studio.jobs.cancelled)
        self.assertEqual(self.current_pipeline()["current_task_id"], task_id)
        self.assertEqual(self.current_pipeline()["status"], "cancelled")
        self.tick()
        self.assertEqual(len(self.app.studio.jobs.submitted), 1)

    def test_cancel_before_submission_build_completes_submits_nothing(self):
        entered, release = threading.Event(), threading.Event()
        original = self.manager._accumulated_setup
        results, errors = [], []

        def build(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            if not release.wait(5):
                raise AssertionError("build gate was not released")
            return result

        def worker():
            try:
                results.append(self.start())
            except Exception as exc:
                errors.append(exc)

        self.manager._accumulated_setup = build
        thread = threading.Thread(target=worker)

        def finish():
            release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())

        thread.start()
        self.addCleanup(finish)
        self.assertTrue(entered.wait(5))
        self.manager.cancel_pipeline(self.book_id)
        finish()
        self.assertEqual(errors, [])
        self.assertEqual(self.app.studio.jobs.submitted, [])
        self.assertEqual(self.current_pipeline()["status"], "cancelled")

    def test_cancel_reads_and_changes_state_in_one_store_lock(self):
        self.start()
        original = self.store._load_manifest
        ownership = []

        def read(book_id):
            if threading.current_thread().name == "writer-race-cancel":
                ownership.append(self.store._lock._is_owned())
            return original(book_id)

        with mock.patch.object(self.store, "_load_manifest", side_effect=read):
            errors = []

            def cancel():
                try:
                    self.manager.cancel_pipeline(self.book_id)
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=cancel, name="writer-race-cancel")
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(ownership)
        self.assertTrue(all(ownership), "cancel read its task/state outside the mutation lock")

    def test_late_cancel_does_not_replace_completed_status(self):
        self.start(kind="setup_step", step="genre")
        task_id = self.last_task()["id"]
        self.app.studio.jobs.finish(task_id, '{"channel":"male","cat":"奇幻","subCat":"东方","tone":"热血","hook":"卖点","tags":["a"]}')
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "done")
        with self.assertRaisesRegex(ValueError, "已结束"):
            self.manager.cancel_pipeline(self.book_id)
        self.assertEqual(self.current_pipeline()["status"], "done")
        self.assertEqual(self.app.studio.jobs.cancelled, [])

    def test_cancel_success_without_task_terminal_remains_stopping(self):
        self.start()
        task_id = self.last_task()["id"]
        self.app.studio.jobs.tasks[task_id]["status"] = "running"
        with mock.patch.object(self.app.studio.jobs, "cancel", return_value=True):
            stopped = self.manager.cancel_pipeline(self.book_id)
            self.assertEqual(stopped["status"], "stopping")
            self.assertEqual(stopped["current_task_id"], task_id)
            self.assertIn("停止尚未确认", stopped["error"])
            self.tick()
            self.assertEqual(self.current_pipeline()["status"], "stopping")
            with self.assertRaisesRegex(ValueError, "停止|进行"):
                self.manager.retry_pipeline(self.book_id)
            with self.assertRaisesRegex(ValueError, "停止|进行"):
                self.start()
            self.app.studio.jobs.finish(task_id, '{"genre":"迟到正文"}')
            self.tick()
        self.assertEqual(self.current_pipeline()["status"], "cancelled")
        self.assertEqual(self.current_pipeline()["completed_task_ids"], [])
        self.assertEqual(len(self.app.studio.jobs.submitted), 1)

    def test_cancel_failure_keeps_handle_and_visible_reason(self):
        self.start()
        task_id = self.last_task()["id"]
        with mock.patch.object(self.app.studio.jobs, "cancel", side_effect=RuntimeError("取消接口暂不可用")):
            stopped = self.manager.cancel_pipeline(self.book_id)
        self.assertEqual(stopped["status"], "stopping")
        self.assertEqual(stopped["current_task_id"], task_id)
        self.assertIn("停止尚未确认", stopped["error"])
        self.assertIn("取消接口暂不可用", stopped["error"])
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "cancelled")

    def test_submit_return_after_book_deletion_cancels_orphan(self):
        finish, _, _ = self.run_blocked(self.start)
        self.store.delete_book(self.book_id)
        finish()
        self.assertIn(self.last_task()["id"], self.app.studio.jobs.cancelled)

    def test_submit_and_cancel_never_hold_store_lock(self):
        submit = self.app.studio.jobs.submit
        cancel = self.app.studio.jobs.cancel

        def checked_submit(data):
            self.assertFalse(self.store._lock._is_owned())
            return submit(data)

        def checked_cancel(task_id):
            self.assertFalse(self.store._lock._is_owned())
            return cancel(task_id)

        with mock.patch.object(self.app.studio.jobs, "submit", side_effect=checked_submit), \
                mock.patch.object(self.app.studio.jobs, "cancel", side_effect=checked_cancel):
            self.start()
            self.manager.cancel_pipeline(self.book_id)

    def test_next_step_submit_reservation_survives_poll_then_cancel(self):
        self.start()
        first = self.last_task()["id"]
        self.app.studio.jobs.finish(first, '{"channel":"male","cat":"奇幻","subCat":"东方","tone":"热血","hook":"卖点","tags":["a"]}')
        finish, _, errors = self.run_blocked(self.tick)
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "running")
        self.assertEqual(self.current_pipeline()["step_index"], 1)
        self.manager.cancel_pipeline(self.book_id)
        finish()
        self.assertEqual(errors, [])
        self.assertIn(self.last_task()["id"], self.app.studio.jobs.cancelled)
        self.assertNotIn(first, self.app.studio.jobs.cancelled)
        self.assertEqual(self.current_pipeline()["status"], "cancelled")
        self.assertEqual(len(self.app.studio.jobs.submitted), 2)

    def test_submit_return_to_failed_pipeline_is_cancelled_and_tracked(self):
        finish, _, errors = self.run_blocked(self.start)
        before = self.current_pipeline()
        self.manager._fail(self.book_id, before, "其他失败")
        finish()
        self.assertEqual(errors, [])
        self.assertEqual(self.current_pipeline()["status"], "failed")
        self.assertEqual(self.current_pipeline()["current_task_id"], self.last_task()["id"])
        self.assertIn(self.last_task()["id"], self.app.studio.jobs.cancelled)

    def test_late_old_attempt_cannot_rebind_current_task(self):
        finish, _, errors = self.run_blocked(self.start)
        old = self.current_pipeline()

        def replace(record):
            record["attempt"] += 1
            record["current_task_id"] = "new-attempt-task"

        self.store.update_pipeline(self.book_id, old["id"], replace)
        finish()
        self.assertEqual(errors, [])
        current = self.current_pipeline()
        self.assertEqual((current["status"], current["attempt"], current["current_task_id"]),
                         ("running", 2, "new-attempt-task"))
        self.assertIn(self.last_task()["id"], self.app.studio.jobs.cancelled)

    def test_late_replaced_pipeline_cannot_rebind_current_task(self):
        finish, _, errors = self.run_blocked(self.start)
        old = self.current_pipeline()

        def replace(record):
            record["id"] = "pl-" + "f" * 12
            record["current_task_id"] = "replacement-task"

        self.store.update_pipeline(self.book_id, old["id"], replace)
        finish()
        self.assertEqual(errors, [])
        current = self.current_pipeline()
        self.assertEqual(current["id"], "pl-" + "f" * 12)
        self.assertEqual(current["current_task_id"], "replacement-task")
        self.assertIn(self.last_task()["id"], self.app.studio.jobs.cancelled)

    def test_task_handle_write_failure_cancels_task(self):
        save = self.store._save_manifest
        failed = False

        def save_once_failed(book_dir, manifest):
            nonlocal failed
            if not failed and (manifest.get("active_pipeline") or {}).get("current_task_id"):
                failed = True
                raise OSError("任务号写盘失败")
            return save(book_dir, manifest)

        with mock.patch.object(self.store, "_save_manifest", side_effect=save_once_failed):
            with self.assertRaisesRegex(OSError, "写盘失败"):
                self.start()
        self.assertEqual(self.current_pipeline()["status"], "failed")
        self.assertIn(self.last_task()["id"], self.app.studio.jobs.cancelled)
        self.assertEqual(self.manager._submissions, {})
        self.assertEqual(self.last_task()["input"]["writer"]["pipeline_id"], self.current_pipeline()["id"])

    def test_recovery_keeps_stopping_handle_without_auto_submit(self):
        self.start()
        task_id = self.last_task()["id"]
        with mock.patch.object(self.app.studio.jobs, "cancel", return_value=True):
            self.manager.cancel_pipeline(self.book_id)
        recovered_store = WriterStore(self.root)
        recovered = WriterPipelineManager(self.app, recovered_store)
        self.assertEqual(recovered.recover(), 1)
        row = recovered_store.get_active_pipeline(self.book_id)
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(row["current_task_id"], task_id)
        self.assertIn("停止结果尚未确认", row["error"])
        recovered.tick()
        self.assertEqual(len(self.app.studio.jobs.submitted), 1)

    def test_duplicate_poll_snapshots_cannot_advance_the_next_step(self):
        self.start()
        old = self.current_pipeline()
        self.app.studio.jobs.finish(self.last_task()["id"], '{"channel":"male","cat":"奇幻","subCat":"东方","tone":"热血","hook":"卖点","tags":["a"]}')
        self.manager._advance(self.book_id, old)
        current = self.current_pipeline()
        self.manager._advance(self.book_id, old)
        self.assertEqual(self.current_pipeline(), current)
        self.assertEqual(len(self.app.studio.jobs.submitted), 2)

    def test_unclaimed_step_cancelled_before_submit_submits_nothing(self):
        original = self.manager._submit_step

        def cancel_before_submit(book_id, pipeline):
            stopped = self.manager.cancel_pipeline(book_id)
            self.assertEqual(stopped["status"], "stopping")
            self.tick()
            return original(book_id, pipeline)

        with mock.patch.object(self.manager, "_submit_step", side_effect=cancel_before_submit):
            result = self.start()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.app.studio.jobs.submitted, [])

    def test_returned_task_is_cancelled_when_cancel_wins_before_handle_write(self):
        entered, release = threading.Event(), threading.Event()
        update = self.store.update_pipeline
        errors = []

        def delayed_update(*args, **kwargs):
            if threading.current_thread().name == "writer-race-persist":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("persist gate was not released")
            return update(*args, **kwargs)

        def worker():
            try:
                self.start()
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(self.store, "update_pipeline", side_effect=delayed_update):
            thread = threading.Thread(target=worker, name="writer-race-persist")
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.manager.cancel_pipeline(self.book_id)
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertIn(self.last_task()["id"], self.app.studio.jobs.cancelled)
        self.assertEqual(self.current_pipeline()["status"], "cancelled")

    def race_cancel_read_against_completion(self, *, last_step):
        self.start(**({"kind": "setup_step", "step": "genre"} if last_step else {}))
        self.app.studio.jobs.finish(self.last_task()["id"], '{"channel":"male","cat":"奇幻","subCat":"东方","tone":"热血","hook":"卖点","tags":["a"]}')
        read_done, release_read = threading.Event(), threading.Event()
        tick_started, tick_done = threading.Event(), threading.Event()
        read = self.store._load_manifest
        read_has_lock, cancel_errors, tick_errors = [], [], []

        def gated_read(book_id):
            result = read(book_id)
            if threading.current_thread().name == "writer-race-cancel-read" and not read_has_lock:
                read_has_lock.append(self.store._lock._is_owned())
                read_done.set()
                if not release_read.wait(5):
                    raise AssertionError("cancel read gate was not released")
            return result

        def cancel_worker():
            try:
                self.manager.cancel_pipeline(self.book_id)
            except Exception as exc:
                cancel_errors.append(exc)

        def tick_worker():
            try:
                tick_started.set()
                self.tick()
            except Exception as exc:
                tick_errors.append(exc)
            finally:
                tick_done.set()

        with mock.patch.object(self.store, "_load_manifest", side_effect=gated_read):
            cancel_thread = threading.Thread(target=cancel_worker, name="writer-race-cancel-read")
            tick_thread = threading.Thread(target=tick_worker, name="writer-race-finish")
            cancel_thread.start()
            try:
                self.assertTrue(read_done.wait(5))
                tick_thread.start()
                self.assertTrue(tick_started.wait(5))
                # Under the fixed lock, completion is blocked until cancel
                # commits. Without it, force completion into the stale-read
                # window so this test deterministically reproduces the bug.
                if not read_has_lock[0]:
                    self.assertTrue(tick_done.wait(5))
            finally:
                release_read.set()
                cancel_thread.join(5)
                if tick_thread.ident is not None:
                    tick_thread.join(5)
        self.assertFalse(cancel_thread.is_alive())
        self.assertFalse(tick_thread.is_alive())
        self.assertEqual(tick_errors, [])
        return read_has_lock[0], cancel_errors

    def test_cancel_cannot_leave_the_concurrently_submitted_next_task_alive(self):
        _, errors = self.race_cancel_read_against_completion(last_step=False)
        self.assertEqual(errors, [])
        self.assertEqual(self.current_pipeline()["status"], "cancelled")
        self.assertFalse([row for row in self.app.studio.jobs.tasks.values()
                          if row["status"] in ("queued", "running")])

    def test_cancel_cannot_overwrite_a_concurrently_completed_pipeline(self):
        cancel_won, errors = self.race_cancel_read_against_completion(last_step=True)
        if cancel_won:
            self.assertEqual(errors, [])
            self.assertEqual(self.current_pipeline()["status"], "cancelled")
        else:
            self.assertEqual(self.current_pipeline()["status"], "done")
            self.assertEqual(len(errors), 1)
            self.assertIn("已结束", str(errors[0]))
