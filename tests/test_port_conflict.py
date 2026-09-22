# -*- coding: utf-8 -*-
"""护栏：端口被占用时不再静默失败（见 `报告-8765端口占用诊断.md` / `计划-端口加固与文件夹拖拽.md` A 部分）。

缺陷形态（本文件钉的就是这几条）：
1. 绑定发生在后台线程、构造函数无 try/except ⇒ 端口被占时线程无声死亡；
   冻结版没有控制台，用户只看到"托盘在、窗口白屏"（`ERR_CONNECTION_REFUSED`）；
2. 健康检查轮询 50×0.1s 后**没有失败分支**，照常开窗；
3. 健康检查只看 status code、不验身份 ⇒ 占用者恰好是个 HTTP 服务时，窗口开到别人的应用上；
4. Windows 的 `SO_REUSEADDR` 允许"双绑"：第二个进程也能绑上同一端口且不报错。

覆盖：中文文案（含可复制的自查命令与两条出路）、`/api/health` 的应用身份标记、
Windows 独占绑定与 TIME_WAIT 回落、launcher 的绑定位置与失败分支，
以及 §九.126「弹窗输入新端口继续启动」（含 AST 守门与 `suggest_free_port` 探测语义）。
"""

from __future__ import annotations

import ast
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core.network import port_conflict_message, suggest_free_port  # noqa: E402
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


class SuggestFreePortTests(unittest.TestCase):
    """§九.126：`suggest_free_port` 只是**建议值**——裸 bind 探测，不 listen、不留监听者。"""

    def _reserve(self, port: int = 0) -> int:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            holder.bind(("127.0.0.1", port))
        except OSError:
            holder.close()
            raise
        holder.listen(1)
        self.addCleanup(holder.close)
        return int(holder.getsockname()[1])

    def _free(self, port: int) -> bool:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
        finally:
            probe.close()
        return True

    def _pick(self, blocked_offsets: tuple[int, ...], verify_free: int | None = None) -> int:
        """找一个 base：指定偏移处的端口可被占用，且（可选）另一偏移处确实空闲。"""
        for _ in range(80):
            base = self._reserve()
            if base + (max(blocked_offsets) if blocked_offsets else 0) + 1 > 65535:
                continue
            usable = True
            for offset in blocked_offsets:
                if not self._free(base + offset):
                    usable = False
                    break
                try:
                    self._reserve(base + offset)
                except OSError:
                    usable = False
                    break
            if not usable:
                continue
            if verify_free is not None and not self._free(base + verify_free):
                continue
            return base
        self.skipTest("没找到合适的端口组合")

    def test_returns_start_plus_one_when_free(self) -> None:
        base = self._pick((), verify_free=1)
        self.assertEqual(suggest_free_port(base, tries=1), base + 1)

    def test_skips_busy_candidate(self) -> None:
        base = self._pick((1,), verify_free=2)
        self.assertEqual(suggest_free_port(base, tries=3), base + 2, "base+1 被占就该往后跳")

    def test_falls_back_to_start_plus_one_when_all_busy(self) -> None:
        base = self._pick((1, 2))
        self.assertEqual(
            suggest_free_port(base, tries=2), base + 1,
            "候选全忙时回落 base+1：建议值只是弹窗初始值，端口仍由用户确认",
        )

    def test_probe_leaves_the_port_free(self) -> None:
        """探测只 bind+close：既不放端口、也不留监听者，否则"建议值"反倒把端口占了。

        判据用**独占绑定**（AppHTTPServer 的真实绑定路径），不用 TCP connect：
        端口空闲与否本来就是"能不能绑上"的问题，而 connect 在沙箱/安全软件下会被
        中间层接走（本地实测会超时而非立即拒绝），不适合当"没人监听"的证据。
        """
        base = self._pick((), verify_free=1)
        picked = suggest_free_port(base, tries=1)
        self.assertEqual(picked, base + 1)
        self.assertTrue(self._free(base + 1), "探测后端口必须还能被绑上")
        server = AppHTTPServer(
            ("127.0.0.1", base + 1), RequestHandler, SimpleNamespace(mcp=_StubMcp())
        )
        self.addCleanup(server.server_close)
        self.assertEqual(int(server.server_address[1]), base + 1)

    def test_invalid_start_is_normalized(self) -> None:
        picked = suggest_free_port("这显然不是端口", tries=3)
        self.assertTrue(8766 <= picked <= 8768, picked)

    def test_network_layer_does_not_reach_into_http_layer(self) -> None:
        """层级约束：`naiba.core.network` 是层级 1，不得 import HTTP 层。

        按 AST 判 import（不按源码文本）：文档里提到 HTTP 层是解释性文字，不算越界。
        """
        tree = ast.parse((ROOT / "naiba/core/network.py").read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        self.assertNotIn("naiba.http", imported)
        self.assertFalse([name for name in imported if name.startswith("naiba.")], imported)


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

    def test_run_prompts_for_alternate_port_before_aborting(self) -> None:
        """§九.126 守门：run() 的 OSError 分支必须**先弹窗问用户**，再考虑退出。

        防止将来被改回「绑定失败就直接 `_abort_startup`」——那样弹窗输入端口的能力
        会静默失效（代码还在、永远走不到），而这正是上一版加固留下的体验缺口。
        """
        run = self._function("run")
        pairs = [
            handler
            for node in ast.walk(run)
            if isinstance(node, ast.Try) and "OSError" in self._exception_names(node)
            for handler in node.handlers
        ]
        self.assertTrue(pairs, "run() 必须捕获绑定的 OSError")
        guarded = False
        for handler in pairs:
            prompt_lines: list[int] = []
            abort_lines: list[int] = []
            for node in ast.walk(handler):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if name == "_ask_alternate_port":
                    prompt_lines.append(node.lineno)
                elif name == "_abort_startup":
                    abort_lines.append(node.lineno)
            if not prompt_lines:
                continue
            self.assertTrue(abort_lines, "用户取消时仍要回落既有中文指引（_abort_startup）")
            self.assertLess(
                min(prompt_lines), min(abort_lines),
                "弹窗必须先于退出，否则永远不会问用户",
            )
            guarded = True
        self.assertTrue(guarded, "run() 的 OSError 分支必须先调 _ask_alternate_port")

    def test_bind_failure_retry_is_a_loop(self) -> None:
        """重试必须是循环：输入的新端口仍被占时要能再问一次，而不是绑不上就退出。"""
        run = self._function("run")
        self.assertTrue(
            any(
                isinstance(node, ast.While)
                and any(
                    isinstance(inner, ast.Call)
                    and (
                        (isinstance(inner.func, ast.Name) and inner.func.id == "_ask_alternate_port")
                        or getattr(inner.func, "attr", "") == "_ask_alternate_port"
                    )
                    for inner in ast.walk(node)
                )
                for node in ast.walk(run)
            ),
            "绑定失败的重试必须写在 while 循环里（见 §九.126）",
        )

    def test_alternate_port_prompt_is_cancellable_and_suggests_a_real_port(self) -> None:
        fn = self._function("_ask_alternate_port")
        calls = {
            node.func.id
            for node in ast.walk(fn)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        attrs = {
            node.func.attr
            for node in ast.walk(fn)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertIn("suggest_free_port", calls, "弹窗默认值必须来自实测空闲端口")
        self.assertIn("askinteger", attrs, "必须是可取消的输入框（取消 = 回落既有中文指引）")

    def test_alternate_port_prompt_degrades_when_tkinter_missing(self) -> None:
        """冻结包缺 tcl/tk 资源时只能回落老路径——绝不能让启动更糟（抛异常）。"""
        import launcher  # noqa: PLC0415 - 重型启动器按需导入

        with mock.patch.dict(sys.modules, {"tkinter": None}):
            self.assertIsNone(launcher._ask_alternate_port(8765, "busy"))

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

    def test_health_probe_ignores_system_proxy(self) -> None:
        """健康检查必须绕过系统代理（2.8.2 真实回归：代理软件用户升级后起不来）。

        裸 `urllib.request.urlopen` 跟随 Windows 系统代理与 `http_proxy` 环境变量，
        而绕过列表只写「localhost」/「<local>」时不含 127.0.0.1 ⇒ 回环自检被送进
        代理后 50 次全失败，应用被判 "down" 拒绝启动、重启也无效。这里把代理指向
        一个必然拒绝连接的死端口：探测仍须返回 "ok"。
        """
        import launcher  # noqa: PLC0415 - 重型启动器按需导入

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

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Ours)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(self._shutdown_server, server, thread)

        dead_proxy = "http://127.0.0.1:1"
        # urllib 的默认 opener 在首次 urlopen 时构建并全局缓存（代理在构建时定死），
        # 所以环境变量生效与否取决于缓存：清掉它，确保这次探测真的按新代理环境走。
        with mock.patch.dict(os.environ, {
            "http_proxy": dead_proxy, "HTTP_PROXY": dead_proxy,
            "https_proxy": dead_proxy, "HTTPS_PROXY": dead_proxy,
        }), mock.patch.object(urllib.request, "_opener", None):
            state = launcher.Launcher()._wait_healthy(f"http://127.0.0.1:{server.server_address[1]}")
        self.assertEqual(state, "ok", "回环健康检查被代理劫持：代理软件用户会完全无法启动")

    @staticmethod
    def _shutdown_server(server: ThreadingHTTPServer, thread: threading.Thread) -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _FakeConfig:
    """最小 config 替身：`run()` 只需要 `data` 与 `save()`。"""

    def __init__(self, data: dict, error: BaseException | None = None) -> None:
        self.data = dict(data)
        self.save_calls = 0
        self.error = error

    def save(self) -> None:
        self.save_calls += 1
        if self.error is not None:
            raise self.error


class AlternatePortPromptTests(unittest.TestCase):
    """§九.126 端到端（全外部副作用打桩）：输入新端口 → 写回 config → 用新端口继续启动。

    不开窗、不绑真端口：沙箱会拦 GUI，结论不能建立在窗口探针上（见 §九 的沙箱约定）。
    """

    def setUp(self) -> None:
        import launcher  # noqa: PLC0415 - 重型启动器按需导入

        self.launcher = launcher
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _run(self, bind_errors, prompt_answers, save_error=None):
        launcher = self.launcher
        app = SimpleNamespace(
            config=_FakeConfig({"host": "0.0.0.0", "port": 8765, "access_token": "tok"}, save_error),
            paths=SimpleNamespace(app_dir=Path(self.tmp.name) / "app"),
            stop=mock.MagicMock(),
        )
        errors = list(bind_errors)
        answers = list(prompt_answers)
        state: dict[str, list] = {"bound": [], "prompts": [], "healthy": [], "status": [], "notified": [], "window": []}

        def fake_bind(_self, host, port):
            state["bound"].append(port)
            if errors:
                pending = errors.pop(0)
                if pending is not None:  # None = 这一次绑定成功
                    raise pending

        def fake_prompt(port, detail=""):
            state["prompts"].append((port, detail))
            return answers.pop(0) if answers else None

        def fake_window(*args, **kwargs):
            state["window"].append(args[1] if len(args) > 1 else kwargs.get("url"))
            return mock.MagicMock()

        fake_webview = mock.MagicMock()
        fake_webview.create_window.side_effect = fake_window
        with mock.patch.dict(sys.modules, {"webview": fake_webview}), \
                mock.patch.object(launcher.srv, "APP", None, create=True), \
                mock.patch.object(launcher.srv, "NaibaChatApp", return_value=app), \
                mock.patch.object(launcher.srv, "STATUS_PATH", Path(self.tmp.name) / "status.json"), \
                mock.patch.object(launcher.srv, "acquire_instance_lock", return_value=mock.MagicMock()), \
                mock.patch.object(
                    launcher.srv, "write_status",
                    side_effect=lambda host, port, token: state["status"].append((host, port)),
                ), \
                mock.patch.object(launcher.Launcher, "_bind_server", fake_bind), \
                mock.patch.object(
                    launcher.Launcher, "_wait_healthy",
                    side_effect=lambda url: state["healthy"].append(url) or "ok",
                ), \
                mock.patch.object(launcher.Launcher, "_build_tray", return_value=mock.MagicMock()), \
                mock.patch.object(launcher.Launcher, "_serve", autospec=True), \
                mock.patch.object(launcher, "_resolve_app_icon", return_value=(None, None)), \
                mock.patch.object(launcher, "_notify_startup_error", side_effect=state["notified"].append), \
                mock.patch.object(launcher, "_ask_alternate_port", side_effect=fake_prompt):
            exit_exc = None
            try:
                launcher.Launcher().run()
            except SystemExit as exc:  # 「取消」路径：run() 以 SystemExit(2) 退出
                exit_exc = exc
        return app, state, exit_exc

    def test_cancel_falls_back_to_existing_guidance_and_writes_nothing(self) -> None:
        app, state, exit_exc = self._run(
            [OSError("[WinError 10048] 通常每个套接字地址只允许使用一次")], [None]
        )
        self.assertIsNotNone(exit_exc, "取消必须退出启动，而不是继续往下开窗")
        self.assertEqual(exit_exc.code, 2)
        self.assertEqual(state["bound"], [8765], "取消后不该再试绑定")
        self.assertEqual(app.config.data["port"], 8765, "取消不得写回端口")
        self.assertEqual(app.config.save_calls, 0)
        self.assertEqual(len(state["notified"]), 1)
        self.assertIn("netstat -ano | findstr :8765", state["notified"][0], "取消要回落既有中文指引")
        self.assertEqual(state["status"], [], "启动失败不该写状态文件")

    def test_entering_new_port_retries_and_persists_to_config(self) -> None:
        app, state, exit_exc = self._run([OSError("busy"), None], [8766])
        self.assertIsNone(exit_exc)
        self.assertEqual(state["bound"], [8765, 8766], "必须拿新端口重试绑定")
        self.assertEqual(app.config.data["port"], 8766)
        self.assertEqual(app.config.save_calls, 1, "换端口要写回 config，下次启动直接生效")
        self.assertEqual(state["status"], [("0.0.0.0", 8766)], "状态文件记录的是新端口")
        self.assertEqual(state["healthy"], ["http://127.0.0.1:8766"], "自检跟着换端口")
        self.assertEqual(state["window"], ["http://127.0.0.1:8766/?token=tok"], "窗口地址跟着换端口")
        self.assertEqual(state["notified"], [], "换端口成功就不该再弹失败提示")

    def test_second_conflict_prompts_again_with_the_latest_port(self) -> None:
        app, state, exit_exc = self._run([OSError("busy-8765"), OSError("busy-8766")], [8766, 8767])
        self.assertIsNone(exit_exc)
        self.assertEqual(state["bound"], [8765, 8766, 8767], "新端口又被占时要能再问一次")
        self.assertEqual([item[0] for item in state["prompts"]], [8765, 8766])
        self.assertIn("busy-8766", state["prompts"][1][1], "第二次弹窗要带最新失败原因")
        self.assertEqual(app.config.data["port"], 8767)

    def test_config_write_failure_does_not_block_startup(self) -> None:
        app, state, exit_exc = self._run(
            [OSError("busy"), None], [8766], save_error=OSError("磁盘只读")
        )
        self.assertIsNone(exit_exc)
        self.assertEqual(app.config.data["port"], 8766, "内存里仍要按新端口启动")
        self.assertEqual(app.config.save_calls, 1)
        self.assertEqual(state["window"], ["http://127.0.0.1:8766/?token=tok"])


if __name__ == "__main__":
    unittest.main()
