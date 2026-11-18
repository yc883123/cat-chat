# -*- coding: utf-8 -*-
"""KVMem adapter safety and lifecycle protocol tests.

测试桩按本机 KVMem 0.19.1 的真实响应结构搭（console_protocol / 内嵌 status /
active_config / ApiKey 脱敏），覆盖：状态取 active_config、完整参数启停、
后台拉起控制程序、按供应商+控制地址隔离的完整参数缓存、停止前保存与失败口径。
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit

from naiba.app import NaibaChatApp
from naiba.kvmem import (ConsoleLauncher, KVMemClient, KVMemError,
                         last_start_path, read_last_run, read_last_start,
                         validate_manager_url, wait_for_console,
                         write_last_run, write_last_start)

ACTIVE_PARAMETERS = {"Model": "model.gguf", "Backend": "ninfer", "Port": 18200,
                     "Context": 262144, "FastPrefill": True, "Gpu": "0"}
INFERENCE_KEY = "sk-infer-1"


class _Console(BaseHTTPRequestHandler):
    """同构替身：同一 handler 分跑控制程序端口与推理端口。"""
    calls = []
    token = "temporary-secret"
    state = "stopped"
    exit_code = None
    active = {"schema_version": 1, "parameters": dict(ACTIVE_PARAMETERS)}
    draft = {"schema_version": 1, "parameters": {"Model": "", "Backend": "ninfer"}}
    backends = ["ninfer"]
    backend_root = r"D:\kvmem-fixture"
    fail_start_reason = ""
    stop_state = "stopped"
    auth_required = False
    protocol_broken = False
    gpus = [{"uuid": "GPU-auto-1", "name": "RTX FIX", "memory_mib": "8000"}]

    def log_message(self, *_args):
        return

    @classmethod
    def reset(cls):
        cls.calls = []
        cls.state = "stopped"
        cls.exit_code = None
        cls.active = {"schema_version": 1, "parameters": dict(ACTIVE_PARAMETERS)}
        cls.draft = {"schema_version": 1, "parameters": {"Model": "", "Backend": "ninfer"}}
        cls.backends = ["ninfer"]
        cls.backend_root = r"D:\kvmem-fixture"
        cls.fail_start_reason = ""
        cls.stop_state = "stopped"
        cls.auth_required = False
        cls.protocol_broken = False
        cls.gpus = [{"uuid": "GPU-auto-1", "name": "RTX FIX", "memory_mib": "8000"}]

    @classmethod
    def active_public(cls):
        parameters = {key: value for key, value in cls.active["parameters"].items() if key != "ApiKey"}
        return {"schema_version": 1, "parameters": parameters}

    def _status(self):
        busy = self.state_field() in {"starting", "ready", "exited"}
        return {"state": self.state_field(), "pid": 4321 if self.state_field() in {"starting", "ready"} else None,
                "exit_code": self.exit_code,
                "active_config": self.active_public() if busy else None,
                "api_key_configured": self.auth_required, "url": None,
                "owner": "temporary" if busy else None}

    def state_field(self):
        return self.__class__.state

    def do_GET(self):
        self.__class__.calls.append(("GET", self.path, dict(self.headers), b""))
        path = urlsplit(self.path).path
        if path == "/api/launcher/info":
            payload = {"token": self.token, "console_protocol": 1,
                       "console_root": self.backend_root, "backend_root": self.backend_root,
                       "backends": list(self.backends), "draft": self.draft,
                       "gpus": list(self.gpus),
                       "status": self._status()}
            if self.__class__.protocol_broken:
                payload.pop("console_protocol")
            self._send(payload)
        elif path in {"/health", "/v1/models"}:
            if self.__class__.auth_required and self.headers.get("Authorization") != f"Bearer {INFERENCE_KEY}":
                self._send({"error": "unauthorized"}, 401)
                return
            payload = {"data": [{"id": "model.gguf"}]} if path == "/v1/models" else {"ok": True}
            self._send(payload)
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.__class__.calls.append(("POST", self.path, dict(self.headers), body))
        if self.headers.get("X-KVMem-Token") != self.token:
            self._send({"error": "Invalid console token"}, 403)
            return
        if self.path.endswith("/launcher/start"):
            if self.__class__.fail_start_reason:
                self._send({"error": self.__class__.fail_start_reason}, 400)
                return
            self.__class__.active = json.loads(body.decode("utf-8"))
            self.__class__.state = "starting"
            self._send(self._status())  # 真实控制程序回完整 status（state 在顶层）。
        elif self.path.endswith("/launcher/stop"):
            self.__class__.state = self.__class__.stop_state
            if self.__class__.state == "stopped":
                self.__class__.active = {"schema_version": 1, "parameters": {}}
                self.__class__.exit_code = None
            self._send(self._status())
        else:
            self._send({"error": "not found"}, 404)

    def _send(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class KVMemFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager = ThreadingHTTPServer(("127.0.0.1", 0), _Console)
        cls.inference = ThreadingHTTPServer(("127.0.0.1", 0), _Console)
        for server in (cls.manager, cls.inference):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.manager.server_address[1]}"
        cls.inference_url = f"http://127.0.0.1:{cls.inference.server_address[1]}/v1"
        # active_config 的端口与替身推理端口一致，否则一律 port_mismatch。
        ACTIVE_PARAMETERS["Port"] = cls.inference.server_address[1]

    @classmethod
    def tearDownClass(cls):
        for server in (cls.manager, cls.inference):
            server.shutdown(); server.server_close()
        _Console.reset()

    def setUp(self):
        _Console.reset()

    def start_posts(self):
        return [body for method, path, _headers, body in _Console.calls
                if method == "POST" and path.endswith("/launcher/start")]

    def stop_posts(self):
        return [body for method, path, _headers, body in _Console.calls
                if method == "POST" and path.endswith("/launcher/stop")]

    def profile(self, **extra):
        base = {"id": "kv", "kind": "local", "service_manager": "kvmem",
                "service_manager_url": self.url, "base_url": self.inference_url,
                "model": "model.gguf", "api_key": ""}
        base.update(extra)
        return base

    def make_app(self, data_dir, profile, launcher=None, active_tasks=()):
        """不跑 __init__ 的最小组合根：只装配 KVMem 链路真正触达的属性。"""
        app = NaibaChatApp.__new__(NaibaChatApp)
        app.paths = SimpleNamespace(data_dir=Path(data_dir))
        app.config = SimpleNamespace(profile=lambda _key: profile)
        app.kvmem_launcher = launcher if launcher is not None else ConsoleLauncher()
        app._kvmem_locks = {}
        app._kvmem_locks_guard = threading.Lock()
        app.storage = SimpleNamespace(list_background_tasks=lambda **_kw: list(active_tasks))
        return app


class KVMemProtocolTests(KVMemFixture):
    def test_manager_url_only_allows_loopback(self):
        self.assertEqual(validate_manager_url("http://127.0.0.1:18199"), "http://127.0.0.1:18199")
        for value in ("http://192.168.1.10:18199", "https://example.com", "http://127.0.0.1:18199/x"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError): validate_manager_url(value)

    def test_status_reads_active_config_over_empty_draft(self):
        _Console.state = "ready"
        _Console.draft = {"schema_version": 1, "parameters": {"Model": "", "Backend": "ninfer"}}
        client = KVMemClient(self.url, self.inference_url)
        payload = client.status()
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["model"], "model.gguf")
        self.assertEqual(payload["backend"], "ninfer")
        self.assertEqual(payload["actual_port"], _Console.active["parameters"]["Port"])
        self.assertNotIn("temporary-secret", json.dumps(payload))
        # 就绪后客户端持有完整运行参数（供自动记录），且不含 ApiKey。
        self.assertEqual(client.last_active_parameters["Context"], 262144)
        self.assertNotIn("ApiKey", client.last_active_parameters)

    def test_exited_maps_to_failed_with_exit_code(self):
        _Console.state = "exited"
        _Console.exit_code = 3221225786
        payload = KVMemClient(self.url, self.inference_url).status()
        self.assertEqual(payload["status"], "failed")
        self.assertIn("退出码", payload.get("error", ""))

    def test_start_posts_full_config_with_types_and_token(self):
        client = KVMemClient(self.url, self.inference_url)
        config = {"schema_version": 1, "parameters": {"Model": "model.gguf", "Backend": "ninfer",
                                                      "Port": 18200, "Context": 262144,
                                                      "FastPrefill": True, "ApiKey": INFERENCE_KEY}}
        result = client.start(config)
        self.assertTrue(result["accepted"])
        bodies = self.start_posts()
        self.assertEqual(json.loads(bodies[-1].decode("utf-8")), config)
        headers = [headers for method, path, headers, _body in _Console.calls
                   if method == "POST" and path.endswith("/launcher/start")]
        self.assertTrue(all(next((value for key, value in headers.items() if key.lower() == "x-kvmem-token"), "") == "temporary-secret" for headers in headers))
        self.assertTrue(all(next((value for key, value in headers.items() if key.lower() == "content-type"), "").startswith("application/json") for headers in headers))

    def test_start_surfaces_console_error_text(self):
        _Console.fail_start_reason = "端口 18200 无法使用，可能已有服务占用"
        client = KVMemClient(self.url, self.inference_url)
        with self.assertRaises(KVMemError) as caught:
            client.start({"schema_version": 1, "parameters": dict(ACTIVE_PARAMETERS)})
        self.assertIn("端口 18200", str(caught.exception))
        self.assertNotIn("不可达", str(caught.exception))

    def test_stop_confirms_stopped_state(self):
        client = KVMemClient(self.url, self.inference_url)
        result = client.stop()
        self.assertEqual(result["state"], "stopped")
        self.assertTrue(self.stop_posts())

    def test_capture_prefers_active_then_draft(self):
        _Console.state = "ready"
        client = KVMemClient(self.url, self.inference_url)
        captured = client.capture()
        self.assertEqual(captured["source"], "active")
        self.assertEqual(captured["parameters"]["Model"], "model.gguf")
        self.assertNotIn("ApiKey", captured["parameters"])
        _Console.state = "stopped"
        _Console.draft = {"schema_version": 1, "parameters": {"Model": "default.gguf", "Backend": "ninfer"}}
        captured = client.capture()
        self.assertEqual(captured["source"], "draft")
        self.assertEqual(captured["parameters"]["Model"], "default.gguf")

    def test_readiness_uses_provider_auth(self):
        _Console.state = "ready"
        _Console.auth_required = True
        client = KVMemClient(self.url, self.inference_url)
        failed = client.readiness("model.gguf")
        self.assertEqual(failed["status"], "failed")
        ready = client.readiness("model.gguf", auth_token=INFERENCE_KEY)
        self.assertEqual(ready["status"], "ready")

    def test_foreign_service_detected_not_treated_as_console(self):
        _Console.protocol_broken = True
        client = KVMemClient(self.url, self.inference_url)
        probe = client.probe()
        self.assertEqual(probe["kind"], "foreign")
        with self.assertRaises(KVMemError) as caught:
            client.info()
        self.assertIn("协议", str(caught.exception))


class KVMemCacheTests(KVMemFixture):
    def test_full_run_roundtrip_isolates_provider_and_strips_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = last_start_path(temporary)
            parameters = {"Model": "model.gguf", "Backend": "ninfer", "Context": 262144,
                          "FastPrefill": True, "ApiKey": INFERENCE_KEY}
            self.assertTrue(write_last_run(target, "kv-a", self.url, parameters))
            record = read_last_run(target, "kv-a", self.url)
            self.assertEqual(record["source"], "saved")
            self.assertEqual(record["config"]["parameters"]["Context"], 262144)
            self.assertTrue(record["config"]["parameters"]["FastPrefill"] is True)
            self.assertNotIn("ApiKey", record["config"]["parameters"])
            self.assertNotIn(INFERENCE_KEY, target.read_text(encoding="utf-8"))
            # 供应商隔离：另一张卡读不到这份记录。
            self.assertIsNone(read_last_run(target, "kv-b", self.url))
            # 控制地址隔离：同供应商指向别的地址也读不到。
            self.assertIsNone(read_last_run(target, "kv-a", "http://127.0.0.1:18199"))

    def test_legacy_four_field_cache_is_limited_migration_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = last_start_path(temporary)
            write_last_start(target, self.url, {"Backend": "ninfer", "Model": "old.gguf", "Gpu": 0})
            record = read_last_run(target, "kv-old", self.url)
            self.assertEqual(record["source"], "legacy")
            self.assertEqual(record["config"]["parameters"],
                             {"Backend": "ninfer", "Model": "old.gguf", "Gpu": "0"})
            self.assertIsNone(read_last_start(target).get("kv-old|" + self.url))

    def test_write_failure_returns_false_and_keeps_old_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = last_start_path(temporary)
            write_last_run(target, "kv-a", self.url, {"Model": "first.gguf", "Backend": "ninfer"})
            with mock.patch("naiba.kvmem.os.replace", side_effect=PermissionError("locked")):
                self.assertFalse(write_last_run(target, "kv-a", self.url, {"Model": "second.gguf", "Backend": "ninfer"}))
            record = read_last_run(target, "kv-a", self.url)
            self.assertEqual(record["config"]["parameters"]["Model"], "first.gguf")
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_concurrent_last_run_writes_are_not_lost(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = last_start_path(temporary)
            barrier = threading.Barrier(12)
            errors = []

            def write(index):
                try:
                    barrier.wait(timeout=5)
                    write_last_run(target, f"kv-{index}", self.url,
                                   {"Model": f"model-{index}.gguf", "Backend": "ninfer"})
                except BaseException as exc:
                    errors.append(repr(exc))

            threads = [threading.Thread(target=write, args=(index,)) for index in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=8)
            self.assertEqual(errors, [])
            for index in range(12):
                record = read_last_run(target, f"kv-{index}", self.url)
                self.assertEqual(record["config"]["parameters"]["Model"], f"model-{index}.gguf")
            self.assertEqual(list(target.parent.glob("*.tmp")), [])


class ConsoleLauncherTests(KVMemFixture):
    def _install(self, root):
        """带空 python.exe 的可校验安装树（Popen 会因空文件失败，用于 spawn 错误分支）。"""
        install = Path(root) / "kvmem"
        (install / "runtime/python").mkdir(parents=True)
        (install / "runtime/python/python.exe").write_bytes(b"")
        (install / "scripts").mkdir()
        (install / "scripts/local-console.py").write_text("# console", encoding="utf-8")
        return install

    def test_install_validation_reports_each_missing_piece(self):
        launcher = ConsoleLauncher()
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "state"
            log_path = Path(temporary) / "console.log"
            with self.assertRaises(KVMemError) as caught:
                launcher.launch(str(Path(temporary) / "missing"), self.url, state_dir=state_dir, log_path=log_path)
            self.assertIn("安装目录不存在", str(caught.exception))
            partial = Path(temporary) / "partial"
            (partial / "scripts").mkdir(parents=True)
            (partial / "scripts/local-console.py").write_text("# console", encoding="utf-8")
            with self.assertRaises(KVMemError) as caught:
                launcher.launch(str(partial), self.url, state_dir=state_dir, log_path=log_path)
            self.assertIn("自带 Python", str(caught.exception))
            no_script = Path(temporary) / "no-script"
            (no_script / "runtime/python").mkdir(parents=True)
            (no_script / "runtime/python/python.exe").write_bytes(b"")
            with self.assertRaises(KVMemError) as caught:
                launcher.launch(str(no_script), self.url, state_dir=state_dir, log_path=log_path)
            self.assertIn("控制程序", str(caught.exception))
            no_manifest = Path(temporary) / "no-manifest"
            (no_manifest / "runtime/python").mkdir(parents=True)
            (no_manifest / "runtime/python/python.exe").write_bytes(b"")
            (no_manifest / "scripts").mkdir()
            (no_manifest / "scripts/local-console.py").write_text("# console", encoding="utf-8")
            with self.assertRaises(KVMemError) as caught:
                launcher.launch(str(no_manifest), self.url, state_dir=state_dir, log_path=log_path)
            self.assertIn("安装元数据", str(caught.exception))

    def test_launch_builds_argument_array_without_browser_or_prompts(self):
        launcher = ConsoleLauncher()
        with tempfile.TemporaryDirectory() as temporary:
            install = self._install(temporary)
            (install / "BUILD-INFO.json").write_text(json.dumps({"version": "0.19.1"}), encoding="utf-8")
            log_path = Path(temporary) / "logs" / "console.log"
            state_dir = Path(temporary) / "state"
            with mock.patch("naiba.kvmem.subprocess.Popen") as popen:
                popen.return_value = mock.Mock(poll=lambda: None)
                launcher.launch(str(install), self.url, state_dir=state_dir, log_path=log_path)
            argv = popen.call_args.args[0]
            self.assertEqual(Path(argv[0]), install / "runtime/python/python.exe")
            self.assertEqual(argv[1:4], ["-X", "utf8", "-I"])
            self.assertEqual(Path(argv[4]), install / "scripts/local-console.py")
            self.assertIn("--host", argv) and self.assertEqual(argv[argv.index("--host") + 1], "127.0.0.1")
            self.assertEqual(argv[argv.index("--port") + 1], str(urlsplit(self.url).port))
            self.assertEqual(Path(argv[argv.index("--backend-root") + 1]), install)
            self.assertEqual(Path(argv[argv.index("--state-dir") + 1]), state_dir)
            self.assertIn("--prefer-bundled-worker", argv)
            self.assertIn("--language", argv) and self.assertEqual(argv[argv.index("--language") + 1], "zh-CN")
            for banned in ("--open-browser", "--prompt-port", "--prompt-address"):
                self.assertNotIn(banned, argv)
            self.assertEqual(popen.call_args.kwargs.get("cwd"), str(install))
            if subprocess.CREATE_NO_WINDOW:
                self.assertEqual(popen.call_args.kwargs.get("creationflags"), subprocess.CREATE_NO_WINDOW)

    def test_launch_spawn_failure_is_reported(self):
        launcher = ConsoleLauncher()
        with tempfile.TemporaryDirectory() as temporary:
            install = self._install(temporary)
            (install / "BUILD-INFO.json").write_text(json.dumps({"version": "0.19.1"}), encoding="utf-8")
            # python.exe 是空文件，Popen 直接失败：报「无法启动」而不是当成功。
            with self.assertRaises(KVMemError) as caught:
                launcher.launch(str(install), self.url, state_dir=Path(temporary) / "state",
                                log_path=Path(temporary) / "console.log")
            self.assertIn("无法启动", str(caught.exception))

    def test_wait_for_console_distinguishes_exit_foreign_and_timeout(self):
        client = KVMemClient(self.url, self.inference_url)
        crashed = SimpleNamespace(poll=lambda: 3, returncode=3)
        with self.assertRaises(KVMemError) as caught:
            wait_for_console(client, crashed, timeout=2)
        self.assertIn("退出码 3", str(caught.exception))
        _Console.protocol_broken = True
        with self.assertRaises(KVMemError) as caught:
            wait_for_console(client, None, timeout=2)
        self.assertIn("不是兼容版本", str(caught.exception))
        _Console.protocol_broken = False
        dead = KVMemClient("http://127.0.0.1:1", self.inference_url)
        with self.assertRaises(KVMemError) as caught:
            wait_for_console(dead, None, timeout=1.0)
        self.assertIn("超时", str(caught.exception))


class KVMemAppTests(KVMemFixture):
    def test_status_saves_full_run_and_fills_stopped_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(temporary, self.profile())
            _Console.state = "ready"
            payload, code = NaibaChatApp.api_kvmem_status(app, "kv")
            self.assertEqual(code, 200)
            self.assertEqual(payload["model"], "model.gguf")
            record = read_last_run(last_start_path(temporary), "kv", self.url)
            self.assertEqual(record["config"]["parameters"]["Model"], "model.gguf")
            self.assertNotIn(INFERENCE_KEY, json.dumps(record))
            # 停止且后台无任何记录时，摘要回填供应商明确保存的配置。
            _Console.state = "stopped"
            profile = self.profile(kvmem_launch_config={"schema_version": 1,
                                                        "parameters": {"Model": "saved.gguf", "Backend": "ninfer", "Port": urlsplit(self.inference_url).port}})
            app.config = SimpleNamespace(profile=lambda _key: profile)
            payload, code = NaibaChatApp.api_kvmem_status(app, "kv")
            self.assertEqual(code, 200)
            self.assertEqual(payload["status"], "stopped")
            self.assertEqual(payload["model"], "saved.gguf")

    def test_start_uses_saved_config_injects_key_and_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = self.profile(api_key=INFERENCE_KEY,
                                   kvmem_install_dir=r"D:\kvmem-fixture",
                                   kvmem_launch_config={"schema_version": 1,
                                                        "parameters": {"Model": "saved.gguf", "Backend": "ninfer",
                                                                        "Port": urlsplit(self.inference_url).port, "Context": 131072}})
            app = self.make_app(temporary, profile)
            payload, code = NaibaChatApp.api_kvmem_start(app, "kv", {"confirm": True})
            self.assertEqual(code, 202, payload)
            self.assertEqual(payload["summary"]["model"], "saved.gguf")
            self.assertEqual(payload["summary"]["config_source"], "provider")
            body = json.loads(self.start_posts()[-1].decode("utf-8"))
            self.assertEqual(body["parameters"]["Context"], 131072)
            self.assertEqual(body["parameters"]["ApiKey"], INFERENCE_KEY)
            record = read_last_run(last_start_path(temporary), "kv", self.url)
            self.assertEqual(record["config"]["parameters"]["Model"], "saved.gguf")
            self.assertNotIn(INFERENCE_KEY, json.dumps(record))
            self.assertEqual(_Console.state, "starting")

    def test_start_merges_same_backend_defaults_from_console_draft(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = self.profile(kvmem_launch_config={"schema_version": 1,
                                                        "parameters": {"Model": "saved.gguf", "Backend": "ninfer",
                                                                        "Port": urlsplit(self.inference_url).port,
                                                                        "Gpu": "GPU-1"}})
            app = self.make_app(temporary, profile)
            # 控制程序草稿带 ENGINE 默认值（视觉等）；最小保存配置缺的键由它补齐。
            _Console.draft = {"schema_version": 1, "parameters": {"Model": "", "Backend": "ninfer",
                                                                    "Vision": True, "Context": 262144,
                                                                    "VisionResidency": "cpu"}}
            payload, code = NaibaChatApp.api_kvmem_start(app, "kv", {"confirm": True})
            self.assertEqual(code, 202, payload)
            body = json.loads(self.start_posts()[-1].decode("utf-8"))
            self.assertIs(body["parameters"]["Vision"], True)
            self.assertEqual(body["parameters"]["Context"], 262144)
            self.assertEqual(body["parameters"]["Model"], "saved.gguf")
            self.assertEqual(body["parameters"]["Gpu"], "GPU-1")
            # 另一后端的草稿绝不混入。
            _Console.state = "stopped"
            _Console.draft = {"schema_version": 1, "parameters": {"Model": "", "Backend": "llamacpp",
                                                                    "Vision": False, "KvTypeK": "q8_0"}}
            payload, code = NaibaChatApp.api_kvmem_start(app, "kv", {"confirm": True})
            self.assertEqual(code, 202, payload)
            body = json.loads(self.start_posts()[-1].decode("utf-8"))
            self.assertNotIn("Vision", body["parameters"])
            self.assertNotIn("KvTypeK", body["parameters"])

    def test_start_autofills_single_gpu_and_requires_choice_for_multi(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = self.profile(kvmem_launch_config={"schema_version": 1,
                                                        "parameters": {"Model": "saved.gguf", "Backend": "ninfer",
                                                                        "Port": urlsplit(self.inference_url).port}})
            app = self.make_app(temporary, profile)
            payload, code = NaibaChatApp.api_kvmem_start(app, "kv", {"confirm": True})
            self.assertEqual(code, 202, payload)
            body = json.loads(self.start_posts()[-1].decode("utf-8"))
            self.assertEqual(body["parameters"]["Gpu"], "GPU-auto-1")
            # 多卡不能替用户猜：给出候选指引，不提交启动。
            _Console.state = "stopped"
            _Console.gpus = [{"uuid": "GPU-1", "name": "卡一", "memory_mib": "8000"},
                             {"uuid": "GPU-2", "name": "卡二", "memory_mib": "8000"}]
            payload, code = NaibaChatApp.api_kvmem_start(app, "kv", {"confirm": True})
            self.assertEqual(code, 400)
            self.assertIn("2 块 GPU", payload["error"])
            self.assertEqual(len(self.start_posts()), 1)

    def test_capture_carries_gpu_candidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(temporary, self.profile())
            payload, code = NaibaChatApp.api_kvmem_capture(app, {"manager_url": self.url})
            self.assertEqual(code, 200)
            self.assertEqual([gpu["uuid"] for gpu in payload.get("gpus", [])], ["GPU-auto-1"])

    def test_start_falls_back_to_legacy_cache_with_inferred_port(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(temporary, self.profile())
            write_last_start(last_start_path(temporary), self.url,
                             {"Backend": "ninfer", "Model": "old.gguf", "Gpu": 0})
            payload, code = NaibaChatApp.api_kvmem_start(app, "kv", {"confirm": True})
            self.assertEqual(code, 202, payload)
            self.assertEqual(payload["summary"]["config_source"], "legacy")
            body = json.loads(self.start_posts()[-1].decode("utf-8"))
            self.assertEqual(body["parameters"]["Model"], "old.gguf")
            self.assertEqual(body["parameters"]["Port"], urlsplit(self.inference_url).port)
            self.assertEqual(body["parameters"]["ListenHost"], "127.0.0.1")

    def test_start_without_any_config_guides_to_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(temporary, self.profile())
            payload, code = NaibaChatApp.api_kvmem_start(app, "kv", {"confirm": True})
            self.assertEqual(code, 400)
            self.assertIn("安装目录", payload["error"])
            self.assertEqual(self.start_posts(), [])

    def test_start_reuses_reachable_console_without_launching(self):
        with tempfile.TemporaryDirectory() as temporary:
            launcher = ConsoleLauncher()
            app = self.make_app(temporary, self.profile(kvmem_install_dir=r"D:\kvmem-fixture"), launcher=launcher)
            profile = self.profile(kvmem_launch_config={"schema_version": 1,
                                                        "parameters": {"Model": "saved.gguf", "Backend": "ninfer", "Port": urlsplit(self.inference_url).port}})
            app.config = SimpleNamespace(profile=lambda _key: profile)
            with mock.patch.object(ConsoleLauncher, "launch", side_effect=AssertionError("must reuse")):
                payload, code = NaibaChatApp.api_kvmem_start(app, "kv", {"confirm": True})
            self.assertEqual(code, 202, payload)
            self.assertEqual(_Console.state, "starting")

    def test_start_when_starting_reports_busy_not_second_submission(self):
        with tempfile.TemporaryDirectory() as temporary:
            _Console.state = "starting"
            app = self.make_app(temporary, self.profile())
            payload, code = NaibaChatApp.api_kvmem_start(app, "kv", {"confirm": True})
            self.assertEqual(code, 200)
            self.assertFalse(payload["accepted"])
            self.assertEqual(payload["status"], "starting")
            self.assertEqual(self.start_posts(), [])

    def test_start_with_foreign_service_does_not_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            _Console.protocol_broken = True
            app = self.make_app(temporary, self.profile(kvmem_install_dir=r"D:\kvmem-fixture"))
            with mock.patch.object(ConsoleLauncher, "launch", side_effect=AssertionError("must not launch")):
                payload, code = NaibaChatApp.api_kvmem_start(app, "kv", {"confirm": True})
            self.assertEqual(code, 400)
            self.assertIn("不是兼容版本", payload["error"])
            self.assertEqual(self.start_posts(), [])

    def test_start_without_console_or_install_dir_fails_clearly(self):
        with tempfile.TemporaryDirectory() as temporary:
            dead_url = "http://127.0.0.1:1"
            profile = self.profile(service_manager_url=dead_url)
            app = self.make_app(temporary, profile)
            payload, code = NaibaChatApp.api_kvmem_start(app, "kv", {"confirm": True})
            self.assertEqual(code, 400)
            self.assertIn("安装目录", payload["error"])

    def test_ensure_console_launches_with_managed_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            launcher = ConsoleLauncher()
            app = self.make_app(temporary, self.profile(kvmem_install_dir=r"D:\kvmem-fixture"), launcher=launcher)
            client = KVMemClient("http://127.0.0.1:1", self.inference_url)
            captured = {}

            def fake_launch(install_dir, manager_url, *, state_dir, log_path):
                captured["install"] = install_dir
                captured["state_dir"] = state_dir
                captured["log_path"] = log_path
                return SimpleNamespace(poll=lambda: None, returncode=None)

            info = {"backend_root": r"D:\kvmem-fixture"}
            with mock.patch.object(ConsoleLauncher, "launch", side_effect=fake_launch), \
                 mock.patch("naiba.kvmem.wait_for_console", return_value=info):
                NaibaChatApp._kvmem_ensure_console(app, self.profile(kvmem_install_dir=r"D:\kvmem-fixture"),
                                                   "http://127.0.0.1:1", client)
            self.assertEqual(Path(captured["install"]), Path(r"D:\kvmem-fixture"))
            self.assertEqual(captured["state_dir"], Path(temporary) / "kvmem" / "console-state" / "1")
            self.assertEqual(captured["log_path"], Path(temporary) / "kvmem" / "console-logs" / "console-1.log")

    def test_ensure_console_rejects_install_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(temporary, self.profile())
            client = KVMemClient("http://127.0.0.1:1", self.inference_url)
            with mock.patch.object(ConsoleLauncher, "launch", return_value=SimpleNamespace(poll=lambda: None)), \
                 mock.patch("naiba.kvmem.wait_for_console", return_value={"backend_root": r"D:\elsewhere"}):
                with self.assertRaises(KVMemError) as caught:
                    NaibaChatApp._kvmem_ensure_console(app, self.profile(kvmem_install_dir=r"D:\kvmem-fixture"),
                                                       "http://127.0.0.1:1", client)
            self.assertIn("不一致", str(caught.exception))

    def test_concurrent_starts_submit_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = self.profile(kvmem_launch_config={"schema_version": 1,
                                                        "parameters": {"Model": "saved.gguf", "Backend": "ninfer",
                                                                        "Port": urlsplit(self.inference_url).port}})
            app = self.make_app(temporary, profile)
            results = []

            def run():
                results.append(NaibaChatApp.api_kvmem_start(app, "kv", {"confirm": True}))

            threads = [threading.Thread(target=run) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
            self.assertTrue(all(thread.is_alive() is False for thread in threads))
            self.assertEqual(len(self.start_posts()), 1)
            accepted = [payload for payload, _code in results if payload.get("accepted")]
            self.assertEqual(len(accepted), 1)

    def test_stop_saves_running_config_then_confirms(self):
        with tempfile.TemporaryDirectory() as temporary:
            _Console.state = "ready"
            app = self.make_app(temporary, self.profile())
            payload, code = NaibaChatApp.api_kvmem_stop(app, "kv", {"confirm": True})
            self.assertEqual(code, 202, payload)
            self.assertEqual(payload["status"], "stopped")
            record = read_last_run(last_start_path(temporary), "kv", self.url)
            self.assertEqual(record["config"]["parameters"]["Model"], "model.gguf")
            self.assertNotIn(INFERENCE_KEY, json.dumps(record))
            self.assertTrue(self.stop_posts())

    def test_stop_save_failure_without_memory_aborts_before_stopping(self):
        with tempfile.TemporaryDirectory() as temporary:
            _Console.state = "ready"
            app = self.make_app(temporary, self.profile())
            with mock.patch("naiba.app.write_last_run", return_value=False):
                payload, code = NaibaChatApp.api_kvmem_stop(app, "kv", {"confirm": True})
            self.assertEqual(code, 400)
            self.assertIn("停止前无法保存", payload["error"])
            self.assertEqual(self.stop_posts(), [])
            self.assertEqual(_Console.state, "ready")

    def test_stop_save_failure_with_saved_config_warns_but_stops(self):
        with tempfile.TemporaryDirectory() as temporary:
            _Console.state = "ready"
            profile = self.profile(kvmem_launch_config={"schema_version": 1,
                                                        "parameters": {"Model": "model.gguf", "Backend": "ninfer"}})
            app = self.make_app(temporary, profile)
            with mock.patch("naiba.app.write_last_run", return_value=False):
                payload, code = NaibaChatApp.api_kvmem_stop(app, "kv", {"confirm": True})
            self.assertEqual(code, 202, payload)
            self.assertEqual(payload["status"], "stopped")
            self.assertIn("已保存的配置", payload.get("warning", ""))

    def test_stop_blocked_by_active_tasks(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(temporary, self.profile(), active_tasks=[{"id": "t1", "conversation_id": "c1"}])
            app.storage.get_conversation = lambda _cid, include_messages=False: {"model_key": "local:kv"}
            payload, code = NaibaChatApp.api_kvmem_stop(app, "kv", {"confirm": True})
            self.assertEqual(code, 409)
            self.assertEqual(payload["status"], "blocked")
            self.assertEqual(self.stop_posts(), [])

    def test_stop_unconfirmed_is_not_reported_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            _Console.state = "ready"
            _Console.stop_state = "starting"
            app = self.make_app(temporary, self.profile())
            payload, code = NaibaChatApp.api_kvmem_stop(app, "kv", {"confirm": True})
            self.assertEqual(code, 400)
            self.assertIn("未确认", payload["error"])
            _Console.stop_state = "stopped"

    def test_stop_when_already_stopped(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(temporary, self.profile())
            payload, code = NaibaChatApp.api_kvmem_stop(app, "kv", {"confirm": True})
            self.assertEqual(code, 200)
            self.assertFalse(payload["accepted"])
            self.assertEqual(self.stop_posts(), [])

    def test_capture_flows_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(temporary, self.profile())
            _Console.state = "ready"
            payload, code = NaibaChatApp.api_kvmem_capture(app, {"manager_url": self.url})
            self.assertEqual(code, 200)
            self.assertEqual(payload["source"], "active")
            self.assertNotIn("temporary-secret", json.dumps(payload))
            # 控制程序未运行：不带确认时 409 needs_confirm，不启动模型。
            dead = KVMemClient("http://127.0.0.1:1", self.inference_url)
            with mock.patch("naiba.kvmem.KVMemClient.probe", return_value={"kind": "unreachable", "info": None, "error": "x"}):
                payload, code = NaibaChatApp.api_kvmem_capture(app, {"manager_url": self.url})
                self.assertEqual(code, 409)
                self.assertTrue(payload.get("needs_confirm"))
                self.assertEqual(self.start_posts(), [])

    def test_capture_requires_install_dir_for_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(temporary, self.profile())
            with mock.patch("naiba.kvmem.KVMemClient.probe", return_value={"kind": "unreachable", "info": None, "error": "x"}):
                payload, code = NaibaChatApp.api_kvmem_capture(app, {"manager_url": self.url, "confirm": True})
            self.assertEqual(code, 400)
            self.assertIn("安装目录", payload["error"])


if __name__ == "__main__":
    unittest.main()
