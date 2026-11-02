# -*- coding: utf-8 -*-
"""Deterministic HTTP/isolation gates for the local recipe browser fixture.

Runs the fixture in a child process so its permanent socket audit hook cannot
change the networking policy of the rest of the unittest suite. No browser and
no runner mocks; all recipe mutations use the real app HTTP API.
"""
from __future__ import annotations

import copy
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "verify" / "_serve_recipe_ui.py"


class RecipeManifestPublicationTests(unittest.TestCase):
    def test_manifest_is_not_visible_during_partial_write(self):
        import ast
        import importlib.util
        from unittest import mock
        spec = importlib.util.spec_from_file_location("recipe_manifest_fixture", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        build_app = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "build_app")
        self.assertTrue(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id == "atomic_write_json" for node in ast.walk(build_app)))
        original_write = Path.write_text
        with tempfile.TemporaryDirectory(prefix="recipe-manifest-") as directory:
            target = Path(directory) / "fixture.json"
            payload = {"app_url": "http://127.0.0.1:1", "ready": "完整"}
            def delayed_write(path, data, *args, **kwargs):
                original_write(path, "", encoding="utf-8")
                self.assertFalse(target.exists(), "Ready manifest must not expose an empty file")
                return original_write(path, data, *args, **kwargs)
            with mock.patch.object(Path, "write_text", delayed_write):
                module.atomic_write_json(target, payload)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), payload)
            self.assertEqual(list(target.parent.glob("*.tmp")), [])


class RecipeUIFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="recipe-fixture-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "isolated"
        self.log = (Path(self.temp.name) / "server.log").open("w+b")
        self.addCleanup(self.log.close)
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--root", str(self.root), "--port", "0"],
            cwd=str(ROOT), env=env, stdout=self.log, stderr=subprocess.STDOUT,
        )
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            manifest = self.root / "fixture.json"
            if manifest.exists():
                self.fixture = json.loads(manifest.read_text(encoding="utf-8"))
                self.base = self.fixture["app_url"]
                return
            if self.process.poll() is not None:
                break
            time.sleep(0.05)
        self.log.seek(0)
        self.fail("fixture did not start: " + self.log.read().decode("utf-8", "replace"))

    def stop(self):
        if self.process.poll() is None:
            if hasattr(self, "fixture"):
                try:
                    self.control("shutdown")
                    self.process.wait(timeout=15)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=10)

    def request(self, method, url, body=None, headers=None, raw=False):
        request = urllib.request.Request(
            url, data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})}, method=method,
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=15) as response:
                data = response.read()
                return response.status, data if raw else json.loads(data)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def api(self, method, path, body=None, **kwargs):
        return self.request(method, self.base + path, body, **kwargs)

    def control(self, operation="status", body=None):
        method = "GET" if operation == "status" else "POST"
        status, result = self.request(method, self.fixture["control_url"] + "/" + operation,
                                      body, {"X-Fixture-Token": self.fixture["control_token"]})
        self.assertEqual(status, 200, result)
        return result

    def assert_decodable_output(self, data):
        if self.fixture["output_type"] == "video":
            encoder = shutil.which("ffmpeg")
            self.assertIsNotNone(encoder)
            self.assertIn(b"ftyp", data[:32])
            clip = Path(self.temp.name) / "downloaded-output.mp4"
            clip.write_bytes(data)
            decoded = subprocess.run([encoder, "-nostdin", "-hide_banner", "-i", str(clip),
                                      "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"],
                                     cwd=str(ROOT), capture_output=True, timeout=15)
            self.assertEqual(decoded.returncode, 0, decoded.stderr.decode("utf-8", "replace"))
            metadata = decoded.stderr.decode("utf-8", "replace")
            self.assertIn("Video: h264", metadata)
            self.assertIn("Audio: aac", metadata)
            self.assertIn("160x120", metadata)
            self.assertIn("Duration: 00:00:01.00", metadata)
        else:
            from PIL import Image
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.size, (256, 192))

    def batch(self, tid):
        status, result = self.api("GET", f"/api/super-agent/threads/{tid}/recipe")
        self.assertEqual(status, 200, result)
        return result["batch"]

    def action(self, tid, action, **values):
        batch = self.batch(tid)
        body = {"action": action, "revision": batch["revision"], "revision_hash": batch["revision_hash"],
                "request_id": "test-" + str(time.time_ns()), **values}
        status, result = self.api("POST", f"/api/super-agent/threads/{tid}/recipe/actions", body)
        self.assertEqual(status, 200, result)
        return result["batch"]

    @staticmethod
    def summary(batch):
        return {"status": batch.get("status"), "error": batch.get("error"), "items": [
            {"id": i["id"], **{phase: {"status": i[phase]["status"], "errors": [a.get("error") or (a.get("validation") or {}).get("errors")
                                                                                   for a in i[phase]["attempts"]]}
                              for phase in ("reverse", "generate")}} for i in batch["items"]]}

    def wait_batch(self, tid, statuses, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            batch = self.batch(tid)
            if batch["status"] in statuses:
                return batch
            time.sleep(0.1)
        self.fail("recipe did not settle: " + json.dumps(batch, ensure_ascii=False)[:3000])

    def create_single(self, mode="i2v", provided=False):
        payload = copy.deepcopy(next(p for p in self.fixture["create_payloads"] if p["recipe_id"].endswith("_" + mode)))
        payload["request_id"] = "test-create-" + str(time.time_ns())
        payload["items"] = payload["items"][:1]
        if provided:
            # Human-authored single prompt is an explicit review input, not a fake result.
            payload["items"][0]["prompt"] = "A blue and gold graphic slowly moves closer."
        status, result = self.api("POST", "/api/super-agent/recipe-batches", payload)
        self.assertEqual(status, 201, result)
        return result["thread_id"]

    def test_startup_is_isolated_credential_free_and_never_dispatches(self):
        config = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(len(config["providers"]), 1)
        provider = config["providers"][0]
        self.assertEqual(provider["api_key"], "")
        self.assertEqual(provider["base_url"], self.fixture["model_url"])
        self.assertEqual(config["comfyui_url"], self.fixture["comfyui_url"])
        self.assertEqual(config["mcp_servers"], [])
        self.assertEqual(config["skills_dirs"], [])
        self.assertFalse(config["proxy"]["use_system_fallback"])
        self.assertEqual(config["default_model_key"], self.fixture["model_key"])
        self.assertEqual(Path(config["data_dir"]).resolve(), (self.root / "data").resolve())
        self.assertEqual(Path(config["workspace_dir"]).resolve(), (self.root / "workspace").resolve())
        self.assertEqual(Path(self.fixture["root"]).resolve(), self.root.resolve())
        if shutil.which("ffmpeg"):
            self.assertEqual(self.fixture["output_type"], "video")
            self.assertEqual(self.fixture["output_mime"], "video/mp4")
            self.assertTrue(self.fixture["simulated_media"]["decode_verified"])
        else:
            self.assertEqual(self.fixture["output_type"], "image")
        self.assertTrue(self.fixture["simulated_media"]["simulated"])
        for image in self.fixture["images"]:
            self.assertTrue(Path(image["path"]).resolve().is_relative_to((self.root / "data" / "uploads").resolve()))
        time.sleep(0.7)  # allow at least one real coordinator tick
        stats = self.control()
        self.assertEqual(stats["counts"]["model"], 0)
        self.assertEqual(stats["counts"]["prompt"], 0)
        self.assertEqual(stats["counts"]["upload"], 0)
        self.assertEqual(stats["blocked_connections"], [])
        self.assertEqual(len(self.fixture["drafts"]), 2)
        for draft in self.fixture["drafts"]:
            batch = self.batch(draft["thread_id"])
            self.assertEqual(batch["status"], "draft")
            self.assertEqual(batch["confirmations"], {})
            self.assertTrue(all(not item["reverse"]["attempts"] and not item["generate"]["attempts"] for item in batch["items"]))
        status, health = self.api("GET", "/api/comfyui/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        status, html = self.api("GET", "/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b"Cat Chat", html)

    def test_http_create_reverse_review_generate_and_decodable_collection(self):
        for mode in ("i2v", "r2v"):
            with self.subTest(mode=mode):
                tid = self.create_single(mode)
                self.assertEqual(self.batch(tid)["status"], "draft")
                before = self.control()["counts"]["prompt"]
                self.action(tid, "confirm_reverse")
                batch = self.wait_batch(tid, {"review", "needs_review"})
                self.assertEqual(batch["status"], "review", self.summary(batch))
                item = batch["items"][0]
                self.assertEqual(item["reverse"]["status"], "done", item)
                studio_id = item["reverse"]["attempts"][-1]["task_id"]
                status, studio = self.api("GET", "/api/studio/tasks/" + studio_id)
                self.assertEqual(status, 200, studio)
                self.assertEqual(studio["task"]["status"], "done")
                self.assertIn("overall_soundscape:", studio["task"]["result"])
                self.assertEqual(self.control()["counts"]["prompt"], before, "reverse/review must not generate")
                self.action(tid, "confirm_generate")
                batch = self.wait_batch(tid, {"completed", "partial", "needs_review"})
                self.assertEqual(batch["status"], "completed", self.summary(batch))
                generated = batch["items"][0]
                self.assertTrue(generated["outputs"], generated)
                comfy_id = generated["generate"]["attempts"][-1]["task_id"]
                status, task = self.api("GET", "/api/comfyui/tasks/" + comfy_id)
                self.assertEqual(status, 200)
                self.assertEqual(task["task"]["status"], "completed")
                output = task["task"]["result"]["outputs"][0]
                status, data = self.api("GET", f"/api/comfyui/tasks/{comfy_id}/files/{output}", raw=True)
                self.assertEqual(status, 200)
                self.assert_decodable_output(data)
                self.assertEqual(self.control()["counts"]["prompt"], before + 1)
        stats = self.control()
        self.assertEqual(stats["counts"]["model"], 2)
        self.assertTrue(all(request["stream"] for request in stats["model_requests"]))
        self.assertEqual(stats["blocked_connections"], [])

    def test_failure_flags_and_hold_use_real_native_tasks(self):
        tid = self.create_single()
        self.control("control", {"fail_reverse": True})
        self.action(tid, "confirm_reverse")
        batch = self.wait_batch(tid, {"needs_review"})
        self.assertEqual(batch["items"][0]["reverse"]["status"], "failed")
        self.assertEqual(self.control()["counts"]["prompt"], 0)
        self.control("control", {"fail_reverse": False})
        self.action(tid, "retry_reverse")
        self.assertEqual(self.wait_batch(tid, {"review", "needs_review"})["status"], "review")
        self.control("control", {"hold": True})
        self.action(tid, "confirm_generate")
        deadline = time.monotonic() + 15
        while self.control()["counts"]["prompt"] == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.control()["counts"]["prompt"], 1)
        time.sleep(0.2)
        self.assertEqual(self.batch(tid)["status"], "generation_running")
        self.control("control", {"hold": False})
        self.assertEqual(self.wait_batch(tid, {"completed", "partial"})["status"], "completed")

    def test_generation_rejection_and_download_failure_do_not_hide_submits(self):
        for flag in ("fail_generate", "fail_download"):
            with self.subTest(flag=flag):
                self.control("control", {"fail_generate": False, "fail_download": False, flag: True})
                tid = self.create_single(provided=True)
                before = self.control()["counts"]["prompt"]
                self.action(tid, "confirm_generate")
                batch = self.wait_batch(tid, {"partial", "needs_review"})
                expected = "collection_failed" if flag == "fail_download" else "failed"
                item = batch["items"][0]
                self.assertEqual(item["generate"]["status"], expected, self.summary(batch))
                self.assertEqual(self.control()["counts"]["prompt"], before + 1)
                self.assertEqual(self.control()["counts"]["model"], 0)
                if flag == "fail_download":
                    self.assertEqual(item["capabilities"], ["retry_collect"])
                    native_id = item["generate"]["attempts"][-1]["task_id"]
                    retry = {"action": "retry_generate", "revision": batch["revision"],
                             "revision_hash": batch["revision_hash"], "item_ids": [item["id"]],
                             "request_id": "deny-regeneration-" + str(time.time_ns())}
                    status, denied = self.api("POST", f"/api/super-agent/threads/{tid}/recipe/actions", retry)
                    self.assertEqual(status, 400, denied)
                    self.assertIn("retry_collect", denied["error"])
                    self.assertEqual(self.control()["counts"]["prompt"], before + 1)
                    self.control("control", {"fail_download": False})
                    self.action(tid, "retry_collect", item_ids=[item["id"]])
                    recovered = self.wait_batch(tid, {"completed", "partial", "needs_review"})
                    self.assertEqual(recovered["status"], "completed", self.summary(recovered))
                    output_item = recovered["items"][0]
                    self.assertEqual(output_item["generate"]["status"], "done")
                    self.assertTrue(output_item["outputs"])
                    self.assertEqual(output_item["generate"]["attempts"][-1]["task_id"], native_id)
                    status, task = self.api("GET", "/api/comfyui/tasks/" + native_id)
                    self.assertEqual(status, 200, task)
                    self.assertEqual(task["task"]["status"], "completed")
                    output = task["task"]["result"]["outputs"][0]
                    status, data = self.api("GET", f"/api/comfyui/tasks/{native_id}/files/{output}", raw=True)
                    self.assertEqual(status, 200)
                    self.assert_decodable_output(data)
                    self.assertEqual(self.control()["counts"]["prompt"], before + 1,
                                     "retry_collect must collect the accepted prompt without regenerating")
                    self.assertEqual(self.control()["counts"]["model"], 0)
        self.assertGreaterEqual(self.control()["counts"]["view"], 1)

    def test_controls_are_authenticated_validate_flags_and_only_reset_explicitly(self):
        status, _ = self.request("GET", self.fixture["control_url"] + "/status")
        self.assertEqual(status, 403)
        status, _ = self.request("POST", self.fixture["control_url"] + "/control", {"fail_generate": True})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", self.fixture["control_url"] + "/control", {"hold": "false"},
                                 {"X-Fixture-Token": self.fixture["control_token"]})
        self.assertEqual(status, 400)
        status, _ = self.request("POST", self.fixture["comfyui_url"] + "/prompt", {"prompt": self.fixture["custom_workflow"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.control()["counts"]["prompt"], 1)
        self.control("control", {"fail_generate": False})
        self.assertEqual(self.control()["counts"]["prompt"], 1)
        stats = self.control("reset-stats", {})
        self.assertTrue(all(value == 0 for value in stats["counts"].values()))
        self.assertEqual(len(stats["prompt_ids"]), 1, "stats reset must not erase accepted jobs")

    def test_custom_workflow_is_editable_and_exposes_safe_prompt_slot(self):
        wid = self.fixture["custom_workflow_id"]
        status, result = self.api("GET", "/api/comfyui/workflows/" + wid)
        self.assertEqual(status, 200)
        entry = result["workflow"]
        self.assertFalse(entry.get("recipe_immutable"))
        self.assertEqual(entry["prompt"], self.fixture["custom_workflow"])
        slot = self.fixture["custom_prompt_slot"]
        status, result = self.api("POST", f"/api/comfyui/workflows/{wid}/params", {"visible": [slot]})
        self.assertEqual(status, 200, result)
        self.assertEqual(self.control()["counts"]["prompt"], 0)

    def test_image_graph_still_returns_png_when_video_fixture_is_enabled(self):
        status, result = self.request("POST", self.fixture["comfyui_url"] + "/prompt", {
            "prompt": {"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "local-image"}}}})
        self.assertEqual(status, 200, result)
        pid = result["prompt_id"]
        status, history = self.request("GET", self.fixture["comfyui_url"] + "/history/" + pid)
        self.assertEqual(status, 200)
        output = history[pid]["outputs"]["92"]["images"][0]
        self.assertTrue(output["filename"].endswith(".png"))
        status, data = self.request("GET", self.fixture["comfyui_url"] + "/view?filename=" + output["filename"], raw=True)
        self.assertEqual(status, 200)
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            self.assertEqual(image.format, "PNG")
        # Explicit image mode never invokes an encoder or creates a media directory.
        import importlib.util
        spec = importlib.util.spec_from_file_location("recipe_fixture_image_mode", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        data, metadata = module.simulated_media(Path(self.temp.name) / "unused", "png")
        self.assertEqual(data, b"")
        self.assertEqual(metadata["type"], "image")
        self.assertEqual(metadata["reason"], "explicit PNG mode")
        self.assertFalse((Path(self.temp.name) / "unused").exists())

    def test_existing_root_is_refused_without_touching_its_contents(self):
        sentinel = self.root / "do-not-touch.txt"
        sentinel.write_bytes(b"untouched")
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root), "--port", "0"],
                                cwd=str(ROOT), capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"new empty directory", result.stderr)
        self.assertEqual(sentinel.read_bytes(), b"untouched")

    def test_socket_guard_blocks_external_tcp_udp_and_dns_before_dispatch(self):
        code = (
            "import importlib.util, pathlib, socket; "
            "spec=importlib.util.spec_from_file_location('fixture', pathlib.Path('verify/_serve_recipe_ui.py')); "
            "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); blocked=m.install_offline_guard(); "
            "exec(\"for action in [lambda: socket.socket().connect(('192.0.2.1', 9)), "
            "lambda: socket.getaddrinfo('example.invalid', 80), "
            "lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'x', ('192.0.2.1', 9))]:\\n"
            " try: action()\\n except PermissionError: pass\\n else: raise AssertionError('guard permitted outbound network')\"); "
            "assert len(blocked)==3, blocked; assert m.loopback_host('127.0.0.1'); assert m.loopback_host('::1')"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
