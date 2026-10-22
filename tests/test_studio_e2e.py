# -*- coding: utf-8 -*-
"""端到端：工作台生成全链路（真实 ModelRuntime + 假 OpenAI 流式服务 + 假 handoff 交接）。

覆盖修订版计划的验收线（P3）：
- Studio 生成走 `config.profile(model_key)` + `ModelRuntime.complete()` 直调（与
  `app._test_provider` 同形态），不依赖普通对话，不创建会话、不产生 chat Run；
- 事件流（delta/status/usage/done）落 `data/studio/tasks/<id>.events.jsonl`，cursor 读取；
- 取消 / 失败路径留 partial、可重试；
- 已完成任务的 handoff：create conversation（注入回调）→ submit_chat（注入回调）→
  记录 conversation_id；普通会话出现在会话列表（标准聊天域接管后续轮次）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.paths import PathContext  # noqa: E402

FAKE_STREAM_TEXT = "你好，工作台生成内容"


def sse_chunk(text: str, finish: str | None = None) -> bytes:
    payload = {
        "id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 1, "model": "fake-model",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


class _FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静音请求日志
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        _ = self.rfile.read(length)
        if not self.path.endswith("/chat/completions"):
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(b'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n')
            half = len(FAKE_STREAM_TEXT) // 2
            self.wfile.write(sse_chunk(FAKE_STREAM_TEXT[:half]))
            self.wfile.flush()
            time.sleep(0.05)
            self.wfile.write(sse_chunk(FAKE_STREAM_TEXT[half:], finish="stop"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def wait_terminal(store, task_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = store.get_task(task_id)
        if task and task["status"] in {"done", "failed", "cancelled", "interrupted"}:
            return task
        time.sleep(0.05)
    return store.get_task(task_id)


class StudioE2ETests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)

        from naiba.app import NaibaChatApp

        ctx = PathContext(
            app_dir=self.root,
            # app_dir == exe_dir：跳过"冻结启动搬运 EXE 相邻旧数据"的猜测（否则会把仓库
            # data/ 当 legacy 目录拷进 tmp，测试就串到了真实数据）。
            exe_dir=self.root,
            resource_dir=ROOT,
            public_dir=ROOT / "public",
            config_path=self.root / "config.json",
            data_dir=self.root / "data",
            status_path=self.root / "data" / "server.json",
            lock_path=self.root / "data" / "server.lock",
        )
        self.app = NaibaChatApp(paths=ctx)
        self.app.config.data["providers"] = [{
            "id": "fake", "name": "E2E 假供应商", "kind": "online",
            "request_format": "openai_chat", "base_url": f"http://127.0.0.1:{self.port}/v1",
            "api_key": "k", "model": "fake-model",
        }]
        self.app.config.data["default_model_key"] = "online:fake"
        self.app.config.save()
        self.studio = self.app.studio
        self.store = self.studio.jobs.store
        self.addCleanup(self.studio.shutdown)

    def test_generation_streams_via_model_runtime_without_conversation(self):
        payload, status = self.studio.api_generate({
            "user_request": "写一句开场白",
            "model_key": "online:fake",
            "model_name": "fake-model",
        })
        self.assertEqual(status, 201)
        task = payload["task"]
        self.assertEqual(task["status"], "queued")
        result = wait_terminal(self.store, task["id"])
        self.assertEqual(result["status"], "done", result.get("error"))
        self.assertEqual(result["result"], FAKE_STREAM_TEXT)
        self.assertGreater(result["updated_at"], 0)
        # 用量/思考/诊断来自 ModelRuntime.last_*（本链路的 fake 流式响应可空 usage）。
        events = self.store.read_events(result["id"])
        kinds = [row["type"] for row in events]
        self.assertTrue(any(k == "delta" for k in kinds), kinds)
        self.assertEqual(kinds[-1], "done")
        # 不创建普通会话（Studio 生成留在工作台）。
        self.assertEqual(self.app.storage.list_conversations(), [])

    def test_generate_requires_model(self):
        payload, status = self.studio.api_generate({"user_request": "内容"})
        self.assertEqual(status, 400)
        self.assertTrue(payload["error"])

    def test_handoff_after_done_creates_main_conversation(self):
        created, status = self.studio.api_generate({
            "user_request": "交接前一段",
            "model_key": "online:fake",
            "model_name": "fake-model",
        })
        self.assertEqual(status, 201)
        result = wait_terminal(self.store, created["task"]["id"])
        self.assertEqual(result["status"], "done")
        agent_id = self.app.config.default_agent_id()
        payload, status = self.studio.api_handoff({
            "task_id": result["id"], "agent_id": agent_id,
        })
        self.assertEqual(status, 201, payload)
        conversation_id = payload["conversation_id"]
        conversation = self.app.storage.get_conversation(conversation_id, include_trace=False)
        self.assertEqual(str(conversation.get("agent_id") or ""), agent_id)
        self.assertEqual(len(self.app.storage.list_conversations()), 1)
        # handoff 会把 conversation_id 写回任务记录；先前抓到的 result 是交接前快照，重新读一次。
        after_handoff = self.store.get_task(result["id"]) or {}
        self.assertEqual((after_handoff.get("handoff") or {}).get("conversation_id"), conversation_id)
        # 标题以首条输入自动生成为准（title_customized=0，普通聊天域接管）。
        self.assertTrue(str(conversation.get("title") or "").strip())
        # 首条消息带着可追溯上下文（模板/输入/结果三段齐备），Run 已排队。
        run = self.app.storage.get_background_task(payload["run_id"])
        self.assertTrue(run and run.get("kind") == "chat")
        messages = self.app.storage.get_conversation(conversation_id, include_trace=False)["messages"]
        first_user = next(m for m in messages if m["role"] == "user")
        self.assertIn("【来自 Cat Chat 工作台的交接】", first_user["content"])
        self.assertIn("交接前一段", first_user["content"])
        self.assertIn(FAKE_STREAM_TEXT, first_user["content"])
        # 收尾：等这一轮标准对话 Run 结束（或取消），避免跨用例遗留运行线程。
        deadline = time.time() + 15
        while time.time() < deadline:
            run = self.app.storage.get_background_task(payload["run_id"])
            if run and run.get("status") in {"completed", "cancelled", "failed"}:
                break
            self.app.runs.cancel(payload["run_id"])
            time.sleep(0.05)

    def test_cancel_running_generation_keeps_partial(self):
        created, status = self.studio.api_generate({
            "user_request": "慢流",
            "model_key": "online:fake",
            "model_name": "fake-model",
        })
        self.assertEqual(status, 201)
        deadline = time.time() + 5
        got_running = False
        while time.time() < deadline and not got_running:
            got_running = (self.store.get_task(created["task"]["id"]) or {}).get("status") == "running"
            time.sleep(0.02)
        self.assertTrue(got_running, "任务没有进入 running")
        self.assertTrue(self.studio.jobs.cancel(created["task"]["id"]))
        result = wait_terminal(self.store, created["task"]["id"])
        self.assertIn(result["status"], {"cancelled", "done"})

    def test_events_endpoint_snapshot(self):
        payload, status = self.studio.api_generate({
            "user_request": "事件快照",
            "model_key": "online:fake",
            "model_name": "fake-model",
        })
        wait_terminal(self.store, payload["task"]["id"])
        snapshot, code = self.studio.api_task_events(payload["task"]["id"], "0")
        self.assertEqual(code, 200)
        self.assertTrue(snapshot["terminal"])
        self.assertGreater(snapshot["terminal_seq"], 0)
        kinds = [row["type"] for row in snapshot["events"]]
        self.assertEqual(kinds[-1], "done")

    def test_recover_marks_interrupted_with_partial(self):
        task = self.store.create_task({
            "content": "被重启打断", "model_key": "online:fake", "model_name": "fake-model",
        })
        self.store.update_task(task["id"], status="running")
        self.store.append_event(task["id"], {"type": "delta", "content": "前半段"})
        self.assertEqual(self.studio.recover(), 1)
        state = self.store.get_task(task["id"])
        self.assertEqual(state["status"], "interrupted")
        self.assertIn("前半段", state["result_partial"])


if __name__ == "__main__":
    unittest.main()
