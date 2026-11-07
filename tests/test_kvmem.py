# -*- coding: utf-8 -*-
"""KVMem adapter safety and lifecycle protocol tests."""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from naiba.kvmem import KVMemClient, KVMemError, validate_manager_url


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


if __name__ == "__main__":
    unittest.main()
