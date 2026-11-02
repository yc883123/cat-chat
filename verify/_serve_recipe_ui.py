# -*- coding: utf-8 -*-
"""LOCAL-ONLY recipe UI fixture with real app, Studio and ComfyUI runners.

Run with the project venv: --root NEW_EMPTY_DIRECTORY --port 0. The manifest
fixture.json contains app_url/control_url, token, managed images and draft IDs.
Only external model/Comfy HTTP services are substituted. No browser is launched.
SaveVideo graphs return a tiny locally encoded synthetic H.264/AAC MP4 when
ffmpeg is available (default --media-mode auto; video requires it). Explicit
--media-mode png retains PNG-only fallback. No downloaded assets or inference:
the simulated one-second clip tests collection/playback, NOT real H3 generation.

Controls (X-Fixture-Token required): GET /__fixture/status,
POST /__fixture/control with boolean fail_reverse/fail_generate/fail_download,
hold_reverse/hold_generate (hold aliases hold_generate), or *_fail_at request
ordinal lists; POST /__fixture/reset-stats explicitly resets counters only.
POST /__fixture/shutdown stops the app. Clearing flags never resets counters.
"""
from __future__ import annotations

import argparse
import copy
import io
import ipaddress
import json
import re
import secrets
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core.fsutil import atomic_write_json

MODEL_ID = "recipe-fixture-vision"
MODEL_KEY = "online:recipe-fixture"


def loopback_host(host):
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:
        return str(host).lower() == "localhost"


def install_offline_guard():
    """Permanent process audit hook: install before constructing the real app.

    Guard actual connect addresses (including resolved DNS/proxy targets), UDP
    sendto and non-loopback DNS lookups. Tests use a dedicated child process.
    """
    blocked = []

    def audit(event, args):
        address = None
        if event in {"socket.connect", "socket.bind"}:
            address = args[1]
        elif event == "socket.sendto":
            address = args[-1]
        elif event == "socket.getaddrinfo":
            address = (args[0], args[1])
        if isinstance(address, tuple) and not loopback_host(address[0]):
            blocked.append({"event": event, "host": str(address[0])})
            raise PermissionError("Recipe fixture forbids non-loopback networking")

    sys.addaudithook(audit)
    return blocked


def png_bytes(index=0):
    from PIL import Image, ImageDraw

    buffer = io.BytesIO()
    image = Image.new("RGB", (256, 192), (35 + index * 25 % 180, 100, 155))
    draw = ImageDraw.Draw(image)
    draw.rectangle((48, 40, 208, 152), fill=(230, 185, 70), outline="white", width=4)
    draw.text((65, 85), f"LOCAL FIXTURE {index + 1}", fill="black")
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def simulated_media(root_dir, mode="auto"):
    """Generate our own bounded local clip; never download or execute an asset."""
    if mode not in {"auto", "video", "png"}:
        raise ValueError("media mode must be auto, video or png")
    encoder = shutil.which("ffmpeg")
    metadata = {"simulated": True, "source": "locally generated synthetic test pattern and tone",
                "duration_seconds": 1.0, "width": 160, "height": 120,
                "video_codec": "h264", "audio_codec": "aac", "fps": 2}
    if mode == "png" or not encoder:
        if mode == "video":
            raise RuntimeError("--media-mode video requires ffmpeg installed on PATH; no download attempted")
        return b"", {"type": "image", "mime": "image/png", "simulated": True,
                     "reason": "explicit PNG mode" if mode == "png" else "ffmpeg not found on PATH"}
    media_dir = root_dir / "fixture-media"
    media_dir.mkdir()
    clip = media_dir / "simulated-local-clip.mp4"
    command = [encoder, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=2:duration=1",
               "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=1",
               "-t", "1", "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "32k", "-movflags", "+faststart", str(clip)]
    result = subprocess.run(command, cwd=str(root_dir), capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Local synthetic MP4 encoding failed: " + result.stderr.decode("utf-8", "replace")[-1000:])
    decoded = subprocess.run([encoder, "-nostdin", "-v", "error", "-i", str(clip),
                              "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"],
                             cwd=str(root_dir), capture_output=True, timeout=15)
    if decoded.returncode:
        raise RuntimeError("Local synthetic MP4 decode verification failed")
    return clip.read_bytes(), {"type": "video", "mime": "video/mp4", **metadata, "decode_verified": True}


def deterministic_prompt(mode="i2v", image_count=1):
    """Canonical catalog-compatible 3/6-section output, no model/tool emulation."""
    if mode == "r2v":
        definitions = "\n".join(
            f"<Subject {i}> is the golden rectangular object and blue background from <Picture {i}>."
            for i in range(1, image_count + 1))
        retention = "\n".join(
            f"<Subject {i}> in [Shot 1]: fully_preserved - Maintain the same visible shape and colors."
            for i in range(1, image_count + 1))
        subjects = ", ".join(f"<Subject {i}>" for i in range(1, image_count + 1))
        return (f"subject_definitions: {definitions}\n\n"
                f"summary: [reference generation] Present {subjects} in a calm continuous composition.\n\n"
                f"retention_analysis: {retention}\n\n"
                f"detailed_description: A clean graphic image retains its blue and gold colors. "
                f"[Shot 1] {subjects} remain clearly visible as the camera slowly moves closer, "
                "then settles without a cut. No speech occurs.\n\n"
                "overall_soundscape: A gentle room ambience continues with a soft camera movement sound.\n\n"
                "non_diegetic_music: N/A")
    return ("For the target video, at 0.00 seconds into the target video, "
            "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
            "integrated_multimodal_description: [Shot 1] A clean graphic image shows a golden "
            "rectangle against a blue background, preserving the first frame composition and colors. "
            "The camera gently pushes closer at low speed, then settles without a cut. No speech occurs.\n\n"
            "overall_soundscape: A gentle room ambience continues with a soft camera movement sound.\n\n"
            "non_diegetic_music: N/A")


class FixtureState:
    def __init__(self, blocked=None):
        self.lock = threading.RLock()
        self.token = secrets.token_hex(24)
        self.app = None
        self.server = None
        self.closed = threading.Event()
        self.changed = threading.Condition(self.lock)
        self.blocked_connections = blocked if blocked is not None else []
        self.flags = {key: False for key in (
            "fail_reverse", "fail_generate", "fail_download", "hold_reverse", "hold_generate")}
        self.flags.update({key: [] for key in ("reverse_fail_at", "generate_fail_at", "download_fail_at")})
        self.counts = {key: 0 for key in ("model", "prompt", "upload", "history", "view", "interrupt", "queue_delete")}
        self.requests = {key: [] for key in ("model", "prompt", "upload")}
        self.prompts = {}
        self.png = png_bytes()
        self.mp4 = b""
        self.media = {"type": "image", "mime": "image/png", "simulated": True}
        self.schemas = {}

    def count(self, key, payload=None):
        with self.lock:
            self.counts[key] += 1
            if key in self.requests and payload is not None:
                self.requests[key].append(copy.deepcopy(payload))
            return self.counts[key]

    def fails(self, phase, number):
        with self.lock:
            return self.flags["fail_" + phase] or number in self.flags[phase + "_fail_at"]

    def wait_reverse(self):
        with self.changed:
            return self.changed.wait_for(lambda: not self.flags["hold_reverse"] or self.closed.is_set(), timeout=120)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy({"counts": self.counts, "flags": self.flags,
                                  "model_requests": self.requests["model"],
                                  "submitted": self.requests["prompt"], "uploads": self.requests["upload"],
                                  "prompt_ids": list(self.prompts), "blocked_connections": self.blocked_connections})


def node_schemas(graphs):
    """Small object_info surface derived only from fixture/catalog input slots."""
    schemas = {}
    for graph in graphs:
        for node in graph.values():
            cls = node["class_type"]
            row = schemas.setdefault(cls, {"input": {"required": {}, "optional": {}},
                                           "output": ["IMAGE"], "name": cls, "category": "fixture"})
            for name, value in node.get("inputs", {}).items():
                kind = "STRING"
                if isinstance(value, bool):
                    kind = "BOOLEAN"
                elif isinstance(value, int):
                    kind = "INT"
                elif isinstance(value, float):
                    kind = "FLOAT"
                elif isinstance(value, list):
                    kind = "IMAGE" if "image" in name or name == "first_frame" else "*"
                decl = [kind, {"default": value}] if not isinstance(value, list) else [kind]
                if cls == "LoadImage" and name == "image":
                    decl = [[f"recipe_input_{i}.png" for i in range(1, 7)], {"image_upload": True}]
                group = "optional" if name.startswith("ref_images.ref_image_") and name != "ref_images.ref_image_0" else "required"
                row["input"][group][name] = decl
    return schemas


class LocalServicesHandler(BaseHTTPRequestHandler):
    state: FixtureState

    def log_message(self, *_):
        pass

    def reply(self, body, status=200, content_type="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorized(self):
        return secrets.compare_digest(self.headers.get("X-Fixture-Token", ""), self.state.token)

    def do_GET(self):
        path = urlsplit(self.path).path
        state = self.state
        if path.startswith("/__fixture/"):
            if not self.authorized():
                return self.reply({"error": "fixture token required"}, 403)
            if path == "/__fixture/status":
                return self.reply(state.snapshot())
            return self.reply({"error": "not found"}, 404)
        if path == "/v1/models":
            return self.reply({"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local-fixture"}]})
        if path == "/comfy/system_stats":
            return self.reply({"system": {"comfyui_version": "recipe-local-fixture", "python_version": "3", "os": "fixture"},
                               "devices": [{"name": "NO GPU: HTTP FIXTURE", "type": "cpu", "vram_total": 0, "vram_free": 0}]})
        if path.startswith("/comfy/object_info"):
            cls = unquote(path.removeprefix("/comfy/object_info").strip("/"))
            return self.reply({cls: state.schemas[cls]} if cls in state.schemas else {} if cls else state.schemas)
        if path == "/comfy/queue":
            with state.lock:
                running = [[index, pid, {}, [], {}] for index, pid in enumerate(state.prompts)
                           if state.flags["hold_generate"] and not state.prompts[pid].get("cancelled")]
            return self.reply({"queue_running": running, "queue_pending": []})
        if path.startswith("/comfy/history/"):
            state.count("history")
            pid = path.rsplit("/", 1)[-1]
            with state.lock:
                item = state.prompts.get(pid)
                held = state.flags["hold_generate"]
            if not item or held or item.get("cancelled"):
                return self.reply({})
            video = bool(state.mp4) and any(node.get("class_type") == "SaveVideo" for node in item["prompt"].values())
            kind, extension = ("videos", ".mp4") if video else ("images", ".png")
            return self.reply({pid: {"status": {"status_str": "success", "completed": True, "messages": []},
                                    "outputs": {"92": {kind: [{"filename": pid + extension, "subfolder": "", "type": "output"}]}}}})
        if path == "/comfy/view":
            number = state.count("view")
            if state.fails("download", number):
                return self.reply({"error": "fixture intentional download failure"}, 400)
            filename = parse_qs(urlsplit(self.path).query).get("filename", [""])[0]
            if filename.endswith(".mp4") and state.mp4:
                return self.reply(state.mp4, content_type="video/mp4")
            return self.reply(state.png, content_type="image/png")
        return self.reply({"error": "unknown local fixture endpoint"}, 404)

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        if size > 32 * 1024 * 1024:
            return self.reply({"error": "fixture request too large"}, 413)
        raw = self.rfile.read(size)
        path = urlsplit(self.path).path
        state = self.state
        if path == "/comfy/upload/image":
            number = state.count("upload", {"bytes": len(raw), "content_type": self.headers.get("Content-Type")})
            name = f"fixture_upload_{number}.png"
            with state.lock:
                state.schemas["LoadImage"]["input"]["required"]["image"][0].append(name)
            return self.reply({"name": name, "subfolder": "", "type": "input"})
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("object required")
        except (ValueError, UnicodeDecodeError):
            return self.reply({"error": "JSON object required"}, 400)
        if path.startswith("/__fixture/"):
            if not self.authorized():
                return self.reply({"error": "fixture token required"}, 403)
            if path == "/__fixture/control":
                values = dict(body)
                if "hold" in values:
                    values["hold_generate"] = values.pop("hold")
                if set(values) - set(state.flags):
                    return self.reply({"error": "unknown fixture flag"}, 400)
                for key, value in values.items():
                    valid = (isinstance(value, list) and all(type(n) is int and n > 0 for n in value)) if key.endswith("_at") else type(value) is bool
                    if not valid:
                        return self.reply({"error": "invalid fixture flag value"}, 400)
                with state.changed:
                    state.flags.update(values)
                    state.changed.notify_all()
                return self.reply(state.snapshot())
            if path == "/__fixture/reset-stats":
                with state.lock:
                    state.counts = dict.fromkeys(state.counts, 0)
                    state.requests = {key: [] for key in state.requests}
                return self.reply(state.snapshot())
            if path == "/__fixture/shutdown":
                state.closed.set()
                with state.changed:
                    state.changed.notify_all()
                if state.server:
                    threading.Thread(target=state.server.shutdown, daemon=True).start()
                return self.reply({"ok": True})
            return self.reply({"error": "not found"}, 404)
        if path == "/v1/chat/completions":
            number = state.count("model", body)
            if not state.wait_reverse() or state.closed.is_set():
                return self.reply({"error": "fixture model hold stopped"}, 400)
            if state.fails("reverse", number):
                return self.reply({"error": {"message": "fixture intentional reverse failure"}}, 400)
            serialized = json.dumps(body.get("messages") or [], ensure_ascii=False)
            mode = "r2v" if "subject_definitions:" in serialized else "i2v"
            count = max(1, min(6, len(re.findall(r'"type"\s*:\s*"image_url"', serialized))))
            text = deterministic_prompt(mode, count)
            if not body.get("stream"):
                return self.reply({"id": f"fixture-{number}", "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 30, "completion_tokens": 60, "total_tokens": 90}})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for start in range(0, len(text), 96):
                    chunk = {"id": f"fixture-{number}", "object": "chat.completion.chunk", "model": MODEL_ID,
                             "choices": [{"index": 0, "delta": {"content": text[start:start + 96]}, "finish_reason": None}]}
                    self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                final = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 30, "completion_tokens": 60, "total_tokens": 90}}
                self.wfile.write(("data: " + json.dumps(final) + "\n\ndata: [DONE]\n\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
            return
        if path == "/comfy/prompt":
            number = state.count("prompt", body)
            if state.fails("generate", number):
                return self.reply({"error": {"message": "fixture intentional generation rejection", "type": "invalid_prompt"},
                                   "node_errors": {"92": {"class_type": "SaveVideo", "errors": [{"message": "fixture rejected generation"}]}}}, 400)
            if not isinstance(body.get("prompt"), dict) or not body["prompt"]:
                return self.reply({"error": "missing prompt graph"}, 400)
            pid = "fixture-" + secrets.token_hex(8)
            with state.lock:
                state.prompts[pid] = {"prompt": copy.deepcopy(body["prompt"]), "cancelled": False}
            return self.reply({"prompt_id": pid, "number": number, "node_errors": {}})
        if path == "/comfy/interrupt":
            state.count("interrupt")
            return self.reply({"ok": True})
        if path == "/comfy/queue":
            state.count("queue_delete")
            with state.lock:
                for pid in body.get("delete") or []:
                    if pid in state.prompts:
                        state.prompts[pid]["cancelled"] = True
            return self.reply({"ok": True})
        if path == "/comfy/free":
            return self.reply({"ok": True})
        return self.reply({"error": "unknown local fixture endpoint"}, 404)


def build_app(root_dir: Path, port=0, *, blocked=None, media_mode="auto"):
    """Build real isolated app; caller serves/closes app HTTP and stops app.

    PathContext.local makes exe_dir == app_dir, preventing legacy data import.
    public_dir alone points to repository read-only assets. No source config is read.
    """
    root_dir = Path(root_dir).resolve()
    if root_dir.exists() and (not root_dir.is_dir() or any(root_dir.iterdir())):
        raise ValueError("--root must be a new empty directory; existing data is never removed")
    root_dir.mkdir(parents=True, exist_ok=True)
    if not 0 <= port <= 65535:
        raise ValueError("invalid app port")
    if blocked is None:
        blocked = install_offline_guard()
    from naiba.app import NaibaChatApp
    from naiba.config import ConfigStore
    from naiba.http import AppHTTPServer, RequestHandler
    from naiba.paths import PathContext
    from naiba.storage.media import store_uploaded_file
    from naiba.super_agent.recipe_catalog import builtins

    state = FixtureState(blocked)
    state.mp4, state.media = simulated_media(root_dir, media_mode)
    handler = type("RecipeLocalServices", (LocalServicesHandler,), {"state": state})
    services = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    services.daemon_threads = True
    thread = threading.Thread(target=services.serve_forever, daemon=True, name="recipe-local-http")
    thread.start()
    app = server = None
    try:
        base = f"http://127.0.0.1:{services.server_port}"
        paths = PathContext.local(root_dir, root_dir / "config.json")
        paths.public_dir = ROOT / "public"
        config = ConfigStore(paths.config_path, paths=paths)
        config.data.update({"host": "127.0.0.1", "port": port, "access_token": "",
                            "data_dir": str(root_dir / "data"), "workspace_dir": str(root_dir / "workspace"),
                            "skills_dirs": [], "mcp_servers": [], "default_model_key": MODEL_KEY,
                            "proxy": {"enabled": False, "url": "", "use_system_fallback": False},
                            "providers": [{"id": "recipe-fixture", "kind": "online", "name": "LOCAL Recipe Vision Fixture",
                                           "base_url": base + "/v1", "api_key": "", "request_format": "openai_chat",
                                           "model": MODEL_ID, "supports_images": True}], "comfyui_url": base + "/comfy"})
        config.save()
        (root_dir / "workspace").mkdir()
        app = NaibaChatApp(paths=paths)
        state.app = app
        catalog = builtins()
        # This user-editable workflow is deliberately separate from immutable official entries.
        custom_graph = copy.deepcopy(next(r["workflow"] for r in catalog if r["mode"] == "i2v"))
        custom_graph["105_104"]["inputs"]["prompt"] = "A calm golden rectangle on a blue background."
        custom_graph["92"]["inputs"]["filename_prefix"] = "recipe_fixture_custom"
        state.schemas = node_schemas([r["workflow"] for r in catalog] + [custom_graph])
        imported, status = app.comfyui.api_workflow_create({"name": "LOCAL · 可编辑 H3 配方工作流", "workflow": custom_graph})
        if status != 201:
            raise RuntimeError(f"custom workflow seed failed: {imported}")
        images = [store_uploaded_file(png_bytes(i), f"recipe_input_{i + 1}.png", paths.data_dir) for i in range(6)]
        recipes, status = app.api_super_agent_recipes()
        if status != 200:
            raise RuntimeError(f"recipe catalog failed: {recipes}")
        drafts = []
        create_payloads = []
        for recipe in recipes["recipes"]:
            if recipe["id"] not in {"minimax_h3_i2v", "minimax_h3_r2v"}:
                continue
            slots = recipe["media_slots"]
            items = []
            for index in range(2):
                refs = [images[index]] if recipe["mode"] == "i2v" else images[:2]
                items.append({"source_path": f"fixture-set/shot-{index + 1}.png", "name": f"离线素材 {index + 1}",
                              "requirements": "Preserve the blue background and golden object. No speech.",
                              "media": [{"role": slots[n]["role"], "path": ref["path"], "name": ref["name"]}
                                        for n, ref in enumerate(refs)]})
            payload = {"recipe_id": recipe["id"], "title": "LOCAL 草稿 · " + recipe["name"], "model_key": MODEL_KEY,
                       "model_name": MODEL_ID, "items": items, "shared_media": [], "overrides": [],
                       "duration_seconds": 15, "shots": 1, "request_id": "fixture-seed-" + recipe["id"]}
            created, status = app.api_super_agent_create_recipe_batch(payload)
            if status != 201:
                raise RuntimeError(f"draft recipe seed failed: {created}")
            batch = created["batch"]
            drafts.append({"recipe_id": recipe["id"], "thread_id": created["thread_id"], "batch_id": batch["id"],
                           "item_ids": [item["id"] for item in batch["items"]], "title": batch["title"]})
            create_payloads.append(payload)
        if len(drafts) != 2:
            raise RuntimeError("expected I2V and R2V builtin recipes")
        server = AppHTTPServer(("127.0.0.1", port), RequestHandler, app)
        server.daemon_threads = True
        state.server = server
        app_url = f"http://127.0.0.1:{server.server_port}"
        fixture = {"root": str(root_dir), "app_url": app_url, "base": app_url,
                   "control_url": base + "/__fixture", "control_base": base, "control_token": state.token,
                   "model_url": base + "/v1", "model_key": MODEL_KEY, "model_name": MODEL_ID,
                   "comfyui_url": base + "/comfy", "images": images, "drafts": drafts,
                   "draft_thread_id": drafts[0]["thread_id"], "create_payloads": create_payloads,
                   "custom_workflow_id": imported["workflow"]["id"], "custom_workflow": custom_graph,
                   "custom_prompt_slot": {"node_id": "105_104", "field_name": "prompt"},
                   "custom_media_slots": [{"role": "first_frame", "node_id": "114", "field_name": "image", "scope": "item", "required": True}],
                   "output_type": state.media["type"], "output_mime": state.media["mime"], "simulated_media": state.media,
                   "coverage": "Real NaibaChatApp/Studio ModelRuntime/ComfyUI runners; only loopback external HTTP services faked. "
                               "SaveVideo uses a synthetic local MP4 when available; PNG otherwise. Simulated media, not H3 inference. Seed drafts never dispatch. "
                               "Use create UI too; seed drafts cover browsers without supported file upload."}
        atomic_write_json(root_dir / "fixture.json", fixture)
        return app, server, services, fixture, state
    except BaseException:
        if server:
            server.server_close()
        if app:
            app.stop()
        services.shutdown()
        services.server_close()
        thread.join(timeout=5)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--media-mode", choices=("auto", "video", "png"), default="auto")
    args = parser.parse_args()
    blocked = install_offline_guard()
    app, server, services, fixture, state = build_app(args.root, args.port, blocked=blocked, media_mode=args.media_mode)
    print(json.dumps({"fixture": str(args.root.resolve() / "fixture.json"),
                      "app_url": fixture["app_url"], "control_url": fixture["control_url"], "root": fixture["root"]}), flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        state.closed.set()
        with state.changed:
            state.changed.notify_all()
        server.server_close()
        app.stop()
        services.shutdown()
        services.server_close()


if __name__ == "__main__":
    main()
