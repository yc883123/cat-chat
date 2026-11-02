"""Versioned durable state for one supervisor per existing main conversation."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from naiba.super_agent.store import _now


class SupervisionStore:
    def __init__(self, storage: Any):
        self.storage = storage

    def get(self, thread_id: str, owner: str | None = None) -> dict[str, Any] | None:
        with self.storage._connect() as db:
            row = db.execute("SELECT * FROM super_supervisions WHERE thread_id=?" + (" AND owner_id=?" if owner is not None else ""),
                             (thread_id, owner) if owner is not None else (thread_id,)).fetchone()
            if row is None:
                return None
            return {**dict(row), "state": json.loads(row["state"])}

    def create(self, thread: Any, node: Any, cid: str, state: dict[str, Any]) -> None:
        with self.storage._connect() as db:
            try:
                db.execute("INSERT INTO super_supervisions(thread_id,node_id,conversation_id,owner_id,state) VALUES(?,?,?,?,?)",
                           (thread.thread_id, node.node_id, cid, thread.owner_id, json.dumps(state, ensure_ascii=False)))
            except sqlite3.IntegrityError as exc:
                raise ValueError("该会话已有未结束的自动续办任务（包括暂停和等待中的任务）") from exc

    def save(self, record: dict[str, Any], state: dict[str, Any], *, release: bool = False, round_state: dict[str, Any] | None = None) -> bool:
        """CAS fences late workers, human takeover and duplicate pollers."""
        with self.storage._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT state FROM super_supervisions WHERE thread_id=? AND version=?", (record["thread_id"], record["version"])).fetchone()
            if previous is None:
                return False
            receipts = json.loads(previous[0]).get("control_requests") or {}
            if receipts:
                state = {**state, "control_requests": {**receipts, **(state.get("control_requests") or {})}}
            changed = db.execute("UPDATE super_supervisions SET state=?,version=version+1,active=? WHERE thread_id=? AND version=?",
                                 (json.dumps(state, ensure_ascii=False), 0 if release else record["active"], record["thread_id"], record["version"])).rowcount
            if not changed:
                return False
            if round_state is not None:
                db.execute("INSERT INTO super_supervision_rounds(thread_id,round,state) VALUES(?,?,?) ON CONFLICT(thread_id,round) DO UPDATE SET state=excluded.state",
                           (record["thread_id"], round_state["round"], json.dumps(round_state, ensure_ascii=False)))
            return True

    def rounds(self, tid: str) -> list[dict[str, Any]]:
        with self.storage._connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT state FROM super_supervision_rounds WHERE thread_id=? ORDER BY round", (tid,))]

    def human_input(self, cid: str) -> None:
        with self.storage._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            boundary = db.execute("SELECT COALESCE(MAX(rowid),0) FROM messages WHERE conversation_id=?", (cid,)).fetchone()[0]
            for row in db.execute("SELECT * FROM super_supervisions WHERE conversation_id=? AND active=1", (cid,)).fetchall():
                state = json.loads(row["state"])
                state.update(resume_phase=state.get("resume_phase") if state.get("phase") == "paused" else state.get("phase"),
                             phase="paused", error="你已在关联会话发言；自动续办已暂停", human_epoch=state.get("human_epoch", 0) + 1,
                             human_boundary=state.get("human_boundary", boundary) if state.get("human_epoch", 0) != state.get("baseline_human_epoch", 0) else boundary,
                             generation=state.get("generation", 0) + 1)
                db.execute("UPDATE super_supervisions SET state=?,version=version+1 WHERE thread_id=?", (json.dumps(state, ensure_ascii=False), row["thread_id"]))
                db.execute("UPDATE super_threads SET status='paused',summary=?,version=version+1,updated_at=? WHERE thread_id=? AND status NOT IN ('completed','cancelled','archived')",
                           (state["error"], _now(), row["thread_id"]))

    def observe_review(self, tid: str, number: int, observation: dict[str, Any]) -> None:
        """Record paid attempts even when their result lost the control CAS.

        This updates only observability on the round and never a scheduling
        version, phase, cancellation generation or saved decision.
        """
        with self.storage._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM super_supervision_rounds WHERE thread_id=? AND round=?", (tid, number)).fetchone()
            if row is None:
                return
            state = json.loads(row[0])
            calls = [call for call in state.get("review_calls", []) if call.get("review_id") != observation["review_id"]]
            state["review_calls"] = calls + [observation]
            db.execute("UPDATE super_supervision_rounds SET state=? WHERE thread_id=? AND round=?",
                       (json.dumps(state, ensure_ascii=False), tid, number))

    def remember_noop(self, tid: str, request_id: str, action: str) -> None:
        """A repeated continue records its key without invalidating a review."""
        with self.storage._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM super_supervisions WHERE thread_id=?", (tid,)).fetchone()
            state = json.loads(row[0])
            requests = dict(state.get("control_requests") or {})
            requests[request_id] = action
            state["control_requests"] = requests
            db.execute("UPDATE super_supervisions SET state=? WHERE thread_id=?", (json.dumps(state, ensure_ascii=False), tid))
