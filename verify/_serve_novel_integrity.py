"""Run real HTTP + Edge checks against newly seeded, provider-free source data."""
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
from naiba.novel import manuscripts
from naiba.paths import PathContext


class QuietHandler(RequestHandler):
    def log_message(self, *_args):
        pass


def seed_book(store, title, *, identical=False):
    text = "".join(f"第{no}章 旧港{no}\n姜乐与陆行舟在旧港追查照片，第{no}章的原文内容。\n" for no in range(1, 4))
    staged = store.stage_import(filename=title + ".txt", data=text.encode("utf-8"), max_len=6000)
    book = store.create_book(import_id=staged["import_id"], picked=[1, 2, 3])
    result = {"id": book["id"], "manuscripts": {}, "originals": {}}
    for no in range(1, 4):
        chapter = store.get_chapter(book["id"], no)
        result["originals"][str(no)] = chapter["base_manuscript_id"]
        content = chapter["content"] if identical else f"姜乐与陆行舟在旧船上。第 {no} 章人工改编稿。"
        result["manuscripts"][str(no)] = store.save_manuscript(book["id"], no, content=content)["manuscript"]["id"]
    return result


def main():
    output = Path(tempfile.mkdtemp(prefix="novel_integrity_", dir=ROOT / "verify"))
    runtime = Path(tempfile.mkdtemp(prefix="naiba-novel-integrity-"))
    paths = PathContext.local(runtime, runtime / "config.json")
    paths.public_dir = ROOT / "public"
    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({"host": "127.0.0.1", "port": 0, "access_token": "",
                        "data_dir": str(runtime / "data"), "workspace_dir": str(runtime / "workspace"),
                        "skills_dirs": [], "providers": [], "mcp_servers": []})
    config.save()
    app = NaibaChatApp(paths=paths)
    store = app.novel.store
    books = [seed_book(store, "正常原文验收"), seed_book(store, "正常改编稿验收")]
    damaged = []
    for damage in ("missing", "corrupt", "ambiguous"):
        source = seed_book(store, "原文异常验收 " + damage, identical=True)
        book_dir = store._book_dir(source["id"])
        original_path = store._manuscript_path(book_dir, 2, source["originals"]["2"])
        if damage == "missing":
            store.delete_manuscript(source["id"], 2, source["originals"]["2"])
        elif damage == "corrupt":
            original_path.write_text("{损坏的 JSON", encoding="utf-8")
        else:
            extra = manuscripts.build(chapter_no=2, kind=manuscripts.KIND_ORIGINAL, content="另一份无法确认的原文", no=0)
            store._write_json(store._manuscript_path(book_dir, 2, extra["id"]), extra)
            index = store._load_chapter_index(book_dir, 2)
            index["manuscript_ids"].append(extra["id"])
            store._write_json(store._chapter_path(book_dir, 2), index)
        damaged.append({**source, "damage": damage})
    script = seed_book(store, "场次排序验收")
    for no in (1, 2, 3):
        store.apply_script(script["id"], task_id=f"fixture-script-{no}",
                           text=f"INT. 场景{no} - 日\n唯一场次正文 {no}", from_chapter=no)
    server = AppHTTPServer(("127.0.0.1", 0), QuietHandler, app)
    server.daemon_threads = True
    fixture = {"base": f"http://127.0.0.1:{server.server_port}", "runtime": str(runtime),
               "books": books, "damaged": damaged, "script": script}
    fixture_path = output / "source.json"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Isolated fixture: {fixture_path}", flush=True)
    env = {**os.environ, "NAIBA_NOVEL_SOURCE_FILE": str(fixture_path), "NAIBA_SMOKE_OUTPUT": str(output)}
    try:
        scripts = ("novel_integrity_smoke.cjs",) if os.environ.get("NAIBA_INTEGRITY_ONLY") else ("writer_novel_import_smoke.cjs", "novel_integrity_smoke.cjs")
        for script_name in scripts:
            subprocess.run(["node", str(ROOT / "verify" / script_name)], cwd=ROOT, env=env, check=True, timeout=240)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.stop()
    print(f"All integrity browsers passed; artifacts: {output}", flush=True)


if __name__ == "__main__":
    main()
