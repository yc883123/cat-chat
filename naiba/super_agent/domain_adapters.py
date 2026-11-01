from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from naiba.super_agent.adapters import AdapterCapabilities, AdapterError, AdapterResult, BaseAdapter
from naiba.super_agent.models import NODE_TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class DomainRef:
    domain: str
    book_id: str
    stage: str
    domain_task_id: str = ""
    domain_revision: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "book_id": self.book_id,
            "stage": self.stage,
            "domain_task_id": self.domain_task_id,
            "domain_revision": self.domain_revision,
        }


class DomainAdapter(BaseAdapter):
    """Base for explicit Writer/Novel adapters.

    Domain records remain the source of truth.  The Node checkpoint only keeps
    a small, replayable request and a reference to the domain task.
    """

    capabilities = AdapterCapabilities(can_retry=True, can_resume=True)

    def _ref(self, node) -> dict[str, Any]:
        checkpoint = node.checkpoint if isinstance(node.checkpoint, dict) else {}
        ref = checkpoint.get("domain_ref")
        if not isinstance(ref, dict):
            raise AdapterError("领域 Node 缺少 domain_ref")
        for key in ("domain", "book_id", "stage"):
            if not str(ref.get(key) or "").strip():
                raise AdapterError(f"领域 Node 缺少 {key}")
        return ref

    def _checkpoint_result(self, node, *, status: str | None = None, raw: Any = None) -> AdapterResult:
        return self._result(node, status=status or node.status, raw=raw)

    @staticmethod
    def _node_status(domain_status: str) -> str:
        return {
            "queued": "running", "running": "running", "waiting": "running",
            "done": "succeeded", "completed": "succeeded", "succeeded": "succeeded",
            "failed": "failed", "cancelled": "cancelled", "interrupted": "interrupted",
            "conflict": "failed",
        }.get(domain_status, "running")

    def _apply_terminal(self, owner_id: str, node, domain_status: str, *, raw: Any = None) -> None:
        target = self._node_status(domain_status)
        if target == "running" or node.status in NODE_TERMINAL_STATUSES:
            return
        event_type = (
            "run_completed" if target == "succeeded"
            else "job_failed" if target == "failed"
            else "run_interrupted" if target == "interrupted"
            else "task_cancelled"
        )
        payload: dict[str, Any] = {"domain_status": domain_status}
        if target == "failed":
            payload["error"] = str((raw or {}).get("error") or "领域任务失败") if isinstance(raw, dict) else "领域任务失败"
        self.service.emit_event(
            owner_id, node.thread_id, event_type, node_id=node.node_id,
            source=self.executor_type, payload=payload,
            source_event_id=f"{self.executor_type}:{node.node_id}:{node.attempt}:{domain_status}",
            idempotency_key=f"{self.executor_type}:terminal:{node.node_id}:{node.attempt}:{domain_status}",
            attempt=node.attempt,
        )
        if target in {"succeeded", "failed"}:
            ref = self._ref(node)
            self.service.create_artifact(
                owner_id, node.thread_id, node.node_id,
                kind=f"{ref['domain']}_{ref['stage']}",
                path_or_ref=f"{ref['domain']}://{ref['book_id']}/{ref['stage']}/{ref.get('domain_task_id') or node.executor_id}",
                title=f"{ref['domain']} {ref['stage']} 结果",
                summary=f"阶段状态：{domain_status}；仅保留引用，不复制正文",
                idempotency_key=f"artifact:{self.executor_type}:{node.node_id}:{node.attempt}",
            )



class WriterPipelineAdapter(DomainAdapter):
    executor_type = "writer_pipeline"

    def _pipeline(self, owner_id: str, node) -> tuple[dict[str, Any], dict[str, Any]]:
        ref = self._ref(node)
        tick = getattr(self.app.writer.pipeline, "tick", None)
        if callable(tick):
            tick()
        payload, status = self.app.writer.api_get_pipeline(ref["book_id"])
        if status >= 400:
            raise AdapterError(str(payload.get("error") or "读取 Writer 流水线失败"))
        pipeline = payload.get("pipeline") if isinstance(payload.get("pipeline"), dict) else {}
        return ref, pipeline

    def __init__(self, service, app):
        super().__init__(service)
        self.app = app

    def start(self, owner_id: str, thread_id: str, node_id: str, *, attempt: int | None = None, **kwargs: Any) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id, attempt=attempt)
        key = self._action_key(kwargs.get("request_id"), kwargs.get("idempotency_key"))
        previous = self._command_result(node, "start", key)
        if previous is not None:
            return previous
        if node.status == "running":
            return self._checkpoint_result(node)
        if node.status != "ready":
            raise AdapterError(f"Node 当前不能启动: {node.status}")
        ref = self._ref(node)
        checkpoint = dict(node.checkpoint or {})
        request = checkpoint.get("start_request") if isinstance(checkpoint.get("start_request"), dict) else {}
        payload, status = self.app.writer.api_start_pipeline(ref["book_id"], request)
        if status >= 400:
            raise AdapterError(str(payload.get("error") or "启动 Writer 流水线失败"))
        pipeline = payload.get("pipeline") if isinstance(payload.get("pipeline"), dict) else {}
        ref = {**ref, "domain_task_id": str(pipeline.get("current_task_id") or ""), "domain_revision": int(pipeline.get("base_revision") or 0)}
        checkpoint["domain_ref"] = ref
        checkpoint["pipeline_id"] = str(pipeline.get("id") or "")
        node = self.service.store.update_node(
            node.node_id, owner_id=owner_id, status="running",
            executor_id=str(pipeline.get("current_task_id") or pipeline.get("id") or ""),
            checkpoint=checkpoint, expected_attempt=node.attempt,
        )
        self.service.emit_event(
            owner_id, thread_id, "run_started", node_id=node_id, source=self.executor_type,
            payload={"domain_ref": ref}, source_event_id=f"writer-start:{node_id}:{node.attempt}",
            idempotency_key=f"writer-start:{node_id}:{node.attempt}", attempt=node.attempt,
        )
        result = self._checkpoint_result(self.service.get_node(owner_id, node_id), raw=pipeline)
        return self._remember_command(node, "start", key, result)

    def status(self, owner_id: str, thread_id: str, node_id: str, *, attempt: int | None = None, **_: Any) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id, attempt=attempt)
        ref, pipeline = self._pipeline(owner_id, node)
        domain_status = str(pipeline.get("status") or "unknown")
        self._apply_terminal(owner_id, node, domain_status, raw=pipeline)
        fresh = self.service.get_node(owner_id, node_id)
        return self._checkpoint_result(fresh, status=domain_status, raw={"pipeline": pipeline, "domain_ref": ref})

    def cancel(self, owner_id: str, thread_id: str, node_id: str, *, attempt: int | None = None, **kwargs: Any) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id, attempt=attempt)
        key = self._action_key(kwargs.get("request_id"), kwargs.get("idempotency_key"))
        previous = self._command_result(node, "cancel", key)
        if previous is not None:
            return previous
        ref = self._ref(node)
        payload, status = self.app.writer.api_cancel_pipeline(ref["book_id"])
        if status >= 400:
            raise AdapterError(str(payload.get("error") or "取消 Writer 流水线失败"))
        result = self.status(owner_id, thread_id, node_id, attempt=attempt)
        return self._remember_command(node, "cancel", key, result)

    def retry(self, owner_id: str, thread_id: str, node_id: str, *, attempt: int | None = None, **kwargs: Any) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id, attempt=attempt)
        key = self._action_key(kwargs.get("request_id"), kwargs.get("idempotency_key"))
        previous = self._command_result(node, "retry", key)
        if previous is not None:
            return previous
        if node.status not in {"failed", "cancelled", "interrupted"}:
            raise AdapterError(f"Node 当前不能重试: {node.status}")
        ref = self._ref(node)
        rebase = bool(kwargs.get("rebase"))
        payload, status = self.app.writer.api_retry_pipeline(ref["book_id"], {"rebase": rebase})
        if status >= 400:
            raise AdapterError(str(payload.get("error") or "重试 Writer 流水线失败"))

        # api_retry_pipeline 已经完成领域 attempt+1 和唯一一次新任务提交。
        # 不能再调用 start，否则会对同一作品重复提交并让两个状态源分叉。
        _, pipeline = self._pipeline(owner_id, node)
        task_id = str(pipeline.get("current_task_id") or "")
        if not task_id:
            raise AdapterError("Writer 重试没有返回新的任务 ID")
        new_ref = {
            **ref,
            "domain_task_id": task_id,
            "domain_revision": int(pipeline.get("base_revision") or 0),
        }
        checkpoint = dict(node.checkpoint or {})
        checkpoint["domain_ref"] = new_ref
        checkpoint["pipeline_id"] = str(pipeline.get("id") or checkpoint.get("pipeline_id") or "")
        node = self.service.retry_node(owner_id, node_id)
        node = self.service.store.update_node(
            node.node_id,
            owner_id=owner_id,
            status="running",
            executor_id=task_id,
            checkpoint=checkpoint,
            expected_attempt=node.attempt,
        )
        self.service.emit_event(
            owner_id, thread_id, "run_started", node_id=node_id, source=self.executor_type,
            payload={"domain_ref": new_ref}, source_event_id=f"writer-retry:{node_id}:{node.attempt}",
            idempotency_key=f"writer-retry:{node_id}:{node.attempt}", attempt=node.attempt,
        )
        result = self._checkpoint_result(node, raw=pipeline)
        return self._remember_command(node, "retry", key, result)

    def resume(self, owner_id: str, thread_id: str, node_id: str, **kwargs: Any) -> AdapterResult:
        return self.retry(owner_id, thread_id, node_id, **kwargs)

    def sync(self, owner_id: str, thread_id: str, node_id: str, **kwargs: Any) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id, attempt=kwargs.get("attempt"))
        ref, pipeline = self._pipeline(owner_id, node)
        status = str(pipeline.get("status") or "unknown")
        if status in {"done", "failed", "cancelled", "interrupted", "conflict"}:
            self._apply_terminal(owner_id, node, status, raw=pipeline)
        return self.status(owner_id, thread_id, node_id, attempt=node.attempt)


class NovelStageAdapter(DomainAdapter):
    executor_type = "novel_stage"

    def __init__(self, service, app):
        super().__init__(service)
        self.app = app

    def _task(self, owner_id: str, node) -> tuple[dict[str, Any], dict[str, Any]]:
        ref = self._ref(node)
        task_id = str(ref.get("domain_task_id") or node.executor_id or "")
        task = self.app.studio.jobs.task(task_id) if task_id else None
        if not task:
            raise AdapterError("Novel Studio 任务不存在")
        return ref, task

    def start(self, owner_id: str, thread_id: str, node_id: str, *, attempt: int | None = None, **kwargs: Any) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id, attempt=attempt)
        key = self._action_key(kwargs.get("request_id"), kwargs.get("idempotency_key"))
        previous = self._command_result(node, "start", key)
        if previous is not None:
            return previous
        if node.status == "running":
            return self._checkpoint_result(node)
        if node.status != "ready":
            raise AdapterError(f"Node 当前不能启动: {node.status}")
        ref = self._ref(node)
        checkpoint = dict(node.checkpoint or {})
        stage_key = str(ref.get("stage_key") or "")
        if bool(ref.get("coordination")) and stage_key == "import":
            # Import is a precondition gate: the book was already created by
            # the Novel import flow, so no model task is submitted here.
            try:
                self.app.novel.store.get_book(ref["book_id"])
            except LookupError as exc:
                raise AdapterError("Novel 导入前置作品不存在") from exc
            node = self.service.store.update_node(
                node.node_id, owner_id=owner_id, status="running", expected_attempt=node.attempt,
            )
            self.service.emit_event(
                owner_id, thread_id, "run_started", node_id=node_id, source=self.executor_type,
                payload={"coordination": "import"}, source_event_id=f"novel-import:{node_id}:{node.attempt}",
                idempotency_key=f"novel-import:{node_id}:{node.attempt}", attempt=node.attempt,
            )
            self.service.emit_event(
                owner_id, thread_id, "run_completed", node_id=node_id, source=self.executor_type,
                payload={"coordination": "import"}, source_event_id=f"novel-import-done:{node_id}:{node.attempt}",
                idempotency_key=f"novel-import-done:{node_id}:{node.attempt}", attempt=node.attempt,
            )
            result = self._checkpoint_result(self.service.get_node(owner_id, node_id), status="succeeded")
            return self._remember_command(node, "start", key, result)
        if bool(ref.get("coordination")) and stage_key == "review":
            approval = self.service.create_approval(
                owner_id, thread_id, node.node_id, action="novel_review",
                description="确认复核当前 Novel 阶段产物并结束阶段链",
                idempotency_key=f"novel-review:{node.node_id}:{node.attempt}",
            )
            checkpoint["approval_id"] = approval.approval_id
            node = self.service.store.update_node(
                node.node_id, owner_id=owner_id, status="waiting_user", checkpoint=checkpoint,
                expected_attempt=node.attempt,
            )
            result = self._checkpoint_result(node, status="waiting_user")
            return self._remember_command(node, "start", key, result)
        request = checkpoint.get("start_request") if isinstance(checkpoint.get("start_request"), dict) else {}
        payload, status = self.app.novel.api_stage(request)
        if status >= 400:
            raise AdapterError(str(payload.get("error") or "启动 Novel 阶段失败"))
        task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
        task_id = str(task.get("id") or "")
        if not task_id:
            raise AdapterError("Novel 阶段没有返回任务 ID")
        ref = {**ref, "domain_task_id": task_id, "domain_revision": int(payload.get("book_revision") or 0)}
        checkpoint["domain_ref"] = ref
        apply_request = dict(checkpoint.get("apply_request") or {})
        if payload.get("book_revision") is not None:
            apply_request["revision"] = int(payload.get("book_revision") or 0)
        checkpoint["apply_request"] = apply_request
        node = self.service.store.update_node(
            node.node_id, owner_id=owner_id, status="running", executor_id=task_id,
            checkpoint=checkpoint, expected_attempt=node.attempt,
        )
        self.service.emit_event(
            owner_id, thread_id, "run_started", node_id=node_id, source=self.executor_type,
            payload={"domain_ref": ref}, source_event_id=f"novel-start:{node_id}:{node.attempt}",
            idempotency_key=f"novel-start:{node_id}:{node.attempt}", attempt=node.attempt,
        )
        result = self._checkpoint_result(self.service.get_node(owner_id, node_id), raw=task)
        return self._remember_command(node, "start", key, result)

    def status(self, owner_id: str, thread_id: str, node_id: str, *, attempt: int | None = None, **_: Any) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id, attempt=attempt)
        ref, task = self._task(owner_id, node)
        task_status = str(task.get("status") or "unknown")
        if task_status in {"done", "failed", "cancelled", "interrupted"} and node.status not in NODE_TERMINAL_STATUSES:
            if task_status == "done":
                # Result application is intentionally gated by Approval.
                if node.status != "waiting_user":
                    approval = self.service.create_approval(
                        owner_id, thread_id, node.node_id, action="novel_apply",
                        description="确认把拆书匠阶段结果保存为作品新版本", idempotency_key=f"novel-apply:{node.node_id}:{node.attempt}",
                    )
                    checkpoint = dict(node.checkpoint or {})
                    checkpoint["approval_id"] = approval.approval_id
                    self.service.store.update_node(node.node_id, owner_id=owner_id, checkpoint=checkpoint, expected_attempt=node.attempt)
            else:
                self._apply_terminal(owner_id, node, task_status, raw=task)
        fresh = self.service.get_node(owner_id, node_id)
        return self._checkpoint_result(fresh, status=task_status, raw={"task": task, "domain_ref": ref})

    def cancel(self, owner_id: str, thread_id: str, node_id: str, *, attempt: int | None = None, **kwargs: Any) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id, attempt=attempt)
        key = self._action_key(kwargs.get("request_id"), kwargs.get("idempotency_key"))
        previous = self._command_result(node, "cancel", key)
        if previous is not None:
            return previous
        if node.executor_id:
            self.app.studio.jobs.cancel(node.executor_id)
        self._apply_terminal(owner_id, node, "cancelled")
        result = self.status(owner_id, thread_id, node_id, attempt=attempt)
        return self._remember_command(node, "cancel", key, result)

    def retry(self, owner_id: str, thread_id: str, node_id: str, *, attempt: int | None = None, **kwargs: Any) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id, attempt=attempt)
        key = self._action_key(kwargs.get("request_id"), kwargs.get("idempotency_key"))
        previous = self._command_result(node, "retry", key)
        if previous is not None:
            return previous
        if node.status not in {"failed", "cancelled", "interrupted"}:
            raise AdapterError(f"Node 当前不能重试: {node.status}")
        node = self.service.retry_node(owner_id, node_id)
        result = self.start(owner_id, thread_id, node_id, attempt=node.attempt, request_id=key)
        return self._remember_command(node, "retry", key, result)

    def resume(self, owner_id: str, thread_id: str, node_id: str, **kwargs: Any) -> AdapterResult:
        return self.retry(owner_id, thread_id, node_id, **kwargs)

    def sync(self, owner_id: str, thread_id: str, node_id: str, **kwargs: Any) -> AdapterResult:
        return self.status(owner_id, thread_id, node_id, attempt=kwargs.get("attempt"))

    def apply(self, owner_id: str, thread_id: str, node_id: str, *, text: str, **_: Any) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id)
        ref = self._ref(node)
        request = dict((node.checkpoint or {}).get("apply_request") or {})
        payload = {**request, "task_id": ref.get("domain_task_id"), "text": text}
        result, status = self.app.novel.api_apply(ref["book_id"], payload)
        if status >= 400:
            raise AdapterError(str(result.get("error") or "Novel 结果写回失败"))
        current = self.service.get_node(owner_id, node_id)
        if current.status == "ready":
            self.service.store.update_node(
                node_id, owner_id=owner_id, status="running", expected_attempt=current.attempt,
            )
        self.service.emit_event(
            owner_id, thread_id, "run_completed", node_id=node_id, source=self.executor_type,
            payload={"output_refs": [f"novel://{ref['book_id']}/{ref['stage']}/{ref['domain_task_id']}"], "domain_ref": ref},
            source_event_id=f"novel-apply:{node_id}:{node.attempt}", idempotency_key=f"novel-apply:{node_id}:{node.attempt}", attempt=node.attempt,
        )
        return self._checkpoint_result(self.service.get_node(owner_id, node_id), raw={"book": result.get("book")})
