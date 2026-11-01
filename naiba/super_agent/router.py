from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from naiba.super_agent.adapters import AdapterError, AdapterResult, BaseAdapter
from naiba.super_agent.service import SuperAgentService


class RouterError(RuntimeError):
    """A deterministic Fast Path request could not be routed."""


class AdapterRegistry:
    """Explicit executor-type registry; it never falls back to a model."""

    def __init__(self, adapters: list[BaseAdapter] | None = None):
        self._adapters: dict[str, BaseAdapter] = {}
        for adapter in adapters or []:
            self.register(adapter)

    def register(self, adapter: BaseAdapter, *, replace: bool = False) -> BaseAdapter:
        executor_type = str(getattr(adapter, "executor_type", "") or "").strip()
        if not executor_type:
            raise ValueError("Adapter 必须声明 executor_type")
        if executor_type in self._adapters and not replace:
            raise ValueError(f"Adapter 已注册: {executor_type}")
        self._adapters[executor_type] = adapter
        return adapter

    def unregister(self, executor_type: str) -> BaseAdapter:
        try:
            return self._adapters.pop(str(executor_type))
        except KeyError as exc:
            raise KeyError(f"未知 executor_type: {executor_type}") from exc

    def get(self, executor_type: str) -> BaseAdapter:
        try:
            return self._adapters[str(executor_type)]
        except KeyError as exc:
            raise RouterError(f"未知 executor_type: {executor_type}") from exc

    def __contains__(self, executor_type: object) -> bool:
        return executor_type in self._adapters

    def __len__(self) -> int:
        return len(self._adapters)

    def items(self):
        return self._adapters.items()


@dataclass(frozen=True, slots=True)
class FastPathRequest:
    owner_id: str
    thread_id: str
    node_id: str
    executor_type: str
    action: str = "start"
    executor_id: str = ""
    source_event_id: str = ""
    idempotency_key: str = ""
    request_id: str = ""
    attempt: int | None = None
    payload: dict[str, Any] | None = None


class FastPathRouter:
    """Route known structured task events without invoking an LLM."""

    ACTIONS = frozenset({"start", "status", "cancel", "retry", "resume", "sync"})

    def __init__(self, service: SuperAgentService, registry: AdapterRegistry):
        self.service = service
        self.registry = registry
        self._requests: dict[tuple[str, str, str, int, str], AdapterResult] = {}

    @staticmethod
    def _request_key(request: FastPathRequest) -> tuple[str, str, str, int, str]:
        return (
            request.thread_id,
            request.node_id,
            request.action,
            request.attempt if request.attempt is not None else -1,
            request.source_event_id or request.idempotency_key or request.request_id,
        )

    def _coerce(self, request: FastPathRequest | dict[str, Any], **kwargs: Any) -> FastPathRequest:
        if isinstance(request, FastPathRequest):
            try:
                result = replace(request, **kwargs) if kwargs else request
            except TypeError as exc:
                raise RouterError(f"Fast Path 参数不完整: {exc}") from exc
        else:
            data = dict(request)
            data.update(kwargs)
            try:
                result = FastPathRequest(**data)
            except TypeError as exc:
                raise RouterError(f"Fast Path 参数不完整: {exc}") from exc
        if not result.owner_id or not result.thread_id or not result.node_id or not result.executor_type:
            raise RouterError("Fast Path 必须提供 owner_id/thread_id/node_id/executor_type")
        return result

    def route(self, request: FastPathRequest | dict[str, Any], **kwargs: Any) -> AdapterResult:
        request = self._coerce(request, **kwargs)
        if request.action not in self.ACTIONS:
            raise RouterError(f"未知 Fast Path action: {request.action}")
        node = self.service.get_node(request.owner_id, request.node_id)
        if node.thread_id != request.thread_id:
            raise RouterError("Node 不属于当前 Thread")
        if node.executor_type != request.executor_type:
            raise RouterError("Node executor_type 与请求不匹配")
        if request.attempt is not None and node.attempt != request.attempt:
            raise RouterError("Node attempt 已过期")
        if request.executor_id and node.executor_id and node.executor_id != request.executor_id:
            raise RouterError("executor_id 与当前 Node 不匹配")
        key = self._request_key(request)
        cacheable = request.action in {"start", "cancel", "retry", "resume"}
        if cacheable and (request.source_event_id or request.idempotency_key or request.request_id):
            previous = self._requests.get(key)
            if previous is not None:
                return previous
        adapter = self.registry.get(request.executor_type)
        payload = dict(request.payload or {})
        method = getattr(adapter, request.action, None)
        if not callable(method):
            raise RouterError(f"Adapter 不支持 action: {request.action}")
        try:
            result = method(
                request.owner_id,
                request.thread_id,
                request.node_id,
                executor_id=request.executor_id,
                attempt=request.attempt,
                source_event_id=request.source_event_id,
                idempotency_key=request.idempotency_key,
                request_id=request.request_id,
                **payload,
            )
        except (LookupError, AdapterError, ValueError, RuntimeError) as exc:
            raise RouterError(str(exc)) from exc
        if not isinstance(result, AdapterResult):
            raise RouterError("Adapter 必须返回 AdapterResult")
        if cacheable and (request.source_event_id or request.idempotency_key or request.request_id):
            self._requests[key] = result
        return result

    def start(self, **kwargs: Any) -> AdapterResult:
        return self.route(FastPathRequest(action="start", **kwargs))

    def status(self, **kwargs: Any) -> AdapterResult:
        return self.route(FastPathRequest(action="status", **kwargs))

    def cancel(self, **kwargs: Any) -> AdapterResult:
        return self.route(FastPathRequest(action="cancel", **kwargs))

    def retry(self, **kwargs: Any) -> AdapterResult:
        return self.route(FastPathRequest(action="retry", **kwargs))

    def resume(self, **kwargs: Any) -> AdapterResult:
        return self.route(FastPathRequest(action="resume", **kwargs))

    def sync(self, **kwargs: Any) -> AdapterResult:
        return self.route(FastPathRequest(action="sync", **kwargs))
