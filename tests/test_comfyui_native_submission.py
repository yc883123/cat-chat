"""Mock-only native receipts、直跑通用契约（幂等/取消/unload_after）。"""
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


class NativeSubmissionTests(unittest.TestCase):
    def test_optional_media_prunes_unused_loader_and_video_components_only(self):
        graph = {"image": {"class_type": "LoadImage", "inputs": {"image": "placeholder.png"}},
                 "video": {"class_type": "LoadVideo", "inputs": {"file": "placeholder.mp4"}},
                 "components": {"class_type": "GetVideoComponents", "inputs": {"video": ["video", 0]}},
                 "h3": {"class_type": "H3", "inputs": {"image": ["image", 0], "video": ["components", 0]}},
                 "unrelated": {"class_type": "LoadImage", "inputs": {"image": "keep.png"}}}
        schema = {"image": {"optional": True}, "video": {"optional": True}}
        with mock.patch("naiba.comfyui.runner.NodeSchemaProvider") as provider:
            provider.return_value.node_inputs.side_effect = lambda cls: schema if cls == "H3" else {"video": {"optional": False}}
            ComfyUIRunner._remove_unselected_optional_media(graph, [], "http://unused.invalid")
        self.assertNotIn("image", graph)
        self.assertNotIn("video", graph)
        self.assertNotIn("components", graph)
        self.assertIn("unrelated", graph)
        self.assertEqual(graph["h3"]["inputs"], {})
        shared = {"loader": {"class_type": "LoadImage", "inputs": {"image": "keep.png"}},
                  "h3": {"class_type": "H3", "inputs": {"optional": ["loader", 0], "required": ["loader", 0]}}}
        with mock.patch("naiba.comfyui.runner.NodeSchemaProvider") as provider:
            provider.return_value.node_inputs.return_value = {"required": {"optional": False}, "optional": {"optional": True}}
            ComfyUIRunner._remove_unselected_optional_media(shared, [], "http://unused.invalid")
        self.assertIn("loader", shared)
        self.assertIn("required", shared["h3"]["inputs"])
        self.assertNotIn("optional", shared["h3"]["inputs"])

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
        self.schema = {"input": {"required": {"seed": ["INT", {}], "text": ["STRING", {}]}}}
        self.entry = {"status": {"status_str": "success"},
                      "outputs": {"1": {"images": [{"filename": "output.png"}]}}}
        # Any accidental network access makes these focused tests fail immediately.
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(client._DIRECT_OPENER, "open", side_effect=AssertionError("network forbidden")).start()
        mock.patch.object(client, "reachable", return_value={}).start()
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
        return self.jobs.submit(self.body, **kwargs)

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

    def test_direct_cancel_ends_queued_task_without_touching_comfyui(self):
        task = self.task()
        with mock.patch.object(client, "interrupt") as interrupt, \
             mock.patch.object(client, "delete_queue_items") as delete:
            self.assertTrue(self.runner.cancel(task["id"]))
        self.assertEqual(self.store.get_task(task["id"])["status"], "cancelled")
        interrupt.assert_not_called()
        delete.assert_not_called()
        self.assertFalse(self.runner.cancel(task["id"]))

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

    def test_unload_after_requested_at_run_boundary(self):
        # 母本 §5.5：一轮工作流执行结束＝一个边界（收尾口径）；开关关闭＝零调用；
        # 受理/失败单独记事件，不影响已成功产物、不改变任务终态。
        free = mock.patch.object(client, "free_models", return_value=(True, "accepted")).start()
        task = self.jobs.submit({**self.body, "shots": 2, "unload_after": True})
        self.runner._run_task(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "completed")
        self.assertEqual(free.call_count, 1)
        self.assertEqual(free.call_args[0][0], "http://changed.invalid")
        unload_events = [event for event in self.store.read_events(task["id"])
                         if event["type"] == "unload_requested"]
        self.assertEqual([{k: e[k] for k in ("type", "accepted", "detail")} for e in unload_events],
                         [{"type": "unload_requested", "accepted": True, "detail": "accepted"}])
        free.reset_mock()
        task = self.jobs.submit({**self.body, "shots": 2})
        self.runner._run_task(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "completed")
        self.assertEqual(free.call_count, 0)
        free.reset_mock()
        free.return_value = (False, "busy")
        task = self.jobs.submit({**self.body, "shots": 1, "unload_after": True})
        self.runner._run_task(task["id"])
        result = self.store.get_task(task["id"])
        self.assertEqual(result["status"], "completed")
        unload_events = [event for event in self.store.read_events(task["id"]) if event["type"] == "unload_requested"]
        self.assertEqual([{k: e[k] for k in ("type", "accepted", "detail")} for e in unload_events],
                         [{"type": "unload_requested", "accepted": False, "detail": "busy"}])

    def test_seed_exact_integer_no_float_roundtrip(self):
        entry = {"params": [{"node_id": "1", "field_name": "seed", "field_type": "seed"}]}
        for seed in (2 ** 63 + 7, str(2 ** 63 + 7)):
            got = self.jobs._validate_overrides(entry, [{"node_id": "1", "field_name": "seed", "value": seed}])
            self.assertEqual(got[0]["value"], 2 ** 63 + 7)
        for seed in ("20.9", 20.9, True):
            with self.assertRaises(ValueError):
                self.jobs._validate_overrides(entry, [{"node_id": "1", "field_name": "seed", "value": seed}])

    def test_studio_policy_identity_fails_before_model_call_on_profile_drift(self):
        store = StudioStore(self.root)
        identity = {"model_key": "provider", "model_name": "selected", "provider_id": "p",
                    "kind": "online", "base_url_hash": self.store.payload_hash("http://before.invalid")}
        task = store.create_task({"content": "test", "model_key": "provider", "model_name": "selected"},
                                 submission_policy={"model_identity": identity})
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
