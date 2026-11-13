"""清除与 KVMem 控件的源码浏览器验收；所有任务、配置和状态均隔离。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from naiba.app import NaibaChatApp
from naiba.config import ConfigStore
from naiba.http import AppHTTPServer, RequestHandler
from naiba.paths import PathContext


def main():
    runtime_root = Path(os.environ.get("NAIBA_SMOKE_RUNTIME_DIR") or ROOT / "verify")
    runtime = Path(tempfile.mkdtemp(prefix="clear_kvmem_", dir=runtime_root))
    paths = PathContext.local(runtime, runtime / "config.json")
    paths.public_dir = ROOT / "public"
    config = ConfigStore(paths.config_path, paths=paths)
    providers = [{"id": key, "kind": "local", "name": key, "model": key + "-model",
                  "service_manager": "kvmem", "service_manager_url": f"http://127.0.0.1:{19000 + index}",
                  "base_url": f"http://127.0.0.1:{19100 + index}/v1", "request_format": "openai"}
                 for index, key in enumerate(("kv-a", "kv-b"))]
    config.data.update({"host": "127.0.0.1", "port": 0, "access_token": "",
                        "data_dir": str(runtime / "data"), "workspace_dir": str(runtime / "workspace"),
                        "providers": providers, "skills_dirs": [], "mcp_servers": []})
    config.save()
    app = NaibaChatApp(paths=paths)
    app.super_agent_domain.runtime.shutdown()
    store = app.comfyui.store

    def reset():
        for task in list(store.iter_tasks()):
            store.delete_task(task["id"])
        now = int(time.time() * 1000)
        for index in range(263):
            key = f"cf-clear-{index:03d}"
            store._write_json(store.task_record_path(key), {
                "id": key, "module": "comfyui", "status": "running" if index == 260 else "completed",
                "created_at": now - index * 1000, "updated_at": now,
                "input": {"batch_id": "cfb-clear" if index > 260 else "", "workflow_name": f"单次验收 {index}"},
                "result": {"outputs": []}, "checkpoint": {}, "progress": {"done": 1, "total": 1},
            })
        return state()

    def state():
        rows = list(store.iter_tasks())
        return {"single": sum(not row.get("input", {}).get("batch_id") for row in rows),
                "batch": sum(bool(row.get("input", {}).get("batch_id")) for row in rows),
                "ids": [row["id"] for row in rows]}

    reset()

    class Handler(RequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            route = urlparse(self.path).path
            if route == "/_fixture/state":
                self._json(state())
            elif route == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            elif route.endswith("/kvmem/status"):
                key = route.split("/")[-3]
                self._json({"status": "ready" if key == "kv-b" else "stopped", "model": key + "-model"})
            elif route == "/api/comfyui/health":
                self._json({"ok": True, "url": "http://127.0.0.1:8188", "devices": []})
            elif route == "/api/comfyui/models":
                self._json({"models": [], "loras": []})
            else:
                super().do_GET()

        def do_POST(self):
            route = urlparse(self.path).path
            if route == "/_fixture/reset":
                self._read_json()
                self._json(reset())
            elif route == "/api/providers/models":
                self._read_json()
                self._json({"models": ["kv-a-model", "kv-b-model"]})
            else:
                super().do_POST()

    server = AppHTTPServer(("127.0.0.1", 0), Handler, app)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {**os.environ, "NAIBA_SMOKE_BASE": f"http://127.0.0.1:{server.server_port}",
           "NAIBA_SMOKE_OUTPUT": str(runtime)}
    print(f"隔离清除/KVMem服务：{env['NAIBA_SMOKE_BASE']}，产物：{runtime}", flush=True)
    try:
        subprocess.run(["node", str(ROOT / "verify" / "clear_kvmem_smoke.cjs")], cwd=ROOT,
                       env=env, check=True, timeout=300)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.stop()


if __name__ == "__main__":
    main()
