# -*- coding: utf-8 -*-
"""KVMem adapter safety and lifecycle protocol tests."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit

from naiba.kvmem import (KVMemClient, KVMemError, last_start_path, read_last_start,
                         validate_manager_url, write_last_start)


class _Controller(BaseHTTPRequestHandler):
    calls = []
    token = "temporary-secret"
    state = "stopped"
    draft = {"backend": "ninfer", "model": "model.gguf", "port": 18200, "engine_params": {"ctx": 8192},
             "schema_version": 1, "parameters": {"Model": "model.gguf", "Backend": "ninfer"}}
    fail_start_reason = ""

    def log_message(self, *_args):
        return

    def do_GET(self):
        self.__class__.calls.append(("GET", self.path, dict(self.headers), b""))
        if self.path == "/api/launcher/info":
            payload = {"service": "KVMem", "protocol_version": "1", "token": self.token, "draft": self.draft}
        elif self.path == "/api/launcher/status":
            payload = {"status": self.state, "port": 18200}
        else:
            payload = {"error": "not found"}
        self._send(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.__class__.calls.append(("POST", self.path, dict(self.headers), body))
        if self.headers.get("X-KVMem-Token") != self.token:
            self._send({"error": "unauthorized"}, 401)
            return
        if self.path.endswith("/start") and self.__class__.fail_start_reason:
            self._send({"error": self.__class__.fail_start_reason}, 400)
            return
        self.__class__.state = "ready" if self.path.endswith("/start") else "stopped"
        self._send({"status": self.__class__.state})

    def _send(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class KVMemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _Controller.calls = []
        _Controller.state = "stopped"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Controller)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=3)

    def test_manager_url_only_allows_loopback(self):
        self.assertEqual(validate_manager_url("http://127.0.0.1:18199"), "http://127.0.0.1:18199")
        for value in ("http://192.168.1.10:18199", "https://example.com", "http://127.0.0.1:18199/x"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError): validate_manager_url(value)

    def test_info_status_start_stop_and_token_not_in_payload(self):
        client = KVMemClient(self.url, "http://127.0.0.1:18200/v1")
        status = client.status()
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(status["backend"], "ninfer")
        self.assertNotIn("temporary-secret", json.dumps(status))
        client.start()
        client.stop()
        post_headers = [headers for method, path, headers, _body in _Controller.calls if method == "POST"]
        self.assertTrue(post_headers)
        self.assertTrue(all(next((value for key, value in headers.items() if key.lower() == "x-kvmem-token"), "") == "temporary-secret" for headers in post_headers))

    def test_readiness_checks_model_and_health(self):
        client = KVMemClient(self.url, "http://127.0.0.1:18200/v1")
        # controller-only fake cannot satisfy inference health; failure must be structured.
        result = client.readiness("model.gguf")
        self.assertIn(result["status"], {"failed", "stopped"})

    def test_no_process_termination_primitive_in_adapter(self):
        import inspect
        source = inspect.getsource(KVMemClient)
        self.assertNotIn("taskkill", source.lower())
        self.assertNotIn("psutil", source.lower())

    def test_start_posts_console_draft_unchanged(self):
        client = KVMemClient(self.url, "http://127.0.0.1:18200/v1")
        result = client.start()
        self.assertTrue(result["accepted"])
        bodies = [body for method, path, _headers, body in _Controller.calls
                  if method == "POST" and path.endswith("/start")]
        self.assertTrue(bodies)
        self.assertEqual(json.loads(bodies[-1].decode("utf-8")), _Controller.draft)

    def test_start_without_selected_model_raises_readable_error(self):
        original = _Controller.draft
        _Controller.draft = {**original, "parameters": {"Model": "", "Backend": "ninfer"}}
        try:
            client = KVMemClient(self.url, "http://127.0.0.1:18200/v1")
            with self.assertRaises(KVMemError) as caught:
                client.start()
            self.assertIn("尚未选择模型", str(caught.exception))
        finally:
            _Controller.draft = original

    def test_start_surfaces_console_error_text(self):
        original = _Controller.fail_start_reason
        _Controller.fail_start_reason = "健康检查未通过：模型文件缺失"
        try:
            client = KVMemClient(self.url, "http://127.0.0.1:18200/v1")
            with self.assertRaises(KVMemError) as caught:
                client.start()
            message = str(caught.exception)
            self.assertIn("模型文件缺失", message)
            self.assertNotIn("不可达", message)
        finally:
            _Controller.fail_start_reason = original

    def test_start_restores_cached_model_after_controller_loses_selection(self):
        original = _Controller.draft
        _Controller.draft = {**original, "parameters": {"Model": "", "Backend": "ninfer", "Gpu": 0}}
        try:
            client = KVMemClient(self.url, "http://127.0.0.1:18200/v1")
            client.start(fallback={"Model": "remembered.gguf", "Backend": "ninfer", "Gpu": "1", "Worker": "w"})
            body = json.loads([body for method, path, _headers, body in _Controller.calls
                               if method == "POST" and path.endswith("/start")][-1])
            self.assertEqual(body["parameters"], {"Model": "remembered.gguf", "Backend": "ninfer", "Gpu": 0, "Worker": "w"})
            self.assertEqual(client.last_draft_parameters["Model"], "remembered.gguf")
            self.assertEqual(client.last_draft_parameters["Gpu"], "0")
            client.stop()
        finally:
            _Controller.draft = original

    def test_cache_does_not_mix_backend_or_another_selected_model(self):
        for parameters in ({"Backend": "ninfer", "Model": ""},
                           {"Backend": "polystrata", "Model": "new-model"}):
            original = dict(parameters)
            KVMemClient.fill_missing_parameters(parameters,
                {"Backend": "polystrata", "Model": "old-model", "Worker": "old-worker"})
            self.assertEqual(parameters, original)

    def test_cache_roundtrip_filters_secrets_and_handles_invalid_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = last_start_path(temporary)
            write_last_start(target, self.url, {"Backend": "ninfer", "Model": ""})
            self.assertFalse(target.exists())
            write_last_start(target, self.url, {"Backend": "ninfer", "Model": "m.gguf", "Gpu": 0,
                                                "token": "secret", "Extra": "private"})
            self.assertEqual(read_last_start(target), {self.url: {"Backend": "ninfer", "Model": "m.gguf", "Gpu": "0"}})
            before = target.stat().st_mtime_ns
            write_last_start(target, self.url, {"Backend": "ninfer", "Model": "m.gguf", "Gpu": 0})
            self.assertEqual(before, target.stat().st_mtime_ns)
            target.write_bytes(b'\xff')
            self.assertEqual(read_last_start(target), {})
            target.write_text("[]", encoding="utf-8")
            self.assertEqual(read_last_start(target), {})

    def test_cache_concurrent_manager_updates_are_not_lost(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = last_start_path(temporary)
            original_read = read_last_start
            barrier = threading.Barrier(12)
            errors = []

            def slow_read(path):
                snapshot = original_read(path)
                time.sleep(0.01)
                return snapshot

            def write(index):
                try:
                    barrier.wait(timeout=5)
                    write_last_start(target, f"http://127.0.0.1:{19000 + index}",
                                     {"Backend": "ninfer", "Model": f"model-{index}"})
                except BaseException as exc:
                    errors.append(repr(exc))

            with mock.patch("naiba.kvmem.read_last_start", side_effect=slow_read):
                threads = [threading.Thread(target=write, args=(index,)) for index in range(12)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=8)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(read_last_start(target)), 12)
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_cache_write_failure_is_nonfatal_and_removes_own_temporary(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = last_start_path(temporary)
            with mock.patch("naiba.kvmem.os.replace", side_effect=PermissionError("locked")):
                write_last_start(target, self.url, {"Backend": "ninfer", "Model": "m.gguf"})
            self.assertEqual(read_last_start(target), {})
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_app_remembers_running_parameters_then_restarts_from_empty_draft(self):
        from naiba.app import NaibaChatApp
        original, state = _Controller.draft, _Controller.state
        with tempfile.TemporaryDirectory() as temporary:
            client = KVMemClient(self.url, "http://127.0.0.1:18200/v1")
            client.last_draft_parameters = {"Backend": "ninfer", "Model": "remembered.gguf"}
            app = SimpleNamespace(paths=SimpleNamespace(data_dir=Path(temporary)),
                                  _kvmem_client=lambda _key: ({"id": "fixture"}, client))
            with mock.patch.object(client, "readiness", return_value={"status": "ready"}):
                payload, code = NaibaChatApp.api_kvmem_status(app, "fixture")
            self.assertEqual(code, 200)
            self.assertEqual(payload["provider_id"], "fixture")
            self.assertEqual(read_last_start(last_start_path(temporary))[self.url]["Model"], "remembered.gguf")
            _Controller.state = "stopped"
            _Controller.draft = {"schema_version": 1, "parameters": {"Backend": "ninfer", "Model": ""}}
            try:
                payload, code = NaibaChatApp.api_kvmem_start(app, "fixture", {"confirm": True})
                self.assertEqual(code, 202, payload)
                self.assertEqual(payload["summary"]["model"], "remembered.gguf")
                self.assertEqual(payload["status"], "starting")
                self.assertNotIn("temporary-secret", json.dumps(payload))
            finally:
                _Controller.draft, _Controller.state = original, state


if __name__ == "__main__":
    unittest.main()
