"""隔离源码服务：并发恢复、工作流切换、归档和 MCP 浏览器验收。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.app import NaibaChatApp
from naiba.config import ConfigStore
from naiba.http import AppHTTPServer, RequestHandler
from naiba.paths import PathContext


def main():
    runtime = Path(tempfile.mkdtemp(prefix="naiba-concurrency-"))
    output = Path(tempfile.mkdtemp(prefix="concurrency_guards_", dir=ROOT / "verify"))
    paths = PathContext.local(runtime, runtime / "config.json")
    paths.public_dir = ROOT / "public"
    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({"host": "127.0.0.1", "port": 0, "access_token": "",
                        "data_dir": str(runtime / "data"), "workspace_dir": str(runtime / "workspace"),
                        "providers": [], "skills_dirs": [], "mcp_servers": []})
    config.save()
    app = NaibaChatApp(paths=paths)
    # 活执行由夹具明确收尾；本验收不派发模型或访问外部服务。
    app.super_agent_domain.runtime.shutdown()
    fixtures = {"threads": {}, "jobs": []}
    for width in (1280, 375):
        thread = app.super_agent.create_thread("local", f"暂停仍在执行 {width}", status="paused")
        node = app.super_agent.create_node("local", thread.thread_id, "归档收尾验收",
                                           idempotency_key=f"archive-{width}", executor_type="fake", status="running")
        app.super_agent.store.update_thread(thread.thread_id, owner_id="local", status="paused")
        fixtures["threads"][str(width)] = {"thread_id": thread.thread_id, "node_id": node.node_id, "attempt": node.attempt}

    def finish_local_job(job_id, _spec, _cancel):
        app.jobs._finish(job_id, "completed", result={"fixture": True})

    app.jobs._workers["check"] = finish_local_job
    for title in ("独立恢复甲", "独立恢复乙"):
        conversation = app.storage.create_conversation(title)
        source = app.storage.create_run(
            conversation["id"], title, {"id": "", "name": "Job"},
            {"job_spec": {"kind": "check", "resumable": True}, "params": {}, "label": title},
            kind="check", owner_session_id=conversation["id"],
        )
        app.storage.update_job(source["id"], status="interrupted", checkpoint={"handle": "same-checkpoint"})
        fixtures["jobs"].append({"source_id": source["id"], "conversation_id": conversation["id"]})
    resumed = app.jobs.resume_interrupted()
    assert len(resumed) == 2, "不同会话同参数必须分别恢复"
    for job in fixtures["jobs"]:
        source = app.storage.get_background_task(job["source_id"])
        job["resumed_id"] = source["result"]["resumed_into"]
        assert app.jobs.wait(job["resumed_id"], timeout=5)["status"] == "completed"
    assert app.jobs.resume_interrupted() == [], "重复恢复不得再创建任务"

    class Handler(RequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/_fixture/settle":
                payload = self._read_json()
                fixture = fixtures["threads"][str(payload["width"])]
                app.super_agent.store.update_node(fixture["node_id"], owner_id="local",
                                                  status="succeeded", expected_attempt=fixture["attempt"])
                self._json({"ok": True})
                return
            return super().do_POST()

    server = AppHTTPServer(("127.0.0.1", 0), Handler, app)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fixtures["base"] = f"http://127.0.0.1:{server.server_port}"
    fixture_path = output / "fixture.json"
    fixture_path.write_text(json.dumps(fixtures, ensure_ascii=False), encoding="utf-8")
    env = {**os.environ, "NAIBA_GUARDS_FIXTURE": str(fixture_path), "NAIBA_SMOKE_BASE": fixtures["base"],
           "NAIBA_SMOKE_OUTPUT": str(output), "NAIBA_MCP_SMOKE_PORT": str(server.server_port)}
    scripts = ["concurrency_guards_smoke.cjs"]
    if not os.environ.get("NAIBA_CONCURRENCY_ONLY"):
        scripts += ["comfyui_navigation_smoke.cjs", "mcp_connectors_smoke.cjs", "browser_smoke.cjs"]
    try:
        print(f"Isolated browser fixture: {fixture_path}", flush=True)
        for script in scripts:
            subprocess.run(["node", str(ROOT / "verify" / script)], cwd=ROOT, env=env, check=True, timeout=240)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.stop()
    print(f"Concurrency browsers passed; artifacts: {output}", flush=True)


if __name__ == "__main__":
    main()
