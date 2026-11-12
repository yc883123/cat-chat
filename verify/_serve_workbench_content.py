"""Offline workbench fixture; open printed URL with the official browser tool.

Uses a temporary data directory and two local document Skills. The model is a
bound Python stub; every request is captured locally, never sent to a provider.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
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


class OfflineModel:
    def __init__(self, runtime):
        self.runtime = runtime
        self.calls = []
        self.lock = threading.Lock()
        self.last_usage = {"input_tokens": 100, "output_tokens": 50}
        self.last_reasoning = ""
        self.last_finish_reason = "stop"

    def complete(self, profile, messages, options, status_cb):
        with self.lock:
            self.calls.append(copy.deepcopy(messages))
            (self.runtime / "model_calls.json").write_text(
                json.dumps(self.calls, ensure_ascii=False, indent=2), encoding="utf-8")
        system = str(messages[0].get("content", ""))
        tool_text = "\n".join(str(row.get("content", "")) for row in messages if row.get("role") == "tool")
        if options.get("tools"):
            roots = re.findall(r'<skill[^>]*root=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</skill>', system, re.S)
            for root, main in roots:
                name = "RULE_A" if "RULE_A" in main else "RULE_B"
                if name not in tool_text:
                    return json.dumps({"type": "tool", "tool": "read_file", "arguments": {
                        "path": root.rstrip("/\\") + "/references/rules.md"}})
        last = str(next((row.get("content", "") for row in reversed(messages) if row.get("role") == "user"), ""))
        if "【第" in last and "场" in last:
            numbers = list(dict.fromkeys(re.findall(r"【第\s*(\d+)\s*场】", last))) or ["1"]
            text = "\n\n".join(f"【第 {number} 场】\n雨夜港口，人物站在窗前，中景，柔和侧光。" for number in numbers)
        else:
            text = f"离线生成完成。已保留 {len(messages)} 条输入与历史消息。\n本轮内容：{last[-180:]}"
        status_cb({"type": "delta", "content": text})
        return text


class QuietHandler(RequestHandler):
    def log_message(self, *_args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8812)
    parser.add_argument("--runtime", type=Path)
    args = parser.parse_args()
    runtime = args.runtime or Path(tempfile.mkdtemp(prefix="workbench-content-"))
    paths = PathContext.local(runtime, runtime / "config.json")
    paths.public_dir = ROOT / "public"
    for folder in ("data", "workspace", "skills"):
        (runtime / folder).mkdir(exist_ok=True)
    for key, name, marker in (("alpha", "内容规则甲", "RULE_A"), ("beta", "内容规则乙·用于检查很长名称在窄屏中的换行和完整显示", "RULE_B")):
        folder = runtime / "skills" / key
        (folder / "references").mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: 离线内容测试\n---\n完整规则 {marker}。请读取 references/rules.md，然后生成正文。\n", encoding="utf-8")
        (folder / "references" / "rules.md").write_text(f"{marker} 文档：所有人物外观应保持一致。", encoding="utf-8")
        (folder / "forbidden.py").write_text("raise RuntimeError('must not run')\n", encoding="utf-8")
    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({
        "host": "127.0.0.1", "port": args.port, "access_token": "",
        "data_dir": str(runtime / "data"), "workspace_dir": str(runtime / "workspace"),
        "skills_dirs": [str(runtime / "skills")], "hidden_skill_ids": [], "mcp_servers": [],
        "providers": [{"id": "offline", "kind": "online", "name": "离线测试模型",
                       "base_url": "http://127.0.0.1:1/v1", "api_key": "test-only",
                       "request_format": "openai", "model": "offline-model", "context_window": 131072}],
    })
    config.save()
    config.set_default_model_key("online:offline")
    app = NaibaChatApp(paths=paths)
    model = OfflineModel(runtime)
    app.models.complete = model.complete
    app.models.list_online_models = lambda profile: [{"id": "offline-model", "name": "offline-model"}]
    from naiba.comfyui import PARAMS_SCHEMA_VERSION
    app.comfyui.store.save_workflow({
        "id": "cw-offline-content", "name": "离线转换验收工作流",
        "prompt": {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "初始提示词"}}},
        "params_version": PARAMS_SCHEMA_VERSION,
        "params": [{"node_id": "1", "field_name": "text", "field_type": "text",
                    "control": "multiline", "label": "提示词", "role": "positive_prompt", "default": "初始提示词"}],
    })
    writer_ids = []
    novel_ids = []
    for title in (() if args.runtime else ("系统词验收甲", "系统词验收乙")):
        book = app.writer.store.create_book(title=title, setup={"title": title, "idea": "雨夜港口收到一封来信。"})
        writer_ids.append(book["id"])
        app.writer.store.save_outline(book["id"], sync=True, chapters=[{"no": 1, "title": "来信", "beat": "打开信封"}])
        source = app.novel.store.stage_import(filename=title + ".txt", data="第1章 雨港\n人物站在窗前，收到一封信。".encode("utf-8"), max_len=6000)
        novel = app.novel.store.create_book(import_id=source["import_id"], picked=[1], title=title)
        novel_ids.append(novel["id"])
        app.novel.store.apply_script(novel["id"], task_id="fixture-script", text="INT. 港口 - 夜\n人物站在窗前，慢慢打开信封。", from_chapter=1)
    server = AppHTTPServer(("127.0.0.1", args.port), QuietHandler, app)
    server.daemon_threads = True
    print(json.dumps({"base": f"http://127.0.0.1:{server.server_port}", "runtime": str(runtime),
                      "writer_ids": writer_ids, "novel_ids": novel_ids}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        app.stop()


if __name__ == "__main__":
    main()
