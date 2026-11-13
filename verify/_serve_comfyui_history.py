"""隔离源码服务：真实磁盘历史分页与桌面/手机 Playwright 验收。"""
from __future__ import annotations

import io
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

import av
from PIL import Image

from naiba.app import NaibaChatApp
from naiba.comfyui import PARAMS_SCHEMA_VERSION
from naiba.config import ConfigStore
from naiba.http import AppHTTPServer, RequestHandler
from naiba.paths import PathContext


def sample_media() -> tuple[bytes, bytes]:
    image = Image.new("RGB", (320, 180), "#798ca5")
    png = io.BytesIO()
    image.save(png, format="PNG")
    video = io.BytesIO()
    with av.open(video, mode="w", format="mp4") as container:
        stream = container.add_stream("libx264", rate=8)
        stream.width, stream.height, stream.pix_fmt = 160, 96, "yuv420p"
        for index in range(8):
            frame = av.VideoFrame.from_image(Image.new("RGB", (160, 96), (50 + index * 10, 100, 145)))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return png.getvalue(), video.getvalue()


class HistoryFixture:
    def __init__(self, app: NaibaChatApp):
        self.app = app
        self.store = app.comfyui.store
        self.batches = app.comfyui.batches
        self.png, self.video = sample_media()
        self.base_time = int(time.time() * 1000) - 86_400_000
        self.added_tasks: set[str] = set()
        self.added_batches: set[str] = set()
        self.sequence = 0
        self.lock = threading.RLock()
        self.store.save_workflow({
            "id": "cw-history-fixture", "name": "历史分页提交验收",
            "prompt": {
                "1": {"class_type": "Textbox", "inputs": {"text": "历史验收提示词"}},
                "2": {"class_type": "FixtureSampler", "inputs": {"seed": 1111, "cfg": 6.5, "enabled": True}},
            },
            "params": [{"node_id": "1", "field_name": "text", "field_type": "text",
                        "control": "multiline", "role": "positive_prompt", "default": "历史验收提示词",
                        "node_title": "Textbox", "label": "1 · Textbox · text"},
                       {"node_id": "2", "field_name": "seed", "field_type": "seed",
                        "default": 1111, "node_title": "FixtureSampler", "label": "2 · FixtureSampler · seed"},
                       {"node_id": "2", "field_name": "cfg", "field_type": "number",
                        "default": 6.5, "node_title": "FixtureSampler", "label": "2 · FixtureSampler · cfg"},
                       {"node_id": "2", "field_name": "enabled", "field_type": "bool",
                        "default": True, "node_title": "FixtureSampler", "label": "2 · FixtureSampler · enabled"}],
            "params_version": PARAMS_SCHEMA_VERSION,
            "visible_params": [{"node_id": "1", "field_name": "text"},
                               {"node_id": "2", "field_name": "seed"}],
            "created_at": self.base_time, "updated_at": self.base_time,
        })
        self.reset()

    def finish_submitted_task(self, task_id: str) -> None:
        """仅替代外部 runner；HTTP 提交、校验和冻结仍走真实生产实现。"""
        with self.lock:
            self.added_tasks.add(task_id)
            task = self.store.get_task(task_id)
            stamp = int(time.time() * 1000)
            shots = int(task["input"].get("shots") or 1)
            target = self.store.task_output_dir(task_id)
            target.mkdir(parents=True, exist_ok=True)
            (target / "image.png").write_bytes(self.png)
            (target / "preview.mp4").write_bytes(self.video)
            self.store.update_task(task_id, status="completed", started_at=stamp, finished_at=stamp,
                                   progress={"done": shots, "total": shots, "failed": 0},
                                   checkpoint={"prompt_ids": [], "completed": list(range(shots)),
                                               "errors": [], "resubmits": {}},
                                   result={"completed": list(range(shots)),
                                           "outputs": ["image.png", "preview.mp4"]})

    def task(self, task_id: str, stamp: int, *, batch_id: str = "", label: str = "") -> None:
        names = ["image.png", "preview.mp4"]
        record = {
            "id": task_id, "status": "completed", "module": "comfyui", "origin_label": "造梦机",
            "detail": label, "created_at": stamp, "updated_at": stamp,
            "started_at": stamp - 1000, "finished_at": stamp,
            "input": {"workflow_id": "cw-history-fixture", "workflow_name": label,
                      "label": label, "batch_id": batch_id, "prompt": {}, "params": [], "shots": 1},
            "progress": {"done": 1, "total": 1, "failed": 0},
            "checkpoint": {"prompt_ids": [], "completed": [0], "errors": [], "resubmits": {}},
            "result": {"completed": [0], "outputs": names}, "error": "", "usage": {},
        }
        target = self.store.task_output_dir(task_id)
        target.mkdir(parents=True, exist_ok=True)
        (target / names[0]).write_bytes(self.png)
        (target / names[1]).write_bytes(self.video)
        path = self.store.task_record_path(task_id)
        self.store._write_json(path, record)
        os.utime(path, (stamp / 1000, stamp / 1000))

    def batch(self, batch_id: str, stamp: int, *, label: str) -> None:
        items = []
        for index in range(3):
            task_id = f"cf-history-{batch_id.removeprefix('cfb-')}-{index}"
            self.task(task_id, stamp + index, batch_id=batch_id, label=f"{label} 条目 {index + 1}")
            items.append({"index": index + 1, "name": f"条目 {index + 1}", "task_id": task_id,
                          "workflow_id": "cw-history-fixture", "workflow_name": label})
        path = self.batches.batch_path(batch_id)
        self.store._write_json(path, {"id": batch_id, "title": label, "created_at": stamp,
                                      "unload_after": False, "items": items})
        os.utime(path, (stamp / 1000, stamp / 1000))

    def reset(self) -> dict:
        with self.lock:
            for task_id in self.added_tasks:
                self.store.delete_task(task_id)
            for batch_id in self.added_batches:
                record = self.batches._read_record(batch_id) or {}
                for item in record.get("items", []):
                    self.store.delete_task(item["task_id"])
                self.batches.batch_path(batch_id).unlink(missing_ok=True)
            self.added_tasks.clear()
            self.added_batches.clear()
            for index in range(96):
                stamp = self.base_time + index * 1000 + (5_000_000 if index >= 90 else 0)
                self.task(f"cf-history-single-{index:03d}", stamp, label=f"单次历史 {index:03d}")
            for index in range(72):
                self.batch(f"cfb-history-{index:03d}", self.base_time + 2_000_000 + index * 1000,
                           label=f"批次历史 {index:03d}")
            return self.state()

    def state(self) -> dict:
        return {"single_count": 96, "batch_count": 72, "batch_task_count": 216,
                "oldest_single": "cf-history-single-000", "newest_single": "cf-history-single-095",
                "oldest_batch": "cfb-history-000", "newest_batch": "cfb-history-071"}

    def add(self, kind: str) -> dict:
        with self.lock:
            self.sequence += 1
            stamp = int(time.time() * 1000) + self.sequence
            if kind == "batch":
                record_id = f"cfb-history-added-{self.sequence:03d}"
                self.batch(record_id, stamp, label=f"新增批次 {self.sequence}")
                self.added_batches.add(record_id)
            else:
                record_id = f"cf-history-added-{self.sequence:03d}"
                self.task(record_id, stamp, label=f"新增单次 {self.sequence}")
                self.added_tasks.add(record_id)
            return {"id": record_id}


def main() -> None:
    runtime = Path(tempfile.mkdtemp(prefix="naiba-comfyui-history-"))
    output = Path(tempfile.mkdtemp(prefix="comfyui_history_", dir=ROOT / "verify"))
    paths = PathContext.local(runtime, runtime / "config.json")
    paths.public_dir = ROOT / "public"
    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({"host": "127.0.0.1", "port": 0, "access_token": "",
                        "data_dir": str(runtime / "data"), "workspace_dir": str(runtime / "workspace"),
                        "providers": [], "skills_dirs": [], "mcp_servers": []})
    config.save()
    app = NaibaChatApp(paths=paths)
    app.super_agent_domain.runtime.shutdown()
    fixture = HistoryFixture(app)
    # 真实任务提交 API 到 runner 边界止步，不连接 ComfyUI、不启动生成。
    app.comfyui.runner.submit = fixture.finish_submitted_task

    class Handler(RequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/_fixture/state":
                self._json(fixture.state())
            elif path == "/api/comfyui/health":
                self._json({"ok": True, "url": "http://127.0.0.1:8188", "devices": []})
            elif path == "/api/comfyui/models":
                self._json({"models": [], "loras": []})
            else:
                super().do_GET()

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/_fixture/reset":
                self._read_json()
                self._json(fixture.reset())
            elif path == "/_fixture/add":
                self._json(fixture.add(str(self._read_json().get("kind") or "single")))
            elif path == "/_fixture/update":
                payload = self._read_json()
                task = fixture.store.update_task(str(payload.get("task_id") or ""),
                                                 detail=str(payload.get("detail") or "轮询更新"))
                self._json({"ok": task is not None})
            else:
                super().do_POST()

    server = AppHTTPServer(("127.0.0.1", 0), Handler, app)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state = {**fixture.state(), "base": f"http://127.0.0.1:{server.server_port}", "output": str(output)}
    fixture_path = output / "fixture.json"
    fixture_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    env = {**os.environ, "NAIBA_COMFYUI_HISTORY_FIXTURE": str(fixture_path),
           "NAIBA_SMOKE_BASE": state["base"], "NAIBA_SMOKE_OUTPUT": str(output)}
    try:
        print(f"Isolated ComfyUI history fixture: {fixture_path}", flush=True)
        subprocess.run(["node", str(ROOT / "verify" / "comfyui_history_smoke.cjs")],
                       cwd=ROOT, env=env, check=True, timeout=600)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.stop()
    print(f"ComfyUI history browsers passed; artifacts: {output}", flush=True)


if __name__ == "__main__":
    main()
