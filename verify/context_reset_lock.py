# -*- coding: utf-8 -*-
"""「重置上下文后前端锁不解除」的回归冒烟（自编排：隔离源码实例 + 真浏览器 DOM 断言）。

被修的 bug（2026-09-18，终端用户在线 API 命中）：会话聊满 → 输入框被锁；模型调 reset_context
成功（或用户手动点「新会话」）之后分割线已落库、种子消息也填进了输入框，**但输入框仍是禁用态**，
交接流程的最后一步永远发不出去。根因是前端取「有效用量」时不认分割线：圆环与锁按分割线**之前**
的满量算（`public/js/03-media.js::updateContextUsage`），后端 `build_model_history` 其实已经从
最新分割线重算历史、轮首闸门也会放行——纯前端显示/锁没跟随重置。

为什么不用「静态 public/ 服务」：这里要的是真数据形态（真库 → 真接口 → 真渲染），静态下
`/api/bootstrap` 等一律 404，圆环连上限都拿不到（§九.92 的教训）。故沿用 `verify/_serve_tmp.py`：
真后端、独立 config / data_dir / 端口，既不碰用户数据，页面又是真实形态。

两部分断言，都跑真实前端代码：
A. 真数据（隔离库播种满量 usage；`context_limit_source` 让前端不必配置 context_window 也能算出 100%）
   ① 打开满量会话 → 锁（禁用态 + 占位符「上下文已满…」+ 圆环 100%）；
   ② 点该条回复上的真实「新会话」→ 圆环归零、输入框解锁（触发口 ②）；
   ③ 点「撤销」→ 满量重新生效、按需回锁（同一扫描天然覆盖，验证 2）；
   ④ 打开「模型已重置」的会话（分割线在末尾）→ 打开即解锁；点「填入种子消息」→ 种子进**可用**
      输入框（触发口 ①的终态形态，也是用户被卡住的那一步）；
   ⑤ 打开「分割线之下还有更小用量」的会话 → 圆环按线以下的用量算（10000/100000 = 10%）。
B. 模块级注入（不依赖后端造数据）——计划里的单消息入口 + 运行中保护 + 遗留 role=session 标记行。

用法：.venv\\Scripts\\python.exe verify\\context_reset_lock.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("NAIBA_CONTEXT_RESET_PORT", "8805"))
BASE = f"http://127.0.0.1:{PORT}"
TMP_ROOT = ROOT / "verify" / "_tmp_context_reset_lock"
SERVER_LOG = ROOT / "verify" / "_context_reset_lock_server.log"

# 满量会话用的上限/用量：种子数据只需前端能算出 100%。
LIMIT = 100000


def wait_until(predicate, timeout: float = 45.0, interval: float = 0.3) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 - 启动期连接失败是预期分支
            pass
        time.sleep(interval)
    return False


def usage(context_tokens: int, requests: int = 1) -> dict:
    return {
        "input_tokens": max(0, context_tokens - 500),
        "output_tokens": 500,
        "total_tokens": context_tokens,
        "context_tokens": context_tokens,
        # 前端只用「context_limit_source 有值 → 采信 context_limit」这一条口径，
        # 于是不必改隔离实例的供应商配置就能造出确定的百分比。
        "context_limit": LIMIT,
        "context_limit_source": "local_config",
        "model_key": "online:probe-te",
        "requests": requests,
        "requests_detail": [{
            "index": index + 1,
            "input_tokens": max(0, context_tokens - 500),
            "output_tokens": 500,
            "total_tokens": context_tokens,
            "cached_tokens": 0,
            "request_ms": 1200,
        } for index in range(requests)],
    }


def seed() -> None:
    """三个会话，分别覆盖：满量（锁）/ 模型已重置（分割线在末尾）/ 分割线之下还有用量。"""
    sys.path.insert(0, str(ROOT))
    from naiba.storage.store import ChatStorage

    storage = ChatStorage(TMP_ROOT / "data" / "chat.db")

    locked = storage.create_conversation("上下文重置解锁冒烟")
    storage.add_message(locked["id"], "user", "上下文重置解锁冒烟：这条会话的上下文已经聊满了")
    storage.add_message(locked["id"], "assistant", "满量回复：本条之上已经没有余量。", {
        "usage": usage(LIMIT),
    })

    reset = storage.create_conversation("上下文重置解锁冒烟-模型重置")
    storage.add_message(reset["id"], "user", "上下文重置解锁冒烟-模型重置：请写交接文档")
    storage.add_message(reset["id"], "assistant", "已交接，本轮到此结束。", {
        "usage": usage(LIMIT),
        "session_start": {
            "at": 1789000000000,
            "source": "tool",
            "handoff_path": "交接-冒烟.md",
            "note": "隔离冒烟夹具",
            "tasks": [{"id": "job_1", "kind": "job", "status": "running", "title": "渲染第 3 批"}],
        },
    })

    below = storage.create_conversation("上下文重置解锁冒烟-线下用量")
    storage.add_message(below["id"], "user", "上下文重置解锁冒烟-线下用量：第一问")
    storage.add_message(below["id"], "assistant", "第一答（已划出上下文）。", {
        "usage": usage(LIMIT),
        "session_start": {"at": 1789000000000, "source": "manual", "handoff_path": "", "tasks": []},
    })
    storage.add_message(below["id"], "user", "第二问：线以下继续")
    storage.add_message(below["id"], "assistant", "第二答（在线以下，用量很小）。", {
        "usage": usage(10000),
    })


def main() -> int:
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)

    log = SERVER_LOG.open("w", encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "PYTHONUTF8": "1",
        "NAIBA_TMP_ROOT": str(TMP_ROOT),
        "NAIBA_TMP_PORT": str(PORT),
    })
    server = subprocess.Popen(  # noqa: S603 - 固定 argv
        [sys.executable, str(ROOT / "verify" / "_serve_tmp.py")],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        if not wait_until(
            lambda: urllib.request.urlopen(f"{BASE}/api/health", timeout=2).status == 200
        ):
            print(f"隔离实例未就绪（日志见 {SERVER_LOG}）")
            return 1
        # 必须在 server 就绪之后播种：storage 启动时会把 queued/running 的存量任务标成
        # interrupted（「服务重启，运行已中断」），先播会被立刻改写（同 ring_usage_smoke）。
        seed()
        print(f"隔离实例就绪 {BASE}（data_dir={TMP_ROOT / 'data'}）")

        smoke_env = dict(os.environ)
        smoke_env.update({
            "NAIBA_SMOKE_BASE": BASE,
            "NAIBA_CONTEXT_RESET_LIMIT": str(LIMIT),
            "NODE_PATH": str(ROOT / "node_modules"),
        })
        try:
            proc = subprocess.run(  # noqa: S603 - 固定 argv
                ["node", str(ROOT / "verify" / "context_reset_lock.cjs")],
                cwd=str(ROOT), env=smoke_env, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            print("找不到 node：请确认它在 PATH 里（本机 node 装在 D:\\Apps\\NodeJS，需先加进 PATH）")
            return 1
        print(proc.stdout or "", end="")
        if proc.stderr.strip():
            print("--- node stderr ---")
            print(proc.stderr)
        return proc.returncode
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=10)
        log.close()
        shutil.rmtree(TMP_ROOT, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
