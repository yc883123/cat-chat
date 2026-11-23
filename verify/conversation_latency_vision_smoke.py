"""隔离源码浏览器验收：会话增删延迟、模型目录等待与图片能力恢复。"""
from __future__ import annotations

from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.app import NaibaChatApp
from naiba.config import ConfigStore
from naiba.http import AppHTTPServer, RequestHandler
from naiba.paths import PathContext
from naiba.storage.store import ChatStorage


def main() -> None:
    output = Path(tempfile.mkdtemp(prefix="conversation_latency_vision_", dir=ROOT / "verify"))
    runtime = output / "runtime"
    runtime.mkdir()
    os.environ["NAIBA_ERROR_DUMP_DIR"] = str(output)
    captured: list[dict] = []
    probes: dict[str, int] = defaultdict(int)
    capture_lock = threading.Lock()
    models = ["fixture-current", "fixture-default", "deepseek-v4.1-flash",
              "fixture-alias-1280", "fixture-alias-375", "fixture-explicit", "fixture-text"]

    class ModelHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, status, payload):
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self.reply(200, {"data": [{"id": model} for model in models]})

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            model = str(payload.get("model") or "")
            is_probe = "只回复 OK" in json.dumps(payload.get("input") or [], ensure_ascii=False)
            with capture_lock:
                captured.append({"model": model, "probe": is_probe, "payload": payload})
                if is_probe:
                    probes[model] += 1
                probe_count = probes[model]
            if is_probe and probe_count == 1:
                self.reply(503, {"error": {"message": "fixture temporary unavailability"}})
                return
            text = "OK" if is_probe else "fixture image answer"
            response = {"id": "fixture-response", "object": "response", "status": "completed",
                        "output": [{"id": "fixture-message", "type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": text}]}],
                        "usage": {"input_tokens": 32, "output_tokens": 4, "total_tokens": 36}}
            if not payload.get("stream"):
                self.reply(200, response)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            events = [{"type": "response.output_text.delta", "delta": text},
                      {"type": "response.completed", "response": response}]
            for event in events:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode("utf-8"))
            self.wfile.flush()

    model_server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    model_thread = threading.Thread(target=model_server.serve_forever, daemon=True)
    model_thread.start()
    paths = PathContext.local(runtime, runtime / "config.json")
    paths.public_dir = ROOT / "public"
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    workspace = runtime / "workspace"
    workspace.mkdir()
    config = ConfigStore(paths.config_path, paths=paths)
    base_url = f"http://127.0.0.1:{model_server.server_port}/v1"
    providers = [{"id": provider_id, "kind": "online", "name": provider_id,
                  "base_url": base_url, "api_key": "fixture-local-only",
                  "request_format": "codex_responses", "model": "fixture-current"}
                 for provider_id in ("fixture-current", "fixture-default", "fixture-vision", "fixture-explicit")]
    providers[-1]["supports_images"] = True
    providers.append({**providers[-1], "id": "fixture-text", "name": "fixture-text", "supports_images": False})
    config.data.update({"host": "127.0.0.1", "port": 0, "access_token": "",
                        "data_dir": str(paths.data_dir), "workspace_dir": str(workspace),
                        "providers": providers, "skills_dirs": [], "mcp_servers": [],
                        "default_model_key": "online:fixture-current",
                        "new_conversation_model_key": "online:fixture-default",
                        "new_conversation_model_name": "fixture-default",
                        "auto_compaction": {"mode": "off"}})
    config.save()
    storage = ChatStorage(paths.data_dir / "chat.db")
    history = storage.create_conversation("历史性能夹具", model_key="online:fixture-current", model_name="fixture-current")
    fixtures = {"widths": {}, "history_id": history["id"], "trace_count": 10000}
    now = int(time.time() * 1000)
    with storage._connect() as db:
        db.executemany("INSERT INTO message_traces(trace_hash,data) VALUES (?,?)",
                       ((f"fixture-trace-{index}", "[]") for index in range(fixtures["trace_count"])))
        db.executemany("INSERT INTO messages(id,conversation_id,role,content,metadata,trace_hash,created_at) VALUES (?,?,?,?,?,?,?)",
                       ((f"fixture-message-{index}", history["id"], "assistant", "history", "{}",
                         f"fixture-trace-{index}", now + index) for index in range(fixtures["trace_count"])))
        db.execute("UPDATE conversations SET archived=1, updated_at=1 WHERE id=?", (history["id"],))
    for width in (1280, 375):
        row = {}
        for key, model_name in (("delete", "fixture-current"), ("known", "deepseek-v4.1-flash"),
                                ("unknown", f"fixture-alias-{width}"), ("explicit", "fixture-explicit"),
                                ("text", "fixture-text")):
            provider_id = model_name if key in {"explicit", "text"} else "fixture-vision"
            if key == "delete":
                provider_id = "fixture-current"
            item = storage.create_conversation(f"{key}-{width}", model_key=f"online:{provider_id}", model_name=model_name)
            row[key] = item["id"]
            if key in {"known", "unknown"}:
                storage.set_conversation_chat_supports_images(item["id"], False)
        fixtures["widths"][str(width)] = row
    # 重放旧版 false 记录的实际升级入口。
    storage.set_user_version(34)
    app = NaibaChatApp(paths=paths)
    app.super_agent_domain.runtime.shutdown()
    from PIL import Image
    image_path = output / "fixture.png"
    Image.new("RGB", (24, 24), (35, 180, 75)).save(image_path)
    fixtures["image_path"] = str(image_path)

    class Handler(RequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if urllib.parse.urlparse(self.path).path == "/_fixture/state":
                with capture_lock:
                    requests = list(captured)
                    counts = dict(probes)
                with app.storage._connect() as db:
                    trace_count = db.execute("SELECT COUNT(*) FROM message_traces WHERE trace_hash LIKE 'fixture-trace-%'").fetchone()[0]
                self._json({"requests": requests, "probes": counts,
                            "conversations": {conversation_id: app.storage.get_conversation(conversation_id, include_messages=False)
                                              for row in fixtures["widths"].values() for conversation_id in row.values()},
                            "trace_count": trace_count})
                return
            return super().do_GET()

    server = AppHTTPServer(("127.0.0.1", 0), Handler, app)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    fixtures["base"] = f"http://127.0.0.1:{server.server_port}"
    fixture_path = output / "fixture.json"
    fixture_path.write_text(json.dumps(fixtures, ensure_ascii=False), encoding="utf-8")
    env = {**os.environ, "NAIBA_CONVERSATION_FIXTURE": str(fixture_path), "NAIBA_SMOKE_OUTPUT": str(output)}
    try:
        print(f"Isolated fixture {fixture_path}", flush=True)
        subprocess.run(["node", str(ROOT / "verify/conversation_latency_vision_smoke.cjs")],
                       cwd=ROOT, env=env, check=True, timeout=240)
    finally:
        server.shutdown()
        server.server_close()
        model_server.shutdown()
        model_server.server_close()
        server_thread.join(timeout=5)
        model_thread.join(timeout=5)
        app.stop()
    print(f"Conversation browser checks passed. Artifacts {output}", flush=True)


if __name__ == "__main__":
    main()
