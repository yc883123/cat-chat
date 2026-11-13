"""真实源码工作台 TXT 验收：外部模型替身，实际 Runner/SkillAgent/文件/写回。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from studio_result_fixture import complete_result_reply, latest_result_receipt, write_result_reply
from naiba.app import NaibaChatApp
from naiba.config import ConfigStore
from naiba.http import AppHTTPServer, RequestHandler
from naiba.paths import PathContext

OUTLINE = "## 第一幕：港口来信\n林汐收到失踪船长留下的铜钥匙。\n\n## 第二幕：旧船调查\n她与陈舟循线找到一间封存的档案室。\n\n## 第三幕：真相与远航\n两人解开谜团，保留原作的人物关系与结局。\n"
ADAPT = "TXT改编验收：林汐收起红伞，推开旧港书屋的门。\r\n\r\n陈舟把铜钥匙放到桌上，两人看见信封上的月亮。🌙\r\n"
WORLD = "TXT世界观验收：故事发生在雨季的旧港。船员依靠潮汐表航行，城里的档案馆由三代守书人照看。\n所有线索都能由已出现的人物观察到，人物不会突然获得未知信息。🌙\n"
PARTIAL_PREFIX, PARTIAL_SUFFIX = OUTLINE.split("## 第二幕", 1)
PARTIAL_SUFFIX = "## 第二幕" + PARTIAL_SUFFIX
WORLD_PREFIX, WORLD_SUFFIX = WORLD.split("\n", 1)
WORLD_PREFIX += "\n"


def main():
    runtime_root = Path(os.environ.get("NAIBA_SMOKE_RUNTIME_DIR") or ROOT / "verify")
    runtime = Path(tempfile.mkdtemp(prefix="workbench_txt_", dir=runtime_root))
    paths = PathContext.local(runtime, runtime / "config.json")
    paths.public_dir = ROOT / "public"
    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({"host": "127.0.0.1", "port": 0, "access_token": "",
                        "data_dir": str(runtime / "data"), "workspace_dir": str(runtime / "workspace"),
                        "skills_dirs": [], "mcp_servers": [], "providers": [{
                            "id": "txt-fixture", "kind": "online", "name": "TXT隔离替身",
                            "base_url": "http://127.0.0.1:1/v1", "api_key": "fixture-only",
                            "request_format": "openai_chat", "model": "txt-fixture", "context_window": 131072}]})
    config.save()
    config.set_default_model_key("online:txt-fixture")
    app = NaibaChatApp(paths=paths)
    app.super_agent_domain.runtime.shutdown()
    app.models.list_online_models = lambda _profile: ["txt-fixture"]
    failed_markers = set()
    model_calls = []

    def complete(profile, messages, options, status_provider=None):
        assert profile["model"] == "txt-fixture"
        text = "\n".join(str(row.get("content") or "") for row in messages)
        match = re.search(r"TXT_(?:OUTLINE|ADAPT|WORLD|PARTIAL|WPARTIAL)_\d+", text)
        marker = match.group() if match else "TXT_WORLD"
        model_calls.append(marker)
        app.models.last_usage = {"input_tokens": 120, "output_tokens": 80}
        app.models.last_reasoning = ""
        app.models.last_finish_reason = "stop"
        app.models.last_tool_calls = []
        receipt = latest_result_receipt(messages)
        if "PARTIAL" in marker:
            if receipt is not None and marker not in failed_markers:
                failed_markers.add(marker)
                raise RuntimeError("隔离验收：第一段已确认后连接中断")
            if receipt is None:
                previous = re.search(r"最新回执：([^\n]+)", text)
                if previous:
                    try:
                        receipt = json.loads(previous.group(1))
                    except ValueError:
                        pass
            if receipt is None:
                return write_result_reply(WORLD_PREFIX if "WPARTIAL" in marker else PARTIAL_PREFIX)
            if receipt.get("segments") == 1:
                return write_result_reply(WORLD_SUFFIX if "WPARTIAL" in marker else PARTIAL_SUFFIX, segment=2)
            return complete_result_reply(messages)
        reply = complete_result_reply(messages)
        if reply is not None:
            return reply
        return write_result_reply(ADAPT if "ADAPT" in marker else OUTLINE if "OUTLINE" in marker else WORLD)

    app.models.complete = complete
    sources = []
    for width in (1280, 375):
        staged = app.novel.store.stage_import(filename=f"TXT原作{width}.txt", max_len=6000,
            data="第1章 旧港来信\n林汐与陈舟在旧港寻找船长留下的档案，铜钥匙开启故事。\n".encode())
        novel = app.novel.store.create_book(import_id=staged["import_id"], picked=[1])
        writer = app.writer.store.create_book(title=f"TXT织书{width}", setup={"title": f"TXT织书{width}", "idea": "雨夜的旧港传来敲门声。"})
        app.writer.store.save_outline(writer["id"], sync=True, chapters=[{"no": 1, "title": "来信", "beat": "打开信封"}])
        # 连贯性面板把“尚无报告”表示为404；播种真实只读报告，避免无关控制台噪声。
        _report, report_status = app.writer.api_run_continuity_check(writer["id"])
        assert report_status == 200
        writer_resume = app.writer.store.create_book(title=f"TXT织书恢复{width}",
            setup={"title": f"TXT织书恢复{width}", "idea": f"旧港的守书人留下半封信。TXT_WPARTIAL_{width}"})
        _report, report_status = app.writer.api_run_continuity_check(writer_resume["id"])
        assert report_status == 200
        sources.append({"width": width, "novel": {"id": novel["id"], "title": novel["title"]},
                        "writer": {"id": writer["id"], "title": writer["title"]},
                        "writer_resume": {"id": writer_resume["id"], "title": writer_resume["title"]}})

    class Handler(RequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            route = urlparse(self.path).path
            if route == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            elif route == "/_fixture/calls":
                self._json({"calls": model_calls})
            else:
                super().do_GET()

        def do_POST(self):
            route = urlparse(self.path).path
            if route == "/_fixture/corrupt":
                task_id = str(self._read_json().get("task_id") or "")
                task = app.studio.jobs.store.get_task(task_id)
                assert task and (task.get("input") or {}).get("module") in {"novel", "writer"}
                app.studio.jobs.store.artifacts._path(task_id, "result.txt").write_bytes(b"corrupt-fixture")
                self._json({"ok": True})
            else:
                super().do_POST()

    server = AppHTTPServer(("127.0.0.1", 0), Handler, app)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fixture = {"base": f"http://127.0.0.1:{server.server_port}", "runtime": str(runtime),
               "sources": sources, "outline": OUTLINE, "adapt": ADAPT, "world": WORLD}
    fixture_path = runtime / "fixture.json"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    env = {**os.environ, "NAIBA_WORKBENCH_TXT_FIXTURE": str(fixture_path),
           "NAIBA_SMOKE_BASE": fixture["base"], "NAIBA_SMOKE_OUTPUT": str(runtime)}
    print(f"TXT隔离源码：{fixture['base']}，产物：{runtime}", flush=True)
    try:
        subprocess.run(["node", str(ROOT / "verify" / "workbench_txt_smoke.cjs")], cwd=ROOT,
                       env=env, check=True, timeout=600)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.stop()


if __name__ == "__main__":
    main()
