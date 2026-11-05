# -*- coding: utf-8 -*-
"""渠道持久状态：QQ 身份 ↔ Cat Chat 会话映射、消息去重、投递分段与确认凭据。

**独立 SQLite**（``data_dir/channels/qq.db``），不进 ``chat.db``：渠道状态是
「谁跟哪个会话说话、哪一段回复已经发出去了」这类通道账本，与对话/消息库的
语义无关；放进主库会让通用模型配置和聊天迁移被迫认识 QQ 概念（计划 §3.4）。

所有写操作都是幂等的（``INSERT OR IGNORE`` / 条件 ``UPDATE``），因为 OneBot
重连后 NapCat 会重推事件、投递线程也可能在崩溃后重跑同一段。
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# 消息处理状态：收到 → 已提交 Run → 投递完成；失败单独记，重连后不改写已完成的。
MESSAGE_RECEIVED = "received"
MESSAGE_SUBMITTED = "submitted"
MESSAGE_REJECTED = "rejected"
MESSAGE_FAILED = "failed"
MESSAGE_REPLIED = "replied"

# 投递状态（与 Cat Chat Run 状态严格分开：QQ 发不出去不等于 Run 失败，计划 §5）。
DELIVERY_PENDING = "pending"
DELIVERY_SENDING = "sending"
DELIVERY_SENT = "sent"
DELIVERY_FAILED = "failed"

# 确认凭据状态：只有 pending 可以批准；过期/终态/重启一律失效（计划 §4.3）。
CONFIRM_PENDING = "pending"
CONFIRM_APPROVED = "approved"
CONFIRM_REJECTED = "rejected"
CONFIRM_EXPIRED = "expired"
CONFIRM_VOID = "void"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_bindings (
    channel_account_id TEXT NOT NULL,
    scope              TEXT NOT NULL,
    qq_user_id         TEXT NOT NULL,
    qq_group_id        TEXT NOT NULL DEFAULT '',
    conversation_id    TEXT NOT NULL,
    created_at         INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL,
    PRIMARY KEY (channel_account_id, scope, qq_user_id, qq_group_id)
);

CREATE TABLE IF NOT EXISTS channel_messages (
    channel_account_id TEXT NOT NULL,
    message_id         TEXT NOT NULL,
    scope              TEXT NOT NULL,
    qq_user_id         TEXT NOT NULL,
    qq_group_id        TEXT NOT NULL DEFAULT '',
    conversation_id    TEXT NOT NULL DEFAULT '',
    run_id             TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL,
    detail             TEXT NOT NULL DEFAULT '',
    created_at         INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL,
    PRIMARY KEY (channel_account_id, message_id)
);

CREATE TABLE IF NOT EXISTS channel_deliveries (
    delivery_id     TEXT PRIMARY KEY,
    channel_account_id TEXT NOT NULL,
    message_id      TEXT NOT NULL,
    run_id          TEXT NOT NULL DEFAULT '',
    conversation_id TEXT NOT NULL DEFAULT '',
    scope           TEXT NOT NULL,
    qq_user_id      TEXT NOT NULL,
    qq_group_id     TEXT NOT NULL DEFAULT '',
    segment_index   INTEGER NOT NULL,
    total_segments  INTEGER NOT NULL,
    content         TEXT NOT NULL,
    status          TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    error           TEXT NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_channel_deliveries_segment
    ON channel_deliveries(channel_account_id, message_id, segment_index);

CREATE TABLE IF NOT EXISTS channel_confirmations (
    confirm_id      TEXT NOT NULL,
    channel_account_id TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    qq_user_id      TEXT NOT NULL,
    qq_group_id     TEXT NOT NULL DEFAULT '',
    tool            TEXT NOT NULL,
    status          TEXT NOT NULL,
    expires_at      INTEGER NOT NULL,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (channel_account_id, confirm_id)
);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class ChannelStore:
    """渠道账本。线程安全：连接按调用新建（SQLite 的默认线程模型下最省心）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # ---- 绑定（QQ 身份 ↔ 会话）----

    def binding_for(
        self, account_id: str, scope: str, qq_user_id: str, qq_group_id: str = ""
    ) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM channel_bindings WHERE channel_account_id=? AND scope=? "
                "AND qq_user_id=? AND qq_group_id=?",
                (str(account_id), str(scope), str(qq_user_id), str(qq_group_id or "")),
            ).fetchone()
        return dict(row) if row else None

    def bind(
        self,
        account_id: str,
        scope: str,
        qq_user_id: str,
        conversation_id: str,
        qq_group_id: str = "",
    ) -> dict[str, Any]:
        """写入或改写绑定（同一身份只保留一个会话）。"""
        stamp = now_ms()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO channel_bindings(channel_account_id, scope, qq_user_id, qq_group_id, "
                "conversation_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(channel_account_id, scope, qq_user_id, qq_group_id) DO UPDATE SET "
                "conversation_id=excluded.conversation_id, updated_at=excluded.updated_at",
                (str(account_id), str(scope), str(qq_user_id), str(qq_group_id or ""),
                 str(conversation_id), stamp, stamp),
            )
        return self.binding_for(account_id, scope, qq_user_id, qq_group_id) or {}

    def unbind(
        self, account_id: str, scope: str, qq_user_id: str, qq_group_id: str = ""
    ) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "DELETE FROM channel_bindings WHERE channel_account_id=? AND scope=? "
                "AND qq_user_id=? AND qq_group_id=?",
                (str(account_id), str(scope), str(qq_user_id), str(qq_group_id or "")),
            )
        return cursor.rowcount > 0

    def list_bindings(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM channel_bindings ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- 入站消息去重 ----

    def lookup_message(self, account_id: str, message_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM channel_messages WHERE channel_account_id=? AND message_id=?",
                (str(account_id), str(message_id)),
            ).fetchone()
        return dict(row) if row else None

    def record_message(
        self,
        account_id: str,
        message_id: str,
        *,
        scope: str,
        qq_user_id: str,
        qq_group_id: str = "",
        conversation_id: str = "",
        run_id: str = "",
        status: str = MESSAGE_RECEIVED,
        detail: str = "",
    ) -> tuple[dict[str, Any], bool]:
        """先持久记录再提交 Run（计划 §5「收到消息后先持久记录」）。

        返回 (记录, 是否本次新建)。``False`` 表示重复事件——调用方必须直接跳过，
        不能因为重连重推而重复创建 Run 或重复计费。
        """
        stamp = now_ms()
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO channel_messages(channel_account_id, message_id, scope, "
                "qq_user_id, qq_group_id, conversation_id, run_id, status, detail, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (str(account_id), str(message_id), str(scope), str(qq_user_id),
                 str(qq_group_id or ""), str(conversation_id), str(run_id), str(status),
                 str(detail)[:2000], stamp, stamp),
            )
            created = cursor.rowcount > 0
        record = self.lookup_message(account_id, message_id) or {}
        return record, created

    def update_message(
        self,
        account_id: str,
        message_id: str,
        *,
        status: str | None = None,
        detail: str | None = None,
        conversation_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        assignments = ["updated_at=?"]
        values: list[Any] = [now_ms()]
        for column, value in (
            ("status", status), ("detail", detail),
            ("conversation_id", conversation_id), ("run_id", run_id),
        ):
            if value is not None:
                assignments.append(f"{column}=?")
                values.append(str(value)[:2000] if column == "detail" else str(value))
        values.extend([str(account_id), str(message_id)])
        with self._lock, self._connect() as db:
            db.execute(
                f"UPDATE channel_messages SET {', '.join(assignments)} "
                "WHERE channel_account_id=? AND message_id=?",
                values,
            )

    def pending_replies(self, account_id: str = "") -> list[dict[str, Any]]:
        """重启后要对账的入站消息：已提交 Run 但投递尚未完成（计划 §5 恢复未完成投递）。"""
        conditions = ["status IN (?, ?)", "run_id <> ''"]
        values: list[Any] = [MESSAGE_SUBMITTED, MESSAGE_FAILED]
        if account_id:
            conditions.append("channel_account_id=?")
            values.append(str(account_id))
        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM channel_messages WHERE {' AND '.join(conditions)} "
                "ORDER BY created_at ASC LIMIT 200",
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def received_messages(self, older_than_ms: int = 0) -> list[dict[str, Any]]:
        """仍是 ``received`` 的记录：崩溃停在「已记录、未提交」的残留。"""
        threshold = now_ms() - max(0, int(older_than_ms))
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM channel_messages WHERE status=? AND created_at <= ? "
                "ORDER BY created_at ASC LIMIT 200",
                (MESSAGE_RECEIVED, threshold),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- 回复投递分段 ----

    def prepare_deliveries(
        self,
        account_id: str,
        message_id: str,
        *,
        run_id: str,
        conversation_id: str,
        scope: str,
        qq_user_id: str,
        qq_group_id: str,
        segments: list[str],
    ) -> list[dict[str, Any]]:
        """登记一个回复的全部待发分段（幂等：已存在的分段保持原状态）。

        已确认送达的分段不会被重写，重试只会补发 pending/failed 的部分
        （计划 §4.2「不重复发送已确认送达部分」）。
        """
        stamp = now_ms()
        total = len(segments)
        with self._lock, self._connect() as db:
            for index, content in enumerate(segments):
                db.execute(
                    "INSERT OR IGNORE INTO channel_deliveries(delivery_id, channel_account_id, "
                    "message_id, run_id, conversation_id, scope, qq_user_id, qq_group_id, "
                    "segment_index, total_segments, content, status, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, str(account_id), str(message_id), str(run_id),
                     str(conversation_id), str(scope), str(qq_user_id), str(qq_group_id or ""),
                     index, total, content, DELIVERY_PENDING, stamp, stamp),
                )
        return self.deliveries_for(account_id, message_id)

    def deliveries_for(self, account_id: str, message_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM channel_deliveries WHERE channel_account_id=? AND message_id=? "
                "ORDER BY segment_index ASC",
                (str(account_id), str(message_id)),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_delivery(
        self, delivery_id: str, status: str, *, error: str = "", increment_attempt: bool = False
    ) -> None:
        attempts = "attempts + 1" if increment_attempt else "attempts"
        with self._lock, self._connect() as db:
            db.execute(
                f"UPDATE channel_deliveries SET status=?, error=?, attempts={attempts}, updated_at=? "
                "WHERE delivery_id=?",
                (str(status), str(error)[:500], now_ms(), str(delivery_id)),
            )

    def unfinished_deliveries(self, account_id: str = "") -> list[dict[str, Any]]:
        conditions = ["status IN (?, ?, ?)"]
        values: list[Any] = [DELIVERY_PENDING, DELIVERY_SENDING, DELIVERY_FAILED]
        if account_id:
            conditions.append("channel_account_id=?")
            values.append(str(account_id))
        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM channel_deliveries WHERE {' AND '.join(conditions)} "
                "ORDER BY created_at ASC, segment_index ASC LIMIT 200",
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- 工具确认凭据 ----

    def register_confirmation(
        self,
        account_id: str,
        confirm_id: str,
        *,
        run_id: str,
        conversation_id: str,
        qq_user_id: str,
        qq_group_id: str,
        tool: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        """登记一条可经 QQ 批准的确认凭据；绑定 conversation + run + QQ 身份。"""
        stamp = now_ms()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO channel_confirmations(confirm_id, channel_account_id, run_id, "
                "conversation_id, qq_user_id, qq_group_id, tool, status, expires_at, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (str(confirm_id), str(account_id), str(run_id), str(conversation_id),
                 str(qq_user_id), str(qq_group_id or ""), str(tool), CONFIRM_PENDING,
                 stamp + max(1, int(ttl_seconds)) * 1000, stamp, stamp),
            )
        return self.get_confirmation(account_id, confirm_id) or {}

    def get_confirmation(self, account_id: str, confirm_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM channel_confirmations WHERE channel_account_id=? AND confirm_id=?",
                (str(account_id), str(confirm_id)),
            ).fetchone()
        return dict(row) if row else None

    def latest_pending_confirmation(
        self, account_id: str, qq_user_id: str, conversation_id: str = ""
    ) -> dict[str, Any] | None:
        conditions = ["channel_account_id=?", "qq_user_id=?", "status=?", "expires_at > ?"]
        values: list[Any] = [str(account_id), str(qq_user_id), CONFIRM_PENDING, now_ms()]
        if conversation_id:
            conditions.append("conversation_id=?")
            values.append(str(conversation_id))
        with self._connect() as db:
            row = db.execute(
                f"SELECT * FROM channel_confirmations WHERE {' AND '.join(conditions)} "
                "ORDER BY created_at DESC LIMIT 1",
                values,
            ).fetchone()
        return dict(row) if row else None

    def resolve_confirmation(self, account_id: str, confirm_id: str, status: str) -> bool:
        """只把 pending 的凭据改成结果状态（一次有效，杜绝重放批准）。"""
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE channel_confirmations SET status=?, updated_at=? "
                "WHERE channel_account_id=? AND confirm_id=? AND status=?",
                (str(status), now_ms(), str(account_id), str(confirm_id), CONFIRM_PENDING),
            )
        return cursor.rowcount > 0

    def expire_stale_confirmations(self) -> int:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE channel_confirmations SET status=?, updated_at=? "
                "WHERE status=? AND expires_at <= ?",
                (CONFIRM_EXPIRED, now_ms(), CONFIRM_PENDING, now_ms()),
            )
        return cursor.rowcount

    def void_confirmations_for_run(self, run_id: str) -> int:
        """Run 终结/重连后旧凭据一律失效，不恢复为可批准状态（计划 §5）。"""
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE channel_confirmations SET status=?, updated_at=? "
                "WHERE run_id=? AND status=?",
                (CONFIRM_VOID, now_ms(), str(run_id), CONFIRM_PENDING),
            )
        return cursor.rowcount

    def void_all_confirmations(self) -> int:
        """进程启动时调用：重启前发出的确认凭据不能复活。"""
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE channel_confirmations SET status=?, updated_at=? WHERE status=?",
                (CONFIRM_VOID, now_ms(), CONFIRM_PENDING),
            )
        return cursor.rowcount
