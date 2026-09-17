# -*- coding: utf-8 -*-
"""冒烟：第一批三项（全文搜索 / 分支导航 / 删除单条消息）的自编排入口。

用**独立数据目录**（`verify/batch1_tmp/`）与独立端口起源源 server，不碰开发 config.json
与开发库（沿用 `regenerate_smoke.py` 的隔离模板：`PathContext.local` + 独立端口 +
`--serve` 子进程 + Playwright）。

播种三个会话，覆盖三件事各自需要的最小真实数据形态：
- **A 源会话**：两轮问答。最后一轮的助手回复里埋 `MAGICTOKEN`，供全文搜索命中定位；
  它同时是分支源（`branch_count = 1`）；
- **B 分支会话**：从 A 的第二问分支 → 只复制「第一问 / 第一答」，带 `branched_from_id`；
- **C 删除实验**：两轮问答，专供删除 / 撤销，避免删掉搜索与分支的依赖数据。

三条主线的真断言（都落在**真实后端状态**上，不是只看 DOM）：
1. 搜索：`.search-hit` 里的片段含命中词且有 `<mark>` 高亮；点命中 → 切到源会话并
   `.message-row.message-located`；`GET /api/conversations/<A>` 仍完好（搜索只读）；
2. 分支：源会话行有 `⑂1` 计数徽标、分支会话行有分支徽标；点徽标 → 面板列出
   「源 + 兄弟」且当前项高亮；点源项能跳过去；
3. 删除：确认框按位置分级给出「仅删这一条 / 连同 AI 回复整轮删除」；整轮删除后
   `GET /api/conversations/<C>` 恰好少两条；撤销后**逐字**复原（含 metadata 与顺序）。

用法：.venv\\Scripts\\python.exe verify\\batch1_smoke.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # 直接跑本脚本时也能 import naiba
PORT = int(os.environ.get("NAIBA_SMOKE_PORT", "8802"))
BASE = f"http://127.0.0.1:{PORT}"
ISOLATED_ROOT = Path(os.environ.get("NAIBA_SMOKE_ROOT", ROOT / "verify" / "batch1_tmp"))
DATA_DIR = ISOLATED_ROOT / "data"

PROVIDER = {"id": "smoke-b1", "name": "冒烟 API B1", "model": "smoke-default-b1"}
BOUND_MODEL = "smoke-b1-pro"

TOKEN = "MAGICTOKEN"
SRC_TITLE = "搜索冒烟：源会话"
DEL_TITLE = "删除冒烟：删除实验"
# 源会话两轮：分支点 = 第二问；MAGICTOKEN 只落在**第二答**里，
# 这样分支会话（只复制第一问/第一答）不会跟着命中，跨会话断言才分得开。
SRC_Q1 = "源会话第一问"
SRC_A1 = "源会话第一答：普通回答"
SRC_Q2 = "源会话第二问：这里是分支点"
SRC_A2 = f"源会话第二答：{TOKEN} 藏在这里"
DEL_Q1 = "删除第一问"
DEL_A1 = "删除第一答"
DEL_Q2 = "删除第二问"
DEL_A2 = "删除第二答"


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
    """播种三个会话，返回给浏览器脚本用的环境变量表。"""
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

    # 源会话：先建再播种（首条 user 消息会顶掉初始标题，这是 add_message 的既有行为，
    # 所以标题字面量在 create 时就定死，断言时按库里的当前标题比对）。
    src = storage.create_conversation(
        SRC_TITLE, model_key=f"online:{PROVIDER['id']}", model_name=BOUND_MODEL,
        workspace_dir=str(ISOLATED_ROOT / "workspace"),
    )
    storage.add_message(src["id"], "user", SRC_Q1)
    src_a1 = storage.add_message(src["id"], "assistant", SRC_A1)["id"]
    src_q2 = storage.add_message(src["id"], "user", SRC_Q2)["id"]
    src_a2 = storage.add_message(src["id"], "assistant", SRC_A2)["id"]

    # 分支会话：分支点 = 第二问 → 新会话只带「第一问 / 第一答」。
    branch = storage.branch_conversation(src["id"], src_q2)["conversation"]

    # 删除实验：独立会话，两轮，专供删除/撤销。
    deletion = storage.create_conversation(
        DEL_TITLE, model_key=f"online:{PROVIDER['id']}", model_name=BOUND_MODEL,
        workspace_dir=str(ISOLATED_ROOT / "workspace"),
    )
    del_q1 = storage.add_message(deletion["id"], "user", DEL_Q1)["id"]
    storage.add_message(deletion["id"], "assistant", DEL_A1)
    storage.add_message(deletion["id"], "user", DEL_Q2)
    storage.add_message(deletion["id"], "assistant", DEL_A2)

    return {
        "NAIBA_SMOKE_SRC_ID": src["id"],
        "NAIBA_SMOKE_BRANCH_ID": branch["id"],
        "NAIBA_SMOKE_DEL_ID": deletion["id"],
        "NAIBA_SMOKE_SRC_Q2_ID": src_q2,
        "NAIBA_SMOKE_SRC_A2_ID": src_a2,
        "NAIBA_SMOKE_SRC_A1_ID": src_a1,
        "NAIBA_SMOKE_DEL_Q1_ID": del_q1,
        # 标题取**库里的当前值**：首条 user 消息会顶掉 create_conversation 的初始标题
        # （add_message 的既有行为），徽标 tooltip / 分支链标题都以后者为准。
        "NAIBA_SMOKE_SRC_TITLE": storage.get_conversation(src["id"], include_messages=False)["title"],
        "NAIBA_SMOKE_DEL_TITLE": storage.get_conversation(deletion["id"], include_messages=False)["title"],
        "NAIBA_SMOKE_TOKEN": TOKEN,
        "NAIBA_SMOKE_SRC_A2": SRC_A2,
    }


def main() -> int:
    global ISOLATED_ROOT, DATA_DIR
    isolated = tempfile.TemporaryDirectory(prefix="batch1_smoke_", dir=ROOT / "verify")
    ISOLATED_ROOT = Path(isolated.name)
    DATA_DIR = ISOLATED_ROOT / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    code = 1
    server: subprocess.Popen | None = None
    log = (ROOT / "verify" / "batch1_smoke_server.log").open("w", encoding="utf-8")
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
        node_env["NODE_PATH"] = str(Path.home() / "node_modules")
        node_env["NAIBA_SMOKE_BASE"] = BASE
        node_env.update(env_snapshot)
        node = subprocess.run(  # noqa: S603 - 固定 argv
            ["node", str(ROOT / "verify" / "batch1_smoke.cjs")],
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
