"""Read final executor results using durable node ownership and attempt fences.

The display endpoint and downstream input share this reader. No arbitrary URI,
last-message fallback, trace, model request, or authorization snapshot is read.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from naiba.core.redaction import redact_execution_text

from naiba.core.contracts import (
    SUBAGENT_OUTCOME_KEY, SUPER_AGENT_CLAIMS_KEY, SUPER_AGENT_EXECUTION_KEY,
    SUPER_AGENT_DEPENDENCY_MANIFEST_KEY, SUPER_AGENT_DEPENDENCY_MAX_ITEMS,
    SUPER_AGENT_DEPENDENCY_ITEM_CHARS, SUPER_AGENT_DEPENDENCY_TOTAL_CHARS,
)


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class ExecutionResults:
    def __init__(self, app: Any, service: Any):
        self.app = app
        self.service = service

    def _node(self, owner_id: str, thread_id: str, node_id: str) -> Any:
        self.service.get_thread(owner_id, thread_id)
        node = self.service.get_node(owner_id, node_id)
        if node.thread_id != thread_id:
            raise LookupError("结果节点不属于当前任务")
        return node

    @staticmethod
    def _claims(node: Any, attempt: int) -> list[dict[str, Any]]:
        result = []
        for key, value in _object(node.checkpoint.get(SUPER_AGENT_CLAIMS_KEY)).items():
            if not isinstance(value, dict):
                continue
            action = str(key).partition(":")[0]
            base = value.get("attempt")
            if action not in {"start", "retry", "resume"} or not isinstance(base, int) or isinstance(base, bool):
                continue
            if _object(value.get("result")).get("submission") == "not_submitted":
                continue
            if base + (1 if action in {"retry", "resume"} else 0) == attempt:
                result.append(value)
        return result

    def _safe_text(self, value: str) -> str:
        """Final prose only, with known configured credentials removed as well."""
        secrets: set[str] = set()
        def visit(item: Any) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    normalized = str(key).lower().replace("_", "").replace("-", "")
                    if normalized in {"apikey", "token", "accesstoken", "secret", "password", "authorization"}:
                        if isinstance(child, str) and len(child) >= 6:
                            secrets.add(child)
                    else:
                        visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)
        visit(getattr(getattr(self.app, "config", None), "data", {}))
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, "[已脱敏]")
        return redact_execution_text(value)

    def _read(self, node: Any, executor_id: str, attempt: int, *, strict: bool = False) -> dict[str, Any]:
        row = self.app.storage.get_background_task(executor_id) if executor_id else None
        cid = str(node.checkpoint.get("conversation_id") or "")
        if not row or not cid or str(row.get("conversation_id") or "") != cid:
            raise ValueError("执行结果不存在或会话归属不匹配")
        kind = str(row.get("kind") or "")
        is_chat = node.executor_type in {"chat_run", "supervised_chat"}
        if ((is_chat and kind != "chat") or (node.executor_type == "subagent" and kind != "subagent")
                or (node.executor_type == "job" and kind in {"chat", "plan_execute"})):
            raise ValueError("结果执行类型不匹配")
        if not is_chat and str(row.get("owner_session_id") or "") != cid:
            raise ValueError("结果 Job owner 不匹配")
        snapshot = _object(self.app.storage.get_run_snapshot(executor_id))
        correlation = str(snapshot.get(SUPER_AGENT_EXECUTION_KEY) or
                          _object(snapshot.get("params")).get(SUPER_AGENT_EXECUTION_KEY) or "")
        claims = self._claims(node, attempt)
        bound = bool(correlation and any(claim.get("correlation") == correlation for claim in claims))
        if node.executor_type == "job":
            bound = (attempt == node.attempt and executor_id == node.executor_id
                     and executor_id == str(node.checkpoint.get("job_id") or "")) or any(
                         _object(c.get("result")).get("executor_id") == executor_id for c in claims)
        elif node.executor_type == "supervised_chat":
            from naiba.super_agent.supervision_store import SupervisionStore
            rounds = SupervisionStore(self.app.storage).rounds(node.thread_id)
            bound = any(r.get("run_id") == executor_id and str(r.get("correlation") or "") == correlation
                        and (bool(correlation) or r.get("adopted_human")) for r in rounds)
        if not bound:
            raise ValueError("结果与节点 attempt 的持久执行关联不匹配")
        status = str(row.get("status") or "unknown")
        if strict and (status != "completed" or node.status != "succeeded" or attempt != node.attempt):
            raise ValueError("依赖结果尚未成功完成或 attempt 已失效")
        text, message_id, compatibility, failure_summary = "", "", "current", ""
        if is_chat:
            message_id = str(_object(row.get("detail")).get("message_id") or "")
            with self.app.storage._connect() as db:
                message = db.execute("SELECT role,content,metadata FROM messages WHERE id=? AND conversation_id=?",
                                     (message_id, cid)).fetchone()
            if message is not None:
                try:
                    metadata = _object(json.loads(message["metadata"] or "{}"))
                except (ValueError, TypeError):
                    metadata = {}
                if message["role"] != "assistant" or metadata.get("run_id") != executor_id:
                    raise ValueError("最终回复不属于指定 Run")
                if metadata.get("partial") or _object(metadata.get("truncated")).get("truncated"):
                    compatibility = "incomplete"
                text = str(message["content"] or "")
        else:
            result = _object(row.get("result"))
            # Only final human-readable fields are allowed out. Never dump
            # result as JSON: providers may also keep headers, traces or params.
            fields = ("response",) if kind == "subagent" else ("response", "output", "text", "summary")
            text = next((result[key] for key in fields if isinstance(result.get(key), str) and result[key].strip()), "")
            if kind == "subagent":
                outcome = _object(result.get(SUBAGENT_OUTCOME_KEY))
                if not outcome:
                    compatibility = "legacy_unverified"
                elif type(outcome.get("version")) is not int or outcome["version"] != 1:
                    compatibility = "unknown_outcome"
                elif outcome.get("status") in {"not_executed", "failed"}:
                    status = str(outcome["status"])
                    failure_summary = str(outcome.get("summary") or row.get("error") or "子助手动作未执行")
                    text = text or failure_summary
                elif outcome.get("status") != "completed":
                    compatibility = "unknown_outcome"
        if strict and (status != "completed" or compatibility != "current"):
            raise ValueError("依赖结果未完成、正文不完整或缺少新版执行结论，需人工处理")
        if not text.strip():
            if strict:
                raise ValueError("依赖结果正文尚未落盘、已删除或不支持提取；未派发后继")
            compatibility = "missing" if status in {"completed", "failed", "not_executed"} else "pending"
            text = str(row.get("error") or ("结果正文不可用" if compatibility == "missing" else "尚未产生最终结果"))
        text = self._safe_text(text)
        summary = self._safe_text(failure_summary or str(row.get("error") or "") or text)[:500]
        protocol = "run" if is_chat else "job"
        return {"node_id": node.node_id, "executor_id": executor_id, "attempt": attempt,
                "status": status, "text": text, "summary": summary,
                "output_refs": [f"{protocol}://{executor_id}"], "compatibility": compatibility,
                "message_id": message_id, "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()}

    def list_results(self, owner_id: str, thread_id: str, node_id: str) -> list[dict[str, Any]]:
        node = self._node(owner_id, thread_id, node_id)
        if node.executor_type not in {"chat_run", "subagent", "job", "supervised_chat"}:
            raise ValueError("此步骤的结果请在原领域工作台查看")
        executions: dict[str, int] = {}
        for key, claim in _object(node.checkpoint.get(SUPER_AGENT_CLAIMS_KEY)).items():
            if not isinstance(claim, dict) or not isinstance(claim.get("attempt"), int):
                continue
            action = str(key).partition(":")[0]
            if action not in {"start", "retry", "resume"}:
                continue
            rid = str(_object(claim.get("result")).get("executor_id") or "")
            if rid:
                executions[rid] = claim["attempt"] + (1 if action in {"retry", "resume"} else 0)
        if node.executor_id:
            executions[node.executor_id] = node.attempt
        results = []
        for rid, attempt in sorted(executions.items(), key=lambda pair: pair[1], reverse=True):
            try:
                results.append(self._read(node, rid, attempt))
            except ValueError as exc:
                # Missing historical results remain visible; viewing must not
                # repair state or trigger execution. No foreign data is leaked.
                results.append({"node_id": node.node_id, "executor_id": rid, "attempt": attempt,
                                "status": "unavailable", "text": str(exc), "summary": str(exc),
                                "output_refs": [], "compatibility": "unavailable"})
        return results

    def dependency_context(self, owner_id: str, thread_id: str, node_id: str) -> dict[str, Any] | None:
        node = self._node(owner_id, thread_id, node_id)
        if not node.dependencies:
            return None
        if len(node.dependencies) > SUPER_AGENT_DEPENDENCY_MAX_ITEMS:
            raise ValueError(f"依赖共有 {len(node.dependencies)} 项，超过 {SUPER_AGENT_DEPENDENCY_MAX_ITEMS} 项预算；未派发")
        sources = []
        for dependency in node.dependencies:
            previous = self._node(owner_id, thread_id, dependency)
            if previous.executor_type not in {"chat_run", "job", "subagent"}:
                raise ValueError("前序领域结果尚无通用正文契约，请在原工作台交接；未派发")
            sources.append(self._read(previous, previous.executor_id, previous.attempt, strict=True))
        total = sum(len(item["text"]) for item in sources)
        if total > SUPER_AGENT_DEPENDENCY_TOTAL_CHARS or any(len(item["text"]) > SUPER_AGENT_DEPENDENCY_ITEM_CHARS for item in sources):
            raise ValueError(f"依赖正文共 {total} 字符，超过单项 {SUPER_AGENT_DEPENDENCY_ITEM_CHARS} / 总量 {SUPER_AGENT_DEPENDENCY_TOTAL_CHARS} 预算；已提供 0 字符，未截断派发，请查看完整结果并缩减")
        manifest = [{key: item[key] for key in ("node_id", "executor_id", "attempt", "content_hash")} for item in sources]
        previous_manifest = node.checkpoint.get(SUPER_AGENT_DEPENDENCY_MANIFEST_KEY)
        if previous_manifest is not None and previous_manifest != manifest:
            raise ValueError("已消费的前序 attempt 或结果版本已变化，请重新确认并创建任务")
        # Persist only references/hashes, not another copy of the result body.
        # The coordinator serializes this with claim/preflight/dispatch.
        if previous_manifest is None:
            checkpoint = dict(node.checkpoint)
            checkpoint[SUPER_AGENT_DEPENDENCY_MANIFEST_KEY] = manifest
            self.service.store.update_node(node.node_id, owner_id=owner_id, checkpoint=checkpoint,
                                           expected_attempt=node.attempt)
        text = "\n\n".join(f"步骤 {item['node_id']} / attempt {item['attempt']} / {item['output_refs'][0]}\n状态：{item['status']}\n{item['text']}" for item in sources)
        return {"thread_id": thread_id, "node_id": node_id, "sources": manifest, "text": text,
                "total_chars": total, "provided_chars": total, "truncated": False}
