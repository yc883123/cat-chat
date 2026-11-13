"""真实 HTTP、本地模型替身和桌面/手机：首轮工具整理与截断恢复。"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

import auto_compaction_e2e as base

ROOT = base.ROOT
STATE = {"scenario": "", "turn": 0, "summaries": 0, "requests": []}
LOCK = threading.Lock()
WORKSPACE: Path


class ModelHandler(base._StubModelHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        messages = payload.get("messages") or []
        text = json.dumps(messages, ensure_ascii=False)
        if "只回复 OK" in text:
            content, finish = "OK", "stop"
        elif "你现在负责给这段对话做一次交接" in text:
            with LOCK:
                STATE["summaries"] += 1
                number = STATE["summaries"]
            content, finish = base.SUMMARY, "length" if number == 1 else "stop"
        elif any(message.get("content") == "下一轮：确认整理后仍可继续" for message in messages):
            content, finish = "下一轮使用已整理视图成功。", "stop"
        else:
            with LOCK:
                STATE["turn"] += 1
                number = STATE["turn"]
                scenario = STATE["scenario"]
            if scenario.startswith("first") and number <= 9:
                content = json.dumps({"type": "tool", "tool": "read_file",
                                      "arguments": {"path": str(WORKSPACE / f"part-{number}.txt")}}, ensure_ascii=False)
            else:
                content = "故障回归：本轮已完成。"
            finish = "stop"
        with LOCK:
            STATE["requests"].append({"scenario": STATE["scenario"], "payload": payload,
                                      "finish_reason": finish})
        self._json(200, {"id": "fault-model", "object": "chat.completion",
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                      "finish_reason": finish}],
                         "usage": {"prompt_tokens": 12000, "completion_tokens": 8192 if finish == "length" else 120,
                                   "total_tokens": 20192 if finish == "length" else 12120}})


def run():
    global WORKSPACE
    from naiba.app import NaibaChatApp
    from naiba.config import ConfigStore
    from naiba.http import AppHTTPServer, RequestHandler
    from naiba.paths import PathContext
    from naiba.storage.store import ChatStorage

    root = base.isolated_root()
    WORKSPACE = root / "workspace"
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    conversations = {}
    for width in (1280, 375):
        conversations[f"history-{width}"] = base.seed(root, f"摘要截断恢复 {width}")
    paths = PathContext.local(root, root / "config.json")
    paths.public_dir = ROOT / "public"
    config = ConfigStore(paths.config_path, paths=paths)
    config.data["auto_compaction"]["threshold_percent"] = 30
    config.save()
    storage = ChatStorage(root / "data" / "chat.db")
    for width in (1280, 375):
        conversation = storage.create_conversation(f"首轮工具整理 {width}",
            model_key=f"online:{base.PROVIDER_ID}", model_name=base.MODEL_NAME,
            agent_id="tutor", permission_mode="full", workspace_dir=str(WORKSPACE))
        conversations[f"first-{width}"] = str(conversation["id"])
    for cid in conversations.values():
        storage.update_conversation_settings(cid, stream_enabled=False)
    for number in range(1, 10):
        (WORKSPACE / f"part-{number}.txt").write_text(f"第{number}份已核实资料。" + "资料内容与执行状态均应保留。" * 230, encoding="utf-8")
    base._StubModelHandler = ModelHandler
    model = base.start_model()
    app = NaibaChatApp(paths=paths)

    class Handler(RequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):
            if self.path == "/_fixture/scenario":
                body = self._read_json()
                with LOCK:
                    STATE.update(scenario=str(body.get("scenario") or ""), turn=0, summaries=0)
                self._json({"ok": True})
                return
            return super().do_POST()

        def do_GET(self):
            if self.path == "/_fixture/state":
                with LOCK:
                    self._json({"scenario": STATE["scenario"], "turn": STATE["turn"], "summaries": STATE["summaries"]})
                return
            return super().do_GET()

    server = AppHTTPServer(("127.0.0.1", base.APP_PORT), Handler, app)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    env = {**os.environ, "NAIBA_SMOKE_BASE": base.BASE,
           "NAIBA_COMPACTION_CONVERSATIONS": json.dumps(conversations), "NAIBA_SMOKE_OUTPUT": str(root)}
    try:
        assert base.wait_health(), "隔离服务未启动"
        for script in ("browser_smoke.cjs", "compaction_faults_e2e.cjs"):
            result = subprocess.run(["node", str(ROOT / "verify" / script)], cwd=ROOT, env=env, check=False)
            if result.returncode:
                return result.returncode
        evidence = {}
        for key, cid in conversations.items():
            conversation = app.storage.get_conversation(cid)
            messages = conversation.get("messages") or []
            views = [message for message in messages if (message.get("metadata") or {}).get("trace_view")]
            if key.startswith("first"):
                # 浏览器已撤销投影；完整原始记录仍在。
                trace_messages = [message for message in messages if (message.get("metadata") or {}).get("trace")]
                longest = max((message["metadata"]["trace"] for message in trace_messages), key=len)
                assert len(longest) == 19, (key, len(longest))
                assert not views, "撤销后不保留旧视图"
            evidence[key] = {"messages": len(messages), "trace_views": len(views)}
        (root / "fault-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print("ALL PASS", json.dumps(evidence, ensure_ascii=False))
        return 0
    finally:
        server.shutdown()
        server.server_close()
        app.stop()
        model.shutdown()
        model.server_close()
        print(f"Compaction fault artifacts: {root}")


if __name__ == "__main__":
    raise SystemExit(run())
