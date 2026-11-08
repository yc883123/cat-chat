from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from naiba.storage.store import ChatStorage
from naiba.super_agent.domain import DomainCoordinator
from naiba.super_agent.service import SuperAgentService
from naiba.writer.service import WriterService
from naiba.writer.pipeline import WriterPipelineManager
from naiba.novel.service import NovelService, NovelStore
from naiba.paths import PathContext
from naiba.app import NaibaChatApp


class FakeJobs:
    def __init__(self):
        self.tasks = {}
        self.submitted = []
        self.cancelled = []

    def submit(self, input_data):
        task = {"id": f"st-{len(self.tasks)+1}", "status": "queued", "input": json.loads(json.dumps(input_data)), "result": "", "result_partial": "", "error": ""}
        self.tasks[task["id"]] = task
        self.submitted.append(task)
        return task

    def task(self, task_id):
        return self.tasks.get(task_id)

    def cancel(self, task_id):
        self.cancelled.append(task_id)
        if task_id in self.tasks:
            self.tasks[task_id]["status"] = "cancelled"
        return True


class P3App:
    def __init__(self, root: Path):
        self.paths = SimpleNamespace(data_dir=root)
        self.config = SimpleNamespace(model_profiles=lambda: [{"model_key": "probe", "model": "probe", "context_window": 100000}])
        self.storage = ChatStorage(root / "chat.db")
        self.super_agent = SuperAgentService(self.storage)
        self.studio = SimpleNamespace(jobs=FakeJobs())
        self.writer = WriterService(self)
        self.writer.pipeline = WriterPipelineManager(self, self.writer.store)
        self.novel = object.__new__(NovelService)
        self.novel.app = self
        self.novel.store = NovelStore(root)
        self.super_agent_domain = DomainCoordinator(self)

    @staticmethod
    def _super_agent_owner():
        return "local"


class P3WriterNovelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = P3App(Path(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)

    def _writer_book(self):
        book = self.app.writer.store.create_book(title="P3 织书台")
        self.app.writer.store.save_setup(book["id"], content={"idea": "一个测试创意"}, kind="manual")
        self.app.writer.store.save_outline(book["id"], chapters=[{"no": 1, "title": "第一章", "beat": "开始", "hook": ""}], kind="generated", sync=True)
        return book["id"]

    def test_writer_explicit_thread_starts_real_pipeline_and_syncs(self):
        book_id = self._writer_book()
        thread, node = self.app.super_agent_domain.create_thread("local", {
            "domain": "writer", "book_id": book_id, "stage": "chapter",
            "start_request": {"kind": "chapter", "chapter_no": 1, "model_name": "probe", "model_key": "probe"},
        })
        result = self.app.super_agent_domain.action("local", thread.thread_id, {
            "action": "start", "node_id": node.node_id, "attempt": 0, "request_id": "start-1",
        })
        task_id = result.executor_id
        self.assertEqual(self.app.studio.jobs.tasks[task_id]["input"]["writer"]["book_id"], book_id)
        self.app.studio.jobs.tasks[task_id].update(status="done", result="章节正文")
        # draft 落盘后流水线进入章末状态步（跨章连贯性修复）：节点仍在运行，
        # 直到状态任务也完成才 succeeded；正文此刻已经写好。
        synced = self.app.super_agent_domain.action("local", thread.thread_id, {
            "action": "sync", "node_id": node.node_id, "attempt": 0,
        })
        self.assertEqual(synced.node_status, "running")
        self.assertEqual(self.app.writer.store.get_chapter(book_id, 1)["content"], "章节正文")
        state_task_id = self.app.writer.store.get_active_pipeline(book_id)["current_task_id"]
        self.assertEqual(self.app.studio.jobs.tasks[state_task_id]["input"]["writer"]["step"], "state")
        self.app.studio.jobs.tasks[state_task_id].update(
            status="done", result=json.dumps({"events": ["第一章事件"]}, ensure_ascii=False))
        synced = self.app.super_agent_domain.action("local", thread.thread_id, {
            "action": "sync", "node_id": node.node_id, "attempt": 0,
        })
        self.assertEqual(synced.node_status, "succeeded")
        self.assertEqual(self.app.writer.store.get_chapter(book_id, 1)["content"], "章节正文")

    def test_novel_done_waits_for_approval_and_apply_is_idempotent(self):
        staged = self.app.novel.store.stage_import(filename="书.txt", data="第1章 标题\n原文正文。".encode(), max_len=6000)
        book = self.app.novel.store.create_book(import_id=staged["import_id"], picked=[1], title="P3 拆书匠")
        thread, node = self.app.super_agent_domain.create_thread("local", {
            "domain": "novel", "book_id": book["id"], "stage": "outline",
            "start_request": {"book_id": book["id"], "stage": "outline", "chapter_ids": [1], "model_name": "probe", "model_key": "probe"},
            "apply_request": {"stage": "outline", "revision": book["revision"]},
        })
        started = self.app.super_agent_domain.action("local", thread.thread_id, {
            "action": "start", "node_id": node.node_id, "attempt": 0, "request_id": "novel-start",
        })
        task = self.app.studio.jobs.tasks[started.executor_id]
        task.update(status="done", result="三幕大纲")
        self.app.super_agent_domain.action("local", thread.thread_id, {"action": "sync", "node_id": node.node_id, "attempt": 0})
        current = self.app.super_agent.get_node("local", node.node_id)
        self.assertEqual(current.status, "waiting_user")
        approval_id = current.checkpoint["approval_id"]
        approval = self.app.super_agent_domain.resolve_approval("local", thread.thread_id, approval_id, {"status": "approved", "request_id": "approve-1"})
        self.assertEqual(approval.status, "approved")
        self.assertEqual(self.app.novel.store.get_book(book["id"])["book"]["outline"], "三幕大纲")
        again = self.app.super_agent_domain.resolve_approval("local", thread.thread_id, approval_id, {"status": "approved", "request_id": "approve-1"})
        self.assertEqual(again.status, "approved")
        self.assertEqual(len(self.app.novel.store.get_book(book["id"])["book"]["outline_versions"]), 1)

    def test_create_requires_model_name_and_action_result_is_safe(self):
        book_id = self._writer_book()
        with self.assertRaises(ValueError):
            self.app.super_agent_domain.create_thread("local", {
                "domain": "writer", "book_id": book_id, "stage": "chapter",
                "start_request": {"kind": "chapter", "chapter_no": 1},
            })
        thread, node = self.app.super_agent_domain.create_thread("local", {
            "domain": "writer", "book_id": book_id, "stage": "chapter",
            "start_request": {"kind": "chapter", "chapter_no": 1, "model_name": "probe"},
        })
        result = self.app.super_agent_domain.action("local", thread.thread_id, {
            "action": "start", "node_id": node.node_id, "request_id": "safe-start",
        })
        payload = result.as_dict()
        self.assertIn("checkpoint", payload)
        facade_payload, status = NaibaChatApp.api_super_agent_action(self.app, thread.thread_id, {
            "action": "status", "node_id": node.node_id,
        })
        self.assertEqual(status, 200)
        self.assertNotIn("checkpoint", facade_payload["result"])
        self.assertNotIn("raw", facade_payload["result"])
        self.assertIn("node_status", facade_payload["result"])

    def test_writer_retry_submits_once_and_binds_new_attempt(self):
        book_id = self._writer_book()
        thread, node = self.app.super_agent_domain.create_thread("local", {
            "domain": "writer", "book_id": book_id, "stage": "chapter",
            "start_request": {"kind": "chapter", "chapter_no": 1, "model_name": "probe"},
        })
        started = self.app.super_agent_domain.action("local", thread.thread_id, {
            "action": "start", "node_id": node.node_id, "request_id": "retry-start",
        })
        self.app.studio.jobs.tasks[started.executor_id]["status"] = "failed"
        self.app.super_agent_domain.action("local", thread.thread_id, {"action": "sync", "node_id": node.node_id})
        before = len(self.app.studio.jobs.submitted)
        retried = self.app.super_agent_domain.action("local", thread.thread_id, {
            "action": "retry", "node_id": node.node_id, "request_id": "retry-once",
        })
        self.assertEqual(len(self.app.studio.jobs.submitted), before + 1)
        self.assertEqual(retried.node_status, "running")
        self.assertEqual(retried.attempt, 1)
        self.assertEqual(retried.executor_id, self.app.writer.pipeline.get_pipeline(book_id)["pipeline"]["current_task_id"])
        again = self.app.super_agent_domain.action("local", thread.thread_id, {
            "action": "retry", "node_id": node.node_id, "request_id": "retry-once",
        })
        self.assertEqual(len(self.app.studio.jobs.submitted), before + 1)
        self.assertEqual(again.attempt, 1)

    def test_interrupted_event_preserves_recovery_state(self):
        thread = self.app.super_agent.create_thread("local", "恢复")
        node = self.app.super_agent.create_node("local", thread.thread_id, "work", idempotency_key="interrupt-work")
        self.app.super_agent.start_node("local", node.node_id)
        self.app.super_agent.emit_event(
            "local", thread.thread_id, "run_interrupted", node_id=node.node_id,
            source="test", source_event_id="interrupt-1", idempotency_key="interrupt-1", attempt=0,
        )
        self.assertEqual(self.app.super_agent.get_node("local", node.node_id).status, "interrupted")
        self.assertEqual(self.app.super_agent.get_thread("local", thread.thread_id).status, "needs_recovery")

    def test_unknown_domain_and_owner_boundary_are_rejected(self):
        with self.assertRaises(ValueError):
            self.app.super_agent_domain.create_thread("local", {"domain": "unknown-domain", "book_id": "x", "stage": "run"})
        thread = self.app.super_agent.create_thread("local", "x")
        with self.assertRaises(LookupError):
            self.app.super_agent_domain.action("other", thread.thread_id, {"action": "status", "node_id": "missing"})

    def test_chat_domain_missing_conversation_is_bad_request_not_unknown_domain(self):
        payload, status = NaibaChatApp.api_super_agent_create_thread(self.app, {"domain": "chat"})
        self.assertEqual(status, 400)
        self.assertIn("conversation_id", payload["error"])
        self.assertEqual(self.app.super_agent.list_threads("local"), [])

    def test_chat_domain_missing_message_is_bad_request(self):
        conversation = self.app.storage.create_conversation(
            model_key="probe", model_name="probe", workspace_dir=self.tmp.name,
        )
        payload, status = NaibaChatApp.api_super_agent_create_thread(self.app, {
            "domain": "chat", "conversation_id": conversation["id"],
        })
        self.assertEqual(status, 400)
        self.assertIn("message", payload["error"])
        self.assertEqual(self.app.super_agent.list_threads("local"), [])

    def test_chat_domain_unknown_conversation_is_not_found(self):
        payload, status = NaibaChatApp.api_super_agent_create_thread(self.app, {
            "domain": "chat", "conversation_id": "does-not-exist", "start_request": {"message": "test"},
        })
        self.assertEqual(status, 404)
        self.assertIn("对话不存在", payload["error"])
        self.assertEqual(self.app.super_agent.list_threads("local"), [])

    def test_real_app_facade_exposes_p3_create_endpoint(self):
        root = Path(self.tmp.name) / "real-app"
        app = NaibaChatApp(paths=PathContext.local(root, root / "config.json"))
        try:
            book = app.writer.store.create_book(title="HTTP P3")
            app.writer.store.save_setup(book["id"], content={"idea": "HTTP 创意"}, kind="manual")
            payload, status = app.api_super_agent_create_thread({
                "domain": "writer", "book_id": book["id"], "stage": "chapter",
                "start_request": {"kind": "chapter", "chapter_no": 1, "model_name": "probe"},
            })
            self.assertEqual(status, 201)
            self.assertEqual(payload["node"]["executor_type"], "writer_pipeline")
            self.assertNotIn("checkpoint", payload["node"])
        finally:
            app.stop()


if __name__ == "__main__":
    unittest.main()
