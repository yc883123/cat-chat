# -*- coding: utf-8 -*-
"""P2 第一切片：Super-agent 只读 HTTP API 与 owner 隔离守门。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from naiba.http import AppHTTPServer, RequestHandler
from naiba.paths import PathContext
from naiba.app import NaibaChatApp
from naiba.storage.store import ChatStorage
from naiba.super_agent.service import SuperAgentService
from naiba.app import (
    _super_agent_approval_payload,
    _super_agent_artifact_payload,
    _super_agent_event_payload,
    _super_agent_node_payload,
    _super_agent_thread_payload,
)


class _App:
    def __init__(self, db_path: Path) -> None:
        self.config = SimpleNamespace(data={"access_token": "secret-token"})
        self.super_agent = SuperAgentService(ChatStorage(db_path))

    def _owner(self) -> str:
        return "local"

    def api_super_agent_threads(self, include_archived: bool = False):
        return {"threads": [_super_agent_thread_payload(item) for item in self.super_agent.list_threads(self._owner(), include_archived=include_archived)]}, 200

    def api_super_agent_thread(self, thread_id):
        try:
            item = self.super_agent.get_thread(self._owner(), thread_id)
        except LookupError:
            return {"error": "Thread 不存在或无权访问"}, 404
        return {"thread": _super_agent_thread_payload(item, detail=True)}, 200

    def api_super_agent_nodes(self, thread_id):
        try:
            items = self.super_agent.list_nodes(self._owner(), thread_id)
        except LookupError:
            return {"error": "Thread 不存在或无权访问"}, 404
        return {"nodes": [_super_agent_node_payload(item) for item in items]}, 200

    def api_super_agent_events(self, thread_id, *, after=0):
        try:
            items = self.super_agent.events(self._owner(), thread_id, after=after)
        except LookupError:
            return {"error": "Thread 不存在或无权访问"}, 404
        return {"events": [_super_agent_event_payload(item) for item in items], "after": after, "last_sequence": items[-1].sequence if items else after}, 200

    def api_super_agent_artifacts(self, thread_id):
        try:
            items = self.super_agent.artifacts(self._owner(), thread_id)
        except LookupError:
            return {"error": "Thread 不存在或无权访问"}, 404
        return {"artifacts": [_super_agent_artifact_payload(item) for item in items]}, 200

    def api_super_agent_approvals(self, thread_id):
        try:
            items = self.super_agent.approvals(self._owner(), thread_id)
        except LookupError:
            return {"error": "Thread 不存在或无权访问"}, 404
        return {"approvals": [_super_agent_approval_payload(item) for item in items]}, 200


class SuperAgentHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_super_agent_http_")
        self.app = _App(Path(self.tmp.name) / "chat.db")
        self.thread = self.app.super_agent.create_thread(
            "local", "任务总管测试", "读取并展示任务状态",
            summary="测试摘要",
            execution_snapshot={"provider_id": "safe-provider", "model_key": "safe-model", "permission_mode": "confirm"},
        )
        self.node = self.app.super_agent.create_node(
            "local", self.thread.thread_id, "inspect", idempotency_key="inspect-1",
            executor_type="fake", dependencies=[],
        )
        self.app.super_agent.emit_event(
            "local", self.thread.thread_id, "progress", node_id=self.node.node_id,
            payload={"step": "started", "internal_reasoning": "must not be exposed"},
            idempotency_key="event-1", attempt=self.node.attempt,
        )
        self.artifact = self.app.super_agent.create_artifact(
            "local", self.thread.thread_id, self.node.node_id,
            kind="text", path_or_ref="ref://safe-result", title="结果", summary="摘要",
            idempotency_key="artifact-1",
        )
        self.approval = self.app.super_agent.create_approval(
            "local", self.thread.thread_id, self.node.node_id,
            action="publish", description="确认发布", idempotency_key="approval-1",
        )
        self.other = self.app.super_agent.create_thread("other", "别人的任务", "不可见")
        self.base = self._serve()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _serve(self) -> str:
        server = AppHTTPServer(("127.0.0.1", 0), RequestHandler, self.app)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()

        def stop() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def _get(self, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_read_endpoints_return_service_data_without_owner_parameter(self) -> None:
        thread_id = self.thread.thread_id
        status, payload = self._get("/api/super-agent/threads?owner_id=other")
        self.assertEqual(status, 200)
        self.assertEqual([item["thread_id"] for item in payload["threads"]], [thread_id])
        self.assertNotIn("owner_id", payload["threads"][0])

        expected = {
            "": "thread",
            "/nodes": "nodes",
            "/events": "events",
            "/artifacts": "artifacts",
            "/approvals": "approvals",
        }
        for suffix, key in expected.items():
            with self.subTest(suffix=suffix):
                status, body = self._get(f"/api/super-agent/threads/{thread_id}{suffix}")
                self.assertEqual(status, 200)
                self.assertIn(key, body)

        status, body = self._get(f"/api/super-agent/threads/{thread_id}")
        self.assertEqual(body["thread"]["title"], "任务总管测试")
        self.assertEqual(body["thread"]["execution_snapshot"]["model_key"], "safe-model")
        status, body = self._get(f"/api/super-agent/threads/{thread_id}/nodes")
        self.assertEqual(body["nodes"][0]["node_id"], self.node.node_id)
        status, body = self._get(f"/api/super-agent/threads/{thread_id}/artifacts")
        self.assertEqual(body["artifacts"][0]["artifact_id"], self.artifact.artifact_id)
        status, body = self._get(f"/api/super-agent/threads/{thread_id}/approvals")
        self.assertEqual(body["approvals"][0]["approval_id"], self.approval.approval_id)

    def test_owner_isolation_and_unknown_thread_are_not_leaky(self) -> None:
        status, body = self._get(f"/api/super-agent/threads/{self.other.thread_id}")
        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": "Thread 不存在或无权访问"})
        for suffix in ("/nodes", "/events", "/artifacts", "/approvals"):
            status, body = self._get(f"/api/super-agent/threads/{self.other.thread_id}{suffix}")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"], "Thread 不存在或无权访问")

    def test_event_cursor_and_invalid_after(self) -> None:
        thread_id = self.thread.thread_id
        status, body = self._get(f"/api/super-agent/threads/{thread_id}/events?after=2")
        self.assertEqual(status, 200)
        self.assertEqual(body["after"], 2)
        self.assertEqual(body["events"], [])
        status, body = self._get(f"/api/super-agent/threads/{thread_id}/events?after=bad")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "after 必须是整数")

    def test_read_payload_is_summary_only(self) -> None:
        thread_id = self.thread.thread_id
        status, body = self._get(f"/api/super-agent/threads/{thread_id}/events")
        raw = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("internal_reasoning", raw)
        self.assertNotIn("secret-token", raw)
        status, body = self._get(f"/api/super-agent/threads/{thread_id}/nodes")
        self.assertNotIn("checkpoint", body["nodes"][0])

    def test_real_app_facade_is_wired(self) -> None:
        with tempfile.TemporaryDirectory(prefix="naiba_real_app_http_") as directory:
            root = Path(directory)
            paths = PathContext.local(root, root / "config.json")
            app = NaibaChatApp(paths=paths)
            try:
                thread = app.super_agent.create_thread("local", "真实装配", "真实 facade")
                payload, status = app.api_super_agent_thread(thread.thread_id)
                self.assertEqual(int(status), 200)
                self.assertEqual(payload["thread"]["thread_id"], thread.thread_id)
            finally:
                app.stop()

    def test_local_auth_keeps_existing_bypass_and_lan_requires_token(self) -> None:
        handler = RequestHandler.__new__(RequestHandler)
        handler.server = SimpleNamespace(app=self.app)
        handler.client_address = ("192.0.2.10", 1234)
        handler.headers = {"Authorization": "Bearer wrong"}
        from urllib.parse import urlparse
        self.assertFalse(handler._authorized(urlparse("/api/super-agent/threads")))
        handler.headers = {"Authorization": "Bearer secret-token"}
        self.assertTrue(handler._authorized(urlparse("/api/super-agent/threads")))



class ThreadArchiveTests(unittest.TestCase):
    """§5.1 删除＝软删除（归档）：运行中先取消、恢复走状态机、产物不动。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = SuperAgentService(ChatStorage(Path(self.tmp.name) / "chat.db"))

    def test_archive_and_restore_roundtrip_keeps_previous_status(self):
        completed = self.service.create_thread("local", "写小说", "completed 目标")
        self.service.store.update_thread(completed.thread_id, owner_id="local", status="active")
        self.service.store.update_thread(completed.thread_id, owner_id="local", status="completed")
        archived = self.service.archive_thread("local", completed.thread_id, request_id="del-1")
        self.assertEqual(archived.status, "archived")
        self.assertIsNotNone(archived.archived_at)
        # 默认列表不含归档；include_archived 可查（已归档视图）。
        self.assertTrue(all(t.status != "archived" for t in self.service.list_threads("local")))
        self.assertTrue(any(t.thread_id == completed.thread_id for t in self.service.list_threads("local", include_archived=True)))
        restored = self.service.restore_thread("local", completed.thread_id, request_id="restore-1")
        self.assertEqual(restored.status, "completed")
        self.assertEqual(restored.archived_at, None)

    def test_archive_running_and_stopping_are_refused(self):
        for status in ("active", "stopping"):
            thread = self.service.create_thread("local", "运行中", "")
            self.service.store.update_thread(thread.thread_id, owner_id="local", status="active")
            if status == "stopping":
                self.service.store.update_thread(thread.thread_id, owner_id="local", status="stopping")
            with self.assertRaisesRegex(RuntimeError, "先取消"):
                self.service.archive_thread("local", thread.thread_id, request_id=f"del-{status}")

    def test_archive_is_idempotent_and_request_id_guarded(self):
        thread = self.service.create_thread("local", "草稿", "")
        self.service.store.update_thread(thread.thread_id, owner_id="local", status="draft")
        first = self.service.archive_thread("local", thread.thread_id, request_id="del-again")
        second = self.service.archive_thread("local", thread.thread_id, request_id="del-again")
        self.assertEqual(first.status, second.status)
        with self.assertRaises(ValueError):
            self.service.archive_thread("local", thread.thread_id, request_id="")

    def test_restore_requires_explicit_transition_not_raw_write(self):
        # 恢复必须走状态机（archived -> 之前状态）；直接写非法目标会被 update_thread 拒绝。
        thread = self.service.create_thread("local", "待确认", "")
        self.service.store.update_thread(thread.thread_id, owner_id="local", status="active")
        self.service.store.update_thread(thread.thread_id, owner_id="local", status="waiting_user")
        self.service.archive_thread("local", thread.thread_id, request_id="del-2")
        with self.assertRaisesRegex(RuntimeError, "非法 Thread 状态转换"):
            self.service.store.update_thread(thread.thread_id, owner_id="local", status="stopping")
        restored = self.service.restore_thread("local", thread.thread_id, request_id="restore-2")
        self.assertEqual(restored.status, "waiting_user")

    def test_artifacts_and_events_survive_archive(self):
        thread = self.service.create_thread("local", "带产物", "")
        node = self.service.create_node("local", thread.thread_id, "job", idempotency_key="node-1")
        self.service.store.update_thread(thread.thread_id, owner_id="local", status="active")
        self.service.store.update_thread(thread.thread_id, owner_id="local", status="completed")
        artifact = self.service.create_artifact("local", thread.thread_id, node.node_id, kind="video",
                                                path_or_ref="data/generated/out.mp4", idempotency_key="art-1", title="out.mp4")
        self.service.archive_thread("local", thread.thread_id, request_id="del-3")
        self.assertEqual([a.artifact_id for a in self.service.artifacts("local", thread.thread_id)], [artifact.artifact_id])
        self.assertTrue(any(e.event_type == "task_archived" for e in self.service.events("local", thread.thread_id)))


if __name__ == "__main__":
    unittest.main()
