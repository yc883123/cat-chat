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
    # kv-a 预存完整启动配置：设置表单反显、保存持久化都按真实结构验证。
    providers[0]["kvmem_install_dir"] = "D:/kvmem-fixture"
    providers[0]["kvmem_launch_config"] = {"schema_version": 1, "parameters": {
        "Model": "D:/models/kv-a.gguf", "Backend": "ninfer", "Port": 18200, "Context": 262144}}
    config.data.update({"host": "127.0.0.1", "port": 0, "access_token": "",
                        "data_dir": str(runtime / "data"), "workspace_dir": str(runtime / "workspace"),
                        "providers": providers, "skills_dirs": [], "mcp_servers": []})
    config.save()
    app = NaibaChatApp(paths=paths)
    app.super_agent_domain.runtime.shutdown()
    store = app.comfyui.store

    # KVMem 替身状态机：start 后先 starting 两次状态查询再 ready，让前端轮询可见。
    kvmem_state = {"kv-a": "stopped", "kv-b": "ready"}
    kvmem_polls = {"kv-a": 0, "kv-b": 0}
    kvmem_only = bool(os.environ.get("NAIBA_KVMEM_ACTION_ONLY"))
    kvmem_guard = threading.RLock()
    kvmem_gates = {}
    kvmem_requests = []
    kvmem_details = {}
    initial_providers = json.loads(json.dumps(providers))  # 桌面轮保存后 375px 轮要还原。

    def kvmem_snapshot():
        with kvmem_guard:
            return {"states": dict(kvmem_state), "requests": list(kvmem_requests),
                    "entered": [{"provider_id": key[0], "target": key[1]} for key, gate in kvmem_gates.items() if gate["entered"]]}

    def kvmem_gate(provider_id, target, details=None):
        with kvmem_guard:
            kvmem_requests.append({"provider_id": provider_id, "target": target, **(details or {})})
            gate = kvmem_gates.get((provider_id, target))
            hold = gate and not gate["entered"]
            if hold:
                gate["entered"] = True
        if hold and not gate["release"].wait(45):
            raise TimeoutError("KVMem浏览器验收未释放门控请求")

    def kvmem_control(body):
        key = (body.get("provider_id", "kv-a"), body.get("target", "status"))
        with kvmem_guard:
            if body["action"] == "hold":
                kvmem_gates[key] = {"entered": False, "release": threading.Event()}
            elif body["action"] == "release":
                kvmem_gates[key]["release"].set()
            elif body["action"] == "set":
                kvmem_state[key[0]] = body["status"]
                kvmem_polls[key[0]] = 0
                kvmem_details[key[0]] = {name: body[name] for name in ("raw_status", "error", "http_status") if name in body}
            else:
                raise ValueError("未知KVMem验收动作")
        return kvmem_snapshot()

    def reset():
        # app 在初始化时自建 ConfigStore；供应商还原必须改它引用的那份内存数据。
        app.config.data["providers"] = json.loads(json.dumps(initial_providers))
        app.config.save()
        with kvmem_guard:
            for gate in kvmem_gates.values():
                gate["release"].set()
            kvmem_gates.clear()
            kvmem_requests.clear()
            kvmem_details.clear()
            kvmem_state.update({"kv-a": "stopped", "kv-b": "ready"})
            kvmem_polls.update({"kv-a": 0, "kv-b": 0})
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
            elif route == "/_fixture/kvmem":
                self._json(kvmem_snapshot())
            elif route == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            elif route.endswith("/kvmem/status"):
                key = route.split("/")[-3]
                kvmem_now = kvmem_state.get(key, "stopped")
                if kvmem_now == "starting" and not kvmem_only:
                    kvmem_polls[key] += 1
                    if kvmem_polls[key] >= 2:
                        kvmem_state[key] = "ready"
                        kvmem_now = "ready"
                details = {"status": kvmem_now, **kvmem_details.get(key, {})}
                kvmem_gate(key, "status", details)
                code = details.pop("http_status", 200)
                self._json({"model": key + "-model", "backend": "ninfer", "actual_port": 18200, **details}, code)
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
            elif route == "/_fixture/kvmem":
                self._json(kvmem_control(self._read_json()))
            elif route == "/api/providers/models":
                self._read_json()
                self._json({"models": ["kv-a-model", "kv-b-model"]})
            elif route.endswith("/kvmem/start"):
                key = route.split("/")[-3]
                kvmem_state[key] = "starting"
                kvmem_polls[key] = 0
                kvmem_gate(key, "start")
                self._json({"ok": True, "accepted": True, "status": "starting",
                            "summary": {"model": key + "-model", "backend": "ninfer", "config_source": "provider"}}, 202)
            elif route.endswith("/kvmem/stop"):
                key = route.split("/")[-3]
                kvmem_state[key] = "stopped"
                kvmem_gate(key, "stop")
                self._json({"ok": True, "status": "stopped", "accepted": True,
                            "message": "已停止 KVMem 托管模型并释放其显存"}, 202)
            elif route == "/api/kvmem/capture":
                self._read_json()
                self._json({"ok": True, "source": "active",
                            "parameters": {"Model": "D:/models/captured.gguf", "Backend": "ninfer",
                                           "Port": 18200, "Context": 131072, "FastPrefill": True,
                                           "VisionResidency": "resident"}})
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
        for gate in kvmem_gates.values():
            gate["release"].set()
        (runtime / "kvmem-requests.json").write_text(json.dumps(kvmem_snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.stop()


if __name__ == "__main__":
    main()
