# -*- coding: utf-8 -*-
"""冒烟：上下文圆环逐请求刷新 + 达阈值弹窗提醒一次（阈值可配）。

编排（全自动，跑完还原现场）：
1. 临时给源码 config.json 加一个模型供应商（context_window=100000）并把
   context_warning_percent 设为 50；
2. 起 8797 源码 server，**就绪之后**才播种「仍在运行」的会话 + usage #1（8,200 = 8.2%）；
3. Playwright：圆环立即显示 8.2% 且**未达阈值不弹窗** → 通知编排追加 usage #2（85,000 = 85%）
   → 断言弹窗出现、文案含阈值 → 关闭弹窗 → 通知追加 usage #3（90,000）→ 断言**不再二次弹窗**；
4. 收尾：kill server、还原 config.json、清理播种数据。

用法：.venv\\Scripts\\python.exe verify\\ring_usage_smoke.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "verify"
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from naiba.storage.store import ChatStorage  # noqa: E402

PORT = 8797
BASE = f"http://127.0.0.1:{PORT}"
CONFIG = ROOT / "config.json"
BACKUP = TMP / "config.ring.bak.json"
STEP = TMP / "ring_step.txt"
SERVER_LOG = TMP / "ring_usage_server.log"
NODE_LOG = TMP / "ring_usage_node.log"
TITLE = "圆环用量冒烟"
IDLE_TITLE = "圆环空闲冒烟"
PROVIDER_ID = "ringsmoke"
WARNING_PERCENT = 50
STORAGE = ChatStorage(ROOT / "data" / "chat.db")


def _usage(context: int, output: int, requests: int) -> dict:
    return {
        "input_tokens": context - output,
        "output_tokens": output,
        "total_tokens": context,
        "cached_tokens": max(0, context - output - 1600),
        "uncached_tokens": 1600,
        "requests": requests,
        "last_input_tokens": context - output,
        "last_output_tokens": output,
        "context_tokens": context,
        "cache_hit_rate": 80.0,
        "requests_detail": [{
            "index": index + 1,
            "input_tokens": context - output,
            "output_tokens": output,
            "cached_tokens": 0,
            "total_tokens": context,
            "request_ms": 1500,
        } for index in range(requests)],
    }


USAGE_1 = _usage(8200, 200, 1)
USAGE_2 = _usage(85000, 500, 2)
USAGE_3 = _usage(88000, 500, 3)   # 只比上次提醒（85%）涨 3% → 不应再弹
USAGE_4 = _usage(91000, 500, 4)   # 再涨 6% → 应再次弹


def cleanup_conversations() -> None:
    for conversation in STORAGE.list_conversations():
        title = str(conversation.get("title") or "")
        if "圆环用量" in title or "圆环空闲" in title:
            STORAGE.delete_conversation(str(conversation["id"]))


def patch_config() -> None:
    BACKUP.write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    providers = [p for p in data.get("providers", []) if p.get("id") != PROVIDER_ID]
    providers.append({
        "id": PROVIDER_ID,
        "kind": "online",
        "name": "圆环冒烟",
        "base_url": "http://127.0.0.1:1",
        "model": "ring-smoke",
        "request_format": "openai_chat",
        "context_window": 100000,
        "api_key": "",
    })
    data["providers"] = providers
    data["default_model_key"] = f"online:{PROVIDER_ID}"
    data["provider_id"] = PROVIDER_ID
    data["context_warning_percent"] = WARNING_PERCENT
    CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def restore_config() -> None:
    if BACKUP.exists():
        CONFIG.write_text(BACKUP.read_text(encoding="utf-8"), encoding="utf-8")
        BACKUP.unlink()


def seed() -> tuple[str, str, str]:
    conversation = STORAGE.create_conversation(TITLE)
    conversation_id = str(conversation["id"])
    run = STORAGE.create_chat_run(
        conversation_id,
        "圆环冒烟：本次请求已完成，圆环应即时刷新",
        [],
        {"id": PROVIDER_ID, "name": "圆环冒烟"},
        {"interaction_mode": "craft"},
        "craft",
        title_text=TITLE,
    )
    run_id = str(run["id"])
    STORAGE.append_run_event(run_id, {"type": "run_started"})
    STORAGE.append_run_event(run_id, {"type": "status", "message": "正在思考（第 1 轮）"})
    STORAGE.append_run_event(run_id, {"type": "usage", "usage": USAGE_1})

    # 第二个会话：已结束（无活动 run）但上下文已超阈值 —— 打开时不该弹窗，点发送才弹。
    idle = STORAGE.create_conversation(IDLE_TITLE)
    idle_id = str(idle["id"])
    STORAGE.add_message(idle_id, "user", "圆环空闲冒烟：这条会话已结束")
    STORAGE.add_message(
        idle_id,
        "assistant",
        "已结束的回复",
        {"usage": {**USAGE_3, "requests_detail": USAGE_3["requests_detail"]}},
    )
    return conversation_id, run_id, idle_id


def wait_for_server(deadline: float = 60.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=3) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - 启动期连接失败是正常过程
            time.sleep(0.5)
    return False


def wait_for_step(expected: str, node: subprocess.Popen, deadline: float = 60.0) -> bool:
    end = time.time() + deadline
    while time.time() < end and node.poll() is None:
        if STEP.exists() and STEP.read_text(encoding="utf-8").strip() == expected:
            return True
        time.sleep(0.2)
    return False


def main() -> int:
    server: subprocess.Popen | None = None
    STEP.unlink(missing_ok=True)
    cleanup_conversations()
    patch_config()
    try:
        env = dict(os.environ)
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        with open(SERVER_LOG, "w", encoding="utf-8") as log:
            server = subprocess.Popen(
                [sys.executable, "server.py", "--host", "127.0.0.1", "--port", str(PORT)],
                cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
            )
        if not wait_for_server():
            print("FAIL  源码 server 未在 60s 内就绪，见", SERVER_LOG)
            return 1

        # 必须在 server 启动之后播种：storage 启动时会把 queued/running 的存量任务
        # 标记为 interrupted（「服务重启，运行已中断」），先播会被立刻中断而无法 resume。
        conversation_id, run_id, idle_id = seed()
        print(f"seeded running={conversation_id} idle={idle_id} warning={WARNING_PERCENT}%")

        node_env = dict(os.environ)
        node_env["NODE_PATH"] = str(Path.home() / "node_modules")
        node_env["NAIBA_SMOKE_BASE"] = BASE
        node_env["NAIBA_RING_STEP"] = str(STEP)
        node_env["NAIBA_RING_TITLE"] = TITLE
        node_env["NAIBA_RING_IDLE_TITLE"] = IDLE_TITLE
        with open(NODE_LOG, "w", encoding="utf-8") as node_log:
            node = subprocess.Popen(
                ["node", str(TMP / "ring_usage_smoke.cjs")],
                cwd=str(ROOT), stdout=node_log, stderr=subprocess.STDOUT, env=node_env,
            )
            if wait_for_step("2", node):
                STORAGE.append_run_event(run_id, {"type": "usage", "usage": USAGE_2})
                print("appended usage #2 (85%)")
            if wait_for_step("3", node):
                STORAGE.append_run_event(run_id, {"type": "usage", "usage": USAGE_3})
                print("appended usage #3 (88%)")
            if wait_for_step("4", node):
                STORAGE.append_run_event(run_id, {"type": "usage", "usage": USAGE_4})
                print("appended usage #4 (91%)")
            node.wait(timeout=120)

        output = NODE_LOG.read_text(encoding="utf-8", errors="replace")
        print(output)
        return 1 if any(line.startswith("FAIL") for line in output.splitlines()) else 0
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
        restore_config()
        cleanup_conversations()
        STEP.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
