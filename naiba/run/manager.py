"""Run 执行引擎（原 async_tasks.py，阶段 2 第三步整体搬迁）。

ConversationRunManager：每会话一轮 Run 的调度器与注册表——提交/快照固化（session.py）、
事件流基建（stream.py）、取消/看门狗/插话/工具确认路由与 executor 隔离；
主循环（submit_chat/_run_chat/submit_plan/_run_plan）由 chat.py 的 ConversationRunMixin 提供。
"""

from __future__ import annotations

from naiba.core.contracts import AppContext

import threading
import time
from pathlib import Path
from typing import Any

from naiba.events import EventBus, status_sync_for
from naiba.run.stream import _RunEventSink
from naiba.run.session import (
    all_tool_names,
    attachments_have_images,
    bake_session_tool_ids,
    enable_conversation_tools,
    generation_options,
    resolve_allowed_tools,
    routing_message,
)

from naiba.run.chat import ConversationRunMixin
from naiba.core.exceptions import ActiveRunError
from naiba.storage.store import ACTIVE_TASK_STATUSES, TERMINAL_TASK_STATUSES


# 「这条会话在不在回答」只由这些 kind 的**顶层** Run 决定（见 store.create_chat_run）。
# 后台子 Job（子代理 / 视觉 / 批处理）的 kind 不在其中，parent_job_id 也非空。
PRIMARY_RUN_KINDS = frozenset({"chat", "plan_execute"})


class ConversationRunManager(ConversationRunMixin):
    # 与存储层共用同一份状态定义（见 store.ACTIVE_TASK_STATUSES 注释）：
    # jobs.cancel() 会把子任务置为 stopping，取消判定必须认它，否则一个正在
    # 收尾的子任务会被当成「已结束」。TERMINAL 一并补上 interrupted（重启清理
    # 产生），让 wait_for_events 对中断过的 run 立即返回而不是空等 15 秒。
    ACTIVE = set(ACTIVE_TASK_STATUSES)
    TERMINAL = set(TERMINAL_TASK_STATUSES)

    def __init__(self, app: AppContext):
        self.app = app
        self.bus = getattr(app, "event_bus", None) or EventBus(app)
        self._lock = threading.RLock()
        self._submit_lock = threading.RLock()
        self._events: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._executors: dict[str, Any] = {}
        self._sinks: dict[str, _RunEventSink] = {}
        self._sinks_lock = threading.Lock()

    def _resolve_allowed_tools(
        self,
        mode: str,
        agent: dict[str, Any],
        web_search_enabled: bool,
        model_key: str = "",
        enabled_tool_ids: list[str] | None = None,
    ) -> list[str]:
        """Freeze one run's tools（实现见 naiba/run/session.py）。"""
        return resolve_allowed_tools(self.app, mode, agent, web_search_enabled, model_key, enabled_tool_ids)

    def _all_tool_names(self) -> list[str]:
        """全部可用工具 id（实现见 naiba/run/session.py）。"""
        return all_tool_names(self.app)

    def _bake_session_tool_ids(
        self, conversation: dict[str, Any], agent: dict[str, Any]
    ) -> list[str]:
        """固化会话启用工具集（实现见 naiba/run/session.py）。"""
        return bake_session_tool_ids(self.app, conversation, agent)

    def enable_conversation_tools(
        self, conversation_id: str, tool_ids: list[str]
    ) -> dict[str, Any]:
        """追加/保底注入工具（实现见 naiba/run/session.py）。"""
        return enable_conversation_tools(self.app, conversation_id, tool_ids)

    def _condition(self, run_id: str) -> threading.Condition:
        """唤醒条件（实现见 naiba/events.py EventBus.ensure：run/job 共用条件池）。"""
        return self.bus.ensure(run_id)

    @staticmethod
    def _generation_options(config: Any, model_key: str = "") -> dict[str, Any]:
        """Read provider-scoped options（实现见 naiba/run/session.py）。"""
        return generation_options(config, model_key)

    @staticmethod
    def _attachments_have_images(attachments: list[Any]) -> bool:
        """附件是否含图片（实现见 naiba/run/session.py）。"""
        return attachments_have_images(attachments)

    @staticmethod
    def _routing_message(message: str, history: list[dict[str, Any]]) -> str:
        """短跟进消息的保留路由上下文（实现见 naiba/run/session.py）。"""
        return routing_message(message, history)

    @staticmethod
    def _active_error(exc: RuntimeError) -> ActiveRunError | None:
        text = str(exc)
        if text.startswith("ACTIVE_RUN:"):
            return ActiveRunError(text.split(":", 1)[1])
        return None

    def submit(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.submit_chat(body)

    def _start(self, run: dict[str, Any], target: Any) -> None:
        run_id = str(run["id"])
        cancel_event = threading.Event()
        self.executor_for_run(run_id)
        with self._lock:
            self._events[run_id] = cancel_event
            self._condition(run_id)
        snapshot = self.app.storage.get_run_snapshot(run_id) or {}
        self.emit(run_id, {
            "type": "run_started",
            "run_id": run_id,
        })
        thread = threading.Thread(
            target=target,
            args=(run_id, cancel_event),
            name=f"naiba-run-{run_id[:8]}",
            daemon=True,
        )
        with self._lock:
            self._threads[run_id] = thread
        thread.start()

    def emit(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """事件发射（实现见 naiba/events.py EventBus）：单点写入 + 唤醒，
        并按策略表同步 background_tasks.status/detail（终态由收尾路径显式维护）。"""
        event = self.bus.emit(run_id, payload)
        sync = status_sync_for(str(payload.get("type") or ""), payload)
        if sync is not None:
            status, detail = sync
            current = self.app.storage.get_background_task(run_id)
            if current and current.get("status") == "cancelling":
                status = None
            self.app.storage.update_background_task(run_id, status=status, detail=detail)
        return event

    def _finish(self, run_id: str) -> None:
        condition = self._condition(run_id)
        with condition:
            condition.notify_all()  # 唤醒可能仍在等待的 http 流线程（随后读终态事件退出）
        with self._lock:
            self._events.pop(run_id, None)
            self._threads.pop(run_id, None)
            self._executors.pop(run_id, None)
        self.bus.drop(run_id)
        self._unregister_sink(run_id)

    def list(self, conversation_id: str = "", active_only: bool = False) -> list[dict[str, Any]]:
        return self.app.storage.list_background_tasks(conversation_id, active_only)

    def list_primary(self, conversation_id: str = "", active_only: bool = False) -> list[dict[str, Any]]:
        """只返回**顶层对话 Run**（后台子 Job 不算）。

        子 Job 不占用会话的运行位，却会被 ``/api/runs`` 的前端消费者当成
        「会话正在回答」，把「分支 / 重新生成 / 新会话」三个救援入口全部挡住
        （2026-09-14 客户机实测：一个卡在 stopping 的子任务让用户只剩重启一条路）。
        与 ``storage.active_run`` 的顶层语义保持一致。
        """
        return [
            item
            for item in self.list(conversation_id, active_only)
            if str(item.get("kind") or "") in PRIMARY_RUN_KINDS
            and not str(item.get("parent_job_id") or "")
        ]

    def get(self, run_id: str) -> dict[str, Any] | None:
        return self.app.storage.get_background_task(run_id)

    def events_after(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        return self.app.storage.list_run_events(run_id, after)

    def wait_for_events(self, run_id: str, after: int = 0, timeout: float = 15.0) -> list[dict[str, Any]]:
        """拉取 after 之后的事件；无则等条件唤醒（15s 超时兜底）。

        run 已不存在或处于终态时不再等待（终态后无新事件，直接返回）。
        等待段实现见 EventBus.wait_for_events。
        """
        events = self.app.storage.list_run_events(run_id, after)
        if events:
            return events
        run = self.get(run_id)
        if not run or run.get("status") in self.TERMINAL:
            return []
        return self.bus.wait_for_events(run_id, after, timeout)

    def cancel(self, run_id: str) -> dict[str, Any] | None:
        with self._submit_lock:
            run = self.get(run_id)
            if not run:
                return None
            is_active = run.get("status") in self.ACTIVE
            children = [
                job for job in self.app.storage.list_background_tasks("", active_only=True, limit=200)
                if str(job.get("parent_job_id") or "") == run_id
            ]
            with self._lock:
                event = self._events.get(run_id)
                in_flight = run_id in self._threads
            if not is_active and not in_flight and not children:
                return run
            if event:
                event.set()
            updated = self.app.storage.update_background_task(
                run_id,
                status="cancelling" if is_active else "cancelled",
                cancel_requested=True,
                detail={"message": "正在取消任务"} if is_active else {"message": "任务已取消"},
                finished=not is_active,
            )
            conversation_id = str(run.get("conversation_id") or "")
            for child in children:
                child_id = str(child.get("id") or "")
                if str(child.get("kind") or "") in {"chat", "plan_execute"}:
                    self.cancel(child_id)
                else:
                    self.app.jobs.cancel(
                        child_id,
                        owner=conversation_id or None,
                        reason="父任务取消",
                    )
            # Completion may have won the lock immediately before cancellation and
            # created a follow-up.  Marking the terminal parent plus cascading its
            # now-active child closes that race as well.
            if is_active:
                self._schedule_forced_cancel(run_id)
            return updated

    def _schedule_forced_cancel(self, run_id: str) -> None:
        def watchdog() -> None:
            try:
                time.sleep(3.0)
                current = self.get(run_id)
                if current and current.get("status") == "cancelling":
                    # 兜底：即便 run 线程没能及时重建“已中止”消息（模型流卡住/空闲），
                    # 也在这里把已累积的内容持久化，避免中途输出丢失。
                    aborted_message = None
                    try:
                        conversation_id = str(current.get("conversation_id") or "")
                        skills = (current.get("detail") or {}).get("skills") or []
                        if conversation_id:
                            aborted_message = self._persist_aborted_message(
                                run_id, conversation_id, skills
                            )
                    except Exception:
                        aborted_message = None
                    self.app.storage.update_background_task(
                        run_id,
                        status="cancelled",
                        detail={"message": "任务已取消"},
                        finished=True,
                    )
                    cancelled_payload: dict[str, Any] = {"type": "cancelled", "message": "任务已取消"}
                    if aborted_message:
                        cancelled_payload["aborted_message"] = aborted_message
                    self.emit(run_id, cancelled_payload)
            except Exception:
                pass

        threading.Thread(target=watchdog, daemon=True).start()

    def _register_sink(self, run_id: str, sink: _RunEventSink) -> None:
        with self._sinks_lock:
            self._sinks[run_id] = sink

    def _unregister_sink(self, run_id: str) -> None:
        with self._sinks_lock:
            self._sinks.pop(run_id, None)

    def _flush_sink(self, run_id: str) -> None:
        """Flush any pending delta buffered in this run's sink (thread-safe)."""
        with self._sinks_lock:
            sink = self._sinks.get(run_id)
        if sink is not None:
            try:
                sink.flush()
            except Exception:
                pass

    def cancel_plan(self, plan_id: str) -> dict[str, Any] | None:
        run = next(
            (item for item in self.list(active_only=True) if str(item.get("plan_id") or "") == plan_id),
            None,
        )
        return self.cancel(str(run["id"])) if run else None

    def owns_confirmation(self, run_id: str, confirm_id: str) -> bool:
        run = self.get(run_id)
        if not run or str(run.get("status") or "") in {"completed", "failed", "cancelled", "cancelling"}:
            return False
        with self._lock:
            executor = self._executors.get(run_id)
        if executor is None:
            return False
        # A run may hold SEVERAL pending confirmations at once (e.g. a parallel
        # batch of out-of-workspace file reads). `run.detail.confirm_id` only
        # tracks the LAST one emitted, so it cannot be the source of truth here —
        # using it made clicking Confirm on any but the last request fail and hang
        # the conversation. Ownership is correctly decided by whether this
        # confirm_id is actually pending on the run's own executor.
        return confirm_id in getattr(executor, "pending_confirmation", {})

    def executor_for_run(self, run_id: str, snapshot: dict[str, Any] | None = None) -> Any:
        """Return the isolated executor owned by one Run, creating it if needed."""
        with self._lock:
            existing = self._executors.get(run_id)
            if existing is not None:
                return existing
        frozen = snapshot if snapshot is not None else (self.app.storage.get_run_snapshot(run_id) or {})
        mode = str(frozen.get("permission_mode") or "confirm")
        base = self.app.executor
        executor = (
            base.clone_for_permission(mode)
            if callable(getattr(base, "clone_for_permission", None))
            else base
        )
        workspace = str(frozen.get("workspace_dir") or "").strip()
        if not workspace:
            # 快照缺失（老数据/异常）时按"该会话自己的工作区"回退，绝不落回启动期默认工作区
            # ——否则会话在工作区 A、引擎却按启动工作区 B 判定与执行（界内路径被判越界）。
            conversation_id = str(frozen.get("conversation_id") or "").strip()
            if not conversation_id:
                run = self.app.storage.get_background_task(run_id) or {}
                conversation_id = str(run.get("conversation_id") or "").strip()
            conversation = (
                self.app.storage.get_conversation(conversation_id, include_messages=False)
                if conversation_id
                else None
            )
            raw = str((conversation or {}).get("workspace_dir") or "").strip()
            try:
                workspace = str(self.app.config.resolve_workspace_dir(raw or None))
            except (OSError, ValueError):
                workspace = ""
        if workspace and hasattr(executor, "workspace"):
            executor.workspace = Path(workspace).resolve()
        # 诊断标签：权限判定日志据此区分"哪个 executor 判的"（run 级 / app 级）
        if hasattr(executor, "debug_label"):
            executor.debug_label = f"run:{run_id}"
        with self._lock:
            return self._executors.setdefault(run_id, executor)

    def confirm_tool(self, run_id: str, confirm_id: str) -> tuple[bool, str] | None:
        if not self.owns_confirmation(run_id, confirm_id):
            return None
        with self._lock:
            executor = self._executors.get(run_id)
        if executor is None or confirm_id not in getattr(executor, "pending_confirmation", {}):
            return None
        return executor.confirm_execute(confirm_id)

    def confirm_tool_async(self, run_id: str, confirm_id: str) -> tuple[bool, str] | None:
        if not self.owns_confirmation(run_id, confirm_id):
            return None
        with self._lock:
            executor = self._executors.get(run_id)
        if executor is None or confirm_id not in getattr(executor, "pending_confirmation", {}):
            return None
        starter = getattr(executor, "confirm_execute_async", None)
        return starter(confirm_id) if callable(starter) else executor.confirm_execute(confirm_id)

    def reject_tool(self, run_id: str, confirm_id: str) -> tuple[bool, str] | None:
        if not self.owns_confirmation(run_id, confirm_id):
            return None
        with self._lock:
            executor = self._executors.get(run_id)
        if executor is None or confirm_id not in getattr(executor, "pending_confirmation", {}):
            return None
        return executor.reject_execute(confirm_id)

    def shutdown(self, timeout: float = 10.0) -> None:
        with self._lock:
            events = list(self._events.values())
            threads = list(self._threads.values())
        for event in events:
            event.set()
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)


# Compatibility for imports used by earlier builds and tests.
BackgroundTaskManager = ConversationRunManager
