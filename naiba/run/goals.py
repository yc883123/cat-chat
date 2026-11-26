"""Opt-in GOAL orchestration on top of the normal chat Run worker."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from naiba.core.contracts import GOAL_RUN_KEY, GOAL_DEFAULT_LIMITS, GOAL_LOCAL_CHECK_KIND, GOAL_CHECK_VERSION
from naiba.core.fsutil import atomic_write_bytes
from naiba.core.trace_view import complete_tool_boundaries, trace_fingerprint
from naiba.storage.goals import GOAL_TERMINAL, GoalConflict, GoalStore, goal_now
from naiba.super_agent.baseline import execution_fingerprint, validate_execution_baseline

logger = logging.getLogger(__name__)

GOAL_TOOLS = frozenset({"read_file", "list_directory", "search_files", "write_file", "edit_file", "reset_context",
                        "job_status", "job_output", "job_wait"})
GOAL_SYSTEM = """GOAL 模式：根据原始目标自主工作，在现有工具权限内继续，先复核产物再交付。不要向用户追问。没有权限或缺少资料时，报告真实阻塞；不要用其他工具绕过。需要交接时在最终回复末尾另起一行写 GOAL: continue、GOAL: completed、GOAL: blocked 或 GOAL: needs_user。服务端独立验收，回复文字不等于验收。"""
_QUANTUM = 24
_MAX_ROUNDS = 32
_MAX_MODEL_REQUESTS = 256
_MAX_TOOL_ACTIONS = 200
_DURATION_MS = 3 * 60 * 60 * 1000
_MAX_FILE_BYTES = 2 * 1024 * 1024
_ACTION_RESULT = re.compile(r"(?:^|\n)GOAL:\s*(continue|completed|blocked|needs_user)\s*\Z", re.IGNORECASE)
_FILE_CRITERION = re.compile(r"^\s*验收文件[：:]\s*(.+?)\s*$", re.MULTILINE)
_SENSITIVE_DIRS = frozenset({".git", ".ssh", ".gnupg", ".aws", ".zcode", ".venv"})


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _criteria(message: str, raw: Any) -> list[dict[str, str]]:
    supplied = raw if raw is not None else [{"path": item.strip('`"')} for item in _FILE_CRITERION.findall(message)]
    if not isinstance(supplied, list) or len(supplied) > 64:
        raise ValueError("验收文件清单最多 64 项")
    result = []
    for item in supplied:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"].strip():
            raise ValueError("验收文件必须指定路径")
        path = item["path"].strip()
        candidate = Path(path)
        if candidate.is_absolute() or candidate.drive or ".." in candidate.parts or any(part.casefold() in _SENSITIVE_DIRS for part in candidate.parts):
            raise ValueError("验收文件必须是工作区内相对路径")
        expected = str(item.get("sha256") or "").lower()
        contains = str(item.get("contains") or "")
        if len(path) > 512 or len(contains) > 4096 or (expected and not re.fullmatch(r"[a-f0-9]{64}", expected)):
            raise ValueError("验收文件条件无效")
        result.append({"path": path, "sha256": expected, "contains": contains})
    return result


def _file_digest(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            data = handle.read(_MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return ""
    if len(data) > _MAX_FILE_BYTES:
        raise ValueError("GOAL 只支持 2 MB 以内的文件核对")
    return _digest(data)


class GoalCycle:
    def __init__(self, service: GoalService, row: dict[str, Any], snapshot: dict[str, Any]):
        self.service = service
        self.row = row
        self.snapshot = snapshot
        self.checkpoint = dict(row["checkpoint"])
        self.checkpoint["round_summary_attempts"] = 0
        self.checkpoint["summary_attempt_signature"] = ""
        self.checkpoint["writes"] = 0
        self.checkpoint["progress_count"] = 0
        self.checkpoint["last_error_sig"] = ""
        self.checkpoint["needs_user_stop"] = False
        self.resume_view = self.checkpoint.get("messages") or []
        self.actions = 0
        self.writes = 0
        self.tool_successes = 0
        self.error_signatures: list[str] = []
        self.needs_user_stop = False
        self.model_calls = 0
        self.stop_reason = ""
        self.stop_code = ""
        self.step = 0
        self._guard_lock = threading.Lock()
        self.cancel_event = None
        self.event = lambda payload: None

    def guard(self) -> str:
        self.stop_code = ""
        row = self.service.store.get(self.row["id"])
        if not row or row["run_id"] != self.row["run_id"] or row["status"] != "running" or self.service._stop.is_set():
            return "目标已暂停或取消"
        if row["deadline_at"] > 0 and row["deadline_at"] <= goal_now():
            self.stop_code = "deadline"
            return "目标已到截止时间"
        baseline = self.snapshot.get(GOAL_RUN_KEY, {}).get("baseline")
        if not baseline:
            self.stop_code = "configuration"
            return "目标执行配置缺失"
        reason = validate_execution_baseline(self.service.app, row["conversation_id"], baseline)
        if reason:
            self.stop_code = "configuration"
            return reason
        if not Path(str(self.snapshot.get("workspace_dir") or "")).is_dir():
            self.stop_code = "configuration"
            return "目标工作区已不存在"
        if row.get("sources"):
            from naiba.run.goal_plan import verify_sources_current
            allow_outside = str(self.snapshot.get("permission_mode") or "") == "full"
            if not verify_sources_current({"sources": row["sources"]}, self.snapshot["workspace_dir"],
                                          self.snapshot.get("attachments") or [],
                                          allow_outside=allow_outside,
                                          data_dir=self.service.app.paths.data_dir):
                self.stop_code = "source_changed"
                return "引用计划已变化或无法读取，请结束目标后按新版本重新创建"
        return ""

    def before_send(self) -> bool:
        with self._guard_lock:
            self.stop_reason = self.guard()
            if not self.stop_reason and not self.service.store.reserve(
                self.row["id"], self.row["run_id"], "model_requests", 1,
                _MAX_MODEL_REQUESTS if _MAX_MODEL_REQUESTS != GOAL_DEFAULT_LIMITS["model_requests"] else None,
            ):
                self.stop_reason = "目标模型请求额度已用完"
                self.stop_code = "budget_model"
            return not self.stop_reason

    def save(self, messages: list[dict[str, Any]], *, outcome: str = "", reason: str = "") -> None:
        self.checkpoint.update({
            "messages": list(messages), "outcome": outcome, "reason": reason,
            "step": self.step, "writes": self.writes,
            "progress_count": self.writes + self.tool_successes,
            "last_error_sig": self.error_signatures[-1] if self.error_signatures else "",
            "needs_user_stop": self.needs_user_stop,
            "owner_run_id": self.row["run_id"],
            "stop_code": self.stop_code,
        })
        self.service.store.checkpoint(self.row["id"], self.row["run_id"], self.checkpoint)

    def boundary(self, messages: list[dict[str, Any]], step: int, step_limit: int) -> bool:
        self.step = step
        if not self.resume_view or step:
            self.save(messages)
        self.stop_reason = self.guard()
        if self.stop_reason:
            self.save(messages, outcome="blocked", reason=self.stop_reason)
            return True
        if self.model_calls + self.actions >= _QUANTUM or (step_limit and step >= step_limit):
            self.save(messages, outcome="continue", reason="本轮额度已用完，自动续接")
            return True
        self.model_calls += 1
        return False

    def compact(self, messages: list[dict[str, Any]], trace_start: int, limit: int,
                budget: int, used: int) -> dict[str, Any]:
        from naiba.core import compaction
        from naiba.core.history import compaction_replay_message

        settings = compaction.resolve_settings(self.service.app.config.data.get("auto_compaction"))
        conversation = self.service.app.storage.get_conversation(self.row["conversation_id"], include_messages=False) or {}
        if compaction.effective_mode(settings, conversation) != compaction.MODE_AUTO:
            return {}
        if not compaction.should_compact(settings, used=used, limit=limit, reserve=max(0, limit - budget)):
            return {}
        attempts = int(self.checkpoint.get("round_summary_attempts") or 0)
        if attempts >= compaction.MAX_RUN_SUMMARY_ATTEMPTS:
            return {}
        from naiba.core.trace_view import is_summary_message, is_tool_result_message

        protected = 2
        for index, item in enumerate(messages[2:], 2):
            if item.get("role") in {"user", "system"} and not is_tool_result_message(item) and not is_summary_message(item):
                protected = index + 1
        boundaries = complete_tool_boundaries(list(messages[protected:]))
        if len(boundaries) <= 2:
            return {}
        end = protected + boundaries[-3]
        region = list(messages[protected:end])
        signature = trace_fingerprint(region)
        if signature == self.checkpoint.get("summary_attempt_signature"):
            return {}
        if compaction.estimate_messages_tokens(region) < compaction.MIN_REGION_TOKENS:
            return {}
        self.checkpoint["summary_attempt_signature"] = signature
        from naiba.llm.profiles import resolve_profile_with_override

        profile = resolve_profile_with_override(self.service.app.config, str(self.snapshot["model_key"]),
                                                str(self.snapshot["model_name"]))
        started = False

        def on_started() -> None:
            nonlocal started
            if not started:
                self.event({"type": "compaction", "action": "started",
                            "message": "正在整理较早的对话和本轮工具记录，当前轮会继续…"})
                started = True

        result = compaction.request_summary(self.service.app, region, profile=profile, settings=settings,
            cancel_event=self.cancel_event, before_request_send=self.before_send,
            on_started=on_started,
            attempt_limit=min(compaction.MAX_SUMMARY_ATTEMPTS, compaction.MAX_RUN_SUMMARY_ATTEMPTS - attempts))
        self.checkpoint["round_summary_attempts"] = attempts + len(result.get("attempts") or [])
        summary = str(result.get("summary") or "")
        if not summary:
            self.event({"type": "compaction", "action": "failed", "reason": result.get("reason") or "unknown",
                        "attempts": result.get("attempts") or [], "message": compaction.failure_message(result.get("reason") or "unknown")})
            self.save(messages)
            return {}
        projected = [*messages[:protected], compaction_replay_message(summary), *messages[end:]]
        before = compaction.estimate_messages_tokens(list(messages))
        after = compaction.estimate_messages_tokens(projected)
        if after >= before:
            self.event({"type": "compaction", "action": "failed", "reason": "freed_too_small",
                        "attempts": result.get("attempts") or [], "message": compaction.failure_message("freed_too_small")})
            self.save(messages)
            return {}
        self.checkpoint["compaction_generation"] = int(self.checkpoint.get("compaction_generation") or 0) + 1
        self.checkpoint["last_compaction"] = {"before": before, "after": after, "attempts": result.get("attempts") or []}
        self.save(projected)
        # Existing trace remains an audit record; the checkpoint carries the full model projection.
        self.event({"type": "compaction", "action": "compacted", "freed_est": before - after,
                    "used": used, "limit": limit, "model_key": self.snapshot["model_key"], "attempts": result.get("attempts") or []})
        if trace_start < end:
            return {"history": projected[1:protected + 1], "trace": list(messages[end:]), "trace_info": {"summary": summary}}
        return {"history": projected[1:protected + 1 + trace_start - end], "limit": limit, "budget": budget}

    def reserve_group(self, messages: list[dict[str, Any]], count: int) -> bool:
        if count >= _QUANTUM:
            self.save(messages, outcome="blocked", reason="一次工具组超过单轮上限")
            return False
        if self.model_calls + self.actions + count > _QUANTUM:
            self.save(messages, outcome="continue", reason="下一个工具组需要换轮")
            return False
        if self.guard() or not self.service.store.reserve(
            self.row["id"], self.row["run_id"], "tool_actions", count,
            _MAX_TOOL_ACTIONS if _MAX_TOOL_ACTIONS != GOAL_DEFAULT_LIMITS["tool_actions"] else None,
        ):
            self.stop_code = self.stop_code or "budget_tools"
            self.save(messages, outcome="blocked", reason="目标权限已变化或工具额度已用完")
            return False
        self.actions += count
        self.checkpoint["group_id"] = uuid.uuid4().hex
        return True

    def finish_text(self, messages: list[dict[str, Any]], content: str) -> str:
        match = _ACTION_RESULT.search(content)
        action = match.group(1).lower() if match else "review"
        body = content[:match.start()].rstrip() if match else content
        self.save(messages, outcome=action, reason=body[:500] if action in {"blocked", "needs_user"} else "")
        return body

    def path(self, raw: Any, *, writing: bool = False) -> Path:
        workspace = Path(str(self.snapshot["workspace_dir"])).resolve(strict=True)
        value = str(raw or "")
        if writing and not value:
            raise GoalConflict("需要明确指定目标文件")
        candidate = Path(value) if value else workspace
        if not candidate.is_absolute():
            candidate = workspace / candidate
        candidate = candidate.resolve()
        if any(part.casefold() in _SENSITIVE_DIRS or part.casefold().startswith(".env") for part in candidate.parts):
            raise GoalConflict("GOAL 不自动访问配置或密钥路径")
        uploads = {Path(str(item.get("path") or "")).resolve() for item in self.snapshot.get("attachments", [])
                   if isinstance(item, dict) and str(item.get("path") or "")}
        outside_attachment = (not candidate.is_relative_to(workspace)
                              and not writing and candidate in uploads and candidate.is_file())
        data_dir = Path(self.service.app.paths.data_dir).resolve()
        if candidate.is_relative_to(data_dir) and not outside_attachment:
            raise GoalConflict("GOAL 不能访问应用数据目录")
        if not candidate.is_relative_to(workspace) and not outside_attachment:
            if str(self.snapshot.get("permission_mode") or "") != "full":
                raise GoalConflict("GOAL 不自动访问工作区外路径；只读附件必须来自本次目标")
        if candidate.is_relative_to(workspace):
            rel = candidate.relative_to(workspace)
            if any(part.casefold() in _SENSITIVE_DIRS or part.casefold().startswith(".env") for part in rel.parts):
                raise GoalConflict("GOAL 不自动访问配置或密钥路径")
        if writing and candidate == workspace:
            raise GoalConflict("不能写入工作区根目录")
        frozen_sources = {Path(item["path"]).resolve() for item in self.row.get("sources", [])}
        if writing and candidate in frozen_sources:
            raise GoalConflict("引用计划已冻结，GOAL 不能修改计划源文件")
        return candidate

    def _job(self, tool: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        from naiba.run.job_waits import summarize_job_result

        job_id = str(arguments.get("job_id") or "")
        allowed = set(self.snapshot.get(GOAL_RUN_KEY, {}).get("job_ids") or [])
        binding = (self.service.store.get(self.row["id"]) or {}).get("wait_binding") or {}
        if binding.get("target_id"):
            allowed.add(binding["target_id"])
        list_goal_tasks = getattr(self.service.app.storage, "list_goal_tasks", None)
        if callable(list_goal_tasks):
            for task in list_goal_tasks(self.row["id"]):
                if task.get("id"):
                    allowed.add(str(task["id"]))
        if job_id not in allowed:
            return False, "只能查询本目标已绑定、派生或启动时明确指定的后台任务"
        job = self.service.app.jobs.get(job_id, owner=self.row["conversation_id"])
        if not job or job.get("conversation_id") != self.row["conversation_id"]:
            self.stop_reason = "目标绑定的后台任务不存在或归属不符"
            return False, self.stop_reason
        if tool == "job_wait" and job["status"] in {"queued", "running", "waiting", "stopping", "cancelling"}:
            self.checkpoint["wait_binding"] = {"kind": "job", "target_id": job_id, "goal_id": self.row["id"],
                "run_id": self.row["run_id"], "state": "waiting"}
            self.tool_successes += 1
            return True, "已登记目标等待，后台任务未结束；不会重复提交或发送模型请求。"
        if tool == "job_output":
            self.tool_successes += 1
            return True, json.dumps(self.service.app.jobs.read(job_id, owner=self.row["conversation_id"]),
                                    ensure_ascii=False)[:6000]
        text, _ = summarize_job_result(job)
        self.tool_successes += 1
        return True, text

    def _record_outcome(self, tool: str, success: bool, result: str) -> None:
        if success:
            self.tool_successes += 1
            self.error_signatures.clear()
        else:
            snippet = " ".join(str(result or "").split())[:160]
            self.error_signatures.append(f"{tool}:{snippet}")

    def exercise(self, tool: str, arguments: dict[str, Any], active: list[dict[str, Any]],
                 executor: Any, context: dict[str, Any], source: str,
                 dispatch: Callable[[], tuple[bool, str]]) -> tuple[bool, str]:
        denial = self.guard()
        if denial:
            self.stop_reason = denial
            return False, denial
        if source == "fenced":
            self.stop_reason = "代码块里的工具动作需要人工确认，GOAL 未执行"
            self.needs_user_stop = True
            return False, self.stop_reason
        if tool in {"job_status", "job_output", "job_wait"}:
            return self._job(tool, arguments)
        if tool == "reset_context":
            if executor._confirmation_reason(tool, arguments, active, context):
                self.stop_reason = "重置上下文需要人工确认，GOAL 未执行"
                self.needs_user_stop = True
                return False, self.stop_reason
            self.checkpoint["rotate"] = True
            return True, "已请求在本组结束后换轮，原始记录保留。"
        if tool in {"read_file", "search_files", "list_directory"}:
            try:
                path = self.path(arguments.get("path"), writing=False)
            except (GoalConflict, ValueError, OSError) as exc:
                self.stop_reason = str(exc) if isinstance(exc, GoalConflict) else "无法安全定位工具路径"
                return False, self.stop_reason
            arguments["path"] = str(path)
            if tool == "list_directory" and ".." in str(arguments.get("pattern") or ""):
                return False, "GOAL 列目录不接受上级路径通配符"
            success, result = dispatch()
            if str(result).startswith("NEED_CONFIRM:"):
                executor.reject_execute(str(result).split(":", 3)[1])
                self.stop_reason = "读取需要人工确认，GOAL 未执行"
                self.needs_user_stop = True
                return False, self.stop_reason
            self._record_outcome(tool, success, result)
            return success, result
        if tool in {"write_file", "edit_file"}:
            try:
                path = self.path(arguments.get("path"), writing=True)
            except (GoalConflict, ValueError, OSError) as exc:
                self.stop_reason = str(exc) if isinstance(exc, GoalConflict) else "无法安全定位工具路径"
                return False, self.stop_reason
            arguments["path"] = str(path)
            if getattr(executor, "permission_mode", "confirm") == "deny":
                self.stop_reason = "当前对话禁止写文件，GOAL 未执行"
                return False, self.stop_reason
            confirm_reason = (
                executor._confirmation_reason(tool, arguments, active, context)
                if callable(getattr(executor, "_confirmation_reason", None))
                else ""
            )
            if confirm_reason:
                self.stop_reason = f"工具 {tool} 需要人工确认（{confirm_reason}），GOAL 已暂停等待处理"
                self.needs_user_stop = True
                return False, self.stop_reason
            try:
                success, result = self._write(tool, path, arguments, context)
                self._record_outcome(tool, success, result)
                return success, result
            except (OSError, ValueError) as exc:
                if self.service.store.pending_actions(self.row["id"]):
                    self.stop_reason = "文件写入结果需要核对，已停止后续动作"
                return False, str(exc) if isinstance(exc, ValueError) else "文件写入失败，将核对落盘状态"
        # 统一会话权限：除文件写入走幂等落盘回执外，其余会话已授权工具按当前会话权限直接调度。
        allowed_tools = set(str(item) for item in (context.get("allowed_tools") or []))
        if tool not in allowed_tools:
            return False, "这项工具未在当前会话授权，GOAL 未执行"
        tool_registry = getattr(self.service.app, "tool_registry", None)
        has_side_effect = bool(tool_registry and tool_registry.side_effect(tool)) and tool != "todo_write"
        action_key = ""
        if has_side_effect:
            action_key = uuid.uuid4().hex
            self.service.store.start_action(
                self.row["id"], self.row["run_id"], action_key,
                {"kind": "generic_tool", "tool": tool},
            )
        success, result = dispatch()
        if str(result).startswith("NEED_CONFIRM:"):
            executor.reject_execute(str(result).split(":", 3)[1])
            if action_key:
                # 尚未执行即被确认门拦截：丢弃未发出的 pending 回执，不能标成已执行。
                self.service.store.discard_action(self.row["id"], action_key)
            self.stop_reason = f"工具 {tool} 需要人工确认，GOAL 已暂停等待处理"
            self.needs_user_stop = True
            return False, self.stop_reason
        if action_key:
            self.service.store.finish_action(self.row["id"], action_key)
        self._record_outcome(tool, success, result)
        return success, result

    def _write(self, tool: str, path: Path, args: dict[str, Any], context: dict[str, Any]) -> tuple[bool, str]:
        key = uuid.uuid4().hex
        goal_id = self.row["id"]
        previous = self.service.store.action(goal_id, key)
        if previous:
            if _file_digest(path) == previous["after"]:
                self.service.store.finish_action(goal_id, key)
                self.writes += 1
                return True, previous["result"]
            self.stop_reason = "上一次写入的结果无法确认，未重新写入"
            return False, self.stop_reason
        before = _file_digest(path)
        if tool == "write_file":
            if args.get("append") or before:
                return False, "GOAL 的 write_file 只创建新文件；修改已有文件请用可核对原文的 edit_file"
            data = str(args.get("content") or "").encode("utf-8")
        else:
            old = str(args.get("old_text") or "")
            if not old or not before:
                return False, "缺少要替换的原文或文件不存在"
            current = path.read_bytes().decode("utf-8")
            count = current.count(old)
            replace_all = bool(args.get("all"))
            if not count or (count > 1 and not replace_all):
                return False, "原文不匹配或不唯一，文件未修改"
            data = current.replace(old, str(args.get("new_text") or ""), -1 if replace_all else 1).encode("utf-8")
        if len(data) > _MAX_FILE_BYTES:
            return False, "GOAL 只支持 2 MB 以内的文件写入"
        after = _digest(data)
        result = f"已{'创建' if not before else '修改'} {path.name}，SHA-256 {after}"
        self.service.store.start_action(goal_id, self.row["run_id"], key,
            {"path": str(path), "before": before, "after": after, "result": result})
        if self.guard() or self.path(path, writing=True) != path or _file_digest(path) != before:
            self.stop_reason = "目标权限或文件内容发生变化，未执行写入"
            return False, self.stop_reason
        if before:
            atomic_write_bytes(path, data)
        else:
            # Do not replace a file created after the initial absence check.
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(f".goal-{uuid.uuid4().hex}.tmp")
            try:
                with temp.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.link(temp, path)
            finally:
                temp.unlink(missing_ok=True)
        self.service.store.finish_action(goal_id, key)
        self.writes += before != after
        return True, result


class GoalService:
    def __init__(self, app: Any):
        self.app = app
        self.store = GoalStore(app.storage)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def public(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = {key: row[key] for key in ("id", "conversation_id", "message", "status", "version", "reason",
            "generation", "run_id", "created_at", "deadline_at", "model_requests", "tool_actions", "evidence")}
        result.update({
            "limits": dict(row.get("limits") or GOAL_DEFAULT_LIMITS),
            "execution_policy": str(row.get("execution_policy") or "bounded"),
            "stop_code": str(row.get("stop_code") or ""),
            "completion_policy": str(row.get("completion_policy") or "manual"),
            "stages": list(row.get("stages") or []),
            "manual_items": list(row.get("manual_items") or []),
            "wait_binding": dict(row.get("wait_binding") or {}),
            "criteria": [{"path": item.get("path"), "kind": str(item.get("kind") or "file"),
                          "check_id": str(item.get("check_id") or "")}
                         for item in (row.get("criteria") or []) if isinstance(item, dict)][:64],
            "sources": [{"path": item.get("path"), "name": item.get("name"), "sha256": item.get("sha256")}
                        for item in (row.get("sources") or []) if isinstance(item, dict)],
        })
        return result

    def rounds(self, goal_id: str, page_size: int = 20, cursor: str = "") -> dict[str, Any]:
        return self.store.rounds(goal_id, page_size, cursor)

    def preview_plan(self, body: dict[str, Any]) -> dict[str, Any]:
        """Freeze explicit plan sources; no source discovery and no authorization here."""
        cid = str(body.get("conversation_id") or "")
        message = str(body.get("message") or "")
        if not cid:
            raise ValueError("需要对话")
        conversation = self.app.storage.get_conversation(cid, include_messages=False)
        if not conversation:
            raise LookupError("对话不存在")
        from naiba.run.goal_plan import preview
        workspace = (self.app.config.resolve_workspace_dir(str(conversation.get("workspace_dir") or ""))
                     if str(conversation.get("workspace_dir") or "").strip()
                     else self.app.config.resolve_workspace_dir())
        allow_outside = str(conversation.get("permission_mode") or "") == "full"
        plan = preview(message, body.get("sources"), workspace, body.get("attachments") or [],
                       allow_outside=allow_outside, data_dir=self.app.paths.data_dir)
        # 原文不回传前端：提交时以 sha256 复核，正文一致性用 plan.message 校验。
        for row in plan.get("sources") or []:
            if isinstance(row, dict):
                row.pop("text", None)
        return plan

    def extend(self, goal_id: str, body: dict[str, Any]) -> dict[str, Any]:
        # 原样透传，让存储层统一做严格校验；过滤或补默认值会改变幂等哈希。
        row = self.store.extend(goal_id, body.get("version"), str(body.get("request_id") or ""),
                                body.get("increments") or {}, body.get("extra_minutes", 0))
        self.start()
        return self.public(row)

    def submit(self, body: dict[str, Any]) -> dict[str, Any]:
        cid = str(body.get("conversation_id") or "")
        message = str(body.get("message") or "").strip()
        request_id = str(body.get("request_id") or "")
        if not cid or not message or not request_id or len(request_id) > 128 or len(message) > 20000:
            raise ValueError("需要对话、目标正文和有效的请求编号")
        plan = body.get("plan") if isinstance(body.get("plan"), dict) else None
        criteria = None
        digest = _digest(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        with self._lock:
            existing = self.store.request(cid, request_id)
            if existing:
                if existing["request_hash"] != digest:
                    raise GoalConflict("同一目标请求编号不能修改内容")
                self.start()
                return self.public(existing)
            conversation = self.app.storage.get_conversation(cid, include_messages=False)
            if not conversation:
                raise LookupError("对话不存在")
            if conversation.get("conv_kind") != "main" or conversation.get("archived"):
                raise ValueError("仅未归档的主对话支持 GOAL")
            job_ids = [str(item).strip() for item in body.get("job_ids") or [] if str(item).strip()][:10]
            for item in job_ids:
                job = self.app.jobs.get(item, owner=cid)
                if not job or job.get("conversation_id") != cid:
                    raise ValueError(f"引用的后台任务不存在或不属于该对话：{item[:12]}")
            from naiba.run.goal_checks import normalize_criteria
            sources: list[dict[str, Any]] = []
            stages: list[dict[str, Any]] = []
            manual_items: list[dict[str, Any]] = []
            if plan:
                if str(plan.get("message") or "") != message:
                    raise GoalConflict("目标正文与计划预览不一致，请重新解析计划")
                from naiba.run.goal_plan import verify_sources_current
                workspace = (self.app.config.resolve_workspace_dir(str(conversation.get("workspace_dir") or ""))
                             if str(conversation.get("workspace_dir") or "").strip()
                             else self.app.config.resolve_workspace_dir())
                allow_outside = str(conversation.get("permission_mode") or "") == "full"
                if not verify_sources_current(plan, workspace, body.get("attachments") or [],
                                              allow_outside=allow_outside, data_dir=self.app.paths.data_dir):
                    raise GoalConflict("引用计划已变化或无法读取，请重新预览后再发送")
                criteria = normalize_criteria(plan.get("criteria"))
                sources = list(plan.get("sources") or [])
                stages = list(plan.get("stages") or [])
                manual_items = list(plan.get("manual_items") or [])
            elif criteria is None:
                # 正文里的「验收文件：」行；显式条件（含本地检查）走严格规范化。
                criteria = normalize_criteria(body["criteria"]) if body.get("criteria") is not None \
                    else _criteria(message, None)
            policy = str(plan.get("completion_policy") or "") if plan else ""
            if policy not in {"machine_all", "mixed", "manual"}:
                policy = "machine_all" if criteria else ("mixed" if manual_items else "manual")
            if manual_items and policy == "machine_all":
                policy = "mixed"
            execution_policy = "bounded" if str(body.get("execution_policy") or "") == "bounded" else "continuous"
            spec = {"id": uuid.uuid4().hex, "request_id": request_id, "request_hash": digest,
                    "message": message, "duration_ms": _DURATION_MS, "criteria": criteria,
                    "job_ids": job_ids, "sources": sources, "stages": stages,
                    "manual_items": manual_items, "completion_policy": policy,
                    "execution_policy": execution_policy}
            self.app.runs.submit_chat(body, goal_spec=spec)
            row = self.store.get(spec["id"])
            self.start()
            return self.public(row)

    def latest(self, conversation_id: str) -> dict[str, Any] | None:
        return self.public(self.store.latest(conversation_id))

    def start(self) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            self._wake.set()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="naiba-goals", daemon=True)
                self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.3)
            self._wake.clear()
            with self._lock:
                if self._stop.is_set():
                    return
                pending = self.store.pending()
                # Reconcile every control/finished round before choosing one
                # queued goal. A slow worker must not starve another's cancel.
                for goal_id, status in pending:
                    if status != "queued":
                        try:
                            self._tick(goal_id)
                        except Exception:
                            logger.exception("目标状态核对失败：%s", goal_id)
                with self.app.runs._lock:
                    live_ids = set(self.app.runs._threads)
                current = self.store.pending()
                if any((self.store.get(goal_id) or {}).get("run_id") in live_ids for goal_id, _ in current):
                    continue
                for goal_id, status in current:
                    if status != "queued":
                        continue
                    if self._stop.is_set():
                        return
                    try:
                        if self._tick(goal_id):
                            break
                    except Exception:  # noqa: BLE001 - a goal must not stop other work
                        logger.exception("目标调度失败：%s", goal_id)

    def _tick(self, goal_id: str) -> bool:
        row = self.store.get(goal_id)
        run_id = row["run_id"]
        run = self.app.runs.get(run_id) or {}
        snapshot = self.app.storage.get_run_snapshot(run_id) or {}
        run_status = run.get("status")
        with self.app.runs._lock:
            live = run_id in self.app.runs._threads
        status = row["status"]
        if status in {"pausing", "cancelling"}:
            if live:
                return True
            if run_status in self.app.runs.ACTIVE:
                self.app.storage.update_background_task(run_id, status="cancelled", finished=True,
                                                        cancel_requested=True, detail={"message": "目标已停止"})
            if status == "cancelling":
                self._cancel_goal_jobs(row)
            self.store.transition(row, "paused" if status == "pausing" else "cancelled", row["reason"], settle=True)
            return False
        if status == "running":
            if live:
                if row["deadline_at"] > 0 and row["deadline_at"] <= goal_now():
                    if self.store.transition(row, "pausing", "目标已到截止时间"):
                        self.app.runs.cancel(run_id, goal_control=True)
                return True
            self._settle(row, run_status, snapshot)
            return False
        if status == "waiting_tool":
            self._tick_waiting(row)
            return False
        if status != "queued" or row["ready_at"] > goal_now():
            return False
        limits = row.get("limits") or GOAL_DEFAULT_LIMITS
        is_continuous = str(row.get("execution_policy") or "bounded") == "continuous"
        deadline_hit = row["deadline_at"] > 0 and row["deadline_at"] <= goal_now()
        rounds_hit = (
            (not is_continuous and row["generation"] >= limits["rounds"])
            or (_MAX_ROUNDS != GOAL_DEFAULT_LIMITS["rounds"] and row["generation"] >= _MAX_ROUNDS)
        )
        if deadline_hit or (rounds_hit and run_status != "queued"):
            if run_status == "queued":
                self.app.runs.cancel(run_id, goal_control=True)
                if not live:
                    self.app.storage.update_background_task(run_id, status="cancelled", finished=True,
                                                            cancel_requested=True, detail={"message": "目标已停止"})
            self.store.transition(row, "blocked", "目标时间或续跑轮次额度已用完", settle=True,
                                  stop_code="deadline" if deadline_hit else "budget_rounds")
            return False
        if not run or not snapshot.get(GOAL_RUN_KEY):
            self.store.transition(row, "blocked", "找不到目标关联的运行记录", settle=True)
            return False
        cid = row["conversation_id"]
        baseline = snapshot[GOAL_RUN_KEY].get("baseline")
        denial = validate_execution_baseline(self.app, cid, baseline) if baseline else "目标执行配置缺失"
        if denial:
            if run_status == "queued":
                self.app.runs.cancel(run_id, goal_control=True)
                if not live:
                    self.app.storage.update_background_task(run_id, status="cancelled", finished=True,
                                                            cancel_requested=True, detail={"message": denial})
            self.store.transition(row, "needs_user", denial, settle=True, stop_code="configuration")
            return False
        if run_status != "queued":
            snapshot["is_first_turn"] = False
            new_run = self.app.storage.create_run(cid, str(run["message"]), snapshot["agent"], snapshot,
                input_message_id=row["input_message_id"], origin_run_id=run_id, goal_id=goal_id,
                goal_bind=lambda db, new_id: self.store.bind_next(db, goal_id, new_id, row["version"]))
            run_id = new_run["id"]
            run = new_run
        if self.store.claim(goal_id, run_id):
            self.app.runs._start(run, self.app.runs._run_chat)
            return True
        return False

    def _tick_waiting(self, row: dict[str, Any]) -> None:
        """Poll a persisted wait binding; waiting never spends model requests."""
        from naiba.run.job_waits import resolve_wait_target, summarize_job_result, summarize_wait_target

        binding = row.get("wait_binding") or {}
        target_id = str(binding.get("target_id") or "")
        kind = str(binding.get("kind") or "")
        if not target_id:
            self.store.transition(row, "needs_user", "等待绑定丢失，需要人工处理", settle=True, stop_code="job_missing")
            return
        if kind == GOAL_LOCAL_CHECK_KIND:
            self._tick_check(row, target_id)
            return
        target = resolve_wait_target(self.app, target_id)
        if target is None:
            self.store.transition(row, "needs_user", "后台任务不存在或无法确认是否已提交，未重新提交",
                                  settle=True, stop_code="job_missing")
            return
        target_status = str(target.get("status") or "")
        if target_status in {"queued", "running", "waiting", "stopping", "cancelling"}:
            return
        summary, _ = summarize_wait_target(target) if kind != "job" else summarize_job_result(target)
        cp = {**(row.get("checkpoint") or {})}
        # 等待已解析：必须清掉交接标记，否则续办轮的工具组闭合会被误判成新等待，
        # 旧的 run_id 再触发「等待绑定不属于当前目标轮次」把目标永久卡在 running。
        cp.pop("wait_binding", None)
        cp["wait_result"] = {"target_id": target_id, "kind": kind, "status": target_status, "summary": summary}
        if target_status == "completed":
            self.store.transition(row, "queued", "后台任务已完成，继续目标", checkpoint=cp, settle=True, retry_count=0)
        else:
            self.store.transition(row, "blocked", f"后台任务结束于 {target_status}，未自动重试",
                                  checkpoint=cp, settle=True, stop_code="job_failed")

    def _tick_check(self, row: dict[str, Any], job_id: str) -> None:
        from naiba.jobs import JobSpec
        from naiba.run.goal_checks import verify_snapshot_current

        raw = self.app.storage.get_background_task(job_id) or {}
        job_status = str(raw.get("status") or "")
        if job_status in {"queued", "interrupted"}:
            params = ((raw.get("checkpoint") or {}).get("goal_check") or {}) if isinstance(raw.get("checkpoint"), dict) else {}
            try:
                self.app.jobs.start_registered(job_id, JobSpec(kind=GOAL_LOCAL_CHECK_KIND,
                    conversation_id=row["conversation_id"], params=params))
            except Exception:
                logger.exception("目标本地检查启动失败：%s", job_id)
            return
        if job_status in {"running", "waiting", "stopping", "cancelling"}:
            return
        result = raw.get("result") if isinstance(raw.get("result"), dict) else {}
        binding = row.get("wait_binding") or {}
        checkpoint = raw.get("checkpoint") if isinstance(raw.get("checkpoint"), dict) else {}
        inputs = (checkpoint.get("goal_check") or {}).get("input_snapshot")
        valid = (job_status == "completed" and bool(result) and not result.get("cancelled")
                 and result.get("input_fingerprint") == binding.get("input_fingerprint")
                 and verify_snapshot_current(inputs))
        evidence = list(result.get("evidence") or [])
        if not valid:
            if not evidence:
                evidence = [{"kind": "check", "passed": False,
                             "reason": "本地验收未能完成或输入已变化，未自动通过"}]
        self._finalize(row, list(row.get("evidence") or []), evidence, failed_reason="本地验收未通过")

    def _finalize(self, row: dict[str, Any], file_evidence: list[dict[str, Any]],
                  check_evidence: list[dict[str, Any]], *, failed_reason: str) -> None:
        merged = [*(file_evidence or []), *(check_evidence or [])]
        if all(item.get("passed") for item in merged):
            if row.get("completion_policy") == "machine_all" and row.get("criteria"):
                self.store.transition(row, "completed", "验收条件已全部通过", evidence=merged, settle=True)
            else:
                self.store.transition(row, "awaiting_review", "机器条件已通过，等待人工验收", evidence=merged, settle=True)
            return
        if row["no_progress"] >= 1:
            self.store.transition(row, "blocked", failed_reason, evidence=merged, settle=True, stop_code="check_failed")
            return
        cp = {**(row.get("checkpoint") or {}), "check_evidence": [], "outcome": "",
              "correction": [str(item.get("path") or item.get("check_id") or "")
                             for item in merged if not item.get("passed")]}
        self.store.transition(row, "queued", "验收未通过，准备纠偏", checkpoint=cp, evidence=merged,
            settle=True, no_progress=row["no_progress"] + 1, retry_count=0)

    def _dispatch_check(self, cycle: GoalCycle, row: dict[str, Any], cp: dict[str, Any],
                        file_evidence: list[dict[str, Any]]) -> None:
        """Register the local check job, its inputs and the wait binding atomically."""
        from naiba.jobs import JobSpec
        from naiba.run.goal_checks import capture_inputs

        check_items = [item for item in row["criteria"] if str(item.get("kind") or "file") != "file"]
        try:
            inputs = capture_inputs(Path(str(cycle.snapshot["workspace_dir"])), check_items)
        except (GoalConflict, ValueError, OSError) as exc:
            self.store.transition(row, "needs_user", f"无法冻结本地验收输入：{exc}", settle=True)
            return
        params = {"input_snapshot": inputs, "criteria": check_items}
        binding = {"kind": GOAL_LOCAL_CHECK_KIND, "goal_id": row["id"], "run_id": row["run_id"],
                   "state": "waiting", "input_fingerprint": str(inputs.get("fingerprint") or "")}

        def job_bind(db, job_id):
            if not self.store.bind_check(db, row, job_id, {**binding, "target_id": job_id}):
                raise GoalConflict("目标状态已变化，未登记本地检查")

        try:
            job = self.app.storage.create_run(row["conversation_id"], "GOAL 本地验收",
                {"id": "", "name": "GOAL", "system_prompt": "", "skill_ids": []},
                {"goal_check": {"criteria": check_items}}, kind=GOAL_LOCAL_CHECK_KIND, detached=True,
                owner_session_id=row["conversation_id"], goal_id=row["id"], job_bind=job_bind,
                initial_checkpoint={"goal_check": params})
        except GoalConflict:
            return  # 状态已变化：本轮放弃登记，下一次核对重新评估。
        except (ValueError, RuntimeError) as exc:
            self.store.transition(row, "needs_user", f"本地验收登记失败：{exc}", settle=True)
            return
        fresh = self.store.get(row["id"])
        if fresh and fresh.get("status") == "waiting_tool" and file_evidence:
            # bind_check 只转状态；文件证据在派发检查的同一收尾里一并持久化，
            # 检查收集时才能和检查证据合并成完整验收记录。
            self.store.transition(fresh, "waiting_tool", evidence=file_evidence)
        self.app.jobs.start_registered(str(job["id"]), JobSpec(kind=GOAL_LOCAL_CHECK_KIND,
            conversation_id=row["conversation_id"], params=params))

    def cycle(self, goal_id: str, run_id: str, snapshot: dict[str, Any]) -> GoalCycle:
        row = self.store.get(goal_id)
        if not row or row["run_id"] != run_id or row["status"] != "running":
            raise GoalConflict("目标已停止或轮次失效")
        conversation = self.app.storage.get_conversation(row["conversation_id"], include_trace=False)
        if not conversation or not any(item.get("id") == row["input_message_id"] for item in conversation.get("messages", [])):
            raise GoalConflict("目标的原始消息已不在对话中")
        return GoalCycle(self, row, snapshot)

    def _cancel_goal_jobs(self, row: dict[str, Any]) -> None:
        """取消目标绑定的等待任务及本目标派生的所有活跃后台任务/子代理。"""
        cid = str(row.get("conversation_id") or "")
        seen: set[str] = set()
        binding = row.get("wait_binding") if isinstance(row.get("wait_binding"), dict) else {}
        target_id = str(binding.get("target_id") or "")
        if target_id:
            seen.add(target_id)
            try:
                self.app.jobs.cancel(target_id, owner=cid, reason="目标已取消")
            except Exception:  # noqa: BLE001 - 外部不可取消任务不阻断目标取消
                logger.debug("目标绑定任务取消失败：%s", target_id, exc_info=True)
        list_goal_tasks = getattr(self.app.storage, "list_goal_tasks", None)
        if callable(list_goal_tasks):
            for task in list_goal_tasks(row["id"], active_only=True):
                tid = str(task.get("id") or "")
                if not tid or tid in seen or tid == str(row.get("run_id") or ""):
                    continue
                seen.add(tid)
                try:
                    self.app.jobs.cancel(tid, owner=cid, reason="目标已取消")
                except Exception:  # noqa: BLE001
                    logger.debug("目标关联任务取消失败：%s", tid, exc_info=True)

    def _rebaseline_snapshot(self, row: dict[str, Any]) -> None:
        """按当前会话设置刷新 GOAL 执行基线（仅在用户显式请求“按当前设置继续”时调用）。"""
        cid = str(row.get("conversation_id") or "")
        run_id = str(row.get("run_id") or "")
        conversation = self.app.storage.get_conversation(cid, include_messages=False)
        snapshot = self.app.storage.get_run_snapshot(run_id) or {}
        if not conversation or not snapshot:
            raise GoalConflict("无法读取当前会话配置，请先检查会话状态")
        agent_id = str(conversation.get("agent_id") or "")
        agent = self.app.config.get_agent(agent_id) or self.app.config.get_agent(self.app.config.default_agent_id()) or snapshot.get("agent") or {}
        model_key = self.app.config.resolve_conversation_model_key(
            str(conversation.get("model_key") or ""), str(conversation.get("provider_id") or ""),
        )
        model_name = str(conversation.get("model_name") or "").strip()
        if not model_name:
            model_name = str((self.app.config.profile(model_key) or {}).get("model") or "")
        workspace_dir = str(
            self.app.config.resolve_workspace_dir(str(conversation.get("workspace_dir") or ""))
            if str(conversation.get("workspace_dir") or "").strip()
            else self.app.config.resolve_workspace_dir()
        )
        from naiba.run.session import resolve_allowed_tools, resolve_session_tool_ids

        web_search_enabled = bool(int(conversation.get("web_search_enabled") or 0))
        conv_kind = str(conversation.get("conv_kind") or "main")
        enabled_tool_ids = resolve_session_tool_ids(self.app, conversation, agent)
        allowed_tools = resolve_allowed_tools(
            self.app, str(snapshot.get("interaction_mode") or "craft"),
            agent, web_search_enabled, model_key, enabled_tool_ids, conv_kind,
        )
        stored_skill_policy = conversation.get("skill_policy")
        goal_meta = dict(snapshot.get(GOAL_RUN_KEY) or {})
        goal_meta["baseline"] = self.baseline(cid)
        updates = {
            "agent": agent,
            "model_key": model_key,
            "model_name": model_name,
            "workspace_dir": workspace_dir,
            "permission_mode": str(conversation.get("permission_mode") or "confirm"),
            "reasoning_effort": str(conversation.get("reasoning_effort") or "auto"),
            "web_search_enabled": web_search_enabled,
            "allowed_tools": allowed_tools,
            **({"skill_policy": dict(stored_skill_policy)} if isinstance(stored_skill_policy, dict) and stored_skill_policy else {}),
            GOAL_RUN_KEY: goal_meta,
        }
        self.app.storage.update_run_snapshot(run_id, updates)

    def _settle(self, row: dict[str, Any], run_status: str | None, snapshot: dict[str, Any]) -> None:
        cycle = GoalCycle(self, row, snapshot)
        recovered_write = False
        for action_id, receipt in self.store.pending_actions(row["id"]):
            if receipt.get("kind") == "generic_tool":
                self.store.transition(
                    row, "needs_user",
                    f"副作用工具 {receipt.get('tool') or ''} 在中断前未确认结果，未自动重试",
                    settle=True, stop_code="unknown_write",
                )
                return
            try:
                actual = _file_digest(cycle.path(receipt["path"], writing=True))
                confirmed = actual == receipt["after"]
            except (OSError, ValueError, GoalConflict, KeyError):
                confirmed = False
            if not confirmed:
                self.store.transition(row, "needs_user", "有写入动作无法核对，未重新执行", settle=True)
                return
            self.store.finish_action(row["id"], action_id)
            recovered_write = True
        cp = row["checkpoint"]
        current_round = cp.get("owner_run_id") == row["run_id"]
        decision = cp.get("outcome", "") if current_round else ""
        if recovered_write and decision not in {"completed", "review"}:
            self.store.transition(row, "needs_user", "上一次写入已核对，缺少完整工具结果，未自动重做", settle=True)
            return
        if decision == "wait_tool":
            binding = cp.get("wait_binding") or {}
            if binding.get("target_id"):
                self.store.transition(row, "waiting_tool", str(cp.get("reason") or "等待后台任务完成"),
                                      checkpoint=cp, settle=True, wait_binding=binding)
            else:
                self.store.transition(row, "needs_user", "等待登记不完整，需要人工处理", settle=True)
            return
        if run_status in {"interrupted", "queued", "running"}:
            if decision in {"continue", "completed", "review"}:
                run_status = "completed"
            elif row["retry_count"] >= 2:
                self.store.transition(row, "blocked", "多次中断后仍未完成", settle=True)
                return
            else:
                self.store.transition(row, "queued", "正在从最近检查点恢复", settle=True,
                    checkpoint={**cp, "outcome": "", "writes": 0, "progress_count": 0}, retry_count=row["retry_count"] + 1)
                return
        if decision in {"blocked", "needs_user"}:
            target_status = "needs_user" if (decision == "needs_user" or cp.get("needs_user_stop")) else "blocked"
            self.store.transition(row, target_status, str(cp.get("reason") or "目标暂时无法继续"), settle=True,
                                  stop_code=str(cp.get("stop_code") or ""))
            return
        if run_status != "completed":
            self.store.transition(row, "blocked", "本轮没有正常完成，请查看对话中的错误", settle=True,
                                  stop_code="error")
            return
        evidence = self._verify(cycle, row["criteria"])
        check_items = [item for item in row["criteria"] if str(item.get("kind") or "file") != "file"]
        if decision not in {"completed", "review"}:
            last_err = str(cp.get("last_error_sig") or "") if current_round else ""
            prev_err = str(cp.get("prev_error_sig") or "")
            repeat_err_count = (int(cp.get("repeat_error_count") or 0) + 1) if (last_err and last_err == prev_err) else (1 if last_err else 0)
            next_cp = {**cp, "prev_error_sig": last_err, "repeat_error_count": repeat_err_count}
            if repeat_err_count >= 3:
                self.store.transition(
                    row, "paused", "检测到连续相同错误循环，已自动暂停，请查看后点击继续目标",
                    checkpoint=next_cp, evidence=evidence, settle=True, stop_code="loop_detected",
                )
                return
            has_progress = bool(current_round and (cp.get("writes") or int(cp.get("progress_count") or 0) > 0))
            no_progress = 0 if has_progress else row["no_progress"] + 1
            is_continuous = str(row.get("execution_policy") or "bounded") == "continuous"
            max_no_progress = 3 if is_continuous else 2
            if no_progress >= max_no_progress:
                self.store.transition(
                    row, "paused" if is_continuous else "blocked",
                    "续跑多轮仍未取得可确认进展", checkpoint=next_cp, evidence=evidence,
                    settle=True, stop_code="no_progress",
                )
                return
            self.store.transition(row, "queued", "准备继续目标", checkpoint=next_cp, evidence=evidence,
                settle=True, no_progress=no_progress, retry_count=0)
            return
        if check_items and not cp.get("check_evidence"):
            self._dispatch_check(cycle, row, cp, evidence)
            return
        check_evidence = list(cp.get("check_evidence") or [])
        merged = [*evidence, *check_evidence]
        file_pass = all(item["passed"] for item in evidence)
        check_pass = not check_items or (bool(check_evidence) and all(item.get("passed") for item in check_evidence))
        if file_pass and check_pass:
            if row["completion_policy"] == "machine_all" and row["criteria"]:
                self.store.transition(row, "completed", "验收条件已全部通过", evidence=merged, settle=True)
            else:
                self.store.transition(row, "awaiting_review", "机器条件已通过，等待人工验收", evidence=merged, settle=True)
            return
        if row["no_progress"] >= 1:
            self.store.transition(row, "blocked", "验收条件未通过", evidence=merged, settle=True, stop_code="check_failed")
            return
        cp = {**cp, "check_evidence": [],
              "correction": [str(item.get("path") or item.get("check_id") or "")
                             for item in merged if not item.get("passed")]}
        self.store.transition(row, "queued", "验收未通过，准备纠偏", checkpoint=cp, evidence=merged,
            settle=True, no_progress=row["no_progress"] + 1, retry_count=0)

    @staticmethod
    def _verify(cycle: GoalCycle, criteria: list[dict[str, str]]) -> list[dict[str, Any]]:
        evidence = []
        for item in criteria:
            if str(item.get("kind") or "file") != "file":
                continue
            digest = ""
            passed = False
            try:
                path = cycle.path(item["path"], writing=True)
                digest = _file_digest(path)
                passed = bool(digest) and (not item.get("sha256") or digest == item["sha256"])
                if passed and item.get("contains"):
                    with path.open("rb") as handle:
                        data = handle.read(_MAX_FILE_BYTES + 1)
                    passed = _digest(data) == digest and item["contains"].encode("utf-8") in data
            except (OSError, ValueError, GoalConflict, KeyError):
                pass
            evidence.append({"path": item.get("path") or "", "passed": passed, "sha256": digest})
        return evidence

    def control(self, goal_id: str, action: str, version: Any = None,
                extras: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            row = self.store.get(goal_id)
            if not row:
                raise LookupError("目标不存在")
            status = row["status"]
            if version is not None and (type(version) is not int or version != row["version"]):
                raise GoalConflict("目标已更新，请重试")
            if action == "extend":
                if status in GOAL_TERMINAL or status == "cancelling":
                    raise GoalConflict("已结束或正在取消的目标不能追加额度")
                return self.extend(goal_id, {**(extras or {}), "version": row["version"]})
            if action == "pause" and status in {"queued", "running", "waiting_tool"}:
                if not self.store.transition(row, "pausing"):
                    raise GoalConflict("目标已更新，请重试")
                self.app.runs.cancel(row["run_id"], goal_control=True)
            elif action == "cancel" and status not in GOAL_TERMINAL and status != "cancelling":
                if not self.store.transition(row, "cancelling"):
                    raise GoalConflict("目标已更新，请重试")
                self.app.runs.cancel(row["run_id"], goal_control=True)
                self._cancel_goal_jobs(row)
            elif action == "resume" and status in {"paused", "blocked", "needs_user"}:
                if self.store.pending_actions(goal_id):
                    raise GoalConflict("上次写入尚未核对，请检查文件后结束目标")
                limits = row.get("limits") or GOAL_DEFAULT_LIMITS
                is_continuous = str(row.get("execution_policy") or "bounded") == "continuous"
                # 旧夹具通过收紧模块常量模拟小额度；continuous 模式下不因默认限额阻塞续跑。
                deadline_hit = row["deadline_at"] > 0 and row["deadline_at"] <= goal_now()
                model_hit = (
                    (not is_continuous and row["model_requests"] >= limits["model_requests"])
                    or (_MAX_MODEL_REQUESTS != GOAL_DEFAULT_LIMITS["model_requests"]
                        and row["model_requests"] >= _MAX_MODEL_REQUESTS)
                )
                tool_hit = (
                    (not is_continuous and row["tool_actions"] >= limits["tool_actions"])
                    or (_MAX_TOOL_ACTIONS != GOAL_DEFAULT_LIMITS["tool_actions"]
                        and row["tool_actions"] >= _MAX_TOOL_ACTIONS)
                )
                if deadline_hit or model_hit or tool_hit:
                    raise GoalConflict("目标额度已用完，请先追加额度或时限")
                if bool((extras or {}).get("rebaseline")):
                    self._rebaseline_snapshot(row)
                # 行级 wait_binding 由本调用清空；检查点里的旧交接标记一并清掉，
                # 续办轮的组闭合不得把它当成本轮的新等待。
                checkpoint = {**row["checkpoint"], "outcome": "", "repeat_error_count": 0, "prev_error_sig": "", "needs_user_stop": False}
                checkpoint.pop("wait_binding", None)
                self.store.transition(row, "queued", checkpoint=checkpoint,
                                      no_progress=0, wait_binding={}, stop_code="")
            elif action == "accept" and status == "awaiting_review":
                self.store.transition(row, "completed", "你已确认完成", settle=True)
            else:
                raise GoalConflict("目标当前状态不支持此操作")
            self.start()
            return self.public(self.store.get(goal_id))

    def pause_run(self, run_id: str, conversation_id: str) -> bool:
        row = self.store.latest(conversation_id)
        if not row or row["run_id"] != run_id or row["status"] not in {"queued", "running"}:
            return False
        self.control(row["id"], "pause")
        return True

    def baseline(self, conversation_id: str) -> dict[str, Any]:
        return execution_fingerprint(self.app, conversation_id)
