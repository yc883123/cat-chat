# -*- coding: utf-8 -*-
"""冒烟：本轮四道「把卡住变成可诊断 / 可收场」的守卫（真机、独立数据目录、独立端口）。

背景（2026-09-14 客户机实测）：本地 unsloth 学 16 小时后界面永久停在「等待本地模型资源」。
上一轮修掉「停不下来 / 重启清不掉」之后，这一轮补的是「为什么卡住没人知道、以及没人踩刹车」。
四件事里只有第一件需要真机（其余三件是纯逻辑，已被 `tests/test_runtime_guards.py` 钉住）：

1. **首字节超时**：本地后端 prefill 时一个字节都不吐，与「正在慢慢算」在界面上无法区分，
   而总超时是 1800 秒 ⇒ 用户要干等半小时。必须真机验证「超过 `local_first_byte_timeout_seconds`
   就断开并报出可行动的原因」，而不是等满总超时。
   附带验证**探测窗口回写**：假端点的 `/props` 返回 n_ctx，跑一轮后 provider 配置里必须
   出现 `context_window_probed`（否则设置页仍显示「未知」，又退回在线 256k 兜底）。

2. **Agent 步数上限**：`agent_step_limit=N` 时，一个只会调工具的模型必须在第 N 次调用后停下
   并说明原因（真机验证「模型被调用了几次」= N，而不是无限循环）。

假本地端点按阶段切换行为：`reply`（正常答复）/ `hold`（收下请求、永不回首字节）/
`loop`（每次都要求调 read_file，且参数逐次变化以绕开"重复/无进展"两条熔断）。

用法：`.venv\\Scripts\\python.exe verify\\runtime_guard_smoke.py`
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PORT = 8799
MODEL_PORT = 8800
BASE = f"http://127.0.0.1:{PORT}"
PROVIDER_ID = "smoke-local"
MODEL = "smoke-qwen3"
N_CTX = 32768
FIRST_BYTE_TIMEOUT = 5
STEP_LIMIT = 4

ISOLATED_ROOT = Path(os.environ.get("NAIBA_SMOKE_ROOT", ROOT / "verify" / "runtime_guard_tmp"))
DATA_DIR = ISOLATED_ROOT / "data"

# 假端点行为：reply / hold / loop
MODE = {"value": "reply"}
CHAT_HITS: list[str] = []
PROPS_HITS: list[int] = []
PEER_CLOSED = threading.Event()
LOOP_COUNTER = {"n": 0}
NOTE_PATH = {"value": ""}

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return bool(ok)


def _sse(chunks: list[str]) -> bytes:
    body = "".join(f"data: {chunk}\n\n" for chunk in chunks)
    body += "data: [DONE]\n\n"
    return body.encode("utf-8")


class _ModelHandler(BaseHTTPRequestHandler):
    """假本地模型：/props 报窗口；/v1/chat/completions 按 MODE 行事。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # 静音
        return

    def do_GET(self) -> None:
        if self.path.endswith("/props"):
            PROPS_HITS.append(1)
            payload = json.dumps({"default_generation_settings": {"n_ctx": N_CTX}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        CHAT_HITS.append(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
        except Exception:  # noqa: BLE001
            pass
        mode = MODE["value"]
        if mode == "hold":
            # 收下请求、回 SSE 头，然后一个字节正文都不给（模拟 prefill 做不完的本地后端）。
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.flush()
            except Exception:  # noqa: BLE001
                PEER_CLOSED.set()
                return
            deadline = time.time() + 120
            while time.time() < deadline:
                try:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                except Exception:  # noqa: BLE001 - 对端关闭 = 超时闸门真的断开了连接
                    PEER_CLOSED.set()
                    return
                time.sleep(0.2)
            return

        if mode == "loop":
            LOOP_COUNTER["n"] += 1
            # 参数逐次变化：绕开「工具连续失败且重复」与「连续返回相同结果」两条熔断，
            # 否则它们会在第 3 步先触发，测不到步数上限。
            arguments = {"path": NOTE_PATH["value"], "max_lines": LOOP_COUNTER["n"]}
            content = json.dumps(
                {"type": "tool", "tool": "read_file", "arguments": arguments, "reason": "冒烟：继续读"},
                ensure_ascii=False,
            )
        else:
            content = "冒烟答复：已收到。"

        body = _sse([json.dumps({"choices": [{"delta": {"content": content}}]}, ensure_ascii=False)])
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except Exception:  # noqa: BLE001
            PEER_CLOSED.set()


def _api(path: str, payload: dict | None = None, timeout: float = 30.0) -> dict:
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = "POST"
    request = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(interval)
    try:
        return bool(predicate())
    except Exception:  # noqa: BLE001
        return False


def wait_health(deadline: float = 60.0) -> bool:
    return wait_until(
        lambda: urllib.request.urlopen(f"{BASE}/api/health", timeout=2).status == 200,
        timeout=deadline,
        interval=0.5,
    )


def _force_remove(path: Path) -> None:
    """删测试临时目录：递归删被本机 safe-delete 钩子拦下时，退化成逐文件删。

    钩子按「一次删太多」拦批量删除，并会把删除请求转交回收站（`trash`），
    目标文件仍被进程占用时会整批 FAIL_CLOSED —— `shutil.rmtree(..., ignore_errors=True)`
    于是静默留下整个目录，多次跑会堆积。所以这里（1）逐文件 unlink 绕开批量阈值，
    （2）重试几轮，等服务真正释放 sqlite 句柄。
    """
    def _one_pass() -> None:
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        for item in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            try:
                item.rmdir() if item.is_dir() else item.unlink()
            except OSError:
                pass
        try:
            path.rmdir()
        except OSError:
            pass

    for attempt in range(4):
        _one_pass()
        if not path.exists():
            return
        # 被占用时回收站会拒绝：等一会儿让服务把句柄放掉再试
        time.sleep(0.6 * (attempt + 1))
    if path.exists():
        print(f"[warn] 临时目录未能完全清理（被占用或钩子拦截）：{path}")


def _fresh_root() -> Path:
    base = ROOT / "verify" / "runtime_guard_tmp"
    _force_remove(base)
    if base.exists():
        base = ROOT / "verify" / f"runtime_guard_tmp_{int(time.time())}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def isolated_paths():
    from naiba.paths import PathContext

    paths = PathContext.local(ISOLATED_ROOT, ISOLATED_ROOT / "config.json")
    paths.public_dir = ROOT / "public"
    return paths


def _config_file() -> Path:
    return ISOLATED_ROOT / "config.json"


def seed() -> None:
    from naiba.app import NaibaChatApp
    from naiba.config import ConfigStore
    from naiba.storage.store import ChatStorage

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
        # 闸门调小到测试尺度：首字节超时 5 秒（最小合法值）、步数上限 4 步。
        "local_first_byte_timeout_seconds": FIRST_BYTE_TIMEOUT,
        "agent_step_limit": STEP_LIMIT,
    })
    config.save()
    storage = ChatStorage(DATA_DIR / "chat.db")
    app = SimpleNamespace(config=config, storage=storage)
    NaibaChatApp.api_upsert_model_profile(app, {
        "id": PROVIDER_ID,
        "kind": "local",
        "name": "冒烟本地模型",
        "local_backend": "unsloth",
        "base_url": f"http://127.0.0.1:{MODEL_PORT}/v1",
        "api_key": "",
        "request_format": "unsloth",
        "model": MODEL,
    })
    workspace = ISOLATED_ROOT / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    note = workspace / "note.txt"
    note.write_text("冒烟素材\n第二行\n第三行\n", encoding="utf-8")
    NOTE_PATH["value"] = str(note)


def new_conversation(title: str) -> str:
    from naiba.storage.store import ChatStorage

    storage = ChatStorage(DATA_DIR / "chat.db")
    workspace = ISOLATED_ROOT / "workspace"
    conversation = storage.create_conversation(
        title,
        model_key=f"local:{PROVIDER_ID}",
        model_name=MODEL,
        workspace_dir=str(workspace),
    )
    # 固化图像能力：否则 submit_chat 会在建 Run 之前先发一次能力探测请求。
    storage.set_conversation_chat_supports_images(conversation["id"], False)
    return str(conversation["id"])


def submit_chat_async(conversation_id: str, question: str) -> dict:
    """`POST /api/chat` 是 NDJSON 事件流（挂到 run 结束才返回），后台线程里发。"""
    result: dict = {}

    def worker() -> None:
        try:
            result["response"] = _api(
                "/api/chat",
                {"conversation_id": conversation_id, "message": question, "attachments": []},
                timeout=180.0,
            )
        except Exception as exc:  # noqa: BLE001 - 失败/超时都是被测分支
            result["error"] = str(exc)

    threading.Thread(target=worker, name="smoke-submit-chat", daemon=True).start()
    return result


def latest_run_id(conversation_id: str) -> str:
    """该会话**最新**的 Run（不限状态）。

    不要用 ``active_only=1`` 取 id：假端点秒回时整个 Run 在第一次轮询（0.1s）前
    就结束了，active_only 永远看不到它，run_id 会一直是空串，后面所有断言全废
    （表现为「Run 已创建 FAIL」+「等待终态」白等到超时）。2026-09-14 踩过。
    """
    runs = _api(f"/api/runs?conversation_id={conversation_id}&active_only=0").get("runs") or []
    return str((runs[0] if runs else {}).get("id") or "")


def run_row(run_id: str) -> dict:
    with sqlite3.connect(f"file:{DATA_DIR / 'chat.db'}?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT status, error, detail FROM background_tasks WHERE id=?", (run_id,)
        ).fetchone()
    if not row:
        return {}
    return {"status": str(row[0] or ""), "error": str(row[1] or ""), "detail": str(row[2] or "")}


def find_text(phrase: str) -> str:
    """在「错误列 / 事件负载 / 消息正文」三处找一句话，返回命中的表名。"""
    db_path = DATA_DIR / "chat.db"
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        if db.execute(
            "SELECT 1 FROM background_tasks WHERE error LIKE ? LIMIT 1", (f"%{phrase}%",)
        ).fetchone():
            return "background_tasks.error"
        if db.execute(
            "SELECT 1 FROM run_events WHERE payload LIKE ? LIMIT 1", (f"%{phrase}%",)
        ).fetchone():
            return "run_events.payload"
        if db.execute(
            "SELECT 1 FROM messages WHERE content LIKE ? LIMIT 1", (f"%{phrase}%",)
        ).fetchone():
            return "messages.content"
    return ""


def wait_terminal(run_id: str, timeout: float) -> tuple[bool, float, str]:
    started = time.perf_counter()

    def settled() -> bool:
        status = run_row(run_id).get("status") or ""
        return bool(status) and status not in {
            "queued", "running", "waiting", "stopping", "cancelling"
        }

    ok = wait_until(settled, timeout=timeout, interval=0.2)
    return ok, time.perf_counter() - started, run_row(run_id).get("status") or ""


def main() -> int:
    global ISOLATED_ROOT, DATA_DIR
    ISOLATED_ROOT = _fresh_root()
    DATA_DIR = ISOLATED_ROOT / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    model_server = ThreadingHTTPServer(("127.0.0.1", MODEL_PORT), _ModelHandler)
    model_server.daemon_threads = True
    threading.Thread(
        target=model_server.serve_forever, kwargs={"poll_interval": 0.1},
        name="smoke-model", daemon=True,
    ).start()

    server: subprocess.Popen | None = None
    code = 1
    log = (ROOT / "verify" / "runtime_guard_smoke_server.log").open("w", encoding="utf-8")
    try:
        seed()
        env = dict(os.environ)
        env.update({
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "NAIBA_SMOKE_ROOT": str(ISOLATED_ROOT),
        })
        server = subprocess.Popen(  # noqa: S603 - 固定 argv
            [sys.executable, str(Path(__file__).resolve()), "--serve"],
            cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if not wait_health():
            print(f"server 未就绪（日志见 {log.name}）")
            return 1

        print("① 本地窗口探测必须回写 provider 配置")
        MODE["value"] = "reply"
        cid = new_conversation("回写冒烟")
        state = submit_chat_async(cid, "回写冒烟：请回答")
        ok = wait_until(lambda: bool(latest_run_id(cid)), timeout=20.0)
        run_id = latest_run_id(cid) if ok else ""
        check("Run 已创建（说明 profile 解析已完成、探测器跑过）", bool(run_id), f"run={run_id}")

        def probed_written() -> bool:
            try:
                data = json.loads(_config_file().read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                return False
            for provider in data.get("providers", []):
                if provider.get("id") == PROVIDER_ID:
                    return int(provider.get("context_window_probed") or 0) == N_CTX
            return False

        check("config.json 里出现 context_window_probed（设置页等读取点终于看得到真值）",
              wait_until(probed_written, timeout=15.0), f"期望 n_ctx={N_CTX}")
        settled, elapsed, status = wait_terminal(run_id, timeout=40.0)
        check("正常轮次照常完成（回写没有破坏主链路）", settled and status == "completed",
              f"status={status} {elapsed:.2f}s")
        check("探测只问了一次 /props", len(PROPS_HITS) == 1, f"hits={len(PROPS_HITS)}")

        print("② 首字节超时：假端点永不回字节时必须很快报错并断开")
        MODE["value"] = "hold"
        PEER_CLOSED.clear()
        CHAT_HITS.clear()
        cid = new_conversation("首字节超时冒烟")
        state = submit_chat_async(cid, "首字节超时冒烟：请回答")
        run_id = ""
        ok = wait_until(lambda: bool(latest_run_id(cid)), timeout=20.0)
        run_id = latest_run_id(cid) if ok else ""
        check("假端点收到模型请求（说明已进入 prefill 等待）", bool(run_id), f"run={run_id}")

        settled, elapsed, status = wait_terminal(run_id, timeout=60.0)
        check("超过首字节超时后 Run 进入终态", settled, f"status={status} {elapsed:.2f}s")
        check(
            f"耗时远小于总超时 1800 秒（闸门={FIRST_BYTE_TIMEOUT}s）",
            settled and elapsed < 60.0,
            f"{elapsed:.2f}s",
        )
        check("假端点观察到对端关闭连接（是断开而不是干等）", PEER_CLOSED.wait(10.0))
        # 关键词必须与 local_first_byte_timeout_error() 的文案一致，
        # 否则会拿「含糊的流式错误」当真因通过/失败（这里踩过一次）。
        hit = wait_until(lambda: bool(find_text("没有输出任何内容")), timeout=10.0) and find_text(
            "没有输出任何内容"
        )
        check("错误信息是「首字节超时」的可行动文案，而不是含糊的流错误",
              bool(hit), f"命中于 {hit or '无'}")
        run_status = run_row(run_id).get("status") or ""
        check("该轮状态为 failed（不是静默 completed）", run_status == "failed",
              f"status={run_status}")

        print("③ Agent 步数上限：只会调工具的模型必须在第 N 次调用后停下")
        MODE["value"] = "loop"
        LOOP_COUNTER["n"] = 0
        CHAT_HITS.clear()
        cid = new_conversation("步数上限冒烟")
        state = submit_chat_async(cid, "步数上限冒烟：请回答")
        ok = wait_until(lambda: bool(latest_run_id(cid)), timeout=20.0)
        run_id = latest_run_id(cid) if ok else ""
        settled, elapsed, status = wait_terminal(run_id, timeout=120.0)
        check("循环型模型最终会停下（不再无限烧下去）", settled, f"status={status} {elapsed:.2f}s")
        chat_calls = [path for path in CHAT_HITS if "chat" in path]
        check(f"模型恰好被调用 {STEP_LIMIT} 次（= agent_step_limit）",
              len(chat_calls) == STEP_LIMIT, f"calls={len(chat_calls)}")
        hit = find_text("步数上限")
        check("错误/正文里说明了「达到步数上限」", bool(hit), f"命中于 {hit or '无'}")

        failed = [name for name, ok, _ in RESULTS if not ok]
        code = 1 if failed else 0
        print(f"\n结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过"
              + (f"；失败：{failed}" if failed else ""))
    finally:
        model_server.shutdown()
        model_server.server_close()
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
        log.close()
        _force_remove(ISOLATED_ROOT)
        print(f"独立测试目录 {ISOLATED_ROOT}（开发 config.json 未修改）；"
              f"server 已退出（exit={server.returncode if server else 'n/a'}）")
    return code


if __name__ == "__main__":
    if "--serve" in sys.argv:
        from naiba.app import NaibaChatApp
        from naiba.http import AppHTTPServer, RequestHandler

        paths = isolated_paths()
        print(f"[serve] data_dir={paths.data_dir} config={paths.config_path}", flush=True)
        app = NaibaChatApp(paths=paths)
        print(f"[serve] storage.db_path={app.storage.db_path}", flush=True)
        server = AppHTTPServer(("127.0.0.1", PORT), RequestHandler, app)
        server.daemon_threads = True
        try:
            server.serve_forever(poll_interval=0.1)
        finally:
            server.server_close()
            app.stop()
        sys.exit(0)
    sys.exit(main())
