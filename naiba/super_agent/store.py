from __future__ import annotations

import json
import hashlib
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from naiba.core.contracts import SUPER_AGENT_CLAIMS_KEY
from naiba.super_agent.models import (
    APPROVAL_STATUSES,
    EVENT_TYPES,
    NODE_STATUSES,
    NODE_TERMINAL_STATUSES,
    THREAD_TERMINAL_STATUSES,
    THREAD_STATUSES,
    NODE_TRANSITIONS,
    THREAD_TRANSITIONS,
    Approval,
    Artifact,
    TaskEvent,
    TaskNode,
    Thread,
)


def _now() -> int:
    return int(time.time() * 1000)


def _json(value: Any, default: Any) -> str:
    if isinstance(default, (list, dict)) and not isinstance(value, type(default)):
        value = default
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps(default, ensure_ascii=False)


def _load(value: Any, default: Any) -> Any:
    try:
        result = json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return default
    if isinstance(default, (list, dict)) and not isinstance(result, type(default)):
        return default.copy()
    return result


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _stable_id(prefix: str, value: str) -> str:
    """Return a deterministic local identifier for an idempotent chain."""
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


def has_unsettled_execution(node: TaskNode) -> bool:
    """Archiving must account for launch receipts as well as displayed status."""
    claims = node.checkpoint.get(SUPER_AGENT_CLAIMS_KEY) or {}
    if isinstance(claims, dict):
        for key, claim in claims.items():
            action = str(key).partition(":")[0]
            if action not in {"start", "retry", "resume"} or not isinstance(claim, dict):
                continue
            result = claim.get("result") or {}
            if isinstance(result, dict) and result.get("submission") == "not_submitted":
                continue
            attempt = claim.get("attempt")
            if type(attempt) is not int or not claim.get("correlation"):
                continue
            target = attempt + (action in {"retry", "resume"})
            # Retry reserves the next attempt before replacing the old terminal
            # node. That small window must also prevent archive.
            if claim.get("state") != "done" and target in {node.attempt, node.attempt + 1}:
                if target == node.attempt or action in {"retry", "resume"}:
                    return True
            if target == node.attempt and node.status not in NODE_TERMINAL_STATUSES:
                return True
    return (node.status not in NODE_TERMINAL_STATUSES
            and (node.status in {"running", "stopping"} or bool(node.executor_id)))


_SNAPSHOT_KEYS = {
    "agent_id", "provider_id", "model_key", "workspace_dir", "permission_mode",
    "enabled_tool_ids", "skill_policy", "vision_capabilities", "cost_policy",
}


def _snapshot(value: Any, *, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize the frozen execution configuration without accepting mutable aliases."""
    raw = dict(fallback or {}) if value is None else value
    if not isinstance(raw, dict):
        raise ValueError("execution_snapshot 必须是对象")
    unknown = set(raw) - _SNAPSHOT_KEYS
    if unknown:
        raise ValueError(f"execution_snapshot 包含未知字段: {sorted(unknown)}")
    result: dict[str, Any] = {}
    for key in _SNAPSHOT_KEYS:
        if key not in raw:
            continue
        item = raw[key]
        if key == "enabled_tool_ids":
            if not isinstance(item, (list, tuple)) or any(not isinstance(v, str) or not v.strip() for v in item):
                raise ValueError("execution_snapshot.enabled_tool_ids 必须是字符串列表")
            result[key] = list(dict.fromkeys(str(v).strip() for v in item))
        elif key in {"skill_policy", "vision_capabilities", "cost_policy"}:
            if not isinstance(item, dict):
                raise ValueError(f"execution_snapshot.{key} 必须是对象")
            result[key] = json.loads(json.dumps(item, ensure_ascii=False))
        else:
            if not isinstance(item, str):
                raise ValueError(f"execution_snapshot.{key} 必须是字符串")
            result[key] = item
    return result


class SuperAgentStore:
    """SQLite persistence for the independent Super-agent task domain."""

    def __init__(self, storage_or_db: Any):
        self.db_path = Path(getattr(storage_or_db, "db_path", storage_or_db))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def claim_command(self, owner_id: str, thread_id: str, node_id: str, action: str,
                      request_id: str) -> tuple[str, dict[str, Any], bool]:
        """Persist intent BEFORE touching an executor. Unfinished intents fail closed.

        Starts are unique per attempt, not per caller key. Retry/resume commands
        are unique across attempts so replay cannot create another generation.
        """
        from naiba.core.contracts import SUPER_AGENT_CLAIMS_KEY
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM super_nodes WHERE owner_id=? AND thread_id=? AND node_id=?",
                             (owner_id, thread_id, node_id)).fetchone()
            if row is None:
                raise LookupError("Node 不存在或无权访问")
            thread = db.execute("SELECT status FROM super_threads WHERE thread_id=? AND owner_id=?",
                                (thread_id, owner_id)).fetchone()
            if action in {"start", "retry", "resume"} and thread["status"] == "archived":
                raise RuntimeError("已归档任务不能派发新执行")
            checkpoint = _load(row["checkpoint"], {})
            claims = dict(checkpoint.get(SUPER_AGENT_CLAIMS_KEY) or {})
            # A rejected, provably unsubmitted start does not occupy an attempt
            # forever. Keep its receipt: replaying the *same* request is still a
            # rejection, while a new explicit request may try once more.
            for prior_key, prior in claims.items():
                if (isinstance(prior, dict) and prior.get("request_id") == request_id
                        and str(prior_key).partition(":")[0] == action):
                    return prior_key, dict(prior), False
            key = f"start:{row['attempt']}" if action == "start" else f"{action}:{request_id}"
            if action == "start":
                same_attempt = [(k, v) for k, v in claims.items() if isinstance(v, dict)
                                and str(k).startswith("start:") and v.get("attempt") == int(row["attempt"])]
                for prior_key, prior in same_attempt:
                    if not (prior.get("state") == "done" and isinstance(prior.get("result"), dict)
                            and prior["result"].get("submission") == "not_submitted"):
                        return prior_key, dict(prior), False
                if key in claims:
                    key += ":" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:24]
            if key in claims:
                return key, dict(claims[key]), False
            claim = {"state": "pending", "attempt": int(row["attempt"]),
                     "request_id": request_id,
                     "correlation": f"{node_id}:{row['attempt']}:{key}"}
            claims[key] = claim
            checkpoint[SUPER_AGENT_CLAIMS_KEY] = claims
            self._update_node_db(db, node_id, owner_id, {"checkpoint": checkpoint}, expected_attempt=int(row["attempt"]))
            db.commit()
            return key, claim, True
        finally:
            db.close()

    def settle_command(self, owner_id: str, node_id: str, key: str, result: dict[str, Any],
                       *, expected_attempt: int | None = None) -> None:
        from naiba.core.contracts import SUPER_AGENT_CLAIMS_KEY
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM super_nodes WHERE node_id=? AND owner_id=?", (node_id, owner_id)).fetchone()
            if row is None:
                raise LookupError("Node 不存在")
            if expected_attempt is not None and int(row["attempt"]) != expected_attempt:
                raise RuntimeError("Node attempt 已过期")
            checkpoint = _load(row["checkpoint"], {})
            claims = dict(checkpoint.get(SUPER_AGENT_CLAIMS_KEY) or {})
            claim = dict(claims.get(key) or {})
            claim.update(state="done", result=result)
            claims[key] = claim
            checkpoint[SUPER_AGENT_CLAIMS_KEY] = claims
            self._update_node_db(db, node_id, owner_id, {"checkpoint": checkpoint}, expected_attempt=int(row["attempt"]))
            db.commit()
        finally:
            db.close()

    def reconciliation_threads(self, limit: int = 64, *, after: str = "") -> list[Thread]:
        db = self._connect()
        try:
            rows = db.execute("SELECT * FROM super_threads t WHERE (status IN ('active','paused','stopping','waiting_user','needs_recovery','cancelled') "
                              "OR (status='archived' AND EXISTS (SELECT 1 FROM super_nodes n WHERE n.thread_id=t.thread_id "
                              "AND n.owner_id=t.owner_id AND n.status NOT IN ('succeeded','failed','cancelled') "
                              "AND (n.status IN ('running','stopping','interrupted','waiting_user') OR n.executor_id!='' "
                              "OR (json_valid(n.checkpoint) AND json_type(n.checkpoint,?)='object'))))) "
                              "AND thread_id>? ORDER BY thread_id LIMIT ?",
                              (f"$.{SUPER_AGENT_CLAIMS_KEY}", after, max(1, min(limit, 128)))).fetchall()
            return [self._thread(row) for row in rows]
        finally:
            db.close()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _thread(row: sqlite3.Row | None) -> Thread | None:
        if row is None:
            return None
        return Thread(
            thread_id=str(row["thread_id"]), owner_id=str(row["owner_id"]),
            title=str(row["title"]), goal=str(row["goal"]), status=str(row["status"]),
            workspace_dir=str(row["workspace_dir"]), router_agent_id=str(row["router_agent_id"]),
            autonomy_level=str(row["autonomy_level"]), summary=str(row["summary"]),
            chain_version=int(row["chain_version"] or 0),
            current_node_id=str(row["current_node_id"]),
            linked_conversation_ids=_load(row["linked_conversation_ids"], []),
            linked_run_ids=_load(row["linked_run_ids"], []),
            linked_job_ids=_load(row["linked_job_ids"], []),
            linked_domain_task_ids=_load(row["linked_domain_task_ids"], []),
            execution_snapshot=_load(row["execution_snapshot"], {}),
            schema_version=int(row["schema_version"] or 1), version=int(row["version"] or 1),
            last_event_at=int(row["last_event_at"] or 0), created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            archived_at=int(row["archived_at"]) if row["archived_at"] is not None else None,
        )

    @staticmethod
    def _node(row: sqlite3.Row | None) -> TaskNode | None:
        if row is None:
            return None
        return TaskNode(
            node_id=str(row["node_id"]), thread_id=str(row["thread_id"]),
            owner_id=str(row["owner_id"]), kind=str(row["kind"]), status=str(row["status"]),
            stage_key=str(row["stage_key"]), sequence=int(row["sequence"] or 0),
            promote_to=str(row["promote_to"]), promotion_policy=_load(row["promotion_policy"], {}),
            chain_instance_id=str(row["chain_instance_id"]),
            parent_id=str(row["parent_id"]), dependencies=_load(row["dependencies"], []),
            executor_type=str(row["executor_type"]), executor_id=str(row["executor_id"]),
            input_refs=_load(row["input_refs"], []), output_refs=_load(row["output_refs"], []),
            approval_required=bool(row["approval_required"]),
            retry_policy=_load(row["retry_policy"], {}), checkpoint=_load(row["checkpoint"], {}),
            attempt=int(row["attempt"] or 0), idempotency_key=str(row["idempotency_key"]),
            execution_snapshot=_load(row["execution_snapshot"], {}),
            cancel_requested=bool(row["cancel_requested"]), error=str(row["error"]),
            schema_version=int(row["schema_version"] or 1), version=int(row["version"] or 1),
            created_at=int(row["created_at"]), updated_at=int(row["updated_at"]),
        )

    @staticmethod
    def _event(row: sqlite3.Row | None) -> TaskEvent | None:
        if row is None:
            return None
        return TaskEvent(
            event_id=str(row["event_id"]), thread_id=str(row["thread_id"]),
            node_id=str(row["node_id"]), source=str(row["source"]),
            event_type=str(row["event_type"]), payload=_load(row["payload"], {}),
            source_event_id=str(row["source_event_id"]),
            idempotency_key=str(row["idempotency_key"]), attempt=int(row["attempt"] or 0),
            sequence=int(row["sequence"]), created_at=int(row["created_at"]),
            handled_at=int(row["handled_at"]) if row["handled_at"] is not None else None,
        )

    @staticmethod
    def _artifact(row: sqlite3.Row | None) -> Artifact | None:
        if row is None:
            return None
        return Artifact(
            artifact_id=str(row["artifact_id"]), thread_id=str(row["thread_id"]),
            node_id=str(row["node_id"]), kind=str(row["kind"]),
            path_or_ref=str(row["path_or_ref"]), title=str(row["title"]),
            summary=str(row["summary"]), version=int(row["version"] or 1),
            idempotency_key=str(row["idempotency_key"]), created_at=int(row["created_at"]),
        )

    @staticmethod
    def _approval(row: sqlite3.Row | None) -> Approval | None:
        if row is None:
            return None
        return Approval(
            approval_id=str(row["approval_id"]), thread_id=str(row["thread_id"]),
            node_id=str(row["node_id"]), action=str(row["action"]),
            description=str(row["description"]), risk_level=str(row["risk_level"]),
            status=str(row["status"]), idempotency_key=str(row["idempotency_key"]),
            requested_at=int(row["requested_at"]),
            resolved_at=int(row["resolved_at"]) if row["resolved_at"] is not None else None,
            resolved_by=str(row["resolved_by"]),
        )

    def create_thread(self, *, owner_id: str, title: str, goal: str = "", **fields: Any) -> Thread:
        owner_id = str(owner_id or "").strip()
        if not owner_id:
            raise ValueError("Thread owner_id 不能为空")
        title = str(title or "").strip()
        if not title:
            raise ValueError("Thread title 不能为空")
        status = str(fields.get("status") or "draft")
        if status not in THREAD_STATUSES:
            raise ValueError(f"非法 Thread 状态: {status}")
        now = _now()
        thread_id = str(fields.get("thread_id") or _id("thread"))
        snapshot = _snapshot(fields.get("execution_snapshot"), fallback={})
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO super_threads(thread_id,owner_id,title,goal,status,workspace_dir,"
                "router_agent_id,autonomy_level,summary,chain_version,current_node_id,linked_conversation_ids,"
                "linked_run_ids,linked_job_ids,linked_domain_task_ids,execution_snapshot,"
                "schema_version,version,last_event_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    thread_id, owner_id, title, str(goal or ""), status,
                    str(fields.get("workspace_dir") or ""), str(fields.get("router_agent_id") or ""),
                    str(fields.get("autonomy_level") or "confirm"), str(fields.get("summary") or ""),
                    int(fields.get("chain_version") or 0), str(fields.get("current_node_id") or ""), _json(fields.get("linked_conversation_ids"), []),
                    _json(fields.get("linked_run_ids"), []), _json(fields.get("linked_job_ids"), []),
                    _json(fields.get("linked_domain_task_ids"), []), _json(snapshot, {}), 1, 1, 0, now, now,
                ),
            )
            db.commit()
        finally:
            db.close()
        return self.get_thread(thread_id, owner_id=owner_id)  # type: ignore[return-value]

    def get_thread(self, thread_id: str, *, owner_id: str | None = None) -> Thread | None:
        db = self._connect()
        try:
            if owner_id is None:
                row = db.execute("SELECT * FROM super_threads WHERE thread_id=?", (thread_id,)).fetchone()
            else:
                row = db.execute("SELECT * FROM super_threads WHERE thread_id=? AND owner_id=?", (thread_id, owner_id)).fetchone()
            return self._thread(row)
        finally:
            db.close()

    def list_threads(self, *, owner_id: str, include_archived: bool = False) -> list[Thread]:
        db = self._connect()
        try:
            sql = "SELECT * FROM super_threads WHERE owner_id=?"
            args: list[Any] = [owner_id]
            if not include_archived:
                sql += " AND status != 'archived'"
            sql += " ORDER BY updated_at DESC"
            return [self._thread(row) for row in db.execute(sql, args).fetchall() if self._thread(row)]
        finally:
            db.close()

    def update_thread(self, thread_id: str, *, owner_id: str, expected_version: int | None = None, **updates: Any) -> Thread:
        allowed = {
            "title", "goal", "status", "workspace_dir", "router_agent_id", "autonomy_level",
            "summary", "chain_version", "current_node_id", "linked_conversation_ids", "linked_run_ids",
            "linked_job_ids", "linked_domain_task_ids", "execution_snapshot",
        }
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"不可更新的 Thread 字段: {sorted(unknown)}")
        if "execution_snapshot" in updates:
            raise ValueError("Execution snapshot 创建后不可修改")
        if "status" in updates and updates["status"] not in THREAD_STATUSES:
            raise ValueError(f"非法 Thread 状态: {updates['status']}")
        current_thread = self.get_thread(thread_id, owner_id=owner_id)
        if current_thread is None:
            raise LookupError("Thread 不存在或无权访问")
        if "status" in updates and updates["status"] != current_thread.status:
            if updates["status"] not in THREAD_TRANSITIONS.get(current_thread.status, set()):
                raise RuntimeError(f"非法 Thread 状态转换: {current_thread.status} -> {updates['status']}")
        if not updates:
            thread = self.get_thread(thread_id, owner_id=owner_id)
            if not thread:
                raise LookupError("Thread 不存在或无权访问")
            return thread
        now = _now()
        assignments: list[str] = []
        args: list[Any] = []
        list_fields = {"linked_conversation_ids", "linked_run_ids", "linked_job_ids", "linked_domain_task_ids"}
        for key, value in updates.items():
            assignments.append(f"{key}=?")
            args.append(_json(value, []) if key in list_fields else _json(value, {}) if key == "execution_snapshot" else value)
        if updates.get("status") == "archived":
            assignments.append("archived_at=?")
            args.append(now)
        elif "status" in updates:
            # 恢复（离开归档态）要清掉归档时间，列表口径以 status 为准（§5.1）。
            assignments.append("archived_at=NULL")
        assignments += ["version=version+1", "updated_at=?"]
        args += [now, thread_id, owner_id]
        where = "thread_id=? AND owner_id=?"
        if expected_version is not None:
            where += " AND version=?"
            args.append(expected_version)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            current_row = db.execute("SELECT status FROM super_threads WHERE thread_id=? AND owner_id=?",
                                     (thread_id, owner_id)).fetchone()
            if current_row is None:
                raise LookupError("Thread 不存在或无权访问")
            if "status" in updates and updates["status"] != current_row["status"]:
                if updates["status"] not in THREAD_TRANSITIONS.get(current_row["status"], set()):
                    raise RuntimeError(f"非法 Thread 状态转换: {current_row['status']} -> {updates['status']}")
            if updates.get("status") == "archived" and current_row["status"] != "archived":
                if current_row["status"] in {"active", "stopping"}:
                    raise RuntimeError("任务正在运行或停止中；请先取消再删除")
                nodes = db.execute("SELECT * FROM super_nodes WHERE thread_id=? AND owner_id=?",
                                   (thread_id, owner_id)).fetchall()
                if any(has_unsettled_execution(self._node(row)) for row in nodes):
                    raise RuntimeError("任务仍有运行、停止或尚未确认结果的执行；请先继续核对或取消，确认停止后再删除")
            cursor = db.execute(f"UPDATE super_threads SET {', '.join(assignments)} WHERE {where}", args)
            if cursor.rowcount != 1:
                db.rollback()
                raise RuntimeError("Thread 版本冲突或无权访问")
            db.commit()
        finally:
            db.close()
        return self.get_thread(thread_id, owner_id=owner_id)  # type: ignore[return-value]

    def create_node(self, *, owner_id: str, thread_id: str, kind: str, idempotency_key: str, **fields: Any) -> TaskNode:
        if not idempotency_key:
            raise ValueError("Task Node idempotency_key 不能为空")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            thread = db.execute("SELECT * FROM super_threads WHERE thread_id=? AND owner_id=?", (thread_id, owner_id)).fetchone()
            if thread is None:
                raise LookupError("Thread 不存在或无权访问")
            if str(thread["status"]) in {"completed", "failed", "cancelled", "archived"}:
                raise RuntimeError(f"终态 Thread 不能创建 Node: {thread['status']}")
            existing = db.execute("SELECT * FROM super_nodes WHERE thread_id=? AND idempotency_key=?", (thread_id, idempotency_key)).fetchone()
            if existing is not None:
                db.commit()
                return self._node(existing)  # type: ignore[return-value]
            snapshot = _snapshot(fields.get("execution_snapshot"), fallback=_load(thread["execution_snapshot"], {}))
            deps = list(fields.get("dependencies") or [])
            if any(not isinstance(dep, str) or not dep for dep in deps):
                raise ValueError("dependencies 必须是非空节点 ID 列表")
            for dep in deps:
                dep_row = db.execute("SELECT thread_id FROM super_nodes WHERE node_id=?", (dep,)).fetchone()
                if dep_row is None or str(dep_row["thread_id"]) != thread_id:
                    raise ValueError("dependencies 必须引用同一 Thread 中已存在的 Node")
            status = str(fields.get("status") or ("ready" if not deps else "pending"))
            if status not in NODE_STATUSES:
                raise ValueError(f"非法 Node 状态: {status}")
            now = _now()
            node_id = str(fields.get("node_id") or _id("node"))
            db.execute(
                "INSERT INTO super_nodes(node_id,thread_id,owner_id,parent_id,kind,status,stage_key,sequence,promote_to,promotion_policy,chain_instance_id,dependencies,"
                "executor_type,executor_id,input_refs,output_refs,approval_required,retry_policy,checkpoint,"
                "attempt,idempotency_key,execution_snapshot,cancel_requested,error,schema_version,version,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    node_id, thread_id, owner_id, str(fields.get("parent_id") or ""), kind, status,
                    str(fields.get("stage_key") or ""), int(fields.get("sequence") or 0), str(fields.get("promote_to") or ""),
                    _json(fields.get("promotion_policy"), {}), str(fields.get("chain_instance_id") or ""),
                    _json(deps, []), str(fields.get("executor_type") or "fake"), str(fields.get("executor_id") or ""),
                    _json(fields.get("input_refs"), []), _json(fields.get("output_refs"), []),
                    1 if fields.get("approval_required") else 0, _json(fields.get("retry_policy"), {}),
                    _json(fields.get("checkpoint"), {}), int(fields.get("attempt") or 0), idempotency_key,
                    _json(snapshot, {}), 0, str(fields.get("error") or ""), 1, 1, now, now,
                ),
            )
            db.execute("UPDATE super_threads SET status='active', current_node_id=?, version=version+1, updated_at=? WHERE thread_id=?", (node_id, now, thread_id))
            db.commit()
        finally:
            db.close()
        return self.get_node(node_id, owner_id=owner_id)  # type: ignore[return-value]

    def create_chain(self, *, owner_id: str, thread_id: str, chain_instance_id: str, nodes: list[dict[str, Any]]) -> list[TaskNode]:
        """Create a chain in one transaction on an existing Thread.

        ``create_node`` intentionally remains the legacy single-node API.  A
        chain cannot use it because dependencies must point at rows inserted in
        the same transaction and a failed graph must leave no partial nodes.
        """
        if not chain_instance_id or not nodes:
            raise ValueError("chain_instance_id 和 nodes 不能为空")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            thread = db.execute("SELECT * FROM super_threads WHERE thread_id=? AND owner_id=?", (thread_id, owner_id)).fetchone()
            if thread is None:
                raise LookupError("Thread 不存在或无权访问")
            self._insert_chain_nodes_db(db, thread_id=thread_id, owner_id=owner_id,
                                        chain_instance_id=chain_instance_id, nodes=nodes)
            first = db.execute(
                "SELECT node_id FROM super_nodes WHERE thread_id=? AND owner_id=? AND chain_instance_id=? ORDER BY sequence LIMIT 1",
                (thread_id, owner_id, chain_instance_id),
            ).fetchone()
            if first is not None:
                db.execute(
                    "UPDATE super_threads SET current_node_id=?, status='active', version=version+1, updated_at=? WHERE thread_id=? AND owner_id=?",
                    (str(first["node_id"]), _now(), thread_id, owner_id),
                )
            db.commit()
        finally:
            db.close()
        return self.list_chain(thread_id, owner_id=owner_id, chain_instance_id=chain_instance_id)

    def _validate_existing_chain_db(self, db: sqlite3.Connection, *, thread_id: str,
                                    owner_id: str, chain_instance_id: str,
                                    nodes: list[dict[str, Any]]) -> list[sqlite3.Row]:
        """Verify a replayed graph without repairing a damaged one."""
        specs = [dict(item) for item in nodes]
        stage_keys = [str(item.get("stage_key") or "").strip() for item in specs]
        by_stage = {key: _stable_id("node", f"{chain_instance_id}:{key}") for key in stage_keys}
        rows = db.execute(
            "SELECT * FROM super_nodes WHERE thread_id=? AND owner_id=? AND chain_instance_id=? ORDER BY sequence, created_at",
            (thread_id, owner_id, chain_instance_id),
        ).fetchall()
        if len(rows) != len(specs):
            raise RuntimeError("幂等 Thread 已存在但阶段链不完整")
        for index, (row, item) in enumerate(zip(rows, specs)):
            stage_key = stage_keys[index]
            raw_deps = item.get("dependencies")
            deps = list(raw_deps) if isinstance(raw_deps, list) else ([stage_keys[index - 1]] if index else [])
            expected_deps = [by_stage.get(str(dep or ""), str(dep or "")) for dep in deps]
            promote = str(item.get("promote_to") or "")
            expected_promote = by_stage.get(promote, promote) if promote else (by_stage[stage_keys[index + 1]] if index + 1 < len(specs) else "")
            expected_idem = str(item.get("idempotency_key") or f"{chain_instance_id}:{stage_key}")
            if (
                str(row["node_id"]) != by_stage[stage_key]
                or str(row["stage_key"]) != stage_key
                or int(row["sequence"] or 0) != index
                or str(row["promote_to"] or "") != expected_promote
                or _load(row["dependencies"], []) != expected_deps
                or str(row["idempotency_key"]) != expected_idem
            ):
                raise RuntimeError("幂等 Thread 已存在但阶段链结构已损坏")
        thread = db.execute("SELECT current_node_id,status FROM super_threads WHERE thread_id=? AND owner_id=?", (thread_id, owner_id)).fetchone()
        node_ids = {str(row["node_id"]) for row in rows}
        if thread is None or (str(thread["current_node_id"] or "") and str(thread["current_node_id"]) not in node_ids):
            raise RuntimeError("幂等 Thread 已存在但当前阶段游标已损坏")
        return rows

    def create_thread_chain(self, *, owner_id: str, title: str, goal: str = "",
                            chain_instance_id: str, chain_version: int,
                            nodes: list[dict[str, Any]], **fields: Any) -> tuple[Thread, list[TaskNode]]:
        """Atomically create (or replay) a Thread and its complete graph."""
        owner_id = str(owner_id or "").strip()
        title = str(title or "").strip()
        if not owner_id or not title:
            raise ValueError("Thread owner_id/title 不能为空")
        if not chain_instance_id or not nodes:
            raise ValueError("chain_instance_id 和 nodes 不能为空")
        thread_id = str(fields.get("thread_id") or _stable_id("thread_chain", f"{owner_id}:{chain_instance_id}"))
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM super_threads WHERE thread_id=? AND owner_id=?", (thread_id, owner_id)).fetchone()
            if existing is not None:
                existing_nodes = self._validate_existing_chain_db(
                    db, thread_id=thread_id, owner_id=owner_id,
                    chain_instance_id=chain_instance_id, nodes=nodes,
                )
                db.commit()
                return self._thread(existing), [self._node(row) for row in existing_nodes if self._node(row)]  # type: ignore[return-value]
            status = str(fields.get("status") or "active")
            if status not in THREAD_STATUSES:
                raise ValueError(f"非法 Thread 状态: {status}")
            now = _now()
            snapshot = _snapshot(fields.get("execution_snapshot"), fallback={})
            db.execute(
                "INSERT INTO super_threads(thread_id,owner_id,title,goal,status,workspace_dir,"
                "router_agent_id,autonomy_level,summary,chain_version,current_node_id,linked_conversation_ids,"
                "linked_run_ids,linked_job_ids,linked_domain_task_ids,execution_snapshot,"
                "schema_version,version,last_event_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    thread_id, owner_id, title, str(goal or ""), status,
                    str(fields.get("workspace_dir") or ""), str(fields.get("router_agent_id") or ""),
                    str(fields.get("autonomy_level") or "confirm"), str(fields.get("summary") or ""),
                    int(chain_version), "", _json(fields.get("linked_conversation_ids"), []),
                    _json(fields.get("linked_run_ids"), []), _json(fields.get("linked_job_ids"), []),
                    _json(fields.get("linked_domain_task_ids"), []), _json(snapshot, {}),
                    1, 1, 0, now, now,
                ),
            )
            self._insert_chain_nodes_db(db, thread_id=thread_id, owner_id=owner_id,
                                        chain_instance_id=chain_instance_id, nodes=nodes)
            first = db.execute(
                "SELECT node_id FROM super_nodes WHERE thread_id=? AND owner_id=? AND chain_instance_id=? ORDER BY sequence LIMIT 1",
                (thread_id, owner_id, chain_instance_id),
            ).fetchone()
            if first is None:
                raise RuntimeError("阶段链没有首节点")
            db.execute(
                "UPDATE super_threads SET current_node_id=?, status='active', version=version+1, updated_at=? WHERE thread_id=? AND owner_id=?",
                (str(first["node_id"]), now, thread_id, owner_id),
            )
            db.commit()
            thread_row = db.execute("SELECT * FROM super_threads WHERE thread_id=? AND owner_id=?", (thread_id, owner_id)).fetchone()
            node_rows = db.execute(
                "SELECT * FROM super_nodes WHERE thread_id=? AND owner_id=? AND chain_instance_id=? ORDER BY sequence, created_at",
                (thread_id, owner_id, chain_instance_id),
            ).fetchall()
            return self._thread(thread_row), [self._node(row) for row in node_rows if self._node(row)]  # type: ignore[return-value]
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _insert_chain_nodes_db(self, db: sqlite3.Connection, *, thread_id: str,
                               owner_id: str, chain_instance_id: str,
                               nodes: list[dict[str, Any]]) -> None:
        """Validate and insert a complete graph while ``db`` is locked."""
        if not nodes:
            raise ValueError("阶段链不能为空")
        specs = [dict(item) for item in nodes]
        stage_keys = [str(item.get("stage_key") or "").strip() for item in specs]
        if any(not key for key in stage_keys) or len(set(stage_keys)) != len(stage_keys):
            raise ValueError("阶段链 stage_key 必须唯一且非空")
        by_stage = {key: _stable_id("node", f"{chain_instance_id}:{key}") for key in stage_keys}
        sequences = [int(item.get("sequence") if item.get("sequence") is not None else index) for index, item in enumerate(specs)]
        if sequences != list(range(len(specs))):
            raise ValueError("阶段链 sequence 必须从 0 连续递增")
        now = _now()
        for index, item in enumerate(specs):
            stage_key = stage_keys[index]
            raw_deps = item.get("dependencies")
            deps = list(raw_deps) if isinstance(raw_deps, list) else ([stage_keys[index - 1]] if index else [])
            dep_ids: list[str] = []
            for dep in deps:
                dep_key = str(dep or "")
                dep_ids.append(by_stage.get(dep_key, dep_key))
            if any(dep_id not in by_stage.values() for dep_id in dep_ids):
                raise ValueError("dependencies 必须引用本链 stage_key")
            promote = str(item.get("promote_to") or "")
            promote_id = by_stage.get(promote, promote) if promote else (by_stage[stage_keys[index + 1]] if index + 1 < len(specs) else "")
            if promote_id and promote_id not in by_stage.values():
                raise ValueError("promote_to 必须引用本链 stage_key")
            expected_status = "ready" if index == 0 else "blocked"
            status = str(item.get("status") or expected_status)
            if status != expected_status:
                raise ValueError("阶段链只能首节点 ready、后续节点 blocked")
            idem = str(item.get("idempotency_key") or f"{chain_instance_id}:{stage_key}")
            existing = db.execute("SELECT * FROM super_nodes WHERE thread_id=? AND idempotency_key=?", (thread_id, idem)).fetchone()
            if existing is not None:
                if str(existing["chain_instance_id"]) != chain_instance_id or str(existing["stage_key"]) != stage_key:
                    raise RuntimeError("阶段链幂等键已被其他 Node 占用")
                continue
            db.execute(
                "INSERT INTO super_nodes(node_id,thread_id,owner_id,parent_id,kind,status,stage_key,sequence,promote_to,promotion_policy,chain_instance_id,dependencies,"
                "executor_type,executor_id,input_refs,output_refs,approval_required,retry_policy,checkpoint,attempt,idempotency_key,execution_snapshot,cancel_requested,error,schema_version,version,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    by_stage[stage_key], thread_id, owner_id, str(item.get("parent_id") or ""),
                    str(item.get("kind") or stage_key), status, stage_key, sequences[index], promote_id,
                    _json(item.get("promotion_policy") or {"on": "success", "once": True}, {}), chain_instance_id,
                    _json(dep_ids, []), str(item.get("executor_type") or "fake"), str(item.get("executor_id") or ""),
                    _json(item.get("input_refs"), []), _json(item.get("output_refs"), []),
                    1 if item.get("approval_required") else 0, _json(item.get("retry_policy"), {}),
                    _json(item.get("checkpoint"), {}), int(item.get("attempt") or 0), idem,
                    _json(item.get("execution_snapshot"), {}), 0, str(item.get("error") or ""), 1, 1, now, now,
                ),
            )

    def get_node(self, node_id: str, *, owner_id: str | None = None) -> TaskNode | None:
        db = self._connect()
        try:
            sql = "SELECT * FROM super_nodes WHERE node_id=?"
            args: list[Any] = [node_id]
            if owner_id is not None:
                sql += " AND owner_id=?"
                args.append(owner_id)
            return self._node(db.execute(sql, args).fetchone())
        finally:
            db.close()

    def list_nodes(self, thread_id: str, *, owner_id: str) -> list[TaskNode]:
        db = self._connect()
        try:
            return [self._node(row) for row in db.execute("SELECT * FROM super_nodes WHERE thread_id=? AND owner_id=? ORDER BY created_at", (thread_id, owner_id)).fetchall() if self._node(row)]
        finally:
            db.close()

    def _update_node_db(self, db: sqlite3.Connection, node_id: str, owner_id: str, updates: dict[str, Any], *, expected_attempt: int | None = None) -> TaskNode:
        allowed = {"status", "output_refs", "checkpoint", "attempt", "cancel_requested", "error", "approval_required", "executor_id"}
        if set(updates) - allowed:
            raise ValueError(f"不可更新的 Node 字段: {sorted(set(updates) - allowed)}")
        if "status" in updates and updates["status"] not in NODE_STATUSES:
            raise ValueError(f"非法 Node 状态: {updates['status']}")
        current_row = db.execute("SELECT * FROM super_nodes WHERE node_id=? AND owner_id=?", (node_id, owner_id)).fetchone()
        if current_row is None:
            raise LookupError("Node 不存在或无权访问")
        if updates.get("status") == "running" or ("attempt" in updates and updates["attempt"] != current_row["attempt"]):
            thread = db.execute("SELECT status FROM super_threads WHERE thread_id=? AND owner_id=?",
                                (current_row["thread_id"], owner_id)).fetchone()
            if thread["status"] == "archived" and (
                    updates.get("attempt", current_row["attempt"]) != current_row["attempt"]
                    or not has_unsettled_execution(self._node(current_row))):
                raise RuntimeError("已归档任务不能派发新执行")
        if "status" in updates and updates["status"] != current_row["status"]:
            if updates["status"] not in NODE_TRANSITIONS.get(str(current_row["status"]), set()):
                raise RuntimeError(f"非法 Node 状态转换: {current_row['status']} -> {updates['status']}")
        assignments: list[str] = []
        args: list[Any] = []
        json_fields = {"output_refs": [], "checkpoint": {}}
        for key, value in updates.items():
            assignments.append(f"{key}=?")
            args.append(_json(value, json_fields[key]) if key in json_fields else 1 if key == "cancel_requested" and value else 0 if key == "cancel_requested" else value)
        assignments += ["version=version+1", "updated_at=?"]
        args.extend([_now(), node_id, owner_id])
        where = "node_id=? AND owner_id=?"
        if expected_attempt is not None:
            where += " AND attempt=?"
            args.append(expected_attempt)
        cursor = db.execute(f"UPDATE super_nodes SET {', '.join(assignments)} WHERE {where}", args)
        if cursor.rowcount != 1:
            raise RuntimeError("Node 不存在、无权访问或 attempt 已过期")
        row = db.execute("SELECT * FROM super_nodes WHERE node_id=?", (node_id,)).fetchone()
        return self._node(row)  # type: ignore[return-value]

    def update_node(self, node_id: str, *, owner_id: str, expected_attempt: int | None = None, **updates: Any) -> TaskNode:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            result = self._update_node_db(db, node_id, owner_id, updates, expected_attempt=expected_attempt)
            db.commit()
            return result
        finally:
            db.close()

    def _promote_ready_nodes_db(self, db: sqlite3.Connection, thread_id: str, owner_id: str) -> list[TaskNode]:
        rows = db.execute("SELECT * FROM super_nodes WHERE thread_id=? AND owner_id=? AND status='pending'", (thread_id, owner_id)).fetchall()
        changed: list[TaskNode] = []
        for row in rows:
            deps = _load(row["dependencies"], [])
            if not deps:
                continue
            states = [db.execute("SELECT status FROM super_nodes WHERE node_id=? AND thread_id=?", (dep, thread_id)).fetchone() for dep in deps]
            if all(item is not None and item["status"] == "succeeded" for item in states):
                db.execute("UPDATE super_nodes SET status='ready', version=version+1, updated_at=? WHERE node_id=?", (_now(), row["node_id"]))
                changed.append(self._node(db.execute("SELECT * FROM super_nodes WHERE node_id=?", (row["node_id"],)).fetchone()))  # type: ignore[arg-type]
            elif any(item is not None and item["status"] in {"failed", "cancelled"} for item in states):
                db.execute("UPDATE super_nodes SET status='blocked', version=version+1, updated_at=? WHERE node_id=?", (_now(), row["node_id"]))
        return changed

    def promote_ready_nodes(self, thread_id: str, *, owner_id: str) -> list[TaskNode]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            changed = self._promote_ready_nodes_db(db, thread_id, owner_id)
            db.commit()
            return changed
        finally:
            db.close()

    def list_chain(self, thread_id: str, *, owner_id: str, chain_instance_id: str = "") -> list[TaskNode]:
        db = self._connect()
        try:
            sql = "SELECT * FROM super_nodes WHERE thread_id=? AND owner_id=?"
            args: list[Any] = [thread_id, owner_id]
            if chain_instance_id:
                sql += " AND chain_instance_id=?"
                args.append(chain_instance_id)
            sql += " ORDER BY sequence, created_at"
            return [self._node(row) for row in db.execute(sql, args).fetchall() if self._node(row)]
        finally:
            db.close()

    def _promote_chain_node_db(self, db: sqlite3.Connection, thread_id: str, node_id: str,
                               *, owner_id: str, expected_attempt: int | None = None,
                               request_id: str = "") -> tuple[sqlite3.Row, bool]:
        """Promote one successful current node and append its audit event.

        The caller owns the transaction.  The source/target state change, the
        Thread cursor, and ``stage_promoted`` are therefore one durable unit.
        """
        current = db.execute(
            "SELECT * FROM super_nodes WHERE node_id=? AND thread_id=? AND owner_id=?",
            (node_id, thread_id, owner_id),
        ).fetchone()
        if current is None:
            raise LookupError("Chain Node 不存在或无权访问")
        if not str(current["chain_instance_id"] or ""):
            return current, False
        if str(current["status"]) != "succeeded":
            return current, False
        if expected_attempt is not None and int(current["attempt"] or 0) != expected_attempt:
            return current, False
        thread = db.execute(
            "SELECT current_node_id,status FROM super_threads WHERE thread_id=? AND owner_id=?",
            (thread_id, owner_id),
        ).fetchone()
        if thread is None:
            raise LookupError("Thread 不存在或无权访问")
        # A stale completion from a previous stage/attempt is observable but
        # cannot move the cursor after a newer stage has become current.
        if str(thread["current_node_id"] or "") != node_id:
            return current, False
        if str(thread["status"] or "") in {"paused", "needs_recovery", "cancelled", "completed", "failed", "archived"}:
            return current, False
        target_id = str(current["promote_to"] or "")
        target = None
        if target_id:
            target = db.execute(
                "SELECT * FROM super_nodes WHERE node_id=? AND thread_id=? AND owner_id=? AND chain_instance_id=?",
                (target_id, thread_id, owner_id, current["chain_instance_id"]),
            ).fetchone()
        if target is not None:
            if str(target["status"]) not in {"blocked", "ready"}:
                return target, False
            now = _now()
            db.execute(
                "UPDATE super_nodes SET status='ready', version=version+1, updated_at=? WHERE node_id=? AND status='blocked'",
                (now, target["node_id"]),
            )
            db.execute(
                "UPDATE super_threads SET current_node_id=?, status='active', version=version+1, updated_at=? WHERE thread_id=? AND owner_id=?",
                (target["node_id"], now, thread_id, owner_id),
            )
            event_key = f"stage-promote:{node_id}:{int(current['attempt'] or 0)}"
            exists = db.execute(
                "SELECT 1 FROM super_events WHERE thread_id=? AND idempotency_key=?",
                (thread_id, event_key),
            ).fetchone()
            if exists is None:
                sequence = int(db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM super_events WHERE thread_id=?", (thread_id,)).fetchone()[0])
                now_event = _now()
                payload = {
                    "from_node_id": str(current["node_id"]),
                    "from_stage": str(current["stage_key"] or ""),
                    "to_node_id": str(target["node_id"]),
                    "to_stage": str(target["stage_key"] or ""),
                    "chain_instance_id": str(current["chain_instance_id"] or ""),
                    "request_id": str(request_id or ""),
                }
                db.execute(
                    "INSERT INTO super_events(event_id,thread_id,node_id,source,event_type,payload,source_event_id,idempotency_key,attempt,sequence,created_at,handled_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (_id("event"), thread_id, str(current["node_id"]), "super-agent", "stage_promoted",
                     _json(payload, {}), event_key, event_key, int(current["attempt"] or 0), sequence, now_event, now_event),
                )
                db.execute("UPDATE super_threads SET last_event_at=?, version=version+1, updated_at=? WHERE thread_id=?", (now_event, now_event, thread_id))
            return db.execute("SELECT * FROM super_nodes WHERE node_id=?", (target["node_id"],)).fetchone(), True  # type: ignore[return-value]
        now = _now()
        db.execute(
            "UPDATE super_threads SET current_node_id=?, status='completed', version=version+1, updated_at=? WHERE thread_id=? AND owner_id=?",
            (node_id, now, thread_id, owner_id),
        )
        event_key = f"stage-promote:{node_id}:{int(current['attempt'] or 0)}"
        exists = db.execute(
            "SELECT 1 FROM super_events WHERE thread_id=? AND idempotency_key=?",
            (thread_id, event_key),
        ).fetchone()
        if exists is None:
            sequence = int(db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM super_events WHERE thread_id=?", (thread_id,)).fetchone()[0])
            now_event = _now()
            payload = {
                "from_node_id": str(current["node_id"]),
                "from_stage": str(current["stage_key"] or ""),
                "to_node_id": "",
                "to_stage": "",
                "chain_instance_id": str(current["chain_instance_id"] or ""),
                "terminal": True,
                "request_id": str(request_id or ""),
            }
            db.execute(
                "INSERT INTO super_events(event_id,thread_id,node_id,source,event_type,payload,source_event_id,idempotency_key,attempt,sequence,created_at,handled_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (_id("event"), thread_id, str(current["node_id"]), "super-agent", "stage_promoted", _json(payload, {}), event_key, event_key, int(current["attempt"] or 0), sequence, now_event, now_event),
            )
            db.execute("UPDATE super_threads SET last_event_at=?, version=version+1, updated_at=? WHERE thread_id=?", (now_event, now_event, thread_id))
        return current, True


    def promote_chain_node(self, thread_id: str, node_id: str, *, owner_id: str,
                           expected_attempt: int | None = None, request_id: str = "") -> TaskNode | None:
        """Public atomic promotion entry point, safe across process restarts."""
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            result, _ = self._promote_chain_node_db(
                db, thread_id, node_id, owner_id=owner_id,
                expected_attempt=expected_attempt, request_id=request_id,
            )
            db.commit()
            return self._node(result)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def append_event_and_apply(self, *, owner_id: str, thread_id: str, event_type: str, source: str, node_id: str = "", payload: dict[str, Any] | None = None, source_event_id: str = "", idempotency_key: str = "", attempt: int = 0) -> tuple[TaskEvent, bool]:
        """Insert and apply a node event in one transaction.

        A late/duplicate event is still auditable, but it cannot cross the
        attempt fence or revive a terminal node. ``handled_at`` is written in
        the same transaction as the state change, so a crash cannot leave an
        applied node with an unhandled duplicate or vice versa.
        """
        if event_type not in EVENT_TYPES:
            raise ValueError(f"非法任务事件类型: {event_type}")
        key = idempotency_key or source_event_id
        if not key:
            raise ValueError("任务事件必须提供稳定的 idempotency_key 或 source_event_id")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM super_threads WHERE thread_id=? AND owner_id=?", (thread_id, owner_id)).fetchone() is None:
                raise LookupError("Thread 不存在或无权访问")
            if node_id:
                node = db.execute("SELECT 1 FROM super_nodes WHERE node_id=? AND thread_id=? AND owner_id=?", (node_id, thread_id, owner_id)).fetchone()
                if node is None:
                    raise LookupError("事件的 Node 不存在或不属于当前 Thread")
            existing = db.execute("SELECT * FROM super_events WHERE thread_id=? AND idempotency_key=?", (thread_id, key)).fetchone()
            if existing is not None:
                db.commit()
                return self._event(existing), False  # type: ignore[return-value]
            if source_event_id:
                existing = db.execute("SELECT * FROM super_events WHERE thread_id=? AND source=? AND source_event_id=?", (thread_id, source, source_event_id)).fetchone()
                if existing is not None:
                    db.commit()
                    return self._event(existing), False  # type: ignore[return-value]
            sequence = int(db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM super_events WHERE thread_id=?", (thread_id,)).fetchone()[0])
            now = _now()
            event_id = _id("event")
            stored_source_event_id = source_event_id or event_id
            db.execute(
                "INSERT INTO super_events(event_id,thread_id,node_id,source,event_type,payload,source_event_id,idempotency_key,attempt,sequence,created_at,handled_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, thread_id, node_id, source, event_type, _json(payload or {}, {}), stored_source_event_id, key, attempt, sequence, now, None),
            )
            db.execute("UPDATE super_threads SET last_event_at=?, version=version+1, updated_at=? WHERE thread_id=?", (now, now, thread_id))
            applied = False
            status_by_event = {"run_started": "running", "progress": "running", "job_completed": "succeeded", "run_completed": "succeeded", "job_failed": "failed", "task_cancelled": "cancelled", "run_interrupted": "interrupted"}
            next_status = status_by_event.get(event_type)
            if node_id and next_status:
                row = db.execute("SELECT * FROM super_nodes WHERE node_id=? AND owner_id=? AND thread_id=?", (node_id, owner_id, thread_id)).fetchone()
                thread_row = db.execute("SELECT status FROM super_threads WHERE thread_id=? AND owner_id=?", (thread_id, owner_id)).fetchone()
                if row is not None and thread_row is not None and int(row["attempt"] or 0) == attempt and not bool(row["cancel_requested"]) and str(thread_row["status"]) not in {"cancelled", "paused", "stopping"} and str(row["status"]) not in NODE_TERMINAL_STATUSES:
                    updates: dict[str, Any] = {"status": next_status}
                    if next_status == "failed":
                        updates["error"] = str((payload or {}).get("error") or "任务失败")
                    if next_status == "succeeded" and isinstance((payload or {}).get("output_refs"), list):
                        updates["output_refs"] = (payload or {}).get("output_refs")
                    self._update_node_db(db, node_id, owner_id, updates, expected_attempt=attempt)
                    applied = True
                    if next_status == "failed":
                        thread_status = str(thread_row["status"])
                        if thread_status not in THREAD_TERMINAL_STATUSES and thread_status not in {"paused", "cancelled"}:
                            db.execute("UPDATE super_threads SET status='failed', version=version+1, updated_at=? WHERE thread_id=? AND owner_id=?", (now, thread_id, owner_id))
                    elif next_status == "interrupted":
                        thread_status = str(thread_row["status"])
                        if thread_status == "active":
                            db.execute("UPDATE super_threads SET status='needs_recovery', summary='服务中断，任务需恢复', version=version+1, updated_at=? WHERE thread_id=? AND owner_id=?", (now, thread_id, owner_id))
                    elif next_status == "succeeded" and str(thread_row["status"]) != "archived":
                        if str(row["chain_instance_id"] or ""):
                            self._promote_chain_node_db(
                                db, thread_id, node_id, owner_id=owner_id,
                                expected_attempt=attempt, request_id=key,
                            )
                        else:
                            self._promote_ready_nodes_db(db, thread_id, owner_id)
                            statuses = [item["status"] for item in db.execute("SELECT status FROM super_nodes WHERE thread_id=? AND owner_id=?", (thread_id, owner_id)).fetchall()]
                            if statuses and all(status in {"succeeded", "cancelled"} for status in statuses):
                                db.execute("UPDATE super_threads SET status='completed', version=version+1, updated_at=? WHERE thread_id=? AND owner_id=?", (now, thread_id, owner_id))
            if applied or not node_id or event_type not in status_by_event:
                db.execute("UPDATE super_events SET handled_at=? WHERE event_id=?", (_now(), event_id))
            db.commit()
            return self._event(db.execute("SELECT * FROM super_events WHERE event_id=?", (event_id,)).fetchone()), True  # type: ignore[return-value]
        finally:
            db.close()

    def append_event(self, *, owner_id: str, thread_id: str, event_type: str, source: str, node_id: str = "", payload: dict[str, Any] | None = None, source_event_id: str = "", idempotency_key: str = "", attempt: int = 0) -> tuple[TaskEvent, bool]:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"非法任务事件类型: {event_type}")
        key = idempotency_key or source_event_id
        if not key:
            raise ValueError("任务事件必须提供稳定的 idempotency_key 或 source_event_id")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM super_threads WHERE thread_id=? AND owner_id=?", (thread_id, owner_id)).fetchone() is None:
                raise LookupError("Thread 不存在或无权访问")
            if node_id:
                node = db.execute("SELECT 1 FROM super_nodes WHERE node_id=? AND thread_id=? AND owner_id=?", (node_id, thread_id, owner_id)).fetchone()
                if node is None:
                    raise LookupError("事件的 Node 不存在或不属于当前 Thread")
            existing = db.execute("SELECT * FROM super_events WHERE thread_id=? AND idempotency_key=?", (thread_id, key)).fetchone()
            if existing is not None:
                db.commit()
                return self._event(existing), False  # type: ignore[return-value]
            if source_event_id:
                existing = db.execute("SELECT * FROM super_events WHERE thread_id=? AND source=? AND source_event_id=?", (thread_id, source, source_event_id)).fetchone()
                if existing is not None:
                    db.commit()
                    return self._event(existing), False  # type: ignore[return-value]
            sequence = int(db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM super_events WHERE thread_id=?", (thread_id,)).fetchone()[0])
            now = _now()
            event_id = _id("event")
            stored_source_event_id = source_event_id or event_id
            db.execute(
                "INSERT INTO super_events(event_id,thread_id,node_id,source,event_type,payload,source_event_id,idempotency_key,attempt,sequence,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, thread_id, node_id, source, event_type, _json(payload or {}, {}), stored_source_event_id, key, attempt, sequence, now),
            )
            db.execute("UPDATE super_threads SET last_event_at=?, version=version+1, updated_at=? WHERE thread_id=?", (now, now, thread_id))
            db.commit()
            event = self.get_event(event_id, owner_id=owner_id)
            return event, True  # type: ignore[return-value]
        finally:
            db.close()

    def get_event(self, event_id: str, *, owner_id: str) -> TaskEvent | None:
        db = self._connect()
        try:
            row = db.execute(
                "SELECT e.* FROM super_events e JOIN super_threads t ON t.thread_id=e.thread_id "
                "WHERE e.event_id=? AND t.owner_id=?", (event_id, owner_id)
            ).fetchone()
            return self._event(row)
        finally:
            db.close()

    def list_events(self, thread_id: str, *, owner_id: str, after: int = 0) -> list[TaskEvent]:
        db = self._connect()
        try:
            rows = db.execute("SELECT e.* FROM super_events e JOIN super_threads t ON t.thread_id=e.thread_id WHERE e.thread_id=? AND t.owner_id=? AND e.sequence>? ORDER BY e.sequence", (thread_id, owner_id, after)).fetchall()
            return [self._event(row) for row in rows if self._event(row)]
        finally:
            db.close()

    def list_unhandled_node_events(self, thread_id: str, *, owner_id: str) -> list[TaskEvent]:
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT e.* FROM super_events e JOIN super_threads t ON t.thread_id=e.thread_id "
                "WHERE e.thread_id=? AND t.owner_id=? AND e.node_id != '' AND e.handled_at IS NULL "
                "ORDER BY e.sequence", (thread_id, owner_id)
            ).fetchall()
            return [self._event(row) for row in rows if self._event(row)]
        finally:
            db.close()

    def mark_event_handled(self, event_id: str, *, owner_id: str) -> None:
        db = self._connect()
        try:
            db.execute(
                "UPDATE super_events SET handled_at=? WHERE event_id=? AND handled_at IS NULL "
                "AND thread_id IN (SELECT thread_id FROM super_threads WHERE owner_id=?)",
                (_now(), event_id, owner_id),
            )
            db.commit()
        finally:
            db.close()

    def create_artifact(self, *, owner_id: str, thread_id: str, node_id: str, kind: str, path_or_ref: str, idempotency_key: str, title: str = "", summary: str = "", version: int = 1) -> Artifact:
        if not idempotency_key:
            raise ValueError("Artifact idempotency_key 不能为空")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM super_threads WHERE thread_id=? AND owner_id=?", (thread_id, owner_id)).fetchone() is None:
                raise LookupError("Thread 不存在或无权访问")
            node = db.execute("SELECT node_id, thread_id, owner_id FROM super_nodes WHERE node_id=?", (node_id,)).fetchone()
            if node is None or str(node["thread_id"]) != thread_id or str(node["owner_id"]) != owner_id:
                raise LookupError("Artifact 的 Node 不存在或不属于当前 Thread")
            row = db.execute("SELECT * FROM super_artifacts WHERE thread_id=? AND idempotency_key=?", (thread_id, idempotency_key)).fetchone()
            if row is None:
                artifact_id = _id("artifact")
                db.execute("INSERT INTO super_artifacts(artifact_id,thread_id,node_id,kind,path_or_ref,title,summary,version,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (artifact_id, thread_id, node_id, kind, path_or_ref, title, summary, int(version), idempotency_key, _now()))
                row = db.execute("SELECT * FROM super_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            db.commit()
            return self._artifact(row)  # type: ignore[return-value]
        finally:
            db.close()

    def list_artifacts(self, thread_id: str, *, owner_id: str) -> list[Artifact]:
        db = self._connect()
        try:
            rows = db.execute("SELECT a.* FROM super_artifacts a JOIN super_threads t ON t.thread_id=a.thread_id WHERE a.thread_id=? AND t.owner_id=? ORDER BY a.created_at", (thread_id, owner_id)).fetchall()
            return [self._artifact(row) for row in rows if self._artifact(row)]
        finally:
            db.close()

    def list_approvals(self, thread_id: str, *, owner_id: str) -> list[Approval]:
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT a.* FROM super_approvals a JOIN super_threads t "
                "ON t.thread_id=a.thread_id WHERE a.thread_id=? AND t.owner_id=? "
                "ORDER BY a.requested_at, a.approval_id",
                (thread_id, owner_id),
            ).fetchall()
            return [self._approval(row) for row in rows if self._approval(row)]
        finally:
            db.close()

    def create_approval_atomic(
        self, *, owner_id: str, thread_id: str, node_id: str, action: str,
        description: str, idempotency_key: str, risk_level: str = "medium",
    ) -> tuple[Approval, TaskEvent | None, bool]:
        """Create/reuse an approval and its waiting state in one transaction."""
        if not idempotency_key:
            raise ValueError("Approval idempotency_key 不能为空")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            thread = db.execute("SELECT * FROM super_threads WHERE thread_id=? AND owner_id=?", (thread_id, owner_id)).fetchone()
            if thread is None:
                raise LookupError("Thread 不存在或无权访问")
            node = db.execute("SELECT * FROM super_nodes WHERE node_id=? AND thread_id=? AND owner_id=?", (node_id, thread_id, owner_id)).fetchone()
            if node is None:
                raise LookupError("Approval 的 Node 不存在或不属于当前 Thread")
            row = db.execute("SELECT * FROM super_approvals WHERE thread_id=? AND idempotency_key=?", (thread_id, idempotency_key)).fetchone()
            if row is not None:
                approval = self._approval(row)
                if approval is None:
                    raise RuntimeError("Approval 数据损坏")
                db.commit()
                return approval, None, False
            if str(node["status"]) in {"succeeded", "failed", "cancelled", "interrupted"}:
                raise RuntimeError("终态 Node 不能重新发起 Approval")
            approval_id = _id("approval")
            now = _now()
            db.execute(
                "INSERT INTO super_approvals(approval_id,thread_id,node_id,action,description,risk_level,status,idempotency_key,requested_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (approval_id, thread_id, node_id, action, description, risk_level, "pending", idempotency_key, now),
            )
            db.execute("UPDATE super_nodes SET status='waiting_user', approval_required=1, version=version+1, updated_at=? WHERE node_id=? AND owner_id=?", (now, node_id, owner_id))
            db.execute("UPDATE super_threads SET status='waiting_user', current_node_id=?, version=version+1, updated_at=? WHERE thread_id=? AND owner_id=?", (node_id, now, thread_id, owner_id))
            event_id = _id("event")
            event_key = f"approval-request:{approval_id}"
            sequence = int(db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM super_events WHERE thread_id=?", (thread_id,)).fetchone()[0])
            payload = {"approval_id": approval_id, "action": action}
            db.execute(
                "INSERT INTO super_events(event_id,thread_id,node_id,source,event_type,payload,source_event_id,idempotency_key,attempt,sequence,created_at,handled_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, thread_id, node_id, "super-agent", "approval_requested", _json(payload, {}), event_key, event_key, int(node["attempt"] or 0), sequence, now, now),
            )
            db.execute("UPDATE super_threads SET last_event_at=? WHERE thread_id=?", (now, thread_id))
            db.commit()
            return self._approval(db.execute("SELECT * FROM super_approvals WHERE approval_id=?", (approval_id,)).fetchone()), self._event(db.execute("SELECT * FROM super_events WHERE event_id=?", (event_id,)).fetchone()), True  # type: ignore[return-value]
        finally:
            db.close()

    def create_approval(self, *, owner_id: str, thread_id: str, node_id: str, action: str, description: str, idempotency_key: str, risk_level: str = "medium") -> Approval:
        approval, _, _ = self.create_approval_atomic(owner_id=owner_id, thread_id=thread_id, node_id=node_id, action=action, description=description, idempotency_key=idempotency_key, risk_level=risk_level)
        return approval

    def get_approval(self, approval_id: str, *, owner_id: str) -> Approval | None:
        db = self._connect()
        try:
            row = db.execute("SELECT a.* FROM super_approvals a JOIN super_threads t ON t.thread_id=a.thread_id WHERE a.approval_id=? AND t.owner_id=?", (approval_id, owner_id)).fetchone()
            return self._approval(row)
        finally:
            db.close()

    def resolve_approval_atomic(self, approval_id: str, *, owner_id: str, status: str, resolved_by: str, request_id: str) -> Approval:
        if status not in {"approved", "rejected", "cancelled"}:
            raise ValueError("Approval 只能解析为 approved/rejected/cancelled")
        if not request_id:
            raise ValueError("Approval 解析必须提供 request_id")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT a.* FROM super_approvals a JOIN super_threads t ON t.thread_id=a.thread_id WHERE a.approval_id=? AND t.owner_id=?", (approval_id, owner_id)).fetchone()
            approval = self._approval(row)
            if approval is None:
                raise LookupError("Approval 不存在或无权访问")
            if approval.status != "pending":
                if approval.status == status:
                    db.commit()
                    return approval
                raise RuntimeError("Approval 已经解析，不能重复改变")
            node = db.execute("SELECT * FROM super_nodes WHERE node_id=? AND thread_id=? AND owner_id=?", (approval.node_id, approval.thread_id, owner_id)).fetchone()
            if node is None:
                raise LookupError("Approval 的 Node 不存在或不属于当前 Thread")
            now = _now()
            db.execute("UPDATE super_approvals SET status=?, resolved_at=?, resolved_by=? WHERE approval_id=?", (status, now, resolved_by, approval_id))
            node_status = "ready" if status == "approved" else "failed"
            error = "" if status == "approved" else f"用户{status}"
            db.execute("UPDATE super_nodes SET status=?, approval_required=0, error=?, version=version+1, updated_at=? WHERE node_id=? AND owner_id=?", (node_status, error, now, approval.node_id, owner_id))
            thread_status = "active" if status == "approved" else "failed"
            db.execute("UPDATE super_threads SET status=?, version=version+1, updated_at=? WHERE thread_id=? AND owner_id=?", (thread_status, now, approval.thread_id, owner_id))
            event_key = f"approval-resolved:{request_id}"
            sequence = int(db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM super_events WHERE thread_id=?", (approval.thread_id,)).fetchone()[0])
            payload = {"approval_id": approval_id, "status": status, "resolved_by": resolved_by, "request_id": request_id}
            db.execute("INSERT INTO super_events(event_id,thread_id,node_id,source,event_type,payload,source_event_id,idempotency_key,attempt,sequence,created_at,handled_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (_id("event"), approval.thread_id, approval.node_id, "super-agent", "approval_received", _json(payload, {}), event_key, event_key, int(node["attempt"] or 0), sequence, now, now))
            db.execute("UPDATE super_threads SET last_event_at=? WHERE thread_id=?", (now, approval.thread_id))
            db.commit()
            return self._approval(db.execute("SELECT * FROM super_approvals WHERE approval_id=?", (approval_id,)).fetchone())  # type: ignore[return-value]
        finally:
            db.close()

    def resolve_approval(self, approval_id: str, *, owner_id: str, status: str, resolved_by: str, request_id: str) -> Approval:
        return self.resolve_approval_atomic(approval_id, owner_id=owner_id, status=status, resolved_by=resolved_by, request_id=request_id)

    def recover_all(self) -> list[Thread]:
        db = self._connect()
        try:
            owners = [str(row[0]) for row in db.execute("SELECT DISTINCT owner_id FROM super_threads").fetchall()]
        finally:
            db.close()
        recovered: list[Thread] = []
        for owner_id in owners:
            recovered.extend(self.recover(owner_id=owner_id))
        return recovered

    def recover(self, *, owner_id: str) -> list[Thread]:
        """Fence unsafe running nodes while preserving paused and approval waits."""
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            now = _now()
            rows = db.execute("SELECT thread_id FROM super_threads WHERE owner_id=? AND status IN ('active','waiting_user','paused')", (owner_id,)).fetchall()
            for row in rows:
                thread_id = str(row["thread_id"])
                db.execute("UPDATE super_nodes SET status='interrupted', error='服务重启，Worker 未安全续跑', version=version+1, updated_at=? WHERE thread_id=? AND owner_id=? AND status IN ('running','stopping')", (now, thread_id, owner_id))
                db.execute("UPDATE super_threads SET status='needs_recovery', summary='服务重启，任务已保留，需用户继续或重试', version=version+1, updated_at=? WHERE thread_id=? AND owner_id=? AND status='active'", (now, thread_id, owner_id))
                sequence = int(db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM super_events WHERE thread_id=?", (thread_id,)).fetchone()[0])
                event_id = _id("event")
                event_key = f"recovery:{thread_id}"
                existing = db.execute("SELECT 1 FROM super_events WHERE thread_id=? AND idempotency_key=?", (thread_id, event_key)).fetchone()
                if existing is None:
                    db.execute("INSERT INTO super_events(event_id,thread_id,node_id,source,event_type,payload,source_event_id,idempotency_key,attempt,sequence,created_at,handled_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (event_id, thread_id, "", "super-agent", "app_restarted", _json({"message": "服务重启，任务需恢复"}, {}), event_key, event_key, 0, sequence, now, now))
            db.commit()
        finally:
            db.close()
        return self.list_threads(owner_id=owner_id, include_archived=True)
