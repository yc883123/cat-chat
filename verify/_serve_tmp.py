# -*- coding: utf-8 -*-
"""临时：隔离源码实例（独立 data_dir / config / 端口），只为前端渲染验证，跑完即删。

为什么需要它：8765 上常驻的是**打包版 exe**，它用的是内置资源，改了仓库
`public/styles.css` 在浏览器里看不到；必须另起一个读仓库 `public/` 的源码实例。
隔离要求（沿用 verify/stop_cancel_smoke.py 的做法）：独立 config + 独立 data_dir
（避开 server.lock 与开发库）+ 独立端口。

用法：.venv\\Scripts\\python.exe verify\\_serve_tmp.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PORT = int(os.environ.get("NAIBA_TMP_PORT", "8799"))
# 允许调用方指定隔离目录：每次跑用一个全新的根，才能断言「全新安装的默认值」，
# 而不是被上一次运行写进 config.json 的值污染（前端检验踩过：首轮读到上一轮的 0）。
ISOLATED_ROOT = Path(
    os.environ.get("NAIBA_TMP_ROOT") or (ROOT / "verify" / "_tmp_render_probe")
)


def isolated_paths():
    from naiba.paths import PathContext

    ISOLATED_ROOT.mkdir(parents=True, exist_ok=True)
    paths = PathContext.local(ISOLATED_ROOT, ISOLATED_ROOT / "config.json")
    paths.public_dir = ROOT / "public"
    return paths


def main() -> None:
    from naiba.app import NaibaChatApp
    from naiba.config import ConfigStore
    from naiba.http import AppHTTPServer, RequestHandler

    paths = isolated_paths()
    (ISOLATED_ROOT / "data").mkdir(parents=True, exist_ok=True)
    (ISOLATED_ROOT / "workspace").mkdir(parents=True, exist_ok=True)

    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({
        "host": "127.0.0.1",
        "port": PORT,
        "access_token": "",
        "data_dir": str(ISOLATED_ROOT / "data"),
        "workspace_dir": str(ISOLATED_ROOT / "workspace"),
        "skills_dirs": [str(ROOT / "skills")],
        "mcp_servers": [],
        # 两张卡片：让设置面板里既有「当前」卡片，也有普通卡片可对照
        "providers": [
            {"id": "probe-te", "kind": "online", "name": "TE", "base_url": "https://example.invalid/v1",
             "api_key": "", "request_format": "responses", "model": "probe-default"},
            {"id": "probe-moda", "kind": "online", "name": "魔搭社区", "base_url": "https://example.invalid/v1",
             "api_key": "", "request_format": "openai", "model": "probe-qwen"},
        ],
    })
    config.save()
    config.set_default_model_key("online:probe-te")

    app = NaibaChatApp(paths=paths)
    print(f"[serve] port={PORT} data_dir={paths.data_dir} public={paths.public_dir}", flush=True)
    server = AppHTTPServer(("127.0.0.1", PORT), RequestHandler, app)
    server.daemon_threads = True
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        app.stop()


if __name__ == "__main__":
    main()
