# -*- coding: utf-8 -*-
"""冒烟：**工具调用前的「进度播报」不得累积进助手消息**（2026-09-19 用户报障回归）。

现象（用户实测）：一轮 Agent 跑下来，助手回复里堆了十来句近义进度——
"我已核对…""根因已经锁定…""现在补协议…"。数据库视角它们各自只是同一条助手消息，
所以不是重复消息，而是**每一轮工具调用前的自然语言前缀都被保留并展示**了：

1. 模型每调用一次工具，先在正文里说一句进度，再吐 K3 的 ``<|open|>tools…`` 协议；
2. ``naiba/llm/stream.py`` 屏蔽协议、**故意保留协议前面的正文**（那是"中途正文"的设计）；
3. ``naiba/run/stream.py::_build_activity_timeline`` 把每段都记成 ``prose`` 条目；
4. 前端按时间线逐段展示 ⇒ 一条回复里摞成满屏废话。

修复（本次）：只保留最后一段正文（``_build_activity_timeline`` 丢弃后面还有工具调用的
prose 段；``chat._rebuild_partial_run`` 同理只取最后一次工具调用之后的 delta），并在
Agent 系统提示里禁止过程预告。

这条冒烟走**全真链路**：真 `POST /api/chat`、真 run、真 Agent 循环、真工具执行、真流解析；
只有模型响应来自本脚本起的本地假 SSE 服务——它**每轮都先吐一句进度再吐 Harmony 工具协议**，
正是缺陷的触发形态。

**双向断言**（缺一不可，否则是空断言）：
- 正向前提：run 轨迹里确实出现过那几句播报（证明模型真的播报了，不是假模型没吐）；
- 负向结论：助手消息的 activity / content 里**一处都没有**，且 activity 只剩一段 prose
  （= 最终答复），工具条目一个不少。

用法：.venv\\Scripts\\python.exe verify\\tool_prefix_prose_smoke.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PORT = 8806                       # 源码 server
MODEL_PORT = 8807                 # 假模型（SSE）
BASE = f"http://127.0.0.1:{PORT}"
ISOLATED_ROOT = Path(os.environ.get("NAIBA_SMOKE_ROOT", ROOT / "verify" / "tool_prefix_prose_tmp"))
DATA_DIR = ISOLATED_ROOT / "data"
PROVIDER_ID = "smoke-k3"
BOUND_MODEL = "smoke-k3-model"
TITLE = "过程播报冒烟"
USER_MESSAGE = "看一下这个项目然后修掉那个问题。"
SAMPLE_FILE = "sample.txt"

# 每一轮工具调用：先一句"进度播报"（用户报障里的原话形态），再跟 K3 的 Harmony 协议。
TOOL_TURNS: list[tuple[str, str, dict[str, str]]] = [
    ("我已核对接口和调用链。", "list_directory", {"path": "."}),
    ("根因已经锁定：流解析层把协议前缀的正文留下了。", "list_directory", {"path": "."}),
    ("现在补协议识别。", "read_file", {"path": SAMPLE_FILE}),
    ("我再确认一遍没有回归。", "read_file", {"path": SAMPLE_FILE}),
]
FINAL_ANSWER = "修好了：工具协议识别与时过程播报丢弃都已落地，测试全绿。"
TIMEOUT = 180.0


def harmony_call(tool: str, arguments: dict[str, str]) -> str:
    """K3 兼容中继在 output_text 里回的保留记号形态（与 agent.py 解析器同构）。"""
    body = "".join(
        f'<|open|>argument key="{name}" type="string"<|sep|>{value}<|close|>argument<|sep|>'
        for name, value in arguments.items()
    )
    return f'<|open|>tools<|sep|><|open|>call tool="{tool}"<|sep|>{body}<|close|>call<|sep|>'


def wait_health(deadline: float = 90.0) -> bool:
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
    from types import SimpleNamespace

    from naiba.app import NaibaChatApp
    from naiba.config import ConfigStore
    from naiba.storage.store import ChatStorage

    paths = isolated_paths()
    workspace = ISOLATED_ROOT / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / SAMPLE_FILE).write_text("smoke fixture\n", encoding="utf-8")

    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({
        "host": "127.0.0.1",
        "port": PORT,
        "access_token": "",
        "data_dir": str(DATA_DIR),
        "workspace_dir": str(workspace),
        "skills_dirs": [str(ROOT / "skills")],
        "mcp_servers": [],
        # 只读工具本就不在确认清单里，显式 auto 免掉工作区越界的确认分支。
        "permission_mode": "auto",
    })
    config.save()
    storage = ChatStorage(DATA_DIR / "chat.db")
    app = SimpleNamespace(config=config, storage=storage)
    NaibaChatApp.api_upsert_model_profile(app, {
        "id": PROVIDER_ID,
        "kind": "online",
        "name": "冒烟 K3 中继",
        "base_url": f"http://127.0.0.1:{MODEL_PORT}/v1",
        "api_key": "sk-smoke",
        "request_format": "openai_chat",
        "model": BOUND_MODEL,
    })
    storage.create_conversation(
        TITLE,
        model_key=f"online:{PROVIDER_ID}",
        model_name=BOUND_MODEL,
        workspace_dir=str(workspace),
    )


class _FakeModelHandler(BaseHTTPRequestHandler):
    """按「已回传的工具结果条数」决定轮次：先吐进度话术，再吐 Harmony 工具协议。"""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            payload = {}
        messages = payload.get("messages") or []
        completed = sum(
            1 for item in messages
            if isinstance(item, dict) and str(item.get("role") or "") == "tool"
        )

        if completed < len(TOOL_TURNS):
            prose, tool, arguments = TOOL_TURNS[completed]
            text = f"{prose}\n{harmony_call(tool, arguments)}"
        else:
            text = FINAL_ANSWER

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def chunk(piece: str) -> None:
            self.wfile.write(
                f"data: {json.dumps(piece, ensure_ascii=False)}\n\n".encode("utf-8")
            )
            self.wfile.flush()

        try:
            chunk('{"id":"fake-1","object":"chat.completion.chunk",'
                  '"choices":[{"index":0,"delta":{"role":"assistant"}}]}')
            # 切成小片逐段发：正文与协议都会经过 stream 层的"是否工具协议"判别。
            for index in range(0, len(text), 8):
                self.wfile.write(
                    ("data: " + json.dumps({
                        "id": "fake-1", "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": {"content": text[index:index + 8]}}],
                    }, ensure_ascii=False) + "\n\n").encode("utf-8")
                )
                self.wfile.flush()
                time.sleep(0.02)
            chunk('{"id":"fake-1","object":"chat.completion.chunk",'
                  '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}')
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


def send_chat(conversation_id: str) -> list[dict]:
    """真 POST /api/chat 并读完整条 NDJSON（连接关闭即 run 收尾）。"""
    body = json.dumps({"conversation_id": conversation_id, "message": USER_MESSAGE}).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}/api/chat", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    events: list[dict] = []
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        for line in response:
            line = line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def check(conversation_id: str, events: list[dict]) -> list[str]:
    """返回失败原因列表（空 = 全绿）。"""
    from naiba.storage.store import ChatStorage

    failures: list[str] = []
    conversation = ChatStorage(DATA_DIR / "chat.db").get_conversation(conversation_id)
    messages = (conversation or {}).get("messages") or []
    assistant = [m for m in messages if m.get("role") == "assistant"]
    if not assistant:
        return ["会话里没有助手消息（run 没跑完？）"]
    message = assistant[-1]
    metadata = message.get("metadata") or {}
    activity = metadata.get("activity") or []
    prose = [item for item in activity if item.get("type") == "prose"]
    tools = [item for item in activity if item.get("type") == "tool"]

    # 正向前提：模型确实每轮都播报了（run 事件流里有那些话），否则下面的负向断言是空断言。
    stream_text = "".join(
        str(event.get("content") or "")
        for event in events if str(event.get("type") or "") == "delta"
    )
    for prose_text, _tool, _args in TOOL_TURNS:
        if prose_text not in stream_text:
            failures.append(f"前提不成立：流事件里没找到播报「{prose_text}」")

    # 工具真的执行了（Harmony 协议被识别 + 执行成功）——共 TOOL_TURNS 个。
    if len(tools) != len(TOOL_TURNS):
        failures.append(f"工具条目 {len(tools)} 个，期望 {len(TOOL_TURNS)} 个")
    failed_runs = [
        str(item.get("run", {}).get("tool") or "")
        for item in tools if not item.get("run", {}).get("success")
    ]
    if failed_runs:
        failures.append(f"工具执行失败：{failed_runs}")

    # 负向结论：过程播报一处都不许留在展示面上。
    if len(prose) != 1:
        failures.append(f"prose 条目 {len(prose)} 段，期望 1 段（只有最终答复）")
    else:
        if prose[0].get("text") != FINAL_ANSWER:
            failures.append(f"留下的不是最终答复：{prose[0].get('text')!r}")
    for item in activity:
        text = str(item.get("text") or "")
        for prose_text, _tool, _args in TOOL_TURNS:
            if prose_text in text:
                failures.append(f"activity 里仍残留过程播报：{text!r}")
    content = str(message.get("content") or "")
    if content != FINAL_ANSWER:
        failures.append(f"消息正文不是最终答复：{content!r}")
    for prose_text, _tool, _args in TOOL_TURNS:
        if prose_text in content:
            failures.append(f"消息正文里仍残留过程播报：{prose_text}")
    return failures


def main() -> int:
    global ISOLATED_ROOT, DATA_DIR
    isolated = tempfile.TemporaryDirectory(
        prefix="tool_prefix_prose_", dir=ROOT / "verify", ignore_cleanup_errors=True
    )
    ISOLATED_ROOT = Path(isolated.name)
    DATA_DIR = ISOLATED_ROOT / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    code = 1
    server: subprocess.Popen | None = None
    fake = start_fake_model()
    log = (ROOT / "verify" / "tool_prefix_prose_smoke_server.log").open("w", encoding="utf-8")
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

        from naiba.storage.store import ChatStorage

        conversation_id = ChatStorage(DATA_DIR / "chat.db").list_conversations()[0]["id"]
        events = send_chat(conversation_id)
        failures = check(conversation_id, events)
        if failures:
            print("冒烟失败：")
            for item in failures:
                print(f"  - {item}")
            code = 1
        else:
            print(
                f"冒烟通过：{len(TOOL_TURNS)} 轮工具调用（每轮都播报过进度）→ "
                "activity 只剩 1 段最终答复，工具条目与正文均无残留。"
            )
            code = 0
    except Exception as exc:  # noqa: BLE001 - 冒烟要把异常变成可读结论
        print(f"冒烟异常：{type(exc).__name__}: {exc}")
        code = 1
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
