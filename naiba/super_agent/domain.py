from __future__ import annotations

import uuid
from typing import Any

from naiba.core.contracts import (SUPER_AGENT_EXECUTION_KEY, SUPERVISED_ACCEPT_COMPLETION_ACTION,
                                  SuperAgentChatConfirmation)
from naiba.super_agent.adapters import AdapterResult
from naiba.super_agent.domain_adapters import NovelStageAdapter, WriterPipelineAdapter
from naiba.super_agent.router import AdapterRegistry, FastPathRequest, FastPathRouter


WRITER_CHAIN = ("setup", "outline", "chapter_plan", "chapter_content")
# The public chain vocabulary intentionally stays stable.  ``import`` and
# ``review`` are coordination gates, not Novel prompt stages; the executable
# generation stages are mapped below to the real Novel service vocabulary.
NOVEL_CHAIN = ("import", "elements", "outline", "screenplay", "review")


def _graph(stages: tuple[str, ...]) -> list[dict[str, Any]]:
    """Pure linear graph builder; no app/store access and deterministic ordering."""
    return [
        {
            "stage_key": stage,
            "sequence": index,
            "dependencies": [stages[index - 1]] if index else [],
            "promote_to": stages[index + 1] if index + 1 < len(stages) else "",
            "promotion_policy": {"on": "success", "once": True},
        }
        for index, stage in enumerate(stages)
    ]


def build_writer_graph() -> list[dict[str, Any]]:
    return _graph(WRITER_CHAIN)


def build_novel_graph() -> list[dict[str, Any]]:
    return _graph(NOVEL_CHAIN)


class DomainCoordinator:
    """Explicit P3 bridge between the task domain and Writer/Novel services."""

    DOMAIN_EXECUTORS = {"writer": "writer_pipeline", "novel": "novel_stage"}
    WRITER_STAGES = {"setup_all", "setup_step", "outline", "outline_text", "outline_all", "split", "outline_review", "chapter", "chapter_segs", "chapter_continue", "chapter_rewrite"}
    NOVEL_STAGES = {"elements", "outline", "script", "storyboard", "adapt"}
    WRITER_CHAIN_STAGE_MAP = {
        "setup": "setup_all", "outline": "outline_all", "chapter_plan": "split", "chapter_content": "chapter",
    }
    # ``import`` and ``review`` are coordination gates in the public chain,
    # not prompt stages.  Only the three middle nodes call NovelService.
    NOVEL_CHAIN_STAGE_MAP = {
        "import": "import", "elements": "elements", "outline": "outline",
        "screenplay": "script", "review": "review",
    }
    NOVEL_CHAIN_GATE_KEYS = frozenset({"import", "review"})

    def __init__(self, app: Any):
        self.app = app
        self.service = app.super_agent
        from naiba.super_agent.results import ExecutionResults
        self.results = ExecutionResults(app, self.service)
        self.service.results = self.results
        self.registry = AdapterRegistry([
            WriterPipelineAdapter(self.service, app),
            NovelStageAdapter(self.service, app),
        ])
        self.router = FastPathRouter(self.service, self.registry)
        from naiba.super_agent.runtime import ExecutionCoordinator
        self.runtime = ExecutionCoordinator(self)

    def chat_confirmations(self, owner_id: str, thread_id: str) -> list[SuperAgentChatConfirmation]:
        """Project live confirmations only for a durably bound current attempt."""
        from naiba.super_agent.runtime import current_execution_claims

        thread = self.service.get_thread(owner_id, thread_id)
        if thread.status in {"completed", "cancelled", "archived", "failed", "stopping"}:
            return []
        snapshotter = getattr(getattr(self.app, "runs", None), "confirmation_snapshot", None)
        if not callable(snapshotter):
            return []
        result: list[SuperAgentChatConfirmation] = []
        for node in self.service.list_nodes(owner_id, thread_id):
            if node.executor_type == "subagent":
                if (not node.executor_id or node.cancel_requested
                        or node.status not in {"running", "waiting_user"}):
                    continue
                # Reuse the same durable owner/type/correlation fence as sync;
                # the live manager revalidates it once more on actual approval.
                try:
                    record = self.registry.get("subagent")._record(node)
                except (LookupError, ValueError, RuntimeError):
                    continue
                if record.get("status") not in {"queued", "running", "waiting"}:
                    continue
                sub_snapshotter = getattr(self.app.runs, "subagent_confirmation_snapshot", None)
                items = sub_snapshotter(node.executor_id) if callable(sub_snapshotter) else []
                current = self.service.get_node(owner_id, node.node_id)
                if (current.attempt != node.attempt or current.executor_id != node.executor_id
                        or current.cancel_requested or current.status not in {"running", "waiting_user"}):
                    continue
                for item in items:
                    if (item.get("run_id") == node.executor_id
                            and item.get("conversation_id") == node.checkpoint.get("conversation_id")
                            and item.get("executor_kind") == "subagent"
                            and item.get("status") in {"pending", "processing"}):
                        result.append({key: item[key] for key in (
                            "confirm_id", "run_id", "conversation_id", "tool", "reason", "status",
                            "executor_kind", "parameters", "scope", "action_source",
                        ) if key in item} | {"node_id": node.node_id})
                continue
            if (node.executor_type not in {"chat_run", "supervised_chat"} or not node.executor_id or node.cancel_requested
                    or node.status not in {"running", "waiting_user"}):
                continue
            conversation_id = str(node.checkpoint.get("conversation_id") or "")
            run = self.app.storage.get_background_task(node.executor_id) or {}
            if (not conversation_id or str(run.get("conversation_id") or "") != conversation_id
                    or run.get("kind") != "chat" or run.get("status") not in {"running", "waiting"}):
                continue
            frozen = self.app.storage.get_run_snapshot(node.executor_id) or {}
            correlation = frozen.get(SUPER_AGENT_EXECUTION_KEY)
            if node.executor_type == "supervised_chat":
                rounds = self.runtime.supervised.store.rounds(thread_id)
                if (not rounds or str(rounds[-1].get("correlation") or "") != str(correlation or "")
                        or rounds[-1].get("run_id") != node.executor_id
                        or (not correlation and not rounds[-1].get("adopted_human"))):
                    continue
            elif not correlation or not any(claim.get("correlation") == correlation for claim in current_execution_claims(node).values()):
                continue
            confirmations = snapshotter(node.executor_id)
            current = self.service.get_node(owner_id, node.node_id)
            if (current.attempt != node.attempt or current.executor_id != node.executor_id
                    or current.cancel_requested or current.status not in {"running", "waiting_user"}):
                continue
            for item in confirmations:
                if (item.get("run_id") == node.executor_id and item.get("conversation_id") == conversation_id
                        and item.get("status") in {"pending", "processing"}):
                    result.append({key: item[key] for key in (
                        "confirm_id", "run_id", "conversation_id", "tool", "reason", "status",
                    )} | {"node_id": node.node_id})
        if self.service.get_thread(owner_id, thread_id).status in {
            "completed", "cancelled", "archived", "failed", "stopping",
        }:
            return []
        return result

    @staticmethod
    def _secret_keys(value: Any, path: str = "") -> list[str]:
        """Find credential-shaped fields before any request can reach a checkpoint."""
        found: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key).strip().lower()
                normalized = key_text.replace("_", "").replace("-", "")
                current = f"{path}.{key_text}" if path else key_text
                if normalized in {"apikey", "accesstoken", "authorization", "token", "secret",
                                  "provider", "providers", "providerconfig", "baseurl"}:
                    found.append(current)
                else:
                    found.extend(DomainCoordinator._secret_keys(item, current))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                found.extend(DomainCoordinator._secret_keys(item, f"{path}[{index}]"))
        return found

    def _validate_model_selection(
        self, model_key: str, provider_id: str, model_name: str, *, context: str,
    ) -> dict[str, str]:
        """Resolve only an existing global profile; never accept inline credentials."""
        model_key = str(model_key or "").strip()
        provider_id = str(provider_id or "").strip()
        model_name = str(model_name or "").strip()
        if not model_name:
            raise ValueError(f"{context} 必须包含 model_name；请先选择模型")
        if not model_key and not provider_id:
            # Old single-stage callers may only provide a model name. Keep that
            # compatibility path; the UI always sends the formal identifiers.
            return {"model_key": "", "provider_id": "", "model_name": model_name}
        if model_key and ":" in model_key:
            derived_provider = model_key.split(":", 1)[1].strip()
            if provider_id and provider_id != derived_provider:
                raise ValueError(f"{context} 的 provider_id 与 model_key 不匹配")
            provider_id = provider_id or derived_provider
        profiles = self.app.config.model_profiles() if callable(getattr(self.app.config, "model_profiles", None)) else []
        matched = None
        for profile in profiles or []:
            key = str(profile.get("model_key") or "").strip()
            if key == model_key or (not model_key and str(profile.get("id") or "").strip() == provider_id):
                matched = profile
                break
        if matched is None and callable(getattr(self.app.config, "profile", None)) and model_key:
            try:
                matched = self.app.config.profile(model_key)
            except (LookupError, ValueError, KeyError):
                matched = None
        if matched is None:
            raise ValueError(f"{context} 的供应商或模型不存在，请先在设置中检查配置")
        canonical_key = str(matched.get("model_key") or model_key).strip()
        # Derive identity from the matched global profile, NEVER from the
        # untrusted provider_id being checked. Bare keys remain valid for old
        # in-process profiles (e.g. probe), with the key as their identity.
        canonical_provider = str(matched.get("id") or "").strip()
        key_provider = canonical_key.split(":", 1)[1].strip() if ":" in canonical_key else ""
        if canonical_provider and key_provider and canonical_provider != key_provider:
            raise ValueError(f"{context} 的全局供应商配置不一致")
        canonical_provider = canonical_provider or key_provider or canonical_key
        if provider_id and provider_id != canonical_provider:
            raise ValueError(f"{context} 的 provider_id 与全局供应商不匹配")
        return {"model_key": canonical_key, "provider_id": canonical_provider, "model_name": model_name}

    MODEL_FIELDS = ("provider_id", "model_key", "model_name")

    @classmethod
    def _common_request(cls, body: dict[str, Any]) -> dict[str, Any]:
        """Nested payload supplies defaults; duplicate model fields must agree.

        Missing fields can be completed by the top level, but an explicit empty
        value is not treated as missing (nor silently replaced using ``or``).
        """
        raw = body.get("start_request", {})
        if not isinstance(raw, dict):
            raise ValueError("start_request 必须是对象")
        request = dict(raw)
        for key in cls.MODEL_FIELDS:
            if key not in body:
                continue
            value = str(body[key] or "").strip()
            if key in request and str(request[key] or "").strip() != value:
                raise ValueError(f"start_request.{key} 与顶层 {key} 矛盾")
            request[key] = value
        return request

    @classmethod
    def _stage_request(cls, common: dict[str, Any], supplied: dict[str, Any]) -> dict[str, Any]:
        """Stage > common; changing identity discards the old selection tuple.

        A name-only override stays on the common provider. A different key or
        provider must select its own name and resolve its own paired identity.
        """
        request = dict(common)
        if any(key in supplied and str(supplied[key] or "").strip() != str(common.get(key) or "").strip()
               for key in ("model_key", "provider_id")):
            for key in cls.MODEL_FIELDS:
                request.pop(key, None)
        request.update(supplied)
        return request

    @staticmethod
    def _execution_snapshot(body: dict[str, Any], selection: dict[str, str]) -> dict[str, str]:
        # model_name belongs to the checkpoint, not the snapshot schema. Chain
        # Thread snapshots describe ONLY the common choice, never an override.
        snapshot = {key: str(body[key] or "")
                    for key in ("agent_id", "workspace_dir", "permission_mode") if key in body}
        snapshot.update({key: selection[key] for key in ("provider_id", "model_key") if selection.get(key)})
        return snapshot

    def _validate(self, domain: str, stage: str, book_id: str) -> str:
        domain = str(domain or "").strip().lower()
        stage = str(stage or "").strip()
        book_id = str(book_id or "").strip()
        if domain not in self.DOMAIN_EXECUTORS:
            raise ValueError("domain 必须是 writer 或 novel")
        allowed = self.WRITER_STAGES if domain == "writer" else self.NOVEL_STAGES
        if stage not in allowed:
            raise ValueError(f"{domain} 不支持阶段: {stage or '（空）'}")
        if not book_id:
            raise ValueError("缺少 book_id")
        try:
            if domain == "writer":
                self.app.writer.store.get_book(book_id)
            else:
                self.app.novel.store.get_book(book_id)
        except LookupError:
            raise
        return self.DOMAIN_EXECUTORS[domain]

    def create_thread(self, owner_id: str, body: dict[str, Any]) -> tuple[Any, Any]:
        if not isinstance(body, dict):
            raise ValueError("请求体必须是对象")
        secret_fields = self._secret_keys(body)
        if secret_fields:
            raise ValueError("任务总管不接受 API Key 或访问口令；请先在全局供应商设置中配置")
        domain = str(body.get("domain") or "").strip().lower()
        self.runtime.policy(body)  # reject invalid booleans before any persistence
        if domain == "mixed":
            return self.runtime.create_mixed(owner_id, body)
        if domain in {"chat", "job", "subagent"}:
            return self.runtime.create(owner_id, body)
        if self._is_chain_request(body):
            return self._create_chain(owner_id, body, domain)
        stage = str(body.get("stage") or "").strip()
        book_id = str(body.get("book_id") or "").strip()
        executor_type = self._validate(domain, stage, book_id)
        if body.get("executor_type") and str(body.get("executor_type")) != executor_type:
            raise ValueError("executor_type 与领域不匹配")
        title = str(body.get("title") or ("织书台任务" if domain == "writer" else "拆书匠任务")).strip()
        goal = str(body.get("goal") or f"完成 {domain}:{stage}").strip()
        request = self._common_request(body)
        selection = self._validate_model_selection(
            request.get("model_key"), request.get("provider_id"), request.get("model_name"),
            context="start_request",
        )
        request.update(selection)
        if domain == "writer":
            if not str(request.get("kind") or stage).strip():
                raise ValueError("Writer start_request 缺少 kind")
            request.setdefault("kind", stage)
            request.setdefault("chapter_no", 1)
        else:
            request.setdefault("book_id", book_id)
            request.setdefault("stage", stage)
            if not isinstance(request.get("chapter_ids"), list):
                request["chapter_ids"] = []
        thread = self.service.create_thread(
            owner_id, title, goal,
            autonomy_level="confirm",
            execution_snapshot=self._execution_snapshot(body, selection),
        )
        ref = {"domain": domain, "book_id": book_id, "stage": stage, "domain_task_id": "", "domain_revision": 0}
        chain_version = int(body.get("chain_version") or 1)
        chain_instance_id = str(body.get("chain_instance_id") or f"{domain}:{book_id}:{uuid.uuid4().hex[:12]}")
        stage_key = str(body.get("stage_key") or stage)
        sequence = int(body.get("sequence") or 1)
        # start_request is an explicit domain payload; do not strip its stage or
        # book identifier, because the existing domain service validates them.
        self.service.store.update_thread(thread.thread_id, owner_id=owner_id, chain_version=chain_version)
        node = self.service.create_node(
            owner_id, thread.thread_id, f"{domain}:{stage}",
            idempotency_key=str(body.get("idempotency_key") or f"{domain}:{book_id}:{stage}"),
            executor_type=executor_type, stage_key=stage_key, sequence=sequence,
            chain_instance_id=chain_instance_id, status="ready", checkpoint={"domain_ref": ref, "start_request": request, "apply_request": dict(body.get("apply_request") or {}),
                "_super_agent_v1": self.runtime.policy(body)},
        )
        self.service.store.update_thread(
            thread.thread_id, owner_id=owner_id,
            linked_domain_task_ids=[f"{domain}:{book_id}:{stage}"],
        )
        return self.service.get_thread(owner_id, thread.thread_id), self.service.get_node(owner_id, node.node_id)

    @staticmethod
    def _is_chain_request(body: dict[str, Any]) -> bool:
        return any(key in body for key in ("chain", "chain_mode", "stages", "stage_requests"))

    def _create_chain(self, owner_id: str, body: dict[str, Any], domain: str) -> tuple[Any, Any]:
        """Create a complete, whitelisted Writer/Novel graph atomically."""
        if domain not in self.DOMAIN_EXECUTORS:
            raise ValueError("domain 必须是 writer 或 novel")
        book_id = str(body.get("book_id") or "").strip()
        if not book_id:
            raise ValueError("缺少 book_id")
        try:
            if domain == "writer":
                self.app.writer.store.get_book(book_id)
            else:
                self.app.novel.store.get_book(book_id)
        except LookupError:
            raise
        chain_name = str(body.get("chain") or body.get("chain_mode") or "").strip()
        expected_name = f"{domain}_v1"
        if chain_name and chain_name != expected_name:
            raise ValueError(f"不支持的 {domain} chain: {chain_name}")
        graph = build_writer_graph() if domain == "writer" else build_novel_graph()
        stage_map = self.WRITER_CHAIN_STAGE_MAP if domain == "writer" else self.NOVEL_CHAIN_STAGE_MAP
        raw_requests = body.get("stage_requests") if isinstance(body.get("stage_requests"), dict) else {}
        common = self._common_request(body)
        common_selection = {}
        if str(common.get("model_name") or "").strip():
            common_selection = self._validate_model_selection(
                common.get("model_key"), common.get("provider_id"), common.get("model_name"),
                context="start_request",
            )
            common.update(common_selection)
        # A chain may provide one model for all executable stages or a model
        # per stage.  Coordination gates (Novel import/review) do not call a
        # model and therefore must not force an otherwise unrelated model name.
        chain_instance_id = str(body.get("chain_instance_id") or "").strip()
        if not chain_instance_id:
            request_key = str(body.get("idempotency_key") or "").strip()
            chain_instance_id = f"{domain}:{book_id}:{request_key}" if request_key else f"{domain}:{book_id}:{uuid.uuid4().hex}"
        chain_version = int(body.get("chain_version") or 1)
        if chain_version < 1:
            raise ValueError("chain_version 必须是正整数")
        nodes: list[dict[str, Any]] = []
        linked_ids: list[str] = []
        for spec in graph:
            stage_key = str(spec["stage_key"])
            actual_stage = stage_map[stage_key]
            supplied = raw_requests.get(stage_key)
            if supplied is None:
                supplied = raw_requests.get(actual_stage)
            if supplied is not None and not isinstance(supplied, dict):
                raise ValueError(f"stage_requests.{stage_key} 必须是对象")
            request = self._stage_request(common, dict(supplied or {}))
            selection = {}
            if domain == "writer" or stage_key not in self.NOVEL_CHAIN_GATE_KEYS:
                selection = self._validate_model_selection(
                    request.get("model_key"), request.get("provider_id"), request.get("model_name"),
                    context=f"stage_requests.{stage_key}",
                )
                request.update(selection)
            else:
                # Coordination gates retain no model credentials and do not
                # inherit an unrelated model snapshot.
                for key in ("provider_id", "model_key", "model_name"):
                    request.pop(key, None)
            if domain == "writer":
                request.setdefault("kind", actual_stage)
                request.setdefault("chapter_no", 1)
            elif stage_key in self.NOVEL_CHAIN_GATE_KEYS:
                # Gates retain the book identity but never carry a fake
                # ``stage`` that could be mistaken for a Novel prompt stage.
                request.setdefault("book_id", book_id)
                request["coordination"] = stage_key
            else:
                request.setdefault("book_id", book_id)
                request["stage"] = actual_stage
                if not isinstance(request.get("chapter_ids"), list):
                    request["chapter_ids"] = []
            apply_requests = body.get("apply_requests") if isinstance(body.get("apply_requests"), dict) else {}
            apply_request = apply_requests.get(stage_key)
            if not isinstance(apply_request, dict):
                apply_request = body.get("apply_request") if isinstance(body.get("apply_request"), dict) else {}
            apply_request = dict(apply_request)
            if domain == "novel" and stage_key not in self.NOVEL_CHAIN_GATE_KEYS:
                apply_request.setdefault("book_id", book_id)
                apply_request.setdefault("stage", actual_stage)
                if "chapter_ids" not in apply_request and isinstance(request.get("chapter_ids"), list):
                    apply_request["chapter_ids"] = list(request["chapter_ids"])
            ref = {"domain": domain, "book_id": book_id, "stage": actual_stage,
                   "stage_key": stage_key, "coordination": stage_key in self.NOVEL_CHAIN_GATE_KEYS,
                   "domain_task_id": "", "domain_revision": 0}
            linked_ids.append(f"{domain}:{book_id}:{stage_key}:{actual_stage}")
            nodes.append({
                "stage_key": stage_key,
                "sequence": int(spec["sequence"]),
                "dependencies": list(spec["dependencies"]),
                "promote_to": str(spec.get("promote_to") or ""),
                "promotion_policy": dict(spec.get("promotion_policy") or {"on": "success", "once": True}),
                "kind": f"{domain}:{stage_key}",
                "executor_type": self.DOMAIN_EXECUTORS[domain],
                "idempotency_key": f"{chain_instance_id}:{stage_key}",
                "status": "ready" if int(spec["sequence"]) == 0 else "blocked",
                "checkpoint": {"domain_ref": ref, "start_request": request, "apply_request": apply_request,
                               "_super_agent_v1": self.runtime.policy(body),
                               "chain_stage": {"key": stage_key, "actual_stage": actual_stage,
                                               "coordination": bool(ref["coordination"]) }},
                "execution_snapshot": self._execution_snapshot(body, selection),
            })
        title = str(body.get("title") or ("织书台阶段链" if domain == "writer" else "拆书匠阶段链")).strip()
        goal = str(body.get("goal") or f"完成 {domain} 阶段链").strip()
        thread, created_nodes = self.service.create_thread_chain(
            owner_id, title, goal, chain_instance_id=chain_instance_id,
            chain_version=chain_version, nodes=nodes,
            autonomy_level="confirm", linked_domain_task_ids=linked_ids,
            execution_snapshot=self._execution_snapshot(body, common_selection),
        )
        if not created_nodes:
            raise RuntimeError("阶段链创建后没有 Node")
        return thread, created_nodes[0]

    def action(self, owner_id: str, thread_id: str, body: dict[str, Any]) -> AdapterResult:
        if not isinstance(body, dict):
            raise ValueError("请求体必须是对象")
        node_id = str(body.get("node_id") or "").strip()
        action = str(body.get("action") or "status").strip()
        request_id = str(body.get("request_id") or body.get("idempotency_key") or "").strip()
        if not node_id and action in {"pause", "continue", "cancel", "resume", SUPERVISED_ACCEPT_COMPLETION_ACTION}:
            return self.runtime.control(owner_id, thread_id, action, request_id)
        if not node_id:
            raise ValueError("缺少 node_id")
        if action in {"start", "cancel", "retry", "resume"} and not request_id:
            raise ValueError("控制操作必须提供 request_id 或 idempotency_key")
        node = self.service.get_node(owner_id, node_id)
        if node.thread_id != thread_id:
            raise LookupError("Node 不属于当前 Thread")
        return self.runtime.command(owner_id, thread_id, node_id, action, request_id,
            attempt=body.get("attempt"), payload=dict(body.get("payload") or {}))

    def resolve_approval(self, owner_id: str, thread_id: str, approval_id: str, body: dict[str, Any]) -> Any:
        with self.runtime.lock:
            thread = self.service.get_thread(owner_id, thread_id)
            if thread.status in {"cancelled", "archived"}:
                raise ValueError("取消或归档任务不能批准执行")
            paused = thread.status == "paused"
            result = self._resolve_approval(owner_id, thread_id, approval_id, body)
            if paused and self.service.get_thread(owner_id, thread_id).status == "active":
                self.service.store.update_thread(thread_id, owner_id=owner_id, status="paused")
            self.runtime.wake_event.set()
            return result

    def _resolve_approval(self, owner_id: str, thread_id: str, approval_id: str, body: dict[str, Any]) -> Any:
        if not isinstance(body, dict):
            raise ValueError("请求体必须是对象")
        status = str(body.get("status") or "").strip()
        request_id = str(body.get("request_id") or "").strip()
        resolved = self.service.resolve_approval(owner_id, thread_id, approval_id, status=status, resolved_by=owner_id, request_id=request_id)
        if status == "approved":
            node = self.service.get_node(owner_id, resolved.node_id)
            # Replaying the same approved request is a read-only idempotent
            # acknowledgement; never call the domain write-back twice.
            if node.status in {"succeeded", "failed", "cancelled"}:
                return resolved
            adapter = self.registry.get(node.executor_type)
            apply_method = getattr(adapter, "apply", None)
            ref = node.checkpoint.get("domain_ref") if isinstance(node.checkpoint, dict) else {}
            # ``review`` is a coordination gate, not a Novel prompt stage;
            # approving it completes the gate without calling api_apply.
            if bool(isinstance(ref, dict) and ref.get("coordination") and ref.get("stage_key") == "review"):
                self.service.emit_event(
                    owner_id, thread_id, "run_completed", node_id=node.node_id,
                    source="novel_stage", payload={"coordination": "review"},
                    source_event_id=f"novel-review-done:{node.node_id}:{node.attempt}",
                    idempotency_key=f"novel-review-done:{node.node_id}:{node.attempt}",
                    attempt=node.attempt,
                )
                return resolved
            if callable(apply_method):
                task_id = str((ref or {}).get("domain_task_id") or "")
                task = self.app.studio.jobs.task(task_id) or {}
                text = str(task.get("result") or task.get("result_partial") or "")
                if text.strip():
                    apply_method(owner_id, thread_id, node.node_id, text=text, request_id=request_id)
        return resolved
