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
from naiba.core.exceptions import TaskCancelled
from naiba.http import AppHTTPServer, RequestHandler
from naiba.paths import PathContext

OUTLINE = "## 第一幕：港口来信\n林汐收到失踪船长留下的铜钥匙。\n\n## 第二幕：旧船调查\n她与陈舟循线找到一间封存的档案室。\n\n## 第三幕：真相与远航\n两人解开谜团，保留原作的人物关系与结局。\n"
ADAPT = "TXT改编验收：林汐收起红伞，推开旧港书屋的门。\r\n\r\n陈舟把铜钥匙放到桌上，两人看见信封上的月亮。🌙\r\n"
WORLD = "TXT世界观验收：故事发生在雨季的旧港。船员依靠潮汐表航行，城里的档案馆由三代守书人照看。\n所有线索都能由已出现的人物观察到，人物不会突然获得未知信息。🌙\n"
PARTIAL_PREFIX, PARTIAL_SUFFIX = OUTLINE.split("## 第二幕", 1)
PARTIAL_SUFFIX = "## 第二幕" + PARTIAL_SUFFIX
WORLD_PREFIX, WORLD_SUFFIX = WORLD.split("\n", 1)
WORLD_PREFIX += "\n"


class WriterCancelFixture:
    """Gate model/submission boundaries while retaining production state changes."""

    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.states = {}
        self.events = []
        self.submissions = []
        self.original_submit = app.studio.jobs.submit
        self.original_schedule = app.studio.jobs.runner.submit
        app.studio.jobs.submit = self.submit
        app.studio.jobs.runner.submit = self.schedule

    def register(self, book_id, mode):
        self.states[book_id] = {"mode": mode, "entered": False, "release": threading.Event()}

    def snapshot(self):
        with self.lock:
            return {"events": list(self.events), "submissions": list(self.submissions),
                    "entered_submissions": [key for key, value in self.states.items() if value["entered"]]}

    def submit(self, data, *args, **kwargs):
        metadata = data.get("writer") or {}
        book_id = metadata.get("book_id")
        with self.lock:
            state = self.states.get(book_id)
            hold = state and state["mode"] == "hold_submit" and not state["entered"]
            if hold:
                state["entered"] = True
        if hold and not state["release"].wait(120):
            raise TimeoutError("取消验收没有释放在途提交")
        task = self.original_submit(data, *args, **kwargs)
        with self.lock:
            self.submissions.append({"task_id": task["id"], **metadata})
        return task

    def schedule(self, task_id):
        task = self.app.studio.jobs.store.get_task(task_id)
        metadata = (task.get("input") or {}).get("writer") or {}
        state = self.states.get(metadata.get("book_id"))
        if state and state["mode"] in {"queued", "hold_submit"}:
            return
        self.original_schedule(task_id)

    def complete(self, _profile, messages, options, status_provider):
        cancel = options.get("cancel_event")
        runner = self.app.studio.jobs.runner
        with runner._registry_lock:
            task_id = next(key for key, event in runner._cancel_events.items() if event is cancel)
        task = self.app.studio.jobs.store.get_task(task_id)
        metadata = task["input"]["writer"]
        state = self.states[metadata["book_id"]]
        step = metadata["step"]
        self.app.models.last_usage = {"input_tokens": 120, "output_tokens": 80}
        self.app.models.last_reasoning = ""
        self.app.models.last_finish_reason = "stop"
        self.app.models.last_tool_calls = []
        if state["mode"] == "cooperative" and step == "title" and metadata["attempt"] == 1:
            with self.lock:
                self.events.append({"event": "model_entered", "task_id": task_id, **metadata})
            status_provider({"type": "delta", "content": "取消验收中的未完成书名草稿。"})
            if not cancel.wait(120):
                raise TimeoutError("浏览器没有取消协作模型")
            with self.lock:
                self.events.append({"event": "model_cancelled", "task_id": task_id, **metadata})
            raise TaskCancelled("隔离模型收到真实取消信号")
        reply = complete_result_reply(messages)
        if reply is not None:
            return reply
        payloads = {
            "genre": json.dumps({"channel": "male", "cat": "都市悬疑", "tone": "冷峻"}, ensure_ascii=False),
            "title": json.dumps(["取消后重试的旧港来信"], ensure_ascii=False),
            "blurb": json.dumps(["旧港的守书人在雨夜收到一封信。"], ensure_ascii=False),
            "world": WORLD,
            "chars": json.dumps([{"name": "林汐", "role": "主角", "identity": "守书人",
                                   "goal": "找到船长", "traits": "细心", "speech": "简短",
                                   "arc": "主动查证", "background": "在旧港长大"}], ensure_ascii=False),
        }
        return write_result_reply(payloads[step])

    def control(self, body):
        action = body.get("action")
        book_id = body.get("book_id")
        if action == "release":
            self.states[book_id]["release"].set()
        elif action == "age":
            store = self.app.writer.store
            current = store.get_active_pipeline(book_id)
            assert current["status"] == "stopping" and not current["current_task_id"]
            store.update_pipeline(book_id, current["id"],
                                  lambda row: row.update(stop_requested_at=row["stop_requested_at"] - 61000))
        elif action == "schedule":
            self.original_schedule(body["task_id"])
        else:
            raise ValueError("未知取消验收控制动作")
        return {"ok": True}

    def release_all(self):
        for state in self.states.values():
            state["release"].set()


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
    cancel_mode = bool(os.environ.get("NAIBA_TXT_ONLY_WRITER_CANCEL"))
    cancel_fixture = WriterCancelFixture(app) if cancel_mode else None
    failed_markers = set()
    model_calls = []
    version_lock_mode = bool(os.environ.get("NAIBA_TXT_ONLY_WRITER_VERSION_LOCK"))
    version_lock_releases = {width: threading.Event() for width in (1280, 375)}
    version_lock_entered = set()
    version_lock_guard = threading.Lock()

    def complete(profile, messages, options, status_provider=None):
        assert profile["model"] == "txt-fixture"
        if cancel_fixture:
            return cancel_fixture.complete(profile, messages, options, status_provider)
        text = "\n".join(str(row.get("content") or "") for row in messages)
        match = re.search(r"TXT_(?:OUTLINE|ADAPT|WORLD|PARTIAL|WPARTIAL)_\d+", text)
        marker = match.group() if match else "TXT_WORLD"
        model_calls.append(marker)
        if version_lock_mode:
            width = next((value for value in version_lock_releases
                          if marker == f"TXT_WORLD_{value}"), None)
            if width is None:
                raise AssertionError(f"版本锁验收收到意外模型请求：{marker}")
            with version_lock_guard:
                first_call = width not in version_lock_entered
                version_lock_entered.add(width)
            if first_call and not version_lock_releases[width].wait(timeout=180):
                raise TimeoutError("版本锁浏览器验收没有释放隔离模型")
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
        if version_lock_mode or cancel_mode:
            app.writer.store.save_setup(writer["id"], content={
                "title": writer["title"], "idea": f"雨夜的旧港传来敲门声。TXT_WORLD_{width}",
                "worldview": "版本锁验收的第二版世界观。"})
            app.writer.store.save_outline(writer["id"], sync=True,
                chapters=[{"no": 1, "title": "来信", "beat": "第二版大纲：打开信封并找到铜钥匙"}])
            app.writer.store.save_chapter_version(writer["id"], 1, content="版本锁验收第一版正文。")
            app.writer.store.save_chapter_version(writer["id"], 1, content="版本锁验收第二版正文。")
        if cancel_mode:
            app.writer.store.save_outline(writer["id"], parts=[
                {"no": 1, "title": "旧港", "text": "取消验收前已保存的第一部大纲。"},
                {"no": 2, "title": "来信", "text": "取消验收前已保存的第二部大纲。"},
            ])
        # 连贯性面板把“尚无报告”表示为404；播种真实只读报告，避免无关控制台噪声。
        _report, report_status = app.writer.api_run_continuity_check(writer["id"])
        assert report_status == 200
        writer_resume = app.writer.store.create_book(title=f"TXT织书恢复{width}",
            setup={"title": f"TXT织书恢复{width}", "idea": f"旧港的守书人留下半封信。TXT_WPARTIAL_{width}"})
        _report, report_status = app.writer.api_run_continuity_check(writer_resume["id"])
        assert report_status == 200
        source = {"width": width, "novel": {"id": novel["id"], "title": novel["title"]},
                  "writer": {"id": writer["id"], "title": writer["title"]},
                  "writer_resume": {"id": writer_resume["id"], "title": writer_resume["title"]}}
        if cancel_fixture:
            cancel_fixture.register(writer["id"], "cooperative")
            source["cancel_books"] = {}
            for scenario, mode in (("submit", "hold_submit"), ("stale", "queued"), ("queued", "queued")):
                title = f"取消验收{scenario}{width}"
                extra = app.writer.store.create_book(title=title, setup={"title": title, "idea": "旧港雨夜来信"})
                app.writer.store.save_setup(extra["id"], content={"title": title, "idea": "第二版旧港创意"})
                app.writer.store.save_outline(extra["id"], sync=True,
                    chapters=[{"no": 1, "title": "来信", "beat": "打开信封"}])
                app.writer.store.save_chapter_version(extra["id"], 1, content="取消前保存的正文。")
                _report, status = app.writer.api_run_continuity_check(extra["id"])
                assert status == 200
                cancel_fixture.register(extra["id"], mode)
                source["cancel_books"][scenario] = {"id": extra["id"], "title": title}
        sources.append(source)

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
            elif route == "/_fixture/writer-cancel" and cancel_fixture:
                self._json(cancel_fixture.snapshot())
            elif route == "/_fixture/version-lock" and version_lock_mode:
                with version_lock_guard:
                    entered = sorted(version_lock_entered)
                self._json({"entered": entered})
            else:
                super().do_GET()

        def do_POST(self):
            route = urlparse(self.path).path
            if route == "/_fixture/writer-cancel" and cancel_fixture:
                self._json(cancel_fixture.control(self._read_json()))
            elif route == "/_fixture/corrupt":
                task_id = str(self._read_json().get("task_id") or "")
                task = app.studio.jobs.store.get_task(task_id)
                assert task and (task.get("input") or {}).get("module") in {"novel", "writer"}
                app.studio.jobs.store.artifacts._path(task_id, "result.txt").write_bytes(b"corrupt-fixture")
                self._json({"ok": True})
            elif route == "/_fixture/version-lock/release" and version_lock_mode:
                width = int(self._read_json().get("width") or 0)
                if width not in version_lock_releases:
                    self._json({"error": "未知验收视口"}, 400)
                    return
                version_lock_releases[width].set()
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
        if cancel_fixture:
            cancel_fixture.release_all()
            (runtime / "writer-cancel-events.json").write_text(
                json.dumps(cancel_fixture.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
        for release in version_lock_releases.values():
            release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.stop()


if __name__ == "__main__":
    main()
