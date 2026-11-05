# -*- coding: utf-8 -*-
"""QQ ↔ Cat Chat 会话适配层：入站消息 → Run → 完整回复 → QQ 投递。

职责边界（计划 §3.1）：本模块**不**执行任何工具、不改模型调用、不重写权限判定。
它只做四件事：
1. 判断这条 QQ 消息该不该进 Cat Chat（白名单 / 绑定 / 去重）；
2. 把消息段换成 ``submit_chat()`` 能接受的正文字与附件；
3. 等 Run 到终态后，从存储读**完整** assistant 消息（不依赖可能分片的 delta）；
4. 按 QQ 单条上限切段投递，并逐段记录投递状态。

为什么"等终态再读存储"：Run 的流式 delta 是给浏览器逐帧渲染用的，可能被截断、
分片，甚至中途丢包。QQ 这边要的是最终成品，所以只认落库后的完整消息（计划 §4.2）。
"""

from __future__ import annotations

import logging
import re
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Protocol

from naiba.channels import onebot, store as channel_store
from naiba.channels.onebot import ParsedMessage

logger = logging.getLogger("naiba.channels.adapter")

# 单次媒体下载上限（与通用上传同源：超过就不进 Cat Chat，回告用户）。
MAX_MEDIA_BYTES = 80 * 1024 * 1024
MEDIA_DOWNLOAD_TIMEOUT = 60.0
# 单个 QQ 用户一次最多带几个附件（防刷屏/防一次拉爆 uploads）。
MAX_MEDIA_ATTACHMENTS = 8
# 工具确认凭据有效期：超时后不可批准，必须回 Cat Chat 网页处理。
CONFIRM_TTL_SECONDS = 300
# 等 Run 终态的兜底超时（小时级任务不该被 QQ 通道无限占线程）。
RUN_WAIT_TIMEOUT_SECONDS = 3600.0
RUN_WAIT_POLL_SECONDS = 0.25

# 「确认 <id>」/「拒绝 <id>」——只认这两个词，别让任意文本变成授权。
_CONFIRM_RE = re.compile(r"^\s*(确认|批准|同意|approve)\s*[:：]?\s*([A-Za-z0-9_\-]{4,64})\s*$", re.I)
_REJECT_RE = re.compile(r"^\s*(拒绝|取消|不同意|reject|deny)\s*[:：]?\s*([A-Za-z0-9_\-]{4,64})\s*$", re.I)

# 允许下载的媒体 URL 协议：只认 http/https，绝不接受 file:// 或裸本地路径。
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


class Sender(Protocol):
    """向 NapCat 发请求的最小面（生产实现走反向 WebSocket）。"""

    def call_action(self, action: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """调用 OneBot API；不可达时返回 None（不抛异常，由调用方记失败）。"""
        ...


class UnsupportedMediaError(RuntimeError):
    """媒体段在协议/宿主能力之外，或来源不可信。"""


def is_local_path_like(value: str) -> bool:
    """OneBot 事件里的"路径"一律不可信：认出盘符/UNC/相对路径/绝对路径。"""
    text = str(value or "").strip()
    if not text:
        return False
    if text.startswith(("\\\\", "//", "/", "./", "../", "~")):
        return True
    return bool(re.match(r"^[A-Za-z]:[\\/]", text))


class OneBotAdapter:
    """单实例适配器（由 ``OneBotChannelService`` 持有并传入 sender）。"""

    def __init__(
        self,
        app: Any,
        channel_store_handle: channel_store.ChannelStore,
        sender: Sender,
        *,
        account_id_getter: Callable[[], str] | None = None,
        media_link_builder: Callable[[str], str] | None = None,
    ):
        self.app = app
        self.store = channel_store_handle
        self.sender = sender
        self._account_id_getter = account_id_getter or (lambda: "")
        self._media_link_builder = media_link_builder
        # 活动 Run 的投递线程（应用关闭时 join，计划 §3.2）。
        self._threads: dict[str, threading.Thread] = {}
        self._threads_lock = threading.Lock()
        self._stop = threading.Event()

    # ---- 生命周期 ----

    def start(self) -> None:
        """重新武装适配器（服务重启/设置保存后调用）。

        没有这一步，一次 ``stop()`` 会把 ``_stop`` 永久置位：之后所有消息处理都
        在 ``_wait_for_terminal`` / ``deliver_pending`` 里直接返回，表现为「NapCat
        连上了、Run 也跑了，但 QQ 永远收不到回复」——只有重启整个应用才能恢复。
        """
        self._stop.clear()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        with self._threads_lock:
            threads = list(self._threads.values())
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    def _spawn(self, key: str, target: Callable[[], None]) -> None:
        def runner() -> None:
            try:
                target()
            except Exception:  # noqa: BLE001 - 单条消息处理失败不能拖垮连接线程
                logger.exception("QQ 消息处理失败")
            finally:
                with self._threads_lock:
                    self._threads.pop(key, None)

        thread = threading.Thread(target=runner, name=f"onebot-msg-{key}", daemon=True)
        with self._threads_lock:
            self._threads[key] = thread
        thread.start()

    # ---- 配置读取 ----

    def config(self) -> dict[str, Any]:
        getter = getattr(self.app.config, "onebot_config", None)
        if callable(getter):
            return getter()
        return dict(getattr(self.app.config, "data", {}).get("onebot") or {})

    # ---- 发送 ----

    def send_text(
        self, scope: str, user_id: str, group_id: str, text: str
    ) -> bool:
        """发一条纯文本；群聊走 group_id，私聊走 user_id。"""
        params: dict[str, Any] = {"message": text, "auto_escape": False}
        action = "send_group_msg" if scope == "group" else "send_private_msg"
        if scope == "group":
            params["group_id"] = int(group_id)
        else:
            params["user_id"] = int(user_id)
        return self.sender.call_action(action, params) is not None

    def _notify(self, message: ParsedMessage, text: str) -> None:
        """回告用户（发送失败只记日志：回告本身失败不该再报错给谁）。"""
        try:
            self.send_text(message.scope, message.qq_user_id, message.qq_group_id, text)
        except Exception:  # noqa: BLE001
            logger.warning("QQ 回告发送失败", exc_info=True)

    # ---- 入站总入口 ----

    def handle_event(self, raw: Any) -> None:
        """处理一条 OneBot 事件（消息 / 心跳 / 生命周期）。

        非消息事件只更新连接状态，不进入任何业务分支。
        """
        if not isinstance(raw, dict):
            return
        post_type = str(raw.get("post_type") or "").strip().lower()
        if post_type != "message":
            # meta_event / notice / request：不处理，避免把无关事件当指令。
            return
        try:
            message = onebot.parse_message_event(raw)
        except onebot.OneBotProtocolError as exc:
            logger.info("忽略非法 OneBot 事件：%s", exc)
            return
        # 重连后 NapCat 会重推事件：已处理过的直接跳过，绝不再建 Run（计划 §4.1）。
        record, created = self.store.record_message(
            message.account_id, message.message_id,
            scope=message.scope, qq_user_id=message.qq_user_id,
            qq_group_id=message.qq_group_id,
        )
        if not created:
            logger.info("跳过重复 OneBot 事件 message_id=%s", message.message_id)
            return
        key = f"{message.account_id}:{message.message_id}"
        self._spawn(key, lambda: self._process(message, record))

    def recover_incomplete(self, older_than_ms: int = 60_000) -> int:
        """把「收到但没提交成 Run」的残留记录标成失败（计划 §5 崩溃后识别未完成记录）。

        崩溃可能停在 record_message 之后、submit_chat 之前：那条记录永远是
        ``received``，既不会重复投递也不会有结果。这里把它标成 failed 并写清原因，
        让运维/用户在状态里看得到，而不是留一条永远"进行中"的幽灵。

        ``older_than_ms`` 默认留一分钟宽限：设置页保存会触发监听重启，此刻可能有
        正常的消息正处在「已记录、正要提交」的窗口里，不能把它们误判成崩溃残留。
        """
        count = 0
        for record in self.store.received_messages(older_than_ms=older_than_ms):
            self.store.update_message(
                str(record.get("channel_account_id") or ""),
                str(record.get("message_id") or ""),
                status=channel_store.MESSAGE_FAILED,
                detail="interrupted_before_submit",
            )
            count += 1
        if count:
            logger.warning("QQ 渠道：%s 条入站消息在提交前中断，已标记失败", count)
        return count

    # ---- 白名单与绑定 ----

    def _authorized(self, message: ParsedMessage, cfg: dict[str, Any]) -> str | None:
        """返回 ``""`` 表示放行；非空字符串是回告原因；``None`` 表示**静默忽略**。

        ``None`` 只用于「群里正常聊天」这一种情况：它不是给 bot 的消息，既不该触发
        Run，也不该在群里回一句提示刷屏（计划 §3.3 的 @ 触发规则）。
        """
        allowed_users = {str(item) for item in cfg.get("allowed_user_ids") or []}
        if message.scope == "group":
            if not cfg.get("allow_groups"):
                return "群聊暂未开放，请私聊使用。"
            if str(message.qq_group_id) not in {
                str(item) for item in cfg.get("allowed_group_ids") or []
            }:
                return "这个群不在允许名单里。"
            # 触发判定排在身份回告之前：群里的无关聊天不该收到任何回复。
            if not message.triggered:
                return None
        if message.qq_user_id not in allowed_users:
            return (
                "这个 QQ 号还没有被允许使用 Cat Chat。"
                if message.scope == "private"
                else "你不是被允许使用 Cat Chat 的用户。"
            )
        return ""

    def _binding(self, message: ParsedMessage) -> dict[str, Any] | None:
        return self.store.binding_for(
            message.account_id, message.scope, message.qq_user_id, message.qq_group_id
        )

    # ---- 主流程 ----

    def _process(self, message: ParsedMessage, record: dict[str, Any]) -> None:
        cfg = self.config()
        denial = self._authorized(message, cfg)
        if denial is None:
            # 群聊里没 @ 机器人的消息：静默跳过，只留一条可查记录。
            self.store.update_message(
                message.account_id, message.message_id,
                status=channel_store.MESSAGE_REJECTED, detail="group_not_triggered",
            )
            return
        if denial:
            self.store.update_message(
                message.account_id, message.message_id,
                status=channel_store.MESSAGE_REJECTED, detail=denial,
            )
            self._notify(message, denial)
            return

        binding = self._binding(message)
        if not binding:
            # 不把第一条消息当成可信绑定：必须由用户在电脑端显式绑定（计划 §3.4）。
            hint = (
                "尚未绑定 Cat Chat 会话。请在运行 Cat Chat 的电脑上把本 QQ 号绑定到一个会话，"
                "然后再发消息。"
            )
            self.store.update_message(
                message.account_id, message.message_id,
                status=channel_store.MESSAGE_REJECTED, detail="unbound",
            )
            self._notify(message, hint)
            return
        conversation_id = str(binding.get("conversation_id") or "")
        if not conversation_id:
            self.store.update_message(
                message.account_id, message.message_id,
                status=channel_store.MESSAGE_REJECTED, detail="empty_binding",
            )
            self._notify(message, "绑定记录已失效，请在电脑端重新绑定。")
            return

        # 工具确认命令优先：它不新建 Run，只把决定转给现有确认链路。
        decision = self._confirm_command(message, cfg)
        if decision is not None:
            self.store.update_message(
                message.account_id, message.message_id,
                status=channel_store.MESSAGE_REPLIED, detail="confirmation",
                conversation_id=conversation_id, run_id=str(decision.get("run_id") or ""),
            )
            return

        if not message.text and not message.media:
            self.store.update_message(
                message.account_id, message.message_id,
                status=channel_store.MESSAGE_REJECTED, detail="empty_message",
            )
            self._notify(message, "这条消息里没有可以处理的文字或附件。")
            return
        if message.unsupported:
            kinds = "、".join(sorted(set(message.unsupported)))
            self._notify(
                message,
                f"以下消息类型暂时无法处理：{kinds}。"
                + ("其余内容会照常处理。" if (message.text or message.media) else "请改用文字发送。"),
            )
            if not message.text and not message.media:
                self.store.update_message(
                    message.account_id, message.message_id,
                    status=channel_store.MESSAGE_REJECTED, detail="unsupported_only",
                )
                return

        attachments, media_error = self._collect_attachments(message)
        if media_error:
            self._notify(message, media_error)
            if not message.text and not attachments:
                self.store.update_message(
                    message.account_id, message.message_id,
                    status=channel_store.MESSAGE_REJECTED, detail="media_failed",
                )
                return

        try:
            run = self.app.runs.submit_chat({
                "conversation_id": conversation_id,
                "message": message.text,
                "attachments": attachments,
            })
        except Exception as exc:  # noqa: BLE001 - 各类提交失败都要回告并留状态
            self._handle_submit_error(message, exc, conversation_id)
            return

        run_id = str(run.get("id") or "")
        self.store.update_message(
            message.account_id, message.message_id,
            status=channel_store.MESSAGE_SUBMITTED, conversation_id=conversation_id,
            run_id=run_id, detail="",
        )
        self._await_and_reply(message, conversation_id, run_id)

    def _handle_submit_error(
        self, message: ParsedMessage, exc: Exception, conversation_id: str
    ) -> None:
        from naiba.core.exceptions import ActiveRunError

        if isinstance(exc, ActiveRunError):
            active = str(exc.run_id or "")
            text = "这个会话上一条回复还在进行中，请等它结束再发。"
            self.store.update_message(
                message.account_id, message.message_id, status=channel_store.MESSAGE_REJECTED,
                detail=f"active_run:{active}", conversation_id=conversation_id, run_id=active,
            )
        elif isinstance(exc, LookupError):
            text = "绑定的 Cat Chat 会话已不存在，请在电脑端重新绑定。"
            self.store.update_message(
                message.account_id, message.message_id, status=channel_store.MESSAGE_REJECTED,
                detail="conversation_missing", conversation_id=conversation_id,
            )
        elif isinstance(exc, ValueError):
            # 最常见的是「还没选模型」：这是配置问题，必须让用户看到可执行的办法。
            text = f"无法开始回答：{exc}"
            self.store.update_message(
                message.account_id, message.message_id, status=channel_store.MESSAGE_REJECTED,
                detail=f"invalid:{str(exc)[:400]}", conversation_id=conversation_id,
            )
        else:
            logger.exception("QQ 消息提交 Run 失败")
            text = "提交失败，请稍后再试。"
            self.store.update_message(
                message.account_id, message.message_id, status=channel_store.MESSAGE_FAILED,
                detail=f"submit:{type(exc).__name__}", conversation_id=conversation_id,
            )
        self._notify(message, text)

    # ---- 媒体入站 ----

    def _collect_attachments(
        self, message: ParsedMessage
    ) -> tuple[list[dict[str, Any]], str]:
        """把消息段里的媒体下载进受管 uploads，返回 (附件, 错误说明)。

        只从 NapCat 给的 http(s) 地址或 OneBot 下载接口取字节；事件里的任何本地路径
        一律拒绝（计划 §4.1「禁止直接信任 OneBot 事件里任意本地路径」）。
        """
        if not message.media:
            return [], ""
        from naiba.storage.media import store_uploaded_file

        attachments: list[dict[str, Any]] = []
        failures: list[str] = []
        for index, segment in enumerate(message.media[:MAX_MEDIA_ATTACHMENTS]):
            try:
                data, filename = self._download_media(message, segment)
                _validate_media_payload(str(segment.get("kind") or "file"), data)
            except UnsupportedMediaError as exc:
                failures.append(f"{segment.get('kind')}：{exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - 网络/超时/体积都要转成人话
                failures.append(f"{segment.get('kind')}：{exc}")
                continue
            imaging = dict(getattr(self.app.config, "data", {}).get("imaging") or {})
            saved = store_uploaded_file(
                data, filename, self.app.paths.data_dir, imaging
            )
            attachments.append({
                "name": str(saved.get("name") or filename),
                "path": str(saved.get("path") or ""),
                "size": int(saved.get("size") or len(data)),
                "thumb_path": str(saved.get("thumb_path") or ""),
            })
            if index + 1 >= MAX_MEDIA_ATTACHMENTS and len(message.media) > MAX_MEDIA_ATTACHMENTS:
                failures.append(
                    f"一次最多处理 {MAX_MEDIA_ATTACHMENTS} 个附件，其余未处理"
                )
        if failures:
            prefix = "部分附件没能接收：" if attachments else "附件没能接收："
            return attachments, prefix + "；".join(failures[:4])
        return attachments, ""

    def _download_media(
        self, message: ParsedMessage, segment: dict[str, Any]
    ) -> tuple[bytes, str]:
        """取一个媒体段的字节。返回 (data, 保存用文件名)。"""
        kind = str(segment.get("kind") or "file")
        url = str(segment.get("url") or "").strip()
        if url:
            return self._fetch_url(url, kind, str(segment.get("name") or ""))
        # 没有直链就走 OneBot 下载接口（NapCat 本地缓存 → 返回 file 路径或 base64）。
        file_id = str(segment.get("file_id") or segment.get("file") or "").strip()
        if not file_id:
            raise UnsupportedMediaError("缺少下载地址或文件标识")
        action = {
            "image": "get_image",
            "audio": "get_record",
            "video": "get_file",
            "file": "get_file",
        }.get(kind, "get_file")
        payload: dict[str, Any] = {"file": file_id}
        if message.scope == "group" and message.qq_group_id:
            payload["group_id"] = int(message.qq_group_id)
        response = self.sender.call_action(action, payload)
        if not isinstance(response, dict):
            raise UnsupportedMediaError("NapCat 没有返回这个文件")
        # OneBot 动作响应是信封 {status, retcode, data}：文件本体在 data 里。
        # 有些实现会把 data 直接摊平，两种都接。
        data = response.get("data") if isinstance(response.get("data"), dict) else response
        # 优先 base64（跨机器可用）；file 字段可能是本地路径，必须校验后才能读。
        import base64

        encoded = str(data.get("base64") or "").strip()
        if encoded:
            try:
                return base64.b64decode(encoded, validate=True), self._safe_filename(
                    kind, str(segment.get("name") or data.get("file") or "")
                )
            except (ValueError, TypeError) as exc:
                raise UnsupportedMediaError(f"返回内容无法解码：{exc}") from exc
        remote_url = str(data.get("url") or "").strip()
        if remote_url:
            return self._fetch_url(remote_url, kind, str(segment.get("name") or ""))
        local = str(data.get("file") or "").strip()
        if local and not is_local_path_like(local):
            # 相对文件名仍可能是 NapCat 的本地缓存路径，不可信。
            raise UnsupportedMediaError("NapCat 只给了本地路径，未提供可下载地址")
        raise UnsupportedMediaError("NapCat 没有提供可下载的地址")

    def _fetch_url(self, url: str, kind: str, name: str) -> tuple[bytes, str]:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme.lower() not in _ALLOWED_URL_SCHEMES or not parts.hostname:
            raise UnsupportedMediaError("只支持 http/https 下载地址")
        from naiba import net as net_io

        chunks: list[bytes] = []
        total = 0
        try:
            with net_io.open(url, timeout=MEDIA_DOWNLOAD_TIMEOUT) as response:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_MEDIA_BYTES:
                        raise UnsupportedMediaError(
                            f"文件超过 {MAX_MEDIA_BYTES // (1024 * 1024)} MB 上限"
                        )
                    chunks.append(chunk)
        except UnsupportedMediaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise UnsupportedMediaError(f"下载失败：{exc}") from exc
        return b"".join(chunks), self._safe_filename(kind, name or parts.path.rsplit("/", 1)[-1])

    @staticmethod
    def _safe_filename(kind: str, name: str) -> str:
        """给下载到的字节一个安全文件名：只留 basename，缺扩展名时按类型补。"""
        base = Path(str(name or "").replace("\\", "/")).name.strip()
        base = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]", "_", base)[:120]
        if not base:
            base = f"qq_{kind}"
        if "." not in base:
            base = f"{base}{onebot.MEDIA_EXTENSIONS.get(kind, '.bin')}"
        return base

    # ---- 等 Run 终态并投递 ----

    def _await_and_reply(
        self, message: ParsedMessage, conversation_id: str, run_id: str
    ) -> None:
        run = self._wait_for_terminal(run_id)
        status = str((run or {}).get("status") or "")
        if self._stop.is_set():
            return
        if status == "cancelled":
            self._void_run_confirmations(run_id)
            self._deliver(message, conversation_id, run_id, "这一轮回答已被取消。")
            return
        if status in {"failed", "interrupted"}:
            self._void_run_confirmations(run_id)
            reason = str((run or {}).get("error") or "").strip()
            detail = str(((run or {}).get("detail") or {}).get("message") or "").strip()
            text = reason or detail or "这一轮回答没有正常完成。"
            self._deliver(message, conversation_id, run_id, text)
            return
        self._void_run_confirmations(run_id)
        reply = self._read_reply(conversation_id, run_id)
        if not reply.strip():
            reply = "这一轮没有产生可发送的回复内容。"
        self._deliver(message, conversation_id, run_id, reply)

    def _wait_for_terminal(self, run_id: str) -> dict[str, Any] | None:
        """等 Run 到终态；期间把待确认工具同步到 QQ（计划 §4.3）。"""
        deadline = time.monotonic() + RUN_WAIT_TIMEOUT_SECONDS
        announced: set[str] = set()
        run: dict[str, Any] | None = None
        while not self._stop.is_set() and time.monotonic() < deadline:
            run = self.app.runs.get(run_id)
            if not run:
                return None
            self._sync_confirmations(run, announced)
            if str(run.get("status") or "") in self.app.runs.TERMINAL:
                return run
            time.sleep(RUN_WAIT_POLL_SECONDS)
        return run

    def _sync_confirmations(self, run: dict[str, Any], announced: set[str]) -> None:
        """把 Run 的有效待确认项登记成 QQ 凭据并通知绑定用户。"""
        run_id = str(run.get("id") or "")
        if not run_id:
            return
        try:
            snapshot = self.app.runs.confirmation_snapshot(run_id)
        except Exception:  # noqa: BLE001 - 确认面读不出来不该中断本轮投递
            return
        conversation_id = str(run.get("conversation_id") or "")
        binding = self._binding_for_conversation(run_id)
        if not binding:
            return
        for item in snapshot or []:
            confirm_id = str(item.get("confirm_id") or "")
            if not confirm_id or confirm_id in announced:
                continue
            if str(item.get("status") or "") != "pending":
                continue
            tool = str(item.get("tool") or "")
            params = item.get("parameters") or {}
            summary = self._confirm_summary(params)
            registered = self.store.register_confirmation(
                str(binding.get("channel_account_id") or ""), confirm_id,
                run_id=run_id, conversation_id=conversation_id,
                qq_user_id=str(binding.get("qq_user_id") or ""),
                qq_group_id=str(binding.get("qq_group_id") or ""),
                tool=tool, ttl_seconds=CONFIRM_TTL_SECONDS,
            )
            if not registered:
                continue
            announced.add(confirm_id)
            lines = [
                f"工具 {tool} 需要你确认。",
            ]
            if summary:
                lines.append(f"操作摘要：{summary}")
            lines.append(f"确认编号：{confirm_id}")
            lines.append(f"有效期：{CONFIRM_TTL_SECONDS // 60} 分钟")
            lines.append(f"回复「确认 {confirm_id}」执行，或「拒绝 {confirm_id}」放弃。")
            lines.append("无法判断风险时，请回 Cat Chat 网页处理。")
            self.send_text(
                str(binding.get("scope") or "private"),
                str(binding.get("qq_user_id") or ""),
                str(binding.get("qq_group_id") or ""),
                "\n".join(lines),
            )

    @staticmethod
    def _confirm_summary(params: Any) -> str:
        """只给必要字段的安全摘要（路径/命令），不搬运自由文本参数。"""
        if not isinstance(params, dict):
            return ""
        parts = []
        for key in ("path", "command", "file", "target"):
            value = str(params.get(key) or "").strip()
            if value:
                parts.append(f"{key}={value[:120]}")
        return "，".join(parts)

    def _binding_for_conversation(self, run_id: str) -> dict[str, Any] | None:
        """按 Run 的会话反查绑定（确认只发给发起该会话的已绑定 QQ 用户）。"""
        run = self.app.runs.get(run_id) or {}
        conversation_id = str(run.get("conversation_id") or "")
        if not conversation_id:
            return None
        for binding in self.store.list_bindings():
            if str(binding.get("conversation_id") or "") == conversation_id:
                return binding
        return None

    def _void_run_confirmations(self, run_id: str) -> None:
        try:
            self.store.void_confirmations_for_run(run_id)
        except Exception:  # noqa: BLE001
            logger.warning("清理 QQ 确认凭据失败", exc_info=True)

    def _read_reply(self, conversation_id: str, run_id: str) -> str:
        """从存储读该 Run 对应的**完整** assistant 消息。"""
        try:
            conversation = self.app.storage.get_conversation(
                conversation_id, include_trace=False
            ) or {}
        except Exception:  # noqa: BLE001
            logger.exception("读取会话失败")
            return ""
        messages = conversation.get("messages") or []
        for item in reversed(messages):
            if str(item.get("role") or "") != "assistant":
                continue
            metadata = item.get("metadata") or {}
            if str(metadata.get("run_id") or "") == run_id:
                return str(item.get("content") or "")
        # run_id 对不上时退化成最后一条 assistant（老数据/异常收尾），但绝不返回空。
        for item in reversed(messages):
            if str(item.get("role") or "") == "assistant":
                return str(item.get("content") or "")
        return ""

    # ---- 投递 ----

    def _deliver(
        self, message: ParsedMessage, conversation_id: str, run_id: str, text: str
    ) -> None:
        body = text
        links = self._media_links_for_run(conversation_id, run_id)
        if links:
            body = f"{body}\n\n" + "\n".join(links)
        segments = onebot.split_message(body)
        if len(segments) > onebot.MAX_MESSAGE_SEGMENTS:
            keep = onebot.MAX_MESSAGE_SEGMENTS - 1
            dropped = len(segments) - keep
            segments = segments[:keep] + [f"（回复太长，另有 {dropped} 段未发送，请回 Cat Chat 网页查看完整内容。）"]
        self.store.prepare_deliveries(
            message.account_id, message.message_id,
            run_id=run_id, conversation_id=conversation_id,
            scope=message.scope, qq_user_id=message.qq_user_id,
            qq_group_id=message.qq_group_id, segments=segments,
        )
        self.deliver_pending(message.account_id, message.message_id)

    def deliver_pending(self, account_id: str, message_id: str) -> bool:
        """补发该消息所有未确认送达的分段；全部成功返回 True。

        逐段状态落库：已 sent 的段不会重发，失败的段下次只补自己（计划 §4.2）。
        """
        rows = self.store.deliveries_for(account_id, message_id)
        for row in rows:
            if str(row.get("status") or "") == channel_store.DELIVERY_SENT:
                continue
            if self._stop.is_set():
                return False
            self.store.mark_delivery(
                str(row["delivery_id"]), channel_store.DELIVERY_SENDING, increment_attempt=True
            )
            ok = self.send_text(
                str(row.get("scope") or "private"),
                str(row.get("qq_user_id") or ""),
                str(row.get("qq_group_id") or ""),
                str(row.get("content") or ""),
            )
            self.store.mark_delivery(
                str(row["delivery_id"]),
                channel_store.DELIVERY_SENT if ok else channel_store.DELIVERY_FAILED,
                error="" if ok else "send_failed",
            )
            if not ok:
                return False
        all_sent = all(
            str(item.get("status") or "") == channel_store.DELIVERY_SENT
            for item in self.store.deliveries_for(account_id, message_id)
        )
        if all_sent:
            self.store.update_message(
                account_id, message_id, status=channel_store.MESSAGE_REPLIED
            )
        return all_sent

    def _media_links_for_run(self, conversation_id: str, run_id: str) -> list[str]:
        """把本轮生成的媒体变成可访问链接（首期口径，计划 §4.2）。

        链接必须带 Cat Chat 的口令（``<img>``/``<a>`` 发不出 Authorization 头），
        否则局域网/外网都打不开。取不到可用地址时明确说明无法访问。
        """
        builder = self._media_link_builder
        if not callable(builder):
            return []
        try:
            conversation = self.app.storage.get_conversation(
                conversation_id, include_trace=False
            ) or {}
        except Exception:  # noqa: BLE001
            return []
        links: list[str] = []
        for item in reversed(conversation.get("messages") or []):
            if str(item.get("role") or "") != "assistant":
                continue
            metadata = item.get("metadata") or {}
            if str(metadata.get("run_id") or "") != run_id:
                continue
            for media in metadata.get("attachments") or []:
                if not isinstance(media, dict):
                    continue
                path = str(media.get("path") or media.get("source") or "").strip()
                if not path:
                    continue
                url = builder(path)
                if url:
                    name = str(media.get("name") or Path(path).name)
                    links.append(f"{name}：{url}")
            break
        if not links and _has_media_metadata(conversation, run_id):
            return ["本轮产出了媒体文件，但当前没有可访问的 Cat Chat 地址（服务仅本机监听），无法在 QQ 内查看。"]
        return links

    # ---- QQ 内工具确认 ----

    def _confirm_command(
        self, message: ParsedMessage, cfg: dict[str, Any]
    ) -> dict[str, Any] | None:
        """把「确认/拒绝 <id>」翻译成现有确认链路调用。返回已处理的结果字典。"""
        text = message.text
        if not text:
            return None
        approve = _CONFIRM_RE.match(text)
        reject = _REJECT_RE.match(text) if not approve else None
        if not approve and not reject:
            return None
        confirm_id = str((approve or reject).group(2))
        account_id = message.account_id
        record = self.store.get_confirmation(account_id, confirm_id)
        if not record:
            self._notify(message, f"没有找到确认编号 {confirm_id}（可能已过期或被处理）。")
            return {"run_id": ""}
        # 只接受**原绑定那条私聊会话**的主人：换人、换号、群聊一律拒绝。
        if message.scope != "private" or str(record.get("qq_user_id")) != message.qq_user_id:
            self._notify(message, "这条确认只能由收到通知的私聊用户处理。")
            return {"run_id": str(record.get("run_id") or "")}
        run_id = str(record.get("run_id") or "")
        # 凭据必须仍然有效（未过期、Run 未终结）：失效即不可批准（计划 §4.3）。
        if not self._confirmation_still_live(record):
            self.store.resolve_confirmation(account_id, confirm_id, channel_store.CONFIRM_EXPIRED)
            self._notify(message, "这条确认已失效，请回 Cat Chat 网页处理。")
            return {"run_id": run_id}
        if approve:
            result = self.app.runs.confirm_tool_async(run_id, confirm_id, None)
            if result is None:
                self.store.resolve_confirmation(account_id, confirm_id, channel_store.CONFIRM_VOID)
                self._notify(message, "确认请求已失效，请回 Cat Chat 网页处理。")
                return {"run_id": run_id}
            self.store.resolve_confirmation(account_id, confirm_id, channel_store.CONFIRM_APPROVED)
            ok, detail = result
            self._notify(message, f"已确认执行 {record.get('tool') or '工具'}。" if ok else f"确认未通过：{detail}")
        else:
            result = self.app.runs.reject_tool(run_id, confirm_id)
            if result is None:
                self.store.resolve_confirmation(account_id, confirm_id, channel_store.CONFIRM_VOID)
                self._notify(message, "确认请求已失效，请回 Cat Chat 网页处理。")
                return {"run_id": run_id}
            self.store.resolve_confirmation(account_id, confirm_id, channel_store.CONFIRM_REJECTED)
            self._notify(message, f"已拒绝执行 {record.get('tool') or '工具'}。")
        return {"run_id": run_id}

    def _confirmation_still_live(self, record: dict[str, Any]) -> bool:
        """凭据仍可批准：未过期、Run 还活着、且这个确认仍在活快照里。"""
        if str(record.get("status") or "") != channel_store.CONFIRM_PENDING:
            return False
        try:
            expires_at = int(record.get("expires_at") or 0)
        except (TypeError, ValueError):
            return False
        if expires_at <= channel_store.now_ms():
            return False
        run_id = str(record.get("run_id") or "")
        run = self.app.runs.get(run_id)
        if not run or str(run.get("status") or "") in self.app.runs.TERMINAL:
            return False
        try:
            snapshot = self.app.runs.confirmation_snapshot(run_id)
        except Exception:  # noqa: BLE001
            return False
        confirm_id = str(record.get("confirm_id") or "")
        return any(str(item.get("confirm_id") or "") == confirm_id for item in snapshot or [])

    # ---- 重启对账 ----

    def recover_pending(self) -> dict[str, int]:
        """进程重启后补投递已生成但没发完的回复（计划 §5）。

        失效批次的确认凭据由 ``void_all_confirmations`` 在服务启动时统一作废，
        这里**不**把它们恢复成可批准状态。
        """
        recovered = 0
        failed = 0
        for record in self.store.pending_replies():
            account_id = str(record.get("channel_account_id") or "")
            message_id = str(record.get("message_id") or "")
            if not account_id or not message_id:
                continue
            deliveries = self.store.deliveries_for(account_id, message_id)
            if not deliveries:
                # 已提交 Run 但还没登记分段：走完整读取+投递流程。
                conversation_id = str(record.get("conversation_id") or "")
                run_id = str(record.get("run_id") or "")
                run = self.app.runs.get(run_id) if run_id else None
                if run and str(run.get("status") or "") in self.app.runs.TERMINAL:
                    reply = self._read_reply(conversation_id, run_id)
                    if reply.strip():
                        recovered += 1
                        self._spawn(
                            f"recover:{account_id}:{message_id}",
                            lambda m=record, c=conversation_id, r=run_id, t=reply: self._deliver_record(m, c, r, t),
                        )
                continue
            recovered += 1
            if not self.deliver_pending(account_id, message_id):
                failed += 1
        return {"recovered": recovered, "failed": failed}

    def _deliver_record(
        self, record: dict[str, Any], conversation_id: str, run_id: str, text: str
    ) -> None:
        message = ParsedMessage(
            account_id=str(record.get("channel_account_id") or ""),
            message_id=str(record.get("message_id") or ""),
            scope=str(record.get("scope") or "private"),
            qq_user_id=str(record.get("qq_user_id") or ""),
            qq_group_id=str(record.get("qq_group_id") or ""),
            text="",
        )
        self._deliver(message, conversation_id, run_id, text)


def _validate_media_payload(kind: str, data: bytes) -> None:
    """下载到的字节必须与声称的类型对得上（计划 §4.1「限制文件类型」）。

    只做「魔数对得上」这一层：QQ 通道是用户自己的账号，威胁模型不是恶意上传，
    而是**类型错标**（例如把可执行文件当图片塞进来），错标后落进 uploads 会被
    下游当成图片/视频去解码，报的是看不懂的解码错误。
    """
    if not data:
        raise UnsupportedMediaError("文件为空")
    signatures = {
        "image": (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF8", b"RIFF", b"BM", b"II*\x00", b"MM\x00*"),
        "video": (b"\x00\x00\x00", b"RIFF", b"ftyp", b"\x1a\x45\xdf\xa3"),
        "audio": (b"RIFF", b"OggS", b"ID3", b"\xff\xf1", b"\xff\xfb", b"#!AMR", b"fLaC", b"\x00\x00\x00"),
    }
    expected = signatures.get(kind)
    if expected is None:
        return
    if kind == "video":
        # mp4/mov 的 ftyp 在第 4..8 字节，webm 走 EBML 头。
        head = data[:16]
        if b"ftyp" in head or head.startswith(b"\x1a\x45\xdf\xa3") or head.startswith(b"RIFF"):
            return
        raise UnsupportedMediaError("内容不像视频文件")
    if not any(data.startswith(signature) for signature in expected):
        raise UnsupportedMediaError(f"内容不像{ {'image':'图片','audio':'音频'}.get(kind, kind) }文件")


def _has_media_metadata(conversation: dict[str, Any], run_id: str) -> bool:
    for item in conversation.get("messages") or []:
        metadata = item.get("metadata") or {}
        if str(metadata.get("run_id") or "") == run_id and metadata.get("attachments"):
            return True
    return False
