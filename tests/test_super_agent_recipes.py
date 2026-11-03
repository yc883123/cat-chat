from __future__ import annotations

import copy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from naiba.comfyui.store import ComfyUIStore
from naiba.storage.store import ChatStorage
from naiba.super_agent.recipe_store import digest
from naiba.super_agent.recipes import RecipeCoordinator
from naiba.super_agent.service import SuperAgentService


GRAPH = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "base"}},
         "2": {"class_type": "KSampler", "inputs": {"positive": ["1", 0], "steps": 20}},
         "3": {"class_type": "LoadImage", "inputs": {"image": "base.png"}}}
RECIPE = {"id": "test_recipe", "name": "Recipe", "description": "local fake only", "mode": "test",
          "workflow": GRAPH, "workflow_fingerprint": digest(GRAPH), "provenance": {"source": "test"},
          "prompt_slot": {"node_id": "1", "field_name": "text"},
          "media_slots": [{"role": "image", "node_id": "3", "field_name": "image", "scope": "item", "required": True}],
          "template": {"id": "test_template", "body": "{{requirements}} {{duration_seconds}} {{image_count}}", "result_rule": {"type": "text"}}}


class FakeJobs:
    def __init__(self, data_dir):
        self.records, self.requests, self.calls = {}, {}, []
        self.store = self
        self.data_dir = data_dir
        self.lose_response = False
        self.cancels = []

    def lookup_request(self, request_key, *, owner_id=""):
        return self.get_task(self.requests.get((owner_id, request_key)))

    def get_task(self, task_id):
        task = self.records.get(task_id)
        if task and task.get("status") == "done" and isinstance(task.get("result"), str) and "raw_result" not in task:
            task["raw_result"] = task["result"]
        return task

    def submit(self, payload, *, request_key, owner_id, source_task_id="", submission_policy=None):
        previous = self.lookup_request(request_key, owner_id=owner_id)
        if previous:
            return previous
        task_id = f"native-{len(self.records) + 1}"
        task = {"id": task_id, "status": "queued", "input": copy.deepcopy(payload), "result": "", "raw_result_source": "runtime_callback", "source_task_id": source_task_id}
        self.records[task_id] = task
        self.requests[(owner_id, request_key)] = task_id
        self.calls.append((payload, request_key, submission_policy))
        if self.lose_response:
            self.lose_response = False
            raise OSError("lost submit response")
        return task

    def cancel(self, task_id):
        self.cancels.append(task_id)
        return True  # cancellation requested, not falsely finished

    def retry(self, task_id, **kwargs):
        task = self.records[task_id]
        self.requests[(kwargs["owner_id"], kwargs["request_key"])] = task_id
        task["status"] = "queued"
        self.calls.append((task_id, kwargs["request_key"], "retry"))
        return task

    def retry_collect(self, task_id, **kwargs):
        return self.retry(task_id, **kwargs)

    def task_output_dir(self, task_id):
        return self.data_dir / "comfyui" / "outputs" / task_id


class RecipeCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.image = self.data / "uploads" / "original.png"
        self.image.parent.mkdir(parents=True)
        Image.new("RGB", (4, 4), "red").save(self.image)
        self.storage = ChatStorage(self.root / "chat.db")
        self.service = SuperAgentService(self.storage)
        self.studio = FakeJobs(self.data)
        self.comfy = FakeJobs(self.data)
        self.workflow_store = ComfyUIStore(self.data)
        self.profile = {"model": "vision", "kind": "online", "provider_id": "p", "base_url": "https://provider.invalid", "api_key": "never-store-this"}
        self.app = SimpleNamespace(paths=SimpleNamespace(data_dir=self.data),
                                   config=SimpleNamespace(data={"comfyui_url": "http://127.0.0.1:8188"}, profile=lambda key: self.profile),
                                   studio=SimpleNamespace(jobs=self.studio, prepare_generation=lambda b: {"content": b["user_request"], **b}),
                                   comfyui=SimpleNamespace(jobs=self.comfy, store=self.workflow_store))
        self.core = RecipeCoordinator(self.app, self.service)
        self.patch = patch.object(RecipeCoordinator, "_builtins", return_value=[copy.deepcopy(RECIPE)])
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.fp = patch.object(RecipeCoordinator, "_fingerprint", side_effect=digest)
        self.fp.start()
        self.addCleanup(self.fp.stop)
        self.validator = patch.object(RecipeCoordinator, "_validate_output", side_effect=lambda b, i, raw: {"valid": raw == "good prompt", "text": raw if raw == "good prompt" else "", "raw": raw, "errors": [] if raw == "good prompt" else ["bad output"]})
        self.validator.start()
        self.addCleanup(self.validator.stop)

    def body(self, count=1, key="create"):
        return {"recipe_id": "test_recipe", "request_id": key, "model_key": "provider:p", "model_name": "vision", "items": [
            {"source_path": f"nested/{n}.png", "media": [{"role": "image", "path": str(self.image)}], "requirements": f"scene {n}"}
            for n in range(count)]}

    def create(self, count=1):
        created = self.core.create("owner", self.body(count))
        self.tid = created["thread_id"]
        return created["batch"]

    def batch(self):
        return self.core.detail("owner", self.tid)["batch"]

    def action(self, action, key=None, **kwargs):
        return self.core.action("owner", self.tid, {"action": action, "request_id": key or action, "revision": self.batch()["revision"], **kwargs})["batch"]

    def reverse_done(self):
        self.action("confirm_reverse")
        self.core.tick()
        self.studio.records["native-1"].update(status="done", result="good prompt")
        self.core.tick()
        return self.batch()

    def test_create_owned_manifest_before_single_thread_and_replay(self):
        first = self.create()
        self.assertEqual(first["status"], "draft")
        self.assertEqual(self.service.get_node("owner", first["node_id"]).status, "waiting_user")
        self.assertEqual(self.service.get_node("owner", first["node_id"]).executor_id, first["id"])
        self.assertEqual(self.core.create("owner", self.body())["thread_id"], self.tid)
        self.assertEqual(len(self.service.list_threads("owner")), 1)
        self.assertNotIn("never-store-this", self.core.store.path.read_bytes().decode("latin1"))
        with self.assertRaises(LookupError):
            self.core.detail("other", self.tid)
        with self.assertRaisesRegex(ValueError, "request_id"):
            self.core.create("owner", {**self.body(), "shots": 2})

    def test_creation_crash_after_manifest_recovers_stable_link(self):
        with patch.object(self.core, "_link", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.core.create("owner", self.body())
        self.assertEqual(len(self.core.store.batches()), 1)
        self.core.recover()
        replay = self.core.create("owner", self.body())
        self.assertEqual(len(self.service.list_threads("owner")), 1)
        self.assertTrue(replay["thread_id"])

    def test_two_confirmations_never_implicit_generation(self):
        self.create()
        batch = self.reverse_done()
        self.assertEqual(batch["status"], "review")
        self.assertEqual(self.comfy.calls, [])
        for _ in range(3):
            self.core.tick()
        self.assertEqual(len(self.studio.calls), 1)
        self.assertEqual(len(self.comfy.calls), 0)
        self.action("confirm_generate")
        self.core.tick()
        self.assertEqual(len(self.comfy.calls), 1)
        payload = self.comfy.calls[0][0]
        self.assertIn({"node_id": "1", "field_name": "text", "value": "good prompt"}, payload["overrides"])

    def test_confirmation_concurrency_and_replay_before_revision(self):
        self.create()
        body = {"action": "confirm_reverse", "request_id": "same", "revision": 1}
        results, errors = [], []
        def confirm():
            try:
                results.append(self.core.action("owner", self.tid, body))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=confirm) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.core.tick()
        self.studio.records["native-1"].update(status="done", result="good prompt")
        self.core.tick()
        self.assertEqual(self.core.action("owner", self.tid, body), results[0])
        self.assertEqual(len(self.studio.calls), 1)
        with self.assertRaisesRegex(ValueError, "request_id"):
            self.core.action("owner", self.tid, {**body, "action": "cancel"})

    def test_lost_response_reconciles_same_key_no_new_submit(self):
        self.create()
        self.studio.lose_response = True
        self.action("confirm_reverse")
        self.core.tick()
        self.assertEqual(self.batch()["status"], "needs_review")
        self.core.tick()
        self.assertEqual(len(self.studio.calls), 1)
        self.studio.records["native-1"].update(status="done", result="good prompt")
        self.core.tick()
        self.assertEqual(len(self.studio.calls), 1)

    def test_pause_blocks_next_item_and_recover_requires_continue(self):
        self.create(2)
        self.action("confirm_reverse")
        self.core.tick()
        self.action("pause")
        self.studio.records["native-1"].update(status="done", result="good prompt")
        self.core.tick()
        self.assertEqual(len(self.studio.calls), 1)
        recovered = RecipeCoordinator(self.app, self.service)
        recovered.recover()
        recovered.tick()
        self.assertEqual(len(self.studio.calls), 1)
        self.core = recovered
        self.action("continue")
        self.core.tick()
        self.assertEqual(len(self.studio.calls), 2)

    def test_cancel_real_request_late_result_never_advances(self):
        self.create(2)
        self.action("confirm_reverse")
        self.core.tick()
        self.action("cancel")
        self.assertEqual(self.studio.cancels, ["native-1"])
        self.assertTrue(self.batch()["cancel_pending"])
        self.assertEqual(self.service.get_thread("owner", self.tid).status, "stopping")
        self.studio.records["native-1"].update(status="done", result="good prompt")
        self.core.tick()
        self.assertEqual(self.batch()["status"], "cancelled")
        self.assertEqual(self.batch()["items"][0]["prompt"], "")
        self.assertEqual(len(self.studio.calls), 1)
        self.assertEqual(self.service.get_thread("owner", self.tid).status, "cancelled")

    def test_malformed_output_keeps_untruncated_raw_and_requires_edit(self):
        self.create()
        self.action("confirm_reverse")
        self.core.tick()
        raw = "bad" * 20000
        self.studio.records["native-1"].update(status="done", result=raw)
        self.core.tick()
        item = self.batch()["items"][0]
        self.assertEqual(item["reverse"]["attempts"][0]["raw"], raw)
        with self.assertRaises(ValueError):
            self.action("confirm_generate")
        self.action("edit", items={item["id"]: {"prompt": "manual correction"}})
        self.action("confirm_generate")
        self.core.tick()
        self.assertEqual(len(self.comfy.calls), 1)
        self.assertEqual(len(self.studio.calls), 1)

    def test_partial_missing_files_not_success_and_collect_only_retry(self):
        self.create()
        self.reverse_done()
        self.action("confirm_generate")
        self.core.tick()
        self.comfy.records["native-1"].update(status="done", result={"outputs": ["missing.mp4"], "completed": [0]}, progress={"done": 1})
        self.core.tick()
        self.assertEqual(self.batch()["status"], "partial")
        self.action("retry_collect")
        self.core.tick()
        self.assertEqual(len(self.studio.calls), 1)
        self.assertEqual(len(self.comfy.records), 1)

    def test_output_file_completion(self):
        self.create()
        self.reverse_done()
        self.action("confirm_generate")
        self.core.tick()
        output = self.comfy.task_output_dir("native-1") / "out.mp4"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"fake local video")
        self.comfy.records["native-1"].update(status="done", result={"outputs": ["out.mp4"], "completed": [0]}, progress={"done": 1})
        self.core.tick()
        self.assertEqual(self.batch()["status"], "completed")
        self.assertEqual(self.service.get_thread("owner", self.tid).status, "completed")

    def test_validation_limits_paths_roles_and_corrupt_images(self):
        cases = []
        for source in ("../escape.png", "C:/escape.png", "/escape.png", "a/../b", "a//b"):
            body = self.body(key=source)
            body["items"][0]["source_path"] = source
            cases.append(body)
        corrupt = self.image.with_name("corrupt.png")
        corrupt.write_bytes(b"not an image")
        body = self.body(key="corrupt")
        body["items"][0]["media"][0]["path"] = str(corrupt)
        cases.append(body)
        body = self.body(key="ambiguous", count=2)
        body["items"][1]["source_path"] = body["items"][0]["source_path"].upper()
        cases.append(body)
        body = self.body(key="roles")
        body["items"][0]["media"] *= 2
        cases.append(body)
        cases.append(self.body(count=51, key="limit"))
        for body in cases:
            with self.subTest(body=body["request_id"]), self.assertRaises(ValueError):
                self.core.create("owner", body)

    def test_model_target_workflow_mutation_stops_confirmation(self):
        self.create()
        self.profile["base_url"] = "https://changed.invalid"
        with self.assertRaisesRegex(ValueError, "模型"):
            self.action("confirm_reverse")
        self.profile["base_url"] = "https://provider.invalid"
        self.app.config.data["comfyui_url"] = "http://127.0.0.1:9999"
        with self.assertRaisesRegex(ValueError, "目标"):
            self.action("confirm_reverse")
        self.app.config.data["comfyui_url"] = "http://127.0.0.1:8188"
        entry = self.workflow_store.get_workflow(self.batch()["recipe"]["workflow_id"])
        entry["prompt"]["1"]["inputs"]["text"] = "changed"
        self.workflow_store.save_workflow(entry)
        with self.assertRaisesRegex(ValueError, "工作流"):
            self.action("confirm_reverse")

    def test_original_mutation_does_not_change_managed_snapshot(self):
        item = self.create()["items"][0]
        original_hash = item["media"][0]["sha256"]
        Image.new("RGB", (4, 4), "blue").save(self.image)
        self.action("confirm_reverse")
        self.core.tick()
        snapshot = Path(self.studio.calls[0][0]["images"][0]["path"])
        import hashlib
        self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), original_hash)
        from naiba.core.paths import normalized_path_key
        self.assertIn(normalized_path_key(snapshot), self.core.referenced_upload_paths())

    def test_managed_snapshot_corruption_rejects_confirmation(self):
        batch = self.create()
        Path(batch["items"][0]["media"][0]["path"]).write_bytes(b"corrupted snapshot")
        with self.assertRaisesRegex(ValueError, "损坏"):
            self.action("confirm_reverse")
        self.assertFalse(self.studio.calls)

    def test_restart_after_native_accept_never_resubmits_or_claims_next(self):
        self.create(2)
        self.action("confirm_reverse")
        self.core.tick()
        core = RecipeCoordinator(self.app, self.service)
        core.recover()
        self.studio.records["native-1"].update(status="done", result="good prompt")
        core.tick()
        self.assertEqual(len(self.studio.calls), 1)
        self.core = core
        self.assertEqual(self.batch()["status"], "interrupted")
        self.action("continue")
        core.tick()
        self.assertEqual(len(self.studio.calls), 2)

    def test_edit_invalidates_authorized_unsubmitted_stages(self):
        self.create()
        confirmed = self.action("confirm_reverse")
        item_id = confirmed["items"][0]["id"]
        self.action("edit", items={item_id: {"prompt": "manually supplied"}})
        self.core.tick()
        self.assertFalse(self.studio.calls)
        self.assertEqual(self.batch()["items"][0]["reverse"]["attempts"][0]["state"], "invalidated")
        self.action("confirm_generate")
        self.core.tick()
        self.assertEqual(len(self.comfy.calls), 1)

    def test_collection_failure_only_permits_collect_retry(self):
        self.create()
        self.reverse_done()
        self.action("confirm_generate")
        self.core.tick()
        self.comfy.records["native-1"].update(status="failed", result={"outputs": []}, checkpoint={"submissions": {"0": {"state": "collection_failed", "prompt_id": "remote-id", "error": "download"}}})
        self.core.tick()
        item = self.batch()["items"][0]
        self.assertEqual(item["generate"]["status"], "collection_failed")
        self.assertIn("retry_collect", item["capabilities"])
        self.assertNotIn("retry_generate", item["capabilities"])
        with self.assertRaisesRegex(ValueError, "retry_collect"):
            self.action("retry_generate")
        self.action("retry_collect")
        self.core.tick()
        self.assertEqual(len(self.comfy.records), 1)

    def test_recover_interrupted_can_cancel_without_invalid_transition(self):
        self.create()
        self.action("confirm_reverse")
        self.core.tick()
        self.core.recover()
        self.assertEqual(self.service.get_thread("owner", self.tid).status, "needs_recovery")
        self.action("cancel")
        self.assertEqual(self.service.get_thread("owner", self.tid).status, "stopping")
        self.studio.records["native-1"]["status"] = "cancelled"
        self.core.tick()
        self.assertEqual(self.service.get_thread("owner", self.tid).status, "cancelled")

    def test_direct_prompt_never_resolves_model_across_generate_retry_collect(self):
        body = self.body()
        body.update(input_mode="prompt")
        body.pop("model_key")
        body.pop("model_name")
        body["items"][0]["prompt"] = "A clear final scene with <Picture 1>."
        self.app.config.profile = lambda key: (_ for _ in ()).throw(AssertionError("unused model resolved"))
        created = self.core.create("owner", body)
        self.tid = created["thread_id"]
        self.assertIsNone(created["batch"]["model"])
        self.assertIsNone(created["batch"]["template"])
        with self.assertRaises(ValueError):
            self.action("confirm_reverse")
        self.action("confirm_generate")
        self.core.tick()
        self.comfy.records["native-1"].update(status="failed", error="generation failed")
        self.core.tick()
        self.action("retry_generate")
        self.core.tick()
        self.comfy.records["native-1"].update(status="failed", checkpoint={"submissions": {"0": {"state": "collection_failed"}}})
        self.core.tick()
        self.action("retry_collect")
        self.core.tick()
        self.assertEqual(self.studio.calls, [])
        self.assertEqual(len(self.comfy.records), 1)

    def test_text_only_assisted_and_zero_reference_r2v(self):
        self.patch.stop()
        self.profile["supports_images_explicit"] = False
        body = {"recipe_id": "minimax_h3_r2v", "input_mode": "script", "request_id": "text-only", "model_key": "text", "model_name": "text", "items": [{"source_path": "scene", "requirements": "A quiet room."}]}
        created = self.core.create("owner", body)
        self.tid = created["thread_id"]
        self.action("confirm_reverse")
        self.core.tick()
        self.assertEqual(self.studio.calls[0][0]["images"], [])
        self.assertNotIn("LoadImage", str(created["batch"]["items"][0]["effective_workflow"]["prompt"]))

    def test_source_recipe_save_as_is_immutable_and_owner_scoped(self):
        first = self.core.save_recipe("owner", {"source_recipe_id": "test_recipe", "name": "One"})["recipe"]
        second = self.core.save_recipe("owner", {"source_recipe_id": first["id"], "name": "Two", "input_mode": "prompt"})["recipe"]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["source_recipe_id"], first["id"])
        self.assertEqual(first["template"]["body"], RECIPE["template"]["body"])
        self.assertIsNone(second["template"])
        self.assertEqual(self.core._recipe("owner", first["id"]), first)
        with self.assertRaises(LookupError):
            self.core.save_recipe("other", {"source_recipe_id": first["id"]})
        self.assertTrue(self.core.workflow_referenced(first["workflow_id"]))

    def test_save_as_explicit_rule_selection_beats_inherited_template(self):
        self.app.studio.catalog = SimpleNamespace(get_body=lambda key: "Selected new body " + key)
        saved = self.core.save_recipe("owner", {"source_recipe_id": "test_recipe", "template_id": "new-rule"})["recipe"]
        self.assertEqual(saved["template"]["id"], "new-rule")
        self.assertEqual(saved["template"]["body"], "Selected new body new-rule")
        with self.assertRaisesRegex(ValueError, "冲突"):
            self.core.save_recipe("owner", {"source_recipe_id": "test_recipe", "template_id": "new-rule", "template": RECIPE["template"]})

    def test_clone_projection_has_no_execution_authority_and_capabilities_are_truthful(self):
        self.create()
        self.reverse_done()
        self.action("confirm_generate")
        self.core.tick()
        self.comfy.records["native-1"].update(status="failed")
        self.core.tick()
        item = self.batch()["items"][0]
        self.assertFalse(item["edit_capabilities"]["prompt"])
        self.assertTrue(item["edit_capabilities"]["included"])
        draft = self.core.clone_draft("owner", self.tid)["draft"]
        self.assertEqual(draft["source_thread_id"], self.tid)
        self.assertEqual(draft["items"][0]["source_item_id"], item["id"])
        for key in ("request_id", "confirmations", "revision", "target", "owner_id", "model", "recipe", "generate", "reverse", "outputs"):
            self.assertNotIn(key, draft)
            self.assertNotIn(key, draft["items"][0])
        draft["request_id"] = "cloned"
        cloned = self.core.create("owner", draft)["batch"]
        self.assertEqual(cloned["items"][0]["generate"]["attempts"], [])
        self.assertEqual(cloned["confirmations"], {})

    def test_nine_images_reach_prompt_model_without_normal_studio_limit_change(self):
        from naiba.studio.images import validate_image_refs, StudioImageError
        from naiba.studio.service import StudioService
        self.patch.stop()
        self.app.studio.prepare_generation = lambda b, **kw: StudioService.prepare_generation(SimpleNamespace(app=self.app), b, **kw)
        body = {"recipe_id": "minimax_h3_r2v", "request_id": "nine", "model_key": "vision", "model_name": "vision", "items": [{"source_path": "scene", "requirements": "A scene.", "media": [{"role": f"picture_{n}", "path": str(self.image)} for n in range(1, 10)]}]}
        created = self.core.create("owner", body)
        self.tid = created["thread_id"]
        self.action("confirm_reverse")
        self.core.tick()
        self.assertEqual(len(self.studio.calls[0][0]["images"]), 9)
        with self.assertRaises(StudioImageError):
            validate_image_refs(self.data, [str(self.image)] * 9)

    def test_audio_reference_verified_and_only_description_reaches_prompt_model(self):
        import wave
        self.patch.stop()
        audio = self.image.with_name("voice.wav")
        with wave.open(str(audio), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(b"\x00\x00" * 16000)
        self.profile["supports_images_explicit"] = False
        body = {"recipe_id": "minimax_h3_r2v", "request_id": "audio", "model_key": "text", "model_name": "text", "items": [{"source_path": "scene", "requirements": "Use the voice.", "media": [{"role": "audio_1", "path": str(audio), "description": "A calm low voice."}]}]}
        created = self.core.create("owner", body)
        self.tid = created["thread_id"]
        media = created["batch"]["items"][0]["media"][0]
        self.assertEqual(media["reference"], "<Audio 1>")
        self.assertEqual(media["media"], "audio")
        self.assertGreater(media["duration_seconds"], 0)
        self.assertEqual(len(media["sha256"]), 64)
        self.action("confirm_reverse")
        self.core.tick()
        payload = self.studio.calls[0][0]
        self.assertEqual(payload["images"], [])
        self.assertIn("A calm low voice.", payload["content"])
        self.assertIn("没有观看或听取原件", payload["content"])
        self.assertNotIn(str(audio), payload["content"])
        fake = self.image.with_name("fake.wav")
        fake.write_bytes(b"not audio")
        with self.assertRaises(ValueError):
            self.core._media([{"role": "audio_1", "path": str(fake)}], "bad", {"audio_1"}, [{"role": "audio_1", "media": "audio"}])

    def test_video_snapshot_has_typed_chain_and_rejects_fake_extension(self):
        import av
        self.patch.stop()
        video = self.image.with_name("clip.mp4")
        with av.open(str(video), "w") as out:
            stream = out.add_stream("mpeg4", rate=24)
            stream.width, stream.height, stream.pix_fmt = 16, 16, "yuv420p"
            for _ in range(24):
                frame = av.VideoFrame.from_image(Image.new("RGB", (16, 16), "blue"))
                for packet in stream.encode(frame):
                    out.mux(packet)
            for packet in stream.encode():
                out.mux(packet)
        body = {"recipe_id": "minimax_h3_r2v", "input_mode": "prompt", "request_id": "video", "items": [{"source_path": "scene", "prompt": "Use <Video 1> as the movement reference.", "media": [{"role": "video_1", "path": str(video)}]}]}
        created = self.core.create("owner", body)
        self.tid = created["thread_id"]
        item = created["batch"]["items"][0]
        self.assertEqual(item["media"][0]["media"], "video")
        self.assertEqual(item["media"][0]["reference"], "<Video 1>")
        self.assertEqual(item["media"][0]["width"], 16)
        self.assertEqual(item["effective_workflow"]["prompt"]["136"]["inputs"]["ref_videos.ref_video_0"], ["recipe_ref_video_1_components", 0])
        self.action("confirm_generate")
        self.core.tick()
        self.assertEqual(self.comfy.calls[0][0]["media_inputs"][0]["media"], "video")
        fake = video.with_name("fake.mp4")
        fake.write_bytes(self.image.read_bytes())
        with self.assertRaises(ValueError):
            self.core._media([{"role": "video_1", "path": str(fake)}], "bad", {"video_1"}, [{"role": "video_1", "media": "video"}])

    def test_nonexistent_reference_blocked_before_generation(self):
        body = self.body()
        body["input_mode"] = "prompt"
        body["items"][0]["prompt"] = "Use <Video 1>."
        self.tid = self.core.create("owner", body)["thread_id"]
        with self.assertRaisesRegex(ValueError, "媒体"):
            self.action("confirm_generate")
        self.assertEqual(self.comfy.calls, [])

    def test_custom_recipe_defaults_saved_and_merged_without_mutating_workflow(self):
        self.workflow_store.save_workflow({"id": "custom-workflow", "name": "Custom", "prompt": copy.deepcopy(GRAPH), "params": []})
        saved = self.core.save_recipe("owner", {"name": "Custom", "workflow_id": "custom-workflow", "workflow_fingerprint": digest(GRAPH), "prompt_slot": {"node_id": "1", "field_name": "text"}, "media_slots": RECIPE["media_slots"], "template_id": "test_template", "overrides": [{"node_id": "2", "field_name": "steps", "value": 30}]})["recipe"]
        body = self.body()
        body["recipe_id"] = saved["id"]
        created = self.core.create("owner", body)["batch"]
        self.assertEqual(created["overrides"], saved["overrides"])
        self.assertEqual(self.workflow_store.get_workflow("custom-workflow")["prompt"], GRAPH)

    def test_custom_recipe_requires_current_authoritative_fingerprint(self):
        self.workflow_store.save_workflow({"id": "custom-workflow", "name": "Custom", "prompt": copy.deepcopy(GRAPH), "params": []})
        body = {"workflow_id": "custom-workflow", "prompt_slot": {"node_id": "1", "field_name": "text"}, "media_slots": RECIPE["media_slots"], "template_id": "test_template"}
        for fingerprint in (None, "", "incorrect"):
            with self.subTest(fingerprint=fingerprint), self.assertRaises(ValueError):
                self.core.save_recipe("owner", {**body, "workflow_fingerprint": fingerprint})
        stale_hash = digest(GRAPH)
        changed = copy.deepcopy(GRAPH)
        changed["2"]["inputs"]["steps"] = 31
        self.workflow_store.save_workflow({"id": "custom-workflow", "name": "Custom", "prompt": changed, "params": []})
        with self.assertRaisesRegex(ValueError, "stale workflow_fingerprint"):
            self.core.save_recipe("owner", {**body, "workflow_fingerprint": stale_hash})
        self.assertEqual(self.core.store.definitions("owner"), [])
        saved = self.core.save_recipe("owner", {**body, "workflow_fingerprint": digest(changed)})["recipe"]
        self.assertEqual(saved["workflow"], changed)

    def test_custom_changed_sampler_rejected_instead_of_silent_default(self):
        graph = copy.deepcopy(GRAPH)
        graph["2"]["inputs"]["sampler_name"] = "euler"
        self.workflow_store.save_workflow({"id": "custom-workflow", "name": "Custom", "prompt": graph, "params": []})
        body = {"workflow_id": "custom-workflow", "workflow_fingerprint": digest(graph), "prompt_slot": {"node_id": "1", "field_name": "text"}, "media_slots": RECIPE["media_slots"], "template_id": "test_template", "overrides": [{"node_id": "2", "field_name": "sampler_name", "value": "dpmpp_2m"}]}
        with self.assertRaisesRegex(ValueError, "sampler_name.*不能静默"):
            self.core.save_recipe("owner", body)
        self.assertEqual(self.core.store.definitions("owner"), [])
        self.assertEqual(self.workflow_store.get_workflow("custom-workflow")["prompt"]["2"]["inputs"]["sampler_name"], "euler")

    def test_r2v_review_exposes_actual_pruned_graph_without_installing(self):
        self.patch.stop()
        body = self.body()
        body["recipe_id"] = "minimax_h3_r2v"
        body["items"][0]["media"][0]["role"] = "picture_1"
        body["items"][0]["prompt"] = "user supplied prompt"
        created = self.core.create("owner", body)
        self.tid = created["thread_id"]
        item = created["batch"]["items"][0]
        graph = item["effective_workflow"]["prompt"]
        self.assertIn("137", graph)
        self.assertNotIn("139", graph)
        self.assertNotIn("recipe_ref_image_3", graph)
        self.assertIsNone(self.workflow_store.get_workflow(item["effective_workflow"]["id"]))
        self.batch()
        self.assertIsNone(self.workflow_store.get_workflow(item["effective_workflow"]["id"]))
        self.action("confirm_generate")
        self.core.tick()
        payload = self.comfy.calls[0][0]
        self.assertEqual(payload["workflow_id"], item["effective_workflow"]["id"])
        self.assertEqual(self.workflow_store.get_workflow(payload["workflow_id"])["prompt"], graph)
        self.assertEqual(self.comfy.calls[0][2]["workflow_fingerprint"], item["effective_workflow"]["fingerprint"])

    def test_integer_override_rejects_fraction_and_unsafe_precision(self):
        for value in (20.9, 20.0, 2 ** 53 + 1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.core._overrides(RECIPE, [{"node_id": "2", "field_name": "steps", "value": value}])
        value = 2 ** 53 - 1
        self.assertEqual(self.core._overrides(RECIPE, [{"node_id": "2", "field_name": "steps", "value": value}])[0]["value"], value)

    def test_custom_selector_rejects_ambiguity_without_patched_validator(self):
        batch = self.create()
        batch["recipe"]["mode"] = "custom"
        item = batch["items"][0]
        self.validator.stop()
        batch["template"]["result_rule"] = {"type": "text"}
        for raw in ('["prompt one","prompt two"]', '{"prompt":"one"}', 'Prompt 3: candidate three\nPrompt 4: candidate four', '提示词三：候选甲\n提示词四：候选乙'):
            with self.subTest(raw=raw):
                result = self.core._validate_output(batch, item, raw)
                self.assertFalse(result["valid"])
                self.assertEqual(result["raw"], raw)
                self.assertEqual(result["text"], "")
        self.assertTrue(self.core._validate_output(batch, item, "A single clear cinematic scene.")["valid"])
        batch["template"]["result_rule"] = {"type": "json_field", "field": "prompt"}
        self.assertFalse(self.core._validate_output(batch, item, '{"prompt":"first","prompt":"second"}')["valid"])
        self.assertTrue(self.core._validate_output(batch, item, '{"prompt":"one clear scene"}')["valid"])

    def test_pending_remote_cancellation_blocks_terminal_projection(self):
        self.create()
        self.reverse_done()
        self.action("confirm_generate")
        self.core.tick()
        self.action("cancel")
        self.comfy.records["native-1"].update(status="interrupted", checkpoint={"cancellation": {"state": "pending_remote"}})
        self.core.tick()
        self.assertTrue(self.batch()["cancel_pending"])
        self.assertEqual(self.service.get_thread("owner", self.tid).status, "stopping")
        self.comfy.records["native-1"].update(status="cancelled", checkpoint={"cancellation": {"state": "confirmed"}})
        self.core.tick()
        self.assertFalse(self.batch()["cancel_pending"])

    def test_missing_completed_artifact_only_reopens_for_collect(self):
        self.test_output_file_completion()
        output = Path(self.batch()["items"][0]["outputs"][0]["path"])
        output.unlink()
        batch = self.batch()
        self.assertEqual(batch["status"], "partial")
        self.assertEqual(batch["items"][0]["generate"]["status"], "collection_failed")
        self.assertEqual(self.service.get_thread("owner", self.tid).status, "completed")
        self.action("retry_collect")
        self.core.tick()
        self.assertEqual(len(self.comfy.records), 1)
        self.assertEqual(self.service.get_thread("owner", self.tid).status, "active")

    def test_corrupt_image_artifact_not_complete(self):
        self.create()
        self.reverse_done()
        self.action("confirm_generate")
        self.core.tick()
        output = self.comfy.task_output_dir("native-1") / "out.png"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"<html>not a PNG</html>")
        self.comfy.records["native-1"].update(status="done", result={"outputs": ["out.png"], "completed": [0]}, progress={"done": 1})
        self.core.tick()
        self.assertEqual(self.batch()["status"], "partial")

    def test_raw_runtime_result_not_cleaned_native_text_is_selected(self):
        self.create()
        self.action("confirm_reverse")
        self.core.tick()
        self.studio.records["native-1"].update(status="done", result="good prompt", raw_result="unapproved explanation</think>good prompt", raw_result_source="runtime_callback")
        self.core.tick()
        self.assertEqual(self.batch()["items"][0]["reverse"]["status"], "needs_review")
        self.assertEqual(self.batch()["items"][0]["reverse"]["attempts"][0]["raw"], "unapproved explanation</think>good prompt")

    def test_custom_prompt_binding_uses_existing_text_field_not_downstream_whitelist(self):
        self.core._validate_binding(GRAPH, {"node_id": "1", "field_name": "text"})
        graph = {"1": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "base"}},
                 "2": {"class_type": "PromptRouter", "inputs": {"text": ["1", 0]}},
                 "3": {"class_type": "CLIPTextEncode", "inputs": {"text": ["2", 0]}},
                 "4": {"class_type": "KSampler", "inputs": {"positive": ["3", 0], "steps": 20}}}
        self.workflow_store.save_workflow({"id": "routed-workflow", "name": "Routed", "prompt": graph, "params": []})
        saved = self.core.save_recipe("owner", {"workflow_id": "routed-workflow", "workflow_fingerprint": digest(graph),
                                                "prompt_slot": {"node_id": "1", "field_name": "value"},
                                                "media_slots": [], "input_mode": "prompt"})["recipe"]
        self.assertEqual(saved["prompt_slot"], {"node_id": "1", "field_name": "value"})
        self.assertEqual(saved["workflow"], graph)
        for slot in ({"node_id": "4", "field_name": "steps"},
                     {"node_id": "1", "field_name": "missing"},
                     {"node_id": "3", "field_name": "text"}):
            with self.subTest(slot=slot), self.assertRaisesRegex(ValueError, "文本字段"):
                self.core._validate_binding(graph, slot)


if __name__ == "__main__":
    unittest.main()
