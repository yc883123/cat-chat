"""Check legacy outline continuation through real source APIs and Edge, without a provider."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.app import NaibaChatApp
from naiba.config import ConfigStore
from naiba.http import AppHTTPServer, RequestHandler
from naiba.paths import PathContext


OLD_OUTLINE = (
    "## 第一幕 建置\n- 姜乐在旧港发现照片，与陆行舟约定保留证据。\n"
    "## 第二幕 对抗\n- 本批素材未覆盖。\n"
    "## 第三幕 结局\n- 本批素材未覆盖。"
)
FULL_OUTLINE = (
    "## 第一幕 建置\n- 姜乐在旧港发现照片，与陆行舟约定保留证据。\n"
    "## 第二幕 对抗\n- 两人查出旧船航线，公开证据并突破阻挠。\n"
    "## 第三幕 结局\n- 真相在旧港揭晓，两人归还照片，故事完整收束。"
)


class QuietHandler(RequestHandler):
    def log_message(self, *_args):
        pass


def seed_legacy_book(store, title, *, zero=False):
    text = "".join(
        f"第{no}章 旧港{no}\n姜乐与陆行舟在旧港追查照片，第{no}章的结尾素材。\n"
        for no in range(1, 4)
    )
    staged = store.stage_import(filename=title + ".txt", data=text.encode("utf-8"), max_len=6000)
    book = store.create_book(import_id=staged["import_id"], picked=[1, 2, 3])
    manifest_path = store._book_dir(book["id"]) / "book.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("outline_versions", None)
    manifest.pop("outline_version", None)
    manifest["outline"] = OLD_OUTLINE
    if zero:
        manifest["outline_version"] = 0
    store._write_json(manifest_path, manifest)
    return {"id": book["id"], "title": title, "manifest": str(manifest_path)}


def main():
    output = Path(tempfile.mkdtemp(prefix="novel_outline_continue_", dir=ROOT / "verify"))
    runtime = Path(tempfile.mkdtemp(prefix="naiba-novel-outline-continue-"))
    paths = PathContext.local(runtime, runtime / "config.json")
    paths.public_dir = ROOT / "public"
    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({
        "host": "127.0.0.1", "port": 0, "access_token": "",
        "data_dir": str(runtime / "data"), "workspace_dir": str(runtime / "workspace"),
        "skills_dirs": [], "mcp_servers": [],
        "providers": [{
            "id": "outline-fixture", "kind": "online", "name": "本地确定性验收",
            "base_url": "http://127.0.0.1:1/v1", "api_key": "",
            "request_format": "openai_chat", "model": "outline-fixture",
            "context_window": 32768,
        }],
    })
    config.save()
    config.set_default_model_key("online:outline-fixture")
    app = NaibaChatApp(paths=paths)
    calls = []
    call_lock = threading.Lock()
    call_path = output / "model_calls.json"
    call_path.write_text("[]", encoding="utf-8")

    # Substitute only external model operations. Novel/Studio HTTP, prompt assembly,
    # queue, runner, event persistence and applying results all remain real.
    app.models.list_online_models = lambda _profile: ["outline-fixture"]

    def complete(profile, messages, options, status_provider=None):
        assert profile["model"] == "outline-fixture"
        rendered = "\n".join(str(message.get("content") or "") for message in messages)
        assert OLD_OUTLINE in rendered, "runner did not receive the selected legacy outline"
        assert "接续要求" in rendered, "runner did not receive continuation rules"
        with call_lock:
            calls.append({"messages": messages, "model": profile["model"]})
            call_path.write_text(json.dumps(calls, ensure_ascii=False), encoding="utf-8")
        if status_provider:
            status_provider({"type": "delta", "content": FULL_OUTLINE})
        app.models.last_reasoning = ""
        app.models.last_usage = {"input_tokens": 100, "output_tokens": 100}
        return FULL_OUTLINE

    app.models.complete = complete
    store = app.novel.store
    books = [seed_legacy_book(store, "桌面旧大纲接续"),
             seed_legacy_book(store, "手机零版号大纲接续", zero=True)]
    other = seed_legacy_book(store, "切换作品默认不接续")
    server = AppHTTPServer(("127.0.0.1", 0), QuietHandler, app)
    server.daemon_threads = True
    fixture = {
        "base": f"http://127.0.0.1:{server.server_port}", "runtime": str(runtime),
        "books": books, "other": other, "old_outline": OLD_OUTLINE,
        "full_outline": FULL_OUTLINE, "model_calls": str(call_path),
    }
    fixture_path = output / "source.json"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Isolated source fixture: {fixture_path}", flush=True)
    env = {**os.environ, "NAIBA_OUTLINE_CONTINUE_SOURCE": str(fixture_path),
           "NAIBA_SMOKE_OUTPUT": str(output)}
    try:
        subprocess.run(["node", str(ROOT / "verify" / "novel_outline_continue_smoke.cjs")],
                       cwd=ROOT, env=env, check=True, timeout=240)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.stop()
    print(f"Outline continuation browsers passed; artifacts: {output}", flush=True)


if __name__ == "__main__":
    main()
