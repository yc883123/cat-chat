"""造梦机真实执行器的取消、回执丢失与旧执行恢复；全程临时目录和假远端。"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from naiba.comfyui import client
from naiba.comfyui.jobs import ComfyUIJobManager, ComfyUITaskError
from naiba.comfyui.runner import ComfyUIRunner
from naiba.comfyui.store import ComfyUIStore
from naiba.core.contracts import COMFYUI_SUBMISSIONS_KEY


class ComfyuiSubmissionRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = SimpleNamespace(paths=SimpleNamespace(data_dir=Path(self.tmp.name)),
                                   config=SimpleNamespace(data={"comfyui_url": "http://fixture.invalid"}))
        self.store = ComfyUIStore(Path(self.tmp.name))
        self.runner = ComfyUIRunner(self.app, self.store)
        self.addCleanup(self.runner.shutdown)
        self.jobs = ComfyUIJobManager(self.app, self.store, self.runner)
        self.entry = {"status": {"status_str": "success"},
                      "outputs": {"1": {"images": [{"filename": "result.png"}]}}}
        self.patch(client._DIRECT_OPENER, "open", side_effect=AssertionError("network forbidden"))
        self.patch(client, "reachable", return_value={})
        self.submit = self.patch(client, "submit", return_value=("new-prompt", ""))
        self.history = self.patch(client, "history_entry", return_value=self.entry)
        self.queue = self.patch(client, "queue_snapshot", return_value=(set(), set()))
        self.delete = self.patch(client, "delete_queue_items", return_value=True)
        self.interrupt = self.patch(client, "interrupt", return_value=True)
        self.patch(client, "download_output", return_value=b"fake-image")
        self.patch(self.runner, "_remove_unselected_optional_media")
        self.media = self.patch(self.runner, "_apply_media", return_value="")

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def task(self, *, old_prompt="", status="queued"):
        task = self.store.create_task({"workflow_id": "fixture", "prompt": {}, "shots": 1})
        if old_prompt or status != "queued":
            task = self.store.update_task(task["id"], status=status, checkpoint={
                "prompt_ids": [old_prompt] if old_prompt else [], "completed": [], "errors": [], "resubmits": {},
            })
        return task

    def finish_worker(self, release):
        release.set()
        self.runner.shutdown(timeout=3)
        self.assertFalse(self.runner._worker.is_alive())

    def test_cancel_during_media_upload_never_submits(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def upload(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return ""
        self.media.side_effect = upload
        task = self.task()
        self.runner.submit(task["id"])
        self.assertTrue(entered.wait(2))
        self.assertTrue(self.runner.cancel(task["id"]))
        self.finish_worker(release)
        self.assertEqual(self.store.get_task(task["id"])["status"], "cancelled")
        self.submit.assert_not_called()

    def test_cancel_in_startup_window_cannot_revive_the_task(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def base(task):
            if threading.current_thread().name == "naiba-comfyui-run":
                entered.set()
                self.assertTrue(release.wait(3))
            return "http://fixture.invalid"
        self.patch(self.runner, "_task_base", side_effect=base)
        task = self.task()
        self.runner.submit(task["id"])
        self.assertTrue(entered.wait(2))
        self.assertTrue(self.runner.cancel(task["id"]))
        self.finish_worker(release)
        self.assertEqual(self.store.get_task(task["id"])["status"], "cancelled")
        self.submit.assert_not_called()

    def test_cancel_while_submit_is_blocked_records_and_stops_late_prompt(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        task = self.task()
        def submit(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return "late-prompt", ""
        def delete(base, ids):
            self.assertEqual(ids, ["late-prompt"])
            self.assertEqual(self.store.get_task(task["id"])["checkpoint"]["prompt_ids"], ids)
            return True
        self.submit.side_effect = submit
        self.delete.side_effect = delete
        self.queue.return_value = ({"late-prompt"}, {"late-prompt"})
        self.runner.submit(task["id"])
        self.assertTrue(entered.wait(2))
        self.assertTrue(self.runner.cancel(task["id"]))
        self.delete.assert_not_called()
        self.finish_worker(release)
        saved = self.store.get_task(task["id"])
        self.assertEqual(saved["status"], "cancelled")
        self.assertEqual(saved["checkpoint"]["prompt_ids"], ["late-prompt"])
        self.assertFalse(saved["checkpoint"][COMFYUI_SUBMISSIONS_KEY])
        self.delete.assert_called_once()
        self.interrupt.assert_called_once()

    def test_cancel_does_not_interrupt_an_unrelated_running_prompt(self):
        for snapshot in [None, ({"ours", "other"}, {"other"}), ({"ours", "other"}, {"ours", "other"})]:
            with self.subTest(snapshot=snapshot):
                self.queue.return_value = snapshot
                self.runner._cancel_prompts("http://fixture.invalid", ["ours"])
        self.assertEqual(self.delete.call_count, 3)
        self.interrupt.assert_not_called()

    def test_retry_rejoins_old_running_prompt_without_submission(self):
        task = self.task(old_prompt="old-prompt")
        self.history.side_effect = [None, self.entry]
        self.queue.return_value = ({"old-prompt"}, {"old-prompt"})
        self.runner._run_task(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "completed")
        self.submit.assert_not_called()

    def test_history_queue_uncertainty_and_nonterminal_history_do_not_resubmit(self):
        for history, queue in [(OSError("offline"), (set(), set())), (None, None),
                               ({"status": {}, "outputs": {}}, (set(), set()))]:
            with self.subTest(history=history, queue=queue):
                task = self.task(old_prompt="old-prompt")
                self.history.side_effect = history if isinstance(history, Exception) else None
                self.history.return_value = history
                self.queue.return_value = queue
                self.runner._run_task(task["id"])
                saved = self.store.get_task(task["id"])
                self.assertEqual(saved["status"], "failed")
                self.assertIn("未重复提交", saved["error"])
                self.assertEqual(saved["checkpoint"]["prompt_ids"], ["old-prompt"])
        self.submit.assert_not_called()

    def test_history_finishing_between_queries_is_collected_without_resubmission(self):
        task = self.task(old_prompt="old-prompt")
        self.history.side_effect = [None, self.entry, self.entry]
        self.runner._run_task(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "completed")
        self.submit.assert_not_called()

    def test_failed_download_retries_collection_of_the_same_prompt(self):
        task = self.task(old_prompt="old-prompt")
        with mock.patch.object(self.runner, "_save_outputs", side_effect=OSError("disk full")):
            self.runner._run_task(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "failed")
        self.store.update_task(task["id"], status="queued")
        self.runner._run_task(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "completed")
        self.submit.assert_not_called()

    def test_unknown_submit_response_blocks_retry_and_survives_reload(self):
        task = self.task()
        self.submit.return_value = ("", "response lost")
        self.runner._run_task(task["id"])
        reloaded = ComfyUIStore(Path(self.tmp.name)).get_task(task["id"])
        self.assertEqual(reloaded["checkpoint"][COMFYUI_SUBMISSIONS_KEY], {"0": "unknown"})
        self.assertIn("是否已受理尚未确认", reloaded["error"])
        self.assertFalse(self.jobs.task(task["id"])["can_retry"])
        with self.assertRaisesRegex(ComfyUITaskError, "尚未确认"):
            self.jobs.retry(task["id"])
        self.assertEqual(self.submit.call_count, 1)

    def test_definite_rejection_leaves_no_unknown_submission(self):
        task = self.task()
        self.submit.return_value = ("", client.SubmissionFailure("invalid node", definite=True))
        self.runner._run_task(task["id"])
        saved = self.store.get_task(task["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertFalse(saved["checkpoint"][COMFYUI_SUBMISSIONS_KEY])
        self.assertTrue(self.jobs.task(task["id"])["can_retry"])

    def test_persist_before_submit_failure_never_calls_remote(self):
        task = self.task()
        update = self.store.update_task
        def fail_marker(task_id, **fields):
            if (fields.get("checkpoint") or {}).get(COMFYUI_SUBMISSIONS_KEY):
                raise OSError("disk full")
            return update(task_id, **fields)
        with mock.patch.object(self.store, "update_task", side_effect=fail_marker):
            self.runner._run_task(task["id"])
        self.submit.assert_not_called()
        self.assertEqual(self.store.get_task(task["id"])["status"], "failed")

    def test_persist_late_receipt_failure_retains_marker_and_stops_known_prompt(self):
        task = self.task()
        update = self.store.update_task
        def fail_receipt(task_id, **fields):
            if (fields.get("checkpoint") or {}).get("prompt_ids"):
                raise OSError("disk full")
            return update(task_id, **fields)
        with mock.patch.object(self.store, "update_task", side_effect=fail_receipt):
            self.runner._run_task(task["id"])
        saved = self.store.get_task(task["id"])
        self.assertEqual(saved["checkpoint"][COMFYUI_SUBMISSIONS_KEY], {"0": "submitting"})
        self.assertIn("new-prompt", saved["error"])
        self.delete.assert_called_once_with("http://fixture.invalid", ["new-prompt"])
        with self.assertRaisesRegex(ComfyUITaskError, "尚未确认"):
            self.jobs.retry(task["id"])

    def test_auto_resubmit_upload_also_honors_cancellation(self):
        task = self.task()
        self.history.return_value = None
        def upload(*args):
            if self.media.call_count == 2:
                self.runner.cancel(task["id"])
            return ""
        self.media.side_effect = upload
        self.runner._run_task(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "cancelled")
        self.assertEqual(self.submit.call_count, 1)


class ComfyuiResponseEvidenceTests(unittest.TestCase):
    def test_malformed_queue_is_unknown_not_empty(self):
        for body in [{}, {"queue_running": [], "queue_pending": None},
                     {"queue_running": [[1]], "queue_pending": []}]:
            with self.subTest(body=body), mock.patch.object(client._DIRECT_OPENER, "open",
                    return_value=BytesIO(json.dumps(body).encode())):
                self.assertIsNone(client.queue_snapshot("http://fixture.invalid"))

    def test_malformed_history_is_unknown_not_absent(self):
        for body in [[], {"pid": "invalid"}]:
            with self.subTest(body=body), mock.patch.object(client._DIRECT_OPENER, "open",
                    return_value=BytesIO(json.dumps(body).encode())):
                with self.assertRaises(OSError):
                    client.history_entry("http://fixture.invalid", "pid")
