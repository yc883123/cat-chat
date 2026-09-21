# -*- coding: utf-8 -*-
"""冒烟：分支时可选「更换 Agent」的自编排入口（真后端 + 无头 Edge）。

用**独立数据目录**（`verify/branch_agent_tmp/`）与独立端口起源码 server，不碰开发
config.json 与开发库（沿用 `batch1_smoke.py` 的隔离模板）。

播种一个**已固化**的源会话：`enabled_tool_ids` / `skill_policy` / `first_turn` 三列都非空
（这正是「Agent 下拉被锁」的成因），历史两轮，分支点 = 第二问。

断言都落在**真实后端状态**上（不只 DOM），并且钉在 §九.81 要求的**可用性**那一层：

1. 源会话：`#agentSelect` 是**禁用**的（已固化 ⇒ 锁），后端三列非空；
2. 点「分支」→ 先弹选择框（不许直接建会话）；选「更换 Agent」后：
   新会话 `enabled_tool_ids == []`、`skill_policy` 空、`first_turn` 空、
   **`agent_id` 仍等于源会话**、历史只复制 2 条；
3. 关键：新会话的 `#agentSelect` 变成**可用**（enable）——「按钮存在」≠「用户能用」；
4. 再走一次选「保持当前 Agent」：新会话工具集**逐字等于源会话**、下拉**仍然禁用**（回归）；
5. 取消：不产生新会话（会话总数不变）；
6. 零页面错误 / 零 4xx 噪音。

用法：.venv\\Scripts\\python.exe verify\\branch_agent_smoke.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PORT = int(os.environ.get("NAIBA_SMOKE_PORT", "8804"))
BASE = f"http://127.0.0.1:{PORT}"
ISOLATED_ROOT = Path(os.environ.get("NAIBA_SMOKE_ROOT", ROOT / "verify" / "branch_agent_tmp"))
DATA_DIR = ISOLATED_ROOT / "data"

PROVIDER = {"id": "smoke-ba", "name": "冒烟 API BA", "model": "smoke-default-ba"}
BOUND_MODEL = "smoke-ba-pro"

SRC_TITLE = "分支换 Agent 冒烟：源会话"
SRC_Q1 = "源会话第一问"
SRC_A1 = "源会话第一答"
SRC_Q2 = "源会话第二问：这里是分支点"
SRC_A2 = "源会话第二答"
FROZEN_TOOLS = ["read_file"]
AGENT_ID = "smoke-agent"


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
    """播种一个已固化的源会话，返回给浏览器脚本用的环境变量表。"""
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
    NaibaChatApp.api_upsert_model_profile(app, {
        "id": PROVIDER["id"], "kind": "online", "name": PROVIDER["name"],
        "base_url": "https://example.invalid/v1", "api_key": "sk-smoke",
        "request_format": "openai_chat", "model": PROVIDER["model"],
    })

    src = storage.create_conversation(
        SRC_TITLE, model_key=f"online:{PROVIDER['id']}", model_name=BOUND_MODEL,
        workspace_dir=str(ISOLATED_ROOT / "workspace"), agent_id=AGENT_ID,
    )
    # 三列固化态 = 「Agent 下拉被锁」的真实成因，缺一条这个冒烟就白跑。
    storage.set_enabled_tool_ids(src["id"], FROZEN_TOOLS)
    storage.set_conversation_skill_policy(src["id"], {"ids": ["demo-skill"]})
    storage.set_conversation_first_turn(src["id"], {"agent_name": "旧 Agent"})
    storage.add_message(src["id"], "user", SRC_Q1)
    storage.add_message(src["id"], "assistant", SRC_A1)
    src_q2 = storage.add_message(src["id"], "user", SRC_Q2)["id"]
    storage.add_message(src["id"], "assistant", SRC_A2)

    return {
        "NAIBA_SMOKE_SRC_ID": src["id"],
        "NAIBA_SMOKE_Q2_ID": src_q2,
        "NAIBA_SMOKE_FROZEN_TOOLS": ",".join(FROZEN_TOOLS),
        "NAIBA_SMOKE_AGENT_ID": AGENT_ID,
    }


def main() -> int:
    global ISOLATED_ROOT, DATA_DIR
    isolated = tempfile.TemporaryDirectory(prefix="branch_agent_smoke_", dir=ROOT / "verify")
    ISOLATED_ROOT = Path(isolated.name)
    DATA_DIR = ISOLATED_ROOT / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    code = 1
    server: subprocess.Popen | None = None
    log = (ROOT / "verify" / "branch_agent_smoke_server.log").open("w", encoding="utf-8")
    try:
        env_snapshot = seed()
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
        node_env["NAIBA_SMOKE_BASE"] = BASE
        node_env.update(env_snapshot)
        node = subprocess.run(  # noqa: S603 - 固定 argv
            [_node_exe(), str(ROOT / "verify" / "branch_agent_smoke.cjs")],
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
        isolated.cleanup()
        print("已清理独立测试目录（开发 config.json 未修改）"
              f"；server 已退出（exit={server.returncode if server else 'n/a'}）")
    return code


def _node_exe() -> str:
    """优先用托管 node（PATH 在本沙箱里不可靠）。"""
    candidate = Path.home() / ".workbuddy" / "binaries" / "node" / "versions" / "22.22.2-3" / "node.exe"
    return str(candidate) if candidate.is_file() else "node"


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
