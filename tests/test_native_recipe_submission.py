"""Mock-only native receipts and conservative recipe recovery contracts."""
from __future__ import annotations

from io import BytesIO
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from naiba.comfyui import client
from naiba.comfyui.jobs import ComfyUIJobManager, ComfyUITaskError
from naiba.comfyui.runner import ComfyUIRunner
from naiba.comfyui.store import ComfyUIStore
from naiba.studio.jobs import StudioJobManager
from naiba.studio.runner import StudioRunner
from naiba.studio.service import StudioService
from naiba.studio.store import StudioStore


class NativeRecipeSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = SimpleNamespace(paths=SimpleNamespace(data_dir=self.root, public_dir=self.root),
                                   config=SimpleNamespace(data={"comfyui_url": "http://changed.invalid"}))
        self.store = ComfyUIStore(self.root)
        self.runner = ComfyUIRunner(self.app, self.store)
        self.jobs = ComfyUIJobManager(self.app, self.store, mock.Mock())
        self.prompt = {"1": {"class_type": "Fake", "inputs": {"seed": -1, "text": "hello"}}}
        self.store.save_workflow({"id": "cw-test", "name": "fake", "prompt": self.prompt, "params": []})
        self.body = {"workflow_id": "cw-test"}
        self.policy = {"mode": "recipe", "target_url": "http://frozen.invalid"}
        self.schema = {"input": {"required": {"seed": ["INT", {}], "text": ["STRING", {}]}}}
        self.entry = {"status": {"status_str": "success"},
                      "outputs": {"1": {"images": [{"filename": "output.png"}]}}}
        # Any accidental network access makes these focused tests fail immediately.
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(client._DIRECT_OPENER, "open", side_effect=AssertionError("network forbidden")).start()
        self.submit = mock.patch.object(client, "submit", return_value=("pid-1", "")).start()
        self.history = mock.patch.object(client, "history_entry", return_value=self.entry).start()
        self.queue = mock.patch.object(client, "queue_ids", return_value=set()).start()
        self.info = mock.patch.object(client, "object_info_node", return_value=self.schema).start()
        from PIL import Image
        stream = BytesIO()
        Image.new("RGB", (2, 2), "red").save(stream, format="PNG")
        self.png = stream.getvalue()
        self.download = mock.patch.object(client, "download_output", return_value=self.png).start()

    def task(self, **kwargs):
        return self.jobs.submit(self.body, submission_policy=self.policy, **kwargs)

    def test_receipt_atomic_replay_conflict_owner_and_restart(self):
        for store in (self.store, StudioStore(self.root)):
            data = {"shots": 1, "content": "test", "model_name": "model"}
            def create(_):
                # Separate instances still serialize lookup/create for the same directory.
                return type(store)(self.root).create_task(
                    data, request_key="same", owner_id="owner", source_task_id="parent")
            with ThreadPoolExecutor(max_workers=12) as pool:
                tasks = list(pool.map(create, range(24)))
            self.assertEqual(len({t["id"] for t in tasks}), 1)
            task = tasks[0]
            self.assertTrue(task["payload_hash"])
            self.assertEqual(task["source_task_id"], "parent")
            restarted = type(store)(self.root)
            self.assertEqual(restarted.lookup_request("same", owner_id="owner")["id"], task["id"])
            self.assertIsNone(restarted.lookup_request("same", owner_id="other"))
            with self.assertRaises(ValueError):
                store.create_task({**data, "content": "different"}, request_key="same", owner_id="owner")
            data["content"] = "mutated"
            self.assertEqual(store.get_task(task["id"])["input"]["content"], "test")

    def test_response_loss_intent_unknown_never_resubmits(self):
        task = self.task(request_key="first", owner_id="batch")
        def lost(base, workflow):
            current = self.store.get_task(task["id"])
            row = current["checkpoint"]["submissions"]["0"]
            self.assertEqual(row["state"], "intent")
            self.assertEqual(row["workflow"], workflow)
            self.assertEqual(row["workflow_fingerprint"], self.store.payload_hash(workflow))
            return "", "TimeoutError"
        self.submit.side_effect = lost
        self.runner._run_task(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["checkpoint"]["submissions"]["0"]["state"], "unknown")
        with self.assertRaises(ComfyUITaskError):
            self.jobs.retry(task["id"], request_key="retry", owner_id="batch")
        self.store.update_task(task["id"], status="queued")
        self.runner._run_task(task["id"])
        self.assertEqual(self.submit.call_count, 1)

    def test_collection_retry_uses_descriptors_same_prompt_same_task(self):
        task = self.task(request_key="original", owner_id="batch")
        def broken(base, item):
            row = self.store.get_task(task["id"])["checkpoint"]["submissions"]["0"]
            self.assertEqual(row["output_items"], [item])
            self.assertEqual(base, self.policy["target_url"])
            raise OSError("disk or network")
        self.download.side_effect = broken
        self.runner._run_task(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["checkpoint"]["submissions"]["0"]["state"], "collection_failed")
        self.history.side_effect = AssertionError("must not need cleared history")
        self.download.side_effect = None
        retry = self.jobs.retry_collect(task["id"], request_key="collect", owner_id="batch")
        self.assertEqual(retry["id"], task["id"])
        self.runner._run_task(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "completed")
        self.jobs.retry_collect(task["id"], request_key="collect", owner_id="batch")
        self.assertEqual(self.jobs.lookup_request("collect", owner_id="batch")["id"], task["id"])
        self.assertEqual(self.submit.call_count, 1)
        self.assertEqual(self.download.call_args.args[0], self.policy["target_url"])

    def test_recorded_prompt_waits_then_adopts_without_submit(self):
        task = self.task()
        self.store.update_task(task["id"], checkpoint={"prompt_ids": ["recorded"], "completed": []})
        self.history.side_effect = [None, self.entry]
        self.queue.return_value = {"recorded"}
        with mock.patch("naiba.comfyui.runner.POLL_INTERVAL_SECONDS", 0):
            self.runner._run_task(task["id"])
        self.submit.assert_not_called()
        self.assertEqual(self.store.get_task(task["id"])["status"], "completed")

    def test_missing_history_never_resubmits(self):
        task = self.task()
        self.store.update_task(task["id"], checkpoint={"prompt_ids": ["recorded"], "completed": []})
        self.history.return_value = None
        self.runner._run_task(task["id"])
        self.submit.assert_not_called()
        self.assertIn("对账", self.store.get_task(task["id"])["error"])

    def test_definite_remote_failure_requires_explicit_new_attempt(self):
        task = self.task(owner_id="batch")
        self.history.return_value = {"status": {"status_str": "error"}}
        self.runner._run_task(task["id"])
        self.assertEqual(self.submit.call_count, 1)
        self.jobs.retry(task["id"], request_key="retry-one", owner_id="batch")
        self.jobs.retry(task["id"], request_key="retry-one", owner_id="batch")
        self.history.return_value = self.entry
        self.runner._run_task(task["id"])
        self.assertEqual(self.submit.call_count, 2)
        self.assertEqual(len(self.store.get_task(task["id"])["checkpoint"]["submission_history"]), 1)
        self.jobs.retry(task["id"], request_key="retry-one", owner_id="batch")
        with self.assertRaises(ComfyUITaskError):
            self.jobs.retry_collect(task["id"], request_key="retry-one", owner_id="batch")

    def test_cancel_only_interrupts_confirmed_own_running_prompt(self):
        for running, interrupt_count in (({"other"}, 0), (None, 0), (set(), 0), ({"own"}, 1)):
            with self.subTest(running=running):
                task = self.task()
                self.store.update_task(task["id"], checkpoint={"prompt_ids": ["own"], "completed": []})
                with mock.patch.object(client, "running_queue_ids", return_value=running), \
                     mock.patch.object(client, "interrupt") as interrupt, \
                     mock.patch.object(client, "delete_queue_items", return_value=True) as delete,                      mock.patch.object(self.runner, "submit"):
                    self.runner.cancel(task["id"])
                self.assertEqual(interrupt.call_count, interrupt_count)
                delete.assert_called_once_with(self.policy["target_url"], ["own"])

    def test_live_preflight_rejects_missing_class_required_and_enum(self):
        for schema in (None, {"input": {"required": {"missing": ["STRING", {}]}}},
                       {"input": {"required": {"text": [["not-hello"], {}]}}}):
            self.info.return_value = schema
            task = self.task()
            self.runner._run_task(task["id"])
            self.assertEqual(self.store.get_task(task["id"])["status"], "failed")
        self.submit.assert_not_called()

    def test_workflow_fingerprint_and_target_changes_rejected(self):
        with self.assertRaises(ComfyUITaskError):
            self.jobs.submit(self.body, submission_policy={**self.policy, "workflow_fingerprint": "changed"})
        task = self.task()
        self.store.update_task(task["id"], status="failed")
        with self.assertRaises(ComfyUITaskError):
            self.jobs.retry(task["id"], submission_policy={**self.policy, "target_url": "http://elsewhere.invalid"})

    def test_native_queue_delivery_dedup_active_and_terminal(self):
        for store, runner_cls in ((self.store, ComfyUIRunner), (StudioStore(self.root), StudioRunner)):
            runner = runner_cls(self.app, store)
            task = store.create_task({"shots": 1})
            entered, release = threading.Event(), threading.Event()
            calls = []
            def run(task_id):
                calls.append(task_id)
                store.update_task(task_id, status="running")
                entered.set()
                release.wait(3)
                store.update_task(task_id, status="completed" if runner_cls is ComfyUIRunner else "done")
            with mock.patch.object(runner, "_run_task", side_effect=run):
                try:
                    runner.submit(task["id"])
                    self.assertTrue(entered.wait(2))
                    for _ in range(5):
                        runner.submit(task["id"])
                    release.set()
                    for _ in range(5):
                        runner.submit(task["id"])
                finally:
                    release.set()
                    runner.shutdown()
            self.assertEqual(calls, [task["id"]])

    def test_restart_keeps_remote_recipe_work_for_adoption(self):
        task = self.task(request_key="original", owner_id="batch")
        self.store.update_task(task["id"], status="running", checkpoint={
            "prompt_ids": ["recorded"], "completed": [], "errors": [],
        })
        self.assertEqual(self.jobs.recover_interrupted(), 1)
        self.jobs.runner.cancel.assert_not_called()
        restarted = ComfyUIJobManager(self.app, ComfyUIStore(self.root), mock.Mock())
        retried = restarted.retry_collect(task["id"], request_key="collect", owner_id="batch")
        self.assertEqual(retried["id"], task["id"])
        self.runner._run_task(task["id"])
        self.submit.assert_not_called()
        self.assertEqual(self.store.get_task(task["id"])["status"], "completed")

    def test_partial_outputs_remain_visible_after_later_shot_collection_failure(self):
        task = self.jobs.submit({**self.body, "shots": 2}, submission_policy=self.policy)
        self.download.side_effect = [self.png, OSError("second output unavailable")]
        self.runner._run_task(task["id"])
        result = self.store.get_task(task["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["progress"]["done"], 1)
        self.assertEqual(len(result["result"]["outputs"]), 1)
        self.assertTrue((self.store.task_output_dir(task["id"]) / result["result"]["outputs"][0]).is_file())
        self.assertEqual(len(result["checkpoint"]["submissions"]), 2)

    def test_definite_reject_is_distinct_from_unknown_network_failure(self):
        task = self.task()
        self.submit.return_value = ("", client.SubmissionFailure("invalid workflow", definite=True))
        self.runner._run_task(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["checkpoint"]["submissions"]["0"]["state"], "remote_failed")
        self.jobs.retry(task["id"], request_key="explicit")
        self.submit.return_value = ("accepted", "")
        self.runner._run_task(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "completed")
        self.assertEqual(self.submit.call_count, 2)

    def test_cancellation_during_submit_records_id_then_scoped_deletes(self):
        task = self.task()
        def submit(base, workflow):
            self.runner.cancel(task["id"])
            return "arrived-late", ""
        self.submit.side_effect = submit
        self.history.return_value = None
        with mock.patch.object(client, "running_queue_ids", return_value={"other"}), \
             mock.patch.object(client, "interrupt") as interrupt, \
             mock.patch.object(client, "delete_queue_items") as delete:
            self.runner._run_task(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["checkpoint"]["prompt_ids"], ["arrived-late"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "cancelled")
        interrupt.assert_not_called()
        delete.assert_called_once_with(self.policy["target_url"], ["arrived-late"])

    def test_submit_replay_queue_failure_and_terminal_are_not_regenerated(self):
        self.jobs.runner.submit.side_effect = RuntimeError("response lost")
        with self.assertRaises(RuntimeError):
            self.task(request_key="lost", owner_id="batch")
        original = self.jobs.lookup_request("lost", owner_id="batch")
        self.jobs.runner.submit.side_effect = None
        replay = self.task(request_key="lost", owner_id="batch")
        self.assertEqual(replay["id"], original["id"])
        self.runner._run_task(original["id"])
        replay = self.task(request_key="lost", owner_id="batch")
        self.assertEqual(replay["id"], original["id"])
        self.runner._run_task(original["id"])
        self.assertEqual(self.submit.call_count, 1)

    def test_studio_policy_identity_fails_before_model_call_on_profile_drift(self):
        store = StudioStore(self.root)
        identity = {"model_key": "provider", "model_name": "selected", "provider_id": "p",
                    "kind": "online", "base_url_hash": self.store.payload_hash("http://before.invalid")}
        task = store.create_task({"content": "test", "model_key": "provider", "model_name": "selected"},
                                 submission_policy={"mode": "recipe", "model_identity": identity})
        self.app.models = mock.Mock()
        runner = StudioRunner(self.app, store)
        profile = {"provider_id": "p", "kind": "online", "base_url": "http://after.invalid"}
        with mock.patch("naiba.studio.runner.resolve_profile_with_override", return_value=profile):
            runner._run_task(task["id"])
        self.app.models.complete.assert_not_called()
        self.assertEqual(store.get_task(task["id"])["status"], "failed")
        profile["base_url"] = "http://before.invalid"
        profile["model"] = "selected"
        with mock.patch("naiba.studio.runner.resolve_profile_with_override", return_value=profile):
            self.assertEqual(runner._resolve_profile({"model_key": "provider", "model_name": "selected",
                                                     "model_snapshot": identity}), profile)

    def test_failed_remote_cancel_stays_pending_and_polls_original_success(self):
        task = self.task()
        calls = []
        def history(base, prompt_id):
            calls.append(prompt_id)
            if len(calls) == 1:
                with mock.patch.object(client, "running_queue_ids", return_value={"pid-1"}), mock.patch.object(client, "interrupt", return_value=False), mock.patch.object(client, "delete_queue_items", return_value=False):
                    self.runner.cancel(task["id"])
                current = self.store.get_task(task["id"])
                self.assertEqual(current["status"], "running")
                self.assertEqual(current["checkpoint"]["cancellation"]["state"], "pending_remote")
                return None
            return self.entry
        self.history.side_effect = history
        self.queue.return_value = {"pid-1"}
        with mock.patch("naiba.comfyui.runner.POLL_INTERVAL_SECONDS", 0):
            self.runner._run_task(task["id"])
        self.assertEqual(calls, ["pid-1", "pid-1"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "completed")
        self.assertEqual(self.submit.call_count, 1)

    def test_failed_remote_cancel_absent_without_ack_is_not_cancelled(self):
        task = self.task()
        self.store.update_task(task["id"], status="interrupted", checkpoint={"prompt_ids": ["remote"], "completed": []})
        self.history.return_value = None
        with mock.patch.object(client, "running_queue_ids", return_value={"remote"}), mock.patch.object(client, "interrupt", return_value=False), mock.patch.object(client, "delete_queue_items", return_value=False), mock.patch.object(self.runner, "submit"):
            self.assertTrue(self.runner.cancel(task["id"]))
        self.runner._run_task(task["id"])
        current = self.store.get_task(task["id"])
        self.assertEqual(current["status"], "interrupted")
        self.assertEqual(current["checkpoint"]["cancellation"]["state"], "pending_remote")
        self.submit.assert_not_called()

    def test_queue_delete_ack_does_not_prove_running_prompt_stopped(self):
        task = self.task()
        self.store.update_task(task["id"], status="interrupted", checkpoint={"prompt_ids": ["remote"], "completed": []})
        self.history.return_value = None
        with mock.patch.object(client, "running_queue_ids", return_value={"remote"}), mock.patch.object(client, "interrupt", return_value=False), mock.patch.object(client, "delete_queue_items", return_value=True), mock.patch.object(self.runner, "submit"):
            self.runner.cancel(task["id"])
        self.runner._run_task(task["id"])
        current = self.store.get_task(task["id"])
        self.assertEqual(current["status"], "interrupted")
        self.assertEqual(current["checkpoint"]["cancellation"]["state"], "pending_remote")
        self.assertTrue(current["checkpoint"]["cancellation"]["requires_interrupt"])

    def test_missing_collected_file_can_recollect_after_completed(self):
        task = self.task()
        self.runner._run_task(task["id"])
        current = self.store.get_task(task["id"])
        (self.store.task_output_dir(task["id"]) / current["result"]["outputs"][0]).unlink()
        self.jobs.retry_collect(task["id"], request_key="recollect")
        self.history.side_effect = AssertionError("descriptors already persisted")
        self.runner._run_task(task["id"])
        current = self.store.get_task(task["id"])
        self.assertEqual(current["status"], "completed")
        self.assertTrue((self.store.task_output_dir(task["id"]) / current["result"]["outputs"][0]).is_file())
        self.assertEqual(self.submit.call_count, 1)

    def test_collect_only_settles_pending_generation_without_submitting_it(self):
        task = self.jobs.submit({**self.body, "shots": 2}, submission_policy=self.policy)
        self.download.side_effect = OSError("temporary")
        self.runner._run_task(task["id"])
        self.jobs.retry_collect(task["id"], request_key="collect")
        self.download.side_effect = None
        self.runner._run_task(task["id"])
        current = self.store.get_task(task["id"])
        self.assertEqual(current["status"], "interrupted")
        self.assertEqual(current["checkpoint"]["pending_generation"], [1])
        self.assertEqual(current["progress"]["done"], 1)
        self.assertEqual(self.submit.call_count, 1)
        self.jobs.retry(task["id"], request_key="generate-remaining")
        self.runner._run_task(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "completed")
        self.assertEqual(self.submit.call_count, 2)

    def test_corrupt_recipe_artifact_retains_descriptor_and_never_regenerates(self):
        task = self.task()
        self.download.return_value = b"<html>service error</html>"
        self.runner._run_task(task["id"])
        current = self.store.get_task(task["id"])
        self.assertEqual(current["checkpoint"]["submissions"]["0"]["state"], "collection_failed")
        self.assertEqual(current["result"]["outputs"], [])
        self.jobs.retry_collect(task["id"], request_key="collect-valid")
        self.download.return_value = self.png
        self.runner._run_task(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "completed")
        self.assertEqual(self.submit.call_count, 1)

    def test_seed_exact_integer_no_float_roundtrip(self):
        entry = {"params": [{"node_id": "1", "field_name": "seed", "field_type": "seed"}]}
        for seed in (2 ** 63 + 7, str(2 ** 63 + 7)):
            got = self.jobs._validate_overrides(entry, [{"node_id": "1", "field_name": "seed", "value": seed}])
            self.assertEqual(got[0]["value"], 2 ** 63 + 7)
        for seed in ("20.9", 20.9, True):
            with self.assertRaises(ValueError):
                self.jobs._validate_overrides(entry, [{"node_id": "1", "field_name": "seed", "value": seed}])

    def test_recipe_raw_content_callback_preserves_verbatim_and_resets_attempt(self):
        store = StudioStore(self.root)
        task = store.create_task({"content": "test", "model_name": "model"}, submission_policy={"mode": "recipe"})
        self.app.config.runtime_guard_options = lambda: {}
        raw = "explanation</think>valid prompt"
        def complete(profile, messages, options, status):
            sink = options["raw_content_sink"]
            sink({"type": "reset"})
            sink({"type": "delta", "content": "failed prior attempt"})
            sink({"type": "reset"})
            sink({"type": "delta", "content": raw})
            sink({"type": "done", "content": raw})
            return "valid prompt"
        self.app.models = SimpleNamespace(complete=complete)
        with mock.patch("naiba.studio.runner.resolve_profile_with_override", return_value={}):
            StudioRunner(self.app, store)._run_task(task["id"])
        current = store.get_task(task["id"])
        self.assertEqual(current["status"], "done")
        self.assertEqual(current["result"], "valid prompt")
        self.assertEqual(current["raw_result"], raw)
        self.assertEqual(current["raw_result_source"], "runtime_callback")

    def test_recipe_without_raw_done_fails_closed(self):
        store = StudioStore(self.root)
        task = store.create_task({"content": "test", "model_name": "model"}, submission_policy={"mode": "recipe"})
        self.app.config.runtime_guard_options = lambda: {}
        self.app.models = SimpleNamespace(complete=lambda *args: "cleaned prompt")
        with mock.patch("naiba.studio.runner.resolve_profile_with_override", return_value={}):
            StudioRunner(self.app, store)._run_task(task["id"])
        current = store.get_task(task["id"])
        self.assertEqual(current["status"], "failed")
        self.assertEqual(current["raw_result_source"], "unavailable")
        self.assertNotEqual(current["raw_result"], "cleaned prompt")

    def test_studio_prepare_reuses_api_validation_and_preserves_errors(self):
        service = StudioService(self.app, mock.Mock(), mock.Mock())
        prepared = service.prepare_generation({"model_name": "model", "user_request": "hello"})
        self.assertEqual(prepared["content"], "hello")
        with self.assertRaises(ValueError):
            service.prepare_generation({"model_name": "model", "vars": []})
        self.assertEqual(service.api_generate({"model_name": "model", "vars": []})[1], 400)
        with mock.patch.object(service.catalog, "item", side_effect=LookupError):
            with self.assertRaises(LookupError):
                service.prepare_generation({"item_id": "missing"})

    def test_studio_retry_linkage_and_frozen_model_identity(self):
        store = StudioStore(self.root)
        jobs = StudioJobManager(self.app, store, mock.Mock())
        task = jobs.submit({"content": "hello", "model_name": "model"}, request_key="first", owner_id="batch")
        store.update_task(task["id"], status="failed")
        retried = jobs.retry(task["id"], request_key="second", owner_id="batch")
        self.assertEqual(retried["source_task_id"], task["id"])
        self.assertEqual(jobs.retry(task["id"], request_key="second", owner_id="batch")["id"], retried["id"])
        runner = StudioRunner(self.app, store)
        with mock.patch("naiba.studio.runner.resolve_profile_with_override", return_value={"model": "changed"}):
            with self.assertRaises(ValueError):
                runner._resolve_profile({"model_name": "model", "model_snapshot": {"model": "original"}})


if __name__ == "__main__":
    unittest.main()
