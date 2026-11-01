from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from naiba.jobs import JobSpec
from naiba.storage.store import ChatStorage
from naiba.super_agent.adapters import AdapterError, FakeAdapter, JobAdapter
from naiba.super_agent.router import AdapterRegistry, FastPathRequest, FastPathRouter, RouterError
from naiba.super_agent.service import SuperAgentService


class _FakeJobRegistry:
    def __init__(self) -> None:
        self.next_id = 0
        self.started: list[JobSpec] = []
        self.jobs: dict[str, dict] = {}
        self.cancelled: list[str] = []
        self.retried: list[str] = []
        self.resumed: list[str] = []

    def start(self, spec: JobSpec, owner: str | None = None) -> str:
        self.next_id += 1
        job_id = f"job-{self.next_id}"
        self.started.append(spec)
        self.jobs[job_id] = {
            "id": job_id,
            "kind": spec.kind,
            "conversation_id": spec.conversation_id,
            "owner_session_id": owner or spec.owner_session_id,
            "status": "running",
            "attempt": 0,
            "checkpoint": dict(spec.checkpoint or {}),
            "result": {},
            "error": "",
        }
        return job_id

    def get(self, job_id: str, owner: str | None = None):
        job = self.jobs.get(job_id)
        if job is None or (owner and job["owner_session_id"] != owner):
            return None
        return dict(job)

    def cancel(self, job_id: str, owner: str | None = None, reason: str | None = None):
        job = self.get(job_id, owner)
        if job is None:
            return None
        job["status"] = "stopping"
        self.cancelled.append(job_id)
        return dict(job)

    def retry(self, job_id: str, owner: str | None = None):
        job = self.get(job_id, owner)
        if job is None:
            return None
        self.retried.append(job_id)
        spec = JobSpec(kind=job["kind"], conversation_id=job["conversation_id"], owner_session_id=owner or "u", checkpoint={"retry": True})
        return self.start(spec, owner=owner)

    def resume(self, job_id: str, owner: str | None = None):
        job = self.get(job_id, owner)
        if job is None or not job.get("checkpoint"):
            return None
        self.resumed.append(job_id)
        spec = JobSpec(kind=job["kind"], conversation_id=job["conversation_id"], owner_session_id=owner or "u", checkpoint=dict(job["checkpoint"]))
        return self.start(spec, owner=owner)


class SuperAgentP1AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.service = SuperAgentService(self.storage)
        self.thread = self.service.create_thread("u", "目标")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def node(self, *, executor_type: str = "fake", **kwargs):
        return self.service.create_node(
            "u", self.thread.thread_id, kwargs.pop("kind", "work"),
            executor_type=executor_type, idempotency_key=kwargs.pop("idempotency_key", f"{executor_type}-1"), **kwargs,
        )

    def test_registry_is_explicit(self) -> None:
        adapter = FakeAdapter(self.service)
        registry = AdapterRegistry([adapter])
        self.assertIs(registry.get("fake"), adapter)
        with self.assertRaises(ValueError):
            registry.register(adapter)
        with self.assertRaises(RouterError):
            registry.get("job")

    def test_fake_adapter_delegates_and_duplicate_start_is_idempotent(self) -> None:
        node = self.node()
        adapter = FakeAdapter(self.service)
        first = adapter.start("u", self.thread.thread_id, node.node_id)
        second = adapter.start("u", self.thread.thread_id, node.node_id)
        self.assertEqual(first.executor_id, second.executor_id)
        self.assertEqual(self.service.get_node("u", node.node_id).status, "running")
        adapter.complete("u", self.thread.thread_id, node.node_id, output_refs=["ref://one"])
        self.assertEqual(self.service.get_node("u", node.node_id).output_refs, ["ref://one"])
        with self.assertRaises(AdapterError):
            adapter.start("u", self.thread.thread_id, node.node_id)

    def test_owner_and_thread_boundaries(self) -> None:
        node = self.node()
        adapter = FakeAdapter(self.service)
        with self.assertRaises(LookupError):
            adapter.start("other", self.thread.thread_id, node.node_id)
        other = self.service.create_thread("u", "其他")
        with self.assertRaises(LookupError):
            adapter.status("u", other.thread_id, node.node_id)

    def test_fast_path_deduplicates_source_event_without_model(self) -> None:
        node = self.node()
        adapter = FakeAdapter(self.service)
        router = FastPathRouter(self.service, AdapterRegistry([adapter]))
        result = router.start(
            owner_id="u", thread_id=self.thread.thread_id, node_id=node.node_id,
            executor_type="fake", source_event_id="evt-1",
        )
        second = router.start(
            owner_id="u", thread_id=self.thread.thread_id, node_id=node.node_id,
            executor_type="fake", source_event_id="evt-1",
        )
        self.assertIs(result, second)
        with self.assertRaises(RouterError):
            router.start(owner_id="u", thread_id=self.thread.thread_id, node_id=node.node_id, executor_type="unknown")

    def test_fast_path_never_calls_model(self) -> None:
        node = self.node()
        adapter = FakeAdapter(self.service)
        router = FastPathRouter(self.service, AdapterRegistry([adapter]))
        model = Mock(side_effect=AssertionError("Fast Path 不得调用模型"))
        router.model = model
        router.status(owner_id="u", thread_id=self.thread.thread_id, node_id=node.node_id, executor_type="fake")
        model.assert_not_called()

    def test_existing_slots_request_can_be_overridden(self) -> None:
        node = self.node()
        router = FastPathRouter(self.service, AdapterRegistry([FakeAdapter(self.service)]))
        request = FastPathRequest(
            owner_id="u", thread_id=self.thread.thread_id, node_id=node.node_id,
            executor_type="fake", action="status",
        )
        result = router.route(request, attempt=0)
        self.assertEqual(result.attempt, 0)

    def test_status_is_not_cached(self) -> None:
        node = self.node(executor_type="job", idempotency_key="status-job")
        jobs = _FakeJobRegistry()
        adapter = JobAdapter(self.service, jobs)
        adapter.start("u", self.thread.thread_id, node.node_id, spec=JobSpec(kind="shell", conversation_id=self.thread.thread_id, checkpoint={"cursor": 1}))
        node = self.service.get_node("u", node.node_id)
        router = FastPathRouter(self.service, AdapterRegistry([adapter]))
        first = router.status(owner_id="u", thread_id=self.thread.thread_id, node_id=node.node_id, executor_type="job", idempotency_key="status-1")
        jobs.jobs[node.executor_id]["status"] = "completed"
        second = router.status(owner_id="u", thread_id=self.thread.thread_id, node_id=node.node_id, executor_type="job", idempotency_key="status-1")
        self.assertEqual(first.status, "running")
        self.assertEqual(second.status, "completed")

    def test_persisted_artifact_refs_are_distinct_from_output_refs(self) -> None:
        node = self.node()
        adapter = FakeAdapter(self.service)
        adapter.start("u", self.thread.thread_id, node.node_id)
        adapter.complete("u", self.thread.thread_id, node.node_id, output_refs=["ref://output"])
        self.service.create_artifact("u", self.thread.thread_id, node.node_id, kind="image", path_or_ref="ref://artifact", idempotency_key="artifact-1")
        result = adapter.status("u", self.thread.thread_id, node.node_id)
        self.assertEqual(result.output_refs, ["ref://output"])
        self.assertEqual(result.artifact_refs, ["ref://artifact"])

    def test_job_adapter_maps_job_and_preserves_stopping(self) -> None:
        node = self.node(executor_type="job", idempotency_key="job-1")
        jobs = _FakeJobRegistry()
        adapter = JobAdapter(self.service, jobs)
        result = adapter.start(
            "u", self.thread.thread_id, node.node_id,
            spec=JobSpec(kind="shell", conversation_id=self.thread.thread_id, params={"x": 1}, checkpoint={"cursor": 2}),
        )
        self.assertEqual(result.executor_id, "job-1")
        self.assertEqual(len(jobs.started), 1)
        repeat = adapter.start("u", self.thread.thread_id, node.node_id)
        self.assertEqual(repeat.executor_id, result.executor_id)
        self.assertEqual(len(jobs.started), 1)
        cancelled = adapter.cancel("u", self.thread.thread_id, node.node_id)
        self.assertEqual(cancelled.status, "stopping")
        self.assertEqual(self.service.get_node("u", node.node_id).status, "stopping")

    def test_retry_fences_old_attempt(self) -> None:
        node = self.node()
        adapter = FakeAdapter(self.service)
        adapter.start("u", self.thread.thread_id, node.node_id)
        adapter.fail("u", self.thread.thread_id, node.node_id, error="bad")
        retried = adapter.retry("u", self.thread.thread_id, node.node_id)
        self.assertEqual(retried.attempt, 1)
        self.assertEqual(retried.node_status, "ready")
        self.service.emit_event(
            "u", self.thread.thread_id, "job_completed", node_id=node.node_id,
            source="late", source_event_id="late-0", idempotency_key="late-0", attempt=0,
            payload={"output_refs": ["old"]},
        )
        self.assertEqual(self.service.get_node("u", node.node_id).status, "ready")

    def test_retry_request_is_persistently_idempotent(self) -> None:
        node = self.node()
        adapter = FakeAdapter(self.service)
        adapter.start("u", self.thread.thread_id, node.node_id)
        adapter.fail("u", self.thread.thread_id, node.node_id, error="bad")
        first = adapter.retry("u", self.thread.thread_id, node.node_id, request_id="retry-1")
        second = FakeAdapter(self.service).retry("u", self.thread.thread_id, node.node_id, request_id="retry-1")
        self.assertEqual(first.attempt, 1)
        self.assertEqual(second.attempt, 1)
        self.assertEqual(self.service.get_node("u", node.node_id).attempt, 1)

    def test_job_retry_syncs_failed_job_before_advancing_node(self) -> None:
        node = self.node(executor_type="job", idempotency_key="retry-job")
        jobs = _FakeJobRegistry()
        adapter = JobAdapter(self.service, jobs)
        adapter.start("u", self.thread.thread_id, node.node_id, spec=JobSpec(kind="shell", conversation_id=self.thread.thread_id, checkpoint={"cursor": 1}))
        node = self.service.get_node("u", node.node_id)
        jobs.jobs[node.executor_id]["status"] = "failed"
        jobs.jobs[node.executor_id]["checkpoint"] = {"cursor": 1}
        result = adapter.retry("u", self.thread.thread_id, node.node_id, request_id="job-retry-1")
        self.assertEqual(result.attempt, 1)
        self.assertEqual(len(jobs.retried), 1)
        self.assertEqual(self.service.get_node("u", node.node_id).attempt, 1)

    def test_job_retry_active_job_does_not_change_node(self) -> None:
        node = self.node(executor_type="job", idempotency_key="active-job")
        jobs = _FakeJobRegistry()
        adapter = JobAdapter(self.service, jobs)
        adapter.start("u", self.thread.thread_id, node.node_id, spec=JobSpec(kind="shell", conversation_id=self.thread.thread_id, checkpoint={"cursor": 1}))
        with self.assertRaises(AdapterError):
            adapter.retry("u", self.thread.thread_id, node.node_id, request_id="job-retry-active")
        current = self.service.get_node("u", node.node_id)
        self.assertEqual(current.attempt, 0)
        self.assertEqual(current.status, "running")
        self.assertEqual(len(jobs.retried), 0)


if __name__ == "__main__":
    unittest.main()
