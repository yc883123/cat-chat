"""ImageContainer browser fixture with real Cat Chat HTTP uploads and a local ComfyUI stub.

Run with the repository venv Python. Default runs comfyui_image_container_smoke.cjs;
--serve-only leaves the isolated service available for interactive browser checks;
--self-check checks isolation and the stub without starting a browser.
All configuration, databases and uploads live in a new temporary directory.
"""
from __future__ import annotations

import argparse
from email.parser import BytesParser
from email.policy import default
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from PIL import Image, ImageDraw  # noqa: E402

WORKFLOW_ID = "cw-image-container-smoke"
WORKFLOW_NAME = "ImageContainer 图片选择验收"
PROMPT = {
    "1": {"class_type": "ImageContainer", "_meta": {"title": "参考图容器"},
          "inputs": {"selected_json": "[]", "pool_json": "[]"}},
    "2": {"class_type": "SaveImage", "inputs": {
        "images": ["1", 0], "filename_prefix": "image_container_smoke"}},
}


def fixture_png(color: str, label: str) -> bytes:
    image = Image.new("RGB", (320, 200), color)
    ImageDraw.Draw(image).text((20, 85), label, fill="white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class ComfyStub:
    """No models, GPUs or video: submitted prompts immediately return a small PNG."""

    def __init__(self):
        self.lock = threading.Lock()
        self.uploads: list[dict] = []
        self.prompts: list[dict] = []
        self.output = fixture_png("#526f9d", "ISOLATED OUTPUT")

    def state(self) -> dict:
        with self.lock:
            return {"uploads": list(self.uploads), "prompts": list(self.prompts)}

    def handler(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send_json(self, payload, status=200):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
                if path == "/system_stats":
                    return self.send_json({"system": {"comfyui_version": "image-container-fixture"}, "devices": []})
                if path == "/queue":
                    return self.send_json({"queue_running": [], "queue_pending": []})
                if path.startswith("/object_info/"):
                    name = path.rsplit("/", 1)[-1]
                    required = {
                        "ImageContainer": {"selected_json": ["STRING", {"multiline": True}],
                                           "pool_json": ["STRING", {"multiline": True}]},
                        "SaveImage": {"images": ["IMAGE"], "filename_prefix": ["STRING"]},
                    }.get(name, {})
                    return self.send_json({name: {"input": {"required": required}}})
                if path.startswith("/history/"):
                    prompt_id = path.rsplit("/", 1)[-1]
                    exists = any(item["prompt_id"] == prompt_id for item in fixture.state()["prompts"])
                    return self.send_json({prompt_id: {"status": {"status_str": "success", "completed": True},
                        "outputs": {"2": {"images": [{"filename": "fixture.png", "subfolder": "", "type": "output"}]}}}}
                        if exists else {})
                if path == "/view":
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(fixture.output)))
                    self.end_headers()
                    self.wfile.write(fixture.output)
                    return
                self.send_json({"error": "Unexpected fixture GET", "path": path}, 404)

            def do_POST(self):
                path = urllib.parse.urlsplit(self.path).path
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if path == "/upload/image":
                    message = BytesParser(policy=default).parsebytes(
                        f'Content-Type: {self.headers["Content-Type"]}\r\nMIME-Version: 1.0\r\n\r\n'.encode() + body)
                    parts = [part for part in message.iter_parts() if part.get_param("name", header="content-disposition") == "image"]
                    if len(parts) != 1:
                        return self.send_json({"error": "Expected one image file"}, 400)
                    content = parts[0].get_payload(decode=True)
                    with Image.open(io.BytesIO(content)) as image:
                        image.verify()
                    with fixture.lock:
                        name = f"uploaded-{len(fixture.uploads) + 1}.png"
                        fixture.uploads.append({"name": name, "source_name": parts[0].get_filename(),
                            "subfolder": "smoke-input", "type": "input", "sha256": hashlib.sha256(content).hexdigest()})
                    return self.send_json({"name": name, "subfolder": "smoke-input", "type": "input"})
                if path == "/prompt":
                    payload = json.loads(body)
                    with fixture.lock:
                        prompt_id = f"image-container-{len(fixture.prompts) + 1}"
                        fixture.prompts.append({"prompt_id": prompt_id, "prompt": payload["prompt"]})
                    return self.send_json({"prompt_id": prompt_id, "number": 1, "node_errors": {}})
                if path in {"/free", "/interrupt", "/queue"}:
                    return self.send_json({})
                self.send_json({"error": "Unexpected fixture POST", "path": path}, 404)

        return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serve-only", action="store_true")
    mode.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    from naiba.app import NaibaChatApp
    from naiba.comfyui import PARAMS_SCHEMA_VERSION
    from naiba.comfyui.params import scan_params
    from naiba.comfyui.store import ComfyUIStore
    from naiba.config import ConfigStore
    from naiba.http import AppHTTPServer, RequestHandler
    from naiba.paths import PathContext

    output = Path(tempfile.mkdtemp(prefix="comfyui_image_container_", dir=REPO / "verify"))
    with tempfile.TemporaryDirectory(prefix="cat-chat-image-container-") as temp:
        runtime = Path(temp).resolve()
        paths = PathContext.local(runtime, runtime / "config.json")
        assert paths.app_dir == paths.exe_dir, "Source migration must remain disabled"
        paths.public_dir = REPO / "public"
        paths.resource_dir = REPO
        stub = ComfyStub()
        comfy_server = ThreadingHTTPServer(("127.0.0.1", 0), stub.handler())
        comfy_thread = threading.Thread(target=comfy_server.serve_forever, daemon=True)
        comfy_thread.start()
        config = ConfigStore(paths.config_path, paths=paths)
        config.data.update({"host": "127.0.0.1", "port": 0, "access_token": "",
            "data_dir": str(runtime / "data"), "workspace_dir": str(runtime / "workspace"),
            "providers": [], "skills_dirs": [], "mcp_servers": [],
            "comfyui_url": f"http://127.0.0.1:{comfy_server.server_port}"})
        config.save()
        app = None
        server = None
        try:
            app = NaibaChatApp(paths=paths)
            app.super_agent_domain.runtime.shutdown()
            app.comfyui.store.save_workflow({"id": WORKFLOW_ID, "name": WORKFLOW_NAME,
                "prompt": PROMPT, "workflow_fingerprint": ComfyUIStore.payload_hash(PROMPT),
                "params": scan_params(PROMPT), "params_version": PARAMS_SCHEMA_VERSION,
                "visible_params": [{"node_id": "1", "field_name": "selected_json"}],
                "created_at": 1, "updated_at": 1})

            class Handler(RequestHandler):
                def log_message(self, *_args):
                    pass

                def do_GET(self):
                    if urllib.parse.urlsplit(self.path).path == "/_fixture/image-container":
                        self._json({**stub.state(), "isolated": True, "workflow_id": WORKFLOW_ID})
                    else:
                        super().do_GET()

            server = AppHTTPServer(("127.0.0.1", 0), Handler, app)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            assets = runtime / "fixture-images"
            assets.mkdir()
            images = []
            for index, color in enumerate(["#a24040", "#368069", "#6555a0"], 1):
                target = assets / f"smoke-{index}.png"
                target.write_bytes(fixture_png(color, f"IMAGE {index}"))
                images.append(str(target))
            fixture = {"base": f"http://127.0.0.1:{server.server_port}", "workflow_id": WORKFLOW_ID,
                "workflow_name": WORKFLOW_NAME, "slot": "1|selected_json", "images": images,
                "runtime": str(runtime), "output": str(output)}
            manifest = output / "fixture.json"
            manifest.write_text(json.dumps(fixture, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"ImageContainer isolated fixture: {manifest}", flush=True)
            print(f"NAIBA_SMOKE_BASE={fixture['base']}", flush=True)
            if args.self_check:
                for endpoint in ["/api/health", "/api/comfyui/health", f"/api/comfyui/workflows/{WORKFLOW_ID}",
                                 "/_fixture/image-container"]:
                    with urllib.request.urlopen(fixture["base"] + endpoint, timeout=10) as response:
                        assert response.status == 200
                        payload = json.load(response)
                        if endpoint.endswith(WORKFLOW_ID):
                            assert payload["workflow"]["prompt"]["1"]["inputs"]["selected_json"] == "[]"
                boundary = "ImageContainerSelfCheckBoundary"
                body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="smoke.png"\r\n'
                        'Content-Type: image/png\r\n\r\n').encode() + Path(images[0]).read_bytes() + f"\r\n--{boundary}--\r\n".encode()
                request = urllib.request.Request(fixture["base"] + "/api/comfyui/media", data=body,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
                with urllib.request.urlopen(request, timeout=10) as response:
                    picked = json.load(response)
                assert Path(picked["path"]).is_relative_to(runtime)
                assert Path(picked["path"]).is_file() and Path(picked["thumb_path"]).is_file()
                assert not stub.state()["prompts"]
                print("PASS isolated root, exact ImageContainer workflow, local ComfyUI health and real Cat Chat image upload", flush=True)
                return 0
            if args.serve_only:
                print("Service ready; Ctrl+C to stop and remove the isolated runtime.", flush=True)
                threading.Event().wait()
                return 0
            env = {**os.environ, "NAIBA_IMAGE_CONTAINER_FIXTURE": str(manifest),
                   "NAIBA_SMOKE_BASE": fixture["base"], "NODE_PATH": str(Path.home() / "node_modules")}
            return subprocess.run(["node", str(REPO / "verify" / "comfyui_image_container_smoke.cjs")],
                                  cwd=REPO, env=env, check=False, timeout=600).returncode
        except KeyboardInterrupt:
            return 0
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            if app is not None:
                app.stop()
            comfy_server.shutdown()
            comfy_server.server_close()
            (output / "stub-state.json").write_text(json.dumps(stub.state(), ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Stopped isolated services; artifacts: {output}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
