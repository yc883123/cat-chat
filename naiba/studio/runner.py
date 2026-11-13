"""Studio 生成执行器：单 worker 串行队列，直调 ModelRuntime.complete()。

- 复用共享模型入口：profile 由 `naiba/llm/profiles.resolve_profile_with_override` 解析；
  执行用 `app.models.complete(profile, messages, options, status_callback)`（与
  `app._test_provider` 同形态），不 import `naiba/run/chat.py`、不碰会话/Run/事件表。
- 串行边界（P0 盘点 §2）：`ModelRuntime.last_usage/last_reasoning/...` 为实例属性，
  并发 complete() 会互踩读数——同一时刻只跑一个生成任务（桌面单用户，本地模型本来就
  要串行；如需并行先为 complete() 增加 per-call 用量捕获）。
- status 回调的 delta/reasoning_delta 在此合批（≥4096 字符或 ≥0.1s）写入 Studio 自己的
  事件流文件；status 与其他事件原样透传。取消信号全程透传 cancel_event。
"""

from __future__ import annotations

import threading
import time
import traceback
from typing import Any

from naiba.core.contracts import (STUDIO_PROMPT_SOURCE_KEY, STUDIO_TRACE_MESSAGES_KEY,
                                  STUDIO_HISTORY_MESSAGES_KEY, STUDIO_SYSTEM_PROMPT_KEY,
                                  STUDIO_OUTPUT_CONTRACT_VERSION_KEY, STUDIO_RESULT_ARTIFACT_KEY)
from naiba.core.exceptions import TaskCancelled
from naiba.studio.conversation import model_history
from naiba.studio.skill_adapter import ContentSkillAdapter, check_context
from naiba.llm.profiles import resolve_profile_with_override
from naiba.studio.store import now_ms
from naiba.studio.model_identity import configured_identity

FLUSH_CHARS = 4096
FLUSH_SECONDS = 0.1
HEARTBEAT_SECONDS = 15.0


class _DeltaBuffer:
    """delta / reasoning_delta 合批（≥4096 字符或 ≥0.1s），flush 先于其他事件。"""

    def __init__(self, emit: "Any"):
        self._emit = emit
        self._lock = threading.Lock()
        self._delta = ""
        self._reasoning = ""
        self._last = time.monotonic()

    def _flush_locked(self) -> None:
        pending = []
        if self._delta:
            pending.append({"type": "delta", "content": self._delta})
            self._delta = ""
        if self._reasoning:
            pending.append({"type": "reasoning_delta", "content": self._reasoning})
            self._reasoning = ""
        self._last = time.monotonic()
        for payload in pending:
            self._emit(payload)

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def forward(self, payload: dict[str, Any]) -> None:
        kind = str(payload.get("type") or "")
        if kind == "delta":
            with self._lock:
                self._delta += str(payload.get("content") or "")
                if len(self._delta) >= FLUSH_CHARS or time.monotonic() - self._last >= FLUSH_SECONDS:
                    self._flush_locked()
            return
        if kind == "reasoning_delta":
            with self._lock:
                self._reasoning += str(payload.get("content") or "")
                if len(self._reasoning) >= FLUSH_CHARS or time.monotonic() - self._last >= FLUSH_SECONDS:
                    self._flush_locked()
            return
        self.flush()
        self._emit(payload)


class _TaskProgress:
    """无输出时发等待提示；与模型回调串行，终态收尾前停止并封住迟到回调。"""

    def __init__(self, buffer: _DeltaBuffer, cancel_event: threading.Event):
        self._buffer = buffer
        self._cancel = cancel_event
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._closed = False
        self._last_output = time.monotonic()
        self._last_update = self._last_output
        self._has_output = False
        self._thread = threading.Thread(target=self._heartbeat, name="naiba-studio-progress", daemon=True)

    def __enter__(self) -> "_TaskProgress":
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        with self._lock:
            self._closed = True
            self._stop.set()
        self._thread.join()

    def forward(self, payload: dict[str, Any]) -> None:
        with self._lock:
            if self._closed or self._cancel.is_set():
                return
            kind = str(payload.get("type") or "")
            if kind in {"reasoning_start", "reasoning_end"}:
                # 聊天模型的边界信号不在 Studio 事件契约中，思考增量已经足够展示。
                return
            if kind in {"delta", "reasoning_delta"} and payload.get("content"):
                self._last_output = time.monotonic()
                self._last_update = self._last_output
                self._has_output = True
            elif kind == "status":
                self._last_update = time.monotonic()
            self._buffer.forward(payload)

    def _heartbeat(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            with self._lock:
                if self._closed or self._cancel.is_set():
                    return
                now = time.monotonic()
                if now - self._last_update < HEARTBEAT_SECONDS:
                    continue
                elapsed = int(now - self._last_output)
                waiting = "等待模型继续输出" if self._has_output else "等待模型响应"
                self._buffer.forward({
                    "type": "status", "message": f"{waiting}…（已 {elapsed} 秒无新内容）",
                })
                self._last_update = now


def _system_message(input_data: dict[str, Any]) -> list[dict[str, str]]:
    """可选系统提示词（图谱反哺等）：为空时消息列表与旧契约完全一致。"""
    system_text = str(input_data.get("system") or "").strip()
    if not system_text:
        return []
    return [{"role": "system", "content": system_text}]


def _merge_usage(total: dict[str, Any], incoming: Any) -> dict[str, Any]:
    """多轮任务的用量累加：数值字段求和，非数值字段以后一轮为准。"""
    if not isinstance(incoming, dict):
        return total
    for key, value in incoming.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            total[key] = total.get(key, 0) + value
        elif value:
            total[key] = value
    return total


class StudioRunner:
    """串行执行队列。enqueue 由 jobs 做；这里只负责跑到终态并落事件/状态。"""
    def __init__(self, app: Any, store: Any):
        self.app = app
        self.store = store
        self._queue: list[str] = []
        self._delivering: set[str] = set()
        self._condition = threading.Condition()
        self._worker: threading.Thread | None = None
        self._shutdown = False
        self._cancel_events: dict[str, threading.Event] = {}
        self._registry_lock = threading.Lock()

    # ---- 队列 ----

    def submit(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        with self._condition:
            if self._shutdown or task_id in self._delivering or not task or task.get("status") != "queued":
                return
            self._delivering.add(task_id)
            self._queue.append(task_id)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._worker_loop, name="naiba-studio-run", daemon=True)
                self._worker.start()

    def cancel(self, task_id: str, *, reason: str = "") -> bool:
        """取消：运行中的经 cancel_event 让底层请求中断；排队中的直接终态化。"""
        with self._registry_lock:
            task = self.store.get_task(task_id)
            if not task or task.get("status") not in {"queued", "running"}:
                return False
            cancel_event = self._cancel_events.get(task_id)
            if cancel_event is not None:
                cancel_event.set()
                return True
            if task["status"] == "queued":
                with self._condition:
                    if task_id in self._queue:
                        self._queue.remove(task_id)
                        self._delivering.discard(task_id)
                self._finish_terminal(
                    task_id, "cancelled", {"type": "cancelled"},
                    result_partial="", detail=reason or "任务已取消",
                )
                return True
            return False

    def shutdown(self, timeout: float = 5.0) -> None:
        with self._condition:
            self._shutdown = True
            remaining = list(self._queue)
            self._queue.clear()
            self._condition.notify_all()
        with self._registry_lock:
            active_events = list(self._cancel_events.values())
        for event in active_events:
            event.set()
        for task_id in remaining:
            self._finish_terminal(
                task_id, "cancelled", {"type": "cancelled"},
                result_partial="", detail="服务关闭，任务已取消",
            )
        worker = self._worker
        if worker is not None:
            worker.join(timeout)

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._shutdown:
                    self._condition.wait(timeout=1.0)
                task_id = self._queue.pop(0) if self._queue else ""
                if self._shutdown:
                    return
            if task_id:
                try:
                    self._run_task(task_id)
                except Exception:  # noqa: BLE001 - worker 不因单任务异常退出
                    traceback.print_exc()
                finally:
                    with self._condition:
                        self._delivering.discard(task_id)
                    # An explicit retry may race the previous worker's final cleanup.
                    task = self.store.get_task(task_id)
                    if not self._shutdown and task and task.get("status") == "queued":
                        self.submit(task_id)

    # ---- 执行 ----

    def _run_task(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if not task or task["status"] != "queued":
            return
        cancel_event = threading.Event()
        with self._registry_lock:
            if not self.store.claim_task(task_id):
                return
            self._cancel_events[task_id] = cancel_event
            if self._shutdown:
                cancel_event.set()
        buffer = _DeltaBuffer(self._make_emitter(task_id))
        # 分块任务的用量跨轮累加；单轮任务与旧行为一致（用 last_usage）。
        usage_sink: dict[str, Any] = {}
        trace_sink: list[dict[str, Any]] = []
        try:
            if cancel_event.is_set():
                raise TaskCancelled("任务已取消")
            self.store.append_event(task_id, {"type": "status", "message": "生成任务开始执行"})
            input_data = dict(task.get("input") or {})
            input_data["_result_task_id"] = task_id
            identity = (task.get("submission_policy") or {}).get("model_identity")
            if isinstance(identity, dict):
                input_data["model_snapshot"] = identity
            with _TaskProgress(buffer, cancel_event) as progress:
                content = self._complete(input_data, progress.forward, cancel_event, usage_sink, trace_sink)
            if cancel_event.is_set():
                raise TaskCancelled("任务已取消")
            buffer.flush()
            # The last cancellation check and done persistence share the
            # cancellation registry lock. A confirmed cancel cannot be followed
            # by done, and a finished task no longer accepts cancellation.
            with self._registry_lock:
                if cancel_event.is_set():
                    raise TaskCancelled("任务已取消")
                self._finish_terminal(task_id, "done", {"type": "done"}, result=content,
                                      usage_override=dict(usage_sink) if usage_sink or input_data.get(STUDIO_OUTPUT_CONTRACT_VERSION_KEY) else None,
                                      trace_messages=trace_sink)
        except Exception as exc:
            buffer.flush()
            traceback.print_exc()
            # llm 层的取消是 RuntimeError("任务已取消")（HTTP 取消按钮/取消等待各点统一口径），
            # 与 TaskCancelled 同义——两条都按 cancelled 收尾，partial 保留。
            if cancel_event.is_set() or "任务已取消" in str(exc) or isinstance(exc, TaskCancelled):  # pragma: no cover - 见上
                self._finish_terminal(
                    task_id, "cancelled", {"type": "cancelled"},
                    result_partial=self.store.partial_from_events(task_id),
                    detail="任务已取消", trace_messages=trace_sink,
                    usage_override=dict(usage_sink),
                )
            else:
                self._finish_terminal(
                    task_id, "failed", {"type": "error", "message": str(exc)},
                    result_partial=self.store.partial_from_events(task_id),
                    error=str(exc), trace_messages=trace_sink,
                    usage_override=dict(usage_sink),
                )
        finally:
            # 让本任务还在途的回调（status_provider / 迭代器）立即抛 TaskCancelled，
            # 不再向事件流吐残余增量；已写完 partial 的读数不受影响。
            cancel_event.set()
            with self._registry_lock:
                self._cancel_events.pop(task_id, None)

    def _make_emitter(self, task_id: str) -> Any:
        store = self.store

        def emit(payload: dict[str, Any]) -> None:
            # progress 在收尾前封住迟到回调；已接收的缓冲即使取消也须落盘以保留 partial。
            try:
                store.append_event(task_id, payload)
            except Exception:  # noqa: BLE001 - 事件落盘失败不中断生成
                traceback.print_exc()

        return emit

    def _resolve_profile(self, input_data: dict[str, Any]) -> dict[str, Any]:
        profile = resolve_profile_with_override(
            self.app.config, str(input_data.get("model_key") or ""), str(input_data.get("model_name") or "")
        )
        snapshot = input_data.get("model_snapshot")
        if isinstance(snapshot, dict):
            # Credentials are never copied into snapshots; compare only frozen identity fields.
            identity = configured_identity(input_data, profile)
            for key, value in snapshot.items():
                if key == "key":
                    continue
                actual = identity[key] if key in identity else profile.get(key)
                if actual != value:
                    raise ValueError("模型配置已改变，请重新确认配方后提交")
        return profile

    def _complete(
        self, input_data: dict[str, Any], status_provider: Any, cancel_event: threading.Event,
        usage_sink: dict[str, Any] | None = None,
        trace_sink: list[dict[str, Any]] | None = None,
    ) -> str:
        if input_data.get(STUDIO_OUTPUT_CONTRACT_VERSION_KEY):
            return self._complete_artifact(input_data, status_provider, cancel_event,
                                           usage_sink if usage_sink is not None else {},
                                           trace_sink if trace_sink is not None else [])
        if input_data.get(STUDIO_PROMPT_SOURCE_KEY) == "skill":
            profile = self._resolve_profile(input_data)
            history = model_history(input_data, profile, self.app.paths.data_dir)
            options = {**(self.app.config.runtime_guard_options() or {}), "stream": True,
                       "cancel_event": cancel_event, "lock_label": "工作台生成"}
            return ContentSkillAdapter(self.app).run(
                input_data, profile, history, options, status_provider,
                usage_sink if usage_sink is not None else {},
                trace_sink if trace_sink is not None else [],
            )
        chunked = input_data.get("chunked")
        if isinstance(chunked, dict) and isinstance(chunked.get("blocks"), list) and chunked.get("blocks"):
            return self._complete_chunked(input_data, chunked, status_provider, cancel_event, usage_sink)
        profile = self._resolve_profile(input_data)
        system_message = _system_message(input_data)
        data_dir = getattr(getattr(self.app, "paths", None), "data_dir", None)
        messages = [*system_message, *model_history(input_data, profile, data_dir)]
        options = {
            "stream": True,
            "cancel_event": cancel_event,
            "lock_label": "工作台生成",
            **(self.app.config.runtime_guard_options() or {}),
            **({"raw_content_sink": input_data["_raw_content_sink"]} if callable(input_data.get("_raw_content_sink")) else {}),
        }
        if input_data.get(STUDIO_HISTORY_MESSAGES_KEY) or input_data.get(STUDIO_SYSTEM_PROMPT_KEY):
            check_context(profile, options, str(input_data.get("system") or ""), messages[len(system_message):])
        content = self.app.models.complete(profile, messages, options, status_provider)
        if trace_sink is not None:
            message = {"role": "assistant", "content": str(content or "")}
            reasoning = getattr(self.app.models, "last_reasoning", "")
            if reasoning:
                message["reasoning_content"] = reasoning
            trace_sink[:] = [message]
        if usage_sink is not None:
            _merge_usage(usage_sink, getattr(self.app.models, "last_usage", {}))
        return content

    def _complete_artifact(self, input_data: dict[str, Any], event: Any,
                           cancel: threading.Event, usage_sink: dict[str, Any],
                           trace_sink: list[dict[str, Any]]) -> str:
        from naiba.studio.artifacts import ResultArtifactError
        from naiba.studio.content import ContentSnapshots, _digest
        from naiba.studio.result_tools import TxtToolContext, run_result_agent
        from naiba.core.contracts import STUDIO_SKILL_SNAPSHOT_KEY, STUDIO_SKILL_IDS_KEY

        task = self.store.get_task(str(input_data.get("_result_task_id") or ""))
        if task is None:
            raise ResultArtifactError("TXT任务记录不存在")
        manifest = self.store.artifacts.manifest(task)
        if manifest["status"] == "final":
            # A crash after final publication never incurs another model call.
            return self.store.artifacts.read(task)[0]
        profile = self._resolve_profile(input_data)
        options = {**(self.app.config.runtime_guard_options() or {}), "stream": True,
                   "cancel_event": cancel, "lock_label": "工作台TXT生成"}
        view = None
        system = str(input_data.get("system") or "")
        if input_data.get(STUDIO_PROMPT_SOURCE_KEY) == "skill":
            snapshot = input_data.get(STUDIO_SKILL_SNAPSHOT_KEY)
            view = ContentSnapshots(self.app.paths.data_dir).view(snapshot)
            if ([row["id"] for row in view.scan()] != list(input_data.get(STUDIO_SKILL_IDS_KEY) or [])
                    or snapshot.get("system_hash") != _digest(system)):
                raise ResultArtifactError("Skill冻结输入损坏，不能生成TXT")
        chunked = input_data.get("chunked") or {}
        blocks = [str(row) for row in chunked.get("blocks") or [] if str(row)] if isinstance(chunked, dict) else []
        for number, block_state in enumerate(manifest["blocks"], 1):
            if block_state.get("final"):
                continue
            if cancel.is_set():
                raise TaskCancelled("任务已取消")
            event({"type": "status", "message": f"正在生成TXT第{number}/{len(manifest['blocks'])}块"})
            local = dict(input_data)
            if blocks:
                content_parts = []
                if number > 1:
                    previous = self.store.artifacts._block_bytes(task["id"], number - 1,
                        self.store.artifacts.manifest(task)["blocks"][number - 2]).decode("utf-8")
                    tail_chars = max(0, min(12000, int(chunked.get("continuity_chars") or 0)))
                    if tail_chars and previous:
                        content_parts.append(str(chunked.get("continuity_label") or "前块末尾") + "\n" + previous[-tail_chars:])
                    event({"type": "delta", "content": "\n\n"})
                prefix = str(chunked.get("prefix") or "")
                content_parts.append(f"{prefix}\n\n{blocks[number - 1]}" if prefix else blocks[number - 1])
                local["content"] = "\n\n".join(content_parts)
            history = model_history(local, profile, self.app.paths.data_dir)
            context = TxtToolContext(self.store, task, number, cancel, event)
            block_usage: dict[str, Any] = {}
            try:
                run_result_agent(self.app, context, profile, history, options, system,
                                 event, block_usage, trace_sink, view=view)
            finally:
                _merge_usage(usage_sink, block_usage)
        return self.store.artifacts.read(task)[0]

    def _complete_chunked(
        self, input_data: dict[str, Any], chunked: dict[str, Any], status_provider: Any,
        cancel_event: threading.Event, usage_sink: dict[str, Any] | None = None,
    ) -> str:
        """分块续写：一个任务内多轮调用，逐块拼接（4A）。

        - 每轮开始发 status 事件（「正在生成第 i/N 块」），前端可显示进度。
        - 轮与轮之间补一个 `\n\n` delta：事件流重建的 partial 与最终结果同构，
          中途取消/失败时已完成块不丢。
        - 第 2 块起带上一轮输出的结尾（continuity），要求紧接续写不重复。
        - 用量跨轮累加，费用汇总不因此少记。
        """
        profile = self._resolve_profile(input_data)
        system_message = _system_message(input_data)
        blocks = [str(block) for block in chunked.get("blocks") or [] if str(block)]
        prefix = str(chunked.get("prefix") or "")
        try:
            tail_chars = max(0, int(chunked.get("continuity_chars") or 0))
        except (TypeError, ValueError):
            tail_chars = 0
        label = str(chunked.get("continuity_label") or "")
        total = len(blocks)
        options = {
            "stream": True,
            "cancel_event": cancel_event,
            "lock_label": "工作台生成",
            **(self.app.config.runtime_guard_options() or {}),
            **({"raw_content_sink": input_data["_raw_content_sink"]} if callable(input_data.get("_raw_content_sink")) else {}),
        }
        outputs: list[str] = []
        for index, block in enumerate(blocks, 1):
            status_provider({"type": "status", "message": f"正在生成第 {index}/{total} 块"})
            segments: list[str] = []
            if index > 1 and tail_chars:
                previous = outputs[-1].strip()
                if previous:
                    tail = previous[-tail_chars:]
                    segments.append(f"{label}\n{tail}" if label else tail)
            segments.append(f"{prefix}\n\n{block}" if prefix else block)
            messages = [*system_message, {"role": "user", "content": "\n\n".join(segments)}]
            text = self.app.models.complete(profile, messages, options, status_provider)
            outputs.append(str(text).strip())
            if usage_sink is not None:
                _merge_usage(usage_sink, getattr(self.app.models, "last_usage", {}))
            if index < total:
                status_provider({"type": "delta", "content": "\n\n"})
        return "\n\n".join(outputs)

    # ---- 终态 ----

    def _finish_terminal(
        self, task_id: str, status: str, terminal_payload: dict[str, Any], *,
        result: str = "", result_partial: str = "", detail: str = "", error: str = "",
        usage_override: dict[str, Any] | None = None,
        trace_messages: list[dict[str, Any]] | None = None,
    ) -> None:
        current_task = self.store.get_task(task_id)
        if current_task and current_task.get(STUDIO_OUTPUT_CONTRACT_VERSION_KEY):
            try:
                text, artifact = self.store.artifacts.read(current_task, require_final=status == "done")
                if status == "done":
                    result = text
                else:
                    result_partial = text
                self.store.update_task(task_id, **{STUDIO_RESULT_ARTIFACT_KEY: artifact})
            except ValueError as exc:
                if status == "done":
                    raise
                result_partial = ""
                self.store.update_task(task_id, **{STUDIO_RESULT_ARTIFACT_KEY: {"schema_version": 1,
                    "status": "invalid", "error": str(exc)}})
        usage: dict[str, Any] = dict(usage_override or {})
        reasoning: list[str] = []
        performance: dict[str, Any] = {}
        finish_reason = ""
        if status == "done" and result:
            # 终态防降级：用户已取消/已收尾的任务不再把状态改成 done（排队取消与 worker 的窄竞态）。
            current = self.store.get_task(task_id)
            if current and current["status"] in {"cancelled", "failed", "interrupted"}:
                return
            # 串行队列保证此刻读 last_* 是本任务自己的读数（P0 并发边界）。
            # 分块任务传 usage_override（跨轮累加值），单轮任务继续读 last_usage。
            usage = dict(usage_override) if usage_override is not None else dict(getattr(self.app.models, "last_usage", {}) or {})
            reasoning_text = str(getattr(self.app.models, "last_reasoning", "") or "")
            reasoning = [reasoning_text] if reasoning_text else []
            performance = dict(getattr(self.app.models, "last_diagnostics", {}) or {})
            finish_reason = str(getattr(self.app.models, "last_finish_reason", "") or "")
            if current_task and current_task.get(STUDIO_OUTPUT_CONTRACT_VERSION_KEY) and not usage:
                # Recovering a verified final file did not call the model. Its
                # accounting and diagnostics must not inherit another task.
                reasoning, performance, finish_reason = [], {}, "stop"
            if usage:
                # 用量事件先于终态落库，前端在 done 前可见费用汇总。
                self.store.append_event(task_id, {"type": "usage", "usage": usage})
        self.store.append_event(task_id, terminal_payload)
        self.store.update_task(
            task_id,
            status=status,
            detail=detail,
            result=result,
            result_partial=result_partial,
            reasoning=reasoning,
            error=error,
            usage=usage,
            performance=performance,
            finish_reason=finish_reason,
            finished_at=now_ms(),
            **{STUDIO_TRACE_MESSAGES_KEY: trace_messages or []},
        )
