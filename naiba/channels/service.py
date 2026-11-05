# -*- coding: utf-8 -*-
"""OneBot v11 反向 WebSocket 服务：NapCat 主动连进来，Cat Chat 收事件、发动作。

为什么要独立监听服务（计划 §3.2）：主 HTTP 服务是 ``ThreadingHTTPServer``，它的
请求处理函数是「读请求 → 写响应 → 结束」，没有长连接语义。把 WebSocket 塞进去
等于在普通 handler 里自造一套协议状态机；这里用 ``websockets.sync`` 单独起一个
线程承载，由应用生命周期管理启停。

鉴权（两道，都不可省）：
1. 路径必须等于配置的 ``path``（NapCat 反向 WS 的 URL 就是我们给的）；
2. ``Authorization: Bearer <onebot.access_token>`` 必须与 **OneBot 专用口令** 匹配
   ——与 Cat Chat 全局 ``access_token`` 分开保存、分开校验。
没有口令的 OneBot 端口等于「谁都能冒充 NapCat 投递 QQ 消息」，所以配置校验
（``ConfigStore.update_onebot``）与服务启动都会拒绝空口令。

连接状态对外只暴露粗粒度事实（连上了/断开/最近错误），不含口令与消息内容。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import uuid
from typing import Any, Callable

from naiba.channels.adapter import OneBotAdapter

logger = logging.getLogger("naiba.channels.service")

# NapCat/OneBot 动作响应超时（发消息、取文件都走它）。
ACTION_TIMEOUT_SECONDS = 70.0
# 单条入站事件的 JSON 上限（畸形/恶意大包直接丢）。
MAX_EVENT_BYTES = 1024 * 1024
# 断线后重推对账的节流：不重复扫描未完成投递。
RECOVERY_THROTTLE_SECONDS = 5.0

STATUS_DISABLED = "disabled"
STATUS_LISTENING = "listening"
STATUS_CONNECTED = "connected"
STATUS_ERROR = "error"


class OneBotConnection:
    """一条已鉴权的反向 WebSocket 连接：收发都被锁保护（多线程共用）。"""

    def __init__(self, ws: Any):
        self._ws = ws
        self._send_lock = threading.Lock()
        self._pending: dict[str, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self.closed = threading.Event()
        self.connected_at = time.time()

    # ---- 收 ----

    def handle_raw(self, raw: str | bytes) -> dict[str, Any] | None:
        """解析一帧；返回事件字典，动作响应（带 echo）在这里消化掉。"""
        if isinstance(raw, bytes):
            if len(raw) > MAX_EVENT_BYTES:
                return None
            try:
                raw = raw.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                return None
        if len(raw) > MAX_EVENT_BYTES:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        echo = value.get("echo")
        if echo is not None:
            with self._pending_lock:
                waiter = self._pending.get(str(echo))
            if waiter is not None:
                waiter["result"] = value
                waiter["event"].set()
                return None
        return value

    # ---- 发 ----

    def _send(self, payload: dict[str, Any]) -> bool:
        if self.closed.is_set():
            return False
        try:
            with self._send_lock:
                self._ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception:  # noqa: BLE001 - 连接已断：让调用方按失败处理
            self.closed.set()
            return False
        return True

    def call_action(
        self, action: str, params: dict[str, Any], timeout: float = ACTION_TIMEOUT_SECONDS
    ) -> dict[str, Any] | None:
        """发一个 OneBot 动作并等响应；失败/超时返回 None（不抛异常）。"""
        echo = uuid.uuid4().hex
        waiter = {"event": threading.Event(), "result": None}
        with self._pending_lock:
            self._pending[echo] = waiter
        try:
            if not self._send({"action": action, "params": params or {}, "echo": echo}):
                return None
            if not waiter["event"].wait(timeout=timeout):
                logger.warning("OneBot 动作超时：%s", action)
                return None
            response = waiter["result"] or {}
            if str(response.get("status") or "") not in {"ok", "async"} and response.get("retcode") not in (0, None):
                logger.warning(
                    "OneBot 动作失败：%s retcode=%s", action, response.get("retcode")
                )
                return None
            return response
        finally:
            with self._pending_lock:
                self._pending.pop(echo, None)

    def close(self) -> None:
        self.closed.set()
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001
            pass


class OneBotChannelService:
    """渠道服务：监听、鉴权、连接状态与适配器装配。

    ``start()`` 只在配置 ``enabled`` 且校验通过时真正监听；任何启动失败都记进
    ``status()`` 而不是抛给应用启动流程（QQ 渠道坏了不该让网页也起不来）。
    """

    def __init__(self, app: Any):
        self.app = app
        from naiba.channels.store import ChannelStore

        self.store = ChannelStore(app.paths.data_dir / "channels" / "qq.db")
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._state = STATUS_DISABLED
        self._error = ""
        self._last_event_at = 0.0
        self._last_recovery = 0.0
        self._account_id = ""
        self._binding: OneBotConnection | None = None
        self.adapter = OneBotAdapter(
            app,
            self.store,
            _ConnectionProxy(self),
            account_id_getter=lambda: self._account_id,
            media_link_builder=self._build_media_link,
        )

    # ---- 配置 ----

    def config(self) -> dict[str, Any]:
        getter = getattr(self.app.config, "onebot_config", None)
        if callable(getter):
            return getter()
        return dict(getattr(self.app.config, "data", {}).get("onebot") or {})

    # ---- 生命周期 ----

    def start(self) -> dict[str, Any]:
        """按配置启动监听；返回当前状态（不抛异常）。"""
        cfg = self.config()
        if not cfg.get("enabled"):
            self._state = STATUS_DISABLED
            return self.status()
        if not cfg.get("access_token"):
            self._state = STATUS_ERROR
            self._error = "未设置 OneBot 口令，QQ 渠道没有启动"
            logger.error(self._error)
            return self.status()
        if not cfg.get("allowed_user_ids"):
            self._state = STATUS_ERROR
            self._error = "未配置允许的 QQ 号，QQ 渠道没有启动"
            logger.error(self._error)
            return self.status()
        try:
            from websockets.sync.server import serve
        except ImportError:
            self._state = STATUS_ERROR
            self._error = "缺少 websockets 依赖，QQ 渠道无法启动"
            logger.exception(self._error)
            return self.status()

        host = str(cfg.get("listen_host") or "127.0.0.1")
        port = int(cfg.get("listen_port") or 0)
        expected_path = str(cfg.get("path") or "/onebot/v11/ws")
        token = str(cfg.get("access_token") or "")
        # 重新武装适配器：上一次 stop() 把停机标志置位了，不清掉的话新连接上来的
        # 消息会「跑了 Run 但永远不投递」。
        self.adapter.start()

        # 重启前发出的确认凭据一律作废：不能让旧编号在重启后重新可批准（计划 §5）。
        try:
            self.store.void_all_confirmations()
        except Exception:  # noqa: BLE001
            logger.warning("作废旧 QQ 确认凭据失败", exc_info=True)
        # 崩溃可能停在「已记录、未提交」：标成失败留痕，不留永久"进行中"的幽灵。
        try:
            self.adapter.recover_incomplete()
        except Exception:  # noqa: BLE001
            logger.warning("QQ 渠道残留记录对账失败", exc_info=True)

        def process_request(connection: Any, request: Any) -> Any:
            from websockets.datastructures import Headers
            from websockets.http11 import Response

            path = str(getattr(request, "path", "") or "").split("?", 1)[0]
            if path.rstrip("/") != expected_path.rstrip("/"):
                return Response(404, "Not Found", Headers(), b"not found")
            header = str(request.headers.get("Authorization") or "")
            provided = header[7:] if header.startswith("Bearer ") else ""
            if not provided:
                # 兼容 NapCat 用查询串传口令的写法（反向 WS 的 access_token 参数）。
                import urllib.parse

                query = urllib.parse.parse_qs(urllib.parse.urlsplit(str(request.path)).query)
                provided = (query.get("access_token") or [""])[0]
            if not provided or not secrets.compare_digest(provided, token):
                return Response(401, "Unauthorized", Headers(), b"unauthorized")
            return None

        def handler(ws: Any) -> None:
            self._serve_connection(ws)

        try:
            server = serve(
                handler,
                host,
                port,
                process_request=process_request,
                max_size=MAX_EVENT_BYTES,
                # NapCat 自己维护心跳；这里只做传输层 keepalive，短间隔有助发现半开连接。
                ping_interval=30,
                ping_timeout=90,
                logger=logger,
            )
        except OSError as exc:
            self._state = STATUS_ERROR
            self._error = f"QQ 渠道端口 {host}:{port} 无法监听：{exc}"
            logger.exception(self._error)
            return self.status()

        thread = threading.Thread(
            target=self._serve_forever, args=(server,), name="onebot-ws-server", daemon=True
        )
        with self._lock:
            self._server = server
            self._thread = thread
            self._state = STATUS_LISTENING
            self._error = ""
        thread.start()
        logger.info("QQ 渠道已监听 ws://%s:%s%s", host, port, expected_path)
        if host not in {"127.0.0.1", "::1", "localhost"}:
            logger.warning(
                "QQ 渠道监听在 %s：OneBot 端口对局域网开放，请确认网络边界与口令强度", host
            )
        return self.status()

    def _serve_forever(self, server: Any) -> None:
        try:
            server.serve_forever()
        except Exception:  # noqa: BLE001 - 服务线程异常只影响渠道
            logger.exception("QQ 渠道监听线程退出")

    def stop(self, timeout: float = 10.0) -> None:
        """停止监听、断开连接、等待后台投递线程收尾（计划 §3.2）。"""
        with self._lock:
            server = self._server
            thread = self._thread
            connection = self._binding
            self._server = None
            self._thread = None
            self._binding = None
        self.adapter.stop(timeout=timeout)
        if connection is not None:
            connection.close()
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001
                logger.warning("关闭 QQ 渠道监听失败", exc_info=True)
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))
        with self._lock:
            if self._state != STATUS_ERROR:
                self._state = STATUS_DISABLED

    def restart(self) -> dict[str, Any]:
        """配置保存后重载监听（改端口/口令/开关都要真的生效）。"""
        self.stop(timeout=5.0)
        self._state = STATUS_DISABLED
        self._error = ""
        return self.start()

    # ---- 连接 ----

    def _serve_connection(self, ws: Any) -> None:
        connection = OneBotConnection(ws)
        with self._lock:
            previous = self._binding
            self._binding = connection
            self._state = STATUS_CONNECTED
            self._error = ""
        # 同一时刻只服务一条反向连接（NapCat 默认单连接）；旧连接留着会双发消息。
        if previous is not None and previous is not connection:
            previous.close()
        logger.info("NapCat 已连接 QQ 渠道")
        try:
            self._recover_after_connect()
            for raw in ws:
                self._last_event_at = time.time()
                event = connection.handle_raw(raw)
                if event is None:
                    continue
                self._note_bot_account(event)
                try:
                    self.adapter.handle_event(event)
                except Exception:  # noqa: BLE001 - 单条事件异常不掐连接
                    logger.exception("处理 OneBot 事件失败")
        except Exception:  # noqa: BLE001
            logger.info("QQ 渠道连接结束", exc_info=True)
        finally:
            connection.close()
            with self._lock:
                if self._binding is connection:
                    self._binding = None
                    if self._state == STATUS_CONNECTED:
                        self._state = STATUS_LISTENING

    def _note_bot_account(self, event: dict[str, Any]) -> None:
        account_id = str(event.get("self_id") or "").strip()
        if account_id:
            self._account_id = account_id

    def _recover_after_connect(self) -> None:
        """NapCat 重连后补投递未完成的回复（断线期间用户的回复不能凭空消失）。"""
        now = time.monotonic()
        if now - self._last_recovery < RECOVERY_THROTTLE_SECONDS:
            return
        self._last_recovery = now
        try:
            result = self.adapter.recover_pending()
        except Exception:  # noqa: BLE001
            logger.warning("QQ 渠道补投递失败", exc_info=True)
            return
        if result.get("recovered"):
            logger.info(
                "QQ 渠道补投递：尝试 %s 条，失败 %s 条",
                result.get("recovered"), result.get("failed", 0),
            )

    # ---- 媒体链接 ----

    def _build_media_link(self, path: str) -> str:
        """把本地媒体变成带口令的可访问 URL（`<img>`/`<a>` 发不出鉴权头）。

        只在服务确实能从局域网/公网被访问时给出；仅本机监听时返回空串，由适配器
        明确告诉用户「无法在 QQ 内查看」而不是发一个打不开的死链。
        """
        from naiba.core.network import network_access_status

        raw = str(path or "").strip()
        if not raw:
            return ""
        host = str(getattr(self.app, "listener_host", "") or "")
        port = int((getattr(self.app, "config", None) and self.app.config.data.get("port")) or 0)
        if not port:
            return ""
        access = network_access_status(host, port)
        base = str(access.get("lan_url") or "")
        if not base:
            return ""
        token = str(self.app.config.data.get("access_token") or "")
        if not token:
            return ""
        import urllib.parse as urlparse

        query = urlparse.urlencode({"token": token, "path": raw})
        return f"{base}/api/file?{query}"

    # ---- 可观察状态 ----

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = self._state
            connection = self._binding
            server = self._server
            error = self._error
        listening = server is not None
        return {
            "enabled": bool(self.config().get("enabled")),
            "state": state,
            "listening": listening,
            "connected": connection is not None and not connection.closed.is_set(),
            "account_id": self._account_id,
            "error": error,
            "last_event_at": int(self._last_event_at * 1000) if self._last_event_at else 0,
        }

    # ---- 显式绑定（本机安全入口）----

    def bind_conversation(
        self,
        *,
        qq_user_id: str,
        conversation_id: str,
        qq_group_id: str = "",
        scope: str = "private",
        account_id: str = "",
    ) -> dict[str, Any]:
        """把 QQ 身份绑到 Cat Chat 会话；只允许从本机设置页调用（见 app 的 HTTP 处理）。

        「收到第一条消息就自动绑定」被明确禁止（计划 §3.4）：那等于让任何能投递
        事件的人把它自己的 QQ 号接到用户的会话上。

        绑定的主键包含 NapCat 的 bot 账号（``self_id``）：同一个人换一个小号 bot
        也应该是一条独立绑定，不能跨 bot 共用上下文。bot 还没连过时必须由调用方
        显式给出账号（本机设置页知道它登的是哪个号）。
        """
        account_id = (
            str(account_id or "").strip() or self._account_id
        )
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            raise ValueError("conversation_id 不能为空")
        if not self.app.storage.get_conversation(conversation_id, include_messages=False):
            raise ValueError("会话不存在")
        if scope not in {"private", "group"}:
            raise ValueError("scope 只能是 private 或 group")
        if not str(qq_user_id or "").strip():
            raise ValueError("QQ 号不能为空")
        if not account_id:
            raise ValueError("请填写 NapCat 登录的 bot QQ 号（或先让 NapCat 连上一次）")
        cfg = self.config()
        allowed = {str(item) for item in cfg.get("allowed_user_ids") or []}
        if str(qq_user_id) not in allowed:
            raise ValueError("该 QQ 号不在允许名单里，请先在 QQ 渠道设置里加入")
        binding = self.store.bind(
            account_id, scope, str(qq_user_id), conversation_id, str(qq_group_id or "")
        )
        return binding

    def unbind(
        self, *, qq_user_id: str, qq_group_id: str = "", scope: str = "private", account_id: str = ""
    ) -> bool:
        account = str(account_id or "").strip() or self._account_id
        if not account:
            return False
        return self.store.unbind(account, scope, str(qq_user_id), str(qq_group_id or ""))

    def bindings(self) -> list[dict[str, Any]]:
        return self.store.list_bindings()

    def bind_new_conversation(
        self,
        *,
        qq_user_id: str,
        qq_group_id: str = "",
        scope: str = "private",
        account_id: str = "",
        title: str = "",
    ) -> dict[str, Any]:
        """给一个 QQ 用户新建专用会话并立即绑定（计划 §3.4「会话首次创建」）。

        模型/Agent 只取 ``onebot.default_*`` 显式配置；都没配时退回全局默认模型/Agent。
        两者都拿不到有效模型时**拒绝创建**并回一句可执行的设置提示，而不是建一条
        发不出任何回复的空会话。
        """
        cfg = self.config()
        account = str(account_id or "").strip() or self._account_id
        if not account:
            # 先校验再建会话：否则绑不上的账号会留下一条谁也用不了的孤儿会话。
            raise ValueError("请填写 NapCat 登录的 bot QQ 号（或先让 NapCat 连上一次）")
        if not str(qq_user_id or "").strip():
            raise ValueError("QQ 号不能为空")
        if scope not in {"private", "group"}:
            raise ValueError("scope 只能是 private 或 group")
        if str(qq_user_id) not in {str(item) for item in cfg.get("allowed_user_ids") or []}:
            raise ValueError("该 QQ 号不在允许名单里，请先在 QQ 渠道设置里加入")
        model_key = str(cfg.get("default_model_key") or "").strip()
        model_name = str(cfg.get("default_model_name") or "").strip()
        agent_id = str(cfg.get("default_agent_id") or "").strip()
        provider_id = ""
        if not model_key:
            # 没有渠道专属默认：沿用全局默认模型（用户已经在设置页选过的那个）。
            model_key = self.app.config.default_model_key()
        if not model_key:
            raise ValueError(
                "还没有可用模型：请先在 Cat Chat 设置页的模型检查里选好默认模型，"
                "或在 QQ 渠道配置里填写默认模型"
            )
        kind, _, model_id = model_key.partition(":")
        if kind in {"online", "local"} and model_id:
            provider_id = model_id
        if not model_name:
            # 名称必须与 model_key 指向的档案一致，否则会话首轮会被二次校验拒绝。
            for profile in self.app.config.model_profiles():
                if str(profile.get("model_key") or "") == model_key:
                    model_name = str(profile.get("model") or "").strip()
                    break
        if not model_name:
            raise ValueError("默认模型没有可用的模型名，请先在设置页重新选择一次默认模型")
        if not agent_id:
            agent_id = self.app.config.default_agent_id()
        if agent_id and not self.app.config.get_agent(agent_id):
            agent_id = self.app.config.default_agent_id()
        label = str(title or "").strip() or f"QQ {qq_user_id}"
        conversation = self.app.storage.create_conversation(
            title=label[:80],
            provider_id=provider_id,
            agent_id=agent_id,
            interaction_mode="craft",
            model_key=model_key,
            model_name=model_name,
            # 权限档刻意锁 confirm：换成 QQ 通道不扩大权限，工具仍要经过确认（计划 §一）。
            permission_mode="confirm",
        )
        self.store.bind(
            account,
            scope, str(qq_user_id), str(conversation.get("id") or ""), str(qq_group_id or ""),
        )
        return conversation


class _ConnectionProxy:
    """把「当前连接」包成适配器要的 Sender：没有连接时动作一律失败。"""

    def __init__(self, service: OneBotChannelService):
        self._service = service

    def call_action(self, action: str, params: dict[str, Any]) -> dict[str, Any] | None:
        with self._service._lock:
            connection = self._service._binding
        if connection is None or connection.closed.is_set():
            logger.info("QQ 渠道没有活动连接，动作 %s 未发送", action)
            return None
        return connection.call_action(action, params)
