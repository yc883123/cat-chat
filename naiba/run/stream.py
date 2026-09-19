"""运行事件流基建（原 async_tasks.py 的事件 sink 与活动时间线，阶段 2 第二步）。

- ``safe_activity`` / ``build_activity_timeline``：把事件序列编排成前端可渲染的
  思维链/工具链时间线（模块级纯函数，任何异常不阻断消息保存/发送）。
- ``_RunEventSink``：模型事件持久化协调器——delta 合流（≥4096 字符或 ≥0.1s 落库）、
  tool_requested/tool_started 去重为单个 tool_start、取消即抛 TaskCancelled。
  双线程（run 线程与看门狗线程）共享同一 sink，delta 缓冲由锁保护。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from naiba.core.exceptions import TaskCancelled

# 推理流采用「流式即刻落库 + 终态合流」：reasoning_delta 到达即落库并推送（保留
# 模型一个词一个词的实时显示节奏）；run 结束后由收尾合流（chat.py「终态压缩」
# → store.compress_run_events）把该 run 的 delta 事件重新整理为整段 reasoning
# （与存量压缩迁移 v14 同口径），历史库不膨胀。


def _safe_activity(
    events: list[dict[str, Any]], reasonings: list[str], runs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """用 try/except 包裹时间线构建，任何异常都不阻断消息保存/发送。"""
    try:
        return _build_activity_timeline(events, reasonings, runs)
    except Exception:
        return []


def _build_activity_timeline(
    events: list[dict[str, Any]], reasonings: list[str], runs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """按运行事件的严格物理时间序，把思考段、正文与工具调用交错产出，供前端按时间显示思维链/工具链。

    正文（delta 事件）只有当本轮确实调用了工具时才作为 ``{"type": "prose", "text": ...}``
    条目出现；若没有任何工具调用，正文就是整段的最终回复，统一放到末尾 content 里，
    避免"正文跑到最前面、思考在最后"的倒序观感。内容复用传入的 reasonings 与 runs
    （避免重复/不一致），仅用 events 的先后顺序决定交错。计数不齐时把剩余段落追加到
    末尾兜底。本函数为模块级。

    **只保留最后一段正文（2026-09-19，用户实测"十句进度话术堆在一条回复里"）**：
    模型每调用一次工具，都会先在正文里说一句"我已定位根因 / 继续验证 / 现在补测试"，
    这类"工具调用前的过程播报"一轮能攒下十来段，且句意高度重复。此前每段都作为独立
    prose 条目交错展示，一条回复里就摞成满屏废话。现在**除最后一段外全部丢弃**：
    一段正文之后若还有工具条目，它必然是某次工具调用前的预告；只有后面不再有工具调用的
    那一段，才是模型真正给出的最终答复。宿主生成的工具状态（tool 条目）足以说明过程，
    不需要复述（源头另在系统提示里约束，见 `naiba/skills/agent.py`）。

    每个条目附带 ``ts``（对应 run_events 事件的 created_at 毫秒时间戳）与
    ``request_index``（模型请求轮次序号，以 usage 事件为边界；前端据此在每次
    新请求的开始块左侧画「·」断点记号）——当前 ts 仅作备用，request_index 用于
    请求轮次定位。排序规则（用户实测反馈定稿）：**思考/工具严格物理序**；
    **最终答复（最后一 prose 段）固定在时间线末尾**（buffered 思考/工具晚于
    答复到达时不再把答复夹在中间）。
    """

    def _ts_for(*candidates: dict[str, Any]) -> int | None:
        for ev in candidates:
            ts = ev.get("created_at")
            if ts:
                return int(ts)
        return None

    activity: list[dict[str, Any]] = []
    has_tools = any(str(ev.get("type") or "") in {"tool_start", "tool_result"} for ev in events)
    ri = 0
    ti = 0
    in_reasoning = False
    prose: list[str] = []
    prose_ts: int | None = None
    request_index = 0  # 已完成的模型请求数；usage 事件为请求边界（活动条目归属于"下一个"请求）

    def flush_prose() -> None:
        nonlocal prose, prose_ts
        if prose and has_tools:
            item: dict[str, Any] = {
                "type": "prose",
                "text": "".join(prose),
                "request_index": request_index + 1,
            }
            if prose_ts:
                item["ts"] = prose_ts
            activity.append(item)
        prose = []
        prose_ts = None

    def flush_reasoning() -> None:
        nonlocal ri
        if in_reasoning and ri < len(reasonings):
            item: dict[str, Any] = {
                "type": "reasoning",
                "text": reasonings[ri],
                "request_index": request_index + 1,
            }
            ts = _ts_for(last_reasoning_end, last_reasoning_start)
            if ts:
                item["ts"] = ts
            activity.append(item)
            ri += 1

    last_reasoning_start: dict[str, Any] = {}
    last_reasoning_end: dict[str, Any] = {}
    last_tool_result: dict[str, Any] = {}

    for ev in events:
        kind = str(ev.get("type") or "")
        if kind == "usage":
            # 每一次请求完成 = 请求轮次边界：其后到达的活动条目归属下一次请求。
            request_index = max(0, int((ev.get("usage") or {}).get("requests") or 0))
            continue
        if kind == "delta":
            if not prose:
                prose_ts = _ts_for(ev)
            prose.append(str(ev.get("content") or ""))
            continue
        flush_prose()
        if kind == "reasoning_start":
            last_reasoning_start = ev
            in_reasoning = True
        elif kind == "reasoning_end":
            last_reasoning_end = ev
            flush_reasoning()
            in_reasoning = False
        elif kind == "reasoning":
            flush_reasoning()
            if ri < len(reasonings):
                item: dict[str, Any] = {
                    "type": "reasoning",
                    "text": reasonings[ri],
                    "request_index": request_index + 1,
                }
                ts = _ts_for(ev)
                if ts:
                    item["ts"] = ts
                activity.append(item)
                ri += 1
        elif kind == "reasoning_delta":
            # Streaming reasoning deltas are coalesced; the text is matched via
            # reasonings at reasoning_end, so keep state without adding here.
            pass
        elif kind == "tool_result":
            last_tool_result = ev
            if ti < len(runs):
                item: dict[str, Any] = {
                    "type": "tool",
                    "run": runs[ti],
                    "request_index": request_index + 1,
                }
                ts = _ts_for(ev)
                if ts:
                    item["ts"] = ts
                activity.append(item)
                ti += 1
    flush_prose()
    flush_reasoning()
    while ri < len(reasonings):
        item: dict[str, Any] = {"type": "reasoning", "text": reasonings[ri], "request_index": request_index + 1}
        ts = _ts_for(last_reasoning_end, last_reasoning_start)
        if ts:
            item["ts"] = ts
        activity.append(item)
        ri += 1
    while ti < len(runs):
        item: dict[str, Any] = {"type": "tool", "run": runs[ti], "request_index": request_index + 1}
        ts = _ts_for(last_tool_result)
        if ts:
            item["ts"] = ts
        activity.append(item)
        ti += 1
    # 最终答复（最后一 prose 段）固定到时间线末尾：模型流式时"后段思考/工具"可能晚于
    # 最终答复到达（buffered），物理序会把答复排在它们之前——用户实测确认最终答复应
    # 显示在时间线之后（思考全部折叠时尤为明显），故最终答复整体后置（其余条目仍严格物理序）。
    #
    # 同时丢弃所有**更早**的 prose 段：它们都是"某次工具调用前的过程播报"（后面还有工具
    # 条目），句意高度重复且对用户无信息量（2026-09-19 实测：一条回复攒了十段近义进度）。
    # 只留最后一段还有个额外好处——activityHasProse 仍为真，前端"正文内嵌在时间线里、
    # 不再在末尾重复渲染"的既有契约（public/js/04-messages.js）保持不变。
    prose_items = [item for item in activity if item.get("type") == "prose"]
    if prose_items:
        other_items = [item for item in activity if item.get("type") != "prose"]
        activity = [*other_items, prose_items[-1]]
    return activity


class _RunEventSink:
    """Persist model events while coalescing high-frequency text deltas."""

    def __init__(self, manager: Any, run_id: str, cancel_event: threading.Event):
        self.manager = manager
        self.run_id = run_id
        self.cancel_event = cancel_event
        self._delta = ""
        self._last_flush = time.monotonic()
        self._announced_tools: set[str] = set()
        self.failure_message: str | None = None
        # Guard the delta buffer so the run thread and the watchdog thread can both
        # flush safely (the watchdog may persist the aborted message without the run
        # thread ever reaching its own flush path).
        self._flush_lock = threading.Lock()

    def __call__(self, payload: dict[str, Any]) -> None:
        if self.cancel_event.is_set():
            raise TaskCancelled("任务已取消")
        if str(payload.get("type") or "") == "delta":
            self._delta += str(payload.get("content") or "")
            now = time.monotonic()
            if len(self._delta) >= 4096 or now - self._last_flush >= 0.1:
                self.flush()
            return
        if str(payload.get("type") or "") == "reasoning_delta":
            # 流式即刻落库：保留"一个词一个词"的实时推送节奏（落库即推送，
            # _stream_run 每事件 flush）；最终形态由 run 收尾的终态合流整理。
            self.manager.emit(self.run_id, payload)
            return
        self.flush()
        kind = str(payload.get("type") or "")
        if kind == "run_failed":
            self.failure_message = str(payload.get("error") or "任务执行失败")
        # SkillAgent emits a rich `tool_requested` event before dispatch and a
        # lower-level `tool_started` event inside the executor. The browser
        # protocol has one lifecycle event, so publish one `tool_start` and
        # suppress the duplicate while retaining the request metadata.
        if kind == "tool_requested":
            tool = str(payload.get("tool") or "")
            if tool:
                self._announced_tools.add(tool)
            payload = {**payload, "type": "tool_start"}
        elif kind == "tool_started":
            tool = str(payload.get("tool") or "")
            if tool in self._announced_tools:
                return
            payload = {**payload, "type": "tool_start"}
            if tool:
                self._announced_tools.add(tool)
        # 视觉工具与其它工具同构：不再旁路为 vision_start/vision_done 状态事件。
        self.manager.emit(self.run_id, payload)

    def flush(self) -> None:
        with self._flush_lock:
            if not self._delta:
                return
            content = self._delta
            self._delta = ""
            self._last_flush = time.monotonic()
        # Emit outside the lock: the content is already claimed above, so a
        # concurrent flush sees an empty buffer and returns without duplicating.
        self.manager.emit(self.run_id, {"type": "delta", "content": content})
