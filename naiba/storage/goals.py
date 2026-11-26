"""Durable GOAL state. Run events are not an execution receipt."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
from typing import Any

from naiba.core.contracts import (GOAL_COMPLETION_POLICIES, GOAL_DEFAULT_LIMITS, GOAL_EXECUTION_POLICIES,
                                  GOAL_MAX_LIMITS, GOAL_STATUSES, GOAL_STOP_CODES)


GOAL_TERMINAL = frozenset({"completed", "cancelled"})
ACTIVE_GOAL_SQL = "status NOT IN ('completed','cancelled')"


class GoalConflict(ValueError):
    pass


def migrate_goals(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS chat_goals (
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        request_id TEXT NOT NULL, request_hash TEXT NOT NULL, message TEXT NOT NULL,
        input_message_id TEXT NOT NULL, run_id TEXT NOT NULL, status TEXT NOT NULL,
        generation INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1,
        model_requests INTEGER NOT NULL DEFAULT 0, tool_actions INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, deadline_at INTEGER NOT NULL,
        reason TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '[]',
        criteria TEXT NOT NULL DEFAULT '[]', checkpoint TEXT NOT NULL DEFAULT '{}',
        no_progress INTEGER NOT NULL DEFAULT 0, retry_count INTEGER NOT NULL DEFAULT 0,
        ready_at INTEGER NOT NULL DEFAULT 0, UNIQUE(conversation_id, request_id)
    )""")
    db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS chat_goals_one_active ON chat_goals(conversation_id) WHERE {ACTIVE_GOAL_SQL}")
    db.execute("CREATE INDEX IF NOT EXISTS chat_goals_queue ON chat_goals(status, ready_at, created_at)")
    db.execute("""CREATE TABLE IF NOT EXISTS chat_goal_rounds (
        goal_id TEXT NOT NULL REFERENCES chat_goals(id) ON DELETE CASCADE,
        generation INTEGER NOT NULL, run_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
        checkpoint TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(goal_id, generation)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS chat_goal_actions (
        goal_id TEXT NOT NULL REFERENCES chat_goals(id) ON DELETE CASCADE,
        action_id TEXT NOT NULL, run_id TEXT NOT NULL, state TEXT NOT NULL,
        receipt TEXT NOT NULL, PRIMARY KEY(goal_id, action_id)
    )""")


def migrate_goal_extensions(db: sqlite3.Connection) -> None:
    """v34; safe to repeat and never commit the caller's migration transaction."""
    db.execute("SAVEPOINT goal_extensions")
    try:
        columns = {row[1] for row in db.execute("PRAGMA table_info(chat_goals)")}
        if not columns:
            raise sqlite3.OperationalError("v34 requires the v33 GOAL schema")
        additions = {
            "limits": "TEXT NOT NULL DEFAULT '" + goal_json(GOAL_DEFAULT_LIMITS) + "'",
            "stop_code": "TEXT NOT NULL DEFAULT ''",
            "sources": "TEXT NOT NULL DEFAULT '[]'",
            "stages": "TEXT NOT NULL DEFAULT '[]'",
            "manual_items": "TEXT NOT NULL DEFAULT '[]'",
            "completion_policy": "TEXT NOT NULL DEFAULT 'manual'",
            "wait_binding": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, definition in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE chat_goals ADD COLUMN {name} {definition}")
        if "completion_policy" not in columns:
            # Old criteria are exclusively machine checks. Do not infer manual
            # obligations or change any of the already consumed budgets.
            db.execute("""UPDATE chat_goals SET completion_policy='machine_all'
                WHERE json_valid(criteria) AND json_type(criteria)='array' AND json_array_length(criteria)>0""")
        round_columns = {row[1] for row in db.execute("PRAGMA table_info(chat_goal_rounds)")}
        round_additions = {
            "created_at": "INTEGER", "finished_at": "INTEGER",
            "reason": "TEXT NOT NULL DEFAULT ''",
            "model_requests_start": "INTEGER", "model_requests_end": "INTEGER",
            "tool_actions_start": "INTEGER", "tool_actions_end": "INTEGER",
            "evidence": "TEXT NOT NULL DEFAULT '[]'",
        }
        for name, definition in round_additions.items():
            if name not in round_columns:
                db.execute(f"ALTER TABLE chat_goal_rounds ADD COLUMN {name} {definition}")
        if "created_at" not in round_columns:
            task_columns = {row[1] for row in db.execute("PRAGMA table_info(background_tasks)")}
            if {"id", "created_at", "finished_at"} <= task_columns:
                db.execute("""UPDATE chat_goal_rounds SET
                    created_at=(SELECT created_at FROM background_tasks WHERE id=run_id),
                    finished_at=(SELECT finished_at FROM background_tasks WHERE id=run_id)""")
            db.execute("""UPDATE chat_goal_rounds SET created_at=COALESCE(created_at,
                CASE WHEN generation=1 THEN (SELECT created_at FROM chat_goals WHERE id=goal_id) END)""")
            # v33 had no per-round usage samples. Unknown historical boundaries
            # remain NULL; only the latest settled round has a known end sample.
            db.execute("""UPDATE chat_goal_rounds SET
                reason=(SELECT reason FROM chat_goals WHERE id=goal_id),
                model_requests_end=(SELECT model_requests FROM chat_goals WHERE id=goal_id),
                tool_actions_end=(SELECT tool_actions FROM chat_goals WHERE id=goal_id)
                WHERE EXISTS (SELECT 1 FROM chat_goals g WHERE g.id=goal_id
                    AND g.run_id=chat_goal_rounds.run_id AND g.status NOT IN ('queued','running','pausing','cancelling'))""")
        if "evidence" not in round_columns:
            for goal_id, run_id, evidence in db.execute("SELECT id,run_id,evidence FROM chat_goals").fetchall():
                db.execute("UPDATE chat_goal_rounds SET evidence=? WHERE goal_id=? AND run_id=?",
                           (goal_json(_evidence_summary(json.loads(evidence))), goal_id, run_id))
        db.execute("""CREATE TABLE IF NOT EXISTS chat_goal_budget_receipts (
            goal_id TEXT NOT NULL REFERENCES chat_goals(id) ON DELETE CASCADE,
            request_id TEXT NOT NULL, request_hash TEXT NOT NULL,
            receipt TEXT NOT NULL, created_at INTEGER NOT NULL,
            PRIMARY KEY(goal_id,request_id), UNIQUE(goal_id,request_id,request_hash)
        )""")
        db.execute("""CREATE INDEX IF NOT EXISTS chat_goals_wait_binding ON chat_goals(
            status,json_extract(wait_binding,'$.kind'),json_extract(wait_binding,'$.target_id'))""")
        db.execute("RELEASE goal_extensions")
    except BaseException:
        db.execute("ROLLBACK TO goal_extensions")
        db.execute("RELEASE goal_extensions")
        raise


def migrate_goal_continuous(db: sqlite3.Connection) -> None:
    """v36 GOAL 增量：显式执行策略（旧目标保留 bounded，新目标默认 continuous）。"""
    columns = {row[1] for row in db.execute("PRAGMA table_info(chat_goals)")}
    if columns and "execution_policy" not in columns:
        db.execute("ALTER TABLE chat_goals ADD COLUMN execution_policy TEXT NOT NULL DEFAULT 'bounded'")


def _has_goal_column(db: sqlite3.Connection, column: str) -> bool:
    return any(row[1] == column for row in db.execute("PRAGMA table_info(chat_goals)"))


def goal_now() -> int:
    return int(time.time() * 1000)


def goal_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _limits(raw: Any = None) -> dict[str, int]:
    if raw is None:
        return dict(GOAL_DEFAULT_LIMITS)
    if not isinstance(raw, dict) or set(raw) - GOAL_DEFAULT_LIMITS.keys():
        raise ValueError("无效的目标限额")
    result = dict(GOAL_DEFAULT_LIMITS)
    for key, value in raw.items():
        if type(value) is not int or not 1 <= value <= GOAL_MAX_LIMITS[key]:
            raise ValueError("目标限额必须是范围内的正整数")
        result[key] = value
    return result


def _evidence_summary(items: Any) -> list[dict[str, Any]]:
    """Only bounded scalar evidence references, never model/tool result bodies."""
    keys = {"id", "kind", "path", "passed", "status", "sha256", "check_id", "stage_id", "label", "reason"}
    return [{key: value[:1024] if isinstance(value, str) else value
             for key, value in item.items() if key in keys and
             (value is None or type(value) in (str, int, bool))}
            for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _wait_binding(binding: Any, goal_id: str, run_id: str) -> dict[str, Any]:
    if not isinstance(binding, dict):
        raise ValueError("等待绑定必须是对象，清除绑定请传 {}")
    if not binding:
        return {}
    if any(not isinstance(binding.get(key), str) or not binding[key] for key in ("kind", "target_id")):
        raise ValueError("等待绑定缺少类型或目标编号")
    if binding.get("goal_id", goal_id) != goal_id or binding.get("run_id", run_id) != run_id:
        raise GoalConflict("等待绑定不属于当前目标轮次")
    return {**binding, "goal_id": goal_id, "run_id": run_id}


def require_no_goal(db: sqlite3.Connection, conversation_id: str) -> None:
    if db.execute(f"SELECT 1 FROM chat_goals WHERE conversation_id=? AND {ACTIVE_GOAL_SQL}",
                  (conversation_id,)).fetchone():
        raise GoalConflict("这条对话有未结束的 GOAL，请先处理目标卡")


def insert_goal(db: sqlite3.Connection, conversation_id: str, message_id: str, run_id: str,
                spec: dict[str, Any], now: int) -> None:
    criteria = spec.get("criteria", [])
    limits = _limits(spec.get("limits"))
    manual_items = spec.get("manual_items", [])
    policy = spec.get("completion_policy", "mixed" if criteria and manual_items else "machine_all" if criteria else "manual")
    if policy not in GOAL_COMPLETION_POLICIES:
        raise ValueError("无效的目标完成策略")
    stop_code = spec.get("stop_code", "")
    if stop_code not in GOAL_STOP_CODES:
        raise ValueError("无效的目标停止原因")
    execution_policy = str(spec.get("execution_policy") or "bounded")
    if execution_policy not in GOAL_EXECUTION_POLICIES:
        raise ValueError("无效的目标执行策略")
    for key in ("criteria", "sources", "stages", "manual_items"):
        if not isinstance(spec.get(key, []), list):
            raise ValueError("目标清单必须是列表")
    if "deadline_at" in spec:
        deadline = spec["deadline_at"]
    elif execution_policy == "continuous":
        deadline = 0
    else:
        deadline = now + spec.get("duration_ms", 3 * 60 * 60 * 1000)
    columns = {row[1] for row in db.execute("PRAGMA table_info(chat_goals)")}
    # Explicit v33 fixtures and old callers remain supported; real installs use
    # the independent v34 migration before constructing new GOALs.
    if "limits" not in columns:
        db.execute("""INSERT INTO chat_goals(id, conversation_id, request_id, request_hash, message,
            input_message_id, run_id, status, created_at, updated_at, deadline_at, criteria)
            VALUES (?,?,?,?,?,?,?,'queued',?,?,?,?)""",
            (spec["id"], conversation_id, spec["request_id"], spec["request_hash"], spec["message"],
             message_id, run_id, now, now, deadline, goal_json(criteria)))
        db.execute("INSERT INTO chat_goal_rounds(goal_id,generation,run_id,status) VALUES (?,1,?,'queued')",
                   (spec["id"], run_id))
        return
    binding = _wait_binding(spec.get("wait_binding", {}), spec["id"], run_id)
    if "execution_policy" in columns:
        db.execute("""INSERT INTO chat_goals(id, conversation_id, request_id, request_hash, message,
            input_message_id, run_id, status, created_at, updated_at, deadline_at, criteria,
            limits,sources,stages,manual_items,completion_policy,wait_binding,stop_code,execution_policy)
            VALUES (?,?,?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,?,?,?,?)""",
            (spec["id"], conversation_id, spec["request_id"], spec["request_hash"], spec["message"],
             message_id, run_id, now, now, deadline, goal_json(criteria), goal_json(limits),
             goal_json(spec.get("sources", [])), goal_json(spec.get("stages", [])),
             goal_json(manual_items), policy, goal_json(binding), stop_code, execution_policy))
    else:
        db.execute("""INSERT INTO chat_goals(id, conversation_id, request_id, request_hash, message,
            input_message_id, run_id, status, created_at, updated_at, deadline_at, criteria,
            limits,sources,stages,manual_items,completion_policy,wait_binding,stop_code)
            VALUES (?,?,?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,?,?,?)""",
            (spec["id"], conversation_id, spec["request_id"], spec["request_hash"], spec["message"],
             message_id, run_id, now, now, deadline, goal_json(criteria), goal_json(limits),
             goal_json(spec.get("sources", [])), goal_json(spec.get("stages", [])),
             goal_json(manual_items), policy, goal_json(binding), stop_code))
    db.execute("""INSERT INTO chat_goal_rounds(goal_id,generation,run_id,status,created_at,
        model_requests_start,tool_actions_start) VALUES (?,1,?,'queued',?,0,0)""",
        (spec["id"], run_id, now))


class GoalStore:
    def __init__(self, storage: Any):
        self.storage = storage

    def get(self, goal_id: str) -> dict[str, Any] | None:
        with self.storage._connect() as db:
            return self._decode(db.execute("SELECT * FROM chat_goals WHERE id=?", (goal_id,)).fetchone())

    def latest(self, conversation_id: str) -> dict[str, Any] | None:
        with self.storage._connect() as db:
            return self._decode(db.execute("SELECT * FROM chat_goals WHERE conversation_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1", (conversation_id,)).fetchone())

    def request(self, conversation_id: str, request_id: str) -> dict[str, Any] | None:
        with self.storage._connect() as db:
            return self._decode(db.execute("SELECT * FROM chat_goals WHERE conversation_id=? AND request_id=?",
                                           (conversation_id, request_id)).fetchone())

    def pending(self) -> list[tuple[str, str]]:
        with self.storage._connect() as db:
            return [(row[0], row[1]) for row in db.execute(
                "SELECT id,status FROM chat_goals WHERE status IN ('queued','running','waiting_tool','pausing','cancelling') "
                "ORDER BY CASE status WHEN 'queued' THEN 1 ELSE 0 END, created_at")]

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for key in ("checkpoint", "criteria", "evidence", "limits", "sources", "stages", "manual_items", "wait_binding"):
            if key in result:
                result[key] = json.loads(result[key])
        # Read-only v33 compatibility, without implicitly running a migration.
        result.setdefault("limits", dict(GOAL_DEFAULT_LIMITS))
        result.setdefault("completion_policy", "machine_all" if result["criteria"] else "manual")
        for key in ("sources", "stages", "manual_items"):
            result.setdefault(key, [])
        result.setdefault("stop_code", "")
        result.setdefault("wait_binding", {})
        result.setdefault("execution_policy", "bounded")
        return result

    def round_run_ids(self, goal_id: str) -> list[str]:
        with self.storage._connect() as db:
            return [str(row[0]) for row in db.execute(
                "SELECT run_id FROM chat_goal_rounds WHERE goal_id=? ORDER BY generation ASC",
                (goal_id,),
            ).fetchall() if row[0]]

    def waiting(self, kind: str = "", target_id: str = "") -> list[dict[str, Any]]:
        with self.storage._connect() as db:
            return [self._decode(row) for row in db.execute(
                "SELECT * FROM chat_goals WHERE status='waiting_tool' "
                "AND (?='' OR json_extract(wait_binding,'$.kind')=?) "
                "AND (?='' OR json_extract(wait_binding,'$.target_id')=?) ORDER BY created_at,id",
                (kind, kind, target_id, target_id))]

    def find_wait_bindings(self, kind: str, target_id: str) -> list[dict[str, Any]]:
        return self.waiting(kind, target_id)

    def rounds(self, goal_id: str, page_size: int = 20, cursor: str = "") -> dict[str, Any]:
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("轮次每页数量必须是 1–100 的整数")
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise ValueError("无效的轮次游标")
        before = None
        if cursor:
            try:
                token = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
                if (not isinstance(token, dict) or token.get("scope") != goal_id
                        or type(token.get("v")) is not int or token["v"] != 1
                        or type(token.get("before")) is not int or not 1 <= token["before"] <= 9223372036854775807):
                    raise ValueError
                before = token["before"]
            except (ValueError, TypeError, UnicodeError) as exc:
                raise ValueError("轮次游标与目标不匹配或已损坏") from exc
        with self.storage._connect() as db:
            if not db.execute("SELECT 1 FROM chat_goals WHERE id=?", (goal_id,)).fetchone():
                raise LookupError("目标不存在")
            # Explicit projection: checkpoints/model messages are never loaded.
            records = db.execute("""SELECT goal_id,generation,run_id,status,created_at,finished_at,reason,
                model_requests_start,model_requests_end,tool_actions_start,tool_actions_end,evidence
                FROM chat_goal_rounds WHERE goal_id=?""" + (" AND generation<?" if before is not None else "") +
                " ORDER BY generation DESC LIMIT ?",
                (goal_id, before, page_size + 1) if before is not None else (goal_id, page_size + 1)).fetchall()
        more = len(records) > page_size
        items = []
        for record in records[:page_size]:
            item = dict(record)
            item["evidence"] = _evidence_summary(json.loads(item["evidence"]))
            items.append(item)
        next_cursor = base64.urlsafe_b64encode(goal_json({"v": 1, "scope": goal_id,
            "before": items[-1]["generation"]}).encode()).decode().rstrip("=") if more else ""
        return {"rounds": items, "count": len(items), "next_cursor": next_cursor, "has_more": more}

    def bind_next(self, db: sqlite3.Connection, goal_id: str, run_id: str, version: int) -> None:
        now = goal_now()
        policy_clause = (
            "(execution_policy='continuous' OR generation<json_extract(limits,'$.rounds'))"
            if _has_goal_column(db, "execution_policy")
            else "generation<json_extract(limits,'$.rounds')"
        )
        updated = db.execute("UPDATE chat_goals SET run_id=?, generation=generation+1, version=version+1, updated_at=?, "
                             f"wait_binding='{{}}' WHERE id=? AND version=? AND status='queued' "
                             f"AND (deadline_at<=0 OR deadline_at>?) AND {policy_clause}",
                             (run_id, now, goal_id, version, now))
        if updated.rowcount != 1:
            raise GoalConflict("目标状态已变化或时间、轮次额度已用完")
        db.execute("""INSERT INTO chat_goal_rounds(goal_id,generation,run_id,status,created_at,
            model_requests_start,tool_actions_start) SELECT id,generation,run_id,'queued',?,model_requests,tool_actions
            FROM chat_goals WHERE id=?""", (now, goal_id))

    def claim(self, goal_id: str, run_id: str) -> bool:
        with self.storage._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = goal_now()
            policy_clause = (
                "(execution_policy='continuous' OR generation<=json_extract(limits,'$.rounds'))"
                if _has_goal_column(db, "execution_policy")
                else "generation<=json_extract(limits,'$.rounds')"
            )
            changed = db.execute("UPDATE chat_goals SET status='running', version=version+1, updated_at=? "
                f"WHERE id=? AND run_id=? AND status='queued' AND (deadline_at<=0 OR deadline_at>?) "
                f"AND {policy_clause}",
                (now, goal_id, run_id, now)).rowcount
            if changed:
                db.execute("UPDATE chat_goal_rounds SET status='running' WHERE goal_id=? AND run_id=?",
                           (goal_id, run_id))
            return bool(changed)

    def checkpoint(self, goal_id: str, run_id: str, checkpoint: dict[str, Any]) -> None:
        encoded = goal_json(checkpoint)
        if len(encoded.encode("utf-8")) > 8 * 1024 * 1024:
            raise GoalConflict("目标模型视图过大，无法安全保存检查点")
        with self.storage._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE chat_goals SET checkpoint=?, updated_at=? "
                                 "WHERE id=? AND run_id=? AND status='running'",
                                 (encoded, goal_now(), goal_id, run_id)).rowcount
            if changed != 1:
                raise GoalConflict("目标检查点已失效")
            db.execute("UPDATE chat_goal_actions SET state='checkpointed' WHERE goal_id=? AND run_id=? AND state='confirmed'",
                       (goal_id, run_id))

    def reserve(self, goal_id: str, run_id: str, counter: str, count: int, maximum: int | None = None) -> bool:
        if (counter not in {"model_requests", "tool_actions"} or type(count) is not int
                or not 1 <= count <= GOAL_MAX_LIMITS[counter]
                or maximum is not None and (type(maximum) is not int or maximum < 0)):
            raise ValueError("无效的目标额度")
        now = goal_now()
        with self.storage._connect() as db:
            limit_clause = (
                f"(execution_policy='continuous' OR {counter}+?<=json_extract(limits,'$.{counter}'))"
                if _has_goal_column(db, "execution_policy")
                else f"{counter}+?<=json_extract(limits,'$.{counter}')"
            )
            return db.execute(f"UPDATE chat_goals SET {counter}={counter}+?, updated_at=? "
                f"WHERE id=? AND run_id=? AND status='running' AND (deadline_at<=0 OR deadline_at>?) "
                f"AND {limit_clause} "
                f"AND (? IS NULL OR {counter}+?<=?)",
                (count, now, goal_id, run_id, now, count, maximum, count, maximum)).rowcount == 1

    def extend(self, goal_id: str, version: int, request_id: str, increments: dict[str, int],
               extra_minutes: int = 0) -> dict[str, Any]:
        if type(version) is not int or version < 1:
            raise ValueError("需要有效的目标版本")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            raise ValueError("需要有效的追加请求编号")
        if not isinstance(increments, dict) or set(increments) - GOAL_DEFAULT_LIMITS.keys():
            raise ValueError("无效的追加额度")
        if any(type(value) is not int or not 1 <= value <= GOAL_DEFAULT_LIMITS[key]
               for key, value in increments.items()):
            raise ValueError("单次追加额度必须是范围内的正整数")
        if type(extra_minutes) is not int or not 0 <= extra_minutes <= 180:
            raise ValueError("单次追加时限必须是 0–180 分钟的整数")
        if not increments and not extra_minutes:
            raise ValueError("至少追加一项额度或时限")
        # Version is a CAS guard, not part of the idempotent operation payload.
        digest = hashlib.sha256(json.dumps({"increments": increments, "extra_minutes": extra_minutes},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.storage._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._decode(db.execute("SELECT * FROM chat_goals WHERE id=?", (goal_id,)).fetchone())
            if current is None:
                raise LookupError("目标不存在")
            receipt = db.execute("SELECT request_hash FROM chat_goal_budget_receipts WHERE goal_id=? AND request_id=?",
                                 (goal_id, request_id)).fetchone()
            if receipt:
                if receipt[0] != digest:
                    raise GoalConflict("同一追加请求编号不能修改内容")
                return current
            if current["version"] != version:
                raise GoalConflict("目标已更新，请重试")
            if current["status"] in GOAL_TERMINAL or current["status"] == "cancelling":
                raise GoalConflict("已结束或正在取消的目标不能追加额度")
            limits = dict(current["limits"])
            for key, value in increments.items():
                if limits[key] + value > GOAL_MAX_LIMITS[key]:
                    raise ValueError("追加后超过目标总额度上限")
                limits[key] += value
            now = goal_now()
            deadline = current["deadline_at"]
            if extra_minutes:
                deadline = max(deadline, now) + extra_minutes * 60 * 1000
                if deadline > current["created_at"] + 24 * 60 * 60 * 1000:
                    raise ValueError("目标总时限不能超过创建后 24 小时")
            db.execute("UPDATE chat_goals SET limits=?,deadline_at=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                       (goal_json(limits), deadline, now, goal_id, version))
            db.execute("INSERT INTO chat_goal_budget_receipts(goal_id,request_id,request_hash,receipt,created_at) "
                "VALUES (?,?,?,?,?)", (goal_id, request_id, digest,
                    goal_json({"limits": limits, "deadline_at": deadline, "version": version + 1}), now))
            return self._decode(db.execute("SELECT * FROM chat_goals WHERE id=?", (goal_id,)).fetchone())

    @staticmethod
    def _settle_round(db: sqlite3.Connection, goal_id: str, run_id: str, now: int) -> None:
        db.execute("""UPDATE chat_goal_rounds SET
            status=(SELECT status FROM chat_goals WHERE id=?),
            reason=(SELECT reason FROM chat_goals WHERE id=?), finished_at=COALESCE(finished_at,?),
            model_requests_end=(SELECT model_requests FROM chat_goals WHERE id=?),
            tool_actions_end=(SELECT tool_actions FROM chat_goals WHERE id=?)
            WHERE goal_id=? AND run_id=?""",
            (goal_id, goal_id, now, goal_id, goal_id, goal_id, run_id))
        evidence = db.execute("SELECT evidence FROM chat_goals WHERE id=?", (goal_id,)).fetchone()
        db.execute("UPDATE chat_goal_rounds SET evidence=? WHERE goal_id=? AND run_id=?",
                   (goal_json(_evidence_summary(json.loads(evidence[0]))), goal_id, run_id))

    def bind_check(self, db: sqlite3.Connection, row: dict[str, Any], job_id: str,
                   binding: dict[str, Any]) -> bool:
        """Use inside create_run(job_bind=...). Caller rolls back on False."""
        bound = _wait_binding({**binding, "target_id": job_id}, row["id"], row["run_id"])
        if binding.get("target_id", job_id) != job_id:
            raise GoalConflict("验收任务编号与等待绑定不一致")
        now = goal_now()
        changed = db.execute("""UPDATE chat_goals SET status='waiting_tool',wait_binding=?,
            version=version+1,updated_at=? WHERE id=? AND version=? AND run_id=? AND status='running'""",
            (goal_json(bound), now, row["id"], row["version"], row["run_id"])).rowcount
        if changed:
            self._settle_round(db, row["id"], row["run_id"], now)
        return bool(changed)

    def transition(self, row: dict[str, Any], status: str, reason: str = "", *,
                   checkpoint: dict[str, Any] | None = None, evidence: list[dict[str, Any]] | None = None,
                   settle: bool = False, no_progress: int | None = None, retry_count: int | None = None,
                   ready_at: int = 0, deadline_at: int | None = None, stop_code: str | None = None,
                   wait_binding: dict[str, Any] | None = None, stages: list[dict[str, Any]] | None = None) -> bool:
        if status not in GOAL_STATUSES or stop_code is not None and stop_code not in GOAL_STOP_CODES:
            raise ValueError("无效的目标状态或停止原因")
        if stages is not None and not isinstance(stages, list):
            raise ValueError("目标阶段必须是列表")
        binding = _wait_binding(wait_binding, row["id"], row["run_id"]) if wait_binding is not None else None
        with self.storage._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = goal_now()
            changed = db.execute("""UPDATE chat_goals SET status=?, reason=?, version=version+1, updated_at=?,
                checkpoint=COALESCE(?,checkpoint), evidence=COALESCE(?,evidence),
                no_progress=COALESCE(?,no_progress), retry_count=COALESCE(?,retry_count), ready_at=?,
                deadline_at=COALESCE(?,deadline_at),stop_code=COALESCE(?,stop_code),
                wait_binding=COALESCE(?,wait_binding),stages=COALESCE(?,stages)
                WHERE id=? AND version=? AND run_id=? AND status NOT IN ('completed','cancelled')""",
                (status, reason, now, goal_json(checkpoint) if checkpoint is not None else None,
                 goal_json(evidence) if evidence is not None else None, no_progress, retry_count,
                 ready_at, deadline_at, stop_code, goal_json(binding) if binding is not None else None,
                 goal_json(stages) if stages is not None else None, row["id"], row["version"], row["run_id"])).rowcount
            if changed and settle:
                self._settle_round(db, row["id"], row["run_id"], now)
                db.execute("UPDATE chat_goal_rounds SET checkpoint=? WHERE goal_id=? AND run_id=?",
                    (goal_json(checkpoint if checkpoint is not None else row["checkpoint"]), row["id"], row["run_id"]))
            return bool(changed)

    def action(self, goal_id: str, action_id: str) -> dict[str, Any] | None:
        with self.storage._connect() as db:
            row = db.execute("SELECT state,receipt FROM chat_goal_actions WHERE goal_id=? AND action_id=?",
                             (goal_id, action_id)).fetchone()
            return {"state": row["state"], **json.loads(row["receipt"])} if row else None

    def start_action(self, goal_id: str, run_id: str, action_id: str, receipt: dict[str, Any]) -> None:
        with self.storage._connect() as db:
            changed = db.execute("INSERT INTO chat_goal_actions(goal_id,action_id,run_id,state,receipt) "
                "SELECT id,?,run_id,'pending',? FROM chat_goals WHERE id=? AND run_id=? "
                "AND status='running' AND (deadline_at<=0 OR deadline_at>?)",
                (action_id, goal_json(receipt), goal_id, run_id, goal_now())).rowcount
            if not changed:
                raise GoalConflict("目标已停止，未发送工具动作")

    def finish_action(self, goal_id: str, action_id: str) -> None:
        with self.storage._connect() as db:
            db.execute("UPDATE chat_goal_actions SET state='confirmed' WHERE goal_id=? AND action_id=? AND state='pending'",
                       (goal_id, action_id))

    def discard_action(self, goal_id: str, action_id: str) -> None:
        """移除未实际执行的待决动作回执（例如工具在真正派发前因 NEED_CONFIRM 被拦截）。"""
        with self.storage._connect() as db:
            db.execute("DELETE FROM chat_goal_actions WHERE goal_id=? AND action_id=? AND state='pending'",
                       (goal_id, action_id))

    def pending_actions(self, goal_id: str) -> list[tuple[str, dict[str, Any]]]:
        with self.storage._connect() as db:
            return [(row[0], json.loads(row[1])) for row in db.execute(
                "SELECT action_id,receipt FROM chat_goal_actions WHERE goal_id=? AND state IN ('pending','confirmed')", (goal_id,))]
