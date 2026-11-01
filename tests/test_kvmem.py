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

    def log_message(self, *_args):
        return

    def do_GET(self):
        self.__class__.calls.append(("GET", self.path, dict(self.headers)))
        if self.path == "/api/launcher/info":
            payload = {"service": "KVMem", "protocol_version": "1", "token": self.token, "draft": {"backend": "ninfer", "model": "model.gguf", "port": 18200, "engine_params": {"ctx": 8192}}}
        elif self.path == "/api/launcher/status":
            payload = {"status": self.state, "port": 18200}
        else:
            payload = {"error": "not found"}
        self._send(payload)

    def do_POST(self):
        self.__class__.calls.append(("POST", self.path, dict(self.headers)))
        if self.headers.get("X-KVMem-Token") != self.token:
            self._send({"error": "unauthorized"}, 401)
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
        post_headers = [headers for method, path, headers in _Controller.calls if method == "POST"]
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


if __name__ == "__main__":
    unittest.main()
