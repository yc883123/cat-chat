# -*- coding: utf-8 -*-
"""隔离的真实 Super-agent 阶段链验收服务。

使用真实 NaibaChatApp/HTTP 路由；仅把 Studio jobs 替换为确定性内存任务，
避免浏览器验收调用真实模型。服务写出 fixture.json，供 smoke 脚本取得已建好的
Writer/Novel 作品 ID。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PORT = int(os.environ.get("NAIBA_STAGE_CHAIN_PORT", "8819"))
ROOT_DIR = Path(os.environ.get("NAIBA_STAGE_CHAIN_ROOT") or (ROOT / "verify" / "_tmp_stage_chain"))
FIXTURE = Path(os.environ.get("NAIBA_STAGE_CHAIN_FIXTURE") or (ROOT_DIR / "fixture.json"))


class DeterministicJobs:
    """Studio-compatible task store; ``smoke-fail`` fails once, then succeeds."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict] = {}
        self.submitted: list[dict] = []
        self.cancelled: list[str] = []
        self._fail_counts: dict[str, int] = {}

    def submit(self, input_data):
        task_id = f"stage-smoke-{len(self.tasks) + 1}"
        copied = json.loads(json.dumps(input_data, ensure_ascii=False))
        model_name = str(copied.get("model_name") or "")
        failed_before = self._fail_counts.get(model_name, 0)
        should_fail = model_name == "smoke-fail" and failed_before == 0
        self._fail_counts[model_name] = failed_before + 1
        writer = copied.get("writer") if isinstance(copied.get("writer"), dict) else {}
        step = str(writer.get("step") or "")
        results = {
            "genre": '{"cat":"冒烟题材","channel":"male","tags":["验收"]}',
            "title": '["阶段链之书"]',
            "blurb": '["阶段链冒烟简介"]',
            "world": "阶段链冒烟世界观",
            "chars": '[{"name":"主角","role":"主角"},{"name":"配角一","role":"配角"},{"name":"配角二","role":"配角"},{"name":"配角三","role":"配角"}]',
        }
        result = results.get(step, "阶段链冒烟结果")
        task = {
            "id": task_id,
            "status": "failed" if should_fail else "done",
            "input": copied,
            "result": result if not should_fail else "",
            "result_partial": "",
            "error": "阶段链故意失败（用于 retry 验收）" if should_fail else "",
            "created_at": int(time.time() * 1000),
            "updated_at": int(time.time() * 1000),
        }
        self.tasks[task_id] = task
        self.submitted.append(task)
        return task

    def task(self, task_id):
        return self.tasks.get(str(task_id))

    def cancel(self, task_id):
        task = self.tasks.get(str(task_id))
        if task:
            task["status"] = "cancelled"
        self.cancelled.append(str(task_id))
        return True

    def shutdown(self):
        return None


def build_app():
    from naiba.app import NaibaChatApp
    from naiba.http import AppHTTPServer, RequestHandler
    from naiba.paths import PathContext

    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    paths = PathContext.local(ROOT_DIR, ROOT_DIR / "config.json")
    paths.public_dir = ROOT / "public"
    from naiba.config import ConfigStore
    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({
        "host": "127.0.0.1", "port": PORT, "access_token": "",
        "data_dir": str(ROOT_DIR / "data"), "workspace_dir": str(ROOT_DIR / "workspace"),
        "skills_dirs": [str(ROOT / "skills")], "mcp_servers": [],
        "providers": [{"id": "probe", "kind": "online", "name": "Probe",
                       "base_url": "https://example.invalid/v1", "api_key": "",
                       "request_format": "openai", "model": "probe"},
                      {"id": "probe-alt", "kind": "online", "name": "Probe Alt",
                       "base_url": "https://example.invalid/v1", "api_key": "",
                       "request_format": "openai", "model": "alternate"}],
    })
    config.save()
    config.set_default_model_key("online:probe")
    app = NaibaChatApp(paths=paths)
    # Catalog discovery uses the real route but never contacts a remote provider.
    def list_models(profile):
        if profile.get("id") == "probe-alt":
            return [{"id": "alternate", "name": "Alternate"}]
        return [{"id": "other", "name": "Other"}, {"id": "probe", "name": "Probe"}]
    app.models.list_online_models = list_models
    jobs = DeterministicJobs()
    app.studio.jobs = jobs

    writer_book = app.writer.store.create_book(title="阶段链 Writer 冒烟")
    app.writer.store.save_setup(writer_book["id"], content={"idea": "阶段链验收创意"}, kind="manual")
    app.writer.store.save_outline(writer_book["id"], chapters=[{"no": 1, "title": "第一章", "beat": "开始", "hook": ""}], kind="generated", sync=True)
    retry_book = app.writer.store.create_book(title="阶段链 Writer Retry 冒烟")
    app.writer.store.save_setup(retry_book["id"], content={"idea": "阶段链重试创意"}, kind="manual")
    app.writer.store.save_outline(retry_book["id"], chapters=[{"no": 1, "title": "第一章", "beat": "开始", "hook": ""}], kind="generated", sync=True)
    staged = app.novel.store.stage_import(filename="stage-chain.txt", data="第1章 标题\n阶段链原文。".encode(), max_len=6000)
    novel_book = app.novel.store.create_book(import_id=staged["import_id"], picked=[1], title="阶段链 Novel 冒烟")
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps({"writer_book_id": writer_book["id"], "retry_writer_book_id": retry_book["id"], "novel_book_id": novel_book["id"]}, ensure_ascii=False), encoding="utf-8")
    server = AppHTTPServer(("127.0.0.1", PORT), RequestHandler, app)
    server.daemon_threads = True
    return app, server


def main() -> None:
    app, server = build_app()
    print(f"[stage-chain] port={PORT} fixture={FIXTURE}", flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.shutdown()
        server.server_close()
        app.stop()


if __name__ == "__main__":
    main()
