from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from naiba.jobs import JobSpec
from naiba.super_agent.models import NODE_TERMINAL_STATUSES, TaskNode
from naiba.super_agent.service import SuperAgentService


class AdapterError(RuntimeError):
    """A deterministic adapter operation could not be applied."""


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    """Capabilities exposed by an executor without exposing its implementation."""

    can_start: bool = True
    can_cancel: bool = True
    can_retry: bool = False
    can_resume: bool = False
    has_checkpoint: bool = True
    produces_artifacts: bool = True


@dataclass(slots=True)
class AdapterResult:
    """Normalized execution view returned by adapters and the Fast Path."""

    executor_type: str
    executor_id: str
    status: str
    attempt: int
    checkpoint: dict[str, Any] = field(default_factory=dict)
    output_refs: list[Any] = field(default_factory=list)
    artifact_refs: list[Any] = field(default_factory=list)
    node_status: str = ""
    raw: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "executor_type": self.executor_type,
            "executor_id": self.executor_id,
            "status": self.status,
            "attempt": self.attempt,
            "checkpoint": dict(self.checkpoint),
            "output_refs": list(self.output_refs),
            "artifact_refs": list(self.artifact_refs),
            "node_status": self.node_status,
            "raw": self.raw,
        }


class BaseAdapter:
    """Small execution mapping layer over the P0 task domain.

    Adapters do not own task state. They validate the P0 owner/thread/node
    relationship and delegate state changes to ``SuperAgentService`` or the
    existing executor manager.
    """

    executor_type = ""
    capabilities = AdapterCapabilities()

    def __init__(self, service: SuperAgentService):
        self.service = service

    def _node(
        self,
        owner_id: str,
        thread_id: str,
        node_id: str,
        *,
        executor_id: str = "",
        attempt: int | None = None,
    ) -> TaskNode:
        self.service.get_thread(owner_id, thread_id)
        node = self.service.get_node(owner_id, node_id)
        if node.thread_id != thread_id:
            raise LookupError("Task Node 不属于当前 Thread")
        if self.executor_type and node.executor_type != self.executor_type:
            raise AdapterError(
                f"Node executor_type 不匹配: {node.executor_type} != {self.executor_type}"
            )
        if attempt is not None and node.attempt != attempt:
            raise AdapterError("Node attempt 已过期")
        if executor_id and node.executor_id and node.executor_id != executor_id:
            raise AdapterError("executor_id 与当前 Node 不匹配")
        return node

    def _result(self, node: TaskNode, *, status: str | None = None, raw: Any = None) -> AdapterResult:
        artifacts = self.service.artifacts(node.owner_id, node.thread_id)
        artifact_refs = [item.path_or_ref for item in artifacts if item.node_id == node.node_id]
        return AdapterResult(
            executor_type=self.executor_type or node.executor_type,
            executor_id=node.executor_id,
            status=status or node.status,
            attempt=node.attempt,
            checkpoint=dict(node.checkpoint or {}),
            output_refs=list(node.output_refs or []),
            artifact_refs=artifact_refs,
            node_status=node.status,
            raw=raw,
        )

    @staticmethod
    def _action_key(request_id: str = "", idempotency_key: str = "") -> str:
        return str(request_id or idempotency_key or "").strip()

    @staticmethod
    def _command_result(node: TaskNode, action: str, key: str) -> AdapterResult | None:
        if not key:
            return None
        checkpoint = node.checkpoint if isinstance(node.checkpoint, dict) else {}
        commands = checkpoint.get("_adapter_commands")
        record = commands.get(action, {}).get(key) if isinstance(commands, dict) and isinstance(commands.get(action), dict) else None
        if not isinstance(record, dict):
            return None
        return AdapterResult(
            executor_type=str(record.get("executor_type") or node.executor_type),
            executor_id=str(record.get("executor_id") or node.executor_id),
            status=str(record.get("status") or node.status),
            attempt=int(record.get("attempt", node.attempt)),
            checkpoint=dict(record.get("checkpoint") or {}),
            output_refs=list(record.get("output_refs") or []),
            artifact_refs=list(record.get("artifact_refs") or []),
            node_status=str(record.get("node_status") or node.status),
            raw=record.get("raw"),
        )

    def _remember_command(self, node: TaskNode, action: str, key: str, result: AdapterResult) -> AdapterResult:
        if not key:
            return result
        current = self.service.get_node(node.owner_id, node.node_id)
        checkpoint = dict(current.checkpoint or {})
        node = current
        commands = checkpoint.get("_adapter_commands")
        commands = {str(k): dict(v) for k, v in commands.items()} if isinstance(commands, dict) else {}
        action_commands = commands.get(action)
        action_commands = dict(action_commands) if isinstance(action_commands, dict) else {}
        action_commands[key] = {
            "executor_type": result.executor_type,
            "executor_id": result.executor_id,
            "status": result.status,
            "attempt": result.attempt,
            "checkpoint": dict(result.checkpoint),
            "output_refs": list(result.output_refs),
            "artifact_refs": list(result.artifact_refs),
            "node_status": result.node_status,
            "raw": result.raw,
        }
        commands[action] = action_commands
        checkpoint["_adapter_commands"] = commands
        updated = self.service.store.update_node(
            node.node_id,
            owner_id=node.owner_id,
            checkpoint=checkpoint,
            expected_attempt=node.attempt,
        )
        return self._result(updated, status=result.status, raw=result.raw)

    def _start_event(
        self,
        node: TaskNode,
        *,
        source: str,
        source_event_id: str = "",
        idempotency_key: str = "",
    ) -> None:
        key = idempotency_key or f"{source}:start:{node.node_id}:{node.attempt}"
        source_id = source_event_id or key
        self.service.emit_event(
            node.owner_id,
            node.thread_id,
            "run_started",
            node_id=node.node_id,
            source=source,
            payload={"attempt": node.attempt, "executor_id": node.executor_id},
            source_event_id=source_id,
            idempotency_key=key,
            attempt=node.attempt,
        )

    def start(self, owner_id: str, thread_id: str, node_id: str, **kwargs: Any) -> AdapterResult:
        raise NotImplementedError

    def status(self, owner_id: str, thread_id: str, node_id: str, **kwargs: Any) -> AdapterResult:
        raise NotImplementedError

    def cancel(self, owner_id: str, thread_id: str, node_id: str, **kwargs: Any) -> AdapterResult:
        raise NotImplementedError

    def retry(self, owner_id: str, thread_id: str, node_id: str, **kwargs: Any) -> AdapterResult:
        raise NotImplementedError

    def resume(self, owner_id: str, thread_id: str, node_id: str, **kwargs: Any) -> AdapterResult:
        raise NotImplementedError

    def checkpoint(self, owner_id: str, thread_id: str, node_id: str, **kwargs: Any) -> dict[str, Any]:
        return dict(self._node(owner_id, thread_id, node_id).checkpoint or {})

    def artifacts(self, owner_id: str, thread_id: str, node_id: str, **kwargs: Any) -> list[Any]:
        node = self._node(owner_id, thread_id, node_id)
        return [item.path_or_ref for item in self.service.artifacts(owner_id, thread_id) if item.node_id == node.node_id]


class FakeAdapter(BaseAdapter):
    """Adapter for the P0 model-free ``FakeWorker``."""

    executor_type = "fake"
    capabilities = AdapterCapabilities(can_retry=True, can_resume=True)

    def start(
        self,
        owner_id: str,
        thread_id: str,
        node_id: str,
        *,
        attempt: int | None = None,
        source_event_id: str = "",
        idempotency_key: str = "",
        **_: Any,
    ) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id, attempt=attempt)
        if node.status == "running" and node.executor_id:
            return self._result(node)
        if node.status != "ready":
            raise AdapterError(f"Node 当前不能启动: {node.status}")
        expected_attempt = node.attempt
        executor_id = f"fake:{node.node_id}:{expected_attempt}"
        node = self.service.store.update_node(
            node.node_id,
            owner_id=owner_id,
            executor_id=executor_id,
            checkpoint={**(node.checkpoint or {}), "executor_id": executor_id},
            expected_attempt=expected_attempt,
        )
        self.service.start_node(owner_id, node.node_id)
        node = self.service.get_node(owner_id, node.node_id)
        return self._result(node)

    def status(self, owner_id: str, thread_id: str, node_id: str, **_: Any) -> AdapterResult:
        return self._result(self._node(owner_id, thread_id, node_id))

    def complete(
        self,
        owner_id: str,
        thread_id: str,
        node_id: str,
        *,
        output_refs: Iterable[Any] | None = None,
        source_event_id: str = "",
    ) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id)
        worker = self.service.worker
        complete = getattr(worker, "complete", None)
        if not callable(complete):
            raise AdapterError("当前 Worker 不支持完成操作")
        complete(owner_id, node_id, output_refs=list(output_refs or []), source_event_id=source_event_id)
        return self.status(owner_id, thread_id, node_id)

    def fail(self, owner_id: str, thread_id: str, node_id: str, *, error: str = "假 Worker 失败") -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id)
        fail = getattr(self.service.worker, "fail", None)
        if not callable(fail):
            raise AdapterError("当前 Worker 不支持失败操作")
        fail(owner_id, node_id, error)
        return self.status(owner_id, thread_id, node_id)

    def cancel(
        self,
        owner_id: str,
        thread_id: str,
        node_id: str,
        *,
        attempt: int | None = None,
        request_id: str = "",
        idempotency_key: str = "",
        **_: Any,
    ) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id, attempt=attempt)
        command_key = self._action_key(request_id, idempotency_key)
        previous = self._command_result(node, "cancel", command_key)
        if previous is not None:
            return previous
        if node.status in NODE_TERMINAL_STATUSES:
            result = self._result(node)
            return self._remember_command(node, "cancel", command_key, result)
        if node.status in {"pending", "ready", "waiting_user"}:
            node = self.service.store.update_node(
                node.node_id,
                owner_id=owner_id,
                status="cancelled",
                cancel_requested=True,
                expected_attempt=node.attempt,
            )
            return self._remember_command(node, "cancel", command_key, self._result(node))
        if node.status == "running":
            self.service.store.update_node(
                node.node_id,
                owner_id=owner_id,
                status="stopping",
                cancel_requested=True,
                expected_attempt=node.attempt,
            )
            self.service.worker.cancel(owner_id, node.node_id, node.attempt)
            self.service.worker.confirm_cancel(owner_id, node.node_id, node.attempt)
        node = self.service.get_node(owner_id, node_id)
        return self._remember_command(node, "cancel", command_key, self._result(node))

    def retry(self, owner_id: str, thread_id: str, node_id: str, *, request_id: str = "", idempotency_key: str = "", **_: Any) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id)
        key = self._action_key(request_id, idempotency_key)
        previous = self._command_result(node, "retry", key)
        if previous is not None:
            return previous
        node = self.service.retry_node(owner_id, node_id)
        result = self._result(node)
        return self._remember_command(node, "retry", key, result)

    def resume(self, owner_id: str, thread_id: str, node_id: str, *, request_id: str = "", idempotency_key: str = "", **_: Any) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id)
        key = self._action_key(request_id, idempotency_key)
        previous = self._command_result(node, "resume", key)
        if previous is not None:
            return previous
        if node.status != "interrupted":
            raise AdapterError(f"Node 当前不能恢复: {node.status}")
        node = self.service.store.update_node(
            node.node_id,
            owner_id=owner_id,
            status="ready",
            expected_attempt=node.attempt,
        )
        return self._remember_command(node, "resume", key, self._result(node))


class JobAdapter(BaseAdapter):
    """Adapter that delegates execution to the existing ``JobRegistry``."""

    executor_type = "job"
    capabilities = AdapterCapabilities(can_retry=True, can_resume=True)

    def __init__(self, service: SuperAgentService, registry: Any):
        super().__init__(service)
        self.registry = registry

    def _job_snapshot(self, node: TaskNode, owner_id: str) -> dict[str, Any]:
        if not node.executor_id:
            raise AdapterError("Node 尚未绑定 Job")
        snapshot = self.registry.get(node.executor_id, owner=owner_id)
        if snapshot is None:
            raise LookupError("Job 不存在或无权访问")
        return snapshot

    @staticmethod
    def _job_status(snapshot: dict[str, Any]) -> str:
        return str(snapshot.get("status") or "unknown")

    def start(
        self,
        owner_id: str,
        thread_id: str,
        node_id: str,
        *,
        spec: JobSpec | dict[str, Any] | None = None,
        source_event_id: str = "",
        idempotency_key: str = "",
        attempt: int | None = None,
        **fields: Any,
    ) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id, attempt=attempt)
        if node.executor_id:
            snapshot = self._job_snapshot(node, owner_id)
            return self._result(node, status=self._job_status(snapshot), raw=snapshot)
        if node.status != "ready":
            raise AdapterError(f"Node 当前不能启动: {node.status}")
        if spec is None:
            spec = fields.pop("job_spec", None)
        if isinstance(spec, dict):
            spec = JobSpec(**spec)
        if spec is None:
            params = fields.pop("params", None)
            if not isinstance(params, dict):
                params = {}
            spec = JobSpec(
                kind=str(fields.pop("kind", node.kind)),
                conversation_id=str(fields.pop("conversation_id", thread_id)),
                params=params,
                label=str(fields.pop("label", "")),
                parent_job_id=fields.pop("parent_job_id", None),
                owner_session_id=owner_id,
                resumable=bool(fields.pop("resumable", True)),
                checkpoint=fields.pop("checkpoint", node.checkpoint),
                detached=bool(fields.pop("detached", False)),
                origin_run_id=str(fields.pop("origin_run_id", "")),
            )
        else:
            spec.owner_session_id = owner_id
        job_id = str(self.registry.start(spec, owner=owner_id))
        checkpoint = dict(node.checkpoint or {})
        checkpoint["job_id"] = job_id
        checkpoint.setdefault("job_checkpoint", dict(spec.checkpoint or {}))
        node = self.service.store.update_node(
            node.node_id,
            owner_id=owner_id,
            executor_id=job_id,
            checkpoint=checkpoint,
            expected_attempt=node.attempt,
        )
        self._start_event(
            node,
            source="job-adapter",
            source_event_id=source_event_id,
            idempotency_key=idempotency_key,
        )
        node = self.service.get_node(owner_id, node.node_id)
        snapshot = self.registry.get(job_id, owner=owner_id)
        return self._result(node, status=self._job_status(snapshot or {}), raw=snapshot)

    def status(self, owner_id: str, thread_id: str, node_id: str, **_: Any) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id)
        snapshot = self._job_snapshot(node, owner_id)
        return self._result(node, status=self._job_status(snapshot), raw=snapshot)

    def cancel(
        self,
        owner_id: str,
        thread_id: str,
        node_id: str,
        *,
        attempt: int | None = None,
        reason: str | None = None,
        request_id: str = "",
        idempotency_key: str = "",
        **_: Any,
    ) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id, attempt=attempt)
        command_key = self._action_key(request_id, idempotency_key)
        previous = self._command_result(node, "cancel", command_key)
        if previous is not None:
            return previous
        snapshot = self._job_snapshot(node, owner_id)
        if self._job_status(snapshot) not in {"completed", "failed", "cancelled", "interrupted"}:
            result = self.registry.cancel(node.executor_id, owner=owner_id, reason=reason)
            if result is None:
                raise LookupError("Job 不存在或无权访问")
            snapshot = result
            if self._job_status(snapshot) not in {"completed", "failed", "cancelled", "interrupted"}:
                if node.status == "running":
                    node = self.service.store.update_node(
                        node.node_id,
                        owner_id=owner_id,
                        status="stopping",
                        cancel_requested=True,
                        expected_attempt=node.attempt,
                    )
        result = self._result(node, status=self._job_status(snapshot), raw=snapshot)
        return self._remember_command(node, "cancel", command_key, result)

    def _replace_job(self, owner_id: str, node: TaskNode, new_job_id: str) -> AdapterResult:
        checkpoint = dict(node.checkpoint or {})
        checkpoint["job_id"] = new_job_id
        node = self.service.store.update_node(
            node.node_id,
            owner_id=owner_id,
            status="running",
            executor_id=new_job_id,
            checkpoint=checkpoint,
            expected_attempt=node.attempt,
        )
        self._start_event(node, source="job-adapter", idempotency_key=f"job-start:{node.node_id}:{node.attempt}")
        snapshot = self.registry.get(new_job_id, owner=owner_id)
        return self._result(node, status=self._job_status(snapshot or {}), raw=snapshot)

    def retry(self, owner_id: str, thread_id: str, node_id: str, *, request_id: str = "", idempotency_key: str = "", **_: Any) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id)
        key = self._action_key(request_id, idempotency_key)
        previous = self._command_result(node, "retry", key)
        if previous is not None:
            return previous
        old_job_id = node.executor_id
        if not old_job_id:
            raise AdapterError("Node 没有可重试的 Job")
        snapshot = self._job_snapshot(node, owner_id)
        status = self._job_status(snapshot)
        if status == "failed":
            self.sync(owner_id, thread_id, node_id)
            node = self._node(owner_id, thread_id, node_id)
        elif status == "cancelled" and node.status != "cancelled":
            self.sync(owner_id, thread_id, node_id)
            node = self._node(owner_id, thread_id, node_id)
        elif status in {"queued", "running", "waiting", "stopping"}:
            raise AdapterError(f"Job 当前不能重试: {status}")
        new_job_id = self.registry.retry(old_job_id, owner=owner_id)
        if not new_job_id:
            raise AdapterError("Job 没有返回新的执行句柄")
        node = self.service.retry_node(owner_id, node_id)
        result = self._replace_job(owner_id, node, str(new_job_id))
        return self._remember_command(node, "retry", key, result)

    def resume(self, owner_id: str, thread_id: str, node_id: str, *, request_id: str = "", idempotency_key: str = "", **_: Any) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id)
        key = self._action_key(request_id, idempotency_key)
        previous = self._command_result(node, "resume", key)
        if previous is not None:
            return previous
        if node.status not in {"interrupted", "failed", "cancelled"}:
            raise AdapterError(f"Node 当前不能恢复: {node.status}")
        if not node.executor_id:
            raise AdapterError("Node 没有可恢复的 Job")
        new_job_id = self.registry.resume(node.executor_id, owner=owner_id)
        if not new_job_id:
            raise AdapterError("Job 没有可用 checkpoint，无法恢复")
        if node.status in {"failed", "cancelled"}:
            node = self.service.retry_node(owner_id, node_id)
        else:
            node = self.service.store.update_node(
                node.node_id,
                owner_id=owner_id,
                status="ready",
                expected_attempt=node.attempt,
            )
        result = self._replace_job(owner_id, node, str(new_job_id))
        return self._remember_command(node, "resume", key, result)

    def sync(self, owner_id: str, thread_id: str, node_id: str, **_: Any) -> AdapterResult:
        """Apply a known Job terminal result through the P0 event gate."""
        node = self._node(owner_id, thread_id, node_id)
        snapshot = self._job_snapshot(node, owner_id)
        status = self._job_status(snapshot)
        if status == "completed":
            result = snapshot.get("result") if isinstance(snapshot.get("result"), dict) else {}
            outputs = result.get("output_refs") if isinstance(result.get("output_refs"), list) else []
            self.service.emit_event(
                owner_id,
                thread_id,
                "job_completed",
                node_id=node_id,
                source="job-adapter",
                payload={"output_refs": outputs, "job_id": node.executor_id},
                source_event_id=f"job-completed:{node.executor_id}",
                idempotency_key=f"job-completed:{node.executor_id}",
                attempt=node.attempt,
            )
        elif status == "failed":
            self.service.emit_event(
                owner_id,
                thread_id,
                "job_failed",
                node_id=node_id,
                source="job-adapter",
                payload={"error": snapshot.get("error") or "Job 失败", "job_id": node.executor_id},
                source_event_id=f"job-failed:{node.executor_id}",
                idempotency_key=f"job-failed:{node.executor_id}",
                attempt=node.attempt,
            )
        elif status == "cancelled" and node.status == "stopping":
            self.service.store.update_node(
                node.node_id,
                owner_id=owner_id,
                status="cancelled",
                expected_attempt=node.attempt,
            )
        return self.status(owner_id, thread_id, node_id)
