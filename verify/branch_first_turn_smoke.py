"""分支对话继承「首轮上下文」折叠卡冒烟的自编排入口（随仓库发布的可复用资产）。

用**独立数据目录**（`verify/branch_first_turn_tmp/`）起源码 server，避免污染开发库
`data/chat.db`：先按 v16 口径播种一个会话（4 条消息 + 会话级 first_turn），
跑 branch_first_turn_smoke.cjs 验证「原会话显示折叠卡 → 分支后新会话也显示」，
跑完还原 config.json 并删除临时数据目录。

用法：.venv\\Scripts\\python.exe verify\\branch_first_turn_smoke.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # 直接跑本脚本时也能 import naiba
PORT = 8795
BASE = f"http://127.0.0.1:{PORT}"
DATA_DIR = ROOT / "verify" / "branch_first_turn_tmp"
SYSTEM_TEXT = "【分支冒烟】这是首轮系统提示词原文"


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


def seed() -> None:
    from naiba.storage.store import ChatStorage

    storage = ChatStorage(DATA_DIR / "chat.db")
    conversation = storage.create_conversation("分支冒烟会话")
    storage.add_message(conversation["id"], "user", "第一问：请介绍这个项目")
    storage.add_message(conversation["id"], "assistant", "第一答：这是冒烟用的历史消息。")
    storage.add_message(conversation["id"], "user", "第二问：继续")
    storage.add_message(conversation["id"], "assistant", "第二答：继续的回复。")
    storage.set_conversation_first_turn(conversation["id"], {
        "system": SYSTEM_TEXT,
        "tools": [{"name": "read_file", "description": "读取文本文件。"}],
        "options": {"temperature": 0.7},
        "model_key": "online:demo",
        "agent_name": "通用 Agent",
        "skills": [],
    })


def main() -> int:
    config_path = ROOT / "config.json"
    original_config = config_path.read_bytes()
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    config = json.loads(original_config.decode("utf-8"))
    config.update({"host": "127.0.0.1", "port": PORT, "data_dir": str(DATA_DIR)})
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 从改完 config.json 起，**任何异常路径都必须还原**（包括播种）——
    # 曾经把 seed() 放在 try 之外，播种一抛异常就把开发配置留在临时端口/临时目录上。
    code = 1
    server: subprocess.Popen | None = None
    log = (ROOT / "verify" / "branch_first_turn_smoke_server.log").open("w", encoding="utf-8")
    try:
        seed()
        env = dict(os.environ)
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        server = subprocess.Popen(  # noqa: S603 - 固定 argv
            [sys.executable, "server.py", "--host", "127.0.0.1", "--port", str(PORT)],
            cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
        )
        if not wait_health():
            print(f"server 未就绪（日志见 {log.name}）")
            return 1
        node_env = dict(os.environ)
        node_env["NODE_PATH"] = str(Path.home() / "node_modules")
        node_env["NAIBA_SMOKE_BASE"] = BASE
        node_env["NAIBA_FIRST_TURN_TEXT"] = SYSTEM_TEXT
        node = subprocess.run(  # noqa: S603 - 固定 argv
            ["node", str(ROOT / "verify" / "branch_first_turn_smoke.cjs")],
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
        log.close()
        # 还原现场：config.json（main_entry 会把 host/port 写回）与临时数据目录。
        config_path.write_bytes(original_config)
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        print(f"已还原 config.json 并清理 {DATA_DIR.name}"
              f"；server 已退出（exit={server.returncode if server else 'n/a'}）")
    return code


if __name__ == "__main__":
    sys.exit(main())
