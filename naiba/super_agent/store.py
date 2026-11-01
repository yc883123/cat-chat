from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from naiba.super_agent.models import (
    APPROVAL_STATUSES,
    EVENT_TYPES,
    NODE_STATUSES,
    NODE_TERMINAL_STATUSES,
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
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps(default, ensure_ascii=False)


def _load(value: Any, default: Any) -> Any:
    try:
        result = json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return default
    return result


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


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
                "router_agent_id,autonomy_level,summary,current_node_id,linked_conversation_ids,"
                "linked_run_ids,linked_job_ids,linked_domain_task_ids,execution_snapshot,"
                "schema_version,version,last_event_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    thread_id, owner_id, title, str(goal or ""), status,
                    str(fields.get("workspace_dir") or ""), str(fields.get("router_agent_id") or ""),
                    str(fields.get("autonomy_level") or "confirm"), str(fields.get("summary") or ""),
                    str(fields.get("current_node_id") or ""), _json(fields.get("linked_conversation_ids"), []),
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
            "summary", "current_node_id", "linked_conversation_ids", "linked_run_ids",
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
        assignments += ["version=version+1", "updated_at=?"]
        args += [now, thread_id, owner_id]
        where = "thread_id=? AND owner_id=?"
        if expected_version is not None:
            where += " AND version=?"
            args.append(expected_version)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
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
                "INSERT INTO super_nodes(node_id,thread_id,owner_id,parent_id,kind,status,dependencies,"
                "executor_type,executor_id,input_refs,output_refs,approval_required,retry_policy,checkpoint,"
                "attempt,idempotency_key,execution_snapshot,cancel_requested,error,schema_version,version,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    node_id, thread_id, owner_id, str(fields.get("parent_id") or ""), kind, status,
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
        current_row = db.execute("SELECT status FROM super_nodes WHERE node_id=? AND owner_id=?", (node_id, owner_id)).fetchone()
        if current_row is None:
            raise LookupError("Node 不存在或无权访问")
        if "status" in updates and updates["status"] != current_row["status"]:
            if updates["status"] not in NODE_TRANSITIONS.get(str(current_row["status"]), set()):
                raise RuntimeError(f"非法 Node 状态转换: {current_row['status']} -> {updates['status']}")
        assignments: list[str] = []
        args: list[Any] = []
        json_fields = {"output_refs", "checkpoint"}
        for key, value in updates.items():
            assignments.append(f"{key}=?")
            args.append(_json(value, []) if key in json_fields else 1 if key == "cancel_requested" and value else 0 if key == "cancel_requested" else value)
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
            status_by_event = {"run_started": "running", "progress": "running", "job_completed": "succeeded", "run_completed": "succeeded", "job_failed": "failed", "task_cancelled": "cancelled"}
            next_status = status_by_event.get(event_type)
            if node_id and next_status:
                row = db.execute("SELECT * FROM super_nodes WHERE node_id=? AND owner_id=? AND thread_id=?", (node_id, owner_id, thread_id)).fetchone()
                thread_row = db.execute("SELECT status FROM super_threads WHERE thread_id=? AND owner_id=?", (thread_id, owner_id)).fetchone()
                if row is not None and thread_row is not None and int(row["attempt"] or 0) == attempt and not bool(row["cancel_requested"]) and str(thread_row["status"]) not in {"cancelled", "paused"} and str(row["status"]) not in NODE_TERMINAL_STATUSES:
                    updates: dict[str, Any] = {"status": next_status}
                    if next_status == "failed":
                        updates["error"] = str((payload or {}).get("error") or "任务失败")
                    if next_status == "succeeded" and isinstance((payload or {}).get("output_refs"), list):
                        updates["output_refs"] = (payload or {}).get("output_refs")
                    self._update_node_db(db, node_id, owner_id, updates, expected_attempt=attempt)
                    applied = True
                    if next_status == "succeeded":
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
