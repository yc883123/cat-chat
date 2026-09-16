# -*- coding: utf-8 -*-
"""冒烟：**流式回复在「点击任务面板某一行」引发的重渲染里不得被拆掉**（2026-09-16 线上缺陷回归）。

现象（用户实测）：后台任务跑着时点任务面板里的某一行 → 界面跳到会话并把消息区整体重渲染，
那一刻正在流式输出的助手气泡（正文此刻还只活在 DOM 里，run 结束才落库）被
``container.replaceChildren()`` 连根拔掉，重渲染后也没人把它挂回来 —— 用户看到的是
「AI 回复消失，只剩下前一条用户侧消息」，而且此后一直不回来。

这条冒烟走**全真链路**：真 `POST /api/chat`、真 run、真事件流，只有模型响应来自本脚本起的
本地假 SSE 服务（按固定节奏吐 delta，好让「流到一半点任务行」这一步稳定复现）。
任务面板那一行用浏览器侧拦截 `/api/tasks` 注入（面板只列后台作业，且点击后就是
`openConversation(当前会话)` —— 缺陷的触发点）。

用法：.venv\\Scripts\\python.exe verify\\stream_rerender_smoke.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PORT = 8798                       # 源码 server
MODEL_PORT = 8799                 # 假模型（SSE）
BASE = f"http://127.0.0.1:{PORT}"
ISOLATED_ROOT = Path(os.environ.get("NAIBA_SMOKE_ROOT", ROOT / "verify" / "stream_rerender_tmp"))
DATA_DIR = ISOLATED_ROOT / "data"
PROVIDER_ID = "smoke-slow"
BOUND_MODEL = "smoke-slow-model"
TITLE = "流式重渲染冒烟"
Q1 = "流式重渲染冒烟：先给历史"
A1 = "第一答：历史内容。"
# 模型逐段吐出的正文：点任务行之后仍应继续落进同一条气泡里。
DELTAS = [
    "先说结论：",
    "这三张图都缺",
    "尺度锚定，",
    "所以我把提示词",
    "重写成了 v2，",
    "每条补上可测量参照物、",
    "数字类比，",
    "以及超出画框的构图。",
    "是否现在提交生成？",
    "（完）",
]
DELTA_DELAY = 0.4                 # 每段间隔：留出「点任务行」的时间窗


def wait_health(deadline: float = 60.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{BASE}/api/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - 启动期连接失败是预期分支
            time.sleep(0.5)
    return False


def isolated_paths():
    from naiba.paths import PathContext

    paths = PathContext.local(ISOLATED_ROOT, ISOLATED_ROOT / "config.json")
    paths.public_dir = ROOT / "public"
    return paths


def seed() -> None:
    from naiba.app import NaibaChatApp
    from naiba.config import ConfigStore
    from naiba.storage.store import ChatStorage
    from types import SimpleNamespace

    paths = isolated_paths()
    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({
        "host": "127.0.0.1",
        "port": PORT,
        "access_token": "",
        "data_dir": str(DATA_DIR),
        "workspace_dir": str(ISOLATED_ROOT / "workspace"),
        "skills_dirs": [str(ROOT / "skills")],
        "mcp_servers": [],
    })
    config.save()
    storage = ChatStorage(DATA_DIR / "chat.db")
    app = SimpleNamespace(config=config, storage=storage)
    NaibaChatApp.api_upsert_model_profile(app, {
        "id": PROVIDER_ID,
        "kind": "online",
        "name": "冒烟慢速 API",
        # 真连本脚本起的假 SSE 服务（流式节奏由它控制）。
        "base_url": f"http://127.0.0.1:{MODEL_PORT}/v1",
        "api_key": "sk-smoke",
        "request_format": "openai_chat",
        "model": BOUND_MODEL,
    })
    conversation = storage.create_conversation(
        TITLE,
        model_key=f"online:{PROVIDER_ID}",
        model_name=BOUND_MODEL,
        workspace_dir=str(ISOLATED_ROOT / "workspace"),
    )
    storage.add_message(conversation["id"], "user", Q1)
    storage.add_message(conversation["id"], "assistant", A1)


class _FakeModelHandler(BaseHTTPRequestHandler):
    """最小 OpenAI 兼容流式响应：按 DELTA_DELAY 逐段吐正文。"""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def chunk(payload: dict) -> None:
            self.wfile.write(
                f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
            )
            self.wfile.flush()

        try:
            chunk({"id": "fake-1", "object": "chat.completion.chunk",
                   "choices": [{"index": 0, "delta": {"role": "assistant"}}]})
            for text in DELTAS:
                time.sleep(DELTA_DELAY)
                chunk({"id": "fake-1", "object": "chat.completion.chunk",
                       "choices": [{"index": 0, "delta": {"content": text}}]})
            time.sleep(DELTA_DELAY)
            chunk({"id": "fake-1", "object": "chat.completion.chunk",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args) -> None:  # noqa: D102 - 关掉访问日志噪音
        return


def start_fake_model() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", MODEL_PORT), _FakeModelHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                     daemon=True).start()
    return server


def main() -> int:
    global ISOLATED_ROOT, DATA_DIR
    # ignore_cleanup_errors：Windows 上刚被 terminate 的服务进程可能还占着 db/-wal 文件，
    # 清理失败不该让一轮冒烟以异常收场（残留的隔离目录不影响下次跑，脚本按时间戳新建）。
    isolated = tempfile.TemporaryDirectory(
        prefix="stream_rerender_", dir=ROOT / "verify", ignore_cleanup_errors=True
    )
    ISOLATED_ROOT = Path(isolated.name)
    DATA_DIR = ISOLATED_ROOT / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    code = 1
    server: subprocess.Popen | None = None
    fake = start_fake_model()
    log = (ROOT / "verify" / "stream_rerender_smoke_server.log").open("w", encoding="utf-8")
    try:
        seed()
        env = dict(os.environ)
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                    "NAIBA_SMOKE_ROOT": str(ISOLATED_ROOT)})
        server = subprocess.Popen(  # noqa: S603 - 固定 argv
            [sys.executable, str(Path(__file__).resolve()), "--serve"],
            cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if not wait_health():
            print(f"server 未就绪（日志见 {log.name}）")
            return 1
        node_env = dict(os.environ)
        node_env["NODE_PATH"] = str(Path.home() / "node_modules")
        node_env["NAIBA_SMOKE_BASE"] = BASE
        node_env["NAIBA_SMOKE_BOUND_MODEL"] = BOUND_MODEL
        node_env["NAIBA_SMOKE_Q1"] = Q1
        node_env["NAIBA_SMOKE_A1"] = A1
        node_env["NAIBA_SMOKE_SEND"] = "点任务行之后写的那条消息"
        node_env["NAIBA_SMOKE_FIRST_DELTA"] = DELTAS[0]
        node_env["NAIBA_SMOKE_LAST_DELTA"] = DELTAS[-1]
        node = subprocess.run(  # noqa: S603 - 固定 argv
            ["node", str(ROOT / "verify" / "stream_rerender_smoke.cjs")],
            cwd=str(ROOT), env=node_env, check=False,
        )
        code = node.returncode
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
        fake.shutdown()
        fake.server_close()
        log.close()
        isolated.cleanup()
        print(f"已清理独立测试目录（开发 config.json 未修改）"
              f"；server 已退出（exit={server.returncode if server else 'n/a'}）")
    return code


if __name__ == "__main__":
    if "--serve" in sys.argv:
        from naiba.app import NaibaChatApp
        from naiba.http import AppHTTPServer, RequestHandler

        app = NaibaChatApp(paths=isolated_paths())
        server = AppHTTPServer(("127.0.0.1", PORT), RequestHandler, app)
        server.daemon_threads = True
        try:
            server.serve_forever(poll_interval=0.1)
        finally:
            server.server_close()
            app.stop()
        sys.exit(0)
    sys.exit(main())
