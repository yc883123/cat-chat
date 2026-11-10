"""贴签匠并发闸门：按供应商档案（API key）限制在途请求数。

口径（``docs/plans/2026-11-10-贴签匠自定义打标多并发监督计划.md`` §3.2/§3.3）：

- 打标员槽位和监督员可能选中同一份供应商档案；同一档案共用一份 API key 额度，
  所以闸门按 ``model_key``（档案身份）计数，**不按模型名**推断额度独立。
- 额度未知时不猜测，使用保守默认值；达到上限排队等待，不丢任务。
- 等待期间响应取消：``TaskCancelled`` 由 runner 按取消收敛。
- 429/限流不在这里重试；调用方按既有 ``classify_error`` + 有限退避处理。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

from naiba.core.exceptions import TaskCancelled

# 未知额度时的保守默认：多数在线供应商允许 ≥3 个并发请求。
DEFAULT_PROVIDER_CONCURRENCY = 3
MIN_PROVIDER_CONCURRENCY = 1
MAX_PROVIDER_CONCURRENCY = 9
# 监督队列的系统上限：单监督模型配置也必须能同时审多张图，但不无限扩张。
SUPERVISOR_MAX_CONCURRENCY = 4
# 自定义档最多 9 个打标员槽位（计划 §2）。
MAX_WORKERS = 9


def validate_provider_limits(raw: Any) -> dict[str, int]:
    """校验并归一化「供应商档案 → 并发上限」；非法值抛 ``ValueError``。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("provider_limits 必须是对象")
    clean: dict[str, int] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        if not name:
            continue
        if isinstance(value, bool):
            raise ValueError(f"供应商「{name}」的并发上限必须是整数")
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"供应商「{name}」的并发上限必须是整数") from None
        if not MIN_PROVIDER_CONCURRENCY <= number <= MAX_PROVIDER_CONCURRENCY:
            raise ValueError(
                f"供应商「{name}」的并发上限必须在 "
                f"{MIN_PROVIDER_CONCURRENCY} 到 {MAX_PROVIDER_CONCURRENCY} 之间"
            )
        clean[name] = number
    return clean


class ProviderGate:
    """按档案计的并发闸门（可跨任务复用；线程安全）。"""

    def __init__(self, limits: dict[str, int] | None = None,
                 default: int = DEFAULT_PROVIDER_CONCURRENCY):
        self._default = max(MIN_PROVIDER_CONCURRENCY,
                            min(MAX_PROVIDER_CONCURRENCY, int(default)))
        self._limits = dict(limits or {})
        self._inflight: dict[str, int] = {}
        self._condition = threading.Condition()

    def limit_for(self, key: str) -> int:
        return int(self._limits.get(str(key), self._default))

    def set_limits(self, limits: dict[str, int], default: int | None = None) -> None:
        """整份替换额度（新任务生效；运行中的任务用自己启动时的快照）。"""
        with self._condition:
            self._limits = dict(limits or {})
            if default is not None:
                self._default = max(MIN_PROVIDER_CONCURRENCY,
                                    min(MAX_PROVIDER_CONCURRENCY, int(default)))
            self._condition.notify_all()

    def inflight(self, key: str) -> int:
        with self._condition:
            return int(self._inflight.get(str(key), 0))

    def acquire(self, key: str, cancel_event: threading.Event | None = None) -> None:
        key = str(key or "")
        limit = self.limit_for(key)
        with self._condition:
            while int(self._inflight.get(key, 0)) >= limit:
                if cancel_event is not None and cancel_event.is_set():
                    raise TaskCancelled("任务已取消")
                self._condition.wait(0.1)
            if cancel_event is not None and cancel_event.is_set():
                raise TaskCancelled("任务已取消")
            self._inflight[key] = int(self._inflight.get(key, 0)) + 1

    def release(self, key: str) -> None:
        key = str(key or "")
        with self._condition:
            remaining = int(self._inflight.get(key, 0)) - 1
            if remaining > 0:
                self._inflight[key] = remaining
            else:
                self._inflight.pop(key, None)
            self._condition.notify_all()

    @contextmanager
    def slot(self, key: str,
             cancel_event: threading.Event | None = None) -> Iterator[None]:
        self.acquire(key, cancel_event)
        try:
            yield
        finally:
            self.release(key)
