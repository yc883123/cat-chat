"""Harness 级统一 Job Registry 与持续检查/唤醒。

继续使用现有 ``background_tasks`` / ``run_events`` 表（字段已通过增量迁移补齐），
不新建平行任务表。提供计划规定的统一接口：

    start(spec, owner) -> job_id
    get(job_id, owner=None) -> snapshot
    list(owner=None, active_only=False) -> snapshots
    read(job_id, cursor=0, owner=None) -> output
    wait(job_id, timeout, owner=None) -> snapshot
    cancel(job_id, owner=None, reason=None) -> snapshot

状态固定为：queued / running / waiting / stopping / completed / failed /
cancelled / interrupted。

Job Worker 支持：提交一次任务、按固定间隔或指数退避检查、把检查结果写入事件、
识别完成/失败/超时、完成后通过 Condition 唤醒等待中的 Agent、输出增量进度。

ComfyUI 生成任务使用专用 Worker（见 ``_run_comfyui``）。
"""
from __future__ import annotations

from naiba.core.contracts import AppContext

import hashlib
import json
import logging
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from naiba import net as net_io
from naiba.core.exceptions import TaskCancelled
from naiba.events import EventBus
from naiba.tools.providers.core import POWERSHELL_UTF8_PREFIX

logger = logging.getLogger("naiba.jobs")

JOB_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
JOB_ACTIVE = {"queued", "running", "waiting", "stopping"}

# ``create_run`` 在没有 label 时写入 ``background_tasks.message`` 的占位名（形如 ``Job(comfyui)``）。
# 它对用户不可读，前端 ``taskDisplayTitle`` 会把它换成类型名——因此恢复 Job 时也不得把它
# 当成"原名"沿用，否则会得到 ``Job(comfyui)（恢复）`` 这种绕过了前端兜底判定的怪名字。
_PLACEHOLDER_JOB_LABEL_RE = re.compile(r"^Job\([a-z_]+\)$")

# 停止超时兜底：worker 最长的"不看取消信号"窗口是 _run_check 的退避上限 60 秒，
# 所以给它 90 秒。到点仍是 stopping 说明 worker 没响应取消（卡在网络等待里），
# 继续挂着会永久占住活动名额（stopping 属于 storage.ACTIVE_TASK_STATUSES），
# 而一个卡住的子任务会让整条会话被判成"回复进行中"、挡住三个救援入口。
STOP_WATCHDOG_SECONDS = 90.0

# 副作用/不可重试的检查型错误归类（MCP、HTTP、Job 查询）用于 Agent Loop 重试决策
RETRYABLE_ERROR_PREFIXES = (
    "MCP",
    "HTTP 5",
    "HTTP 408",
    "HTTP 429",
    "urllib",
    "Timeout",
    "连接",
    "超时",
)


@dataclass
class JobSpec:
    kind: str
    conversation_id: str
    params: dict[str, Any] = field(default_factory=dict)
    label: str = ""
    parent_job_id: str | None = None
    owner_session_id: str = ""
    resumable: bool = False
    checkpoint: dict[str, Any] | None = None


@dataclass
class CheckSpec:
    interval_seconds: float = 5.0
    timeout_seconds: float = 600.0
    max_attempts: int = 60
    backoff_seconds: float = 0.0
    check_kind: str = "http_poll"


# 提交失败原因写入 job error / result.errors 时的长度上限：够定位节点问题，
# 又不会把一条 400 响应（可能含整段工作流回显）原样灌进任务记录。
_COMFYUI_REJECT_MAX_CHARS = 600


def _summarize_comfyui_reject(status: int, body: str) -> str:
    """把 ComfyUI 拒绝提交的响应压成一句可读原因（node_errors 优先）。

    400 响应形如 {"error": {...}, "node_errors": {"20": {"class_type": "Seed (rgthree)",
    "errors": [{"message": "...", "details": "seed"}]}}}。模型要靠这段文字自诊，
    所以必须带上节点号、节点类型与具体校验错误，而不是一句笼统的「提交失败」。
    """
    text = (body or "").strip()
    try:
        parsed = json.loads(text)
    except Exception:
        return f"HTTP {status} {text[:_COMFYUI_REJECT_MAX_CHARS]}"
    parts: list[str] = []
    err = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(err, dict) and err.get("message"):
        parts.append(str(err["message"]))
    node_errors = parsed.get("node_errors") if isinstance(parsed, dict) else None
    if isinstance(node_errors, dict):
        for node_id, item in list(node_errors.items())[:3]:
            class_type = ""
            reasons: list[str] = []
            if isinstance(item, dict):
                class_type = str(item.get("class_type") or "")
                for node_err in (item.get("errors") or [])[:3]:
                    if not isinstance(node_err, dict):
                        continue
                    msg = str(node_err.get("message") or "")
                    details = str(node_err.get("details") or "")
                    if details and details != msg:
                        msg = f"{msg}（{details}）"
                    if msg:
                        reasons.append(msg)
            head = f"节点 {node_id}" + (f"（{class_type}）" if class_type else "")
            parts.append(head + ("：" + "；".join(reasons) if reasons else ""))
    if not parts:
        return f"HTTP {status} {text[:_COMFYUI_REJECT_MAX_CHARS]}"
    return f"HTTP {status} " + "；".join(parts)[:_COMFYUI_REJECT_MAX_CHARS]


# 子代理事件 → 任务面板：日志行 / 当前步骤的长度口径。
# 300 与前端任务详情的 300 字截断口径一致（taskDetailRows）；步骤行更短，避免把
# 一整句状态文案塞进「当前步骤」这一列。
_SUBAGENT_LOG_MAX_CHARS = 300
_SUBAGENT_STEP_MAX_CHARS = 120


def _truncate_note(text: str, limit: int) -> str:
    """压成单行并按上限截断（省略号自述，不静默丢内容）。"""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: max(1, limit - 1)] + "…"


def _subagent_log_line(payload: dict[str, Any]) -> str:
    """子代理事件 → 任务日志一行；返回空串表示该事件不落日志。

    子代理只发 agent 域事件（status / tool_requested / tool_result / run_failed …），
    其中**没有一个带 ``line`` 字段**，而前端任务日志只认 ``line``
    （``public/js/08-conversations.js`` 的 ``String(event?.line ?? '')``），于是面板恒显示
    「暂无输出」。这里把 agent 事件翻译成 Job 域的 ``job_log`` 行，前端与 ``job_output``
    工具（读 line/message/content）同时受益。

    红线：``tool_result`` 的 result 内容可能含图片 base64 / 大段文本，**成功时一律不进日志**；
    失败时 result 就是错误文案，截断 300 字后写进日志（用户与模型都要靠它定位）。
    """
    kind = str(payload.get("type") or "")
    if kind == "status":
        return _truncate_note(str(payload.get("message") or ""), _SUBAGENT_LOG_MAX_CHARS)
    if kind == "tool_requested":
        return f"→ 调用工具 {str(payload.get('tool') or '工具').strip() or '工具'}"
    if kind == "tool_result":
        tool = str(payload.get("tool") or "工具").strip() or "工具"
        if bool(payload.get("success")):
            return f"✓ 工具 {tool} 完毕"
        return _truncate_note(
            f"✗ 工具 {tool} 失败：{payload.get('result') or ''}", _SUBAGENT_LOG_MAX_CHARS
        )
    if kind == "run_failed":
        return _truncate_note(f"错误：{payload.get('error') or ''}", _SUBAGENT_LOG_MAX_CHARS)
    # reasoning / usage / skills / tool_started / skill_warning … 太碎或纯诊断，不落日志。
    return ""


def _subagent_current_step(payload: dict[str, Any]) -> str:
    """子代理事件 → ``background_tasks.current_step``；返回空串表示不动步骤。"""
    kind = str(payload.get("type") or "")
    if kind == "status":
        return _truncate_note(str(payload.get("message") or ""), _SUBAGENT_STEP_MAX_CHARS)
    if kind == "tool_requested":
        tool = str(payload.get("tool") or "工具").strip() or "工具"
        return _truncate_note(f"正在执行 {tool}", _SUBAGENT_STEP_MAX_CHARS)
    if kind == "tool_result":
        return "推理中"
    # run_failed 不改步骤：终态由 _finish 统一写，避免面板先闪一个中途文案。
    return ""


def _subagent_event_sink(registry: "JobRegistry", job_id: str) -> Callable[[dict[str, Any]], None]:
    """构造子代理事件接收器：翻译成 Job 日志/步骤，**并原样透传**原始事件。

    原始事件照旧落 ``run_events``：``NAIBA_STRICT_EVENTS`` 契约校验、``job_output``
    的 message/content 读取、以及历史取证都依赖它，翻译只是**追加**不是替换。
    """

    def sink(payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        line = _subagent_log_line(payload)
        if line:
            registry._emit(job_id, {"type": "job_log", "line": line})
        step = _subagent_current_step(payload)
        if step:
            # _set_status 自带 cancel_requested 闸门：取消后不会把面板回写成「运行中」。
            registry._set_status(job_id, "running", current_step=step)
        registry._emit(job_id, payload)

    return sink


class JobRegistry:
    def __init__(self, app: AppContext):
        self.app = app
        self.bus = getattr(app, "event_bus", None) or EventBus(app)
        self._lock = threading.RLock()
        self._cancel: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        # 由 app 接线注入：运行子 Agent 的函数 (job_id, spec, cancel, sink) -> None
        self.agent_runner: Callable[[str, JobSpec, threading.Event, Callable[[dict], None]], None] | None = None
        self._workers: dict[str, Callable[[str, JobSpec, threading.Event], None]] = {
            "shell": self._run_shell,
            "check": self._run_check,
            "http_poll": self._run_check,
            "comfyui": self._run_comfyui,
            "subagent": self._run_subagent,
        }

    # ---- 内部工具 ----
    def _condition(self, job_id: str) -> threading.Condition:
        """唤醒条件（实现见 naiba/events.py EventBus.ensure：run/job 共用条件池）。"""
        return self.bus.ensure(job_id)

    def _now(self) -> int:
        return int(time.time() * 1000)

    def _emit(self, job_id: str, payload: dict[str, Any]) -> None:
        """事件发射（实现见 naiba/events.py EventBus）：单点写入 + 唤醒。
        job 通道保持容错语义（记录违规告警，事件写失败仅打堆栈，不打断 worker）。"""
        try:
            self.bus.emit(job_id, payload, raise_on_error=False)
        except Exception:
            logger.exception("Job 事件写入失败：job=%s", job_id)

    def _snapshot(self, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": job.get("id"),
            "kind": job.get("kind"),
            "conversation_id": job.get("conversation_id"),
            "parent_job_id": job.get("parent_job_id") or "",
            "owner_session_id": job.get("owner_session_id") or "",
            "status": job.get("status"),
            "progress": float(job.get("progress") or 0),
            "current_step": str(job.get("current_step") or ""),
            "attempt": int(job.get("attempt") or 0),
            "checkpoint": job.get("checkpoint") if isinstance(job.get("checkpoint"), dict) else {},
            "result": job.get("result") if isinstance(job.get("result"), dict) else {},
            "error": job.get("error") or "",
            "created_at": job.get("created_at"),
            "started_at": job.get("started_at"),
            "updated_at": job.get("updated_at"),
            "finished_at": job.get("finished_at"),
        }

    def _set_status(self, job_id: str, status: str, **fields: Any) -> None:
        current = self.app.storage.get_background_task(job_id)
        if current and current.get("cancel_requested") and status in JOB_ACTIVE:
            return
        self.app.storage.update_job(job_id, status=status, **fields)
        self._emit(job_id, {"type": "job_status", "status": status, **fields})

    def _finish(self, job_id: str, status: str, error: str = "", result: dict[str, Any] | None = None, detail: dict[str, Any] | None = None) -> None:
        current = self.app.storage.get_background_task(job_id)
        if current and current.get("cancel_requested") and status != "cancelled":
            status = "cancelled"
            error = error or "用户取消"
        self.app.storage.update_job(
            job_id, status=status, error=error, result=result or {}, detail=detail, finished=True
        )
        self._emit(job_id, {"type": "job_finished", "status": status, "error": error, "result": result or {}})
        # 异步产物写回：Job 在工具返回之后才产出的媒体挂回发起它的助手消息
        # （comfyui_batch wait=false / run_in_background 的主路径；写回失败不影响终态）。
        self._write_back_media(job_id)
        condition = self._condition(job_id)
        with condition:
            condition.notify_all()

    def _write_back_media(self, job_id: str) -> None:
        """终态后把 Job 产物挂回发起它的助手消息（见 storage/job_media.py）。"""
        writer = getattr(self.app, "job_media_writer", None)
        if writer is None:
            return
        job = self.app.storage.get_background_task(job_id)
        if not job:
            return
        try:
            written = writer.write_back(job)
        except Exception:  # noqa: BLE001 - 写回失败必须记录且不得影响 Job 终态
            logger.exception("Job 产物写回失败：job=%s", job_id)
            return
        if written:
            logger.info(
                "Job 产物已写回消息：job=%s message=%s added=%s",
                job_id, written["message_id"], written["added"],
            )

    # ---- 统一接口 ----
    def start(self, spec: JobSpec, owner: str | None = None) -> str:
        owner = owner or spec.owner_session_id or spec.conversation_id
        # 父任务取消闸门：父已被取消（或正在停止）时拒绝创建。取消级联只抓「取消
        # 那一刻」已存在的子任务；没有这道闸门，没及时响应取消信号的 worker 之后
        # 仍能派生新 Job（实测：subagent 被取消后仍创建 comfyui Job 并跑完 6/6）。
        # 注意不能拦「父已终态」：回答完成后派生/恢复的后台 Job 都以终态 Run 为
        # 归属锚点，resume()/retry() 也要在终态父下重建 Job。
        parent_id = str(spec.parent_job_id or "")
        if parent_id:
            parent = self.app.storage.get_background_task(parent_id)
            if parent:
                parent_status = str(parent.get("status") or "")
                if parent.get("cancel_requested") or parent_status in {"stopping", "cancelling", "cancelled"}:
                    raise TaskCancelled("父任务已取消，不再创建新任务")
        snapshot = {
            "job_spec": {"kind": spec.kind, "resumable": spec.resumable},
            "params": spec.params,
            "label": spec.label,
        }
        run = self.app.storage.create_run(
            spec.conversation_id,
            spec.label or f"Job({spec.kind})",
            {"id": "", "name": "Job", "system_prompt": "", "skill_ids": []},
            snapshot,
            kind=spec.kind,
            parent_job_id=spec.parent_job_id or "",
            owner_session_id=owner,
        )
        job_id = str(run["id"])
        self.app.storage.update_job(
            job_id,
            status="queued",
            progress=0,
            attempt=0,
            current_step="已入队",
            checkpoint=spec.checkpoint or {},
        )
        self._emit(job_id, {"type": "job_status", "status": "queued", "kind": spec.kind})
        cancel = threading.Event()
        with self._lock:
            self._cancel[job_id] = cancel
            self._condition(job_id)
        worker = self._workers.get(spec.kind, self._run_unknown)
        thread = threading.Thread(
            target=worker, args=(job_id, spec, cancel), name=f"job-{spec.kind}-{job_id[:8]}", daemon=True
        )
        with self._lock:
            self._threads[job_id] = thread
        thread.start()
        return job_id

    def get(self, job_id: str, owner: str | None = None) -> dict[str, Any] | None:
        job = self.app.storage.get_background_task(job_id)
        if not job:
            return None
        if owner and job.get("owner_session_id") != owner:
            return None
        return self._snapshot(job)

    def list(self, owner: str | None = None, active_only: bool = False) -> list[dict[str, Any]]:
        jobs = self.app.storage.list_background_tasks("", active_only)
        if owner:
            jobs = [j for j in jobs if j.get("owner_session_id") == owner]
        return [self._snapshot(j) for j in jobs]

    def read(self, job_id: str, cursor: int = 0, owner: str | None = None) -> dict[str, Any]:
        if not self.get(job_id, owner):
            return {"events": [], "cursor": cursor}
        events = self.app.storage.list_run_events(job_id, cursor)
        new_cursor = events[-1]["sequence"] if events else cursor
        return {"events": events, "cursor": new_cursor}

    def wait(self, job_id: str, timeout: float, owner: str | None = None) -> dict[str, Any] | None:
        job = self.get(job_id, owner)
        if not job:
            raise LookupError("Job 不存在或无权访问")
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            job = self.get(job_id, owner)
            if job and job["status"] in JOB_TERMINAL:
                return job
            condition = self._condition(job_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            with condition:
                condition.wait(timeout=min(remaining, 1.0))
        return self.get(job_id, owner)

    def cancel(self, job_id: str, owner: str | None = None, reason: str | None = None) -> dict[str, Any] | None:
        job = self.get(job_id, owner)
        if not job:
            return None
        if job["status"] in JOB_TERMINAL:
            return job
        event: threading.Event | None = None
        with self._lock:
            event = self._cancel.get(job_id)
        if event:
            event.set()
        # cancel_requested 是"停止请求已下发"的持久标记：_set_status 靠它冻结状态，
        # 否则 worker 的下一次状态更新会把 stopping 打回 running（用户看到的是"停不下来"）。
        snapshot = self._snapshot(
            self.app.storage.update_job(
                job_id,
                status="stopping",
                current_step="正在停止",
                cancel_requested=True,
                detail={"message": reason or "用户取消"},
            )
            or {}
        )
        # 父任务取消时递归取消所有子任务（级联）
        self._cancel_children(job_id, owner, reason)
        # 只有登记过取消事件的才是 JobRegistry 自己起的 worker；没登记的（如被误传的
        # 顶层 Run）不设兜底，免得把一条仍在跑的回答强行标成已取消。
        if event is not None:
            self._schedule_stop_watchdog(job_id)
        return snapshot

    def _schedule_stop_watchdog(self, job_id: str, timeout: float | None = None) -> None:
        """停止超时兜底：worker 没响应取消信号时强制收尾（见 STOP_WATCHDOG_SECONDS 注释）。

        ``timeout`` 为 None 时按调用时刻读取模块常量——这样测试与后续调参都只改一处，
        不会因为默认参数在 def 期被求值而失效。
        """
        delay = STOP_WATCHDOG_SECONDS if timeout is None else timeout

        def watchdog() -> None:
            try:
                time.sleep(delay)
                current = self.app.storage.get_background_task(job_id)
                if not current or str(current.get("status") or "") != "stopping":
                    return
                logger.warning(
                    "Job 停止超时，强制收尾：job=%s kind=%s step=%s",
                    job_id, current.get("kind"), current.get("current_step"),
                )
                detail = current.get("detail") if isinstance(current.get("detail"), dict) else {}
                detail = dict(detail)
                detail["message"] = "停止超时，已强制结束"
                self.app.storage.update_job(
                    job_id,
                    status="cancelled",
                    error="停止超时，已强制结束",
                    detail=detail,
                    finished=True,
                )
                self._emit(
                    job_id,
                    {"type": "job_finished", "status": "cancelled", "error": "停止超时，已强制结束"},
                )
                self._forget_job_thread(job_id)
                condition = self._condition(job_id)
                with condition:
                    condition.notify_all()
            except Exception:  # noqa: BLE001 - 兜底线程不得把异常抛到线程外
                logger.exception("Job 停止超时兜底失败：job=%s", job_id)

        threading.Thread(
            target=watchdog, name=f"job-stop-watchdog-{job_id[:8]}", daemon=True
        ).start()

    def _forget_job_thread(self, job_id: str) -> None:
        """摘掉该 job 的取消事件与线程句柄：worker 已不再消费它们，留着只会让
        ``shutdown()`` 在一个永远不会退出的线程上白等到超时。"""
        with self._lock:
            self._cancel.pop(job_id, None)
            self._threads.pop(job_id, None)

    def _cancel_children(self, parent_id: str, owner: str | None, reason: str | None) -> None:
        children = [j for j in self.list(owner=owner) if str(j.get("parent_job_id") or "") == parent_id]
        for child in children:
            self.cancel(str(child["id"]), owner=owner, reason=reason or "父任务取消")

    def resume(self, job_id: str, owner: str | None = None, extra_checkpoint: dict[str, Any] | None = None) -> str | None:
        """从 checkpoint 恢复一个可恢复 Job（仅声明 resumable 且非副作用未知的任务）。"""
        job = self.get(job_id, owner)
        if not job:
            return None
        if not job.get("checkpoint"):
            return None
        checkpoint = dict(job["checkpoint"])
        if extra_checkpoint:
            checkpoint.update(extra_checkpoint)
        # Job parameters live in the durable run snapshot.  Do not rely on
        # the final result (which intentionally contains only outputs), or a
        # resumed ComfyUI batch would lose its workflow list after restart.
        persisted = self.app.storage.get_run_snapshot(job_id) or {}
        persisted_params = persisted.get("params") if isinstance(persisted, dict) else {}
        if not isinstance(persisted_params, dict):
            persisted_params = {}
        if not persisted_params:
            persisted_params = dict(job.get("result", {}).get("params", {})) if isinstance(job.get("result"), dict) else {}
        persisted_params = dict(persisted_params)
        # 记录来源 Job：resume/retry 产生的新 Job 可追溯“由谁恢复”，跨对话查询也能核对。
        persisted_params["_resumed_from"] = job_id
        # 任务名绝不能取 current_step：那是**进度文字**（「完成 8/10」「提交第 3/10 段」），
        # 而 background_tasks.message 直接顶在任务面板的标题位。旧实现把它当 label 写进去，
        # 于是同一组里并排两条行变成「ComfyUI 批量生成」与「完成 8/10」——用户看到的是一句
        # 没有主语、也看不出是恢复件的进度（2026-09-20 实测事故，见维护说明 §九）。
        # 原名按可信度取：①发起时写进 run snapshot 的 JobSpec.label；②源 Job 已落库的 message。
        # 注意 get()/``_snapshot()`` 刻意不带 message（它是展示字段，不在 Job 快照契约里），
        # 所以第二路必须回库取，否则会静默丢名 → 标题退化成 Job(kind)。
        # 两者都空、或只有一个占位名（Job(comfyui)）时留空，由 create_run 写占位名、
        # 前端 taskDisplayTitle 再回退成类型名显示；绝不把进度文字顶上去。
        persisted_label = str(persisted.get("label") or "") if isinstance(persisted, dict) else ""
        stored = self.app.storage.get_background_task(job_id) or {}
        base_label = (persisted_label or str(stored.get("message") or "")).strip()
        if _PLACEHOLDER_JOB_LABEL_RE.match(base_label):
            base_label = ""
        if base_label.endswith("（恢复）"):
            label = base_label  # 连续恢复不叠加后缀（否则「X（恢复）（恢复）」）
        elif base_label:
            label = f"{base_label}（恢复）"
        else:
            label = ""
        spec = JobSpec(
            kind=job["kind"],
            conversation_id=job["conversation_id"],
            params=persisted_params,
            label=label,
            parent_job_id=job["parent_job_id"] or None,
            owner_session_id=job["owner_session_id"],
            resumable=True,
            checkpoint=checkpoint,
        )
        try:
            new_id = self.start(spec, owner=job["owner_session_id"])
        except TaskCancelled as exc:
            # 父任务已取消：显式 resume/retry 也不应复活用户取消掉的工作。
            raise ValueError(str(exc)) from exc
        if new_id:
            # 在新 Job 的增量输出里写入一条来源说明，job_output 与记录均可见。
            self._emit(new_id, {"type": "output", "line": f"（本 Job 由 Job {job_id} 恢复/重试）"})
        return new_id

    def retry(self, job_id: str, owner: str | None = None) -> str | None:
        """用户确认后重试失败步骤（等价于从 checkpoint 恢复，但显式标记 retry_step）。

        仅允许在已结束的非成功状态下调用；要求 Job 带有 checkpoint 且可恢复。
        """
        job = self.get(job_id, owner)
        if not job:
            return None
        if job["status"] in ("running", "waiting", "stopping", "queued"):
            raise ValueError("Job 仍在运行中，无法重试")
        if job["status"] == "completed":
            raise ValueError("Job 已成功完成，无需重试")
        if not job.get("checkpoint"):
            raise ValueError("Job 没有可用 checkpoint，无法重试")
        checkpoint = dict(job["checkpoint"])
        checkpoint["retry_step"] = True
        try:
            return self.resume(job_id, owner=owner, extra_checkpoint=checkpoint)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"重试失败：{exc}") from exc

    def resume_interrupted(self) -> list[str]:
        """Resume durable, explicitly resumable jobs interrupted by restart.

        幂等 + 去重（历史事故防线）：恢复后必须把**源 Job** 标记为「已由某新 Job 接续」，
        否则它下一轮启动仍是 interrupted、会被再恢复一次，Job 数随重启次数成倍增长
        （1→2→4→8…，实测曾把同一批 8 段生成放大成每对话 13 份）。
        另外，checkpoint 完全相同的中断 Job 视为同一批任务的重复副本，只恢复最新的一条。
        """
        resumed: list[str] = []
        seen_signatures: set[str] = set()
        # list_background_tasks 按 created_at DESC 返回（新→旧），每组第一条即最新。
        for job in self.app.storage.list_background_tasks("", active_only=False, limit=200):
            if str(job.get("status") or "") != "interrupted":
                continue
            job_id = str(job.get("id") or "")
            if not job.get("checkpoint"):
                continue
            if self._resume_settled(job):
                continue
            snapshot = self.app.storage.get_run_snapshot(job_id) or {}
            job_spec = snapshot.get("job_spec") if isinstance(snapshot, dict) else {}
            if not isinstance(job_spec, dict) or not bool(job_spec.get("resumable")):
                continue
            # Only deterministic workers can safely resume automatically.
            if str(job.get("kind") or "") not in {"comfyui", "check", "http_poll"}:
                continue
            signature = self._resume_signature(job)
            if signature in seen_signatures:
                self._mark_superseded(job_id, reason="同一批中断任务已由较新的 Job 接续，未重复恢复")
                continue
            seen_signatures.add(signature)
            try:
                new_id = self.resume(job_id, owner=str(job.get("owner_session_id") or "") or None)
            except TaskCancelled:
                # 父任务在停止前已被用户取消：恢复它等于复活用户明确不要的工作。
                # 打上接续标记，避免每轮启动都重复尝试并刷错误日志。
                self._mark_superseded(job_id, reason="父任务已取消，不再恢复")
                continue
            except Exception:
                logger.exception("恢复中断 Job 失败：job=%s", job_id)
                continue
            if new_id:
                resumed.append(new_id)
                self._mark_superseded(job_id, new_id=new_id)
        return resumed

    @staticmethod
    def _resume_settled(job: dict[str, Any]) -> bool:
        """该中断 Job 是否已被处理过（接续成新 Job，或被判定为重复副本）。

        两种都要跳过，否则重复副本会在下一轮启动里再挑出一条来恢复，
        让同一批任务被反复「复活」。显式 retry()/resume() 不受此标记影响。
        """
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        return bool(str(result.get("resumed_into") or "") or str(result.get("resume_skipped") or ""))

    @staticmethod
    def _resume_signature(job: dict[str, Any]) -> str:
        """同一批任务的指纹：checkpoint 相同即视为重复副本。"""
        checkpoint = job.get("checkpoint") if isinstance(job.get("checkpoint"), dict) else {}
        payload = json.dumps(checkpoint, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def _mark_superseded(self, job_id: str, new_id: str = "", reason: str = "") -> None:
        """给中断 Job 打上持久标记，使它不再被后续启动重复恢复。

        ``new_id`` 非空 → 记录「已由哪个新 Job 接续」；为空 → 记录「被判定为重复副本」。
        两种标记都必须非空，否则该 Job 下一轮又会被当成待恢复对象。
        """
        job = self.app.storage.get_background_task(job_id)
        if not job:
            return
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        result = dict(result)
        if new_id:
            result["resumed_into"] = new_id
        else:
            result["resume_skipped"] = reason or "同一批中断任务已由较新的 Job 接续"
        if reason:
            result["superseded_reason"] = reason
        self.app.storage.update_job(job_id, result=result)

    # ---- 通用 Worker 框架 ----
    def _run_unknown(self, job_id: str, spec: JobSpec, cancel: threading.Event) -> None:
        self._set_status(job_id, "running", current_step=f"未知 Job 类型：{spec.kind}")
        self._finish(job_id, "failed", error=f"不支持的 Job 类型：{spec.kind}")

    def _run_subagent(self, job_id: str, spec: JobSpec, cancel: threading.Event) -> None:
        if not self.agent_runner:
            self._set_status(job_id, "running", current_step="子任务运行器未配置")
            self._finish(job_id, "failed", error="子任务运行器未配置")
            return
        self._set_status(job_id, "running", current_step="已启动")
        try:
            # 事件接收器必须做「agent 事件 → Job 日志/步骤」的翻译：子代理事件一个都不带
            # ``line``，直通会让任务面板全程「暂无输出 + 当前步骤停在已启动」。
            self.agent_runner(job_id, spec, cancel, _subagent_event_sink(self, job_id))
            job = self.get(job_id)
            if job and job["status"] not in JOB_TERMINAL:
                self._finish(job_id, "completed", result={"subagent_job_id": job_id})
        except Exception as exc:
            logger.exception("子 Agent Job 执行失败：job=%s", job_id)
            self._finish(job_id, "failed", error=str(exc))
        finally:
            # 兜底：子 Agent 被取消/失败而退出时，它派生的子 Job 不得继续跑。
            # 取消级联（cancel → _cancel_children）只抓「取消那一刻」已存在的子
            # 任务；worker 晚于取消创建的子 Job 会逃逸，在这里收网。
            job = self.get(job_id)
            if job and (job.get("cancel_requested") or str(job.get("status") or "") in {"cancelled", "failed"}):
                self._cancel_children(job_id, owner=None, reason="父任务已结束")

    def _run_shell(self, job_id: str, spec: JobSpec, cancel: threading.Event) -> None:
        params = spec.params or {}
        command = str(params.get("command") or "").strip()
        cwd = str(params.get("cwd") or "").strip() or str(self.app.config.data.get("workspace_dir") or ".")
        timeout = min(max(int(params.get("timeout", 120)), 1), 900)
        if not command:
            self._set_status(job_id, "running", current_step="缺少 command 参数")
            self._finish(job_id, "failed", error="shell Job 缺少 command 参数")
            return
        self._set_status(job_id, "running", current_step="执行命令")
        proc = None
        try:
            proc = subprocess.Popen(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                    # 与 pwsh 工具一致：强制 UTF-8 输出控制台与管道（否则中文路径/输出
                    # 按 locale 编码，父进程按 UTF-8 解码必乱码）。
                    POWERSHELL_UTF8_PREFIX + "\n" + command,
                ],
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            output: list[str] = []
            start = time.monotonic()
            while True:
                if cancel.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                    self._finish(job_id, "cancelled", error="用户取消", result={"exit_code": -1, "output": "\n".join(output)})
                    return
                line = proc.stdout.readline() if proc.stdout else ""
                if line == "" and proc.poll() is not None:
                    break
                if line:
                    output.append(line.rstrip("\n"))
                    self._emit(job_id, {"type": "job_log", "line": line.rstrip("\n")})
                elapsed = time.monotonic() - start
                self.app.storage.update_job(job_id, progress=min(99.0, elapsed / timeout * 100))
                if elapsed >= timeout:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                    self._finish(job_id, "failed", error="shell Job 超时", result={"exit_code": -1, "output": "\n".join(output)})
                    return
            rc = proc.wait()
            text = "\n".join(output)
            self._set_status(job_id, "running", progress=100, current_step="命令结束")
            self._finish(
                job_id,
                "completed" if rc == 0 else "failed",
                error="" if rc == 0 else f"exit_code={rc}",
                result={"exit_code": rc, "output": text[:50000]},
            )
        except Exception as exc:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._finish(job_id, "failed", error=f"{type(exc).__name__}: {exc}")

    def _run_check(self, job_id: str, spec: JobSpec, cancel: threading.Event) -> None:
        """通用 HTTP 提交 + 轮询检查 Worker（持续检查/唤醒）。

        params:
          submit: {"type":"http","url":...,"method":...,"json":...,"headers":...}
                  或 {"type":"mcp","server":...,"tool":...,"arguments":...}
          poll:   {"url":..., "handle_path":"data.id", "status_path":"data.status",
                   "done":["completed"], "fail":["failed"]}
          check:  {interval_seconds, timeout_seconds, max_attempts, backoff_seconds, check_kind}
        """
        params = spec.params or {}
        check = CheckSpec(**{k: v for k, v in (params.get("check") or {}).items() if k in CheckSpec.__dataclass_fields__})
        submit = params.get("submit") or {}
        poll = params.get("poll") or {}
        attempt = 0
        self._set_status(job_id, "running", current_step="提交任务", attempt=attempt)
        handle = self._submit(check, submit)
        if handle is None:
            self._finish(job_id, "failed", error="Job 提交失败")
            return
        self.app.storage.update_job(job_id, checkpoint={"handle": handle})
        self._emit(job_id, {"type": "job_check", "phase": "submitted", "handle": handle, "attempt": attempt})
        deadline = time.monotonic() + check.timeout_seconds
        backoff = 0.0
        while True:
            if cancel.is_set():
                self._finish(job_id, "cancelled", error="用户取消", result={"handle": handle})
                return
            if time.monotonic() > deadline or attempt >= check.max_attempts:
                self._finish(job_id, "failed", error="Job 轮询超时", result={"handle": handle})
                return
            time.sleep(check.interval_seconds + backoff)
            attempt += 1
            self.app.storage.update_job(job_id, attempt=attempt, current_step=f"第 {attempt} 次检查")
            status = self._poll_status(poll, handle)
            self._emit(job_id, {"type": "job_check", "phase": "polling", "attempt": attempt, "status": status, "next_check_in": check.interval_seconds + backoff})
            done = poll.get("done", ["completed"])
            fail = poll.get("fail", ["failed"])
            if status in done:
                self._set_status(job_id, "running", progress=100, current_step="完成")
                self._finish(job_id, "completed", result={"handle": handle, "status": status})
                return
            if status in fail:
                self._finish(job_id, "failed", error=f"Job 失败：{status}", result={"handle": handle, "status": status})
                return
            if check.backoff_seconds:
                backoff = min(check.backoff_seconds * (2 ** min(attempt, 5)), 60.0)

    def _submit(self, check: CheckSpec, submit: dict[str, Any]) -> Any | None:
        try:
            if submit.get("type") == "mcp":
                ok, out = self.app.executor.mcp_registry.call(
                    str(submit.get("server") or ""), str(submit.get("tool") or ""), submit.get("arguments") or {}
                )
                if not ok:
                    return None
                return out
            url = str(submit.get("url") or "")
            method = str(submit.get("method") or "POST").upper()
            data = submit.get("json")
            headers = dict(submit.get("headers") or {})
            encoded = None
            if data is not None:
                encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
            req = urllib.request.Request(url, data=encoded, headers=headers, method=method)
            with net_io.open(req, timeout=min(int(submit.get("timeout", 60)), 180)) as resp:
                body = resp.read(100000).decode("utf-8", errors="replace")
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return body
        except Exception as exc:
            logger.exception("Job 提交请求失败：submit=%s", submit.get("url") or submit.get("type"))
            return None

    def _poll_status(self, poll: dict[str, Any], handle: Any) -> str:
        url = str(poll.get("url") or "").replace("{handle}", str(handle))
        if not url:
            return "unknown"
        try:
            with net_io.open(url, timeout=min(int(poll.get("timeout", 30)), 120)) as resp:
                body = resp.read(100000).decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return "unknown"
            status_path = poll.get("status_path", "status")
            node = data
            for key in status_path.split("."):
                if isinstance(node, dict):
                    node = node.get(key)
                else:
                    return "unknown"
            return str(node or "unknown")
        except Exception:
            return "unknown"

    # ---- ComfyUI 专用 Worker（持续检查 + 唤醒 + 可恢复） ----
    def _run_comfyui(self, job_id: str, spec: JobSpec, cancel: threading.Event) -> None:
        params = spec.params or {}
        comfy_url = str(
            params.get("comfyui_url")
            or self.app.config.data.get("comfyui_url")
            or "http://127.0.0.1:8188"
        ).rstrip("/")
        if not comfy_url:
            self._set_status(job_id, "running", current_step="未配置 ComfyUI URL")
            self._finish(job_id, "failed", error="未配置 ComfyUI URL")
            return
        workflow = params.get("workflow") or {}
        shots = int(params.get("shots") or 1)
        checkpoint = dict(spec.checkpoint or {})
        completed_shots = checkpoint.get("completed_shots", [])
        prompt_ids = checkpoint.get("prompt_ids", [])

        # 1. 验证 ComfyUI 可达
        if not self._comfyui_reachable(comfy_url):
            self._finish(job_id, "failed", error="ComfyUI 服务不可达", result={"comfyui_url": comfy_url})
            return

        # High-level batch calls use a list of already prepared API workflows.
        # Keep the legacy single-workflow path below for compatibility.
        workflows = params.get("workflows")
        if isinstance(workflows, list) and workflows:
            self._run_comfyui_batch(job_id, spec, cancel, comfy_url, workflows)
            return

        self._set_status(job_id, "running", current_step=f"开始生成 {shots} 段", progress=0,
                         checkpoint={"completed_shots": completed_shots, "prompt_ids": prompt_ids})
        errors: list[str] = []
        for index in range(len(completed_shots), shots):
            if cancel.is_set():
                self._finish(job_id, "cancelled", error="用户取消",
                             result={"completed_shots": completed_shots, "prompt_ids": prompt_ids, "errors": errors})
                return
            self.app.storage.update_job(job_id, current_step=f"提交第 {index + 1}/{shots} 段",
                                        progress=round(index / shots * 100, 1))
            self._emit(job_id, {"type": "job_check", "phase": "submit_shot", "shot": index + 1, "total": shots})
            prompt_id, submit_reason = self._comfyui_submit(comfy_url, workflow, index)
            if not prompt_id:
                reason = f"第 {index + 1} 段提交失败：{submit_reason}" if submit_reason else f"第 {index + 1} 段提交失败（工作流或节点错误）"
                errors.append(reason)
                self._emit(job_id, {"type": "job_check", "phase": "shot_failed", "shot": index + 1, "reason": reason})
                # 单镜头失败记录原因，不自动跳过；依赖 checkpoint 供后续恢复
                self._finish(job_id, "failed", error=reason,
                             result={"completed_shots": completed_shots, "prompt_ids": prompt_ids, "errors": errors})
                return
            prompt_ids.append(prompt_id)
            self.app.storage.update_job(job_id, checkpoint={"completed_shots": completed_shots, "prompt_ids": prompt_ids})
            # 2. 轮询历史
            ok, files, reason = self._comfyui_wait_history(comfy_url, prompt_id, cancel, job_id, index, shots)
            if cancel.is_set():
                self._finish(job_id, "cancelled", error="用户取消",
                             result={"completed_shots": completed_shots, "prompt_ids": prompt_ids, "errors": errors})
                return
            if not ok:
                errors.append(reason)
                self._finish(job_id, "failed", error=reason,
                             result={"completed_shots": completed_shots, "prompt_ids": prompt_ids, "errors": errors})
                return
            completed_shots.append({"index": index, "prompt_id": prompt_id, "files": files})
            self.app.storage.update_job(
                job_id,
                progress=round((index + 1) / shots * 100, 1),
                current_step=f"第 {index + 1}/{shots} 段完成",
                checkpoint={"completed_shots": completed_shots, "prompt_ids": prompt_ids},
            )
            self._emit(job_id, {"type": "job_check", "phase": "shot_done", "shot": index + 1, "files": files})
        self._finish(
            job_id, "completed",
            result={"completed_shots": completed_shots, "prompt_ids": prompt_ids, "errors": errors},
        )

    def _run_comfyui_batch(
        self,
        job_id: str,
        spec: JobSpec,
        cancel: threading.Event,
        comfy_url: str,
        workflows: list[dict[str, Any]],
    ) -> None:
        """Fire all prompts first, then poll them as one durable batch."""
        checkpoint = dict(spec.checkpoint or {})
        submitted = list(checkpoint.get("submitted") or [])
        completed = list(checkpoint.get("completed") or [])
        errors: list[dict[str, Any]] = list(checkpoint.get("errors") or [])
        total = len(workflows)
        self._set_status(job_id, "running", current_step=f"提交 {total} 个工作流", progress=0,
                         checkpoint={"submitted": submitted, "completed": completed, "errors": errors})

        # Submit only work not present in the checkpoint, making resume safe.
        for index in range(len(submitted), total):
            if cancel.is_set():
                self._finish(job_id, "cancelled", error="用户取消",
                             result={"prompt_ids": submitted, "completed": completed, "errors": errors})
                return
            prompt_id, submit_reason = self._comfyui_submit(comfy_url, workflows[index], index)
            if not prompt_id:
                reason_text = f"提交失败：{submit_reason}" if submit_reason else "提交失败"
                errors.append({"index": index, "error": reason_text})
                self._finish(job_id, "failed", error=f"第 {index + 1} 段{reason_text}",
                             result={"prompt_ids": submitted, "completed": completed, "errors": errors})
                return
            submitted.append(prompt_id)
            self.app.storage.update_job(
                job_id,
                progress=round((index + 1) / max(total, 1) * 30, 1),
                current_step=f"已提交 {index + 1}/{total}",
                checkpoint={"submitted": submitted, "completed": completed, "errors": errors},
            )
            self._emit(job_id, {"type": "job_check", "phase": "batch_submitted", "index": index, "prompt_id": prompt_id, "total": total})

        # Poll each prompt independently; completed/errored entries are checkpointed so
        # interruption does not resubmit or lose already finished segments.
        done_indexes = {int(item.get("index")) for item in completed if isinstance(item, dict) and str(item.get("index", "")).isdigit()}
        # 已失败的镜头同样算完成：否则恢复后会重新轮询，把同一处错误反复计入 errors。
        done_indexes.update(int(item.get("index")) for item in errors if isinstance(item, dict) and str(item.get("index", "")).isdigit())
        # 每个镜头「丢失后重提交」的次数，持久化在 checkpoint，重启不会把预算清零。
        resubmits = dict(checkpoint.get("resubmits") or {})
        max_resubmit = max(0, int(spec.params.get("max_resubmit", 1) or 0))
        deadline = time.monotonic() + float(spec.params.get("wait_timeout", 7200) or 7200)
        while len(done_indexes) < total:
            if cancel.is_set():
                self._finish(job_id, "cancelled", error="用户取消",
                             result={"prompt_ids": submitted, "completed": completed, "errors": errors})
                return
            if time.monotonic() > deadline:
                self._finish(job_id, "failed", error="ComfyUI 批量轮询超时",
                             result={"prompt_ids": submitted, "completed": completed, "errors": errors})
                return
            # 每轮取一次全队列：只有「历史里没有、队列里也没有」才算该 prompt 已被 ComfyUI 丢弃。
            queue_ids = self._comfyui_queue_ids(comfy_url)
            for index, prompt_id in enumerate(submitted):
                if index in done_indexes:
                    continue
                ok, files, reason = self._comfyui_history_once(comfy_url, prompt_id)
                if ok:
                    completed.append({"index": index, "prompt_id": prompt_id, "files": files})
                    done_indexes.add(index)
                    self._emit(job_id, {"type": "job_check", "phase": "batch_done", "index": index, "files": files, "total": total})
                    continue
                if reason and reason.startswith("ERROR:"):
                    errors.append({"index": index, "prompt_id": prompt_id, "error": reason[6:]})
                    done_indexes.add(index)
                    self._emit(job_id, {"type": "job_check", "phase": "batch_failed", "index": index, "error": reason[6:]})
                    continue
                if queue_ids is None or prompt_id in queue_ids:
                    # 仍在排队/执行中；队列查询失败时保守处理，不判定丢失。
                    continue
                # ComfyUI 重启或队列被清空后，旧 prompt_id 既不在历史也不在队列。
                # 这种「查不到」以前会被当成「还在跑」，一直空转到 wait_timeout（默认 2 小时），
                # 任务面板就长期挂着「运行中 · 完成 5/8」且无法推进——这里改为可恢复处理。
                used = int(resubmits.get(str(index), 0) or 0)
                if used < max_resubmit and index < len(workflows):
                    new_prompt_id, resubmit_reason = self._comfyui_submit(comfy_url, workflows[index], index)
                    if new_prompt_id:
                        resubmits[str(index)] = used + 1
                        submitted[index] = new_prompt_id
                        self.app.storage.update_job(
                            job_id,
                            checkpoint={"submitted": submitted, "completed": completed,
                                        "errors": errors, "resubmits": resubmits},
                        )
                        self._emit(job_id, {"type": "job_check", "phase": "batch_resubmit",
                                            "index": index, "prompt_id": new_prompt_id, "total": total})
                        continue
                    logger.error("ComfyUI 丢失重提交失败：job=%s index=%s %s", job_id, index, resubmit_reason)
                errors.append({
                    "index": index,
                    "prompt_id": prompt_id,
                    "error": "ComfyUI 已丢失该任务（服务重启或队列被清空）",
                })
                done_indexes.add(index)
                self._emit(job_id, {"type": "job_check", "phase": "batch_lost", "index": index, "total": total})
            progress = 30 + (len(done_indexes) / max(total, 1) * 70)
            self.app.storage.update_job(
                job_id, progress=round(progress, 1), current_step=f"完成 {len(done_indexes)}/{total}",
                checkpoint={"submitted": submitted, "completed": completed, "errors": errors, "resubmits": resubmits},
            )
            if len(done_indexes) < total:
                time.sleep(1.0 if total <= 4 else 3.0)
        status = "completed" if not errors else ("failed" if not completed else "completed")
        self._finish(job_id, status, error="" if not errors else "部分工作流失败",
                     result={"prompt_ids": submitted, "completed": completed, "errors": errors, "total": total})

    def _comfyui_history_once(self, base: str, prompt_id: str) -> tuple[bool, list[str], str]:
        """Return (done, output files, reason); empty history means still running."""
        try:
            with net_io.open(f"{base}/history/{prompt_id}", timeout=30) as resp:
                history = json.loads(resp.read(200000).decode("utf-8", errors="replace"))
            entry = history.get(prompt_id) if isinstance(history, dict) else None
            if not entry:
                return False, [], ""
            status = (entry.get("status") or {}).get("status_str", "")
            if status and status not in {"success", "completed"}:
                if status in {"error", "failed"}:
                    return False, [], f"ERROR:ComfyUI 状态 {status}"
                return False, [], ""
            files = self._comfyui_output_urls(base, entry)
            if not files:
                return False, [], "ERROR:未找到输出文件"
            return True, files, ""
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False, [], ""
            return False, [], f"ERROR:HTTP {exc.code}"
        except Exception:
            return False, [], ""

    def _comfyui_queue_ids(self, base: str) -> set[str] | None:
        """当前 ComfyUI 队列中 pending/running 的 prompt_id 集合；查询失败返回 None。"""
        try:
            with net_io.open(f"{base}/queue", timeout=15) as resp:
                data = json.loads(resp.read(200000).decode("utf-8", errors="replace"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        ids: set[str] = set()
        for key in ("queue_running", "queue_pending"):
            for item in data.get(key) or []:
                # 队列项形如 [number, prompt_id, workflow, extra, outputs]
                if isinstance(item, (list, tuple)) and len(item) > 1:
                    ids.add(str(item[1]))
        return ids

    def _comfyui_reachable(self, base: str) -> bool:
        try:
            with net_io.open(f"{base}/system_stats", timeout=10) as resp:
                return resp.status == 200
        except Exception:
            return False

    @staticmethod
    def _comfyui_output_urls(base: str, entry: dict[str, Any]) -> list[str]:
        files: list[str] = []
        for node in (entry.get("outputs") or {}).values():
            if not isinstance(node, dict):
                continue
            for kind in ("images", "gifs", "videos", "audio"):
                for item in node.get(kind, []) or []:
                    if not isinstance(item, dict) or not item.get("filename"):
                        continue
                    query = urllib.parse.urlencode({
                        "filename": str(item["filename"]),
                        "subfolder": str(item.get("subfolder") or ""),
                        "type": str(item.get("type") or "output"),
                    })
                    files.append(f"{base}/view?{query}")
        return files

    def _comfyui_submit(self, base: str, workflow: dict[str, Any], index: int) -> tuple[str, str]:
        """提交单个工作流，返回 (prompt_id, 失败原因)。成功时 reason 为空串。

        提交被 ComfyUI 拒绝（HTTP 4xx/5xx）或网络失败时，必须把服务端返回的原因
        带回去。此前只回 None，任务上只剩一句「第 N 段提交失败」，模型与用户都看
        不到真实原因（2026-09-20 事故：rgthree seed 超上限 400 被吞，模型连盲试 6 次）。
        """
        del index
        payload = {"prompt": workflow, "client_id": uuid.uuid4().hex}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/prompt", data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with net_io.open(req, timeout=60) as resp:
                body = json.loads(resp.read(100000).decode("utf-8", errors="replace"))
            prompt_id = str(body.get("prompt_id") or "")
            if not prompt_id:
                return "", "ComfyUI 响应缺少 prompt_id"
            return prompt_id, ""
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(4000).decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            reason = _summarize_comfyui_reject(exc.code, detail)
            logger.error("ComfyUI 提交任务失败：base=%s HTTP %s %s", base, exc.code, reason)
            return "", reason
        except Exception as exc:
            logger.exception("ComfyUI 提交任务失败：base=%s", base)
            return "", f"{type(exc).__name__}: {exc}"

    def _comfyui_wait_history(
        self, base: str, prompt_id: str, cancel: threading.Event, job_id: str, index: int, total: int
    ) -> tuple[bool, list[str], str]:
        deadline = time.monotonic() + 1800
        attempt = 0
        while True:
            if cancel.is_set():
                return False, [], "用户取消"
            if time.monotonic() > deadline:
                return False, [], f"第 {index + 1} 段生成超时"
            attempt += 1
            self.app.storage.update_job(job_id, attempt=attempt)
            try:
                with net_io.open(f"{base}/history/{prompt_id}", timeout=30) as resp:
                    history = json.loads(resp.read(200000).decode("utf-8", errors="replace"))
                entry = history.get(prompt_id) if isinstance(history, dict) else None
                if entry:
                    files = self._comfyui_output_urls(base, entry)
                    # 验证文件存在且非空
                    ok_all = bool(files)
                    for url in files:
                        try:
                            with net_io.open(url, timeout=20) as r:
                                if int(r.headers.get("Content-Length", "0")) == 0:
                                    ok_all = False
                        except Exception:
                            ok_all = False
                    if ok_all:
                        return True, files, ""
                    return False, files, f"第 {index + 1} 段产物缺失或为空"
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    # 尚未完成，继续轮询
                    pass
                else:
                    return False, [], f"第 {index + 1} 段查询错误：HTTP {exc.code}"
            except Exception:
                pass
            time.sleep(1.0)

    def shutdown(self, timeout: float = 10.0) -> None:
        with self._lock:
            events = list(self._cancel.values())
            threads = list(self._threads.values())
        for event in events:
            event.set()
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)


# 兼容别名
BackgroundTaskManager = JobRegistry
