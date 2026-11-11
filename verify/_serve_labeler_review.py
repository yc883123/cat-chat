"""贴签匠回归用隔离源码服务；模型只提供目录，不允许真实生成。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    from naiba.app import NaibaChatApp
    from naiba.config import ConfigStore
    from naiba.http import AppHTTPServer, RequestHandler
    from naiba.paths import PathContext

    port = int(os.environ.get("NAIBA_TMP_PORT", "8807"))
    isolated = Path(tempfile.mkdtemp(prefix="labeler-review-"))
    paths = PathContext.local(isolated, isolated / "config.json")
    paths.public_dir = ROOT / "public"
    (isolated / "data").mkdir()
    (isolated / "workspace").mkdir()
    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({
        "host": "127.0.0.1", "port": port, "access_token": "",
        "data_dir": str(isolated / "data"),
        "workspace_dir": str(isolated / "workspace"),
        "skills_dirs": [], "mcp_servers": [],
        "providers": [
            {"id": f"review-{name}", "kind": "online", "name": f"测试 {name}",
             "base_url": "http://127.0.0.1:1/v1", "api_key": "test-only",
             "request_format": "openai", "model": f"{name}-model"}
            for name in ("A", "B", "C", "D")
        ],
    })
    config.save()
    config.set_default_model_key("online:review-A")
    app = NaibaChatApp(paths=paths)

    def deny_generation(*_args, **_kwargs):
        raise RuntimeError("浏览器回归实例禁止真实模型调用")

    app.models.complete = deny_generation
    app.models.list_online_models = lambda profile: [
        {"id": profile["model"], "name": profile["model"]},
    ]
    server = AppHTTPServer(("127.0.0.1", port), RequestHandler, app)
    server.daemon_threads = True
    print(f"[labeler-review] port={port} isolated={isolated}", flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        app.stop()


if __name__ == "__main__":
    main()
