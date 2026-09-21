# -*- coding: utf-8 -*-
"""护栏：端口被占用时不再静默失败（见 `报告-8765端口占用诊断.md` / `计划-端口加固与文件夹拖拽.md` A 部分）。

缺陷形态（本文件钉的就是这几条）：
1. 绑定发生在后台线程、构造函数无 try/except ⇒ 端口被占时线程无声死亡；
   冻结版没有控制台，用户只看到"托盘在、窗口白屏"（`ERR_CONNECTION_REFUSED`）；
2. 健康检查轮询 50×0.1s 后**没有失败分支**，照常开窗；
3. 健康检查只看 status code、不验身份 ⇒ 占用者恰好是个 HTTP 服务时，窗口开到别人的应用上；
4. Windows 的 `SO_REUSEADDR` 允许"双绑"：第二个进程也能绑上同一端口且不报错。

覆盖：中文文案（含可复制的自查命令与两条出路）、`/api/health` 的应用身份标记、
Windows 独占绑定与 TIME_WAIT 回落、launcher 的绑定位置与失败分支。
"""

from __future__ import annotations

import ast
import json
import socket
import sys
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core.network import port_conflict_message  # noqa: E402
from naiba.http import (  # noqa: E402
    HEALTH_APP_MARKER,
    AppHTTPServer,
    RequestHandler,
    _port_has_listener,
)

WINDOWS = sys.platform == "win32"


class _StubMcp:
    def states(self):
        return []


class _ForeignHandler(BaseHTTPRequestHandler):
    """冒充占用者：`/api/health` 回 200，但响应体里没有我们的 app 标记。"""

    protocol_version = "HTTP/1.0"

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 契约
        body = json.dumps({"status": "ok", "who": "someone-else"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 测试里不要刷屏
        return


class PortConflictMessageTests(unittest.TestCase):
    """A1/A4：中文文案必须能自己解决问题（端口号 + 自查命令 + 两条出路）。"""

    def test_message_has_port_command_and_both_ways_out(self) -> None:
        text = port_conflict_message(8765, host="0.0.0.0", detail="[WinError 10048] 通常...")
        self.assertIn("8765", text)
        self.assertIn("netstat -ano | findstr :8765", text)
        self.assertIn("[WinError 10048]", text, "底层异常摘要要带上，便于对照排查")
        self.assertIn('"port"', text, "必须告诉用户能改 config.json 的 port")
        self.assertIn("8766", text, "要给一个具体可抄的备选端口")
        self.assertIn("%LOCALAPPDATA%", text, "安装版配置路径")
        self.assertIn("防火墙", text, "换端口的连带影响要说清")

    def test_message_without_detail_still_works(self) -> None:
        text = port_conflict_message(8765)
        self.assertIn("8765", text)
        self.assertNotIn("系统返回", text)


class HealthMarkerTests(unittest.TestCase):
    """A3：`/api/health` 必须自报身份，launcher 才能拒绝"开到别人服务上"。"""

    def setUp(self) -> None:
        self.app = SimpleNamespace(mcp=_StubMcp())
        self.server = AppHTTPServer(("127.0.0.1", 0), RequestHandler, self.app)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)

    def _shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _health(self, port: int) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_health_reports_app_marker(self) -> None:
        payload = self._health(self.port)
        self.assertEqual(payload.get("status"), "ok")
        self.assertEqual(payload.get("app"), HEALTH_APP_MARKER)
        self.assertIn("mcp", payload, "老字段不能被这次的加固挤掉")

    def test_marker_is_a_stable_literal(self) -> None:
        # 这个值同时写在 /api/health 与 launcher 的校验里（经 server 门面转出）。
        # 改值不会有人报错，但会让"验身份"悄悄失效——所以在这里钉死。
        self.assertEqual(HEALTH_APP_MARKER, "cat-chat")


class WindowsBindTests(unittest.TestCase):
    """A5：Windows 独占绑定 + TIME_WAIT 回落（别的平台保持原语义）。"""

    def setUp(self) -> None:
        self.app = SimpleNamespace(mcp=_StubMcp())

    @unittest.skipUnless(WINDOWS, "Windows 专属：SO_EXCLUSIVEADDRUSE 语义（别的平台沿用原 allow_reuse_address）")
    def test_second_bind_on_same_port_fails(self) -> None:
        """防静默双绑：端口已被本进程的服务听着时，第二个绑定必须报错。"""
        first = AppHTTPServer(("127.0.0.1", 0), RequestHandler, self.app)
        self.addCleanup(first.server_close)
        port = int(first.server_address[1])
        self.assertTrue(_port_has_listener("127.0.0.1", port))
        with self.assertRaises(OSError):
            AppHTTPServer(("127.0.0.1", port), RequestHandler, self.app).server_close()

    @unittest.skipUnless(WINDOWS, "Windows 专属：SO_EXCLUSIVEADDRUSE / TIME_WAIT 语义")
    def test_exclusive_failure_without_listener_falls_back_to_reuse(self) -> None:
        """独占绑定失败但**探不到监听者**时，必须回落 SO_REUSEADDR 重绑（否则崩溃后起不来）。"""
        holder = socket.socket()
        self.addCleanup(holder.close)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen(5)
        port = int(holder.getsockname()[1])
        # 真实探针会连上 holder（返回 True ⇒ 直接报错），这里强制走"只剩 TIME_WAIT"分支。
        with mock.patch("naiba.http._port_has_listener", return_value=False):
            server = AppHTTPServer(("127.0.0.1", port), RequestHandler, self.app)
        self.addCleanup(server.server_close)
        self.assertEqual(int(server.server_address[1]), port, "回落分支必须真的绑在同一个端口上")

    @unittest.skipUnless(WINDOWS, "Windows 专属：SO_EXCLUSIVEADDRUSE / TIME_WAIT 语义")
    def test_rebinds_after_crash_with_time_wait(self) -> None:
        """崩溃后立即重启：端口只剩 TIME_WAIT 残连时仍要绑得上（既有能力不许退化）。"""
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        port = int(listener.getsockname()[1])
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        accepted, _ = listener.accept()
        accepted.close()   # 服务端先关 ⇒ 该连接的本地端口进入 TIME_WAIT（模拟上一次进程崩掉）
        client.close()
        listener.close()
        self.assertFalse(_port_has_listener("127.0.0.1", port), "此刻不该还有监听者")
        server = AppHTTPServer(("127.0.0.1", port), RequestHandler, self.app)
        self.addCleanup(server.server_close)
        self.assertEqual(int(server.server_address[1]), port)

    def test_port_has_listener_reflects_reality(self) -> None:
        holder = socket.socket()
        self.addCleanup(holder.close)
        holder.bind(("127.0.0.1", 0))
        holder.listen(5)
        port = int(holder.getsockname()[1])
        self.assertTrue(_port_has_listener("127.0.0.1", port))
        holder.close()
        self.assertFalse(_port_has_listener("127.0.0.1", port))


class LauncherStartupTests(unittest.TestCase):
    """A1/A2：绑定必须在主线程；健康检查失败必须有失败分支。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (ROOT / "launcher.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def _function(self, name: str) -> ast.FunctionDef:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail(f"launcher.py 里找不到 {name}")

    def test_binding_happens_in_bind_server_not_in_thread_body(self) -> None:
        """`AppHTTPServer(...)` 必须只在 `_bind_server` 里构造（后台线程构造函数无 try/except）。"""
        owners = [
            node.name
            for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef)
            and any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "AppHTTPServer"
                for inner in ast.walk(node)
            )
        ]
        self.assertEqual(owners, ["_bind_server"], f"绑定位置漂了：{owners}")

    def _exception_names(self, node: ast.Try) -> set[str]:
        names: set[str] = set()
        for handler in node.handlers:
            target = handler.type
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
            elif isinstance(target, ast.Tuple):
                names.update(
                    item.id if isinstance(item, ast.Name) else getattr(item, "attr", "")
                    for item in target.elts
                )
        return names

    def test_run_guards_bind_and_health_failures(self) -> None:
        run = self._function("run")
        caught = set()
        for node in ast.walk(run):
            if isinstance(node, ast.Try):
                caught |= self._exception_names(node)
        self.assertIn("OSError", caught, "run() 必须捕获绑定失败（OSError）")
        self.assertIn("_wait_healthy", self.source)
        self.assertIn("_abort_startup", self.source, "启动期硬失败必须走统一出口（弹提示 + 清现场 + 退出）")
        # A2：健康检查结果必须参与判定，而不是"轮询完就往下走"。
        self.assertIn('health != "ok"', self.source)

    def test_health_probe_returns_three_states(self) -> None:
        import launcher  # noqa: PLC0415 - 与 tests/test_app_icon.py 同款：重型启动器按需导入

        probe = launcher.Launcher()._wait_healthy

        class _Ours(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):  # noqa: N802
                body = json.dumps({"status": "ok", "app": HEALTH_APP_MARKER}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        state = {}
        for name, handler in (("ours", _Ours), ("foreign", _ForeignHandler)):
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                state[name] = probe(f"http://127.0.0.1:{server.server_address[1]}")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
        self.assertEqual(state["ours"], "ok")
        self.assertEqual(state["foreign"], "foreign", "别人的 200 不能被当成自己人")


if __name__ == "__main__":
    unittest.main()
