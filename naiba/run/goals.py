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
        self.resume_view = self.checkpoint.get("messages") or []
        self.actions = 0
        self.writes = 0
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
        if row["deadline_at"] <= goal_now():
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
            if not verify_sources_current({"sources": row["sources"]}, self.snapshot["workspace_dir"],
                                          self.snapshot.get("attachments") or []):
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
        self.checkpoint.update({"messages": list(messages), "outcome": outcome, "reason": reason,
                                "step": self.step, "writes": self.writes, "owner_run_id": self.row["run_id"],
                                "stop_code": self.stop_code})
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
        result = compaction.request_summary(self.service.app, region, profile=profile, settings=settings,
            cancel_event=self.cancel_event, before_request_send=self.before_send,
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
        outside_attachment = False
        if not candidate.is_relative_to(workspace):
            uploads = {Path(str(item.get("path") or "")).resolve() for item in self.snapshot.get("attachments", [])
                       if isinstance(item, dict) and str(item.get("path") or "")}
            if writing or candidate not in uploads or not candidate.is_file():
                raise GoalConflict("GOAL 不自动访问工作区外路径；只读附件必须来自本次目标")
            outside_attachment = True
        rel = candidate.relative_to(workspace) if not outside_attachment else Path(candidate.name)
        if any(part.casefold() in _SENSITIVE_DIRS or part.casefold().startswith(".env") for part in rel.parts):
            raise GoalConflict("GOAL 不自动访问配置或密钥路径")
        data_dir = Path(self.service.app.paths.data_dir).resolve()
        if candidate.is_relative_to(data_dir) and not outside_attachment:
            raise GoalConflict("GOAL 不能访问应用数据目录")
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
        if job_id not in allowed:
            return False, "只能查询本目标已绑定或启动时明确指定的后台任务"
        job = self.service.app.jobs.get(job_id, owner=self.row["conversation_id"])
        if not job or job.get("conversation_id") != self.row["conversation_id"]:
            self.stop_reason = "目标绑定的后台任务不存在或归属不符"
            return False, self.stop_reason
        if tool == "job_wait" and job["status"] in {"queued", "running", "waiting", "stopping", "cancelling"}:
            self.checkpoint["wait_binding"] = {"kind": "job", "target_id": job_id, "goal_id": self.row["id"],
                "run_id": self.row["run_id"], "state": "waiting"}
            return True, "已登记目标等待，后台任务未结束；不会重复提交或发送模型请求。"
        if tool == "job_output":
            return True, json.dumps(self.service.app.jobs.read(job_id, owner=self.row["conversation_id"]),
                                    ensure_ascii=False)[:6000]
        text, _ = summarize_job_result(job)
        return True, text

    def exercise(self, tool: str, arguments: dict[str, Any], active: list[dict[str, Any]],
                 executor: Any, context: dict[str, Any], source: str,
                 dispatch: Callable[[], tuple[bool, str]]) -> tuple[bool, str]:
        denial = self.guard()
        if denial:
            self.stop_reason = denial
            return False, denial
        if source == "fenced":
            self.stop_reason = "代码块里的工具动作需要人工确认，GOAL 未执行"
            return False, self.stop_reason
        if tool not in GOAL_TOOLS:
            return False, "这项工具不能由 GOAL 自动执行"
        if tool in {"job_status", "job_output", "job_wait"}:
            return self._job(tool, arguments)
        if tool == "reset_context":
            if executor._confirmation_reason(tool, arguments, active, context):
                self.stop_reason = "重置上下文需要人工确认，GOAL 未执行"
                return False, self.stop_reason
            self.checkpoint["rotate"] = True
            return True, "已请求在本组结束后换轮，原始记录保留。"
        if tool in {"list_directory", "search_files"} and arguments.get("recursive"):
            return False, "GOAL 首版不递归扫描工作区，请先定位到具体目录"
        try:
            path = self.path(arguments.get("path"), writing=tool in {"write_file", "edit_file"})
        except (GoalConflict, ValueError, OSError) as exc:
            self.stop_reason = str(exc) if isinstance(exc, GoalConflict) else "无法安全定位工具路径"
            return False, self.stop_reason
        arguments["path"] = str(path)
        if tool == "search_files" and not path.is_file():
            return False, "GOAL 首版只搜索明确指定的文件，不递归搜索目录"
        if tool == "list_directory" and ("**" in str(arguments.get("pattern") or "") or ".." in str(arguments.get("pattern") or "")):
            return False, "GOAL 列目录不接受递归或上级路径通配符"
        if tool in {"read_file", "search_files", "list_directory"}:
            success, result = dispatch()
            if str(result).startswith("NEED_CONFIRM:"):
                executor.reject_execute(str(result).split(":", 3)[1])
                self.stop_reason = "读取需要人工确认，GOAL 未执行"
                return False, self.stop_reason
            return success, result
        if getattr(executor, "permission_mode", "confirm") == "deny":
            self.stop_reason = "当前对话禁止写文件，GOAL 未执行"
            return False, self.stop_reason
        try:
            return self._write(tool, path, arguments, context)
        except (OSError, ValueError) as exc:
            if self.service.store.pending_actions(self.row["id"]):
                self.stop_reason = "文件写入结果需要核对，已停止后续动作"
            return False, str(exc) if isinstance(exc, ValueError) else "文件写入失败，将核对落盘状态"

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
        plan = preview(message, body.get("sources"), workspace, body.get("attachments") or [])
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
                if not verify_sources_current(plan, workspace, body.get("attachments") or []):
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
            spec = {"id": uuid.uuid4().hex, "request_id": request_id, "request_hash": digest,
                    "message": message, "duration_ms": _DURATION_MS, "criteria": criteria,
                    "job_ids": job_ids, "sources": sources, "stages": stages,
                    "manual_items": manual_items, "completion_policy": policy}
            self.app.runs.submit_chat(body, goal_spec=spec, allowed_tools_limit=list(GOAL_TOOLS))
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
            self.store.transition(row, "paused" if status == "pausing" else "cancelled", row["reason"], settle=True)
            return False
        if status == "running":
            if live:
                if row["deadline_at"] <= goal_now():
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
        rounds_limit = (limits["rounds"] if _MAX_ROUNDS == GOAL_DEFAULT_LIMITS["rounds"]
                        else min(limits["rounds"], _MAX_ROUNDS))
        if row["deadline_at"] <= goal_now() or row["generation"] >= rounds_limit and run_status != "queued":
            if run_status == "queued":
                self.app.runs.cancel(run_id, goal_control=True)
            self.store.transition(row, "blocked", "目标时间或续跑轮次额度已用完", settle=True,
                                  stop_code="deadline" if row["deadline_at"] <= goal_now() else "budget_rounds")
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
            self.store.transition(row, "needs_user", denial, settle=True)
            return False
        if run_status != "queued":
            snapshot["is_first_turn"] = False
            new_run = self.app.storage.create_run(cid, str(run["message"]), snapshot["agent"], snapshot,
                input_message_id=row["input_message_id"], origin_run_id=run_id,
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
                owner_session_id=row["conversation_id"], job_bind=job_bind,
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

    def _settle(self, row: dict[str, Any], run_status: str | None, snapshot: dict[str, Any]) -> None:
        cycle = GoalCycle(self, row, snapshot)
        recovered_write = False
        for action_id, receipt in self.store.pending_actions(row["id"]):
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
                    checkpoint={**cp, "outcome": "", "writes": 0}, retry_count=row["retry_count"] + 1)
                return
        if decision in {"blocked", "needs_user"}:
            self.store.transition(row, decision, str(cp.get("reason") or "目标暂时无法继续"), settle=True,
                                  stop_code=str(cp.get("stop_code") or ""))
            return
        if run_status != "completed":
            self.store.transition(row, "blocked", "本轮没有正常完成，请查看对话中的错误", settle=True,
                                  stop_code="error")
            return
        evidence = self._verify(cycle, row["criteria"])
        check_items = [item for item in row["criteria"] if str(item.get("kind") or "file") != "file"]
        if decision not in {"completed", "review"}:
            no_progress = 0 if current_round and cp.get("writes") else row["no_progress"] + 1
            if no_progress >= 2:
                self.store.transition(row, "blocked", "续跑多轮仍未取得可确认进展", evidence=evidence, settle=True)
                return
            self.store.transition(row, "queued", "准备继续目标", checkpoint=cp, evidence=evidence,
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
            elif action == "resume" and status in {"paused", "blocked", "needs_user"}:
                if self.store.pending_actions(goal_id):
                    raise GoalConflict("上次写入尚未核对，请检查文件后结束目标")
                limits = row.get("limits") or GOAL_DEFAULT_LIMITS
                # 旧夹具通过收紧模块常量模拟小额度；默认值时以持久限额为准。
                model_limit = (limits["model_requests"] if _MAX_MODEL_REQUESTS == GOAL_DEFAULT_LIMITS["model_requests"]
                               else min(limits["model_requests"], _MAX_MODEL_REQUESTS))
                tool_limit = (limits["tool_actions"] if _MAX_TOOL_ACTIONS == GOAL_DEFAULT_LIMITS["tool_actions"]
                              else min(limits["tool_actions"], _MAX_TOOL_ACTIONS))
                if row["deadline_at"] <= goal_now() or row["model_requests"] >= model_limit \
                        or row["tool_actions"] >= tool_limit:
                    raise GoalConflict("目标额度已用完，请先追加额度或时限")
                # 行级 wait_binding 由本调用清空；检查点里的旧交接标记一并清掉，
                # 续办轮的组闭合不得把它当成本轮的新等待。
                checkpoint = {**row["checkpoint"], "outcome": ""}
                checkpoint.pop("wait_binding", None)
                self.store.transition(row, "queued", checkpoint=checkpoint,
                                      no_progress=0, wait_binding={})
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
