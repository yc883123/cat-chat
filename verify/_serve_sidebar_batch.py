# -*- coding: utf-8 -*-
"""隔离源码实例：只为侧栏「批量管理」浏览器冒烟服务（独立 data_dir / config / 端口）。

与 ``_serve_tmp.py`` 同款，差别只在启动时直接用存储接口播种，让侧栏一打开就有
多工作区、超过 5 条的折叠组、跨工作区收藏、已归档项和一条带 queued 运行记录的会话：

- 归档 / 收藏 / 删除的判定全部走真实接口；
- ``未分组-忙碌`` 那条预置一条 queued 运行记录，用来验证「有任务在跑就跳过不删」；
- 每次启动重建隔离目录（上一次的删除/归档结果不能污染下一轮断言）。

用法：项目根\\.venv\\Scripts\\python.exe verify\\_serve_sidebar_batch.py
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PORT = int(os.environ.get("NAIBA_SIDEBAR_PORT", "8791"))
ISOLATED_ROOT = Path(
    os.environ.get("NAIBA_SIDEBAR_ROOT") or (ROOT / "verify" / "_tmp_sidebar_batch")
)


def isolated_paths():
    from naiba.paths import PathContext

    ISOLATED_ROOT.mkdir(parents=True, exist_ok=True)
    paths = PathContext.local(ISOLATED_ROOT, ISOLATED_ROOT / "config.json")
    paths.public_dir = ROOT / "public"
    return paths


def _seed(storage, workspace_a: str, workspace_b: str) -> None:
    created: dict[str, str] = {}

    def add(title: str, group: str = "", **extra) -> str:
        conversation = storage.create_conversation(
            title, workspace_dir=workspace_a if group else "", workspace_group=group, **extra
        )
        storage.add_message(conversation["id"], "user", f"{title} 的问题")
        storage.add_message(conversation["id"], "assistant", f"{title} 的回答")
        # 首条用户消息会把标题自动改成前 36 字（title_customized=0），播种后再写回固定标题，
        # 否则冒烟脚本按标题定位会话会全部落空。
        storage.update_conversation_settings(conversation["id"], title=title)
        created[title] = conversation["id"]
        return conversation["id"]

    # 两个工作区各超过 5 条：默认折叠态会出「展开其余 N 个会话」，够撑出滚动条。
    for index in range(1, 10):
        add(f"批量冒烟A-{index:02d}", "批量A")
    for index in range(1, 8):
        add(f"批量冒烟B-{index:02d}", "批量B")
    for index in range(1, 7):
        add(f"批量冒烟U-{index:02d}")

    # 收藏两条（跨工作区）+ 已归档两条：验证勾选去重与「取消归档」分支。
    for title in ("批量冒烟A-01", "批量冒烟U-01"):
        storage.set_conversation_favorite(created[title], True)
    for title in ("批量冒烟A-09", "批量冒烟B-07"):
        storage.set_conversation_archived(created[title], True)

    # 「正在回复」/「有任务在跑」的那条：批量删除必须跳过它。
    storage.create_chat_run(
        created["批量冒烟U-06"],
        "这条还在回复",
        [],
        {"id": "batch-smoke-agent", "name": "冒烟"},
        {"interaction_mode": "craft"},
        "craft",
    )


def main() -> None:
    from naiba.app import NaibaChatApp
    from naiba.config import ConfigStore
    from naiba.http import AppHTTPServer, RequestHandler

    if ISOLATED_ROOT.exists():
        shutil.rmtree(ISOLATED_ROOT, ignore_errors=True)
    paths = isolated_paths()
    data_dir = ISOLATED_ROOT / "data"
    workspace_a = ISOLATED_ROOT / "workspace-a"
    workspace_b = ISOLATED_ROOT / "workspace-b"
    for directory in (data_dir, workspace_a, workspace_b):
        directory.mkdir(parents=True, exist_ok=True)

    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({
        "host": "127.0.0.1",
        "port": PORT,
        "access_token": "",
        "data_dir": str(data_dir),
        "workspace_dir": str(workspace_a),
        "skills_dirs": [str(ROOT / "skills")],
        "mcp_servers": [],
        "workspaces": [
            {"name": "批量A", "dir": str(workspace_a)},
            {"name": "批量B", "dir": str(workspace_b)},
        ],
        # 空 key 的占位卡：冒烟脚本用 localStorage 关掉首启引导（维护说明 6.1）。
        "providers": [
            {"id": "smoke-te", "kind": "online", "name": "TE", "base_url": "https://example.invalid/v1",
             "api_key": "", "request_format": "responses", "model": "smoke-default"},
        ],
    })
    config.save()
    config.set_default_model_key("online:smoke-te")

    app = NaibaChatApp(paths=paths)
    _seed(app.storage, str(workspace_a), str(workspace_b))
    print(f"[sidebar-batch] port={PORT} data_dir={data_dir} public={paths.public_dir}", flush=True)
    server = AppHTTPServer(("127.0.0.1", PORT), RequestHandler, app)
    server.daemon_threads = True
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        app.stop()


if __name__ == "__main__":
    main()
