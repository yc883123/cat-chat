# -*- coding: utf-8 -*-
"""B 步前置冒烟：**运行中并发改会话**时，本轮必须仍按"冻结那一刻的历史"走。

为什么需要它：`create_chat_run` 提交时定下 `input_message_id`（本轮用户消息 id），运行期
`_frozen_history_for_run` 就以它为**游标**取"到它为止的消息前缀"当作冻结历史，`build_model_history`
与 `_turn_index_for_run` 都吃这份前缀。这条"冻结前缀"契约就是"不再把整段会话固化进快照"
（每轮省掉 2× 历史的写入）的承重结构——先把它钉住，才敢改掉那份快照副本。

做法：起**真实服务**（隔离根），模型后端是**可暂停的假后端**——它把收到的请求记录
下来并卡住不返回，于是运行线程停在"已冻结、等模型回"的窗口里；这时从活库并发写入，
再放行。判据：

1. 线上模型请求的历史与**冻结前缀**逐行一致（回放确实只吃提交那一刻的历史）；
2. 并发写入后，按 `input_message_id` 重新解析出的冻结前缀**逐字不变**；
3. 并发写入的内容没有出现在本轮的线上请求里；
4. `snapshot` 里没有 `conversation_messages`（新契约：不再固化整段会话），只留 `history_size`；
5. 运行照常收尾，且终态瘦身生效（库里的 done 事件去掉完整 message）。

用法：项目根\\.venv\\Scripts\\python.exe verify\\run_freeze_smoke.py
（`--serve` 是子进程入口，不用手工调用；README/维护说明不需要登记它）
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
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PORT = int(os.environ.get("NAIBA_SMOKE_PORT", "8821"))
MODEL_PORT = int(os.environ.get("NAIBA_SMOKE_MODEL_PORT", "8822"))
BASE = f"http://127.0.0.1:{PORT}"
ISOLATED_ROOT = Path(os.environ.get("NAIBA_SMOKE_ROOT", ROOT / "verify" / "run_freeze_tmp"))
DATA_DIR = ISOLATED_ROOT / "data"

PROVIDER_ID = "smoke-freeze-local"
PROVIDER_NAME = "冒烟本地模型（可暂停）"
BOUND_MODEL = "smoke-local-freeze"

CONVERSATION_TITLE = "运行中并发改会话冒烟"
HISTORY_Q1 = "冻结前的第一问"
HISTORY_A1 = "冻结前的第一答"
HISTORY_Q2 = "冻结前的第二问"
HISTORY_A2 = "冻结前的第二答"
SEND_MESSAGE = "这一轮会并发改会话"
MODEL_ANSWER = "模型回答（放行后返回）"
INJECTED_TEXT = "并发注入的内容（不得进入本轮历史）"

HOLD_TIMEOUT_SECONDS = 20.0
REQUEST_TIMEOUT_SECONDS = 90.0


# ---------------- 假模型后端：记录请求 + 可暂停 ----------------

class _HoldGate:
    """把假后端的响应卡住，直到主线程放行（带超时兜底，避免冒烟永久挂住）。"""

    def __init__(self) -> None:
        self._release = threading.Event()
        self._arrived = threading.Event()

    def arrive_and_wait(self) -> None:
        self._arrived.set()
        self._release.wait(HOLD_TIMEOUT_SECONDS)

    def release(self) -> None:
        self._release.set()

    def wait_arrival(self, timeout: float = HOLD_TIMEOUT_SECONDS) -> bool:
        return self._arrived.wait(timeout)


class _FakeModelState:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        self.gate = _HoldGate()
        self.hold_enabled = True

    def record(self, payload: dict) -> None:
        with self.lock:
            self.requests.append(payload)

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.requests)


STATE = _FakeModelState()


class _FakeModelHandler(BaseHTTPRequestHandler):
    """OpenAI 兼容的假后端：记录请求体，卡住一段时间再流式回一段固定回答。"""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except (json.JSONDecodeError, TypeError):
            payload = {"_unparsed": raw[:200].decode("utf-8", "replace")}
        # 只有**真正的对话补全**才记录并卡住。判据用"最后一条非 system 消息里带着本冒烟的
        # 提问文本"：本地后端的上下文探测同样会 POST /v1/chat/completions（形状完全一样），
        # 按 URL/形状判定会让探测请求先占掉 hold 闸门，于是冒烟在"run 还没建、用户消息还没
        # 落库"的时刻就以为进了冻结窗口（实测踩过：所有断言整体错位一节）。
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        signal = json.dumps(messages, ensure_ascii=False)
        is_completion = (
            self.path.rstrip("/").endswith("/chat/completions")
            and SEND_MESSAGE in signal
        )
        if not is_completion:
            self._respond_probe()
            return
        STATE.record(payload)
        STATE.gate.arrive_and_wait()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def chunk(body: dict) -> None:
            self.wfile.write(f"data: {json.dumps(body, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            chunk({"id": "fake-freeze", "object": "chat.completion.chunk",
                   "choices": [{"index": 0, "delta": {"role": "assistant"}}]})
            chunk({"id": "fake-freeze", "object": "chat.completion.chunk",
                   "choices": [{"index": 0, "delta": {"content": MODEL_ANSWER}}]})
            chunk({"id": "fake-freeze", "object": "chat.completion.chunk",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._respond_probe()

    def _respond_probe(self) -> None:
        """本地后端探测（/props、/v1/models 等）：回一个足够小的窗口，别真去探上下文。"""
        body = json.dumps({"n_ctx": 8192, "data": [], "object": "list"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: D102 - 关掉访问日志噪音
        return


def start_fake_model() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", MODEL_PORT), _FakeModelHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                     daemon=True).start()
    return server


# ---------------- 隔离实例与播种 ----------------

def isolated_paths():
    from naiba.paths import PathContext

    paths = PathContext.local(ISOLATED_ROOT, ISOLATED_ROOT / "config.json")
    paths.public_dir = ROOT / "public"
    return paths


def seed() -> dict[str, str]:
    from naiba.app import NaibaChatApp
    from naiba.config import ConfigStore
    from naiba.storage.store import ChatStorage

    paths = isolated_paths()
    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({
        "host": "127.0.0.1", "port": PORT, "access_token": "",
        # data_dir 必须是**绝对路径**：留相对值的话，子进程会按自己的 CWD 解析，
        # 于是"app 写一份、断言读另一份"，现象是运行明明成功但库里查不到任何行。
        "data_dir": str(DATA_DIR), "workspace_dir": str(ISOLATED_ROOT / "workspace"),
        "skills_dirs": [str(ROOT / "skills")], "mcp_servers": [],
    })
    config.save()
    storage = ChatStorage(DATA_DIR / "chat.db")
    app = SimpleNamespace(config=config, storage=storage)
    NaibaChatApp.api_upsert_model_profile(app, {
        "id": PROVIDER_ID, "kind": "local", "name": PROVIDER_NAME,
        "base_url": f"http://127.0.0.1:{MODEL_PORT}/v1", "api_key": "",
        "request_format": "llama_cpp", "model": BOUND_MODEL,
        "context_window": 8192,
    })
    conversation = storage.create_conversation(
        CONVERSATION_TITLE,
        model_key=f"local:{PROVIDER_ID}",
        model_name=BOUND_MODEL,
        permission_mode="full",
        workspace_dir=str(ISOLATED_ROOT / "workspace"),
    )
    storage.add_message(str(conversation["id"]), "user", HISTORY_Q1)
    storage.add_message(str(conversation["id"]), "assistant", HISTORY_A1)
    storage.add_message(str(conversation["id"]), "user", HISTORY_Q2)
    storage.add_message(str(conversation["id"]), "assistant", HISTORY_A2)
    return {
        "NAIBA_SMOKE_CONVERSATION_ID": str(conversation["id"]),
        "NAIBA_SMOKE_CONVERSATION_TITLE": CONVERSATION_TITLE,
    }


# ---------------- HTTP 工具 ----------------

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


def post_json(path: str, body: dict, timeout: float = REQUEST_TIMEOUT_SECONDS) -> tuple[int, dict]:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def get_json(path: str, timeout: float = 30.0) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def history_lines(payload: dict) -> list[str]:
    """把一次模型请求里的消息压成可比对的文本行（忽略 system，只比历史内容）。"""
    lines: list[str] = []
    for item in payload.get("messages") or []:
        if not isinstance(item, dict) or item.get("role") == "system":
            continue
        content = item.get("content")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or part.get("content") or "")
                for part in content
                if isinstance(part, dict)
            )
        lines.append(f"{item.get('role')}:{content}")
    return lines


# ---------------- 用例主体 ----------------

class _Check:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def ok(self, condition: bool, message: str) -> bool:
        print(("  ✅ " if condition else "  ❌ ") + message)
        if not condition:
            self.failures.append(message)
        return bool(condition)

    def dump(self) -> int:
        if self.failures:
            print(f"\n失败 {len(self.failures)} 项：")
            for item in self.failures:
                print(f"  - {item}")
            return 1
        print("\n全部通过")
        return 0


class _StreamThread(threading.Thread):
    """后台消费 /api/chat 的 NDJSON 流（run 未结束时该请求会一直挂着）。

    **必须逐行增量读**：`urlopen(...)` 整体读会在流结束前一直缓冲，拿不到"运行中"就已
    出现的事件——而本冒烟恰恰要在运行中途读状态。
    """

    def __init__(self, body: dict) -> None:
        super().__init__(daemon=True)
        self.body = body
        self.events: list[dict] = []
        self.error: str = ""
        self.status = 0
        self._lock = threading.Lock()

    def run(self) -> None:
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=REQUEST_TIMEOUT_SECONDS)
        try:
            payload = json.dumps(self.body, ensure_ascii=False).encode("utf-8")
            connection.request("POST", "/api/chat", body=payload,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.status = response.status
            while True:
                line = response.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    continue
                with self._lock:
                    self.events.append(event)
        except Exception as exc:  # noqa: BLE001 - 流断开是预期分支（超时/收尾）
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def wait_for_run_id(self, timeout: float = 10.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            run_id = self.run_id
            if run_id:
                return run_id
            time.sleep(0.1)
        return ""

    @property
    def run_id(self) -> str:
        with self._lock:
            events = list(self.events)
        for event in events:
            if event.get("run_id"):
                return str(event["run_id"])
        return ""

    def terminal_event(self) -> dict | None:
        with self._lock:
            events = list(self.events)
        for event in reversed(events):
            if event.get("type") in {"done", "cancelled", "error"}:
                return event
        return None


def main() -> int:
    global ISOLATED_ROOT, DATA_DIR
    if "--serve" in sys.argv:
        # 子进程入口：这里必须最先分流，绝不能落到 main()（否则会再跑一遍用例）。
        return serve()
    isolated = tempfile.TemporaryDirectory(
        prefix="run_freeze_", dir=ROOT / "verify", ignore_cleanup_errors=True
    )
    ISOLATED_ROOT = Path(isolated.name)
    DATA_DIR = ISOLATED_ROOT / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    check = _Check()
    code = 1
    server: subprocess.Popen | None = None
    fake = start_fake_model()
    log = (ROOT / "verify" / "run_freeze_smoke_server.log").open("w", encoding="utf-8")
    try:
        seed()
        env = dict(os.environ)
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                    "PYTHONUNBUFFERED": "1",  # 子进程 stdout 是文件：不设会整块缓冲、日志空着
                    "NAIBA_SMOKE_ROOT": str(ISOLATED_ROOT)})
        server = subprocess.Popen(  # noqa: S603 - 固定 argv
            [sys.executable, str(Path(__file__).resolve()), "--serve"],
            cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if not wait_health():
            print(f"server 未就绪（日志见 {log.name}）")
            return 1
        print(f"[preflight] 隔离 config 已就绪：{ISOLATED_ROOT / 'config.json'}")
        code = run_checks(check)
    finally:
        STATE.gate.release()
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
        print("已清理独立测试目录（开发 config.json 未修改）")
    return code


def run_checks(check: _Check) -> int:
    from naiba.storage.store import ChatStorage

    storage = ChatStorage(DATA_DIR / "chat.db")
    # 播种与断言都在同一个进程、同一个 data_dir 上：直接读库取会话 id（不绕 HTTP，
    # 免得被侧栏的"归档/模式过滤"这类展示层规则影响）。
    seeded = [
        item for item in storage.list_conversations()
        if str(item.get("model_key") or "") == f"local:{PROVIDER_ID}"
    ]
    conversation_id = str(seeded[0]["id"]) if seeded else ""
    print(f"\n[0] 库内会话 {len(storage.list_conversations())} 个")
    if not check.ok(bool(conversation_id), f"找到播种会话（model_key=local:{PROVIDER_ID}）"):
        return 1

    live_before = storage.get_conversation(conversation_id)["messages"]
    print(f"\n[1] 播种会话：{len(live_before)} 条历史消息")

    print("\n[2] 发起一轮对话（假后端会卡住不返回）")
    stream = _StreamThread({"conversation_id": conversation_id, "message": SEND_MESSAGE,
                            "attachments": []})
    stream.start()
    if not check.ok(STATE.gate.wait_arrival(HOLD_TIMEOUT_SECONDS),
                    "假后端已收到模型请求并卡住（运行线程进入冻结窗口）"):
        return 1
    # NDJSON 是逐行到的：模型请求已到达 ≠ 客户端已解析出首行事件。
    run_id = stream.wait_for_run_id(10.0)
    if not check.ok(bool(run_id), f"拿到 run_id：{run_id}"):
        print(f"     stream.status={stream.status} error={stream.error!r}")
        return 1

    frozen = (storage.get_run_snapshot(run_id) or {}).get("conversation_messages") or []
    if frozen:
        # 旧数据形态（本次改动之前建的 run）：冻结副本还在快照里。
        print("     注：该 run 的快照仍带 conversation_messages 副本（旧数据形态）")
        frozen_lines = [
            f"{m.get('role')}:{m.get('content')}" for m in frozen
            if m.get("role") in {"user", "assistant"}
        ]
    else:
        # 新形态：快照不再固化整段会话，冻结语义由 input_message_id 这个**游标**表达。
        boundary = str((storage.get_background_task(run_id) or {}).get("input_message_id") or "")
        live_now = storage.get_conversation(conversation_id)["messages"]
        frozen = []
        for message in live_now:
            frozen.append(message)
            if str(message.get("id") or "") == boundary:
                break
        frozen_lines = [
            f"{m.get('role')}:{m.get('content')}" for m in frozen
            if m.get("role") in {"user", "assistant"}
        ]
    check.ok(len(frozen) == len(live_before) + 1,
             f"冻结历史恰为「历史 {len(live_before)} + 本轮用户消息 1」= {len(frozen)} 条")
    online_payload = STATE.snapshot()[0]
    online_lines = history_lines(online_payload)
    check.ok(online_lines == frozen_lines,
             "判据①：线上模型请求的历史与冻结历史逐行一致（回放确实走冻结前缀）")
    freeze_fingerprint = json.dumps(frozen, ensure_ascii=False, sort_keys=True)
    online_count_before = len(online_payload.get("messages") or [])

    print("\n[3] 在冻结窗口内并发改会话（直连存储层模拟另一路写入）")
    storage.add_message(conversation_id, "user", INJECTED_TEXT)
    live_after = storage.get_conversation(conversation_id)["messages"]
    check.ok(len(live_after) == len(live_before) + 2,
             f"活库确实被改了：{len(live_before)} → {len(live_after)} 条"
             "（+1 本轮用户消息、+1 并发写入）")

    # 判据②：并发写入之后，重新按游标解析出来的冻结前缀必须与并发前逐字一致。
    # 这是"不再整段固化进快照"之后唯一承重的保证——前缀稳定性靠 (created_at, rowid) 顺序契约。
    rerun_boundary = str((storage.get_background_task(run_id) or {}).get("input_message_id") or "")
    refrozen: list[dict] = []
    for message in storage.get_conversation(conversation_id)["messages"]:
        refrozen.append(message)
        if str(message.get("id") or "") == rerun_boundary:
            break
    check.ok(
        json.dumps(refrozen, ensure_ascii=False, sort_keys=True) == freeze_fingerprint,
        "判据②：并发写入后按 input_message_id 重新解析的冻结前缀逐字不变",
    )
    check.ok(
        INJECTED_TEXT not in json.dumps(online_payload, ensure_ascii=False),
        "判据③：并发写入的内容没有出现在本轮的线上请求里",
    )
    snapshot_now = storage.get_run_snapshot(run_id) or {}
    check.ok(
        "conversation_messages" not in snapshot_now,
        "新契约：快照里不再固化整段会话（每轮省掉 2× 历史的写入）",
    )
    check.ok(
        int(snapshot_now.get("history_size") or -1) == len(frozen),
        f"快照只留体量标记：history_size={snapshot_now.get('history_size')}（期望 {len(frozen)}）",
    )

    print("\n[4] 放行模型，观察收尾")
    STATE.gate.release()
    stream.join(timeout=REQUEST_TIMEOUT_SECONDS)
    check.ok(not stream.is_alive(), "运行请求已在超时内结束")
    check.ok(not stream.error, f"流没有报错（error={stream.error!r}）")
    terminal = stream.terminal_event() or {}
    check.ok(terminal.get("type") == "done", f"终态事件为 done（实得 {terminal.get('type')!r}）")
    # done 事件在**流里**必须带 message（前端靠它即时渲染）；库里那份由收尾瘦身去掉。
    check.ok(terminal.get("message") is not None,
             "流里的终态事件带 message（前端即时渲染依赖它，瘦身只动库里那份）")

    # 收尾链路（compress + slim）都在 finally 里，done 事件落库可能稍晚于 done 流结束。
    done_payloads: list[dict] = []
    deadline = time.time() + 20
    while time.time() < deadline:
        with storage._connect() as db:  # noqa: SLF001 - 冒烟直接读事件表核对瘦身
            rows = db.execute(
                "SELECT payload FROM run_events WHERE run_id = ? AND event_type = 'done'",
                (run_id,),
            ).fetchall()
        done_payloads = [json.loads(row[0]) for row in rows]
        if done_payloads and all("message" not in item for item in done_payloads):
            break
        time.sleep(0.3)
    check.ok(bool(done_payloads), "库里确实落了一条 done 事件")
    check.ok(
        all("message" not in item for item in done_payloads),
        "A 步瘦身生效：库里的 done 事件已去掉完整 message 对象",
    )
    stored = storage.get_run_snapshot(run_id) or {}
    check.ok("conversation_messages" not in stored,
             "A 步瘦身生效：收尾后快照不再固化整段会话")

    print("\n[5] 收尾核对")
    final_messages = storage.get_conversation(conversation_id)["messages"]
    check.ok(
        any(str(m.get("content") or "") == MODEL_ANSWER for m in final_messages),
        "模型回答已入库",
    )
    check.ok(
        any(str(m.get("content") or "") == INJECTED_TEXT for m in final_messages),
        "并发写入的内容仍在会话里（只影响之后的轮次，不回溯本轮）",
    )
    # 只发了 1 次模型请求（没有工具轮）：线上请求条数应等于冻结快照的消息数。
    requests = STATE.snapshot()
    check.ok(
        all(len(item.get("messages") or []) == online_count_before for item in requests),
        f"全程 {len(requests)} 次模型请求的历史长度都等于冻结那一刻（{online_count_before} 条）",
    )
    return check.dump()


def serve() -> int:
    """子进程入口：起**真实服务**（隔离根 + 开发用 public/）。"""
    from naiba.app import NaibaChatApp
    from naiba.http import AppHTTPServer, RequestHandler

    print(f"[serve] ISOLATED_ROOT={ISOLATED_ROOT}", flush=True)
    app = NaibaChatApp(paths=isolated_paths())
    print(f"[serve] resolve_data_dir={app.config.resolve_data_dir()}", flush=True)
    print(f"[serve] storage.db={app.storage.db_path}", flush=True)
    server = AppHTTPServer(("127.0.0.1", PORT), RequestHandler, app)
    server.daemon_threads = True
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        app.stop()
    return 0


if __name__ == "__main__":
    if "--serve" in sys.argv:
        sys.exit(serve())
    sys.exit(main())
