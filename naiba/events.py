"""统一事件总线（事件体系重构阶段 2）。

历史双通道（run 的 ``ConversationRunManager.emit`` 与 job 的 ``JobRegistry._emit``）
各自实现「写事件表 + 唤醒等待者」，condition 池也两套——本模块把两者收敛为
**单点写入 + 单点唤醒**：

- ``EventBus.emit``：唯一事件发射入口（run/job/subagent/恢复标注全部经此）；
  写入走 ``storage.append_run_event``（存储仍是 SQLite 唯一写入口）；
- ``EventBus.ensure``：条件池（run 与 job 共用同一张，消除双 condition 体系）；
- ``validate_event_payload``：事件契约前置校验（type ∈ EventType、键 ⊆ 该 type 的
  契约图 ∪ 公共键）——``NAIBA_STRICT_EVENTS=1`` 时严格强制，默认仅记录告警；
- ``status_sync_for``：run 通道「事件 → background_tasks.status/detail」策略表
  （自 manager.emit 的 if-elif 链收敛；jobs 通道状态由 _set_status/_finish 独立维护）。

依赖方向：events → storage → core（无环；run/manager 与 jobs 均依赖本模块）。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from naiba.core.contracts import EVENT_PAYLOAD_KEYS, EventType

logger = logging.getLogger("naiba.events")

# emit 后由存储层附加的公共键（不属于事件自身的契约负载）。
SHARED_EVENT_KEYS = frozenset({"type", "run_id", "sequence", "created_at"})


def strict_events_enabled() -> bool:
    """严格模式：事件契约违规直接抛错（开发/测试用，防止漏登记事件漂移）。"""
    return os.environ.get("NAIBA_STRICT_EVENTS") == "1"


def validate_event_payload(payload: dict[str, Any]) -> list[str]:
    """事件契约校验：返回违规清单（空列表 = 合法）。

    - type 必须 ∈ EventType（新增事件先登记枚举，再发射）；
    - 键必须 ⊆ 该 type 的契约图（EVENT_PAYLOAD_KEYS）∪ 公共键；宽松条目仅校验 type。
    """
    if not isinstance(payload, dict):
        return ["payload 不是 dict"]
    kind = str(payload.get("type") or "")
    if kind not in EventType.__members__.values():
        return [f"未知事件 type：{kind!r}（未登记进 EventType）"]
    allowed = EVENT_PAYLOAD_KEYS.get(kind, frozenset())
    if allowed is None:
        return []  # 宽松条目（job 域动态字段）：仅校验 type 存在性
    extra = sorted(set(payload.keys()) - allowed - SHARED_EVENT_KEYS)
    return [f"事件 {kind} 负载键未登记进契约：{extra}"] if extra else []


class EventBus:
    """事件总线：write + notify 单点（run/job 共用）。

    语义与历史双通道完全一致：``emit`` 先写事件表再唤醒等待者；
    ``wait_for_events`` 先读后等（无事件才等待）；通知在锁内发出。

    持 app（AppContext）而非 storage：写入时经 ``app.storage`` 取最新实例
    （测试/装配可能替换 storage 对象，构造期绑定会脱节）。
    """

    def __init__(self, app: Any):
        self.app = app
        self._lock = threading.RLock()
        self._conditions: dict[str, threading.Condition] = {}

    @property
    def storage(self) -> Any:
        return self.app.storage

    def ensure(self, task_id: str) -> threading.Condition:
        """取（或建）该任务的唤醒条件；run 结束/清理时由 ``drop`` 移除。"""
        with self._lock:
            return self._conditions.setdefault(str(task_id), threading.Condition(self._lock))

    def emit(
        self,
        task_id: str,
        payload: dict[str, Any],
        *,
        raise_on_error: bool = True,
        db: Any = None,
    ) -> dict[str, Any]:
        """写事件 + 唤醒等待者；返回带 run_id/sequence/created_at 的完整事件。

        ``raise_on_error`` 运行通道默认 True（契约/写入违规显式报错）；
        job 通道传 False 保持既有容错语义（记录违规告警，不打断 worker）。
        ``db``：调用方（run sink）持有的长连接，给了就复用它落库（省掉每事件
        开/关连接的整轮开销）；缺省走存储层每调用一条连接的默认路径。
        """
        if raise_on_error or strict_events_enabled():
            violations = validate_event_payload(payload)
            if violations:
                raise ValueError("事件契约违规：" + "; ".join(violations))
        elif validate_event_payload(payload):
            logger.warning("事件契约违规（已忽略）：%s", validate_event_payload(payload))
        if db is not None:
            event = self.storage.append_run_event(task_id, payload, db=db)
        else:
            # 条件式传参：测试桩/旧存储的 append_run_event 不认识 db 参数，不能硬塞。
            event = self.storage.append_run_event(task_id, payload)  # 运行不存在时抛 LookupError
        condition = self.ensure(task_id)
        with condition:
            condition.notify_all()
        return event

    def wait_for_events(self, task_id: str, after: int = 0, timeout: float = 15.0) -> list[dict[str, Any]]:
        """等待并读取 ``after`` 之后的新事件（先读后等；超时返回空列表）。

        与历史 manager.wait_for_events 的轮询段等价；「run 已终态是否还等」的
        判定由调用方（manager）负责，本方法只做条件等待。
        """
        events = self.storage.list_run_events(task_id, after)
        if events:
            return events
        condition = self.ensure(task_id)
        with condition:
            condition.wait(timeout=max(0.1, timeout))
        return self.storage.list_run_events(task_id, after)

    def drop(self, task_id: str) -> None:
        """任务结束时移除其唤醒条件（防长期持有的 condition 泄漏）。"""
        with self._lock:
            self._conditions.pop(str(task_id), None)


def status_sync_for(kind: str, payload: dict[str, Any]) -> tuple[str | None, dict[str, Any]] | None:
    """run 通道：事件 → background_tasks(status, detail) 同步策略表。

    自 manager.emit 的 if-elif 链收敛（语义逐项对拍 golden：skills 只写 detail
    不改 status）；返回 None 表示该事件不驱动任务状态（终态由收尾路径显式 update，
    cancelling 冻结由调用方另行判断）。
    """
    if kind == "skills":
        return None, {"message": "已启用 Skill", "skills": payload.get("skills") or []}
    if kind == "status":
        return "running", {"message": str(payload.get("message") or "正在执行")}
    if kind == "tool_start":
        return "running", {
            "message": f"正在执行 {payload.get('tool') or '工具'}",
            "tool": str(payload.get("tool") or ""),
        }
    if kind == "tool_confirm":
        return "waiting", {
            "message": "等待工具确认",
            "tool": str(payload.get("tool_name") or ""),
            "tool_desc": str(payload.get("tool_desc") or ""),
            "arguments": payload.get("arguments") or {},
            "confirm_id": str(payload.get("confirm_id") or ""),
        }
    if kind == "tool_result":
        return "running", {"message": f"工具 {payload.get('tool') or ''} 执行完毕"}
    return None
