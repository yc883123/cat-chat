# -*- coding: utf-8 -*-
"""Reusable offline V1 UI fixture: real NaibaChatApp, storage and HTTP routes.

Only Studio job results and a loopback model HTTP provider are deterministic.
No Super-agent methods/routes are patched. Control HTTP is separate from the app.
Run with the project venv; NAIBA_V1_UI_ROOT must be a new empty directory.
The caller owns lifetime/cleanup and keeps fixture.json, screenshots and logs.
"""
from __future__ import annotations

import argparse
import copy
import ipaddress
import json
import os
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def install_offline_guard():
    """Fail closed before an outbound TCP connection, including background workers."""
    blocked = []

    def audit(event, args):
        if event != "socket.connect":
            return
        address = args[1]
        if not isinstance(address, tuple):
            return
        host = str(address[0])
        try:
            permitted = ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
        except ValueError:
            permitted = host.lower() == "localhost"
        if not permitted:
            blocked.append(host)
            raise PermissionError("V1 UI fixture forbids non-loopback networking")

    sys.addaudithook(audit)
    return blocked


class DeterministicStudioJobs:
    """Only task execution is replaced; real Writer pipelines/Novel apply are used."""

    def __init__(self):
        self._tasks = {}
        self.submitted = []
        self.cancelled = []
        self.failures = set()
        self.lock = threading.RLock()

    def submit(self, input_data):
        with self.lock:
            copied = copy.deepcopy(input_data)
            task_id = f"v1-studio-{len(self._tasks) + 1}"
            model = str(copied.get("model_name") or "")
            novel = copied.get("novel") or {}
            writer = copied.get("writer") or {}
            marker = (model, novel.get("book_id") or writer.get("book_id"))
            fail = model == "smoke-fail" and marker not in self.failures
            if fail:
                self.failures.add(marker)
            results = {
                "genre": '{"cat":"冒烟题材","channel":"male","tags":["验收"]}',
                "title": '["V1 离线之书"]', "blurb": '["离线冒烟简介"]',
                "world": "离线世界观", "chars": '[{"name":"主角","role":"主角"},{"name":"配角一"},{"name":"配角二"},{"name":"配角三"}]',
            }
            task = {
                "id": task_id, "status": "failed" if fail else "done", "input": copied,
                "result": "" if fail else results.get(str(writer.get("step") or ""), "人物：主角\n场景：书房\n道具：书\n离线阶段结果。"),
                "result_partial": "", "error": "V1 deterministic retry failure" if fail else "",
                "created_at": int(time.time() * 1000), "updated_at": int(time.time() * 1000),
            }
            self._tasks[task_id] = task
            self.submitted.append(copy.deepcopy(task))
            return task

    def task(self, task_id):
        return self._tasks.get(str(task_id))

    def tasks(self, active_only=False, limit=60, **filters):
        rows = list(self._tasks.values())
        if active_only:
            rows = [row for row in rows if row["status"] in {"queued", "running"}]
        excluded = set(str(filters.get("exclude_modules") or "").split(","))
        excluded.add(filters.get("exclude_module") or "")
        module = filters.get("module")
        return [row for row in rows if row.get("input", {}).get("module") not in excluded
                and (not module or row.get("input", {}).get("module") == module)][:limit]

    def cancel(self, task_id):
        with self.lock:
            task = self.task(task_id)
            if task:
                task["status"] = "cancelled"
            self.cancelled.append(str(task_id))
        return True

    def shutdown(self):
        return None


class FixtureState:
    def __init__(self):
        self.app = None
        self.catalog = "normal"
        self.model_requests = []
        self.blocked_connections = []
        self.token = secrets.token_hex(16)
        self.saved_providers = []
        self.task_backups = {}
        self.feedback_targets = {}
        self.feedback_calls = {}
        self.feedback_gates = {}
        self.feedback_threads = {}
        self.tool_executions = {}
        self.scenarios = {}


class ModelAndControlHandler(BaseHTTPRequestHandler):
    """Real HTTP model catalog, plus authenticated fixture-only controls."""
    state: FixtureState

    def log_message(self, *_):
        pass

    def reply(self, body, status=200):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.endswith("/models"):
            if self.state.catalog == "error":
                return self.reply({"error": "V1 catalog intentional failure"}, 503)
            names = [] if self.state.catalog == "empty" else (["alternate"] if self.path.startswith("/alt/") else ["other", "probe", "smoke-fail"])
            return self.reply({"object": "list", "data": [{"id": name, "object": "model", "owned_by": "fixture"} for name in names]})
        if self.path == "/job/status":
            return self.reply({"status": "completed"})
        if self.path == "/__fixture/status" and self.headers.get("X-Fixture-Token") == self.state.token:
            return self.reply({"model_requests": self.state.model_requests, "tool_executions": self.state.tool_executions, "studio_submissions": self.state.app.studio.jobs.submitted,
                               "scenarios": self.state.scenarios, "blocked_connections": self.state.blocked_connections})
        return self.reply({"error": "unknown fixture endpoint"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/job/submit":
            return self.reply({"id": "fixture-job"})
        if self.path.endswith("/chat/completions"):
            self.state.model_requests.append(body)
            serialized = json.dumps(body.get("messages") or [], ensure_ascii=False)
            scenario = next((value for key, value in self.state.scenarios.items() if key in serialized), None)
            if scenario:
                scenario["requests"] += 1
                if scenario["requests"] > 18:
                    return self.reply({"error": "runtime fixture inference budget exceeded"}, 409)
                messages = body.get("messages") or []
                checking = str(messages[0].get("content", "")).startswith("你是任务结果审查器")
                if checking:
                    scenario["reviews"] += 1
                    decision = "needs_user" if scenario["kind"] == "supervised_needs_user" else "continue" if scenario["kind"] == "supervised_continue" and scenario["reviews"] == 1 else "done"
                    content = json.dumps({"decision": decision, "summary": "离线逐项检查", "criteria_results": [{"criterion": 0,
                        "status": "passed" if decision == "done" else "unknown" if decision == "needs_user" else "failed", "evidence_refs": [], "judgment": "model"}],
                        "missing_items": ["补写结尾"] if decision == "continue" else [], "next_instruction": scenario["marker"] + " 请补写明确结尾" if decision == "continue" else "",
                        "user_question": "请核对缺少的资料，或明确确认完成" if decision == "needs_user" else "", "progress_summary": "真实本地HTTP检查"}, ensure_ascii=False)
                    delta = {"content": content}
                else:
                    scenario["executions"] += 1
                    if scenario["kind"] in {"supervised_control", "supervised_cancel", "chat_busy"}:
                        self.state.feedback_gates[scenario["target"]].wait(30)
                    has_tool_result = any(row.get("role") == "tool" for row in messages)
                    if scenario["kind"] == "subagent_approval" and not has_tool_result:
                        delta = {"tool_calls": [{"index": 0, "id": "runtime-call", "type": "function", "function": {"name": "list_directory", "arguments": json.dumps({"path": scenario["target"]}, ensure_ascii=False)}}]}
                    else:
                        delta = {"content": scenario["marker"] + " 唯一正文 RESULT_BODY_" + scenario["marker"] + "；这是完整流畅的结果。"}
                if not body.get("stream"):
                    return self.reply({"id": "runtime-completion", "choices": [{"message": {"role": "assistant", **delta}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 17, "completion_tokens": 11, "total_tokens": 28}})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Connection", "close")
                self.end_headers()
                for payload in ({"choices": [{"index": 0, "delta": {"role": "assistant", **delta}}]}, {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if "tool_calls" in delta else "stop"}], "usage": {"prompt_tokens": 17, "completion_tokens": 11, "total_tokens": 28}}):
                    self.wfile.write(("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True
                return
            marker = next((key for key in self.state.feedback_targets if key in serialized), None)
            if marker:
                count = self.state.feedback_calls.get(marker, 0)
                self.state.feedback_calls[marker] = count + 1
                if count >= 3:
                    return self.reply({"error": "feedback fixture inference budget exceeded"}, 409)
                delta = {"content": "离线审批流程已结束。"} if count else {"tool_calls": [{"index": 0,
                    "id": "feedback-call", "type": "function", "function": {"name": "list_directory",
                    "arguments": json.dumps({"path": self.state.feedback_targets[marker]}, ensure_ascii=False)}}]}
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Connection", "close")
                self.end_headers()
                for payload in ({"choices": [{"index": 0, "delta": {"role": "assistant", **delta}}]},
                                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop" if count else "tool_calls"}]}):
                    self.wfile.write(("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True
                return
            # Chat/subagent are deliberately tested create-only (autostart=false).
            # An accidental dispatch must fail, not silently count as runtime success.
            return self.reply({"error": "Unexpected model dispatch in create-only UI smoke"}, 409)
        if self.path != "/__fixture/control" or self.headers.get("X-Fixture-Token") != self.state.token:
            return self.reply({"error": "not authorized"}, 403)
        operation = body.get("operation")
        if operation == "runtime_setup":
            app = self.state.app
            kind = str(body.get("kind") or "subagent_text")
            marker = "runtime-" + secrets.token_hex(8)
            target = app.paths.data_dir.parent / "outside-workspace" / marker
            target.mkdir(parents=True)
            self.state.feedback_gates[str(target)] = threading.Event()
            scenario = {"kind": kind, "marker": marker, "target": str(target), "requests": 0, "executions": 0, "reviews": 0}
            self.state.scenarios[marker] = scenario
            conversation = app.storage.create_conversation(title=kind + " " + marker,
                provider_id="probe", model_key="online:probe", model_name="probe", permission_mode="confirm",
                workspace_dir=str(app.paths.data_dir.parent / "workspace"))
            app.storage.set_enabled_tool_ids(conversation["id"], ["list_directory"])
            scenario["conversation_id"] = conversation["id"]
            return self.reply({"conversation_id": conversation["id"], **scenario})
        elif operation == "runtime_busy":
            scenario = self.state.scenarios[body["marker"]]
            run = self.state.app.runs.submit_chat({"conversation_id": scenario["conversation_id"], "message": scenario["marker"] + " 原会话正在执行",
                "provider_id": "probe", "model_key": "online:probe", "model_name": "probe"})
            return self.reply({"run_id": run["id"]})
        elif operation == "runtime_release":
            self.state.feedback_gates[self.state.scenarios[body["marker"]]["target"]].set()
        elif operation == "catalog":
            self.state.catalog = body["mode"]
        elif operation == "providers":
            self.state.app.config.data["providers"] = [] if body.get("empty") else copy.deepcopy(self.state.saved_providers)
        elif operation == "approval_task":
            task_id = str(body.get("task_id") or "")
            if not task_id and body.get("node_id"):
                task_id = self.state.app.super_agent.get_node("local", body["node_id"]).executor_id
            task = self.state.app.studio.jobs.task(task_id)
            if not task:
                return self.reply({"error": "task not found"}, 404)
            if body.get("broken"):
                self.state.task_backups[task_id] = copy.deepcopy(task["input"])
                task["input"]["novel"]["book_id"] = "fixture-wrong-book"
            else:
                task["input"] = self.state.task_backups.pop(task_id)
        elif operation == "reset_failures":
            self.state.app.studio.jobs.failures.clear()
        elif operation == "feedback_chat":
            app = self.state.app
            marker = "feedback-" + secrets.token_hex(8)
            target = app.paths.data_dir.parent / "outside-workspace" / marker
            target.mkdir(parents=True)
            self.state.feedback_targets[marker] = str(target)
            self.state.feedback_gates[str(target)] = threading.Event()
            conversation = app.storage.create_conversation(title=str(body.get("title") or "离线审批验收"),
                provider_id="probe", model_key="online:probe", model_name="probe", permission_mode="confirm",
                workspace_dir=str(app.paths.data_dir.parent / "workspace"))
            app.storage.set_enabled_tool_ids(conversation["id"], ["list_directory"])
            payload, status = app.api_super_agent_create_thread({"domain": "chat", "conversation_id": conversation["id"],
                "title": str(body.get("title") or "离线审批验收"), "goal": "读取测试目录，等待用户确认", "autostart": True,
                "start_request": {"message": marker, "provider_id": "probe", "model_key": "online:probe", "model_name": "probe"}})
            if status != 201:
                return self.reply(payload, status)
            tid = payload["thread"]["thread_id"]
            self.state.feedback_threads[tid] = str(target)
            return self.reply({"thread_id": tid, "conversation_id": conversation["id"], "node_id": payload["nodes"][0]["node_id"]})
        elif operation == "release_feedback":
            target = self.state.feedback_threads[str(body["thread_id"])]
            self.state.feedback_gates[target].set()
        elif operation == "shutdown":
            for gate in self.state.feedback_gates.values():
                gate.set()
            threading.Thread(target=self.state.server.shutdown, daemon=True).start()
        else:
            return self.reply({"error": "unknown fixture control"}, 400)
        return self.reply({"ok": True})


def build_app(root_dir: Path, port=0):
    """Return app/server/model-server/fixture; do not run a browser or app server."""
    from naiba.app import NaibaChatApp
    from naiba.config import ConfigStore
    from naiba.http import AppHTTPServer, RequestHandler
    from naiba.jobs import JobSpec
    from naiba.paths import PathContext

    root_dir = root_dir.resolve()
    if root_dir.exists() and any(root_dir.iterdir()):
        raise ValueError("NAIBA_V1_UI_ROOT must be a new empty directory; never remove existing data")
    root_dir.mkdir(parents=True, exist_ok=True)
    state = FixtureState()
    handler = type("IsolatedModelHandler", (ModelAndControlHandler,), {"state": state})
    model_server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    model_server.daemon_threads = True
    model_base = f"http://127.0.0.1:{model_server.server_port}"
    model_thread = threading.Thread(target=model_server.serve_forever, daemon=True, name="v1-loopback-model")
    model_thread.start()
    paths = PathContext.local(root_dir, root_dir / "config.json")
    paths.public_dir = ROOT / "public"
    config = ConfigStore(paths.config_path, paths=paths)
    providers = [
        {"id": "probe", "kind": "online", "name": "V1 Probe", "base_url": model_base + "/v1", "api_key": "", "request_format": "openai_chat", "model": "probe"},
        {"id": "probe-alt", "kind": "online", "name": "V1 Alternate", "base_url": model_base + "/alt/v1", "api_key": "", "request_format": "openai_chat", "model": "alternate"},
    ]
    config.data.update({"host": "127.0.0.1", "port": port, "access_token": "", "data_dir": str(root_dir / "data"),
                        "workspace_dir": str(root_dir / "workspace"), "skills_dirs": [], "mcp_servers": [], "providers": providers})
    config.save()
    config.set_default_model_key("online:probe")
    app = NaibaChatApp(paths=paths)
    state.app = app
    state.saved_providers = copy.deepcopy(providers)
    app.studio.jobs = DeterministicStudioJobs()
    # Keep the real permission policy and ToolExecutor; hold only this harmless
    # fixture directory read so a browser can observe the asynchronous phase.
    directory_tool = app.tool_registry.get("list_directory")
    read_directory = directory_tool.execute
    def held_directory_read(arguments, active_skills, run_context):
        target = str(arguments.get("path") or "")
        gate = state.feedback_gates.get(target)
        if gate is not None:
            state.tool_executions[target] = state.tool_executions.get(target, 0) + 1
            if not gate.wait(45):
                return False, "离线审批验收等待释放超时"
        return read_directory(arguments, active_skills, run_context)
    directory_tool.execute = held_directory_read

    def writer(title):
        book = app.writer.store.create_book(title=title)
        app.writer.store.save_setup(book["id"], content={"idea": "离线真实作品创意"}, kind="manual")
        app.writer.store.save_outline(book["id"], chapters=[{"no": 1, "title": "第一章", "beat": "开始", "hook": ""}], sync=True)
        app.writer.store.save_outline(book["id"], parts=[{"no": 1, "title": "第一部：起点", "text": "第一部大纲"}, {"no": 2, "title": "第二部：转折", "text": "第二部大纲"}])
        return book

    writer_book = writer("V1 织书台 · 按名选择")
    retry_book = writer("V1 织书台 · 失败重试")
    staged = app.novel.store.stage_import(filename="v1.txt", data="第1章 起点\n原文一。\n第2章 转折\n原文二。\n第3章 结局\n原文三。".encode(), max_len=6000)
    novel = app.novel.store.create_book(import_id=staged["import_id"], picked=[1, 2, 3], title="V1 拆书匠 · 按名选择")
    conversation = app.storage.create_conversation(title="V1 离线父会话", provider_id="probe", model_key="online:probe", model_name="probe", permission_mode="confirm", workspace_dir=str(root_dir / "workspace"))
    job_id = app.jobs.start(JobSpec(kind="http_poll", conversation_id=conversation["id"], owner_session_id=conversation["id"],
        label="V1 既有离线后台任务", params={"submit": {"type": "http", "url": model_base + "/job/submit"},
        "poll": {"url": model_base + "/job/status"}, "check": {"interval_seconds": 0.01, "timeout_seconds": 5}}), owner=conversation["id"])
    app.jobs.wait(job_id, timeout=5, owner=conversation["id"])
    server = AppHTTPServer(("127.0.0.1", port), RequestHandler, app)
    state.server = server
    server.daemon_threads = True
    fixture = {"base": f"http://127.0.0.1:{server.server_port}", "control_base": model_base, "control_token": state.token,
               "writer_book_id": writer_book["id"], "writer_title": writer_book["title"], "retry_writer_book_id": retry_book["id"], "retry_writer_title": retry_book["title"],
               "novel_book_id": novel["id"], "novel_title": novel["title"], "conversation_id": conversation["id"], "conversation_title": conversation["title"], "job_id": job_id,
               "runtime_coverage": "Chat/subagent create-only: autostart=false; no execution pass claimed"}
    return app, server, model_server, fixture, state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=os.environ.get("NAIBA_V1_UI_ROOT"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("NAIBA_V1_UI_PORT", "0")))
    args = parser.parse_args()
    if args.root is None:
        parser.error("--root or NAIBA_V1_UI_ROOT is required (new empty directory)")
    blocked = install_offline_guard()
    app, server, model_server, fixture, state = build_app(Path(args.root), args.port)
    state.blocked_connections = blocked
    fixture_path = Path(args.root) / "fixture.json"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    print(f"[v1-ui] base={fixture['base']} fixture={fixture_path}", flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        app.stop()
        model_server.shutdown()
        model_server.server_close()


if __name__ == "__main__":
    main()
