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

from naiba.core.exceptions import TaskCancelled
from naiba.llm.profiles import resolve_profile_with_override
from naiba.studio.store import now_ms

FLUSH_CHARS = 4096
FLUSH_SECONDS = 0.1


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


class StudioRunner:
    """串行执行队列。enqueue 由 jobs 做；这里只负责跑到终态并落事件/状态。"""

    def __init__(self, app: Any, store: Any):
        self.app = app
        self.store = store
        self._queue: list[str] = []
        self._condition = threading.Condition()
        self._worker: threading.Thread | None = None
        self._shutdown = False
        self._cancel_events: dict[str, threading.Event] = {}
        self._registry_lock = threading.Lock()

    # ---- 队列 ----

    def submit(self, task_id: str) -> None:
        with self._condition:
            if self._shutdown:
                return
            self._queue.append(task_id)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._worker_loop, name="naiba-studio-run", daemon=True)
                self._worker.start()

    def cancel(self, task_id: str, *, reason: str = "") -> bool:
        """取消：运行中的经 cancel_event 让底层请求中断；排队中的直接终态化。"""
        with self._registry_lock:
            cancel_event = self._cancel_events.get(task_id)
        if cancel_event is not None:
            cancel_event.set()
            return True
        task = self.store.get_task(task_id)
        if not task:
            return False
        if task["status"] == "queued":
            with self._condition:
                if task_id in self._queue:
                    self._queue.remove(task_id)
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
        for event in list(self._cancel_events.values()):
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

    # ---- 执行 ----

    def _run_task(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if not task or task["status"] != "queued":
            return
        cancel_event = threading.Event()
        with self._registry_lock:
            self._cancel_events[task_id] = cancel_event
        buffer = _DeltaBuffer(self._make_emitter(task_id))
        try:
            self.store.update_task(task_id, status="running", started_at=now_ms(), detail="")
            self.store.append_event(task_id, {"type": "status", "message": "生成任务开始执行"})
            content = self._complete(task.get("input") or {}, buffer.forward, cancel_event)
            buffer.flush()
            result_partial = ""
            self._finish_terminal(task_id, "done", {"type": "done"}, result=content)
            _ = result_partial
        except Exception as exc:
            buffer.flush()
            traceback.print_exc()
            # llm 层的取消是 RuntimeError("任务已取消")（HTTP 取消按钮/取消等待各点统一口径），
            # 与 TaskCancelled 同义——两条都按 cancelled 收尾，partial 保留。
            if "任务已取消" in str(exc) or isinstance(exc, TaskCancelled):  # pragma: no cover - 见上
                self._finish_terminal(
                    task_id, "cancelled", {"type": "cancelled"},
                    result_partial=self.store.partial_from_events(task_id),
                    detail="任务已取消",
                )
            else:
                self._finish_terminal(
                    task_id, "failed", {"type": "error", "message": str(exc)},
                    result_partial=self.store.partial_from_events(task_id),
                    error=str(exc),
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
            if self._cancelled_for(task_id).is_set() and str(payload.get("type") or "") in {"delta", "reasoning_delta"}:
                # 取消后剩余的尾流事件直接丢弃（partial 已在收尾重建）。
                return
            try:
                store.append_event(task_id, payload)
            except Exception:  # noqa: BLE001 - 事件落盘失败不中断生成
                traceback.print_exc()

        return emit

    def _cancelled_for(self, task_id: str) -> threading.Event:
        with self._registry_lock:
            return self._cancel_events.get(task_id) or threading.Event()

    def _complete(
        self, input_data: dict[str, Any], status_provider: Any, cancel_event: threading.Event
    ) -> str:
        profile = resolve_profile_with_override(
            self.app.config, str(input_data.get("model_key") or ""), str(input_data.get("model_name") or "")
        )
        images = [ref for ref in (input_data.get("images") or []) if isinstance(ref, dict)]
        if images:
            # 带图生成：结构化多模态消息（图号与图片依次配对），不把路径拼成提示文本。
            vision = getattr(self.app, "vision", None)
            if vision is not None and not vision.brain_supports_images(profile):
                raise RuntimeError(
                    "当前模型不支持图片输入：请换一个支持视觉的模型，"
                    "或先用「识别图片」把描述写进输入后再生成"
                )
            from naiba.vision.images import encode_image_file

            parts: list[dict[str, Any]] = []
            for index, ref in enumerate(images, 1):
                part = encode_image_file(str(ref.get("path") or ""))
                if part is None:
                    raise RuntimeError(
                        f"图{index}无法读取（文件可能已被清理）：请重新上传后再生成"
                    )
                label = str(ref.get("name") or "").strip() or f"图{index}"
                parts.append({"type": "text", "text": f"\n\n图{index}（{label}）："})
                parts.append(part)
            parts.append({"type": "text", "text": "\n\n" + str(input_data.get("content") or "")})
            messages = [{"role": "user", "content": parts}]
        else:
            messages = [{"role": "user", "content": str(input_data.get("content") or "")}]
        options = {
            "stream": True,
            "cancel_event": cancel_event,
            "lock_label": "工作台生成",
            **(self.app.config.runtime_guard_options() or {}),
        }
        return self.app.models.complete(profile, messages, options, status_provider)

    # ---- 终态 ----

    def _finish_terminal(
        self, task_id: str, status: str, terminal_payload: dict[str, Any], *,
        result: str = "", result_partial: str = "", detail: str = "", error: str = "",
    ) -> None:
        usage: dict[str, Any] = {}
        reasoning: list[str] = []
        performance: dict[str, Any] = {}
        finish_reason = ""
        if status == "done" and result:
            # 终态防降级：用户已取消/已收尾的任务不再把状态改成 done（排队取消与 worker 的窄竞态）。
            current = self.store.get_task(task_id)
            if current and current["status"] in {"cancelled", "failed", "interrupted"}:
                return
            # 串行队列保证此刻读 last_* 是本任务自己的读数（P0 并发边界）。
            usage = dict(getattr(self.app.models, "last_usage", {}) or {})
            reasoning_text = str(getattr(self.app.models, "last_reasoning", "") or "")
            reasoning = [reasoning_text] if reasoning_text else []
            performance = dict(getattr(self.app.models, "last_diagnostics", {}) or {})
            finish_reason = str(getattr(self.app.models, "last_finish_reason", "") or "")
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
        )
