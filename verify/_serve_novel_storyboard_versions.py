"""Real source Novel APIs and isolated data for storyboard script-version browsers."""
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
from naiba.novel import prompts
from naiba.paths import PathContext


OLD_SCRIPT = (
    "INT. 旧港仓库 - 夜\n旧稿红伞：林汐推开铁门，看见红伞挂在梁上。\n\n"
    "EXT. 旧港石桥 - 晨\n旧稿铜钥匙：她把铜钥匙交给陈舟，桥下潮水退去。"
)
CURRENT_SCRIPT = (
    "INT. 新城车站 - 日\n新稿蓝灯：林汐走进车站，看见蓝灯闪烁。\n\n"
    "EXT. 新城广场 - 日\n新稿银箱：陈舟把银箱放在长椅旁。\n\n"
    "INT. 新城档案室 - 夜\n新稿石印：两人打开写着石印编号的档案。"
)
GENERATED_SCRIPT = (
    "INT. 山城书屋 - 日\n新增稿紫书：林汐翻开一本紫色旧书。\n\n"
    "EXT. 山城阶梯 - 日\n新增稿竹篮：陈舟带着竹篮沿阶梯上山。\n\n"
    "INT. 山城茶室 - 傍晚\n新增稿青杯：两人在青色茶杯旁交换线索。\n\n"
    "EXT. 山城码头 - 夜\n新增稿纸舟：纸舟顺水漂向灯火。"
)
SINGLE_SCRIPT = "EXT. 唯一稿海湾 - 日\n唯一稿白船：海面只有一艘白船，林汐站在岸边。"


class QuietHandler(RequestHandler):
    def log_message(self, *_args):
        pass


def seed_book(store, title: str, *, kind: str) -> dict:
    text = "第1章 验收小说原文\n林汐与陈舟在港口寻找旧档案，后来转往山城。\n"
    staged = store.stage_import(filename=f"{title}.txt", data=text.encode("utf-8"), max_len=6000)
    book = store.create_book(import_id=staged["import_id"], picked=[1])
    bid = book["id"]
    first = SINGLE_SCRIPT if kind == "single" else OLD_SCRIPT
    store.apply_script(bid, task_id=f"seed-{kind}-old", text=first, chapter_ids=[1])
    if kind == "multi":
        store.apply_script(bid, task_id="seed-current", text=CURRENT_SCRIPT, chapter_ids=[1])
    manifest_path = store._book_dir(bid) / "book.json"
    if kind == "legacy":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("script_versions", None)
        manifest.pop("script_version_id", None)
        manifest["script_version"] = 0
        store._write_json(manifest_path, manifest)
    return {"id": bid, "title": title, "manifest": str(manifest_path)}


def main() -> None:
    output = Path(tempfile.mkdtemp(prefix="novel_storyboard_versions_", dir=ROOT / "verify"))
    runtime = Path(tempfile.mkdtemp(prefix="naiba-novel-storyboard-versions-"))
    paths = PathContext.local(runtime, runtime / "config.json")
    paths.public_dir = ROOT / "public"
    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({
        "host": "127.0.0.1", "port": 0, "access_token": "",
        "data_dir": str(runtime / "data"), "workspace_dir": str(runtime / "workspace"),
        "skills_dirs": [], "mcp_servers": [],
        "providers": [{
            "id": "storyboard-fixture", "kind": "online", "name": "本地确定性验收",
            "base_url": "http://127.0.0.1:1/v1", "api_key": "",
            "request_format": "openai_chat", "model": "storyboard-fixture",
            "context_window": 32768,
        }],
    })
    config.save()
    config.set_default_model_key("online:storyboard-fixture")
    app = NaibaChatApp(paths=paths)
    app.super_agent_domain.runtime.shutdown()
    calls: list[dict] = []
    call_lock = threading.Lock()
    model_gate_lock = threading.Lock()
    model_gate = {"armed": False, "release": None}
    call_path = output / "model_calls.json"
    call_path.write_text("[]", encoding="utf-8")
    # Only the external model boundary is substituted. Novel/Studio HTTP, budget
    # assembly, task queue, runner, events, snapshots and result saves are real.
    app.models.list_online_models = lambda _profile: ["storyboard-fixture"]

    def complete(profile, messages, options, status_provider=None):
        assert profile["model"] == "storyboard-fixture"
        content = "\n".join(str(row.get("content") or "") for row in messages if row.get("role") == "user")
        if "【剧本场次】" in content:
            scene_text = content.split("【剧本场次】", 1)[1].split("【补充要求】", 1)[0].strip()
            scenes, parsed = prompts.parse_storyboard(scene_text)
            assert parsed and scenes, "real storyboard input must include source scenes"
            result = "\n\n".join(
                f"【第 {row['scene_no']} 场】{row['scene_head']}\n隔离验收画面：{row['prompt']}"
                for row in scenes
            )
            stage = "storyboard"
        else:
            assert "验收小说原文" in content, "script generation must use the real chapter"
            result, stage = GENERATED_SCRIPT, "script"
        with call_lock:
            calls.append({"stage": stage, "model": profile["model"], "messages": messages, "result": result})
            call_path.write_text(json.dumps(calls, ensure_ascii=False), encoding="utf-8")
        gate = None
        with model_gate_lock:
            if stage == "storyboard" and model_gate["armed"]:
                model_gate["armed"] = False
                gate = model_gate["release"]
        if gate is not None:
            assert gate.wait(30), "browser did not release the isolated model boundary"
        if status_provider:
            status_provider({"type": "delta", "content": result})
        app.models.last_reasoning = ""
        app.models.last_usage = {"input_tokens": 100, "output_tokens": 100}
        return result

    app.models.complete = complete
    sets = [
        {kind: seed_book(app.novel.store, f"{label}{name}", kind=kind)
         for kind, name in [("multi", "多版剧本分镜"), ("single", "单版剧本分镜"), ("legacy", "旧格式剧本分镜")]}
        for label in ("桌面", "手机")
    ]

    class Handler(QuietHandler):
        def do_POST(self):
            endpoint = urllib.parse.urlparse(self.path).path
            if endpoint == "/_fixture/model-hold":
                self._read_json()
                with model_gate_lock:
                    model_gate.update(armed=True, release=threading.Event())
                self._json({"ok": True})
            elif endpoint == "/_fixture/model-release":
                self._read_json()
                with model_gate_lock:
                    gate = model_gate["release"]
                    model_gate["armed"] = False
                if gate is not None:
                    gate.set()
                self._json({"ok": True})
            else:
                super().do_POST()

    server = AppHTTPServer(("127.0.0.1", 0), Handler, app)
    server.daemon_threads = True
    fixture = {"base": f"http://127.0.0.1:{server.server_port}", "runtime": str(runtime),
               "sets": sets, "old_script": OLD_SCRIPT, "current_script": CURRENT_SCRIPT,
               "generated_script": GENERATED_SCRIPT, "single_script": SINGLE_SCRIPT,
               "old_markers": ["旧稿红伞", "旧稿铜钥匙"],
               "current_markers": ["新稿蓝灯", "新稿银箱", "新稿石印"],
               "generated_markers": ["新增稿紫书", "新增稿竹篮", "新增稿青杯", "新增稿纸舟"],
               "model_calls": str(call_path)}
    fixture_path = output / "source.json"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Isolated source fixture: {fixture_path}", flush=True)
    env = {**os.environ, "NAIBA_STORYBOARD_VERSIONS_SOURCE": str(fixture_path),
           "NAIBA_SMOKE_BASE": fixture["base"], "NAIBA_SMOKE_OUTPUT": str(output),
           "NAIBA_SMOKE_SHOTS": str(output)}
    try:
        checks = ("novel_storyboard_versions_smoke.cjs", "browser_smoke.cjs")
        if "--focused" not in sys.argv:
            checks += ("novel_ui_smoke.cjs",)
        for script in checks:
            if script == "novel_ui_smoke.cjs":
                # The broad UI fixture expects these two provider identities and
                # supplies its own catalog responses. Core acceptance above used
                # only the deterministic storyboard provider.
                app.config.data["providers"] = [
                    {"id": "probe-te", "kind": "online", "name": "TE",
                     "base_url": "http://127.0.0.1:1/v1", "api_key": "",
                     "request_format": "responses", "model": "probe-default"},
                    {"id": "probe-moda", "kind": "online", "name": "魔搭社区",
                     "base_url": "http://127.0.0.1:1/v1", "api_key": "",
                     "request_format": "openai", "model": "probe-qwen"},
                ]
                app.config.set_default_model_key("online:probe-te")
                app.config.save()
            subprocess.run(["node", str(ROOT / "verify" / script)], cwd=ROOT,
                           env=env, check=True, timeout=360)
    finally:
        with model_gate_lock:
            gate = model_gate["release"]
        if gate is not None:
            gate.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.stop()
    print(f"Storyboard version browsers passed; artifacts: {output}", flush=True)


if __name__ == "__main__":
    main()
