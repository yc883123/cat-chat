"""Single-node supervised Chat, bounded reviews, persistent dispatch intentions."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from contextlib import nullcontext
from typing import Any

from naiba.core.contracts import (SUPER_AGENT_EXECUTION_KEY, SUPERVISED_DEFAULT_CONTINUATIONS,
    SUPERVISED_DEFAULT_WINDOW_MINUTES, SUPERVISED_EXECUTOR, SUPERVISED_MAX_CONTINUATIONS,
    SUPERVISED_REVIEW_TIMEOUT_SECONDS)
from naiba.super_agent.adapters import AdapterCapabilities, AdapterError, BaseAdapter
from naiba.core.exceptions import ActiveRunError
from naiba.super_agent.baseline import PreflightRejected
from naiba.super_agent.models import THREAD_TRANSITIONS
from naiba.super_agent.review import REVIEW_INPUT_LIMIT, REVIEW_SYSTEM, parse_review
from naiba.super_agent.store import _now
from naiba.super_agent.supervision_store import SupervisionStore


def validate_settings(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - {"completion_criteria", "max_continuations", "window_minutes"}:
        raise ValueError("自动续办设置不合法")
    criteria = raw.get("completion_criteria")
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 20 or any(not isinstance(c, str) or not c.strip() or len(c) > 1000 for c in criteria):
        raise ValueError("请填写1至20条完成标准，每条不超过1000字")
    maximum = raw.get("max_continuations", SUPERVISED_DEFAULT_CONTINUATIONS)
    minutes = raw.get("window_minutes", SUPERVISED_DEFAULT_WINDOW_MINUTES)
    if type(maximum) is not int or not 1 <= maximum <= SUPERVISED_MAX_CONTINUATIONS:
        raise ValueError("自动续办次数必须为1至5")
    if type(minutes) is not int or not 1 <= minutes <= 120:
        raise ValueError("运行窗口必须为1至120分钟")
    return {"criteria": [c.strip() for c in criteria], "max_continuations": maximum, "window_minutes": minutes}


class SupervisedChatAdapter(BaseAdapter):
    executor_type = SUPERVISED_EXECUTOR
    capabilities = AdapterCapabilities(can_retry=False, can_resume=False)

    def __init__(self, coordinator: Any):
        super().__init__(coordinator.service)
        self.coordinator = coordinator
        self.app = coordinator.app
        self.store = SupervisionStore(self.app.storage)
        self.capacity = threading.BoundedSemaphore(2)
        self.workers: dict[str, tuple[threading.Event, int]] = {}
        self.worker_threads: set[threading.Thread] = set()
        self.workers_lock = threading.Lock()
        self._fence_restart()

    def _fence_restart(self) -> None:
        with self.app.storage._connect() as db:
            rows = db.execute("SELECT * FROM super_supervisions WHERE active=1").fetchall()
            for row in rows:
                state = json.loads(row["state"])
                if state["round"] or state["phase"] != "ready":
                    state.update(resume_phase=state.get("resume_phase", state["phase"]) if state["phase"] in {"paused", "needs_recovery"} else state["phase"], phase="needs_recovery", generation=state["generation"] + 1,
                                 error="服务已重启，请确认继续；未自动重提执行或审查")
                    db.execute("UPDATE super_supervisions SET state=?,version=version+1 WHERE thread_id=?", (json.dumps(state, ensure_ascii=False), row["thread_id"]))
                    db.execute("UPDATE super_threads SET status='needs_recovery',summary=? WHERE thread_id=? AND status NOT IN ('cancelled','completed','archived')", (state["error"], row["thread_id"]))

    def create(self, owner: str, body: dict[str, Any]) -> tuple[Any, Any]:
        if body.get("domain") != "chat" or body.get("nodes") or body.get("chain"):
            raise ValueError("自动续办仅支持一个已有主会话")
        settings = validate_settings(body.get("supervision"))
        goal = str(body.get("goal") or "").strip()
        if not goal or len(goal) > 6000:
            raise ValueError("请填写有效任务目标")
        cid = str(body.get("conversation_id") or "")
        prepared = self.coordinator.prepare(body)
        checkpoint = prepared["checkpoint"]
        checkpoint["start_request"]["message"] = goal + "\n\n完成标准：\n" + "\n".join(settings["criteria"])
        thread = self.service.create_thread(owner, str(body.get("title") or "自动续办任务"), goal,
            execution_snapshot=prepared["execution_snapshot"], linked_conversation_ids=[prepared["conversation_id"]])
        node = self.service.create_node(owner, thread.thread_id, "chat", idempotency_key="supervised-chat",
            executor_type=self.executor_type, status="ready", checkpoint=checkpoint,
            execution_snapshot=prepared["execution_snapshot"])
        state = {**settings, "phase": "ready", "round": 0, "review_count": 0, "generation": 0,
                 "goal_version": 1, "human_epoch": 0, "baseline_human_epoch": 0,
                 "deadline_at": 0, "error": "", "token_usage": None}
        try:
            self.store.create(thread, node, prepared["conversation_id"], state)
        except Exception:
            # Failed creation leaves no runnable orphan; never release another
            # supervisor's conversation occupancy.
            self.service.store.update_thread(thread.thread_id, owner_id=owner, status="cancelled")
            raise
        self.coordinator.wake_event.set()
        return thread, node

    def human_input(self, cid: str) -> None:
        self.store.human_input(cid)
        with self.workers_lock:
            for tid, (event, _) in self.workers.items():
                record = self.store.get(tid)
                if record and record["conversation_id"] == cid:
                    event.set()

    def detail(self, owner: str, tid: str) -> dict[str, Any] | None:
        record = self.store.get(tid, owner)
        if record is None:
            return None
        state = dict(record["state"])
        rounds = self.store.rounds(tid)
        execution_by_run = {}
        execution_calls_by_run = {}
        for item in rounds:
            for executed in [*item.get("prior_runs", []), item]:
                if executed.get("run_id"):
                    execution_by_run[executed["run_id"]] = executed.get("execution_usage")
                    execution_calls_by_run[executed["run_id"]] = executed.get("execution_calls") or []
        execution = list(execution_by_run.values())
        calls = [call for item in rounds for call in item.get("review_calls", [])]
        reviews = [call.get("usage") for call in calls]
        execution_calls = [call for values in execution_calls_by_run.values() for call in values]
        def known_total(values: list[Any]) -> int | None:
            known = [value["total_tokens"] for value in values if isinstance(value, dict) and type(value.get("total_tokens")) is int]
            return sum(known) if known else None
        all_usage = execution + reviews
        known = known_total(all_usage)
        unknown = any(value is None for value in all_usage) or any(call.get("retries", 0) for call in calls + execution_calls)
        state.update(max_rounds=1 + state["max_continuations"],
                     remaining_continuations=max(0, 1 + state["max_continuations"] - max(1, state["round"])),
                     rounds=rounds, can_accept_completion=self._can_accept(record),
                     token_usage={"total_tokens": None if unknown else known, "known_tokens": known or 0,
                                  "has_unknown": unknown, "execution_tokens": known_total(execution), "review_tokens": known_total(reviews)},
                     review_attempt_count=sum(call.get("attempts") or 1 for call in calls),
                     review_retry_count=None if any(call.get("retries") is None for call in calls) else sum(call["retries"] for call in calls),
                     execution_attempt_count=sum(sum(call.get("attempts") or 1 for call in values) if values else 1 for values in execution_calls_by_run.values()),
                     execution_retry_count=None if any(not values for values in execution_calls_by_run.values()) or any(call.get("retries") is None for call in execution_calls) else sum(call["retries"] for call in execution_calls))
        return state

    def _wait(self, record: dict[str, Any], error: str, phase: str = "waiting_user") -> None:
        if record["state"].get("phase") == phase and record["state"].get("error") == error:
            return
        state = {**record["state"], "phase": phase, "error": error}
        if self.store.save(record, state):
            thread = self.service.get_thread(record["owner_id"], record["thread_id"])
            if thread.status in {"active", "waiting_user", "paused", "needs_recovery"}:
                target = phase if phase in {"paused", "needs_recovery"} else "waiting_user"
                if target != thread.status and target not in THREAD_TRANSITIONS[thread.status]:
                    self.service.store.update_thread(thread.thread_id, owner_id=thread.owner_id, status="active")
                self.service.store.update_thread(thread.thread_id, owner_id=thread.owner_id,
                    status=target, summary=error)

    def start(self, owner: str, tid: str, nid: str, **_: Any) -> Any:
        record = self.store.get(tid, owner)
        if not record:
            raise AdapterError("监督记录不存在")
        if record["state"]["round"]:
            return self.sync(owner, tid, nid)
        self._dispatch(record)
        return self._result(self._node(owner, tid, nid))

    def _fingerprint_ok(self, node: Any, cid: str) -> bool:
        frozen_configuration = node.checkpoint.get("configuration_fingerprint")
        if frozen_configuration:
            from naiba.super_agent.baseline import validate_execution_baseline
            return not validate_execution_baseline(self.app, cid, frozen_configuration)
        current = self.app.storage.get_conversation(cid, include_trace=False)
        frozen = node.checkpoint.get("conversation_fingerprint", {})
        # First Chat submission freezes the server's default tool set and empty
        # skill policy; those defaults are equivalent to the creation baseline.
        def normalized(key: str, value: Any) -> Any:
            if key == "enabled_tool_ids":
                return sorted(value or [])
            if key == "skill_policy":
                return value or {"mode": "exclusive", "skill_ids": []}
            return value or ""
        return bool(current and all(normalized(k, current.get(k)) == normalized(k, v) for k, v in frozen.items()))

    def _dispatch(self, record: dict[str, Any]) -> None:
        cid, tid, owner = record["conversation_id"], record["thread_id"], record["owner_id"]
        guard = getattr(self.app.runs, "supervised_submission_guard", None)
        # Production uses the same guard as ordinary user submissions.
        with guard(cid) if callable(guard) else nullcontext():
            current = self.store.get(tid, owner)
            if current["version"] != record["version"]:
                return
            state = current["state"]
            thread = self.service.get_thread(owner, tid)
            node = self._node(owner, tid, current["node_id"])
            if thread.status != "active" or node.cancel_requested or state["phase"] not in {"ready", "dispatch_ready"}:
                return
            if not self._fingerprint_ok(node, cid):
                self._wait(current, "会话模型、Agent或权限已改变，请重新确认执行基准")
                return
            if state["round"] >= 1 + state["max_continuations"] or (state["deadline_at"] and _now() >= state["deadline_at"]):
                self._wait(current, "未完成，已达到自动续办上限或运行截止时间", "limit_reached")
                return
            number = state["round"] + 1
            correlation = f"supervised:{tid}:v{state['goal_version']}:r{number}"
            request = dict(node.checkpoint["start_request"])
            if number > 1:
                request["message"] = state["next_instruction"] + "\n\n原任务边界及完成标准仍然有效；需要新授权时请等待用户。"
            round_state = {"round": number, "phase": "dispatching", "correlation": correlation,
                           "human_epoch": state["human_epoch"], "generation": state["generation"],
                           "instruction": request["message"], "run_id": "", "review": None}
            state = {**state, "round": number, "phase": "dispatching", "deadline_at": state["deadline_at"] or (_now() + state["window_minutes"] * 60000)}
            if not self.store.save(current, state, round_state=round_state):
                return
            try:
                run = self.app.runs.submit_chat({**request, "conversation_id": cid}, execution_key=correlation, task_model_override=True,
                    configuration_fingerprint=node.checkpoint.get("configuration_fingerprint"),
                    supervised_context={"thread_id": tid, "node_id": node.node_id, "round": number,
                                        "review_id": state.get("review_id", ""), "previous_run_id": state.get("run_id", ""),
                                        "expected_version": current["version"] + 1, "human_epoch": state["human_epoch"],
                                        "generation": state["generation"], "goal_version": state["goal_version"]})
            except (PreflightRejected, ActiveRunError) as exc:
                rejected = self.store.get(tid)
                if rejected["state"]["phase"] == "dispatching" and rejected["state"]["human_epoch"] == state["human_epoch"]:
                    restored = {**rejected["state"], "round": number - 1, "phase": "waiting_user",
                                "error": f"本轮未提交，未消耗执行轮次：{exc}"}
                    self.store.save(rejected, restored, round_state={**round_state, "phase": "not_submitted", "error": str(exc)})
                    self.service.store.update_thread(tid, owner_id=owner, status="waiting_user", summary=restored["error"])
                return
            except Exception as exc:
                uncertain = self.store.get(tid)
                self._wait(uncertain, f"派发结果需恢复，未自动重提：{type(exc).__name__}", "needs_recovery")
                return
            self._bind(self.store.get(tid), round_state, str(run["id"]))

    def _bind(self, record: dict[str, Any], round_state: dict[str, Any], rid: str) -> None:
        state = {**record["state"], "run_id": rid}
        if state["phase"] == "dispatching":
            state["phase"] = "executing"
        round_state = {**round_state, "run_id": rid, "phase": "executing"}
        if not self.store.save(record, state, round_state=round_state):
            return
        node = self._node(record["owner_id"], record["thread_id"], record["node_id"])
        if node.status == "ready":
            node = self.service.store.update_node(node.node_id, owner_id=node.owner_id, status="running")
        self.service.store.update_node(node.node_id, owner_id=node.owner_id, executor_id=rid)
        thread = self.service.get_thread(node.owner_id, node.thread_id)
        ids = list(thread.linked_run_ids)
        if rid not in ids:
            self.service.store.update_thread(thread.thread_id, owner_id=node.owner_id, linked_run_ids=ids + [rid])

    def _recover_dispatch(self, record: dict[str, Any], round_state: dict[str, Any]) -> bool:
        with self.app.storage._connect() as db:
            rows = db.execute("SELECT id FROM background_tasks WHERE conversation_id=? AND kind='chat' AND json_valid(snapshot) AND json_extract(snapshot,?)=? LIMIT 2",
                              (record["conversation_id"], f"$.{SUPER_AGENT_EXECUTION_KEY}", round_state["correlation"])).fetchall()
        if len(rows) == 1:
            self._bind(record, round_state, str(rows[0][0]))
            return True
        self._wait(record, "派发结果无法唯一确认，需人工恢复；未重新提交", "needs_recovery")
        return False

    def _evidence(self, record: dict[str, Any], run: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], bool]:
        mid = str((run.get("detail") or {}).get("message_id") or "")
        with self.app.storage._connect() as db:
            row = db.execute("SELECT id,role,content,metadata FROM messages WHERE id=? AND conversation_id=?", (mid, record["conversation_id"])).fetchone()
        if not row or row["role"] != "assistant":
            raise ValueError("当前Run最终回复尚未完整落盘")
        metadata = json.loads(row["metadata"] or "{}")
        if str(metadata.get("run_id") or "") != str(run["id"]):
            raise ValueError("最终回复不属于当前Run")
        truncation = metadata.get("truncated")
        incomplete = bool(metadata.get("partial") or (isinstance(truncation, dict) and truncation.get("truncated")) or len(row["content"]) > 10000)
        evidence: dict[str, Any] = {}
        # run_events are an independent server-produced trace. A claimed path
        # in assistant prose or a successful list/read tool is no test proof.
        events = self.app.storage.list_run_events(run["id"], limit=200)
        with self.app.storage._connect() as db:
            total = int(db.execute("SELECT COUNT(*) FROM run_events WHERE run_id=?", (run["id"],)).fetchone()[0])
        incomplete = incomplete or total > len(events)
        for event in events:
            if event.get("type") != "tool_result":
                continue
            payload = event
            if isinstance(payload, dict):
                ref = f"run://{run['id']}/event/{event.get('sequence', event.get('id', ''))}"
                original_result = str(payload.get("result") or "")
                result = self.coordinator.domain.results._safe_text(original_result)
                # Tool invocation success is a verified execution fact, not a
                # blanket statement that a business criterion passed. The
                # reviewer still must match the shown command/check to it.
                clean = len(original_result) <= 4000 and not any(mark in original_result for mark in ("已截断", "TRUNCATED"))
                tool = str(payload.get("tool") or "")
                arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
                shell = tool in {"pwsh", "run_skill_script", "run_command", "execute_command", "shell", "run_shell"}
                verified = payload.get("success") is True and clean and (not shell or re.match(r"^exit_code=0(?:\n|\r|$)", result) is not None)
                safe_arguments = self.app.runs._confirmation_parameters(arguments)
                evidence[ref] = {"verified": verified, "kind": "tool_result", "tool": tool,
                                 "arguments": safe_arguments, "result": result[:4000],
                                 "result_chars_total": len(original_result), "result_chars_provided": min(len(result), 4000), "truncated": not clean}
                incomplete = incomplete or not clean
        background_jobs = self._background_jobs(record, run)
        creators = {"run_in_background", "subagent", "subagent_spawn", "comfyui_batch", "extract_video_frames_exact"}
        if not background_jobs and any(value.get("tool") in creators for value in evidence.values()):
            raise ValueError("后台结果无法按持久归属关联，需用户处理；未重提后台任务")
        for job in background_jobs:
            stored_result = job.get("result") if isinstance(job.get("result"), dict) else {}
            body = {key: stored_result[key] for key in ("response", "output", "text", "summary") if isinstance(stored_result.get(key), str)}
            if type(stored_result.get("exit_code")) is int:
                body["exit_code"] = stored_result["exit_code"]
            outcome = stored_result.get("execution_outcome")
            valid_outcome = (isinstance(outcome, dict) and type(outcome.get("version")) is int and outcome["version"] == 1 and outcome.get("status") == "completed")
            if isinstance(outcome, dict):
                body["execution_outcome"] = {key: outcome[key] for key in ("version", "status", "summary") if key in outcome}
            original_result = json.dumps(body, ensure_ascii=False)
            result = self.coordinator.domain.results._safe_text(original_result)
            clean = len(original_result) <= 4000 and not (job.get("detail") or {}).get("partial")
            clean = clean and (job.get("kind") != "subagent" or valid_outcome)
            evidence[f"job://{job['id']}"] = {"kind": "background_result", "verified": job.get("status") == "completed" and clean,
                "status": job.get("status"), "result": result[:4000], "truncated": not clean}
            incomplete = incomplete or not clean or job.get("status") != "completed"
        # A result record is real, but not by itself deterministic business QA.
        # First version stays conservative until an evidence-specific checker
        # verifies a tool's exit status/check scope. Subjective criteria remain
        # usable through explicitly user-selected [主观] standards.
        evidence[f"message://{mid}"] = {"verified": False, "kind": "assistant_statement", "text": self.coordinator.domain.results._safe_text(row["content"][:10000])}
        payload = {"goal": self.service.get_thread(record["owner_id"], record["thread_id"]).goal,
                   "criteria": record["state"]["criteria"], "round": record["state"]["round"],
                   "remaining_continuations": record["state"]["max_continuations"] + 1 - record["state"]["round"],
                   "evidence": evidence, "incomplete": incomplete, "execution_usage": self._usage(metadata.get("usage")),
                   "execution_calls": (metadata.get("performance") or {}).get("chat_requests") or [],
                   "omitted": {"events_total": total, "events_provided": len(events),
                               "assistant_chars_total": len(row["content"]), "assistant_chars_provided": min(10000, len(row["content"]))},
                   "previous_review": record["state"].get("last_review"), "boundary": "仅沿用原会话已有权限；不得授予新权限"}
        if record["state"].get("human_epoch"):
            with self.app.storage._connect() as db:
                additions = db.execute("SELECT content,metadata FROM messages WHERE conversation_id=? AND role='user' AND rowid>? ORDER BY rowid DESC LIMIT 5",
                    (record["conversation_id"], record["state"].get("human_boundary", 0))).fetchall()
            human = []
            for addition in reversed(additions):
                source = json.loads(addition["metadata"] or "{}")
                if source.get("supervised_source") or source.get("dependency_source") or (source.get("interjection") and not source.get("interjection_consumed")):
                    continue
                human.append(addition["content"])
            payload["human_additions"] = [value[:2000] for value in human]
            if len(human) > 4 or any(len(value) > 2000 for value in human):
                incomplete = True
                payload["incomplete"] = True
                payload["omitted"]["human_additions"] = "人工补充超过4条或2000字，部分内容未提供"
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded) > REVIEW_INPUT_LIMIT:
            raise ValueError("关键检查依据超出审查输入预算，需用户处理")
        return payload, evidence, incomplete

    def _background_jobs(self, record: dict[str, Any], run: dict[str, Any]) -> list[dict[str, Any]]:
        # A server persisted parent edge is authoritative; prose with an
        # invented Job id cannot establish a dependency or bypass this wait.
        with self.app.storage._connect() as db:
            rows = db.execute("SELECT id,conversation_id,owner_session_id FROM background_tasks WHERE parent_job_id=?", (run["id"],)).fetchall()
        jobs = []
        for row in rows:
            if str(row["conversation_id"] or "") != record["conversation_id"] or str(row["owner_session_id"] or "") != record["conversation_id"]:
                raise ValueError("后台依赖归属不匹配，需用户核对")
            job = self.app.storage.get_background_task(row["id"])
            if job:
                jobs.append(job)
        return jobs

    @staticmethod
    def _usage(raw: Any) -> dict[str, int] | None:
        if not isinstance(raw, dict):
            return None
        records = raw.get("requests_detail")
        if isinstance(records, list) and records:
            values = [SupervisedChatAdapter._usage(item) for item in records]
            if any(item is None for item in values):
                return None
            return {key: sum(item[key] for item in values) for key in ("input_tokens", "output_tokens", "total_tokens")}
        if not any(type(raw.get(key)) is int for key in ("input_tokens", "output_tokens", "total_tokens")):
            return None
        value = {key: max(0, raw.get(key, 0)) if type(raw.get(key)) is int else 0 for key in ("input_tokens", "output_tokens", "total_tokens")}
        value["total_tokens"] = value["total_tokens"] or value["input_tokens"] + value["output_tokens"]
        return value

    def _launch_review(self, record: dict[str, Any], run: dict[str, Any], round_state: dict[str, Any]) -> None:
        with self.workers_lock:
            if record["thread_id"] in self.workers or not self.capacity.acquire(blocking=False):
                return
            cancel = threading.Event()
            self.workers[record["thread_id"]] = (cancel, _now())
        try:
            payload, evidence, incomplete = self._evidence(record, run)
        except Exception as exc:
            with self.workers_lock:
                self.workers.pop(record["thread_id"], None)
            self.capacity.release()
            self._wait(record, str(exc))
            return
        review_id = f"{record['thread_id']}:r{record['state']['round']}:h{record['state']['human_epoch']}:review:{record['state']['review_count'] + 1}"
        state = {**record["state"], "phase": "reviewing", "review_count": record["state"]["review_count"] + 1,
                 "review_started_at": _now(), "review_id": review_id}
        round_state = {**round_state, "execution_usage": payload.get("execution_usage"), "execution_calls": payload.get("execution_calls") or []}
        if not self.store.save(record, state, round_state={**round_state, "phase": "reviewing"}):
            with self.workers_lock:
                self.workers.pop(record["thread_id"], None)
            self.capacity.release()
            return
        expected = {**record, "version": record["version"] + 1, "state": state}
        def review_worker() -> None:
            observation = {"review_id": review_id, "usage": None, "attempts": None, "retries": None, "status": "failed"}
            try:
                node = self._node(record["owner_id"], record["thread_id"], record["node_id"])
                request = node.checkpoint["start_request"]
                profile = dict(self.app.config.profile(request["model_key"]))
                profile["model"] = request["model_name"]
                self.app.models.last_usage = {}
                self.app.models.last_diagnostics = {}
                # Separate context and no tools/Agent role. Runtime cancellation
                # and total/request deadlines are independent of coordinator.
                raw = self.app.models.complete(profile, [{"role": "system", "content": REVIEW_SYSTEM},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                    {"stream": True, "tools": [], "max_tokens": 2500, "reasoning_enabled": False,
                     "cancel_event": cancel, "request_timeout_seconds": SUPERVISED_REVIEW_TIMEOUT_SECONDS,
                     "stream_total_timeout_seconds": SUPERVISED_REVIEW_TIMEOUT_SECONDS, "lock_label": "任务总管结果检查"})
                observation["usage"] = self._usage(getattr(self.app.models, "last_usage", {}))
                result = parse_review(raw, state["criteria"], evidence, incomplete=incomplete)
                if cancel.is_set():
                    observation["status"] = "cancelled"
                    return
                self._accept_review(expected, result, round_state, evidence)
                observation["status"] = "completed"
            except Exception as exc:
                current = self.store.get(record["thread_id"])
                if current and current["version"] == expected["version"]:
                    self._wait(current, f"结果检查失败：{type(exc).__name__}；未自动续办")
            finally:
                diagnostics = getattr(self.app.models, "last_diagnostics", {}) or {}
                attempts = diagnostics.get("attempts")
                if type(attempts) is int and attempts > 0:
                    observation.update(attempts=attempts, retries=max(0, attempts - 1))
                observation["usage"] = observation["usage"] or self._usage(getattr(self.app.models, "last_usage", {}))
                try:
                    self.store.observe_review(record["thread_id"], state["round"], observation)
                finally:
                    with self.workers_lock:
                        self.workers.pop(record["thread_id"], None)
                        self.worker_threads.discard(threading.current_thread())
                    self.capacity.release()
                    self.coordinator.wake_event.set()
        worker = threading.Thread(target=review_worker, name="supervised-review", daemon=True)
        with self.workers_lock:
            self.worker_threads.add(worker)
            worker.start()

    def _accept_review(self, expected: dict[str, Any], result: dict[str, Any], round_state: dict[str, Any], evidence: dict[str, Any]) -> None:
        guard = getattr(self.app.runs, "supervised_submission_guard", None)
        with guard(expected["conversation_id"]) if callable(guard) else nullcontext():
            self._accept_review_guarded(expected, result, round_state, evidence)

    def _accept_review_guarded(self, expected: dict[str, Any], result: dict[str, Any], round_state: dict[str, Any], evidence: dict[str, Any]) -> None:
        current = self.store.get(expected["thread_id"])
        if current["version"] != expected["version"]:
            return
        node = self._node(current["owner_id"], current["thread_id"], current["node_id"])
        if not self._fingerprint_ok(node, current["conversation_id"]):
            self._wait(current, "执行基准已改变，请重新确认任务；旧检查不会推进", "paused")
            return
        if current["state"]["deadline_at"] and _now() >= current["state"]["deadline_at"]:
            self._wait(current, "未完成，已到运行截止时间", "limit_reached")
            return
        state = dict(current["state"])
        signature = hashlib.sha256(json.dumps({"missing": sorted(result["missing_items"]), "instruction": result["next_instruction"],
            "result": {k: v for k, v in evidence.items()}}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        # Rewording a requested action cannot reset identical unresolved items.
        missing_key = json.dumps(sorted(result["missing_items"]), ensure_ascii=False)
        # Reference ids change each round, so compare normalized content.
        objective_missing = any(item["status"] != "passed" and not state["criteria"][item["criterion"]].startswith("[主观]") for item in result["criteria_results"])
        subjective = not objective_missing and any(criterion.startswith("[主观]") for criterion in state["criteria"])
        progress_evidence = [value for value in evidence.values() if subjective or value.get("kind") != "assistant_statement"]
        content_digest = hashlib.sha256(json.dumps(sorted((json.dumps(v, sort_keys=True, ensure_ascii=False) for v in progress_evidence)), ensure_ascii=False).encode()).hexdigest()
        repeated = bool(state.get("missing_key") == missing_key and state.get("evidence_digest") == content_digest)
        state.update(last_review=result, reviewed_run_id=state.get("run_id"), reviewed_human_epoch=state["human_epoch"],
                     next_instruction=result["next_instruction"], progress_summary=result["progress_summary"],
                     review_signature=signature, missing_key=missing_key, evidence_digest=content_digest)
        decision = result["decision"]
        state["phase"] = {"done": "completed", "continue": "dispatch_ready", "needs_user": "waiting_user", "blocked": "blocked"}[decision]
        state["error"] = result["user_question"] or (result["summary"] if decision in {"blocked", "needs_user"} else "")
        if repeated and decision == "continue":
            state.update(phase="blocked", error="连续检查未发现有效新进展，自动续办已停止")
        if decision == "continue" and state["round"] >= state["max_continuations"] + 1:
            state.update(phase="limit_reached", error="未完成，已达到自动续办上限")
        if not self.store.save(current, state, release=decision == "done", round_state={**round_state, "phase": "reviewed", "review": result}):
            return
        owner, tid, nid = current["owner_id"], current["thread_id"], current["node_id"]
        if decision == "done":
            self.service.emit_event(owner, tid, "run_completed", node_id=nid, source=self.executor_type,
                idempotency_key=f"supervised-done:{tid}:{state['goal_version']}", payload={"output_refs": [f"run://{state['run_id']}"]})
        elif state["phase"] in {"blocked", "waiting_user", "limit_reached"}:
            self.service.store.update_thread(tid, owner_id=owner, status="waiting_user", summary=state["error"])

    def sync(self, owner: str, tid: str, nid: str, **_: Any) -> Any:
        node = self._node(owner, tid, nid)
        record = self.store.get(tid, owner)
        state = record["state"]
        thread = self.service.get_thread(owner, tid)
        if state["phase"] == "completed":
            self._complete(record)
            return self._result(self._node(owner, tid, nid))
        if thread.status in {"completed", "cancelled", "archived"}:
            return self._result(node)
        if state["phase"] == "stopping" or node.cancel_requested or thread.status == "stopping":
            self.cancel_review(tid)
            rid = state.get("run_id")
            run = self.app.storage.get_background_task(rid) if rid else None
            with self.workers_lock:
                reviewing = tid in self.workers
            jobs = self._background_jobs(record, run) if run else []
            active_jobs = any(job.get("status") not in {"completed", "failed", "cancelled", "interrupted"} for job in jobs)
            if run and (run.get("status") not in {"completed", "failed", "cancelled", "interrupted"} or active_jobs):
                self.app.runs.cancel(rid)
            elif not reviewing and (run is not None or not state["round"]):
                if self.store.save(record, {**state, "phase": "cancelled"}, release=True):
                    self.service.store.update_node(nid, owner_id=owner, status="cancelled", cancel_requested=True)
                    self.service.store.update_thread(tid, owner_id=owner, status="cancelled")
            return self._result(self._node(owner, tid, nid))
        if state["deadline_at"] and _now() >= state["deadline_at"]:
            self.cancel_review(tid)
            if state.get("run_id"):
                run = self.app.storage.get_background_task(state["run_id"]) or {}
                if run.get("status") in {"running", "queued", "waiting"}:
                    self.app.runs.cancel(state["run_id"])
            self._wait(record, "未完成，已到运行截止时间", "limit_reached")
            return self._result(node)
        if thread.status in {"paused", "needs_recovery"}:
            return self._result(node)
        if state["phase"] in {"executing", "waiting_approval", "waiting_background", "reviewing", "dispatch_ready"} and not self._fingerprint_ok(node, record["conversation_id"]):
            self.cancel_review(tid)
            if state.get("run_id"):
                self.app.runs.cancel(state["run_id"])
            self._wait(record, "执行基准已改变，自动续办已暂停；请重新确认任务", "paused")
            return self._result(node)
        if state["phase"] == "dispatch_ready":
            self._dispatch(record)
        elif state["phase"] == "dispatching":
            self._recover_dispatch(record, self.store.rounds(tid)[-1])
        elif state["phase"] in {"executing", "waiting_approval", "waiting_background", "stopping"}:
            run = self.app.storage.get_background_task(state.get("run_id", "")) or {}
            status = run.get("status")
            if status in {"completed", "failed", "cancelled", "interrupted"}:
                if node.cancel_requested or thread.status == "stopping":
                    self.store.save(record, {**state, "phase": "cancelled"}, release=True)
                    self.service.store.update_node(nid, owner_id=owner, status="cancelled", cancel_requested=True)
                elif status != "completed":
                    self._wait(record, f"本轮执行{status}，不会自动重跑可能有副作用的操作", "blocked")
                else:
                    try:
                        jobs = self._background_jobs(record, run)
                        if any(job.get("status") not in {"completed", "failed", "cancelled", "interrupted"} for job in jobs):
                            if state["phase"] != "waiting_background":
                                self.store.save(record, {**state, "phase": "waiting_background", "error": "等待本轮后台任务真实完成"})
                        elif any(job.get("status") != "completed" for job in jobs):
                            self._wait(record, "本轮后台任务未成功，不会自动重提", "blocked")
                        else:
                            self._launch_review(record, run, self.store.rounds(tid)[-1])
                    except ValueError as exc:
                        self._wait(record, str(exc))
            elif status in {"running", "queued", "waiting", "stopping", "cancelling"}:
                confirmations = self.app.runs.confirmation_snapshot(state["run_id"])
                pending = any(item.get("status") == "pending" for item in confirmations)
                target = "waiting_approval" if pending else "executing"
                if target != state["phase"] and not node.cancel_requested:
                    self.store.save(record, {**state, "phase": target})
                    self.service.store.update_thread(tid, owner_id=owner, status="waiting_user" if pending else "active")
            else:
                self._wait(record, "原Run不存在或状态不明，未重新提交", "needs_recovery")
        elif state["phase"] == "reviewing":
            with self.workers_lock:
                worker = self.workers.get(tid)
            if not worker:
                self._wait(record, "检查已中断，需要人工继续；不会重复请求审查", "needs_recovery")
            elif _now() - worker[1] >= SUPERVISED_REVIEW_TIMEOUT_SECONDS * 1000:
                worker[0].set()
                self._wait(record, "结果检查超时，已停止自动派发", "blocked")
        return self._result(self._node(owner, tid, nid), status=state["phase"])

    status = sync

    def cancel_review(self, tid: str) -> None:
        with self.workers_lock:
            worker = self.workers.get(tid)
            if worker:
                worker[0].set()

    def shutdown(self, timeout: float = 10.0) -> None:
        """A database can close only after every actual review thread returns."""
        with self.workers_lock:
            for event, _ in self.workers.values():
                event.set()
            threads = list(self.worker_threads)
        deadline = time.monotonic() + max(0.0, timeout)
        for worker in threads:
            if worker is not threading.current_thread():
                worker.join(max(0.0, deadline - time.monotonic()))

    def _can_accept(self, record: dict[str, Any]) -> bool:
        state = record["state"]
        result = state.get("last_review")
        if (state["phase"] not in {"waiting_user", "blocked", "paused", "needs_recovery", "limit_reached"}
                or not isinstance(result, dict) or state.get("reviewed_run_id") != state.get("run_id")
                or state.get("reviewed_human_epoch") != state["human_epoch"]):
            return False
        results = result.get("criteria_results") or []
        if not results or any(item["status"] == "failed" for item in results) or not any(item["status"] == "unknown" for item in results):
            return False
        with self.workers_lock:
            if record["thread_id"] in self.workers:
                return False
        run = self.app.storage.get_background_task(state.get("run_id", "")) or {}
        if run.get("status") != "completed":
            return False
        node = self._node(record["owner_id"], record["thread_id"], record["node_id"])
        if node.cancel_requested or node.status in {"cancelled", "stopping", "failed"}:
            return False
        try:
            return all(job.get("status") == "completed" for job in self._background_jobs(record, run))
        except ValueError:
            return False

    def _complete(self, record: dict[str, Any]) -> None:
        state = record["state"]
        thread = self.service.get_thread(record["owner_id"], record["thread_id"])
        if thread.status in {"paused", "needs_recovery", "waiting_user"}:
            self.service.store.update_thread(record["thread_id"], owner_id=record["owner_id"], status="active")
        node = self._node(record["owner_id"], record["thread_id"], record["node_id"])
        if node.cancel_requested or thread.status in {"cancelled", "stopping", "archived"}:
            return
        if node.status == "interrupted":
            node = self.service.store.update_node(node.node_id, owner_id=node.owner_id, status="ready")
        if node.status == "ready":
            self.service.store.update_node(node.node_id, owner_id=node.owner_id, status="running")
        self.service.emit_event(record["owner_id"], record["thread_id"], "run_completed", node_id=record["node_id"], source=self.executor_type,
            idempotency_key=f"supervised-done:{record['thread_id']}:{state['goal_version']}",
            payload={"output_refs": [f"run://{state['run_id']}"]})
        if state.get("completion_source") == "user":
            self.service.store.update_thread(record["thread_id"], owner_id=record["owner_id"], summary="用户确认完成（保留原检查结果）")

    def _human_baseline(self, record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        state = dict(record["state"])
        rounds = self.store.rounds(record["thread_id"])
        current_round = dict(rounds[-1]) if rounds else {"round": max(1, state["round"])}
        with self.app.storage._connect() as db:
            messages = db.execute("SELECT id,metadata FROM messages WHERE conversation_id=? AND role='user' AND rowid>? ORDER BY rowid DESC",
                                  (record["conversation_id"], state.get("human_boundary", 0))).fetchall()
        for row in messages:
            metadata = json.loads(row["metadata"] or "{}")
            if metadata.get("supervised_source") or metadata.get("dependency_source"):
                continue
            # Unsent/interjected content is not silently consumed on resume.
            if metadata.get("interjection") and not metadata.get("interjection_consumed"):
                raise AdapterError("人工插话尚未被执行消费，请在聊天中明确引导或发送后再继续")
            rid = str(metadata.get("run_id") or "")
            run = self.app.storage.get_background_task(rid) if rid else None
            if not run or str(run.get("conversation_id")) != record["conversation_id"]:
                continue
            history = list(current_round.get("prior_runs") or [])
            if current_round.get("run_id") and current_round["run_id"] != rid:
                history.append({key: current_round.get(key) for key in ("run_id", "input_message_id", "review", "execution_usage", "execution_calls")})
            snapshot = self.app.storage.get_run_snapshot(rid) or {}
            current_round.update(prior_runs=history, run_id=rid, input_message_id=row["id"], phase="executing", review=None,
                                 human_epoch=state["human_epoch"], execution_usage=None, adopted_human=True,
                                 correlation=str(snapshot.get(SUPER_AGENT_EXECUTION_KEY) or ""))
            state.update(run_id=rid, round=max(1, state["round"]), phase="executing", baseline_human_epoch=state["human_epoch"], last_review=None,
                         reviewed_run_id=None, reviewed_human_epoch=None, next_instruction="")
            return state, current_round
        raise AdapterError("尚未找到人工补充后的有效执行结果，请先在关联聊天发送或引导消息")

    def control(self, owner: str, tid: str, action: str, *, request_id: str = "") -> Any:
        record = self.store.get(tid, owner)
        if not record:
            raise AdapterError("监督任务不存在")
        guard = getattr(self.app.runs, "supervised_submission_guard", None)
        with guard(record["conversation_id"]) if callable(guard) else nullcontext():
            return self._control_guarded(owner, tid, action, request_id=request_id)

    def _control_guarded(self, owner: str, tid: str, action: str, *, request_id: str) -> Any:
        record = self.store.get(tid, owner)
        if not record:
            raise AdapterError("监督任务不存在")
        node = self._node(owner, tid, record["node_id"])
        state = dict(record["state"])
        action = "continue" if action == "resume" else action
        requests = dict(state.get("control_requests") or {})
        if request_id and request_id in requests:
            if requests[request_id] != action:
                raise AdapterError("相同请求标识不能用于另一控制动作")
            if state["phase"] == "completed":
                self._complete(record)
            return self._result(node)
        if request_id:
            requests[request_id] = action
            state["control_requests"] = requests
        if action == "pause":
            if state["phase"] in {"completed", "cancelled", "stopping"}:
                return self._result(node)
            self.cancel_review(tid)
            state.update(phase="paused", generation=state["generation"] + (state["phase"] != "paused"),
                         resume_phase=state.get("resume_phase") if state["phase"] == "paused" else state["phase"])
            self.store.save(record, state)
            if self.service.get_thread(owner, tid).status == "needs_recovery":
                self.service.store.update_thread(tid, owner_id=owner, status="active")
            self.service.store.update_thread(tid, owner_id=owner, status="paused", summary="已暂停自动续办；在途执行仍可能运行")
        elif action == "continue":
            if state["phase"] in {"ready", "executing", "waiting_approval", "waiting_background", "dispatch_ready", "dispatching", "reviewing"}:
                if request_id:
                    self.store.remember_noop(tid, request_id, action)
                return self._result(node)
            if state["phase"] in {"completed", "cancelled", "limit_reached"}:
                raise AdapterError("任务已结束或达到限制；继续不会重置预算")
            if state["deadline_at"] and _now() >= state["deadline_at"]:
                raise AdapterError("运行窗口已到期，请重新确认新任务")
            if not self._fingerprint_ok(node, record["conversation_id"]):
                raise AdapterError("执行基准已变化，请重新创建并确认")
            with self.workers_lock:
                if tid in self.workers:
                    raise AdapterError("上一检查正在停止，请稍后继续")
            phase = "executing" if state.get("run_id") else "dispatch_ready"
            round_state = None
            if state["human_epoch"] != state.get("baseline_human_epoch", 0):
                state, round_state = self._human_baseline(record)
                state["control_requests"] = requests
                phase = "executing"
            elif state.get("resume_phase") == "dispatch_ready":
                phase = "dispatch_ready"
            elif state.get("resume_phase") == "dispatching" or (state["phase"] == "needs_recovery" and not state.get("run_id") and state["round"]):
                phase = "dispatching"
            elif (isinstance(state.get("last_review"), dict) and state.get("reviewed_run_id") == state.get("run_id")
                    and state.get("reviewed_human_epoch") == state["human_epoch"]):
                decision = state["last_review"]["decision"]
                phase = {"done": "completed", "continue": "dispatch_ready", "needs_user": "waiting_user", "blocked": "blocked"}[decision]
                if state.get("resume_phase") == "blocked" or state["phase"] == "blocked":
                    phase = "blocked"
            state.update(phase=phase, error=(state.get("last_review", {}).get("user_question") or state.get("last_review", {}).get("summary") or "")
                         if phase in {"blocked", "waiting_user"} else "", generation=state["generation"] + 1)
            if not self.store.save(record, state, round_state=round_state):
                return self._result(node)
            if round_state is not None:
                self._bind(self.store.get(tid), round_state, state["run_id"])
            target = "waiting_user" if phase in {"waiting_user", "blocked"} else "active"
            current_thread = self.service.get_thread(owner, tid)
            if target != current_thread.status and target not in THREAD_TRANSITIONS[current_thread.status]:
                self.service.store.update_thread(tid, owner_id=owner, status="active")
            self.service.store.update_thread(tid, owner_id=owner, status=target)
            if node.status == "interrupted":
                self.service.store.update_node(node.node_id, owner_id=owner, status="ready")
                if state.get("run_id"):
                    self.service.store.update_node(node.node_id, owner_id=owner, status="running")
        elif action == "accept_completion":
            if not self._can_accept(record):
                raise AdapterError("只有当前有效检查仍含无法判断且无未通过项时，才能由用户确认完成")
            state.update(phase="completed", completion_source="user", user_confirmed_at=_now(),
                         generation=state["generation"] + 1, error="")
            if self.store.save(record, state, release=True):
                self._complete({**record, "state": state})
        elif action == "cancel":
            self.cancel_review(tid)
            state.update(phase="stopping", generation=state["generation"] + 1)
            self.store.save(record, state)
            rid = state.get("run_id")
            run = self.app.storage.get_background_task(rid) if rid else None
            if node.status == "ready" and not rid:
                self.service.store.update_node(node.node_id, owner_id=owner, status="cancelled", cancel_requested=True)
                self.store.save(self.store.get(tid), {**state, "phase": "cancelled"}, release=True)
                self.service.store.update_thread(tid, owner_id=owner, status="cancelled")
            else:
                if node.status == "ready":
                    self.service.store.update_node(node.node_id, owner_id=owner, status="running")
                thread = self.service.get_thread(owner, tid)
                if thread.status == "needs_recovery":
                    self.service.store.update_thread(tid, owner_id=owner, status="active")
                self.service.store.update_thread(tid, owner_id=owner, status="stopping")
                self.service.store.update_node(node.node_id, owner_id=owner, status="stopping", cancel_requested=True)
                if rid and run:
                    self.app.runs.cancel(rid)
                self.sync(owner, tid, node.node_id)
        else:
            raise ValueError("监督任务不支持该控制动作")
        self.coordinator.wake_event.set()
        return self._result(self._node(owner, tid, node.node_id))

    def cancel(self, owner: str, tid: str, nid: str, **_: Any) -> Any:
        return self.control(owner, tid, "cancel")
