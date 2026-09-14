# -*- coding: utf-8 -*-
"""隔离源码实例（真实 API）——专供「视频抽帧前端反推」端到端验证。

与 _serve_tmp.py 的差别只有一处：供应商换成**真实可用的在线多模态**（默认 mimo /
mimo-v2.5：多模态 + 工具调用 + 无限流，从仓库 config.json 取 base_url + api_key，
脚本本身不落任何密钥）。
其余隔离要求完全一致：独立 config + 独立 data_dir + 独立端口，避开常驻 exe 的
server.lock 与开发库。

为什么必须隔离：8765 上常驻打包版 exe，读内置资源；本次要验的是**仓库源码 + 仓库
public/** 的真实前端行为（见 _serve_tmp.py 顶部说明）。

环境变量：
  NAIBA_TMP_PORT    端口（默认 8797）
  NAIBA_TMP_ROOT    隔离根（默认 verify/_tmp_video_e2e）
  NAIBA_E2E_PROVIDER 供应商名（默认 mimo）
  NAIBA_E2E_MODEL    模型 id（默认 mimo-v2.5，多模态 + 工具调用）

用法：.venv\\Scripts\\python.exe verify\\_serve_video_e2e.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PORT = int(os.environ.get("NAIBA_TMP_PORT", "8797"))
# 必须是**绝对路径**：相对路径会被 PathContext 相对 config 所在目录再拼一次，
# data_dir 变成 `verify/_tmp_video_e2e/verify/_tmp_video_e2e/data`（实测踩过）。
_RAW_ROOT = os.environ.get("NAIBA_TMP_ROOT")
ISOLATED_ROOT = (
    (_RAW_ROOT if Path(_RAW_ROOT).is_absolute() else ROOT / _RAW_ROOT).resolve()
    if _RAW_ROOT
    else (ROOT / "verify" / "_tmp_video_e2e")
)
PROVIDER_NAME = os.environ.get("NAIBA_E2E_PROVIDER", "mimo")
MODEL_ID = os.environ.get("NAIBA_E2E_MODEL", "mimo-v2.5")


def pick_real_provider() -> dict:
    """从仓库 config.json 里取一个真实供应商，复制其 base_url / api_key。"""
    raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    for item in raw.get("providers", []):
        if str(item.get("name") or "") == PROVIDER_NAME and item.get("api_key"):
            return {
                "id": "e2e-" + str(item.get("id") or "prov"),
                "kind": "online",
                "name": PROVIDER_NAME,
                "base_url": item.get("base_url"),
                "api_key": item.get("api_key"),
                "request_format": item.get("request_format") or "openai_chat",
                "model": MODEL_ID,
            }
    raise SystemExit(f"仓库 config.json 里找不到可用供应商「{PROVIDER_NAME}」")


def main() -> None:
    from naiba.paths import PathContext
    from naiba.app import NaibaChatApp
    from naiba.config import ConfigStore
    from naiba.http import AppHTTPServer, RequestHandler

    ISOLATED_ROOT.mkdir(parents=True, exist_ok=True)
    paths = PathContext.local(ISOLATED_ROOT, ISOLATED_ROOT / "config.json")
    paths.public_dir = ROOT / "public"
    (ISOLATED_ROOT / "data").mkdir(parents=True, exist_ok=True)
    (ISOLATED_ROOT / "workspace").mkdir(parents=True, exist_ok=True)

    provider = pick_real_provider()
    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({
        "host": "127.0.0.1",
        "port": PORT,
        "access_token": "",
        "data_dir": str(ISOLATED_ROOT / "data"),
        "workspace_dir": str(ISOLATED_ROOT / "workspace"),
        "skills_dirs": [str(ROOT / "skills")],
        "mcp_servers": [],
        "providers": [provider],
    })
    config.save()
    config.set_default_model_key("online:" + provider["id"])

    app = NaibaChatApp(paths=paths)
    print(
        f"[serve-video-e2e] port={PORT} data_dir={paths.data_dir} "
        f"provider={provider['name']} model={MODEL_ID} "
        f"public={paths.public_dir}",
        flush=True,
    )
    server = AppHTTPServer(("127.0.0.1", PORT), RequestHandler, app)
    server.daemon_threads = True
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        app.stop()


if __name__ == "__main__":
    main()
