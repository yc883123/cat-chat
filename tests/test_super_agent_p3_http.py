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
from unittest.mock import patch

from naiba.app import (
    NaibaChatApp,
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

    # Bind the production create facade: a copied implementation would miss
    # regressions in its full ``nodes`` response and safe DTO projection.
    api_super_agent_create_thread = NaibaChatApp.api_super_agent_create_thread

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

    def _profiles(self):
        self.app.config.model_profiles = lambda: [
            {"id": "alpha", "model_key": "online:alpha", "model": "default-a",
             "api_key": "server-only-secret", "base_url": "https://unused.invalid"},
            {"id": "beta", "model_key": "local:beta", "model": "default-b"},
            {"model_key": "probe", "model": "probe"},
        ]

    def _assert_safe_dto(self, payload):
        if isinstance(payload, dict):
            for key, value in payload.items():
                self.assertNotIn(key, {"checkpoint", "raw", "start_request", "api_key", "base_url", "provider", "providers"})
                self._assert_safe_dto(value)
        elif isinstance(payload, list):
            for value in payload:
                self._assert_safe_dto(value)
        self.assertNotIn("server-only-secret", json.dumps(payload, ensure_ascii=False))

    def test_production_create_facade_single_resolves_one_safe_selection(self):
        self._profiles()
        book_id = self._writer_book()
        for label, top, nested, expected in [
            ("top", {"provider_id": "alpha", "model_key": "online:alpha", "model_name": "runtime-a"}, {},
             {"provider_id": "alpha", "model_key": "online:alpha", "model_name": "runtime-a"}),
            ("nested", {}, {"model_key": "local:beta", "model_name": "runtime-b"},
             {"provider_id": "beta", "model_key": "local:beta", "model_name": "runtime-b"}),
            ("compatible-completion", {"model_key": "online:alpha"}, {"provider_id": "alpha", "model_name": "runtime-a"},
             {"provider_id": "alpha", "model_key": "online:alpha", "model_name": "runtime-a"}),
            ("provider-only", {"provider_id": "beta", "model_name": "runtime-b"}, {},
             {"provider_id": "beta", "model_key": "local:beta", "model_name": "runtime-b"}),
            ("legacy", {}, {"model_name": "legacy-model"},
             {"provider_id": "", "model_key": "", "model_name": "legacy-model"}),
            ("duplicates", {"model_key": " online:alpha ", "model_name": "runtime-a"},
             {"provider_id": "alpha", "model_key": "online:alpha", "model_name": "runtime-a"},
             {"provider_id": "alpha", "model_key": "online:alpha", "model_name": "runtime-a"}),
        ]:
            with self.subTest(selection=label):
                status, dto = self._request("POST", "/api/super-agent/threads", {
                    "domain": "writer", "book_id": book_id, "stage": "chapter", **top,
                    "start_request": {"kind": "chapter", "chapter_no": 1, **nested},
                })
                self.assertEqual(status, 201, dto)
                self.assertEqual(len(dto["nodes"]), 1)
                self.assertEqual(dto["nodes"][0], dto["node"])
                self._assert_safe_dto(dto)
                node = self.app.super_agent.get_node("local", dto["node"]["node_id"])
                self.assertEqual({key: node.checkpoint["start_request"][key] for key in expected}, expected)
                snapshot = {key: expected[key] for key in ("provider_id", "model_key") if expected[key]}
                self.assertEqual(node.execution_snapshot, snapshot)
                self.assertEqual(dto["thread"]["execution_snapshot"], snapshot)
                status, events = self._request("GET", f'/api/super-agent/threads/{node.thread_id}/events')
                self.assertEqual(status, 200)
                self._assert_safe_dto(events)

    def test_production_facade_config_fallback_keeps_only_safe_profile_identity(self):
        # profile() returns credentials in production, whereas model_profiles()
        # is sanitized. Exercise the fallback without letting it enter storage.
        self.app.config.model_profiles = lambda: []
        self.app.config.profile = lambda key: {
            "id": "alpha", "kind": "online", "api_key": "server-only-secret",
            "base_url": "https://unused.invalid", "model": "default-a",
        }
        body = {
            "domain": "writer", "book_id": self._writer_book(), "stage": "chapter",
            "model_key": "online:alpha", "provider_id": "alpha", "model_name": "runtime-not-catalogued",
        }
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")), \
                patch("naiba.llm.runtime.ModelRuntime.list_online_models", side_effect=AssertionError("directory forbidden")) as directory:
            dto, status = NaibaChatApp.api_super_agent_create_thread(self.app, body)
        self.assertEqual(status, 201, dto)
        directory.assert_not_called()
        self._assert_safe_dto(dto)
        node = self.app.super_agent.get_node("local", dto["node"]["node_id"])
        self.assertEqual(node.checkpoint["start_request"]["model_name"], "runtime-not-catalogued")
        self.assertNotIn("server-only-secret", json.dumps(node.checkpoint))
        self.assertNotIn("base_url", node.checkpoint["start_request"])
        self.assertEqual(node.execution_snapshot, {"provider_id": "alpha", "model_key": "online:alpha"})

    def test_production_create_facade_rejects_ambiguous_or_invalid_selections(self):
        self._profiles()
        book_id = self._writer_book()
        for top, nested, message in [
            ({"model_key": "online:alpha"}, {"model_key": "local:beta", "model_name": "b"}, "矛盾"),
            ({"provider_id": "alpha"}, {"provider_id": "beta", "model_name": "b"}, "矛盾"),
            ({"model_name": "a"}, {"model_name": "b"}, "矛盾"),
            ({"model_name": "a"}, {"model_name": ""}, "矛盾"),
            ({"model_key": "online:alpha"}, {"provider_id": "beta", "model_name": "b"}, "不匹配"),
            ({"provider_id": "missing", "model_name": "b"}, {}, "不存在"),
            ({"model_key": "online:missing", "model_name": "b"}, {}, "不存在"),
            ({"model_key": "probe", "provider_id": "arbitrary", "model_name": "b"}, {}, "不匹配"),
            ({"model_key": "online:alpha", "model_name": ""}, {}, "model_name"),
        ]:
            with self.subTest(top=top, nested=nested):
                status, dto = self._request("POST", "/api/super-agent/threads", {
                    "domain": "writer", "book_id": book_id, "stage": "chapter", **top,
                    "start_request": nested,
                })
                self.assertEqual(status, 400, dto)
                self.assertIn(message, dto["error"])
        self.assertEqual(self.app.super_agent.list_threads("local"), [])

    def test_production_create_facade_rejects_inline_config_at_any_depth(self):
        book_id = self._writer_book()
        for extra in [
            {"api_key": "client-secret"}, {"provider": {"id": "probe", "kind": "online"}},
            {"providers": [{"id": "probe"}]}, {"base_url": "https://unused.invalid"},
            {"apply_request": {"Provider-Config": {"id": "probe"}}},
            {"start_request": {"model_name": "probe", "base-url": "https://unused.invalid"}},
            {"stage_requests": {"outline": {"provider": {"id": "probe"}}}},
            {"apply_requests": {"outline": {"items": [{"Authorization": "client-secret"}]}}},
        ]:
            with self.subTest(extra=extra):
                status, dto = self._request("POST", "/api/super-agent/threads", {
                    "domain": "writer", "book_id": book_id, "stage": "chapter",
                    "start_request": {"model_name": "probe"}, **extra,
                })
                self.assertEqual(status, 400, dto)
                self.assertIn("不接受 API Key", dto["error"])
                self.assertNotIn("client-secret", json.dumps(dto))
                self._assert_safe_dto(dto)
        self.assertEqual(self.app.super_agent.list_threads("local"), [])

    def test_production_create_facade_chain_override_fallback_and_gates(self):
        self._profiles()
        status, dto = self._request("POST", "/api/super-agent/threads", {
            "domain": "novel", "book_id": self._novel_book(), "chain": "novel_v1",
            "provider_id": "alpha", "model_key": "online:alpha", "model_name": "common",
            "stage_requests": {"elements": {"model_key": "local:beta", "model_name": "override"}},
        })
        self.assertEqual(status, 201, dto)
        self.assertEqual(len(dto["nodes"]), 5)
        self.assertEqual(dto["thread"]["execution_snapshot"], {"provider_id": "alpha", "model_key": "online:alpha"})
        self._assert_safe_dto(dto)
        nodes = self.app.super_agent.list_nodes("local", dto["thread"]["thread_id"])
        self.assertEqual(nodes[1].execution_snapshot, {"provider_id": "beta", "model_key": "local:beta"})
        self.assertEqual(nodes[2].checkpoint["start_request"]["model_name"], "common")
        self.assertEqual(nodes[3].checkpoint["start_request"]["stage"], "script")
        for gate in (nodes[0], nodes[-1]):
            self.assertEqual(gate.execution_snapshot, {})
            for key in ("provider_id", "model_key", "model_name"):
                self.assertNotIn(key, gate.checkpoint["start_request"])
        before = len(self.app.super_agent.list_threads("local"))
        status, dto = self._request("POST", "/api/super-agent/threads", {
            "domain": "writer", "book_id": self._writer_book(), "chain": "writer_v1",
            "stage_requests": {"setup": {"model_name": "legacy"}},
        })
        self.assertEqual(status, 400, dto)
        self.assertIn("stage_requests.outline", dto["error"])
        self.assertEqual(len(self.app.super_agent.list_threads("local")), before)

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
