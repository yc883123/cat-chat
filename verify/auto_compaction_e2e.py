# -*- coding: utf-8 -*-
"""隔离真实服务 + 桩模型 + Playwright：自动整理成功后继续本轮。

覆盖真实 HTTP 链路：
1. 上一轮 assistant 的落库 input_tokens 触发轮首自动整理；
2. 当前 provider/模型收到一次隐藏、非流式总结请求；
3. 整理标记与 compaction 事件落库；
4. 正式流式请求在重建后的历史上继续完成，前端显示整理卡和最终答复。

用法：项目根\\.venv\\Scripts\\python.exe verify\\auto_compaction_e2e.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

APP_PORT = int(os.environ.get("NAIBA_COMPACTION_E2E_PORT", "8831"))
MODEL_PORT = int(os.environ.get("NAIBA_COMPACTION_E2E_MODEL_PORT", "8832"))
BASE = f"http://127.0.0.1:{APP_PORT}"
MODEL_BASE = f"http://127.0.0.1:{MODEL_PORT}/v1"
PROVIDER_ID = "compaction-e2e"
MODEL_NAME = "compaction-e2e-model"
TITLE = "自动整理继续本轮冒烟"
PROMPT = "整理后继续本轮：请给出最终确认。"
FINAL_ANSWER = "正式回复：整理后继续成功。"
SUMMARY = """## 当前目标
- 验证自动整理后本轮继续完成。

## 用户约束
- 必须保留历史事实并继续回答。

## 已确认事实
- 桩模型收到过整理前的历史消息。

## 已作决定
- 使用当前会话同一个模型生成交接总结。

## 完成进度
- 自动整理请求已完成。

## 待办事项
- 继续完成用户当前这一轮。

## 关键工具结果与文件路径
- verify/auto_compaction_e2e.py

## 尚不确定的事项
- 无。

## 下一步
- 返回正式答复。
"""


class _ModelState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict] = []

    def add(self, payload: dict, stream: bool) -> None:
        with self.lock:
            self.requests.append({"payload": payload, "stream": bool(stream)})

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.requests)


MODEL_STATE = _ModelState()


class _StubModelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": MODEL_NAME, "object": "model"}]})
        else:
            self._json(200, {"object": "list", "data": []})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        stream = bool(payload.get("stream"))
        MODEL_STATE.add(payload, stream)
        if not stream:
            if "只回复 OK" in json.dumps(payload.get("messages") or [], ensure_ascii=False):
                self._json(200, {
                    "id": "context-probe", "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
                                  "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 32, "completion_tokens": 1, "total_tokens": 33},
                })
                return
            self._json(200, {
                "id": "compaction-summary",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": SUMMARY},
                              "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7000, "completion_tokens": 180, "total_tokens": 7180},
            })
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def chunk(body: dict) -> None:
            self.wfile.write(f"data: {json.dumps(body, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            chunk({"id": "compaction-answer", "object": "chat.completion.chunk",
                   "choices": [{"index": 0, "delta": {"role": "assistant"}}]})
            chunk({"id": "compaction-answer", "object": "chat.completion.chunk",
                   "choices": [{"index": 0, "delta": {"content": FINAL_ANSWER}}]})
            chunk({"id": "compaction-answer", "object": "chat.completion.chunk",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 5600, "completion_tokens": 40, "total_tokens": 5640}})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args) -> None:  # noqa: D102
        return


def start_model() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", MODEL_PORT), _StubModelHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    return server


def isolated_root() -> Path:
    return Path(tempfile.mkdtemp(prefix="compaction_e2e_", dir=ROOT / "verify"))


def seed(root: Path) -> str:
    from naiba.config import ConfigStore
    from naiba.paths import PathContext
    from naiba.storage.store import ChatStorage

    paths = PathContext.local(root, root / "config.json")
    paths.public_dir = ROOT / "public"
    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({
        "host": "127.0.0.1", "port": APP_PORT, "access_token": "",
        "data_dir": str(root / "data"), "workspace_dir": str(root / "workspace"),
        "skills_dirs": [str(ROOT / "skills")], "mcp_servers": [],
        "providers": [{
            "id": PROVIDER_ID, "kind": "online", "name": "整理 E2E 桩模型",
            "base_url": MODEL_BASE, "api_key": "", "request_format": "openai_chat",
            # 默认 Agent 工具定义本身会占掉一大块窗口；夹具要考的是
            # 「真实 usage 触发 → 总结 → 正式回复继续」，因此给正式回复留足工具预算。
            "model": MODEL_NAME, "context_window": 32000,
        }],
        "default_model_key": f"online:{PROVIDER_ID}",
        "provider_id": PROVIDER_ID,
        "auto_compaction": {"mode": "auto", "threshold_percent": 70,
                             "retain_turns": 1, "summary_max_tokens": 1024},
        "context_warning_percent": 99,
    })
    config.save()
    storage = ChatStorage(root / "data" / "chat.db")
    conversation = storage.create_conversation(
        TITLE, model_key=f"online:{PROVIDER_ID}", model_name=MODEL_NAME,
        # 让隔离夹具使用窄工具集：master 的默认长会话工具 schema
        # 会吞掉 8k 桩模型窗口，整理成功后反而把正式请求挡成窗口已满；
        # 本冒烟验证的是「隐藏总结 → 重建历史 → 正式请求继续」，不是全工具
        # schema 的预算上限。tutor 的只读工具集仍会走真实会话固化/提示词链路。
        agent_id="tutor", permission_mode="full", workspace_dir=str(root / "workspace"),
    )
    conversation_id = str(conversation["id"])
    filler = "历史事实与讨论内容，用于确保整理区间足够长。" * 12
    for index in range(5):
        storage.add_message(conversation_id, "user", f"历史问题 {index + 1}：{filler}")
        metadata = {}
        if index == 4:
            metadata = {"usage": {
                "input_tokens": 24000, "output_tokens": 200, "total_tokens": 24200,
                "context_tokens": 24200, "context_limit": 32000,
                "context_limit_source": "local_config", "requests": 1,
            }}
        storage.add_message(conversation_id, "assistant", f"历史回答 {index + 1}：{filler}", metadata)
    storage.update_conversation_settings(conversation_id, title=TITLE)
    return conversation_id


def wait_health(deadline: float = 30.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    return False


def run() -> int:
    from naiba.app import NaibaChatApp
    from naiba.config import ConfigStore
    from naiba.http import AppHTTPServer
    from naiba.paths import PathContext

    root = isolated_root()
    model = start_model()
    app = None
    server = None
    try:
        conversation_id = seed(root)
        paths = PathContext.local(root, root / "config.json")
        paths.public_dir = ROOT / "public"
        app = NaibaChatApp(paths=paths)
        server = AppHTTPServer(("127.0.0.1", APP_PORT), __import__("naiba.http", fromlist=["RequestHandler"]).RequestHandler, app)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        if not wait_health():
            print("FAIL  隔离服务未就绪")
            return 1
        env = dict(os.environ)
        env.update({"NAIBA_SMOKE_BASE": BASE, "NAIBA_COMPACTION_CONVERSATION": conversation_id,
                    "NAIBA_COMPACTION_TITLE": TITLE, "NODE_PATH": str(Path.home() / "node_modules")})
        import subprocess
        proc = subprocess.run(["node", str(ROOT / "verify" / "auto_compaction_e2e.cjs")],
                              cwd=str(ROOT), env=env, check=False)
        if proc.returncode:
            print("MODEL_REQUESTS", json.dumps(MODEL_STATE.snapshot(), ensure_ascii=False)[:4000])
            try:
                print("CONVERSATION", json.dumps(app.storage.get_conversation(conversation_id), ensure_ascii=False)[:12000])
            except Exception as exc:  # noqa: BLE001
                print("CONVERSATION_READ_FAIL", repr(exc))
            return proc.returncode
        requests = MODEL_STATE.snapshot()
        # provider 启动时还会有一次非流式上下文探测（「只回复 OK」），
        # 不把它误算成隐藏整理请求。
        summary_requests = [
            item for item in requests
            if not item["stream"]
            and "只回复 OK" not in json.dumps(item["payload"].get("messages") or "", ensure_ascii=False)
        ]
        answer_requests = [item for item in requests if item["stream"]]
        if len(summary_requests) != 1 or len(answer_requests) != 1:
            print(f"FAIL  模型请求数不符：summary={len(summary_requests)} answer={len(answer_requests)}")
            return 1
        if PROMPT not in json.dumps(answer_requests[0]["payload"], ensure_ascii=False):
            print("FAIL  正式请求未包含本轮用户输入")
            return 1
        print("PASS  桩模型收到 1 次隐藏总结 + 1 次正式回复")
        print("ALL PASS")
        return 0
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if app is not None:
            app.stop()
        model.shutdown()
        model.server_close()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(run())
