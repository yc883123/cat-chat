# -*- coding: utf-8 -*-
"""真实服务验收：起一个隔离 Cat Chat + 假 NapCat，走完整 QQ 私聊链路。

与 ``tests/test_onebot_e2e.py`` 的区别在于**走真实传输层**：这里真的开反向
WebSocket 端口、用 ``websockets`` 客户端扮演 NapCat 连进来、发 OneBot 事件、
接 OneBot 动作响应，验证的是「鉴权 → 解析 → 提交 → 投递」这条线上没有被单测
桩掉的部分（计划 S0「建立本地假 NapCat 服务」/ S3 前的自检）。

用法（语义路径由脚本自身推导）::

    .venv\\Scripts\\python.exe verify/onebot_fake_napcat_smoke.py

不使用真实 QQ 与真实模型供应商：模型是一个本地假 OpenAI 流式服务。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REPLY = "假模型的完整回复：这条消息经 QQ 渠道走完了整条链路。"
TOKEN = "smoke-onebot-token"
QQ_USER = "20000"
BOT_ACCOUNT = "10000"


class _ModelHandler(BaseHTTPRequestHandler):
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
        self.end_headers()
        payload = {
            "id": "chatcmpl-smoke", "object": "chat.completion.chunk", "created": 1,
            "model": "fake-model",
            "choices": [{"index": 0, "delta": {"content": REPLY}, "finish_reason": "stop"}],
        }
        self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main() -> int:
    from websockets.sync.client import connect

    from naiba.app import NaibaChatApp
    from naiba.config import normalize_onebot
    from naiba.paths import PathContext

    failures: list[str] = []

    def check(condition: bool, label: str) -> None:
        print(("PASS " if condition else "FAIL ") + label)
        if not condition:
            failures.append(label)

    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    model = ThreadingHTTPServer(("127.0.0.1", 0), _ModelHandler)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    ctx = PathContext(
        app_dir=root, exe_dir=root, resource_dir=ROOT, public_dir=ROOT / "public",
        config_path=root / "config.json", data_dir=root / "data",
        status_path=root / "data" / "server.json", lock_path=root / "data" / "server.lock",
    )
    app = NaibaChatApp(paths=ctx)
    try:
        app.config.data["providers"] = [{
            "id": "fake", "name": "假供应商", "kind": "online",
            "request_format": "openai_chat",
            "base_url": f"http://127.0.0.1:{model.server_address[1]}/v1",
            "api_key": "k", "model": "fake-model",
        }]
        app.config.set_default_model_key("online:fake")
        # 端口先用系统分配的一个空闲端口占位测出来，避免和本机正在运行的实例撞端口。
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        app.config.data["onebot"] = normalize_onebot({
            "enabled": True, "listen_host": "127.0.0.1", "listen_port": free_port,
            "access_token": TOKEN, "allowed_user_ids": [QQ_USER],
        })
        app.config.save()
        status = app.onebot.restart()
        check(status["listening"], f"反向 WebSocket 已监听（{status['error'] or 'ok'}）")
        port = app.onebot._server.socket.getsockname()[1]
        url = f"ws://127.0.0.1:{port}/onebot/v11/ws"

        # 鉴权：错口令必须连不上。
        try:
            with connect(url, additional_headers={"Authorization": "Bearer wrong"}, open_timeout=5):
                check(False, "错口令被拒绝")
        except Exception:
            check(True, "错口令被拒绝")

        conversation = app.storage.create_conversation(
            title="假 NapCat 验收", model_key="online:fake", model_name="fake-model",
        )
        binding = app.onebot.bind_conversation(
            qq_user_id=QQ_USER, conversation_id=conversation["id"], account_id=BOT_ACCOUNT,
        )
        check(binding["conversation_id"] == conversation["id"], "QQ 身份已绑定到会话")

        sent: list[str] = []
        with connect(url, additional_headers={"Authorization": f"Bearer {TOKEN}"}, open_timeout=5) as ws:
            ws.send(json.dumps({
                "post_type": "message", "message_type": "private",
                "self_id": int(BOT_ACCOUNT), "message_id": 9001, "user_id": int(QQ_USER),
                "message": [{"type": "text", "data": {"text": "你好"}}],
            }))
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    raw = ws.recv(timeout=1)
                except TimeoutError:
                    continue
                payload = json.loads(raw)
                action = str(payload.get("action") or "")
                if action == "send_private_msg":
                    sent.append(str((payload.get("params") or {}).get("message") or ""))
                    # 真实 NapCat 会回一条带 echo 的动作响应：假 NapCat 必须照做，
                    # 否则服务端要等到动作超时，验收就会误判成「发送失败」。
                    ws.send(json.dumps({
                        "status": "ok", "retcode": 0, "data": {"message_id": len(sent)},
                        "echo": payload.get("echo"),
                    }))
                    break
                if action:
                    ws.send(json.dumps({
                        "status": "ok", "retcode": 0, "data": {}, "echo": payload.get("echo"),
                    }))
        check(bool(sent), "收到 NapCat 发出的 send_private_msg")
        check(any(REPLY in text for text in sent), "回复正文是落库后的完整消息")

        # 投递状态在发完最后一段之后才翻成 replied：等它落定，别在竞态窗口里断言。
        record: dict = {}
        deadline = time.time() + 10
        while time.time() < deadline:
            record = app.onebot.store.lookup_message(BOT_ACCOUNT, "9001") or {}
            if record.get("status") == "replied":
                break
            time.sleep(0.1)
        check(record.get("status") == "replied", f"投递状态为 replied（实际 {record.get('status')}）")
        run = app.storage.get_background_task(str(record.get("run_id") or ""))
        check(bool(run) and run.get("status") == "completed", "Run 正常完成")
    finally:
        app.stop()
        model.shutdown()
        tmp.cleanup()

    if failures:
        print(f"\n{len(failures)} 项未通过：{'；'.join(failures)}")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
