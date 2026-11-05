"""发布 UI 编排的真实子进程回归：失败阻断、超时回收和独立进程树归属。"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from verify.release_ui_smoke import ROOT, run_gate

SERVER = r'''
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
root=Path(os.environ['NAIBA_TUT_ROOT']);output=Path(os.environ['NAIBA_SMOKE_OUTPUT'])
(output/'server.pid').write_text(str(os.getpid()))
if MODE=='exit':
    print('启动失败夹具',flush=True);sys.exit(7)
if MODE=='timeout':
    time.sleep(60);sys.exit(0)
config=json.loads((root/'config.json').read_text(encoding='utf-8'))
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        payload={'data_location':{'is_frozen':MODE!='unfrozen','data_dir':str(root/'data'),'config_path':str(root/'config.json')},
            'settings':config,'providers':config['providers'],'model_profiles':[], 'mcp_servers':config['mcp_servers']}
        data=json.dumps(payload).encode();self.send_response(200);self.send_header('Content-Length',str(len(data)))
        self.end_headers();self.wfile.write(data)
ThreadingHTTPServer(('127.0.0.1',int(os.environ['NAIBA_TUT_PORT'])),Handler).serve_forever()
'''

NODE = r'''
import json, os, subprocess, sys, time
from pathlib import Path
out=Path(os.environ['NAIBA_SMOKE_OUTPUT']);root=Path(os.environ['NAIBA_TUT_ROOT'])
config=json.loads((root/'config.json').read_text(encoding='utf-8'))
assert not config['providers'] and not config['mcp_servers'] and not config['onebot']['enabled']
assert not os.environ.get('NAIBA_TUT_PROVIDERS_FROM') and not os.environ.get('NAIBA_TUT_SEED_MCP')
assert Path(os.environ['LOCALAPPDATA']).is_relative_to(root)
with (out/'invocations.log').open('a') as log:log.write(Path(sys.argv[-1]).name+'\n')
if MODE=='fail':
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    (out/'child.pid').write_text(str(child.pid));print('UI_ASSERTION_FAILED',flush=True);sys.exit(17)
if MODE=='timeout':
    (out/'node.pid').write_text(str(os.getpid()));time.sleep(60)
print('UI_FIXTURE_OK',flush=True)
'''


def alive(pid: int) -> bool:
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE，只读等待进程状态
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class ReleaseUIGateRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="release-ui-test-", dir=ROOT / "verify")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "evidence"
        self.runtime = self.root / "runtime"

    def run_fixture(self, server_mode="ok", node_mode="ok", **overrides):
        server = self.root / "server.py"
        server.write_text(f"MODE={server_mode!r}\n" + SERVER, encoding="utf-8")
        node = self.root / "node.py"
        node.write_text(f"MODE={node_mode!r}\n" + NODE, encoding="utf-8")
        with mock.patch.dict(os.environ, {"NAIBA_TUT_PROVIDERS_FROM": "must-not-be-read.json",
                                         "NAIBA_TUT_SEED_MCP": "1"}):
            return run_gate([sys.executable, str(server)], [sys.executable, str(node)],
                            artifacts=self.output, runtime_parent=self.runtime,
                            startup_timeout=overrides.get("startup_timeout", 5),
                            script_timeout=overrides.get("script_timeout", 5))

    def assert_stopped(self, name):
        pid = int((self.output / name).read_text())
        deadline = time.monotonic() + 3
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(pid), f"本轮进程未退出：{name}")

    def test_success_runs_both_scripts_and_removes_only_runtime(self):
        self.assertEqual(self.run_fixture(), 0)
        self.assertEqual((self.output / "invocations.log").read_text().splitlines(),
                         ["browser_smoke.cjs", "chat_composer_layout_smoke.cjs"])
        self.assert_stopped("server.pid")
        self.assertEqual(list(self.runtime.iterdir()), [])
        self.assertTrue((self.output / "server.log").exists())
        self.assertFalse(list(self.output.glob("*.json")))
        self.assertFalse(list(self.output.glob("*.db")))

    def test_failed_browser_blocks_next_gate_and_kills_its_orphan_child_not_other_process(self):
        unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"],
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            self.assertEqual(self.run_fixture(node_mode="fail"), 1)
            self.assertEqual((self.output / "invocations.log").read_text().splitlines(), ["browser_smoke.cjs"])
            self.assertIn("UI_ASSERTION_FAILED", (self.output / "browser_smoke.log").read_text(encoding="utf-8"))
            self.assertIn("exit 17", (self.output / "gate.log").read_text(encoding="utf-8"))
            self.assert_stopped("server.pid")
            self.assert_stopped("child.pid")
            self.assertIsNone(unrelated.poll(), "门禁误杀了它没有启动的进程")
            self.assertEqual(list(self.runtime.iterdir()), [])
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)

    def test_http_startup_timeout_blocks_browser_and_reaps_service(self):
        self.assertEqual(self.run_fixture(server_mode="timeout", startup_timeout=0.5), 1)
        self.assertIn("HTTP 就绪", (self.output / "gate.log").read_text(encoding="utf-8"))
        self.assertFalse((self.output / "invocations.log").exists())
        self.assert_stopped("server.pid")

    def test_source_server_cannot_masquerade_as_packaged_ui(self):
        self.assertEqual(self.run_fixture(server_mode="unfrozen"), 1)
        self.assertIn("没有提供本轮隔离正式包", (self.output / "gate.log").read_text(encoding="utf-8"))
        self.assertFalse((self.output / "invocations.log").exists())
        self.assert_stopped("server.pid")

    def test_browser_timeout_is_failure_and_reaps_browser_and_service(self):
        self.assertEqual(self.run_fixture(node_mode="timeout", script_timeout=0.5), 1)
        self.assert_stopped("server.pid")
        self.assert_stopped("node.pid")
        self.assertFalse((self.output / "chat_composer_layout_smoke.log").exists())


if __name__ == "__main__":
    unittest.main()
