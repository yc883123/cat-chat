# -*- coding: utf-8 -*-
"""P3 HTTP control-plane acceptance: create, control, approvals, recovery."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from naiba.app import (
    _super_agent_action_payload,
    _super_agent_approval_payload,
    _super_agent_artifact_payload,
    _super_agent_event_payload,
    _super_agent_node_payload,
    _super_agent_thread_payload,
)
from naiba.http import AppHTTPServer, RequestHandler

from tests.test_super_agent_p3_writer_novel import P3App


class P3HTTPApp(P3App):
    """Use the real domain coordinator with the production HTTP facade shape."""

    @staticmethod
    def _super_agent_owner() -> str:
        return "local"

    def api_super_agent_create_thread(self, body):
        try:
            thread, node = self.super_agent_domain.create_thread(self._super_agent_owner(), body)
            return {
                "thread": _super_agent_thread_payload(thread, detail=True),
                "node": _super_agent_node_payload(node),
            }, 201
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except (ValueError, RuntimeError) as exc:
            return {"error": str(exc)}, 400

    def api_super_agent_action(self, thread_id, body):
        try:
            result = self.super_agent_domain.action(self._super_agent_owner(), thread_id, body)
            return {"result": _super_agent_action_payload(result)}, 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except (ValueError, RuntimeError) as exc:
            return {"error": str(exc)}, 400

    def api_super_agent_resolve_approval(self, thread_id, approval_id, body):
        try:
            approval = self.super_agent_domain.resolve_approval(
                self._super_agent_owner(), thread_id, approval_id, body,
            )
            return {"approval": _super_agent_approval_payload(approval)}, 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except (ValueError, RuntimeError) as exc:
            return {"error": str(exc)}, 400

    def api_super_agent_threads(self):
        return {"threads": [_super_agent_thread_payload(item) for item in self.super_agent.list_threads("local")]}, 200

    def api_super_agent_thread(self, thread_id):
        try:
            thread = self.super_agent.get_thread("local", thread_id)
        except LookupError:
            return {"error": "Thread 不存在或无权访问"}, 404
        return {"thread": _super_agent_thread_payload(thread, detail=True)}, 200

    def api_super_agent_nodes(self, thread_id):
        try:
            nodes = self.super_agent.list_nodes("local", thread_id)
        except LookupError:
            return {"error": "Thread 不存在或无权访问"}, 404
        return {"nodes": [_super_agent_node_payload(node) for node in nodes]}, 200

    def api_super_agent_events(self, thread_id, *, after=0):
        try:
            events = self.super_agent.events("local", thread_id, after=after)
        except LookupError:
            return {"error": "Thread 不存在或无权访问"}, 404
        return {"events": [_super_agent_event_payload(event) for event in events], "after": after, "last_sequence": events[-1].sequence if events else after}, 200

    def api_super_agent_artifacts(self, thread_id):
        try:
            artifacts = self.super_agent.artifacts("local", thread_id)
        except LookupError:
            return {"error": "Thread 不存在或无权访问"}, 404
        return {"artifacts": [_super_agent_artifact_payload(item) for item in artifacts]}, 200

    def api_super_agent_approvals(self, thread_id):
        try:
            approvals = self.super_agent.approvals("local", thread_id)
        except LookupError:
            return {"error": "Thread 不存在或无权访问"}, 404
        return {"approvals": [_super_agent_approval_payload(item) for item in approvals]}, 200


class SuperAgentP3HTTPAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_p3_http_acceptance_")
        self.app = P3HTTPApp(Path(self.tmp.name))
        self.server = AppHTTPServer(("127.0.0.1", 0), RequestHandler, self.app)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _request(self, method, path, body=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def _writer_book(self):
        book = self.app.writer.store.create_book(title="P3 HTTP Writer")
        self.app.writer.store.save_setup(book["id"], content={"idea": "HTTP 验收创意"}, kind="manual")
        self.app.writer.store.save_outline(book["id"], chapters=[{"no": 1, "title": "第一章", "beat": "开始", "hook": ""}], kind="generated", sync=True)
        return book["id"]

    def _novel_book(self):
        staged = self.app.novel.store.stage_import(filename="http.txt", data="第1章 标题\n原文正文。".encode(), max_len=6000)
        book = self.app.novel.store.create_book(import_id=staged["import_id"], picked=[1], title="P3 HTTP Novel")
        return book["id"]

    def _create(self, domain, book_id, stage, request, **extra):
        payload = {
            "domain": domain,
            "book_id": book_id,
            "stage": stage,
            "start_request": request,
            "idempotency_key": f"http-{domain}-{book_id}-{stage}",
        }
        payload.update(extra)
        status, body = self._request("POST", "/api/super-agent/threads", payload)
        self.assertEqual(status, 201, body)
        self.assertNotIn("checkpoint", body["node"])
        return body["thread"]["thread_id"], body["node"]["node_id"]

    def _action(self, thread_id, node_id, action, request_id, **extra):
        payload = {"action": action, "node_id": node_id, "request_id": request_id}
        payload.update(extra)
        return self._request("POST", f"/api/super-agent/threads/{thread_id}/actions", payload)

    def test_writer_create_start_cancel_retry_and_safe_action_dto(self):
        book_id = self._writer_book()
        thread_id, node_id = self._create("writer", book_id, "chapter", {
            "kind": "chapter", "chapter_no": 1, "model_name": "probe", "model_key": "probe",
        })
        status, started = self._action(thread_id, node_id, "start", "http-start")
        self.assertEqual(status, 200, started)
        self.assertEqual(started["result"]["node_status"], "running")
        self.assertNotIn("checkpoint", started["result"])
        self.assertNotIn("raw", started["result"])

        task_id = started["result"]["executor_id"]
        before = len(self.app.studio.jobs.submitted)
        status, cancelled = self._action(thread_id, node_id, "cancel", "http-cancel")
        self.assertEqual(status, 200, cancelled)
        self.assertEqual(cancelled["result"]["node_status"], "cancelled")
        status, retried = self._action(thread_id, node_id, "retry", "http-retry")
        self.assertEqual(status, 200, retried)
        self.assertEqual(retried["result"]["node_status"], "running")
        self.assertEqual(retried["result"]["attempt"], 1)
        self.assertEqual(len(self.app.studio.jobs.submitted), before + 1)
        status, replay = self._action(thread_id, node_id, "retry", "http-retry")
        self.assertEqual(status, 200, replay)
        self.assertEqual(len(self.app.studio.jobs.submitted), before + 1)

    def test_create_without_model_is_rejected_before_node_creation(self):
        book_id = self._writer_book()
        status, body = self._request("POST", "/api/super-agent/threads", {
            "domain": "writer", "book_id": book_id, "stage": "chapter",
            "start_request": {"kind": "chapter", "chapter_no": 1},
        })
        self.assertEqual(status, 400)
        self.assertIn("model_name", body["error"])
        self.assertEqual(self.app.super_agent.list_threads("local"), [])

    def test_novel_approval_reject_has_no_writeback(self):
        book_id = self._novel_book()
        thread_id, node_id = self._create("novel", book_id, "outline", {
            "book_id": book_id, "stage": "outline", "chapter_ids": [1], "model_name": "probe", "model_key": "probe",
        }, apply_request={"stage": "outline"})
        status, started = self._action(thread_id, node_id, "start", "novel-start-reject")
        self.assertEqual(status, 200, started)
        self.app.studio.jobs.tasks[started["result"]["executor_id"]].update(status="done", result="不应写回")
        status, synced = self._action(thread_id, node_id, "sync", "novel-sync-reject")
        self.assertEqual(status, 200, synced)
        status, approvals = self._request("GET", f"/api/super-agent/threads/{thread_id}/approvals")
        self.assertEqual(status, 200)
        approval_id = approvals["approvals"][0]["approval_id"]
        status, rejected = self._request("POST", f"/api/super-agent/threads/{thread_id}/approvals/{approval_id}", {
            "status": "rejected", "request_id": "novel-reject",
        })
        self.assertEqual(status, 200, rejected)
        self.assertEqual(rejected["approval"]["status"], "rejected")
        status, nodes = self._request("GET", f"/api/super-agent/threads/{thread_id}/nodes")
        self.assertEqual(status, 200)
        self.assertEqual(nodes["nodes"][0]["status"], "failed")
        self.assertNotEqual(self.app.novel.store.get_book(book_id)["book"].get("outline"), "不应写回")

    def test_novel_approval_approve_is_idempotent_and_writes_once(self):
        book_id = self._novel_book()
        thread_id, node_id = self._create("novel", book_id, "outline", {
            "book_id": book_id, "stage": "outline", "chapter_ids": [1], "model_name": "probe", "model_key": "probe",
        }, apply_request={"stage": "outline"})
        status, started = self._action(thread_id, node_id, "start", "novel-start-approve")
        self.assertEqual(status, 200, started)
        self.app.studio.jobs.tasks[started["result"]["executor_id"]].update(status="done", result="三幕大纲")
        status, _ = self._action(thread_id, node_id, "sync", "novel-sync-approve")
        self.assertEqual(status, 200)
        _, approvals = self._request("GET", f"/api/super-agent/threads/{thread_id}/approvals")
        approval_id = approvals["approvals"][0]["approval_id"]
        approval_path = f"/api/super-agent/threads/{thread_id}/approvals/{approval_id}"
        status, approved = self._request("POST", approval_path, {"status": "approved", "request_id": "novel-approve"})
        self.assertEqual(status, 200, approved)
        status, replay = self._request("POST", approval_path, {"status": "approved", "request_id": "novel-approve"})
        self.assertEqual(status, 200, replay)
        book = self.app.novel.store.get_book(book_id)["book"]
        self.assertEqual(book.get("outline"), "三幕大纲")
        self.assertEqual(len(book.get("outline_versions") or []), 1)

    def test_http_read_after_restart_recovery_is_interrupted_and_idempotent(self):
        book_id = self._writer_book()
        thread_id, node_id = self._create("writer", book_id, "chapter", {
            "kind": "chapter", "chapter_no": 1, "model_name": "probe", "model_key": "probe",
        })
        status, started = self._action(thread_id, node_id, "start", "recovery-start")
        self.assertEqual(status, 200, started)
        self.app.super_agent.recover_all()
        self.app.super_agent.recover_all()
        status, thread = self._request("GET", f"/api/super-agent/threads/{thread_id}")
        self.assertEqual(status, 200)
        self.assertEqual(thread["thread"]["status"], "needs_recovery")
        status, nodes = self._request("GET", f"/api/super-agent/threads/{thread_id}/nodes")
        self.assertEqual(status, 200)
        self.assertEqual(nodes["nodes"][0]["status"], "interrupted")
        status, events = self._request("GET", f"/api/super-agent/threads/{thread_id}/events")
        self.assertEqual(status, 200)
        self.assertEqual(sum(event["event_type"] == "app_restarted" for event in events["events"]), 1)


if __name__ == "__main__":
    unittest.main()
