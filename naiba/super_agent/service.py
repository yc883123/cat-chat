from __future__ import annotations

import threading
from dataclasses import asdict
from typing import Any, Callable

from naiba.super_agent.models import (
    NODE_TERMINAL_STATUSES,
    NODE_STATUSES,
    THREAD_TERMINAL_STATUSES,
    THREAD_TRANSITIONS,
    THREAD_STATUSES,
    Approval,
    Artifact,
    TaskEvent,
    TaskNode,
    Thread,
)
from naiba.super_agent.store import SuperAgentStore


class SuperAgentService:
    """P0 deterministic task coordinator.

    It owns only the task-domain state. Existing Chat/Job/Studio managers stay
    unchanged; adapters can be added later without changing these contracts.
    """

    def __init__(self, storage_or_db: Any, *, worker: Any | None = None):
        self.store = SuperAgentStore(storage_or_db)
        self.worker = worker or FakeWorker(self)
        self._lock = threading.RLock()
        self.domain_router: Any | None = None

    def create_thread(self, owner_id: str, title: str, goal: str = "", **kwargs: Any) -> Thread:
        return self.store.create_thread(owner_id=owner_id, title=title, goal=goal, **kwargs)

    def get_thread(self, owner_id: str, thread_id: str) -> Thread:
        thread = self.store.get_thread(thread_id, owner_id=owner_id)
        if thread is None:
            raise LookupError("Thread 不存在或无权访问")
        return thread

    def list_threads(self, owner_id: str, *, include_archived: bool = False) -> list[Thread]:
        return self.store.list_threads(owner_id=owner_id, include_archived=include_archived)

    def create_node(self, owner_id: str, thread_id: str, kind: str, *, idempotency_key: str, **kwargs: Any) -> TaskNode:
        self.get_thread(owner_id, thread_id)
        return self.store.create_node(owner_id=owner_id, thread_id=thread_id, kind=kind, idempotency_key=idempotency_key, **kwargs)

    def create_thread_chain(self, owner_id: str, title: str, goal: str, *,
                            chain_instance_id: str, chain_version: int,
                            nodes: list[dict[str, Any]], **kwargs: Any) -> tuple[Thread, list[TaskNode]]:
        return self.store.create_thread_chain(
            owner_id=owner_id, title=title, goal=goal,
            chain_instance_id=chain_instance_id, chain_version=chain_version,
            nodes=nodes, **kwargs,
        )

    def get_node(self, owner_id: str, node_id: str) -> TaskNode:
        node = self.store.get_node(node_id, owner_id=owner_id)
        if node is None:
            raise LookupError("Task Node 不存在或无权访问")
        return node

    def list_nodes(self, owner_id: str, thread_id: str) -> list[TaskNode]:
        self.get_thread(owner_id, thread_id)
        return self.store.list_nodes(thread_id, owner_id=owner_id)

    def list_chain(self, owner_id: str, thread_id: str, chain_instance_id: str = "") -> list[TaskNode]:
        self.get_thread(owner_id, thread_id)
        return self.store.list_chain(thread_id, owner_id=owner_id, chain_instance_id=chain_instance_id)

    def promote_chain_node(self, owner_id: str, thread_id: str, node_id: str, *, expected_attempt: int | None = None, request_id: str = "") -> TaskNode | None:
        self.get_thread(owner_id, thread_id)
        return self.store.promote_chain_node(thread_id, node_id, owner_id=owner_id, expected_attempt=expected_attempt, request_id=request_id)

    def create_approval(self, owner_id: str, thread_id: str, node_id: str, *, action: str, description: str, idempotency_key: str, risk_level: str = "medium") -> Approval:
        node = self.get_node(owner_id, node_id)
        if node.thread_id != thread_id:
            raise ValueError("Approval 的 Node 不属于 Thread")
        approval, _, _ = self.store.create_approval_atomic(
            owner_id=owner_id, thread_id=thread_id, node_id=node_id, action=action,
            description=description, idempotency_key=idempotency_key, risk_level=risk_level,
        )
        return approval

    def resolve_approval(self, owner_id: str, thread_id: str, approval_id: str, *, status: str, resolved_by: str, request_id: str) -> Approval:
        approval = self.store.get_approval(approval_id, owner_id=owner_id)
        if approval is None or approval.thread_id != thread_id:
            raise LookupError("Approval 不存在或无权访问")
        return self.store.resolve_approval_atomic(
            approval_id, owner_id=owner_id, status=status, resolved_by=resolved_by,
            request_id=request_id,
        )

    def emit_event(self, owner_id: str, thread_id: str, event_type: str, *, node_id: str = "", source: str = "super-agent", payload: dict[str, Any] | None = None, source_event_id: str = "", idempotency_key: str = "", attempt: int = 0) -> TaskEvent:
        if node_id:
            event, _ = self.store.append_event_and_apply(
                owner_id=owner_id,
                thread_id=thread_id,
                node_id=node_id,
                source=source,
                event_type=event_type,
                payload=payload,
                source_event_id=source_event_id,
                idempotency_key=idempotency_key,
                attempt=attempt,
            )
            return event
        event, _ = self.store.append_event(
            owner_id=owner_id,
            thread_id=thread_id,
            node_id=node_id,
            source=source,
            event_type=event_type,
            payload=payload,
            source_event_id=source_event_id,
            idempotency_key=idempotency_key,
            attempt=attempt,
        )
        return event

    def events(self, owner_id: str, thread_id: str, *, after: int = 0) -> list[TaskEvent]:
        self.get_thread(owner_id, thread_id)
        return self.store.list_events(thread_id, owner_id=owner_id, after=after)

    def _apply_event(self, owner_id: str, event: TaskEvent) -> None:
        if not event.node_id:
            return
        node = self.store.get_node(event.node_id, owner_id=owner_id)
        if node is None or node.thread_id != event.thread_id:
            return
        if event.attempt != node.attempt:
            return
        status_by_event = {
            "run_started": "running",
            "progress": "running",
            "job_completed": "succeeded",
            "run_completed": "succeeded",
            "job_failed": "failed",
            "task_cancelled": "cancelled",
            "run_interrupted": "interrupted",
        }
        next_status = status_by_event.get(event.event_type)
        if next_status is None:
            return
        if node.status in NODE_TERMINAL_STATUSES and next_status != node.status:
            return
        if next_status == "succeeded" and node.status == "succeeded":
            return
        updates: dict[str, Any] = {"status": next_status}
        if next_status == "failed":
            updates["error"] = str(event.payload.get("error") or "任务失败")
        if next_status == "succeeded":
            outputs = event.payload.get("output_refs")
            if isinstance(outputs, list):
                updates["output_refs"] = outputs
        self.store.update_node(node.node_id, owner_id=owner_id, expected_attempt=node.attempt, **updates)
        if next_status == "failed":
            thread = self.get_thread(owner_id, event.thread_id)
            if thread.status not in THREAD_TERMINAL_STATUSES and thread.status not in {"paused", "cancelled"}:
                self.store.update_thread(event.thread_id, owner_id=owner_id, status="failed")
        elif next_status == "interrupted":
            thread = self.get_thread(owner_id, event.thread_id)
            if thread.status == "active":
                self.store.update_thread(event.thread_id, owner_id=owner_id, status="needs_recovery", summary="服务中断，任务需恢复")
        elif next_status == "succeeded":
            if node.chain_instance_id:
                self.store.promote_chain_node(
                    event.thread_id, node.node_id, owner_id=owner_id,
                    expected_attempt=node.attempt, request_id=event.idempotency_key,
                )
            else:
                self.store.promote_ready_nodes(event.thread_id, owner_id=owner_id)
            nodes = self.store.list_nodes(event.thread_id, owner_id=owner_id)
            if nodes and all(item.status in {"succeeded", "cancelled"} for item in nodes):
                thread = self.get_thread(owner_id, event.thread_id)
                if thread.status not in THREAD_TERMINAL_STATUSES:
                    self.store.update_thread(event.thread_id, owner_id=owner_id, status="completed")

    def start_node(self, owner_id: str, node_id: str) -> TaskNode:
        node = self.get_node(owner_id, node_id)
        if node.status != "ready":
            raise RuntimeError(f"Node 当前不能启动: {node.status}")
        thread = self.get_thread(owner_id, node.thread_id)
        if thread.status in THREAD_TERMINAL_STATUSES or thread.status in {"paused", "cancelled"}:
            raise RuntimeError(f"Thread 当前不能启动: {thread.status}")
        self.store.update_node(node_id, owner_id=owner_id, status="running", expected_attempt=node.attempt)
        self.emit_event(owner_id, node.thread_id, "run_started", node_id=node_id, payload={"attempt": node.attempt}, idempotency_key=f"start:{node_id}:{node.attempt}", attempt=node.attempt)
        self.worker.start(owner_id, node_id, node.attempt)
        return self.get_node(owner_id, node_id)

    def pause_thread(self, owner_id: str, thread_id: str, *, request_id: str) -> Thread:
        if not request_id:
            raise ValueError("暂停操作必须提供 request_id")
        thread = self.get_thread(owner_id, thread_id)
        if thread.status in THREAD_TERMINAL_STATUSES or thread.status == "cancelled":
            raise RuntimeError("终态 Thread 不能暂停")
        for node in self.store.list_nodes(thread_id, owner_id=owner_id):
            if node.status == "running":
                self.worker.pause(owner_id, node.node_id, node.attempt)
        result = self.store.update_thread(thread_id, owner_id=owner_id, status="paused")
        self.emit_event(owner_id, thread_id, "task_paused", payload={"request_id": request_id}, idempotency_key=f"pause:{request_id}")
        return result

    def resume_thread(self, owner_id: str, thread_id: str, *, request_id: str) -> Thread:
        if not request_id:
            raise ValueError("继续操作必须提供 request_id")
        thread = self.get_thread(owner_id, thread_id)
        if thread.status != "paused":
            raise RuntimeError("只有 paused Thread 可以继续")
        result = self.store.update_thread(thread_id, owner_id=owner_id, status="active")
        for node in self.store.list_nodes(thread_id, owner_id=owner_id):
            if node.status == "running":
                self.worker.resume(owner_id, node.node_id, node.attempt)
        for event in self.store.list_unhandled_node_events(thread_id, owner_id=owner_id):
            self._apply_event(owner_id, event)
            self.store.mark_event_handled(event.event_id, owner_id=owner_id)
        self.emit_event(owner_id, thread_id, "task_resumed", payload={"request_id": request_id}, idempotency_key=f"resume:{request_id}")
        return self.get_thread(owner_id, thread_id)

    # 删除＝软删除（统一实施计划 §5.1 定稿）：不动产物文件、批次、事件、审批
    # 与关联会话，保留执行事实；运行中 / 停止中必须先取消再删。
    _ARCHIVABLE_STATUSES = frozenset({"draft", "waiting_user", "paused", "failed", "needs_recovery", "completed", "cancelled"})

    def archive_thread(self, owner_id: str, thread_id: str, *, request_id: str, expected_version: int | None = None) -> Thread:
        if not request_id:
            raise ValueError("删除操作必须提供 request_id")
        thread = self.get_thread(owner_id, thread_id)
        if thread.status == "archived":
            return thread
        if thread.status in {"active", "stopping"}:
            raise RuntimeError("任务正在运行或停止中；请先取消再删除")
        if thread.status not in self._ARCHIVABLE_STATUSES:
            raise RuntimeError(f"当前状态 {thread.status} 不能删除")
        result = self.store.update_thread(thread_id, owner_id=owner_id, status="archived",
                                         expected_version=thread.version if expected_version is None else expected_version)
        self.emit_event(owner_id, thread_id, "task_archived",
                        payload={"request_id": request_id, "previous_status": thread.status},
                        idempotency_key=f"archive:{request_id}")
        return result

    def restore_thread(self, owner_id: str, thread_id: str, *, request_id: str) -> Thread:
        """恢复：按最近一次归档事件回到归档前状态；不得绕过状态机直接改库。"""
        if not request_id:
            raise ValueError("恢复操作必须提供 request_id")
        thread = self.get_thread(owner_id, thread_id)
        if thread.status != "archived":
            return thread
        previous = "cancelled"
        for event in self.events(owner_id, thread_id):
            if event.event_type == "task_archived" and isinstance(event.payload, dict):
                candidate = str(event.payload.get("previous_status") or "")
                if candidate in THREAD_TRANSITIONS.get("archived", set()):
                    previous = candidate
        result = self.store.update_thread(thread_id, owner_id=owner_id, status=previous)
        self.emit_event(owner_id, thread_id, "task_restored",
                        payload={"request_id": request_id, "status": previous},
                        idempotency_key=f"restore:{request_id}")
        return self.get_thread(owner_id, thread_id)

    def cancel_thread(self, owner_id: str, thread_id: str, *, request_id: str) -> Thread:
        if not request_id:
            raise ValueError("取消操作必须提供 request_id")
        thread = self.get_thread(owner_id, thread_id)
        if thread.status in THREAD_TERMINAL_STATUSES or thread.status == "cancelled":
            return thread
        nodes = self.store.list_nodes(thread_id, owner_id=owner_id)
        for node in nodes:
            if node.status in {"pending", "ready", "waiting_user"}:
                self.store.update_node(node.node_id, owner_id=owner_id, status="cancelled", cancel_requested=True, expected_attempt=node.attempt)
            elif node.status == "running":
                self.store.update_node(node.node_id, owner_id=owner_id, status="stopping", cancel_requested=True, expected_attempt=node.attempt)
                self.worker.cancel(owner_id, node.node_id, node.attempt)
        result = self.store.update_thread(thread_id, owner_id=owner_id, status="cancelled", summary="用户取消")
        self.emit_event(owner_id, thread_id, "task_cancelled", payload={"request_id": request_id}, idempotency_key=f"cancel:{request_id}")
        for node in self.store.list_nodes(thread_id, owner_id=owner_id):
            if node.status == "stopping":
                self.worker.confirm_cancel(owner_id, node.node_id, node.attempt)
        return self.get_thread(owner_id, thread_id)

    def retry_node(self, owner_id: str, node_id: str) -> TaskNode:
        node = self.get_node(owner_id, node_id)
        if node.status not in {"failed", "cancelled", "interrupted"}:
            raise RuntimeError("只有 failed/cancelled/interrupted Node 可以重试")
        result = self.store.update_node(node_id, owner_id=owner_id, status="ready", attempt=node.attempt + 1, cancel_requested=False, error="", expected_attempt=node.attempt)
        self.store.update_thread(result.thread_id, owner_id=owner_id, status="active")
        return result

    def recover(self, owner_id: str) -> list[Thread]:
        return self.store.recover(owner_id=owner_id)

    def recover_all(self) -> list[Thread]:
        return self.store.recover_all()

    def shutdown(self) -> None:
        shutdown = getattr(self.worker, "shutdown", None)
        if callable(shutdown):
            shutdown()

    def create_artifact(self, owner_id: str, thread_id: str, node_id: str, **kwargs: Any) -> Artifact:
        return self.store.create_artifact(owner_id=owner_id, thread_id=thread_id, node_id=node_id, **kwargs)

    def artifacts(self, owner_id: str, thread_id: str) -> list[Artifact]:
        self.get_thread(owner_id, thread_id)
        return self.store.list_artifacts(thread_id, owner_id=owner_id)

    def approvals(self, owner_id: str, thread_id: str) -> list[Approval]:
        self.get_thread(owner_id, thread_id)
        return self.store.list_approvals(thread_id, owner_id=owner_id)


class FakeWorker:
    """Deterministic, model-free Worker used by the P0 contract tests."""

    def __init__(self, service: SuperAgentService):
        self.service = service
        self._cancelled: set[tuple[str, str, int]] = set()
        self._paused: set[tuple[str, str, int]] = set()

    def start(self, owner_id: str, node_id: str, attempt: int) -> None:
        node = self.service.get_node(owner_id, node_id)
        self.service.emit_event(owner_id, node.thread_id, "progress", node_id=node_id, source="fake-worker", payload={"step": "started"}, source_event_id=f"fake-start:{node_id}:{attempt}", idempotency_key=f"fake-progress:{node_id}:{attempt}", attempt=attempt)

    def complete(self, owner_id: str, node_id: str, *, output_refs: list[Any] | None = None, source_event_id: str = "") -> TaskEvent:
        node = self.service.get_node(owner_id, node_id)
        return self.service.emit_event(owner_id, node.thread_id, "job_completed", node_id=node_id, source="fake-worker", payload={"output_refs": output_refs or []}, source_event_id=source_event_id or f"fake-complete:{node_id}:{node.attempt}", idempotency_key=f"fake-complete:{node_id}:{node.attempt}", attempt=node.attempt)

    def fail(self, owner_id: str, node_id: str, error: str = "假 Worker 失败") -> TaskEvent:
        node = self.service.get_node(owner_id, node_id)
        return self.service.emit_event(owner_id, node.thread_id, "job_failed", node_id=node_id, source="fake-worker", payload={"error": error}, source_event_id=f"fake-fail:{node_id}:{node.attempt}", idempotency_key=f"fake-fail:{node_id}:{node.attempt}", attempt=node.attempt)

    def cancel(self, owner_id: str, node_id: str, attempt: int) -> None:
        self._cancelled.add((owner_id, node_id, attempt))

    def confirm_cancel(self, owner_id: str, node_id: str, attempt: int) -> None:
        node = self.service.get_node(owner_id, node_id)
        if node.status == "stopping" and node.attempt == attempt:
            self.service.store.update_node(node_id, owner_id=owner_id, status="cancelled", expected_attempt=attempt)

    def pause(self, owner_id: str, node_id: str, attempt: int) -> None:
        self._paused.add((owner_id, node_id, attempt))

    def resume(self, owner_id: str, node_id: str, attempt: int) -> None:
        self._paused.discard((owner_id, node_id, attempt))
