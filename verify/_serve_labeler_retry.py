"""贴签匠同批重试的隔离浏览器验收；仅模型调用使用本地替身。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    from PIL import Image
    from naiba.app import NaibaChatApp
    from naiba.config import ConfigStore
    from naiba.http import AppHTTPServer, RequestHandler
    from naiba.paths import PathContext

    runtime = Path(tempfile.mkdtemp(prefix="labeler_retry_", dir=ROOT / "verify"))
    paths = PathContext.local(runtime, runtime / "config.json")
    paths.public_dir = ROOT / "public"
    skill = runtime / "skills" / "retry-caption"
    skill.mkdir(parents=True)
    skill.joinpath("SKILL.md").write_text(
        "---\nname: retry-caption\ndescription: 同批重试浏览器验收\n---\n"
        "RETRY_SKILL_FIXTURE: describe the image as JSON tags and description.\n",
        encoding="utf-8",
    )
    for width in (1440, 375):
        for mode in ("serial", "concurrent"):
            folder = runtime / "inputs" / f"{mode}-{width}"
            folder.mkdir(parents=True)
            for index in range(1, 101):
                Image.new("RGB", (8, 8), (index, width % 255, 90)).save(folder / f"{index:03}.png")

    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({
        "host": "127.0.0.1", "port": 0, "access_token": "",
        "data_dir": str(runtime / "data"), "workspace_dir": str(runtime / "workspace"),
        "skills_dirs": [], "mcp_servers": [],
        "providers": [{"id": "retry-fixture", "kind": "online", "name": "重试验收模型",
                       "base_url": "http://127.0.0.1:1/v1", "api_key": "fixture-only",
                       "request_format": "openai", "model": "retry-model"}],
    })
    config.save()
    config.set_default_model_key("online:retry-fixture")
    app = NaibaChatApp(paths=paths)
    app.super_agent_domain.runtime.shutdown()
    calls: dict[str, dict] = {}
    guard = threading.RLock()
    release_retry = threading.Event()
    retry_entered = threading.Event()

    def complete(_profile, messages, options, _status=None):
        item_id = str(options.get("item_id") or "")
        name = str(options.get("item_name") or "")
        if not item_id or "RETRY_SKILL_FIXTURE" not in str(messages):
            raise RuntimeError("浏览器隔离实例拒绝非贴签匠 Skill 模型调用")
        with guard:
            row = calls.setdefault(item_id, {"name": name, "count": 0})
            row["count"] += 1
            count = row["count"]
        app.models.last_usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        app.models.last_finish_reason = "stop"
        if name == "100.png":
            if count == 1:
                raise RuntimeError("浏览器验收：本图首次打标失败")
            retry_entered.set()
            if not release_retry.wait(30):
                raise TimeoutError("浏览器验收未释放失败图片重试")
        return json.dumps({"tags": ["retry-ok"], "description": f"完整标注 {name}"}, ensure_ascii=False)

    app.models.complete = complete
    app.models.list_online_models = lambda _profile: [{"id": "retry-model", "name": "retry-model"}]

    def snapshot():
        with guard:
            return {"calls": dict(calls), "retry_entered": retry_entered.is_set()}

    class Handler(RequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            route = urlparse(self.path).path
            if route == "/_fixture/state":
                self._json(snapshot())
            elif route == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            else:
                super().do_GET()

        def do_POST(self):
            route = urlparse(self.path).path
            if route == "/_fixture/prepare":
                self._read_json()
                retry_entered.clear()
                release_retry.clear()
                self._json(snapshot())
            elif route == "/_fixture/release":
                self._read_json()
                release_retry.set()
                self._json(snapshot())
            else:
                super().do_POST()

    server = AppHTTPServer(("127.0.0.1", 0), Handler, app)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {**os.environ, "NAIBA_SMOKE_BASE": f"http://127.0.0.1:{server.server_port}",
           "NAIBA_SMOKE_OUTPUT": str(runtime), "NAIBA_SMOKE_INPUTS": str(runtime / "inputs")}
    env.setdefault("NODE_PATH", str(Path.home() / "node_modules"))
    print(f"贴签匠隔离重试验收：{env['NAIBA_SMOKE_BASE']}；产物：{runtime}", flush=True)
    try:
        for script in ("labeler_retry_smoke.cjs", "browser_smoke.cjs"):
            subprocess.run(["node", str(ROOT / "verify" / script)], cwd=ROOT, env=env,
                           check=True, timeout=360)
    finally:
        release_retry.set()
        runtime.joinpath("model_calls.json").write_text(
            json.dumps(snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.stop()


if __name__ == "__main__":
    main()
