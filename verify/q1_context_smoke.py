# -*- coding: utf-8 -*-
"""冒烟：Q1「本地模型上下文溢出的用户可见报错」的自编排入口。

用**独立数据目录**（tempfile 新建）+ 独立端口起源源 server，不碰开发 config.json 与开发库
（沿用 `batch1_smoke.py` 的隔离模板：`PathContext.local` + 独立端口 + `--serve` 子进程 +
Playwright/Edge）。

两段验证，一段静态、一段**真链路**：

① **静态形态**（播种三条带 `metadata.truncated` 的助手回复）：`cause=context` 的提示行说
   「上下文窗口已耗尽 + 新会话」，`cause=output` 说「已达模型输出上限」，**没有 cause 的
   老数据**仍按输出上限显示（向后兼容）。这三条的判据是「用户看到什么」，不是「源码里有
   没有这个分支」。
② **真链路**：起一个假本地后端（`POST /v1/chat/completions` 直接回 400 + llama.cpp 的
   `exceed_context_size_error` 响应体，含 `n_ctx`），在浏览器里对一个绑定该本地模型的会话
   真发一条消息 → 真 run → 断言消息区里出现**可读、可行动**的失败文案（含后端名、后端自报的
   真实窗口 4096 tokens、「新会话」指引），而不是原始 JSON。

用法：.venv\\Scripts\\python.exe verify\\q1_context_smoke.py
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
sys.path.insert(0, str(ROOT))  # 直接跑本脚本时也能 import naiba
PORT = int(os.environ.get("NAIBA_SMOKE_PORT", "8803"))
MODEL_PORT = int(os.environ.get("NAIBA_SMOKE_MODEL_PORT", "8804"))
BASE = f"http://127.0.0.1:{PORT}"
ISOLATED_ROOT = Path(os.environ.get("NAIBA_SMOKE_ROOT", ROOT / "verify" / "q1_context_tmp"))
DATA_DIR = ISOLATED_ROOT / "data"

PROVIDER_ID = "smoke-q1-local"
PROVIDER_NAME = "冒烟本地模型 Q1"
BOUND_MODEL = "smoke-local-q1"
# 会话里**显式配置**的窗口故意写成 32768（与后端自报的 4096 不同）：用来证明文案优先采用
# 后端自报的真值，而不是配置里那个偏大的数字。
CONFIGURED_WINDOW = 32768
BACKEND_WINDOW = 4096

RENDER_TITLE = "溢出提示行冒烟"
FAIL_TITLE = "溢出真链路冒烟"
FAIL_Q = "这一轮会撞本地窗口"

# llama.cpp 的真实错误体（照抄实测响应；n_ctx 是真值，配置里写的不是）。
LLAMA_CPP_400 = {
    "error": {
        "code": 400,
        "message": (
            "the request exceeds the available context size. "
            "try increasing the n_ctx option or use a larger context size model"
        ),
        "type": "exceed_context_size_error",
        "n_prompt_tokens": 5120,
        "n_ctx": BACKEND_WINDOW,
    },
}

CONTEXT_A = "撞窗口的那一轮回答。"
OUTPUT_A = "撞输出上限的那一轮回答。"
LEGACY_A = "老数据：只有 truncated 没有 cause 的那一轮回答。"


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
    paths.public_dir = ROOT / "public"  # 关键：读仓库 public/，而不是打包版内置资源
    return paths


def seed() -> dict[str, str]:
    """播种配置 + 两个会话，返回给浏览器脚本用的环境变量表。"""
    from types import SimpleNamespace

    from naiba.app import NaibaChatApp
    from naiba.config import ConfigStore
    from naiba.storage.store import ChatStorage

    paths = isolated_paths()
    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({
        "host": "127.0.0.1", "port": PORT, "access_token": "",
        "data_dir": str(DATA_DIR), "workspace_dir": str(ISOLATED_ROOT / "workspace"),
        "skills_dirs": [str(ROOT / "skills")], "mcp_servers": [],
    })
    config.save()
    storage = ChatStorage(DATA_DIR / "chat.db")
    app = SimpleNamespace(config=config, storage=storage)
    # 本地供应商指向假后端。context_window 显式给值 → 来源是 local_config，
    # 运行期不会再去探测（否则会往假后端打一次 /props）。
    NaibaChatApp.api_upsert_model_profile(app, {
        "id": PROVIDER_ID, "kind": "local", "name": PROVIDER_NAME,
        "base_url": f"http://127.0.0.1:{MODEL_PORT}/v1", "api_key": "",
        "request_format": "llama_cpp", "model": BOUND_MODEL,
        "context_window": CONFIGURED_WINDOW,
    })

    # ① 静态形态：三条不同成因/年代的截断自述。
    render = storage.create_conversation(
        RENDER_TITLE, model_key=f"local:{PROVIDER_ID}", model_name=BOUND_MODEL,
        workspace_dir=str(ISOLATED_ROOT / "workspace"),
    )
    storage.add_message(render["id"], "user", "上下文溢出的提示行长什么样")
    storage.add_message(render["id"], "assistant", CONTEXT_A, {
        "truncated": {
            "finish_reason": "length", "truncated": True, "continued": True,
            "unfinished_tail": True, "cause": "context",
        },
    })
    storage.add_message(render["id"], "assistant", OUTPUT_A, {
        "truncated": {"finish_reason": "length", "truncated": True, "cause": "output"},
    })
    storage.add_message(render["id"], "assistant", LEGACY_A, {
        "truncated": {"finish_reason": "length", "truncated": True},
    })

    # ② 真链路：空会话，由浏览器真发一条消息，撞假后端的 400。
    failing = storage.create_conversation(
        FAIL_TITLE, model_key=f"local:{PROVIDER_ID}", model_name=BOUND_MODEL,
        workspace_dir=str(ISOLATED_ROOT / "workspace"),
    )

    return {
        "NAIBA_SMOKE_RENDER_ID": render["id"],
        "NAIBA_SMOKE_FAIL_ID": failing["id"],
        "NAIBA_SMOKE_RENDER_TITLE": storage.get_conversation(render["id"], include_messages=False)["title"],
        "NAIBA_SMOKE_FAIL_TITLE": storage.get_conversation(failing["id"], include_messages=False)["title"],
        "NAIBA_SMOKE_FAIL_Q": FAIL_Q,
        "NAIBA_SMOKE_PROVIDER_NAME": PROVIDER_NAME,
        "NAIBA_SMOKE_BACKEND_WINDOW": str(BACKEND_WINDOW),
    }


class _FakeLocalHandler(BaseHTTPRequestHandler):
    """最小「本地后端」：任何请求都回 llama.cpp 的上下文溢出 400。"""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = json.dumps(LLAMA_CPP_400, ensure_ascii=False).encode("utf-8")
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self.send_error(404)

    def log_message(self, *args) -> None:  # noqa: D102 - 关掉访问日志噪音
        return


def start_fake_model() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", MODEL_PORT), _FakeLocalHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                     daemon=True).start()
    return server


def main() -> int:
    global ISOLATED_ROOT, DATA_DIR
    # 同 stream_rerender_smoke：Windows 上刚被 terminate 的服务进程可能还占着 db 文件，
    # 清理失败不该让整轮冒烟以异常收场。
    isolated = tempfile.TemporaryDirectory(
        prefix="q1_context_", dir=ROOT / "verify", ignore_cleanup_errors=True
    )
    ISOLATED_ROOT = Path(isolated.name)
    DATA_DIR = ISOLATED_ROOT / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    code = 1
    server: subprocess.Popen | None = None
    fake: ThreadingHTTPServer | None = None
    log = (ROOT / "verify" / "q1_context_smoke_server.log").open("w", encoding="utf-8")
    try:
        env_snapshot = seed()
        fake = start_fake_model()
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
        node_env.update(env_snapshot)
        node = subprocess.run(  # noqa: S603 - 固定 argv
            ["node", str(ROOT / "verify" / "q1_context_smoke.cjs")],
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
        if fake is not None:
            fake.shutdown()
            fake.server_close()
        log.close()
        isolated.cleanup()
        print("已清理独立测试目录（开发 config.json 未修改）"
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
