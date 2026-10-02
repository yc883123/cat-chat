# -*- coding: utf-8 -*-
"""护栏：出网的消息载荷不带 `metadata.trace`（前端零读取），写入回路的快照原样保留。

为什么钉死：`trace` 是「本轮发给模型的完整字节序列」，只服务后端回放，`public/` 里没有任何
引用，却是消息 metadata 里最占体积的一份（实测单条可以到几百 KB）。`GET /api/conversations/<id>`
每次都把整段会话的 trace 序列化一遍——前端每开一次会话、每轮收尾重载（终态消息改由会话 API
提供，见 run/chat.py）都要付一次。剥掉它，长会话每次打开省几十 MB。

**但改写不得越界**：`/api/messages/delete` 的 `removed` 是撤销删除的写回快照（前端把它原样
POST 回 `/api/messages/restore`），在那里剥 trace 就等于撤销时把 trace 永久丢掉——本守门同时
钉住这条边界，防止以后有人图省事在出口做"一刀切"。

真机形态另有 Playwright 冒烟（页面打开长会话不再下载 trace，见 verify/browser_smoke.cjs 一族）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core.messages import strip_message_traces  # noqa: E402
from naiba.http import AppHTTPServer, RequestHandler  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402

TRACE = [{"role": "user", "content": "问"}, {"role": "assistant", "content": "答"}]


def _message(message_id: str, content: str = "答复") -> dict:
    return {
        "id": message_id,
        "role": "assistant",
        "content": content,
        "created_at": 1,
        "metadata": {
            "trace": TRACE,
            "reasoning": ["想过"],
            "tool_runs": [{"tool": "read_file"}],
            "usage": {"context_tokens": 100},
        },
    }


class _StubApp:
    """`GET /api/conversations/<id>` 与 `/branch` 只用到 storage 的这两个方法。"""

    def __init__(self, conversation: dict) -> None:
        self.config = SimpleNamespace(data={"access_token": ""})
        self.storage = SimpleNamespace(
            get_conversation=lambda _cid, include_messages=True: json.loads(
                json.dumps(conversation)
            ),
            branch_conversation=lambda _cid, _mid, reset_agent=False: {
                "conversation": json.loads(json.dumps(conversation)),
                "branch_message": {"id": "u1", "role": "user", "content": "分支点"},
            },
        )


class OutboundMessagePayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_payload_")
        self.addCleanup(self.tmp.cleanup)
        self.conversation = {
            "id": "conv-1",
            "title": "出网载荷实验",
            "messages": [_message("m1"), _message("m2", "第二答")],
        }

    def _serve(self, conversation: dict | None = None, app=None) -> str:
        app = app or _StubApp(conversation or self.conversation)
        server = AppHTTPServer(("127.0.0.1", 0), RequestHandler, app)
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()

        def _stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(_stop)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def _get(self, url: str) -> tuple[int, dict]:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")

    def _post(self, url: str, body: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    # ---- 1. 出网不带 trace ----

    def test_conversation_get_has_no_trace(self) -> None:
        base = self._serve()
        status, payload = self._get(f"{base}/api/conversations/conv-1")
        self.assertEqual(status, 200)
        messages = payload["messages"]
        self.assertEqual(len(messages), 2, "消息条数不变")
        for message in messages:
            self.assertNotIn("trace", message["metadata"], "出网消息不得带 trace")
            # 其余 metadata 一个都不能少（前端要渲染思考/工具卡/圆环）
            self.assertEqual(message["metadata"]["reasoning"], ["想过"])
            self.assertEqual(message["metadata"]["tool_runs"], [{"tool": "read_file"}])
            self.assertEqual(message["metadata"]["usage"], {"context_tokens": 100})
            self.assertTrue(message["content"])

    def test_branch_response_has_no_trace(self) -> None:
        base = self._serve()
        status, payload = self._post(
            f"{base}/api/conversations/conv-1/branch", {"message_id": "u1"}
        )
        self.assertIn(status, (200, 201))
        self.assertEqual(payload["branch_message"]["content"], "分支点", "预填字段照旧")
        for message in payload["conversation"]["messages"]:
            self.assertNotIn("trace", message["metadata"], "分支响应里的复制历史同样不带 trace")

    # ---- 2. 写回快照必须原样（不能被"出口一刀切"波及） ----

    def test_delete_snapshot_keeps_trace(self) -> None:
        app = _StubApp(self.conversation)
        app.api_delete_message = lambda _body: (
            {"ok": True, "mode": "single", "removed": [_message("m1")]},
            200,
        )
        server = AppHTTPServer(("127.0.0.1", 0), RequestHandler, app)
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=5)))
        base = f"http://127.0.0.1:{server.server_address[1]}"
        status, payload = self._post(f"{base}/api/messages/delete", {"conversation_id": "conv-1"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["removed"][0]["metadata"]["trace"], TRACE,
                         "撤销删除的写回快照必须逐字节完整（剥了等于撤销时丢 trace）")

    # ---- 3. 存储层与出网层的口径差异 ----

    def test_storage_still_hydrates_trace(self) -> None:
        """剥 trace 只发生在 HTTP 出口：存储层读回必须照旧带上它（回放/审计靠它）。"""
        storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        conversation = storage.create_conversation(title="口径差异")
        storage.add_message(str(conversation["id"]), "assistant", "答复", {"trace": TRACE})
        loaded = storage.get_conversation(str(conversation["id"]))
        self.assertEqual(loaded["messages"][0]["metadata"]["trace"], TRACE)

    def test_http_response_drops_huge_trace_end_to_end(self) -> None:
        """真 storage + 真 handler：一条 200KB 的 trace 不进出网响应，响应体小一个量级。

        这条是"为什么值得改"的量化判据：存储层那份照旧带 trace（回放要用），出网那份不带。
        """
        storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        conversation = storage.create_conversation(title="大 trace 会话")
        conversation_id = str(conversation["id"])
        storage.add_message(conversation_id, "user", "问")
        storage.add_message(
            conversation_id, "assistant", "答", {"trace": [{"role": "user", "content": "x" * 200_000}]}
        )
        stored_bytes = len(
            json.dumps(storage.get_conversation(conversation_id), ensure_ascii=False).encode("utf-8")
        )
        app = SimpleNamespace(
            config=SimpleNamespace(data={"access_token": ""}),
            storage=storage,
        )
        base = self._serve(app=app)
        status, payload = self._get(f"{base}/api/conversations/{conversation_id}")
        self.assertEqual(status, 200)
        self.assertGreater(stored_bytes, 200_000, "前提：库内那份确实很大")
        for message in payload["messages"]:
            self.assertNotIn("trace", message.get("metadata") or {})
        response_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self.assertLess(
            response_bytes, stored_bytes / 10,
            f"出网响应必须小一个量级（库内 {stored_bytes} 字节 → 出网 {response_bytes} 字节）",
        )

    def test_helper_returns_count_and_tolerates_odd_shapes(self) -> None:
        messages = [_message("m1"), {"role": "assistant"}, "not-a-dict", _message("m2")]
        self.assertEqual(strip_message_traces(messages), 2)
        self.assertEqual(strip_message_traces(None), 0)
        self.assertNotIn("trace", messages[0]["metadata"])
        self.assertEqual(strip_message_traces(messages), 0, "幂等：再剥一次没有任何改动")


if __name__ == "__main__":
    unittest.main()
