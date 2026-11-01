from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from naiba.storage.store import ChatStorage
from naiba.super_agent.service import SuperAgentService


class SuperAgentP0Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.service = SuperAgentService(self.storage)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_thread_owner_and_frozen_snapshot(self) -> None:
        thread = self.service.create_thread(
            "owner-a", "目标", "完成目标",
            execution_snapshot={"provider_id": "p1", "model_key": "m1", "permission_mode": "confirm"},
        )
        self.assertEqual(self.service.get_thread("owner-a", thread.thread_id).owner_id, "owner-a")
        with self.assertRaises(LookupError):
            self.service.get_thread("owner-b", thread.thread_id)
        self.assertEqual(thread.execution_snapshot["model_key"], "m1")
        with self.assertRaises(LookupError):
            self.service.list_nodes("owner-b", thread.thread_id)

    def test_dependencies_and_duplicate_completion_are_idempotent(self) -> None:
        thread = self.service.create_thread("u", "目标")
        first = self.service.create_node("u", thread.thread_id, "first", idempotency_key="first")
        second = self.service.create_node("u", thread.thread_id, "second", idempotency_key="second", dependencies=[first.node_id])
        duplicate = self.service.create_node("u", thread.thread_id, "first", idempotency_key="first")
        self.assertEqual(duplicate.node_id, first.node_id)
        self.assertEqual(second.status, "pending")

        self.service.start_node("u", first.node_id)
        self.service.worker.complete("u", first.node_id, output_refs=["artifact-ref"])
        self.assertEqual(self.service.get_node("u", second.node_id).status, "ready")
        self.service.worker.complete("u", first.node_id, output_refs=["different-ref"])
        self.assertEqual(self.service.get_node("u", first.node_id).output_refs, ["artifact-ref"])

    def test_approval_pause_cancel_retry_and_artifact_idempotency(self) -> None:
        thread = self.service.create_thread("u", "目标")
        node = self.service.create_node("u", thread.thread_id, "publish", idempotency_key="publish")
        approval = self.service.create_approval(
            "u", thread.thread_id, node.node_id,
            action="publish", description="发布结果", idempotency_key="publish-approval",
        )
        self.assertEqual(self.service.get_node("u", node.node_id).status, "waiting_user")
        self.service.resolve_approval("u", thread.thread_id, approval.approval_id, status="approved", resolved_by="u", request_id="approval-1")
        self.assertEqual(self.service.get_node("u", node.node_id).status, "ready")
        self.service.start_node("u", node.node_id)
        self.service.worker.fail("u", node.node_id, "可重试失败")
        self.assertEqual(self.service.get_node("u", node.node_id).status, "failed")
        self.assertEqual(self.service.get_thread("u", thread.thread_id).status, "failed")
        retried = self.service.retry_node("u", node.node_id)
        self.assertEqual(retried.attempt, 1)
        self.service.pause_thread("u", thread.thread_id, request_id="pause-1")
        self.service.resume_thread("u", thread.thread_id, request_id="resume-1")
        artifact = self.service.create_artifact(
            "u", thread.thread_id, node.node_id, kind="text", path_or_ref="ref://result",
            idempotency_key="result", title="结果", summary="摘要",
        )
        self.assertEqual(self.service.create_artifact(
            "u", thread.thread_id, node.node_id, kind="text", path_or_ref="other",
            idempotency_key="result",
        ).artifact_id, artifact.artifact_id)
        self.service.cancel_thread("u", thread.thread_id, request_id="cancel-1")
        self.assertEqual(self.service.get_thread("u", thread.thread_id).status, "cancelled")

    def test_pause_and_cancel_fence_late_worker_event(self) -> None:
        thread = self.service.create_thread("u", "目标")
        node = self.service.create_node("u", thread.thread_id, "work", idempotency_key="work")
        self.service.start_node("u", node.node_id)
        self.service.pause_thread("u", thread.thread_id, request_id="pause-late")
        self.service.worker.complete("u", node.node_id, output_refs=["late-paused"])
        self.assertEqual(self.service.get_node("u", node.node_id).status, "running")
        self.service.resume_thread("u", thread.thread_id, request_id="resume-late")
        self.assertEqual(self.service.get_node("u", node.node_id).status, "succeeded")

        thread2 = self.service.create_thread("u", "取消目标")
        node2 = self.service.create_node("u", thread2.thread_id, "work", idempotency_key="work-2")
        self.service.start_node("u", node2.node_id)
        self.service.cancel_thread("u", thread2.thread_id, request_id="cancel-late")
        self.service.worker.complete("u", node2.node_id, output_refs=["late-cancelled"])
        self.assertEqual(self.service.get_node("u", node2.node_id).status, "cancelled")

    def test_attempt_fencing_and_event_cursor(self) -> None:
        thread = self.service.create_thread("u", "目标")
        node = self.service.create_node("u", thread.thread_id, "work", idempotency_key="work")
        self.service.start_node("u", node.node_id)
        self.service.worker.fail("u", node.node_id)
        self.service.retry_node("u", node.node_id)
        old = self.service.emit_event(
            "u", thread.thread_id, "job_completed", node_id=node.node_id,
            source="late-worker", source_event_id="old", idempotency_key="old", attempt=0,
            payload={"output_refs": ["late"]},
        )
        self.assertEqual(self.service.get_node("u", node.node_id).status, "ready")
        events = self.service.events("u", thread.thread_id)
        self.assertEqual([event.sequence for event in events], list(range(1, len(events) + 1)))
        later = self.service.emit_event("u", thread.thread_id, "user_message", payload={"text": "继续"}, idempotency_key="later")
        self.assertEqual(self.service.events("u", thread.thread_id, after=old.sequence)[0].sequence, old.sequence + 1)
        self.assertEqual(later.sequence, old.sequence + 1)

    def test_recovery_preserves_thread_and_fences_running_node(self) -> None:
        thread = self.service.create_thread("u", "目标")
        node = self.service.create_node("u", thread.thread_id, "work", idempotency_key="work")
        self.service.start_node("u", node.node_id)
        recovered = self.service.recover("u")
        self.assertEqual(recovered[0].thread_id, thread.thread_id)
        self.assertEqual(self.service.get_node("u", node.node_id).status, "interrupted")
        self.assertEqual(self.service.get_thread("u", thread.thread_id).status, "needs_recovery")


if __name__ == "__main__":
    unittest.main()
