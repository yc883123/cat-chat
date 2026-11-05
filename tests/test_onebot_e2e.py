# -*- coding: utf-8 -*-
"""端到端：QQ 私聊 → 真实 Run → 完整回复 → 分段投递（假 NapCat + 假 OpenAI 模型）。

与 ``test_onebot_channel.py`` 的分工：那份用假 Run 管理器覆盖分支与状态账本，
这份跑**真实**的 ``NaibaChatApp`` + ``ConversationRunManager`` + 存储 + 一个本地
假 OpenAI 流式服务，只把 NapCat 和模型换成假的（计划 §七「自动化测试全部使用
本地假 NapCat 和假模型」）。

它守住的是最容易「单测全绿、真机不通」的一段：适配器等 Run 终态、从存储读回
落库后的完整 assistant 消息、再按 QQ 上限切段发出去。
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

from naiba.channels import store as channel_store  # noqa: E402
from naiba.config import normalize_onebot  # noqa: E402
from naiba.paths import PathContext  # noqa: E402

LONG_REPLY = "第一段：这是模型的完整回复。\n\n" + ("正文内容" * 1200) + "\n\n最后一段。"


def sse_chunk(text: str, finish: str | None = None) -> bytes:
    payload = {
        "id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 1,
        "model": "fake-model",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


class _FakeModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
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
            half = len(LONG_REPLY) // 2
            self.wfile.write(sse_chunk(LONG_REPLY[:half]))
            self.wfile.flush()
            self.wfile.write(sse_chunk(LONG_REPLY[half:], finish="stop"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


class _FakeNapCat:
    """假 NapCat：记录发出去的消息；GetFile/GetImage 返回 base64 字节。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    def call_action(self, action, params):
        with self._lock:
            self.calls.append((action, dict(params)))
        if action in {"get_image", "get_file", "get_record"}:
            import base64

            return {"status": "ok", "retcode": 0, "data": {
                "base64": base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii"),
                "file": "a.png",
            }}
        return {"status": "ok", "retcode": 0, "data": {"message_id": len(self.calls)}}

    def texts(self) -> list[str]:
        with self._lock:
            return [
                str(params.get("message") or "")
                for action, params in self.calls if action.startswith("send_")
            ]


class QQEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.model_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeModelHandler)
        self.model_port = self.model_server.server_address[1]
        threading.Thread(target=self.model_server.serve_forever, daemon=True).start()
        self.addCleanup(self.model_server.shutdown)

        from naiba.app import NaibaChatApp

        ctx = PathContext(
            app_dir=self.root,
            exe_dir=self.root,
            resource_dir=ROOT,
            public_dir=ROOT / "public",
            config_path=self.root / "config.json",
            data_dir=self.root / "data",
            status_path=self.root / "data" / "server.json",
            lock_path=self.root / "data" / "server.lock",
        )
        self.app = NaibaChatApp(paths=ctx)
        self.addCleanup(self.app.stop)
        self.app.config.data["providers"] = [{
            "id": "fake", "name": "E2E 假供应商", "kind": "online",
            "request_format": "openai_chat",
            "base_url": f"http://127.0.0.1:{self.model_port}/v1",
            "api_key": "k", "model": "fake-model",
        }]
        self.app.config.set_default_model_key("online:fake")
        self.app.config.data["onebot"] = normalize_onebot({
            "enabled": True, "access_token": "onebot-secret",
            "allowed_user_ids": ["20000"], "allow_groups": False,
        })
        self.app.config.save()
        self.napcat = _FakeNapCat()
        self.app.onebot.adapter.sender = self.napcat
        self.store = channel_store.ChannelStore(self.root / "data" / "channels" / "qq.db")

    def _make_conversation(self):
        payload, status = self.app.api_create_conversation({
            "title": "QQ 端到端", "model_key": "online:fake", "model_name": "fake-model",
        })
        self.assertEqual(status, 201, payload)
        return str(payload["id"])

    def _bind(self, conversation_id, qq_user_id="20000", account_id="10000"):
        binding = self.app.onebot.bind_conversation(
            qq_user_id=qq_user_id, conversation_id=conversation_id,
            account_id=account_id,
        )
        self.assertEqual(binding["conversation_id"], conversation_id)

    def _event(self, text="你好", *, message_id=1, user_id=20000, extra=None):
        return {
            "post_type": "message", "message_type": "private",
            "self_id": 10000, "message_id": message_id, "user_id": user_id,
            "message": [{"type": "text", "data": {"text": text}}] + list(extra or []),
        }

    def _wait_replied(self, message_id, timeout=40.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            record = self.store.lookup_message("10000", str(message_id))
            if record and record.get("status") == channel_store.MESSAGE_REPLIED:
                return record
            time.sleep(0.1)
        return self.store.lookup_message("10000", str(message_id))

    def test_private_message_runs_really_and_returns_segmented_reply(self):
        conversation_id = self._make_conversation()
        self._bind(conversation_id)
        self.app.onebot.adapter.handle_event(self._event("请给我一段长回复", message_id=7))
        record = self._wait_replied(7)
        self.assertIsNotNone(record, "消息没有进入投递完成状态")
        self.assertEqual(record["status"], channel_store.MESSAGE_REPLIED)
        self.assertEqual(record["conversation_id"], conversation_id)
        self.assertTrue(record["run_id"])

        sent = [text for text in self.napcat.texts() if text]
        self.assertGreaterEqual(len(sent), 2, "长回复应被切分为多段")
        self.assertTrue(all(len(text) <= 3000 for text in sent))
        joined = "".join(sent)
        self.assertIn("第一段：这是模型的完整回复。", joined)
        self.assertIn("最后一段。", joined)

        # Run 真的完成了，且完整消息落在会话里（不是只靠 delta）。
        run = self.app.storage.get_background_task(record["run_id"])
        self.assertEqual(run["status"], "completed")
        conversation = self.app.storage.get_conversation(conversation_id, include_trace=False)
        assistant = [m for m in conversation["messages"] if m["role"] == "assistant"]
        self.assertTrue(assistant)
        self.assertIn("最后一段。", assistant[-1]["content"])

    def test_duplicate_event_under_real_run_creates_one_run(self):
        conversation_id = self._make_conversation()
        self._bind(conversation_id)
        event = self._event("只回答一次", message_id=8)
        self.app.onebot.adapter.handle_event(event)
        self.app.onebot.adapter.handle_event(event)
        record = self._wait_replied(8)
        self.assertEqual(record["status"], channel_store.MESSAGE_REPLIED)
        runs = [
            item for item in self.app.storage.list_background_tasks(conversation_id, limit=50)
            if item.get("kind") == "chat"
        ]
        self.assertEqual(len(runs), 1)

    def test_image_attachment_reaches_conversation(self):
        conversation_id = self._make_conversation()
        self._bind(conversation_id)
        self.app.onebot.adapter.handle_event(self._event(
            "看这张图", message_id=9,
            extra=[{"type": "image", "data": {"file": "a.png", "file_id": "fid-1", "url": ""}}],
        ))
        record = self._wait_replied(9)
        self.assertIsNotNone(record)
        conversation = self.app.storage.get_conversation(conversation_id, include_trace=False)
        user_messages = [m for m in conversation["messages"] if m["role"] == "user"]
        self.assertTrue(user_messages)
        metadata = user_messages[0].get("metadata") or {}
        attachments = metadata.get("attachments") or []
        self.assertEqual(len(attachments), 1)
        self.assertTrue(Path(attachments[0]["path"]).is_file())

    def test_unbound_user_does_not_reach_model(self):
        conversation_id = self._make_conversation()
        self._bind(conversation_id)
        self.app.onebot.adapter.handle_event(self._event("你好", message_id=10, user_id=99999))
        deadline = time.time() + 5
        while time.time() < deadline:
            record = self.store.lookup_message("10000", "10")
            if record and record["status"] == channel_store.MESSAGE_REJECTED:
                break
            time.sleep(0.05)
        record = self.store.lookup_message("10000", "10")
        self.assertEqual(record["status"], channel_store.MESSAGE_REJECTED)
        self.assertTrue(any("还没有被允许" in text for text in self.napcat.texts()))
        conversation = self.app.storage.get_conversation(conversation_id, include_trace=False)
        self.assertEqual(
            [m for m in conversation["messages"] if m["role"] == "user"], []
        )


if __name__ == "__main__":
    unittest.main()
