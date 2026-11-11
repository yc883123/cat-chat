"""Production bridges to existing Chat/Job executors; no model loop lives here."""
from __future__ import annotations

import logging
import threading
from dataclasses import replace
from typing import Any

from naiba.core.contracts import (SUPER_AGENT_EXECUTION_KEY, SUPER_AGENT_POLICY_KEY,
                                  SUPER_AGENT_CLAIMS_KEY, SUPER_AGENT_BINDING_KEY,
                                  SUPER_AGENT_DEPENDENCY_KEY, SUBAGENT_OUTCOME_KEY)
from naiba.core.exceptions import ActiveRunError
from naiba.super_agent.baseline import PreflightRejected, execution_fingerprint, validate_execution_baseline
from naiba.jobs import JobSpec
from naiba.super_agent.adapters import AdapterCapabilities, AdapterError, AdapterResult, BaseAdapter
from naiba.super_agent.models import NODE_TERMINAL_STATUSES
from naiba.super_agent.store import has_unsettled_execution

logger = logging.getLogger(__name__)


def current_execution_claims(node: Any) -> dict[str, dict[str, Any]]:
    """Return only launch intents for this generation (retry claims precede increment)."""
    claims = (node.checkpoint or {}).get(SUPER_AGENT_CLAIMS_KEY) or {}
    result = {}
    if not isinstance(claims, dict):
        return result
    for key, claim in claims.items():
        action = str(key).partition(":")[0]
        if action not in {"start", "retry", "resume"} or not isinstance(claim, dict):
            continue
        if (claim.get("result") or {}).get("submission") == "not_submitted":
            continue
        attempt = claim.get("attempt")
        if not isinstance(attempt, int) or isinstance(attempt, bool):
            continue
        target = attempt + (1 if action in {"retry", "resume"} else 0)
        if target == node.attempt and isinstance(claim.get("correlation"), str) and claim["correlation"]:
            result[str(key)] = claim
    return result


class RuntimeAdapter(BaseAdapter):
    capabilities = AdapterCapabilities(can_retry=True, can_resume=False)

    def __init__(self, service: Any, app: Any, executor_type: str):
        super().__init__(service)
        self.app = app
        self.executor_type = executor_type

    def _record(self, node: Any) -> dict[str, Any]:
        record = self.app.storage.get_background_task(node.executor_id) if node.executor_id else None
        cid = str(node.checkpoint.get("conversation_id") or "")
        if not record or str(record.get("conversation_id") or "") != cid:
            raise LookupError("执行不存在或不属于当前对话")
        record = {**record, "snapshot": self.app.storage.get_run_snapshot(node.executor_id) or {}}
        if self.executor_type != "chat_run" and str(record.get("owner_session_id") or "") != cid:
            raise LookupError("Job owner 与所属对话不一致")
        kind = str(record.get("kind") or "")
        if (self.executor_type == "chat_run" and kind != "chat") or (self.executor_type == "subagent" and kind != "subagent"):
            raise LookupError("执行类型与当前节点不一致")
        if self.executor_type == "job" and kind in {"chat", "plan_execute"}:
            raise LookupError("Chat Run 不能作为 Job 关联")
        if self.executor_type != "job":
            correlation = self._correlation(record)
            if not correlation or not any(correlation == claim["correlation"] for claim in current_execution_claims(node).values()):
                raise LookupError("执行关联与当前 Node attempt 不一致")
        return record

    @staticmethod
    def _correlation(record: dict[str, Any]) -> str:
        snapshot = record.get("snapshot") or {}
        if not isinstance(snapshot, dict):
            return ""
        params = snapshot.get("params") or {}
        return str(snapshot.get(SUPER_AGENT_EXECUTION_KEY) or
                   (params.get(SUPER_AGENT_EXECUTION_KEY) if isinstance(params, dict) else "") or "")

    def _bind(self, node: Any, execution_id: str) -> Any:
        current = self._node(node.owner_id, node.thread_id, node.node_id, attempt=node.attempt)
        if current.executor_id and current.executor_id != execution_id:
            raise AdapterError("当前 Node 已关联另一执行，不能覆盖")
        record = self._record(replace(current, executor_id=execution_id))
        node = current
        if not node.executor_id:
            node = self.service.store.update_node(node.node_id, owner_id=node.owner_id,
                executor_id=execution_id, expected_attempt=node.attempt)
        # The launch event may already exist when linkage failed after submit.
        # Repair state explicitly; replaying an idempotent event cannot do it.
        if node.status == "interrupted" and record.get("status") not in {"interrupted"}:
            node = self.service.store.update_node(node.node_id, owner_id=node.owner_id,
                status="ready", expected_attempt=node.attempt)
        if node.status == "ready":
            node = self.service.store.update_node(node.node_id, owner_id=node.owner_id,
                status="running", expected_attempt=node.attempt)
        if node.status == "running":
            self._start_event(node, source=self.executor_type)
        thread = self.service.get_thread(node.owner_id, node.thread_id)
        field = "linked_run_ids" if self.executor_type == "chat_run" else "linked_job_ids"
        raw_ids = getattr(thread, field, None)
        ids = list(raw_ids) if isinstance(raw_ids, list) else []
        if execution_id not in ids:
            self.service.store.update_thread(node.thread_id, owner_id=node.owner_id,
                **{field: ids + [execution_id]})
        if node.error.startswith(("控制结果需对账", "对账失败")) or (node.error.startswith("服务重启") and node.status != "interrupted"):
            node = self.service.store.update_node(node.node_id, owner_id=node.owner_id,
                error="", expected_attempt=node.attempt)
        for key, claim in current_execution_claims(node).items():
            if claim.get("state") != "done" and (claim["correlation"] == self._correlation(record)
                    or (self.executor_type == "job" and key.startswith("start:") and execution_id == node.checkpoint.get("job_id"))):
                self.service.store.settle_command(node.owner_id, node.node_id, key,
                    {"executor_id": execution_id, "attempt": node.attempt, "status": record.get("status")},
                    expected_attempt=node.attempt)
        if thread.status == "needs_recovery" and not any(
                n.status == "interrupted" for n in self.service.list_nodes(node.owner_id, node.thread_id)):
            self.service.store.update_thread(node.thread_id, owner_id=node.owner_id,
                status="active", summary="已接回原任务，未重复执行")
        return self.service.get_node(node.owner_id, node.node_id)

    def find_claim_execution(self, node: Any, correlation: str) -> str:
        # SQL filters the tiny correlation field, never scans event/log bodies.
        with self.app.storage._connect() as db:
            rows = db.execute("SELECT id FROM background_tasks WHERE conversation_id=? AND "
                "CASE WHEN json_valid(snapshot) THEN (json_extract(snapshot,?)=? OR json_extract(snapshot,?)=?) ELSE 0 END LIMIT 2",
                (str(node.checkpoint.get("conversation_id") or ""), f"$.{SUPER_AGENT_EXECUTION_KEY}", correlation,
                 f"$.params.{SUPER_AGENT_EXECUTION_KEY}", correlation)).fetchall()
            if len(rows) > 1:
                raise AdapterError("同一执行关联存在多条记录，需人工对账")
            if rows:
                return str(rows[0]["id"])
        return ""

    def preflight(self, node: Any) -> dict[str, Any] | None:
        """No execution side effects: run before claiming and again at submission."""
        if self.executor_type not in {"chat_run", "subagent"}:
            return None
        frozen = node.checkpoint.get("configuration_fingerprint")
        if frozen:
            denial = validate_execution_baseline(self.app, str(node.checkpoint["conversation_id"]), frozen)
            if denial:
                raise PreflightRejected(denial)
        elif self.executor_type == "subagent":
            raise PreflightRejected("旧子助手缺少可验证的授权基准，请重新创建任务；未派发")
        if node.dependencies:
            results = getattr(self.service, "results", None) or getattr(getattr(self.app, "super_agent_domain", None), "results", None)
            if results is None:
                raise PreflightRejected("前序正文读取服务不可用，未派发")
            try:
                return results.dependency_context(node.owner_id, node.thread_id, node.node_id)
            except (ValueError, LookupError) as exc:
                raise PreflightRejected(str(exc)) from exc
        return None

    def start(self, owner_id: str, thread_id: str, node_id: str, *, correlation: str = "", **_: Any) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id)
        if node.executor_id:
            self._bind(node, node.executor_id)
            return self.sync(owner_id, thread_id, node_id)
        if node.status != "ready":
            raise AdapterError(f"Node 当前不能启动: {node.status}")
        checkpoint = node.checkpoint
        cid = str(checkpoint["conversation_id"])
        request = dict(checkpoint.get("start_request") or {})
        dependency_context = self.preflight(node)
        if self.executor_type == "chat_run":
            current = self.app.storage.get_conversation(cid, include_trace=False)
            frozen = {} if checkpoint.get("configuration_fingerprint") else checkpoint.get("conversation_fingerprint") or {}
            for key, value in frozen.items():
                if current.get(key) != value:
                    raise PreflightRejected(f"会话 {key} 已变化；请重新创建任务以复核权限/模型")
            internal = {"execution_key": correlation}
            if checkpoint.get("configuration_fingerprint"):
                internal["task_model_override"] = True
                internal["configuration_fingerprint"] = checkpoint["configuration_fingerprint"]
            if dependency_context:
                internal["dependency_context"] = dependency_context
            run = self.app.runs.submit_chat({**request, "conversation_id": cid}, **internal)
            execution_id = str(run["id"])
        elif self.executor_type == "subagent":
            params = dict(request)
            params[SUPER_AGENT_EXECUTION_KEY] = correlation
            params[SUPER_AGENT_BINDING_KEY] = {"owner_id": owner_id, "thread_id": thread_id,
                "node_id": node_id, "attempt": node.attempt, "correlation": correlation}
            params["configuration_fingerprint"] = checkpoint["configuration_fingerprint"]
            if dependency_context:
                params[SUPER_AGENT_DEPENDENCY_KEY] = dependency_context
            execution_id = self.app.jobs.start(JobSpec(kind="subagent", conversation_id=cid,
                params=params, label=str(checkpoint.get("label") or "任务总管子 Agent"),
                owner_session_id=cid, detached=True), owner=cid)
        else:
            execution_id = str(checkpoint.get("job_id") or "")
            if not execution_id:
                raise AdapterError("通用 Job 只允许绑定既有 job_id，不能绕过工具权限新建")
        node = self._bind(node, execution_id)
        return self._result(node, status=str(self._record(node).get("status") or "running"))

    def status(self, owner_id: str, thread_id: str, node_id: str, **_: Any) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id)
        return self._result(node, status=str(self._record(node).get("status") or "unknown"))

    def reconcile_binding(self, node: Any) -> bool:
        """Repair a launch's bookkeeping using persisted evidence, never submit."""
        if node.executor_id:
            self._bind(node, node.executor_id)
            return True
        found_ids = set()
        for claim in current_execution_claims(node).values():
            found = self.find_claim_execution(node, claim["correlation"])
            if found:
                found_ids.add(found)
        if len(found_ids) > 1:
            raise AdapterError("当前 Node attempt 存在多个执行，需人工对账")
        if found_ids:
            self._bind(node, found_ids.pop())
            return True
        return False

    def _project_chat_waiting(self, node: Any, status: str) -> None:
        if self.executor_type not in {"chat_run", "subagent"} or status not in {"queued", "running", "waiting"} or node.cancel_requested:
            return
        snapshot = getattr(self.app.runs, "subagent_confirmation_snapshot" if self.executor_type == "subagent" else "confirmation_snapshot", None)
        confirmations = snapshot(node.executor_id) if callable(snapshot) else []
        waiting = any(item.get("status") == "pending" for item in confirmations)
        target = "waiting_user" if waiting else "running"
        if node.status in {"running", "waiting_user"} and node.status != target:
            self.service.store.update_node(node.node_id, owner_id=node.owner_id,
                status=target, expected_attempt=node.attempt)
        thread = self.service.get_thread(node.owner_id, node.thread_id)
        if thread.status == "active" and waiting:
            self.service.store.update_thread(node.thread_id, owner_id=node.owner_id, status="waiting_user")
        elif thread.status == "waiting_user" and not waiting:
            pending = any(a.status == "pending" for a in self.service.approvals(node.owner_id, node.thread_id))
            other_waiting = any(n.status == "waiting_user" for n in self.service.list_nodes(node.owner_id, node.thread_id))
            if not pending and not other_waiting:
                self.service.store.update_thread(node.thread_id, owner_id=node.owner_id, status="active")

    def sync(self, owner_id: str, thread_id: str, node_id: str, **_: Any) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id)
        record = self._record(node)
        status = str(record.get("status") or "unknown")
        if self.executor_type == "subagent" and status == "completed":
            outcome = (record.get("result") or {}).get(SUBAGENT_OUTCOME_KEY)
            if (not isinstance(outcome, dict) or type(outcome.get("version")) is not int
                    or outcome.get("version") != 1 or outcome.get("status") != "completed"):
                status = "failed"
                record["error"] = str((outcome.get("summary") if isinstance(outcome, dict) else "") or "子助手结果缺少兼容的有效完成结论，需人工核对；未推进后继")
        if node.status in NODE_TERMINAL_STATUSES:
            return self._result(node, status=status)
        thread = self.service.get_thread(owner_id, thread_id)
        if thread.status == "paused":
            return self._result(node, status=status)
        self._project_chat_waiting(node, status)
        if status in {"cancelled", "interrupted"} or (node.cancel_requested and status in {"failed", "completed"}):
            target = "cancelled" if node.cancel_requested or status == "cancelled" else "interrupted"
            if target != node.status:
                self.service.store.update_node(node_id, owner_id=owner_id, status=target,
                    expected_attempt=node.attempt)
            if target == "interrupted" and thread.status in {"active", "waiting_user"}:
                self.service.store.update_thread(thread_id, owner_id=owner_id, status="needs_recovery")
        elif status in {"completed", "failed"}:
            event_type = "run_completed" if status == "completed" else "job_failed"
            self.service.emit_event(owner_id, thread_id, event_type, node_id=node_id,
                source=self.executor_type, attempt=node.attempt,
                idempotency_key=f"{self.executor_type}:{node.executor_id}:{node.attempt}:{status}",
                payload={"error": str(record.get("error") or "执行失败"),
                         "output_refs": [f"run://{node.executor_id}" if self.executor_type == "chat_run" else f"job://{node.executor_id}"]})
            self.service.create_artifact(owner_id, thread_id, node_id, kind=self.executor_type,
                path_or_ref=f"{'run' if self.executor_type == 'chat_run' else 'job'}://{node.executor_id}",
                title="对话结果" if self.executor_type == "chat_run" else "后台任务结果",
                summary=f"执行状态：{status}", idempotency_key=f"runtime-artifact:{node_id}:{node.attempt}")
        return self._result(self.service.get_node(owner_id, node_id), status=status)

    def cancel(self, owner_id: str, thread_id: str, node_id: str, **_: Any) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id)
        if node.status in NODE_TERMINAL_STATUSES:
            return self._result(node)
        if not node.executor_id:
            node = self.service.store.update_node(node_id, owner_id=owner_id,
                status="cancelled", cancel_requested=True, expected_attempt=node.attempt)
            return self._result(node)
        self._record(node)
        if node.status in {"running", "waiting_user"}:
            self.service.store.update_node(node_id, owner_id=owner_id, status="stopping",
                cancel_requested=True, expected_attempt=node.attempt)
        if self.executor_type == "chat_run":
            self.app.runs.cancel(node.executor_id)
        else:
            self.app.jobs.cancel(node.executor_id, owner=str(node.checkpoint["conversation_id"]))
        return self.sync(owner_id, thread_id, node_id)

    def retry(self, owner_id: str, thread_id: str, node_id: str, *, correlation: str = "", **_: Any) -> AdapterResult:
        node = self._node(owner_id, thread_id, node_id)
        self.preflight(node)
        if node.status not in {"failed", "cancelled", "interrupted"}:
            raise AdapterError("只有失败、取消或中断节点可显式重试")
        if self.executor_type == "job":
            record = self._record(node)
            if record.get("status") in {"queued", "running", "waiting", "stopping", "cancelling"}:
                raise AdapterError("原 Job 仍在运行，不能重试")
            if str(record.get("kind")) not in {"comfyui", "check", "http_poll"}:
                raise AdapterError("此 Job 含未知副作用，不支持从总管重试")
            # Existing checkpoint policy remains the authority; no arbitrary params.
            new_id = self.app.jobs.retry(node.executor_id, owner=str(node.checkpoint["conversation_id"]))
            if not new_id:
                raise AdapterError("Job 没有可安全重试的 checkpoint")
            node = self.service.retry_node(owner_id, node_id)
            self.service.store.update_node(node_id, owner_id=owner_id, executor_id="", expected_attempt=node.attempt)
            return self._result(self._bind(node, str(new_id)))
        if node.executor_id:
            record = self._record(node)
            if record.get("status") in {"queued", "running", "waiting", "stopping", "cancelling"}:
                raise AdapterError("原执行仍在运行，不能重试")
        node = self.service.retry_node(owner_id, node_id)
        self.service.store.update_node(node_id, owner_id=owner_id, executor_id="", expected_attempt=node.attempt)
        return self.start(owner_id, thread_id, node_id, correlation=correlation)

    def resume(self, *args: Any, **kwargs: Any) -> AdapterResult:
        raise AdapterError("对话/子 Agent 不支持安全断点续跑，请显式重试；既有 Job 由 checkpoint 恢复")


class ExecutionCoordinator:
    """Bounded worker, durable command gate and control plane for all adapters."""

    def __init__(self, domain: Any):
        self.domain = domain
        self.app = domain.app
        self.service = domain.service
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.cursor = ""
        for kind in ("chat_run", "job", "subagent"):
            domain.registry.register(RuntimeAdapter(self.service, self.app, kind))
        from naiba.super_agent.supervised import SupervisedChatAdapter
        self.supervised = SupervisedChatAdapter(self)
        domain.registry.register(self.supervised)

    @staticmethod
    def policy(body: dict[str, Any]) -> dict[str, bool]:
        result = {}
        for key in ("autostart", "auto_advance"):
            value = body.get(key, False)
            if not isinstance(value, bool):
                raise ValueError(f"{key} 必须是布尔值")
            result[key] = value
        return result

    def prepare(self, body: dict[str, Any]) -> dict[str, Any]:
        domain = str(body.get("domain") or "")
        cid = str(body.get("conversation_id") or "").strip()
        if not cid:
            raise ValueError("缺少 conversation_id")
        conversation = self.app.storage.get_conversation(cid, include_trace=False) if cid else None
        if not conversation:
            raise LookupError("对话不存在")
        if conversation.get("conv_kind") == "aux" or conversation.get("kind") == "aux":
            raise ValueError("辅助对话不能由任务总管扩大权限")
        request = self.domain._common_request(body)
        policy = self.policy(body)
        job_id = str(body.get("job_id") or request.get("job_id") or "")
        executor = "chat_run" if domain == "chat" else domain
        snapshot = {}
        if domain == "job":
            if not job_id or set(request) - {"job_id"} or body.get("params") or body.get("kind"):
                raise ValueError("Job 仅允许关联已有 job_id，不接受 kind/params 新建")
            record = self.app.storage.get_background_task(job_id)
            if not record or record.get("conversation_id") != cid or record.get("owner_session_id") != cid:
                raise LookupError("Job 不存在或不属于当前对话 owner")
            if record.get("kind") in {"chat", "plan_execute"}:
                raise ValueError("Chat Run 不能作为 Job 关联")
            request = {}
        else:
            for key in self.domain.MODEL_FIELDS:
                request.setdefault(key, conversation.get(key) or "")
            selection = self.domain._validate_model_selection(request.get("model_key"), request.get("provider_id"),
                request.get("model_name"), context="start_request")
            request.update(selection)
            snapshot = {"model_key": selection["model_key"], "provider_id": selection["provider_id"],
                "workspace_dir": str(conversation.get("workspace_dir") or self.app.config.resolve_workspace_dir()),
                "permission_mode": str(conversation.get("permission_mode") or "confirm")}
            if domain == "chat":
                if set(request) - {"message", "attachments", "folders", *self.domain.MODEL_FIELDS}:
                    raise ValueError("Chat start_request 包含不支持字段")
                if not str(request.get("message") or "").strip() and not request.get("attachments"):
                    raise ValueError("Chat 缺少 message 或附件")
            else:
                if set(request) - {"instruction", "allowed_tools", "fork", *self.domain.MODEL_FIELDS}:
                    raise ValueError("subagent start_request 包含不支持字段")
                if not str(request.get("instruction") or "").strip():
                    raise ValueError("子 Agent 缺少 instruction")
                fork = request.get("fork", False)
                if not isinstance(fork, bool):
                    raise ValueError("fork 必须是布尔值")
                agent = self.app.config.get_agent(str(conversation.get("agent_id") or "")) or {}
                if not agent:
                    agent = self.app.config.get_agent(self.app.config.default_agent_id()) or {}
                from naiba.run.session import resolve_session_tool_ids
                allowed = self.app.runs._resolve_allowed_tools("craft", agent,
                    bool(conversation.get("web_search_enabled")), selection["model_key"],
                    resolve_session_tool_ids(self.app, conversation, agent))
                from naiba.subagent import SUBAGENT_BLOCKED_TOOLS
                requested = request.get("allowed_tools", allowed)
                if not isinstance(requested, list) or any(t not in allowed for t in requested):
                    raise ValueError("子 Agent 工具不可超出父会话授权")
                request = {"instruction": request["instruction"], "fork": fork,
                    "allowed_tools": [t for t in requested if t not in SUBAGENT_BLOCKED_TOOLS],
                    "skill_policy": dict(conversation.get("skill_policy") or {"mode": "auto", "skill_ids": []}),
                    "execution_snapshot": {**snapshot, "model_name": selection["model_name"]}}
        return {"domain": domain, "conversation_id": cid, "executor_type": executor,
                "execution_snapshot": snapshot, "checkpoint": {"conversation_id": cid,
                "start_request": request, "job_id": job_id, SUPER_AGENT_POLICY_KEY: policy,
                "configuration_fingerprint": execution_fingerprint(self.app, cid,
                    execution_model=selection) if domain != "job" else {},
                "conversation_fingerprint": {key: conversation.get(key) for key in
                    ("workspace_dir", "permission_mode", "enabled_tool_ids", "agent_id", "model_key", "provider_id", "model_name")}}}

    def create(self, owner: str, body: dict[str, Any]) -> tuple[Any, Any]:
        if body.get("execution_mode") == "supervised":
            return self.supervised.create(owner, body)
        if body.get("execution_mode") not in {None, "", "single"}:
            raise ValueError("未知聊天执行方式")
        prepared = self.prepare(body)
        domain, cid = prepared["domain"], prepared["conversation_id"]
        snapshot, checkpoint = prepared["execution_snapshot"], prepared["checkpoint"]
        executor, job_id = prepared["executor_type"], checkpoint["job_id"]
        policy = checkpoint[SUPER_AGENT_POLICY_KEY]
        thread = self.service.create_thread(owner, str(body.get("title") or f"{domain} 任务"),
            str(body.get("goal") or ""), execution_snapshot=snapshot, linked_conversation_ids=[cid])
        node = self.service.create_node(owner, thread.thread_id, domain, executor_type=executor,
            idempotency_key=str(body.get("idempotency_key") or f"{domain}:{cid}:{job_id}"),
            status="ready", execution_snapshot=snapshot,
            checkpoint=checkpoint)
        if domain == "job":
            self.command(owner, thread.thread_id, node.node_id, "start", f"bind:{job_id}")
        elif policy["autostart"]:
            self.wake_event.set()
        return self.service.get_thread(owner, thread.thread_id), self.service.get_node(owner, node.node_id)

    def create_mixed(self, owner: str, body: dict[str, Any]) -> tuple[Any, Any]:
        import uuid
        raw = body.get("nodes")
        if not isinstance(raw, list) or not 1 <= len(raw) <= 32:
            raise ValueError("mixed.nodes 必须包含 1 至 32 个线性节点")
        policy = self.policy(body)
        prepared_nodes = []
        cids = []
        for index, item in enumerate(raw):
            if isinstance(item, dict) and (item.get("execution_mode") == "supervised" or item.get("supervision")):
                raise ValueError("自动续办不能嵌入混合阶段链")
            if not isinstance(item, dict) or item.get("domain") not in {"chat", "job", "subagent", "writer", "novel"}:
                raise ValueError("mixed 节点 domain 非法")
            domain = item["domain"]
            if domain in {"chat", "job", "subagent"}:
                prepared = self.prepare(item)
                cids.append(prepared["conversation_id"])
            else:
                stage, book_id = str(item.get("stage") or ""), str(item.get("book_id") or "")
                executor = self.domain._validate(domain, stage, book_id)
                request = self.domain._common_request(item)
                selection = self.domain._validate_model_selection(request.get("model_key"), request.get("provider_id"),
                    request.get("model_name"), context=f"nodes.{index}")
                request.update(selection)
                if domain == "writer":
                    request.setdefault("kind", stage)
                    request.setdefault("chapter_no", 1)
                else:
                    request.update(book_id=book_id, stage=stage)
                    request.setdefault("chapter_ids", [])
                prepared = {"executor_type": executor, "execution_snapshot": self.domain._execution_snapshot(item, selection),
                    "checkpoint": {"domain_ref": {"domain": domain, "book_id": book_id, "stage": stage,
                    "domain_task_id": "", "domain_revision": 0}, "start_request": request,
                    "apply_request": dict(item.get("apply_request") or {})}}
            prepared_nodes.append((item, prepared))
        chain_id = str(body.get("chain_instance_id") or body.get("request_id") or body.get("idempotency_key") or uuid.uuid4().hex)
        specs = []
        keys = [str(item.get("key") or f"step_{index}") for index, (item, _) in enumerate(prepared_nodes)]
        if len(set(keys)) != len(keys):
            raise ValueError("mixed 节点 key 必须唯一")
        for index, (item, prepared) in enumerate(prepared_nodes):
            checkpoint = dict(prepared["checkpoint"])
            checkpoint[SUPER_AGENT_POLICY_KEY] = policy
            specs.append({"kind": str(item["domain"]), "executor_type": prepared["executor_type"],
                "stage_key": keys[index], "sequence": index,
                "dependencies": [keys[index - 1]] if index else [],
                "promote_to": keys[index + 1] if index + 1 < len(keys) else "",
                "promotion_policy": {"on": "success", "once": True},
                "idempotency_key": f"{chain_id}:{keys[index]}",
                "status": "ready" if index == 0 else "blocked", "checkpoint": checkpoint,
                "execution_snapshot": prepared["execution_snapshot"]})
        thread, nodes = self.service.create_thread_chain(owner, str(body.get("title") or "混合任务链"),
            str(body.get("goal") or ""), chain_instance_id=chain_id, chain_version=1,
            nodes=specs, linked_conversation_ids=list(dict.fromkeys(cids)))
        self.wake_event.set()
        return thread, nodes[0]

    def command(self, owner: str, thread_id: str, node_id: str, action: str, request_id: str,
                *, attempt: int | None = None, payload: dict[str, Any] | None = None) -> AdapterResult:
        with self.lock:
            node = self.service.get_node(owner, node_id)
            if node.thread_id != thread_id:
                raise LookupError("Node 不属于当前 Thread")
            if attempt is not None and attempt != node.attempt:
                raise AdapterError("Node attempt 已过期")
            adapter = self.domain.registry.get(node.executor_type)
            thread = self.service.get_thread(owner, thread_id)
            if thread.status == "archived":
                if action not in {"status", "sync"}:
                    raise AdapterError("已归档任务不能控制执行，请先恢复任务")
                self._tick_archived_thread(thread)
                return adapter._result(self.service.get_node(owner, node_id))
            if node.executor_type == "supervised_chat":
                if action in {"status", "sync"}:
                    return adapter.sync(owner, thread_id, node_id)
                if action == "start":
                    return adapter.start(owner, thread_id, node_id)
                if action in {"cancel", "resume"}:
                    return adapter.control(owner, thread_id, action)
                raise AdapterError("自动续办不支持重试旧节点，请使用暂停或继续")
            if action in {"status", "sync"}:
                if self.service.get_thread(owner, thread_id).status == "paused":
                    return adapter._result(node, status="paused")
                if isinstance(adapter, RuntimeAdapter):
                    adapter.reconcile_binding(node)
                return getattr(adapter, action)(owner, thread_id, node_id, attempt=node.attempt)
            if action not in {"start", "cancel", "retry", "resume"} or not request_id:
                raise ValueError("控制动作必须提供合法 action 和 request_id")
            if payload:
                raise ValueError("控制动作不接受执行参数覆盖；请重新创建任务")
            thread = self.service.get_thread(owner, thread_id)
            if action in {"start", "retry", "resume"} and thread.status in {"paused", "stopping", "archived", "completed"}:
                raise AdapterError("暂停或终态任务不能派发，请先继续")
            saved_claim = (node.checkpoint.get(SUPER_AGENT_CLAIMS_KEY) or {}).get(f"{action}:{request_id}")
            receipts = node.checkpoint.get(SUPER_AGENT_CLAIMS_KEY) or {}
            for receipt_key, receipt in receipts.items():
                if (str(receipt_key).partition(":")[0] == action and receipt.get("request_id") == request_id
                        and (receipt.get("result") or {}).get("submission") == "not_submitted"):
                    raise PreflightRejected(str(receipt["result"].get("error") or "此前请求明确未派发，请重新开始"))
            if action != "start" and isinstance(saved_claim, dict):
                if saved_claim.get("state") == "done":
                    return adapter._result(node)
                if (isinstance(adapter, RuntimeAdapter) and action in {"retry", "resume"}
                        and saved_claim.get("attempt") == node.attempt - 1
                        and adapter.reconcile_binding(node)):
                    return adapter.sync(owner, thread_id, node_id)
            if action == "start":
                if isinstance(adapter, RuntimeAdapter) and adapter.reconcile_binding(node):
                    return adapter.sync(owner, thread_id, node_id)
                if node.executor_id:
                    return adapter.status(owner, thread_id, node_id)
                if isinstance(adapter, RuntimeAdapter) and current_execution_claims(node):
                    raise AdapterError("原启动结果尚未对账；不会自动重放或重新提交")
            if isinstance(adapter, RuntimeAdapter) and action in {"retry", "cancel"} and not node.executor_id:
                attached = adapter.reconcile_binding(node)
                node = self.service.get_node(owner, node_id)
                if not attached and current_execution_claims(node):
                    raise AdapterError("原执行尚未对账，无法确认能安全重试或取消；不会重新提交")
            if isinstance(adapter, RuntimeAdapter) and action == "retry":
                if node.status not in {"failed", "cancelled", "interrupted"}:
                    raise AdapterError("只有失败、取消或中断节点可显式重试")
                if node.executor_id and adapter._record(node).get("status") in {"queued", "running", "waiting", "stopping", "cancelling"}:
                    raise AdapterError("原执行仍在运行，不能重试")
            if action == "resume" and not self.node_capabilities(node)["resume"]:
                raise AdapterError("当前执行器不支持安全断点续跑，请显式重试（重新执行）")
            if isinstance(adapter, RuntimeAdapter) and action in {"start", "retry"} and not self.node_capabilities(node)[action]:
                raise AdapterError("当前执行记录与状态不支持此操作，未派发")
            if isinstance(adapter, RuntimeAdapter) and action in {"start", "retry"}:
                try:
                    adapter.preflight(node)
                except PreflightRejected as exc:
                    refusal_key, _, _ = self.service.store.claim_command(owner, thread_id, node_id, action, request_id)
                    self.service.store.settle_command(owner, node_id, refusal_key,
                        {"submission": "not_submitted", "error": str(exc), "attempt": node.attempt},
                        expected_attempt=node.attempt)
                    self._record_not_submitted(node, str(exc))
                    raise
            key, claim, fresh = self.service.store.claim_command(owner, thread_id, node_id, action, request_id)
            if not fresh:
                if claim.get("state") == "done":
                    if (claim.get("result") or {}).get("submission") == "not_submitted":
                        raise PreflightRejected(str(claim["result"].get("error") or "此前请求明确未派发，请重新开始"))
                    return adapter._result(self.service.get_node(owner, node_id))
                if isinstance(adapter, RuntimeAdapter) and action in {"start", "retry", "resume"}:
                    target_attempt = int(claim["attempt"]) + (1 if action in {"retry", "resume"} else 0)
                    if target_attempt not in {node.attempt, node.attempt + 1} or (target_attempt != node.attempt and action == "start"):
                        raise AdapterError("持久 claim 已不属于当前 Node attempt")
                    found = adapter.find_claim_execution(node, str(claim["correlation"]))
                    if found:
                        if target_attempt != node.attempt:
                            node = self.service.retry_node(owner, node_id)
                            node = self.service.store.update_node(node_id, owner_id=owner, executor_id="", expected_attempt=node.attempt)
                        adapter._bind(node, found)
                        result = adapter.sync(owner, thread_id, node_id)
                        self.service.store.settle_command(owner, node_id, key, {"executor_id": found, "attempt": node.attempt}, expected_attempt=node.attempt)
                        return result
                raise AdapterError("启动/控制结果不确定，已保留持久 claim；不会自动重放副作用，请先核对原执行")
            try:
                result = getattr(adapter, action)(owner, thread_id, node_id, attempt=node.attempt,
                    request_id=request_id, idempotency_key=request_id, correlation=claim["correlation"])
            except (PreflightRejected, ActiveRunError) as exc:
                current = self.service.get_node(owner, node_id)
                error = "此会话正在执行，请结束后再开始；本次未派发" if isinstance(exc, ActiveRunError) else str(exc)
                self.service.store.settle_command(owner, node_id, key,
                    {"submission": "not_submitted", "error": error, "attempt": current.attempt},
                    expected_attempt=current.attempt)
                self._record_not_submitted(current, error)
                raise PreflightRejected(error) from exc
            except Exception as exc:
                current = self.service.get_node(owner, node_id)
                if current.status not in NODE_TERMINAL_STATUSES:
                    if current.status == "ready":
                        current = self.service.store.update_node(node_id, owner_id=owner, status="running", expected_attempt=current.attempt)
                    if current.status in {"running", "waiting_user", "stopping"}:
                        self.service.store.update_node(node_id, owner_id=owner, status="interrupted",
                            error=f"控制结果需对账，未自动重放：{str(exc)[:300]}", expected_attempt=current.attempt)
                    if self.service.get_thread(owner, thread_id).status in {"active", "waiting_user", "stopping"}:
                        self.service.store.update_thread(thread_id, owner_id=owner, status="needs_recovery",
                            summary=f"执行未确认：{str(exc)[:200]}；需对账或显式重试")
                raise
            self.service.store.settle_command(owner, node_id, key,
                {"executor_id": result.executor_id, "attempt": result.attempt, "status": result.status},
                expected_attempt=result.attempt)
            self.wake_event.set()
            return result

    def _record_not_submitted(self, node: Any, error: str) -> None:
        node = self.service.get_node(node.owner_id, node.node_id)
        checkpoint = dict(node.checkpoint)
        # A deterministic refusal must not become an implicit polling queue.
        checkpoint[SUPER_AGENT_POLICY_KEY] = {**(checkpoint.get(SUPER_AGENT_POLICY_KEY) or {}),
            "autostart": False, "auto_advance": False}
        self.service.store.update_node(node.node_id, owner_id=node.owner_id,
            checkpoint=checkpoint, error=error[:400], expected_attempt=node.attempt)

    def node_capabilities(self, node: Any) -> dict[str, bool]:
        """The same executor/state/ownership gate drives UI and control requests."""
        result = dict.fromkeys(("start", "cancel", "retry", "resume"), False)
        if node.executor_type not in self.domain.registry:
            return result  # Legacy/uninstalled executors remain readable, never runnable.
        adapter = self.domain.registry.get(node.executor_type)
        thread = self.service.get_thread(node.owner_id, node.thread_id)
        if thread.status in {"archived", "completed"}:
            return result
        active_thread = thread.status not in {"paused", "stopping", "cancelled"}
        claims = current_execution_claims(node)
        unknown = bool(not node.executor_id and any(c.get("state") != "done" for c in claims.values()))
        result["start"] = bool(adapter.capabilities.can_start and active_thread and node.status == "ready" and not node.executor_id and not unknown)
        result["cancel"] = bool(adapter.capabilities.can_cancel and node.status not in NODE_TERMINAL_STATUSES and not unknown)
        terminal = node.status in {"failed", "cancelled", "interrupted"}
        result["retry"] = bool(adapter.capabilities.can_retry and active_thread and terminal and not unknown)
        result["resume"] = bool(adapter.capabilities.can_resume and active_thread and node.status == "interrupted" and node.executor_id)
        if isinstance(adapter, RuntimeAdapter) and node.executor_id:
            try:
                record = adapter._record(node)
            except (LookupError, ValueError, AdapterError):
                return dict.fromkeys(result, False)
            if record.get("status") in {"queued", "running", "waiting", "stopping", "cancelling"}:
                result["retry"] = False
            if node.executor_type == "job":
                result["retry"] = result["retry"] and record.get("kind") in {"comfyui", "check", "http_poll"} and bool(record.get("checkpoint"))
        return result

    def control(self, owner: str, thread_id: str, action: str, request_id: str) -> AdapterResult:
        if not request_id:
            raise ValueError("Thread 控制必须提供 request_id")
        with self.lock:
            thread = self.service.get_thread(owner, thread_id)
            nodes = self.service.list_nodes(owner, thread_id)
            if len(nodes) == 1 and nodes[0].executor_type == "supervised_chat":
                return self.supervised.control(owner, thread_id, action, request_id=request_id)
            if action == "pause":
                if thread.status not in {"active", "waiting_user", "paused"}:
                    raise ValueError("当前任务不可暂停")
                if thread.status != "paused":
                    self.service.store.update_thread(thread_id, owner_id=owner, status="paused",
                        summary="已暂停新派发；在途执行仍可能运行，继续后对账，不伪装执行器暂停")
                event = "task_paused"
            elif action in {"continue", "resume"}:
                if thread.status not in {"paused", "needs_recovery", "active", "waiting_user"}:
                    raise ValueError("当前任务不可继续")
                if thread.status in {"paused", "needs_recovery"}:
                    self.service.store.update_thread(thread_id, owner_id=owner, status="active", summary="已继续；中断且无安全句柄的执行需显式重试")
                event = "task_resumed"
            elif action == "cancel":
                if thread.status not in {"completed", "archived"}:
                    # Stop dispatch first, then send actual executor cancellations.
                    if thread.status in {"failed", "needs_recovery"}:
                        self.service.store.update_thread(thread_id, owner_id=owner, status="active")
                    if self.service.get_thread(owner, thread_id).status not in {"cancelled", "stopping"}:
                        self.service.store.update_thread(thread_id, owner_id=owner, status="stopping", summary="取消已请求；在途任务等待执行器确认")
                    for node in nodes:
                        if node.status in NODE_TERMINAL_STATUSES:
                            continue
                        adapter = self.domain.registry.get(node.executor_type)
                        if isinstance(adapter, RuntimeAdapter) and not node.executor_id:
                            adapter.reconcile_binding(node)
                            node = self.service.get_node(owner, node.node_id)
                            if not node.executor_id and current_execution_claims(node):
                                self.service.store.update_thread(thread_id, owner_id=owner,
                                    status="needs_recovery", summary="原执行尚未对账，无法确认停止；未重新提交")
                                continue
                        if node.executor_id:
                            self.command(owner, thread_id, node.node_id, "cancel", f"{request_id}:{node.node_id}")
                        else:
                            self.service.store.update_node(node.node_id, owner_id=owner, status="cancelled",
                                cancel_requested=True, expected_attempt=node.attempt)
                event = "task_cancelled"
            else:
                raise ValueError("未知 Thread 控制动作")
            self.service.emit_event(owner, thread_id, event, idempotency_key=f"control:{action}:{request_id}", payload={"request_id": request_id})
            self.wake_event.set()
            node = self.service.get_node(owner, nodes[0].node_id)
            return self.domain.registry.get(node.executor_type)._result(node, status=self.service.get_thread(owner, thread_id).status)

    def tick(self, limit: int = 32) -> None:
        with self.lock:
            threads = self.service.store.reconciliation_threads(limit, after=self.cursor)
            self.cursor = threads[-1].thread_id if threads else ""
            for thread in threads:
                try:
                    self._tick_thread(thread)
                except Exception:
                    logger.exception("Super-agent 对账失败 thread=%s", thread.thread_id)

    def _tick_thread(self, thread: Any) -> None:
        owner, tid = thread.owner_id, thread.thread_id
        if thread.status == "archived":
            self._tick_archived_thread(thread)
            return
        if thread.status == "paused":
            for node in self.service.list_nodes(owner, tid):
                if node.executor_type == "supervised_chat":
                    self.supervised.sync(owner, tid, node.node_id)
            return  # In-flight remains honestly running; terminal events are read after continue.
        nodes = self.service.list_nodes(owner, tid)[:64]
        for node in nodes:
            if node.executor_type not in self.domain.registry or node.status in NODE_TERMINAL_STATUSES:
                continue
            adapter = self.domain.registry.get(node.executor_type)
            try:
                if node.executor_type == "supervised_chat":
                    record = self.supervised.store.get(tid, owner)
                    if record and record["state"]["phase"] == "ready" and thread.status == "active" and (node.checkpoint.get(SUPER_AGENT_POLICY_KEY) or {}).get("autostart"):
                        adapter.start(owner, tid, node.node_id)
                    else:
                        adapter.sync(owner, tid, node.node_id)
                    continue
                attached = isinstance(adapter, RuntimeAdapter) and adapter.reconcile_binding(node)
                if attached:
                    adapter.sync(owner, tid, node.node_id, attempt=node.attempt)
                elif node.executor_id:
                    if node.status == "interrupted":
                        # Reattach a known execution, never submit it again on restart.
                        if node.executor_type == "novel_stage":
                            adapter._task(owner, node)
                        else:
                            adapter._pipeline(owner, node)
                        node = self.service.store.update_node(node.node_id, owner_id=owner, status="ready", expected_attempt=node.attempt)
                        node = self.service.store.update_node(node.node_id, owner_id=owner, status="running", expected_attempt=node.attempt)
                        if self.service.get_thread(owner, tid).status == "needs_recovery":
                            self.service.store.update_thread(tid, owner_id=owner, status="active")
                    adapter.sync(owner, tid, node.node_id, attempt=node.attempt)
                elif node.status == "interrupted":
                    continue
                else:
                    policy = node.checkpoint.get(SUPER_AGENT_POLICY_KEY) or {}
                    # First node needs autostart; successors need auto_advance.
                    first = not node.dependencies and node.sequence == 0
                    enabled = policy.get("autostart") if first else policy.get("auto_advance")
                    current_thread = self.service.get_thread(owner, tid)
                    pending_approvals = any(a.status == "pending" for a in self.service.approvals(owner, tid))
                    if node.status == "ready" and enabled and current_thread.status == "active" and not pending_approvals:
                        self.command(owner, tid, node.node_id, "start", f"auto:{node.node_id}:{node.attempt}", attempt=node.attempt)
            except (AdapterError, LookupError, ValueError, RuntimeError) as exc:
                logger.debug("Super-agent 对账等待 node=%s reason=%s", node.node_id, exc)
                current = self.service.get_node(owner, node.node_id)
                if current.status in {"running", "waiting_user", "stopping"}:
                    self.service.store.update_node(current.node_id, owner_id=owner, status="interrupted",
                        error=f"对账失败：{str(exc)[:300]}", expected_attempt=current.attempt)
                    if self.service.get_thread(owner, tid).status in {"active", "waiting_user", "stopping"}:
                        self.service.store.update_thread(tid, owner_id=owner, status="needs_recovery", summary="执行句柄需人工对账，未重新提交")
        current_thread = self.service.get_thread(owner, tid)
        if current_thread.status == "stopping" and all(n.status in NODE_TERMINAL_STATUSES for n in self.service.list_nodes(owner, tid)):
            self.service.store.update_thread(tid, owner_id=owner, status="cancelled", summary="所有节点已确认停止")

    def _tick_archived_thread(self, thread: Any) -> None:
        """Collect executions hidden by older versions without resuming a chain."""
        owner, tid = thread.owner_id, thread.thread_id
        for node in self.service.list_nodes(owner, tid):
            if not has_unsettled_execution(node) or node.executor_type not in self.domain.registry:
                continue
            adapter = self.domain.registry.get(node.executor_type)
            try:
                if isinstance(adapter, RuntimeAdapter):
                    if adapter.reconcile_binding(node):
                        adapter.sync(owner, tid, node.node_id, attempt=node.attempt)
                    continue
                requires_review = node.executor_type in {"novel_stage", "supervised_chat"}
                if node.executor_type == "writer_pipeline":
                    ref = adapter._ref(node)
                    # The ordinary Writer accessor ticks its pipeline. Archived
                    # reconciliation must only read the existing execution.
                    record = self.app.writer.store.get_active_pipeline(ref["book_id"]) or {}
                    pipeline_id = str(node.checkpoint.get("pipeline_id") or node.executor_id or "")
                    if not pipeline_id or str(record.get("id") or "") != pipeline_id:
                        raise AdapterError("已归档节点的原流水线不存在或已被替换")
                    output = f"writer://{ref['book_id']}/{ref['stage']}/{node.executor_id}"
                elif node.executor_type == "novel_stage":
                    ref, record = adapter._task(owner, node)
                    output = f"novel://{ref['book_id']}/{ref['stage']}/{node.executor_id}"
                elif node.executor_type == "supervised_chat":
                    supervision = self.supervised.store.get(tid, owner)
                    if not supervision or supervision["node_id"] != node.node_id:
                        raise AdapterError("已归档节点的监督记录不存在")
                    run_id = str(supervision["state"].get("run_id") or "")
                    record = self.app.storage.get_background_task(run_id) or {}
                    if not record or record.get("conversation_id") != supervision["conversation_id"]:
                        raise AdapterError("已归档监督任务的原执行尚未确认")
                    terminal = {"completed", "failed", "cancelled", "interrupted"}
                    if any(job.get("status") not in terminal for job in self.supervised._background_jobs(supervision, record)):
                        continue
                    output = f"run://{run_id}"
                else:
                    continue
                status = str(record.get("status") or "")
                if status not in {"done", "completed", "failed", "cancelled", "interrupted", "conflict"}:
                    continue
                if node.status == "interrupted":
                    node = self.service.store.update_node(node.node_id, owner_id=owner, status="ready", expected_attempt=node.attempt)
                if node.status == "ready":
                    node = self.service.store.update_node(node.node_id, owner_id=owner, status="running", expected_attempt=node.attempt)
                if node.cancel_requested:
                    self.service.store.update_node(node.node_id, owner_id=owner, status="cancelled",
                        expected_attempt=node.attempt)
                elif status in {"done", "completed"} and requires_review:
                    error = "归档前的执行已结束；恢复任务后确认结果，未推进后续步骤"
                    if node.status != "waiting_user" or node.error != error or node.output_refs != [output]:
                        self.service.store.update_node(node.node_id, owner_id=owner, status="waiting_user",
                            output_refs=[output], error=error, expected_attempt=node.attempt)
                else:
                    event = ("task_cancelled" if node.cancel_requested or status == "cancelled"
                             else "run_interrupted" if status == "interrupted"
                             else "job_failed" if status in {"failed", "conflict"} else "run_completed")
                    self.service.emit_event(owner, tid, event, node_id=node.node_id, source=node.executor_type,
                        attempt=node.attempt, idempotency_key=f"archived-result:{node.node_id}:{node.attempt}:{status}",
                        payload={"output_refs": [output], "error": str(record.get("error") or "执行失败")})
                self.service.create_artifact(owner, tid, node.node_id, kind=node.executor_type,
                    path_or_ref=output, title="归档前的执行结果", summary=f"执行状态：{status}；未推进后续步骤",
                    idempotency_key=f"archived-artifact:{node.node_id}:{node.attempt}")
            except (AdapterError, LookupError, ValueError, RuntimeError) as exc:
                error = f"已归档执行需核对：{str(exc)[:250]}"
                current = self.service.get_node(owner, node.node_id)
                if current.error != error:
                    self.service.store.update_node(node.node_id, owner_id=owner, error=error, expected_attempt=node.attempt)

    def start(self) -> None:
        if self.thread is not None:
            return
        def loop() -> None:
            while not self.stop_event.is_set():
                self.wake_event.wait(0.5)
                self.wake_event.clear()
                if self.stop_event.is_set():
                    break
                # Temporary isolated apps may dispose their DB before shutting
                # down; never recreate an empty database or leak a worker error.
                if not self.service.store.db_path.is_file():
                    break
                self.tick()
        self.thread = threading.Thread(target=loop, name="super-agent-reconcile", daemon=True)
        self.thread.start()

    def shutdown(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        shutdown = getattr(self.supervised, "shutdown", None)
        if callable(shutdown):
            shutdown(timeout=5)
