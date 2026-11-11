"""真实 HTTP 静默流必须结束 Studio 任务、保留产出并释放串行队列。"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from naiba.llm.runtime import ModelRuntime
from naiba.studio.runner import StudioRunner
from naiba.studio.store import StudioStore


REASONING = "上游已返回的思考"
PARTIAL = "尚未写完的正文"
COMPLETE = "后续任务正常完成"


def _chunk(**delta):
    return ("data: " + json.dumps({
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }, ensure_ascii=False) + "\n\n").encode("utf-8")


class _StreamHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        content = request["messages"][-1]["content"]
        with self.server.requests_lock:
            self.server.requests.append(content)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            if content != "stall":
                self.wfile.write(_chunk(content=COMPLETE))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            self.wfile.write(_chunk(reasoning_content=REASONING))
            if self.server.partial:
                self.wfile.write(_chunk(content=self.server.partial))
            self.wfile.flush()
            # 不发送 done、不关闭 HTTP；注释保活与彻底静默分别复现两条等待路径。
            while not self.server.release.wait(0.02):
                if self.server.keepalive:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class _ObservedModels(ModelRuntime):
    """只观察真实模型回调何时已被 Studio 接收，用事件同步取消测试。"""

    def __init__(self):
        super().__init__()
        self.reasoning_seen = threading.Event()
        self.content_seen = threading.Event()

    def complete(self, profile, messages, options, status=None):
        def observe(payload):
            if status is not None:
                status(payload)
            if payload.get("type") == "reasoning_delta" and payload.get("content"):
                self.reasoning_seen.set()
            if payload.get("type") == "delta" and payload.get("content"):
                self.content_seen.set()

        return super().complete(profile, messages, options, observe)


class StudioStreamStallTests(unittest.TestCase):
    def setUp(self):
        self.initial_threads = set(threading.enumerate())
        self.folder = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.folder.name))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _StreamHandler)
        self.server.daemon_threads = False
        self.server.release = threading.Event()
        self.server.requests_lock = threading.Lock()
        self.server.requests = []
        self.server.keepalive = False
        self.server.partial = ""
        self.server_thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.02),
            name="studio-stall-http",
        )
        self.server_thread.start()
        config = Mock()
        config.profile.return_value = {
            "kind": "online", "name": "loopback fixture", "model": "fixture-model",
            "request_format": "openai_chat", "api_key": "fixture",
            "base_url": f"http://127.0.0.1:{self.server.server_port}/v1",
        }
        config.runtime_guard_options.return_value = {}
        self.models = _ObservedModels()
        self.runner = StudioRunner(SimpleNamespace(config=config, models=self.models), self.store)
        self.addCleanup(self._close)

    def _close(self):
        # 即使断言失败，也解除服务端等待并关闭、join 所有本测试启动的线程。
        self.server.release.set()
        self.runner.shutdown(timeout=5)
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(5)
        self.assertFalse(self.server_thread.is_alive())
        if self.runner._worker is not None:
            self.runner._worker.join(5)
            self.assertFalse(self.runner._worker.is_alive())
        self.folder.cleanup()
        self.assertEqual([
            thread.name for thread in threading.enumerate()
            if thread not in self.initial_threads
            and thread.name in {"naiba-stream-abort", "naiba-studio-progress", "naiba-studio-run"}
        ], [], "本次静默流的看门狗、进度或执行线程不得遗留")

    def _submit(self, content):
        task = self.store.create_task({
            "content": content, "model_key": "online:fixture", "model_name": "fixture-model",
        })
        self.runner.submit(task["id"])
        return task["id"]

    def _terminal(self, task_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = self.store.get_task(task_id)
            if task["status"] in {"failed", "done", "cancelled"}:
                return task
            time.sleep(0.01)
        self.fail(f"任务未及时收敛：{self.store.get_task(task_id)}")

    def _assert_stall_releases_queue(self, *, keepalive, partial):
        self.server.keepalive = keepalive
        self.server.partial = partial
        with patch("naiba.llm.runtime.ONLINE_STREAM_IDLE_TIMEOUT_SECONDS", 0.3):
            stalled_id = self._submit("stall")
            self.assertTrue(self.models.reasoning_seen.wait(3), "没有收到真实上游思考")
            next_id = self._submit("next")
            self.assertIsNone(self.store.get_task(next_id)["started_at"], "后续任务必须先排队")
            stalled = self._terminal(stalled_id)
            completed = self._terminal(next_id)
        self.assertEqual(stalled["status"], "failed", stalled)
        self.assertIn("没有返回新的内容", stalled["error"])
        self.assertEqual(stalled["result_partial"], partial)
        self.assertGreater(stalled["finished_at"], 0)
        events = self.store.read_events(stalled_id)
        reasoning = "".join(row.get("content", "") for row in events if row["type"] == "reasoning_delta")
        self.assertEqual(reasoning, REASONING)
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(completed["status"], "done", completed)
        self.assertEqual(completed["result"], COMPLETE)
        self.assertGreaterEqual(completed["started_at"], stalled["finished_at"])
        self.assertEqual(self.server.requests, ["stall", "next"], "静默失败不得隐式重放模型请求")

    def test_reasoning_then_keepalive_fails_and_advances_queue(self):
        self._assert_stall_releases_queue(keepalive=True, partial="")

    def test_silent_stream_preserves_partial_and_advances_queue(self):
        self._assert_stall_releases_queue(keepalive=False, partial=PARTIAL)

    def test_shutdown_cancels_silent_stream_and_keeps_buffered_tail(self):
        self.server.partial = PARTIAL
        # 保证短尾尚未达到合批阈值；取消后的收尾 flush 也必须保留已接收文本。
        with patch("naiba.studio.runner.FLUSH_SECONDS", 60.0):
            task_id = self._submit("stall")
            self.assertTrue(self.models.content_seen.wait(3), "真实正文回调尚未到达")
            self.assertFalse(any(row["type"] == "delta" for row in self.store.read_events(task_id)))
            started = time.monotonic()
            self.runner.shutdown(timeout=4)
            self.assertLess(time.monotonic() - started, 5)
            self.assertFalse(self.runner._worker.is_alive(), "关闭必须真正解除 socket 阻塞")
        task = self.store.get_task(task_id)
        self.assertEqual(task["status"], "cancelled", task)
        self.assertEqual(task["result_partial"], PARTIAL)
        self.assertGreater(task["finished_at"], 0)
        events = self.store.read_events(task_id)
        self.assertEqual(events[-1]["type"], "cancelled")
        self.assertEqual("".join(row.get("content", "") for row in events if row["type"] == "reasoning_delta"), REASONING)
        self.assertEqual(self.server.requests, ["stall"])


if __name__ == "__main__":
    unittest.main()
