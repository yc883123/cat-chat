# -*- coding: utf-8 -*-
"""围栏（``` / ~~~）来源动作的来源标记与确认流程 —— 真实链路验收（2026-10-01）。

对照计划 `docs/plans/2026-10-01-代码协议误判与围栏动作确认修复计划.md` §六「验收」：
**全真链路**——真 `POST /api/chat`、真 run、真 Agent 循环、真工具执行、真流解析、真确认端点；
只有模型响应来自本脚本起的本地假 SSE 服务（它按剧本吐「整条围栏动作 / 正文夹围栏示例 /
围栏示例 + 尾部裸协议 / 围栏包住的 final」四种形态，正是缺陷的触发形态）。

两条链：
- A 段（无浏览器，本文件直接断言）：S1 正文夹围栏示例不执行、S3 围栏动作被拒不重发、
  S4 围栏包 final 直接放行、S5 围栏示例 + 尾部裸协议只执行裸协议。
- B 段（Playwright，`fence_action_smoke.cjs`）：S2 整条围栏动作弹确认卡 →
  卡上文案/`data-allow-run` 是 fenced 专属形态 → 点「允许本轮继续执行后续操作」→
  同一 Run 的**第二个**围栏动作不再弹卡 → 全程只有 1 张卡、两个工具都执行成功；
  手机宽度（375px）另跑一遍，断卡不溢出且不横向滚动。

**停止条件**（计划原文：出现任一即不许发布）：①「示例仍能直接进入执行器」
②「拒绝后仍发起新模型调用」③「正文 delta 在围栏后消失」。
本脚本对这三条各有明确断言（S1/S5 对应 ①、S3 对应 ②、S1/S5 的正文完整性对应 ③）。

用法：.venv\\Scripts\\python.exe verify\\fence_action_smoke.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PORT = 8822
MODEL_PORT = 8823
BASE = f"http://127.0.0.1:{PORT}"

ISOLATED_ROOT = ROOT / "verify" / "_tmp_fence_action"
DATA_DIR = ISOLATED_ROOT / "data"
WORKSPACE = ISOLATED_ROOT / "workspace"
PROVIDER_ID = "smoke-fence"
BOUND_MODEL = "smoke-fence-model"
SAMPLE_FILE = "sample.txt"
TIMEOUT = 180.0

# 会话标题 = 剧本键；假模型按**首条 user 消息**选剧本。
S1 = "S1 正文夹围栏示例"
S2 = "S2 整条围栏授权"
S2M = "S2M 手机围栏授权"
S3 = "S3 围栏动作被拒"
S4 = "S4 围栏包 final"
S5 = "S5 围栏示例加尾部裸协议"

SAMPLE_TEXT = "fence smoke fixture\n"


def fence(payload: str) -> str:
    return f"```json\n{payload}\n```"


def tool_json(tool: str, **arguments: str) -> str:
    return json.dumps({"type": "tool", "tool": tool, "arguments": arguments}, ensure_ascii=False)


# ---- 剧本 ----
FENCE_LIST = fence(tool_json("list_directory", path="."))
FENCE_READ = fence(tool_json("read_file", path=SAMPLE_FILE))
# S1：正文里夹一段围栏示例（正是用户报障的形态）——整条既不是"整文围栏"也没有尾部裸协议。
S1_TEXT = (
    "如果你想让我列目录，可以把工具协议写成这样：\n\n"
    f"{fence(tool_json('list_directory', path='.'))}\n\n"
    "上面只是格式说明，本机并没有真的执行。"
)
# S4：整条响应是一段围栏，里面是 final —— 直接当正文，不弹卡。
S4_TEXT = fence(json.dumps({"type": "final", "content": "这是最终答复。"}, ensure_ascii=False))
# S5：围栏示例（read_file，不该执行）+ 尾部裸协议（list_directory，该执行）。
S5_TEXT = (
    "先看个例子：\n\n"
    f"{fence(tool_json('read_file', path=SAMPLE_FILE))}\n\n"
    "现在真的列一下目录。\n"
    f"{tool_json('list_directory', path='.')}"
)
S5_FINAL = "目录列完了。"
S2_FINAL = "两步都做完了。"
S3_SHOULD_NOT_BE_CALLED = "（不应到达：拒绝后不得再发起模型调用）"

PROMPT_KEYS = (S1, S2, S2M, S3, S4, S5)
CALLS: dict[str, int] = {key: 0 for key in PROMPT_KEYS}
CALLS_LOCK = threading.Lock()


def script_for(prompt: str, completed: int) -> str:
    if prompt == S1:
        return S1_TEXT
    if prompt == S4:
        return S4_TEXT
    if prompt == S5:
        return S5_TEXT if completed == 0 else S5_FINAL
    if prompt in (S2, S2M):
        if completed == 0:
            return FENCE_LIST
        if completed == 1:
            return FENCE_READ
        return S2_FINAL
    if prompt == S3:
        return FENCE_LIST if completed == 0 else S3_SHOULD_NOT_BE_CALLED
    return "（未知剧本）"


class _FakeModelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        """模型目录：前端「输入区模型下拉」为空时发送会被当场挡下并 toast，
        所以浏览器段必须让 `/v1/models` 真的有内容。"""
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps(
                {"object": "list", "data": [{"id": BOUND_MODEL, "object": "model"}]},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            payload = {}
        messages = payload.get("messages") or []
        prompt = ""
        for item in messages:
            if isinstance(item, dict) and str(item.get("role") or "") == "user":
                prompt = str(item.get("content") or "")
                break
        completed = sum(
            1 for item in messages
            if isinstance(item, dict) and str(item.get("role") or "") == "tool"
        )
        with CALLS_LOCK:
            CALLS[prompt] = CALLS.get(prompt, 0) + 1
        text = script_for(prompt, completed)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def chunk(piece: str) -> None:
            self.wfile.write(f"data: {piece}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            chunk('{"id":"fake-1","object":"chat.completion.chunk",'
                  '"choices":[{"index":0,"delta":{"role":"assistant"}}]}')
            # 切成小片逐段发：正文与协议都要经过流层的「是不是工具协议」判别，
            # 围栏跨 chunk 的开合状态正是本次修复点。
            for index in range(0, len(text), 8):
                self.wfile.write(
                    ("data: " + json.dumps({
                        "id": "fake-1", "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": {"content": text[index:index + 8]}}],
                    }, ensure_ascii=False) + "\n\n").encode("utf-8")
                )
                self.wfile.flush()
                time.sleep(0.01)
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


# ---------------------------------------------------------------- 隔离实例装配
def isolated_paths():
    from naiba.paths import PathContext

    ISOLATED_ROOT.mkdir(parents=True, exist_ok=True)
    paths = PathContext.local(ISOLATED_ROOT, ISOLATED_ROOT / "config.json")
    paths.public_dir = ROOT / "public"
    return paths


def seed() -> dict[str, str]:
    """播种四个会话（S1/S3/S4/S5）与两个浏览器用会话（S2/S2M）。

    必须在服务就绪**之后**调用：启动清理会把存量 run 改成 interrupted。
    """
    from naiba.app import NaibaChatApp
    from naiba.config import ConfigStore
    from naiba.storage.store import ChatStorage

    paths = isolated_paths()
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    (WORKSPACE / SAMPLE_FILE).write_text(SAMPLE_TEXT, encoding="utf-8")

    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({
        "host": "127.0.0.1",
        "port": PORT,
        "access_token": "",
        "data_dir": str(DATA_DIR),
        "workspace_dir": str(WORKSPACE),
        "skills_dirs": [str(ROOT / "skills")],
        "mcp_servers": [],
        # auto 档：裸协议/native 照旧免确认 —— 用来证明"围栏来源要确认"是**额外**加的，
        # 不是把所有自动执行都关掉了（计划 §五 I/J 的回归点）。
        "permission_mode": "auto",
    })
    config.save()
    storage = ChatStorage(DATA_DIR / "chat.db")
    from types import SimpleNamespace

    app = SimpleNamespace(config=config, storage=storage)
    NaibaChatApp.api_upsert_model_profile(app, {
        "id": PROVIDER_ID,
        "kind": "online",
        "name": "冒烟围栏中继",
        "base_url": f"http://127.0.0.1:{MODEL_PORT}/v1",
        "api_key": "sk-smoke",
        "request_format": "openai_chat",
        "model": BOUND_MODEL,
    })
    ids: dict[str, str] = {}
    for title in (S1, S3, S4, S5, S2, S2M):
        conversation = storage.create_conversation(
            title,
            model_key=f"online:{PROVIDER_ID}",
            model_name=BOUND_MODEL,
            workspace_dir=str(WORKSPACE),
        ) or {}
        ids[title] = str(conversation.get("id") or "")
    return ids


# ---------------------------------------------------------------- HTTP 助手
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


def get_json(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=30) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def run_events(run_id: str) -> list[dict]:
    """经 HTTP 读该 Run 的完整事件流（NDJSON）。

    **刻意不使用 `ChatStorage` 直连库文件**：隔离实例自己持着同一个 SQLite（WAL + `-shm`），
    父进程再开一条连接去写（`_initialize` 的 PRAGMA/迁移）会撞上
    `attempt to write a readonly database`。读真值走后端接口既避开这件事，也更贴近用户路径。
    """
    events: list[dict] = []
    with urllib.request.urlopen(f"{BASE}/api/runs/{run_id}/events?after=0", timeout=30) as response:
        for line in response:
            line = line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def post(path: str, body: dict) -> dict:
    request = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def run_chat(conversation_id: str, message: str,
             collected: list[dict] | None = None) -> list[dict]:
    """真 POST /api/chat，读完整条 NDJSON（连接关闭即 run 收尾）。"""
    body = json.dumps({"conversation_id": conversation_id, "message": message}).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}/api/chat", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    events: list[dict] = collected if collected is not None else []
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


def wait_for_confirm(events: list[dict], deadline: float = 60.0) -> dict | None:
    end = time.time() + deadline
    while time.time() < end:
        for event in list(events):
            if str(event.get("type") or "") == "tool_confirm":
                return event
        time.sleep(0.2)
    return None


def wait_run_end(thread: threading.Thread, deadline: float = 90.0) -> bool:
    thread.join(timeout=deadline)
    return not thread.is_alive()


# ---------------------------------------------------------------- 断言助手
class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def eq(self, actual, expected, label: str) -> None:
        if actual != expected:
            self.failures.append(f"{label}：实际 {actual!r}，期望 {expected!r}")

    def ok(self, condition: bool, label: str) -> None:
        if not condition:
            self.failures.append(label)

    def contains(self, haystack: str, needle: str, label: str) -> None:
        if needle not in haystack:
            self.failures.append(f"{label}：{haystack[:200]!r} 里找不到 {needle!r}")


def last_assistant(conversation_id: str) -> dict:
    conversation = get_json(f"/api/conversations/{conversation_id}") or {}
    messages = conversation.get("messages") or []
    assistant = [item for item in messages if item.get("role") == "assistant"]
    return assistant[-1] if assistant else {}


def activity_of(message: dict) -> list[dict]:
    return list((message.get("metadata") or {}).get("activity") or [])


def tool_names(message: dict) -> list[str]:
    return [
        str((item.get("run") or {}).get("tool") or "")
        for item in activity_of(message) if item.get("type") == "tool"
    ]


# ---------------------------------------------------------------- A 段
def scenario_s1(ids: dict[str, str], checker: Checker) -> None:
    conversation_id = ids[S1]
    events = run_chat(conversation_id, S1)
    message = last_assistant(conversation_id)
    content = str(message.get("content") or "")
    checker.eq(tool_names(message), [], "S1 正文里的围栏示例不得进入执行器")
    checker.ok(
        not any(str(e.get("type")) == "tool_confirm" for e in events),
        "S1 不应出现确认卡事件",
    )
    # 停止条件 ③：围栏后的正文 delta 不得消失。
    deltas = "".join(str(e.get("content") or "") for e in events if str(e.get("type")) == "delta")
    checker.contains(deltas, "上面只是格式说明", "S1 流式正文必须完整（围栏后的文字不得被吞）")
    checker.contains(content, "上面只是格式说明", "S1 落库正文必须完整")
    checker.contains(content, '{"type": "tool"', "S1 围栏示例原文必须原样保留给用户看")
    checker.eq(CALLS[S1], 1, "S1 模型调用次数")


def scenario_s4(ids: dict[str, str], checker: Checker) -> None:
    conversation_id = ids[S4]
    events = run_chat(conversation_id, S4)
    message = last_assistant(conversation_id)
    checker.eq(tool_names(message), [], "S4 围栏包 final 不得执行工具")
    checker.ok(
        not any(str(e.get("type")) == "tool_confirm" for e in events),
        "S4 围栏包 final 不应弹确认卡",
    )
    checker.eq(str(message.get("content") or ""), "这是最终答复。", "S4 正文应取围栏内的 final 内容")


def scenario_s5(ids: dict[str, str], checker: Checker) -> None:
    conversation_id = ids[S5]
    events = run_chat(conversation_id, S5)
    message = last_assistant(conversation_id)
    names = tool_names(message)
    checker.eq(names, ["list_directory"], "S5 只应执行尾部裸协议那一个工具")
    checker.ok(
        not any(str(e.get("type")) == "tool_confirm" for e in events),
        "S5 裸协议在 auto 档不应弹确认卡（I/J 回归点）",
    )
    # 停止条件 ③：围栏里的那段正文必须真的流到用户面前。
    # 断言取**流式 delta**而不是落库 activity —— activity 只保留最后一段正文
    # （过程播报会被丢弃，那是 §九.97 的设计），它会把这个断言变成假红。
    deltas = "".join(str(e.get("content") or "") for e in events if str(e.get("type")) == "delta")
    checker.contains(deltas, "先看个例子", "S5 围栏示例所在的那段正文必须流出来（不得被吞）")
    checker.contains(deltas, '"tool": "read_file"', "S5 围栏示例原文必须可见")
    checker.eq(str(message.get("content") or ""), S5_FINAL, "S5 最终答复")


def scenario_s3(ids: dict[str, str], checker: Checker) -> None:
    conversation_id = ids[S3]
    collected: list[dict] = []
    worker = threading.Thread(target=run_chat, args=(conversation_id, S3, collected), daemon=True)
    worker.start()
    confirm = wait_for_confirm(collected)
    if confirm is None:
        checker.ok(False, "S3 未等到 tool_confirm（围栏动作没有先确认？）")
        return
    checker.eq(str(confirm.get("action_source") or ""), "fenced", "S3 确认事件的 action_source")
    run_id = str(confirm.get("run_id") or "")
    checker.ok(bool(run_id), "S3 确认事件必须带 run_id")
    post("/api/tool/reject", {"confirm_id": str(confirm.get("confirm_id") or ""), "run_id": run_id})
    checker.ok(wait_run_end(worker), "S3 拒绝后 run 必须在限期内收尾")
    # 停止条件 ②：拒绝后不得再发起新模型调用（只在轮询结束后取数，避免竞态）。
    checker.eq(CALLS[S3], 1, "S3 拒绝后模型调用次数")

    message = last_assistant(conversation_id)
    tools = [item.get("run") or {} for item in activity_of(message) if item.get("type") == "tool"]
    # 被拒的动作会留一条**未执行**的记账（success=False + result 说明），但绝不许真的跑起来。
    executed = [
        run for run in tools
        if run.get("success") or "未执行" not in str(run.get("result") or "")
    ]
    checker.eq(executed, [], "S3 被拒的围栏动作不得真的执行")
    checker.eq(
        [str(run.get("action_source") or "") for run in tools], ["fenced"],
        "S3 被拒动作的记账必须带 action_source=fenced",
    )
    content = str(message.get("content") or "")
    checker.contains(content, "list_directory", "S3 收尾正文必须保留模型原文")
    checker.contains(content, "未执行", "S3 收尾必须标注该动作未执行")
    checker.ok("FENCED_REJECTED" not in content, "S3 内部标记不得落库")


# ---------------------------------------------------------------- B 段（浏览器）
def browser_part(checker: Checker) -> None:
    env = dict(os.environ)
    env.update({
        "NAIBA_SMOKE_BASE": BASE,
        "NAIBA_FENCE_S2_TITLE": S2,
        "NAIBA_FENCE_S2M_TITLE": S2M,
        "NODE_PATH": str(ROOT / "node_modules"),
    })
    try:
        proc = subprocess.run(  # noqa: S603 - 固定 argv
            ["node", str(ROOT / "verify" / "fence_action_smoke.cjs")],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=420,
        )
    except FileNotFoundError:
        checker.ok(False, "找不到 node（本机装在 D:\\Apps\\NodeJS，需先加进 PATH）")
        return
    print(proc.stdout or "", end="")
    if proc.stderr.strip():
        print("--- node stderr ---")
        print(proc.stderr)
    if proc.returncode != 0:
        checker.ok(False, "浏览器段未通过（详见上方 node 输出）")
        return
    # 后端侧复核：只确认一次、两个工具都成功。
    listing = get_json("/api/conversations") or {}
    rows = [c for c in (listing.get("conversations") or []) if c.get("title") in (S2, S2M)]
    by_title = {str(row.get("title")): str(row.get("id")) for row in rows}
    for title in (S2, S2M):
        conversation_id = by_title.get(title, "")
        if not conversation_id:
            checker.ok(False, f"找不到会话 {title}")
            continue
        message = last_assistant(conversation_id)
        names = tool_names(message)
        checker.eq(names, ["list_directory", "read_file"], f"{title} 两个围栏动作都应执行")
        checker.eq(str(message.get("content") or ""), S2_FINAL, f"{title} 最终答复")
        runs = [
            item.get("run") or {} for item in activity_of(message) if item.get("type") == "tool"
        ]
        checker.ok(
            all(bool(run.get("success")) for run in runs),
            f"{title} 工具执行必须全部成功：{runs}",
        )
        run_id = str((message.get("metadata") or {}).get("run_id") or "")
        confirms = [
            event for event in run_events(run_id)
            if str(event.get("type")) == "tool_confirm"
        ]
        checker.eq(len(confirms), 1, f"{title} 整个 Run 只应确认一次")


def main() -> int:
    import shutil

    fake = start_fake_model()
    log = (ROOT / "verify" / "_fence_action_server.log").open("w", encoding="utf-8")
    server: subprocess.Popen | None = None
    checker = Checker()
    try:
        # 每轮全新隔离根：上一轮的 run 记录会让"确认次数"这类断言读到旧数据。
        shutil.rmtree(ISOLATED_ROOT, ignore_errors=True)
        ISOLATED_ROOT.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # 播种必须在起服务**之前**：运行中的实例持有一份内存里的 ConfigStore，
        # 它不会重读盘上的 config.json —— 起完服务再写供应商，模型请求会报
        # 「找不到模型配置」。而"运行记录"倒是要等服务起来再播（启动清理会改写），
        # 这里播的只有配置与会话，不受那条约束影响。
        ids = seed()
        env = dict(os.environ)
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        server = subprocess.Popen(  # noqa: S603 - 固定 argv
            [sys.executable, str(Path(__file__).resolve()), "--serve"],
            cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if not wait_health():
            print(f"隔离实例未就绪（日志见 {log.name}）")
            return 1
        print(f"隔离实例就绪 {BASE}（data_dir={DATA_DIR}）", flush=True)

        scenario_s1(ids, checker)
        scenario_s4(ids, checker)
        scenario_s5(ids, checker)
        scenario_s3(ids, checker)
        browser_part(checker)

        if checker.failures:
            print(f"\n冒烟失败（{len(checker.failures)} 项）：")
            for item in checker.failures:
                print(f"  - {item}")
            return 1
        print(
            "冒烟通过：正文夹围栏示例不执行且正文完整；围栏包 final 直接放行；"
            "围栏示例+尾部裸协议只执行裸协议（auto 档不弹卡）；围栏动作被拒不执行也不重发；"
            "整条围栏动作桌面与手机各确认一次、批准本轮后第二个动作免确认。"
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - 冒烟要把异常变成可读结论
        import traceback

        print(f"冒烟异常：{type(exc).__name__}: {exc}")
        print(traceback.format_exc())
        return 1
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
        print("开发 config.json 未修改；隔离目录保留供排查（_tmp_fence_action）")


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
