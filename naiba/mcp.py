from __future__ import annotations

import asyncio
import os
import threading
import time
from contextlib import AsyncExitStack
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import timedelta
from typing import Any, Callable

from naiba.core.contracts import (
    MCP_START_TIMEOUT_MS_DEFAULT,
    MCP_START_TIMEOUT_MS_MAX,
    MCP_START_TIMEOUT_MS_MIN,
)


# ---- 「连接建立」超时（每服务可配，单位毫秒；常量登记在 core/contracts.py）----
# 与 ``env["MCP_TOOL_TIMEOUT"]`` / ``call_timeout_seconds``（单次**工具调用**，默认 620 秒）
# 是两件独立的事：拉包慢的 npx 服务、``rmg_navserver`` 这类重服务常常 20 秒建不上连接，
# 而它们真正的工具调用可能一次跑几分钟。两个超时**不要合并**。
#
# 缺省仍是 20 秒：引入该配置之前所有服务的实际行为逐字不变（旧配置缺这个字段 = 20000）。


def clamp_start_timeout_ms(value: Any) -> int:
    """把启动超时夹到合法区间（毫秒）。

    运行期只做夹取、不抛错：配置里存了脏值也不该让服务连不上；写入侧
    （``ConfigStore.upsert_mcp_server``）才是严格校验那一关。
    """
    if isinstance(value, bool):
        return MCP_START_TIMEOUT_MS_DEFAULT
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return MCP_START_TIMEOUT_MS_DEFAULT
    return max(MCP_START_TIMEOUT_MS_MIN, min(MCP_START_TIMEOUT_MS_MAX, milliseconds))


def _format_seconds(value: float) -> str:
    """报错文案里的秒数：整数值不写成 ``30.0``（用户配的是毫秒，看到小数会以为配错了）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


class MCPError(RuntimeError):
    """MCP 通用错误基类。"""


class MCPStartupError(MCPError):
    """stdio 服务启动 / 初始化失败（区别于调用失败）。"""


class MCPServerConnection:
    def __init__(
        self,
        server_id: str,
        command: str,
        args: list[str],
        env: dict[str, str],
        start_timeout_ms: int = MCP_START_TIMEOUT_MS_DEFAULT,
        hidden_tools: list[str] | None = None,
    ):
        self.server_id = server_id
        self.command = command
        self.args = args
        self.env = env
        # 工具屏蔽名单（config.json 的 mcp_servers[].hidden_tools）。
        # 用途：某些 MCP 工具的参数 schema 嵌套过深（如 rea 的
        # compare_web_captures 达 17 层），会被上游以
        # "JSON schema exceeds the maximum nesting depth of 10 levels"
        # 整单拒绝；屏蔽后这些工具不进入工具表、不进模型请求。
        self.hidden_tools = {str(name).strip() for name in (hidden_tools or []) if str(name).strip()}
        # 连接建立超时（秒）：per-server，缺省 20 秒（= 引入该配置前的硬编码值）。
        self.start_timeout_ms = clamp_start_timeout_ms(start_timeout_ms)
        self.start_timeout_seconds = self.start_timeout_ms / 1000
        # Video workflows can take longer than ordinary MCP calls.  A server
        # may opt in without extending timeouts for every other MCP service.
        # 注意：这是**单次工具调用**超时，与上面的 start_timeout_ms 无关。
        try:
            self.call_timeout_seconds = max(20, int(env.get("MCP_TOOL_TIMEOUT", "620")))
        except (TypeError, ValueError):
            self.call_timeout_seconds = 620
        self.tools: list[dict[str, Any]] = []
        self.error = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session = None
        self._stack: AsyncExitStack | None = None
        self._stop_signal: asyncio.Event | None = None
        # mcp SDK 1.x 的 call_tool 期望 read_timeout_seconds 为 timedelta；2.x 为 float 秒数。
        # 连接建立时按实际打包的 SDK 签名自适应，避免传错类型导致
        # AttributeError: 'float' object has no attribute 'total_seconds'。
        self._read_timeout_is_timedelta = True
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._call_lock = threading.Lock()
        self._stopping = False
        self._stopped = False
        # 重连相关
        self._reconnect_attempts = 0
        self.max_reconnect_attempts = 5
        self.backoff_base = 2.0
        self.on_tools_discovered: Callable[[str, list[dict[str, Any]]], None] | None = None
        # 运行期活动状态（供 UI 显示 calling/idle）
        self.active_calls = 0
        self.activity: str = "idle"  # 取值 "calling" | "idle"
        self.last_used_at: float | None = None

    def start(self, timeout: float | None = None) -> None:
        """启动连接；``timeout`` 为 None 时用该服务自己的 ``start_timeout_ms``。

        显式传值的调用点行为不变（旧签名 ``start(timeout=20)`` 仍照旧生效）。
        """
        if timeout is None:
            timeout = self.start_timeout_seconds
        if self._thread and self._thread.is_alive():
            return
        self.error = ""
        self._ready = threading.Event()
        self._stopping = False
        self._stopped = False
        self._session = None
        self._stack = None
        self._loop = None
        self._stop_signal = None
        self._thread = threading.Thread(target=self._thread_main, name=f"mcp-{self.server_id}", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self.error = f"MCP 服务启动超过 {_format_seconds(timeout)} 秒"
            raise MCPStartupError(self.error)

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_connection())
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
        finally:
            self._loop.close()
            if self._stopping:
                self._stopping = False
                self._stopped = True

    async def _run_connection(self) -> None:
        self._stop_signal = asyncio.Event()
        try:
            await self._connect()
            self.error = ""
            self._ready.set()
            await self._stop_signal.wait()
        finally:
            if self._stack:
                await self._stack.aclose()

    async def _connect(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        # 适配 mcp SDK 的 call_tool 入参类型：1.x 用 timedelta，2.x 用 float 秒数。
        # 按实际打包版本运行时探测一次（mcp 是懒加载，无法在模块顶部导入）。
        try:
            import inspect as _inspect
            param = _inspect.signature(ClientSession.call_tool).parameters.get("read_timeout_seconds")
            annotation = str(getattr(param, "annotation", "") or "")
            self._read_timeout_is_timedelta = bool(annotation) and "timedelta" in annotation
        except Exception:
            self._read_timeout_is_timedelta = True  # 保守：mcp 1.x 用 timedelta

        self._stack = AsyncExitStack()
        parameters = StdioServerParameters(
            command=self.command,
            args=self.args,
            env={**os.environ, **self.env},
        )
        read_stream, write_stream = await self._stack.enter_async_context(stdio_client(parameters))
        self._session = await self._stack.enter_async_context(ClientSession(read_stream, write_stream))
        await self._session.initialize()
        result = await self._session.list_tools()
        new_tools = []
        for tool in result.tools:
            annotations: dict[str, Any] = {}
            ann = getattr(tool, "annotations", None)
            if ann is not None:
                # mcp SDK 新旧字段命名：旧版 camelCase，新版 snake_case。
                hint_fields = {
                    "readOnlyHint": "read_only_hint",
                    "destructiveHint": "destructive_hint",
                    "idempotentHint": "idempotent_hint",
                    "openWorldHint": "open_world_hint",
                }
                for hint, snake in hint_fields.items():
                    value = getattr(ann, hint, None)
                    if value is None:
                        value = getattr(ann, snake, None)
                    if value is not None:
                        annotations[hint] = value
            new_tools.append(
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": getattr(tool, "input_schema", None)
                    or getattr(tool, "inputSchema", None)
                    or {},
                    "annotations": annotations,
                }
            )
        # 屏蔽名单：从工具表里剔除（不影响服务本身的其它工具）。
        if self.hidden_tools:
            new_tools = [t for t in new_tools if t["name"] not in self.hidden_tools]
        # 工具列表变化时重新同步
        if [t["name"] for t in new_tools] != [t["name"] for t in self.tools]:
            self.tools = new_tools
            if self.on_tools_discovered:
                self.on_tools_discovered(self.server_id, new_tools)

    def reconnect_once(self, timeout: float | None = None) -> bool:
        """执行一次有界重连（无指数退避），成功返回 True。

        ``timeout`` 缺省 None = 用该服务自己的启动超时。
        """
        self._reset_thread()
        try:
            self.start(timeout=timeout)
        except MCPError:
            return False
        return bool(self._session)

    def call(self, tool_name: str, arguments: dict[str, Any], timeout: int | None = None) -> tuple[bool, str]:
        timeout = timeout or self.call_timeout_seconds
        with self._call_lock:
            self.active_calls += 1
            self.activity = "calling"
            self.last_used_at = time.time()
            try:
                if self.error and "已注销" in self.error:
                    return False, self.error
                if self.error or not self._session or not self._loop:
                    # 调用时发现断线：从未启动的先按需启动一次（lazy start），
                    # 否则执行有界重连，再返回明确的启动/调用错误。
                    if self._thread is None and not self.error:
                        try:
                            # 统一走 per-server 口径：lazy start 不再自带 15-45 秒推导，
                            # 否则用户把启动超时调大后，第一次工具调用仍按旧值放弃。
                            self.start()
                        except Exception as exc:
                            return False, f"启动 MCP 服务失败：{exc}"
                        if not self._session or not self._loop:
                            return False, self.error or "MCP 服务启动后仍不可用"
                    if not self.reconnect_once():
                        return False, self.error or "MCP 服务重连失败"
                    if not self._session or not self._loop:
                        return False, self.error or "MCP 服务尚未连接"
                future = asyncio.run_coroutine_threadsafe(
                    self._session.call_tool(
                        tool_name,
                        arguments,
                        # mcp SDK 1.x 期望 read_timeout_seconds 为 timedelta（内部调
                        # .total_seconds()）；2.x 为 float 秒数。按打包版本自适应。
                        read_timeout_seconds=(
                            timedelta(seconds=timeout)
                            if self._read_timeout_is_timedelta
                            else float(timeout)
                        ),
                    ),
                    self._loop,
                )
                try:
                    result = future.result(timeout=timeout)
                except FutureTimeoutError:
                    future.cancel()
                    return False, f"MCP 工具调用超过 {timeout} 秒"
                except Exception as exc:
                    future.cancel()
                    # 连接类错误尝试重连
                    if "Session" in type(exc).__name__ or "closed" in str(exc).lower():
                        try:
                            self.reconnect_with_backoff()
                        except MCPError as reconn_exc:
                            return False, str(reconn_exc)
                    return False, f"{type(exc).__name__}: {exc}"
                blocks = []
                for item in result.content:
                    text = getattr(item, "text", None)
                    if text is not None:
                        blocks.append(text)
                        continue
                    try:
                        blocks.append(item.model_dump_json())
                    except Exception:
                        blocks.append(str(item))
                is_error = getattr(result, "is_error", None)
                if is_error is None:
                    is_error = getattr(result, "isError", False)
                return not bool(is_error), "\n".join(blocks)
            finally:
                self.active_calls = 0
                self.activity = "idle"

    def reconnect_with_backoff(self) -> None:
        """指数退避重连；达到最大重试次数后注销该 Server 的工具。"""
        while self._reconnect_attempts < self.max_reconnect_attempts:
            self._reconnect_attempts += 1
            delay = self.backoff_base * (2 ** (self._reconnect_attempts - 1))
            time.sleep(delay)
            self._reset_thread()
            try:
                self.start()
                self._reconnect_attempts = 0
                return
            except MCPError:
                continue
        # 重试耗尽，注销工具
        self.tools = []
        self.error = f"MCP 服务 {self.server_id} 重连失败，工具已注销"

    def _reset_thread(self) -> None:
        self._stopping = True
        if self._loop and self._loop.is_running() and self._stop_signal:
            self._loop.call_soon_threadsafe(self._stop_signal.set)
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        self._session = None
        self._stack = None
        self._loop = None
        self._stop_signal = None
        self._ready = threading.Event()
        self._stopping = False
        self._stopped = True

    def stop(self) -> None:
        self._stopping = True
        if self._loop and self._loop.is_running() and self._stop_signal:
            self._loop.call_soon_threadsafe(self._stop_signal.set)
        if self._thread:
            self._thread.join(timeout=5)
        if self._thread and self._thread.is_alive():
            return
        self._thread = None
        self._session = None
        self._stack = None
        self._loop = None
        self._stop_signal = None
        self._ready = threading.Event()
        self.error = ""
        self._stopping = False
        self._stopped = True

    def state(self) -> dict[str, Any]:
        if self._stopping:
            status = "stopping"
        elif self.error:
            status = "error"
        elif self._session:
            status = "connected"
        elif self._stopped:
            status = "stopped"
        else:
            status = "idle"
        return {
            "id": self.server_id,
            "connected": status == "connected",
            "status": status,
            "error": self.error,
            "tools": self.tools,
            "reconnect_attempts": self._reconnect_attempts,
            "active_calls": self.active_calls,
            "activity": self.activity,
            "last_used_at": self.last_used_at,
            # 配置回显（列表/编辑表单要用）：env **只给 KEY**，值一律不出这个口子
            # ——env 里常见 GITHUB_TOKEN 一类令牌，回显值等于把它们摊在接口响应里。
            "command": self.command,
            "args": list(self.args),
            "env_keys": sorted(self.env.keys()),
            "start_timeout_ms": self.start_timeout_ms,
        }


class MCPRegistry:
    def __init__(self, configs: list[dict[str, Any]]):
        self.connections: dict[str, MCPServerConnection] = {}
        # start() is used by application bootstrap and keeps connections alive until stop().
        self._persistent = False
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self.on_tools_discovered: Callable[[str, list[dict[str, Any]]], None] | None = None
        self.on_tools_deregistered: Callable[[str], None] | None = None
        for config in configs:
            if not config.get("enabled", True):
                continue
            server_id = str(config.get("id") or "").strip()
            command = str(config.get("command") or "").strip()
            if not server_id or not command:
                continue
            self.connections[server_id] = self._connection(config)

    @staticmethod
    def _connection(config: dict[str, Any]) -> MCPServerConnection:
        connection = MCPServerConnection(
            str(config["id"]),
            str(config["command"]),
            [str(item) for item in config.get("args", [])],
            {str(key): str(value) for key, value in (config.get("env") or {}).items()},
            # 旧配置没有这个字段 → 走缺省 20000（= 引入前的硬编码 20 秒），无需数据迁移。
            start_timeout_ms=clamp_start_timeout_ms(config.get("start_timeout_ms")),
            hidden_tools=config.get("hidden_tools") or [],
        )
        return connection

    def _wire(self, connection: MCPServerConnection) -> None:
        connection.on_tools_discovered = self.on_tools_discovered

    def upsert(self, config: dict[str, Any]) -> dict[str, Any]:
        """Add or replace one server without interrupting unrelated MCP sessions."""
        server_id = str(config.get("id") or "").strip()
        command = str(config.get("command") or "").strip()
        if not server_id or not command:
            raise ValueError("MCP server id and command are required")
        normalized = {
            "id": server_id,
            "command": command,
            "args": [str(item) for item in config.get("args", [])],
            "env": {str(key): str(value) for key, value in (config.get("env") or {}).items()},
            "start_timeout_ms": clamp_start_timeout_ms(config.get("start_timeout_ms")),
            "hidden_tools": [str(item) for item in (config.get("hidden_tools") or [])],
            "enabled": bool(config.get("enabled", True)),
        }
        with self._lifecycle_lock:
            with self._lock:
                previous = self.connections.pop(server_id, None)
        if previous:
            previous.stop()
        if not normalized["enabled"]:
            if self.on_tools_deregistered:
                self.on_tools_deregistered(server_id)
            return {"id": server_id, "status": "disabled", "connected": False, "tools": [], "error": ""}
        connection = self._connection(normalized)
        self._wire(connection)
        with self._lifecycle_lock:
            with self._lock:
                self.connections[server_id] = connection
                should_start = self._persistent
        if should_start:
            connection.start()
        return connection.state()

    def remove(self, server_id: str) -> bool:
        """Remove one server entirely: stop its connection, deregister tools, drop it."""
        server_id = str(server_id or "").strip()
        with self._lifecycle_lock:
            with self._lock:
                connection = self.connections.pop(server_id, None)
        if connection is None:
            return False
        try:
            connection.stop()
        except Exception:
            pass
        if self.on_tools_deregistered:
            try:
                self.on_tools_deregistered(server_id)
            except Exception:
                pass
        return True

    def start(self) -> None:
        with self._lifecycle_lock:
            with self._lock:
                self._persistent = True
                connections = list(self.connections.values())
        for connection in connections:
            try:
                connection.start()
            except MCPStartupError:
                # 启动失败已记录在 connection.error（state()/lightweight_status() 可观测），
                # 单个服务失败不中断其余连接。
                continue

    def stop(self) -> None:
        with self._lifecycle_lock:
            with self._lock:
                self._persistent = False
                connections = list(self.connections.values())
        for connection in connections:
            connection.stop()

    def retry_unconnected_until_stopped(self, interval: float = 30.0) -> None:
        """Watchdog: keep re-starting MCP connections that failed to establish at
        app startup (server was briefly down / still starting). Once a server comes
        up, it connects and registers its tools, so a new session bakes them in.
        Runs as a daemon and stops when ``stop()`` flips ``_persistent``."""
        def _loop() -> None:
            while self._persistent:
                with self._lifecycle_lock:
                    with self._lock:
                        connections = list(self.connections.values())
                for connection in connections:
                    st = connection.state()
                    if st.get("status") in {"error", "idle", "stopped"} and not connection._thread:
                        try:
                            # 不传 timeout：沿用每个服务自己的启动超时配置。
                            connection.start()
                        except MCPError:
                            pass
                        except Exception:
                            pass
                time.sleep(interval)
        threading.Thread(target=_loop, name="naiba-mcp-retry", daemon=True).start()

    def connection(self, server_id: str) -> MCPServerConnection | None:
        """公开获取连接对象（线程安全）；未注册时返回 None。"""
        with self._lock:
            return self.connections.get(str(server_id or "").strip())

    def call(self, server_id: str, tool_name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        # Normalize ids emitted by older Skill prompts. The legacy app and
        # ComfyUI ids were removed from configuration; both now resolve to
        # the first-party comfy-mcp registration when it exists.
        if server_id in {"naiba-chat", "comfyui", "comfyui-mcp"} and "comfy-mcp" in self.connections:
            server_id = "comfy-mcp"
        with self._lock:
            connection = self.connections.get(server_id)
        if not connection:
            return False, f"未注册 MCP 服务：{server_id}"
        return connection.call(tool_name, arguments)

    def states(self) -> list[dict[str, Any]]:
        with self._lock:
            connections = list(self.connections.values())
        return [connection.state() for connection in connections]

    def lightweight_status(self) -> list[dict[str, Any]]:
        """廉价轮询端点：仅返回连接级状态，不含 tools 明细。"""
        with self._lock:
            connections = list(self.connections.values())
        result: list[dict[str, Any]] = []
        for connection in connections:
            conn_state = connection.state()
            result.append(
                {
                    "id": connection.server_id,
                    "status": conn_state["status"],
                    "connected": conn_state["connected"],
                    "active_calls": connection.active_calls,
                    "activity": connection.activity,
                    "last_used_at": connection.last_used_at,
                }
            )
        return result

    def tool_guide(self) -> str:
        rows = []
        with self._lock:
            connections = list(self.connections.items())
        for server_id, connection in connections:
            if connection.error:
                rows.append(f"- {server_id}: 不可用（{connection.error}）")
                continue
            for tool in connection.tools:
                schema = tool.get("input_schema") or {}
                rows.append(
                    f"- {server_id}.{tool['name']}: {tool['description'][:300]} "
                    f"参数={schema}"
                )
        return "\n".join(rows)

    # ---- Harness：将工具注册为 mcp__<server>__<tool> ----
    def register_tools_into(self, tool_registry: Any) -> None:
        """把每个 MCP 工具以 mcp__<server>__<tool> 名称注册到统一工具表。"""
        self.on_tools_discovered = tool_registry.register_mcp_tools
        with self._lock:
            connections = list(self.connections.items())
        for server_id, connection in connections:
            self._wire(connection)
            discovered = list(connection.tools)
            tool_registry.register_mcp_tools(server_id, discovered)
