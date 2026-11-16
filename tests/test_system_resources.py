# -*- coding: utf-8 -*-
"""Deterministic host resource collection, cache and authenticated HTTP guards."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from naiba import system_resources as resources
from naiba.app import NaibaChatApp
from naiba.http import AppHTTPServer, RequestHandler

_MIB = 1024 * 1024
_MEMORY = {"available": True, "total_bytes": 8 * _MIB, "used_bytes": 2 * _MIB, "percent": 25.0}


class SystemResourcesCollectionTests(unittest.TestCase):
    def test_windows_memory_uses_global_memory_status_ex(self):
        def query(pointer):
            status = pointer._obj
            self.assertEqual(status.dwLength, 64)
            status.ullTotalPhys = 32 * _MIB
            status.ullAvailPhys = 24 * _MIB
            status.dwMemoryLoad = 99  # Percent must use the same byte counts.
            return 1

        kernel = SimpleNamespace(GlobalMemoryStatusEx=Mock(side_effect=query))
        with patch.object(resources.os, "name", "nt"), patch.object(resources.ctypes, "WinDLL", return_value=kernel, create=True):
            self.assertEqual(resources._read_memory(), {
                "available": True, "total_bytes": 32 * _MIB,
                "used_bytes": 8 * _MIB, "percent": 25.0,
            })
        self.assertEqual(ctypes.sizeof(resources._MemoryStatusEx), 64)

    def test_memory_failure_and_invalid_counts_are_unavailable(self):
        for result, total, available in ((0, 100, 10), (1, 0, 0), (1, 100, 101)):
            def query(pointer):
                pointer._obj.ullTotalPhys = total
                pointer._obj.ullAvailPhys = available
                return result

            kernel = SimpleNamespace(GlobalMemoryStatusEx=Mock(side_effect=query))
            with self.subTest(result=result, total=total, available=available), patch.object(resources.os, "name", "nt"), patch.object(resources.ctypes, "WinDLL", return_value=kernel, create=True), patch.object(resources, "_read_gpus", return_value=([], "", "gpu_not_found")):
                payload = resources.SystemResources().get()
                self.assertEqual(payload["memory"], {
                    "available": False, "total_bytes": None,
                    "used_bytes": None, "percent": None,
                })
                self.assertEqual(payload["error"], "memory_unavailable")

    def test_multi_gpu_csv_and_mib_to_bytes(self):
        self.assertEqual(resources._parse_gpus('0, NVIDIA RTX 4090, 24576, 6144\n2, "NVIDIA, Test", 1024.5, 512.25\n'), [
            {"index": 0, "name": "NVIDIA RTX 4090", "total_bytes": 24576 * _MIB, "used_bytes": 6144 * _MIB, "percent": 25.0},
            {"index": 2, "name": "NVIDIA, Test", "total_bytes": int(1024.5 * _MIB), "used_bytes": int(512.25 * _MIB), "percent": 50.0},
        ])

    def test_invalid_gpu_rows_reject_whole_snapshot(self):
        rows = [
            "0, GPU, N/A, 1", "0, GPU, 0, 0", "0, GPU, 10, 11",
            "0, GPU, 10, -1", "0, GPU, NaN, 1", "0, GPU, Infinity, 1",
            "-1, GPU, 10, 1", "x, GPU, 10, 1", "0, , 10, 1",
            "0, GPU, 10", "0, GPU, 10, 1, extra", "0, GPU, 10, 1\n0, GPU, 10, 1",
            '0, "GPU\nprivate", 10, 1', "0, GPU, 1e1000000, 0",
        ]
        for row in rows:
            with self.subTest(row=row), self.assertRaises(ValueError):
                resources._parse_gpus(row)

    def test_discovery_uses_system_directory_and_absolute_path_entries(self):
        with tempfile.TemporaryDirectory(prefix="naiba_smi_") as tmp:
            root = Path(tmp)
            system_dir = root / "system"
            path_dir = root / "path"
            system_dir.mkdir()
            path_dir.mkdir()
            executable = "nvidia-smi.exe" if os.name == "nt" else "nvidia-smi"
            system_exe = system_dir / executable
            path_exe = path_dir / executable
            system_exe.touch()
            path_exe.touch()
            if os.name != "nt":
                system_exe.chmod(0o700)
                path_exe.chmod(0o700)

            def system_directory(buffer, size):
                buffer.value = str(system_dir)
                return len(str(system_dir))

            kernel = SimpleNamespace(GetSystemDirectoryW=Mock(side_effect=system_directory))
            with patch.dict(os.environ, {"PATH": os.pathsep.join(("", ".", str(path_dir)))}, clear=True), patch.object(resources.ctypes, "WinDLL", return_value=kernel, create=True):
                found = resources._find_nvidia_smi()
                self.assertEqual(found, str((system_exe if os.name == "nt" else path_exe).resolve()))
                self.assertTrue(Path(found).is_absolute())
                system_exe.unlink()
                self.assertEqual(resources._find_nvidia_smi(), str(path_exe.resolve()))
            with patch.dict(os.environ, {"PATH": os.pathsep.join(("", ".", "relative"))}, clear=True), patch.object(resources.ctypes, "WinDLL", side_effect=OSError("private path"), create=True):
                self.assertIsNone(resources._find_nvidia_smi())

    def test_gpu_subprocess_is_absolute_hidden_shell_false_and_bounded(self):
        executable = str(Path(tempfile.gettempdir()).resolve() / "nvidia-smi.exe")
        with patch.object(resources, "_find_nvidia_smi", return_value=executable), patch.object(resources.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="0, GPU, 100, 25\n")) as run:
            gpus, error, reason = resources._read_gpus()
        self.assertEqual(len(gpus), 1)
        self.assertEqual((error, reason), ("", ""))
        args, kwargs = run.call_args
        self.assertEqual(args[0], [executable, "--query-gpu=index,name,memory.total,memory.used", "--format=csv,noheader,nounits"])
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["timeout"], 2.0)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        if os.name == "nt":
            self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)

    def test_missing_gpu_does_not_start_process(self):
        with patch.object(resources, "_find_nvidia_smi", return_value=None), patch.object(resources.subprocess, "run") as run:
            self.assertEqual(resources._read_gpus(), ([], "", "gpu_not_found"))
            run.assert_not_called()

    def test_no_devices_invalid_output_and_query_failure(self):
        for code, output, expected in ((0, "", ("", "gpu_no_devices")), (0, "0, GPU, N/A, 0", ("gpu_data_invalid", "")), (1, "secret argv path hostname", ("gpu_query_failed", ""))):
            with self.subTest(code=code, output=output), patch.object(resources, "_find_nvidia_smi", return_value=str(Path(tempfile.gettempdir()) / "nvidia-smi.exe")), patch.object(resources.subprocess, "run", return_value=SimpleNamespace(returncode=code, stdout=output)):
                self.assertEqual(resources._read_gpus(), ([], *expected))

    def test_timeout_and_os_failure_have_safe_error_codes_and_are_cached(self):
        for failure, expected in ((subprocess.TimeoutExpired(["private executable", "secret"], 2, output="private stdout", stderr="private stderr"), "gpu_timeout"), (OSError("private executable path"), "gpu_query_failed")):
            with self.subTest(expected=expected), patch.object(resources, "_read_memory", return_value=_MEMORY), patch.object(resources, "_find_nvidia_smi", return_value=str(Path(tempfile.gettempdir()) / "nvidia-smi.exe")), patch.object(resources.subprocess, "run", side_effect=failure) as run:
                collector = resources.SystemResources()
                payload = collector.get()
                self.assertEqual(payload["error"], expected)
                self.assertTrue(payload["memory"]["available"])
                self.assertFalse(payload["gpu_available"])
                self.assertNotIn("private", json.dumps(payload))
                self.assertEqual(collector.get(), payload)
                self.assertEqual(run.call_count, 1)

    def test_collection_failure_still_returns_whitelist_json(self):
        with patch.object(resources, "_read_memory", side_effect=OSError("private host path")), patch.object(resources, "_read_gpus", side_effect=OSError("argv secret")), patch.object(resources.time, "time", return_value=1234.567):
            collector = resources.SystemResources()
            payload = collector.get()
            self.assertEqual(collector.get(), payload)
        self.assertEqual(set(payload), {"source", "sampled_at", "memory", "gpus", "gpu_available", "error", "reason"})
        self.assertEqual(set(payload["memory"]), {"available", "total_bytes", "used_bytes", "percent"})
        self.assertEqual(payload["source"], "host")
        self.assertEqual(payload["sampled_at"], 1234567)
        self.assertEqual(payload["error"], "memory_unavailable;gpu_query_failed")
        self.assertEqual(payload["gpus"], [])
        self.assertFalse(payload["gpu_available"])
        self.assertNotIn("private", json.dumps(payload))
        self.assertNotIn("secret", json.dumps(payload))


class SystemResourcesCacheTests(unittest.TestCase):
    def test_success_and_failure_ttl_expire_after_three_seconds(self):
        for result in (([], "", "gpu_no_devices"), ([], "gpu_timeout", "")):
            with self.subTest(result=result), patch.object(resources, "_read_memory", return_value=_MEMORY), patch.object(resources, "_read_gpus", return_value=result) as gpu, patch.object(resources.time, "monotonic", return_value=100.0) as clock:
                collector = resources.SystemResources()
                first = collector.get()
                first["memory"]["used_bytes"] = -1
                clock.return_value = 102.999
                self.assertEqual(collector.get()["memory"]["used_bytes"], 2 * _MIB)
                self.assertEqual(gpu.call_count, 1)
                clock.return_value = 103.0
                collector.get()
                self.assertEqual(gpu.call_count, 2)

    def test_cold_concurrent_requests_merge_into_one_query(self):
        started, release = threading.Event(), threading.Event()
        barrier = threading.Barrier(8)

        def sample_gpu():
            started.set()
            self.assertTrue(release.wait(3))
            return [], "", "gpu_no_devices"

        def request(collector):
            barrier.wait(timeout=3)
            return collector.get()

        collector = resources.SystemResources()
        with patch.object(resources, "_read_memory", return_value=_MEMORY), patch.object(resources, "_read_gpus", side_effect=sample_gpu) as gpu, ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(request, collector) for _ in range(8)]
            try:
                self.assertTrue(started.wait(3))
                self.assertEqual(gpu.call_count, 1)
            finally:
                release.set()
            results = [future.result(timeout=3) for future in futures]
            self.assertEqual(gpu.call_count, 1)
            self.assertTrue(all(result == results[0] for result in results))
            self.assertTrue(all(result is not results[0] for result in results[1:]))

    def test_refresh_returns_previous_sample_to_other_callers(self):
        collector = resources.SystemResources()
        started, release = threading.Event(), threading.Event()
        with patch.object(resources, "_read_memory", return_value=_MEMORY), patch.object(resources, "_read_gpus", return_value=([], "", "gpu_no_devices")):
            previous = collector.get()
        collector._expires_at = 0

        def sample_gpu():
            started.set()
            self.assertTrue(release.wait(3))
            return [], "gpu_timeout", ""

        with patch.object(resources, "_read_memory", return_value=_MEMORY), patch.object(resources, "_read_gpus", side_effect=sample_gpu) as gpu, ThreadPoolExecutor(max_workers=2) as pool:
            refresh = pool.submit(collector.get)
            try:
                self.assertTrue(started.wait(3))
                self.assertEqual(pool.submit(collector.get).result(timeout=1), previous)
                self.assertEqual(gpu.call_count, 1)
            finally:
                release.set()
            self.assertEqual(refresh.result(timeout=3)["error"], "gpu_timeout")
            self.assertEqual(collector.get()["error"], "gpu_timeout")
            self.assertEqual(gpu.call_count, 1)


class SystemResourcesHTTPTests(unittest.TestCase):
    def setUp(self):
        # Exercise the real app method without starting unrelated app services.
        self.app = NaibaChatApp.__new__(NaibaChatApp)
        self.app.config = SimpleNamespace(data={"access_token": "resource-test-token"})
        self.app.system_resources = resources.SystemResources()
        self.memory = patch.object(resources, "_read_memory", return_value=_MEMORY).start()
        self.addCleanup(patch.stopall)
        self.gpu = patch.object(resources, "_read_gpus", return_value=([{
            "index": 0, "name": "GPU", "total_bytes": 4 * _MIB,
            "used_bytes": _MIB, "percent": 25.0,
        }], "", "")).start()
        self.server = AppHTTPServer(("127.0.0.1", 0), RequestHandler, self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def _get(self, suffix="", headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request("GET", "/api/system/resources" + suffix, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            connection.close()

    def test_local_http_returns_host_whitelist_and_no_store(self):
        status, payload, headers = self._get()
        self.assertEqual(status, 200)
        self.assertEqual(payload["source"], "host")
        self.assertTrue(payload["gpu_available"])
        self.assertIsInstance(payload["sampled_at"], int)
        self.assertEqual(set(payload), {"source", "sampled_at", "memory", "gpus", "gpu_available", "error", "reason"})
        self.assertEqual(set(payload["gpus"][0]), {"index", "name", "total_bytes", "used_bytes", "percent"})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self._get()[1], payload)
        self.assertEqual(self.gpu.call_count, 1)

    def test_lan_requires_existing_bearer_or_query_token(self):
        with patch.object(RequestHandler, "_is_local_request", return_value=False):
            for suffix, headers in (("", {}), ("?token=wrong", {}), ("", {"Authorization": "Bearer wrong"})):
                status, payload, _ = self._get(suffix, headers)
                self.assertEqual(status, 401)
                self.assertNotIn("memory", payload)
            self.memory.assert_not_called()
            self.gpu.assert_not_called()
            for suffix, headers in (("", {"Authorization": "Bearer resource-test-token"}), ("?token=resource-test-token", {})):
                status, payload, _ = self._get(suffix, headers)
                self.assertEqual(status, 200)
                self.assertEqual(payload["source"], "host")
            self.assertEqual(self.gpu.call_count, 1)


if __name__ == "__main__":
    unittest.main()
