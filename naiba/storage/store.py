from __future__ import annotations

import functools
import hashlib
import json
import logging
import math
import re
import shutil
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from naiba.core.contracts import TERMINAL_RUN_EVENT_TYPES
from naiba.core.messages import MetadataKeys
from naiba.core.paths import normalized_path_key

logger = logging.getLogger("naiba.storage.store")

# metadata 键的合法形状（一层、标识符）：`merge_message_metadata` 要把键名拼进 SQLite 的
# JSON 路径（路径不支持占位符），所以形状校验就是那条拼接的安全边界。
_METADATA_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# 当前数据库 schema 版本（user_version）。每次新增迁移 +1。
CURRENT_SCHEMA_VERSION = 24

# 自该版本起存在"数据改写型"迁移（v14 起），执行前自动备份整库。
FIRST_DATA_WRITING_MIGRATION = 14

# 「未结束」的任务状态（run 与 job 共用 background_tasks 表）。
# 重启清理、活动列表查询与取消判定必须共用这一份定义：
# ``jobs.cancel()`` 会把子任务置为 ``stopping``（见 naiba/jobs.py 的 JOB_ACTIVE），
# 而 store 侧的启动清理与判定曾经漏掉它，于是产生「重启也清不掉的僵尸」——
# 一个卡在 stopping 的子任务会让整条会话永久显示「回复进行中」，把「分支 /
# 重新生成 / 新会话」三个救援入口全部挡住（2026-09-14 客户机实测）。
ACTIVE_TASK_STATUSES: tuple[str, ...] = (
    "queued",
    "running",
    "waiting",
    "stopping",
    "cancelling",
)

# 终态（含启动清理自身产生的 interrupted）：状态一定在这里或上面的集合里。
TERMINAL_TASK_STATUSES: tuple[str, ...] = (
    "completed",
    "failed",
    "cancelled",
    "interrupted",
)

# 「顶层对话 Run」的 kind——只有这些 kind 的顶层记录才算"这条会话在不在回答"。
# 其余 kind（shell/check/http_poll/comfyui/subagent）都是后台作业：任务面板只列它们，
# 否则用户看到的是自己发过的每一句话被当成"任务"。唯一一份定义，manager 直接引用。
PRIMARY_RUN_KINDS: tuple[str, ...] = ("chat", "plan_execute")

# 服务重启把 in-flight 任务判为中断时写进 background_tasks.error / run_events 的原因文案。
# 两处（任务行 + 事件流）共用一份，运行层重建「中断轮次」的部分消息时也复用它。
INTERRUPTED_TASK_REASON = "服务重启，运行已中断"


def _status_in_clause(statuses: tuple[str, ...]) -> str:
    """把状态集合渲染成 SQL 的 ``IN (...)`` 片段（字面量，不含用户输入）。"""
    return "(" + ", ".join(f"'{status}'" for status in statuses) + ")"


def _kinds_not_in_clause(kinds: tuple[str, ...]) -> str:
    """把 kind 集合渲染成 SQL 的 ``NOT IN (...)`` 片段（字面量，不含用户输入）。"""
    return "(" + ", ".join(f"'{kind}'" for kind in kinds) + ")"


def _attachment_title(attachments: list[dict[str, Any]] | None) -> str:
    """纯附件轮次的会话标题回退：首个附件名（无名字时取路径文件名）。"""
    for item in attachments or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip() or Path(str(item.get("path") or "")).name
        if name:
            return name[:36]
    return "新对话"


def _migrate_to_v1(db: sqlite3.Connection) -> None:
    """Schema 版本 1 的迁移：补齐历史列并回填 legacy model_key。

    全部操作幂等：列已存在时通过 `try: SELECT ... except OperationalError` 跳过 ALTER。
    """
    # Migration: add mode column to existing tables
    try:
        db.execute("SELECT mode FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute("ALTER TABLE conversations ADD COLUMN mode TEXT NOT NULL DEFAULT 'online'")
    for column, definition in (
        ("title_customized", "INTEGER NOT NULL DEFAULT 0"),
        ("system_prompt", "TEXT NOT NULL DEFAULT ''"),
        ("stream_enabled", "INTEGER NOT NULL DEFAULT 1"),
        ("provider_id", "TEXT NOT NULL DEFAULT ''"),
        ("model_key", "TEXT NOT NULL DEFAULT ''"),
        ("agent_id", "TEXT NOT NULL DEFAULT ''"),
        ("interaction_mode", "TEXT NOT NULL DEFAULT 'craft'"),
        ("permission_mode", "TEXT NOT NULL DEFAULT 'confirm'"),
    ):
        try:
            db.execute(f"SELECT {column} FROM conversations LIMIT 1")
        except sqlite3.OperationalError:
            db.execute(f"ALTER TABLE conversations ADD COLUMN {column} {definition}")
    # 旧会话回填 model_key：legacy 仅使用 online 前缀（provider_id 一律按 online 处理）。
    try:
        db.execute(
            "UPDATE conversations SET model_key = 'online:' || provider_id "
            "WHERE model_key = '' AND provider_id != ''"
        )
    except sqlite3.OperationalError:
        pass
    for column, definition in (
        ("kind", "TEXT NOT NULL DEFAULT 'chat'"),
        ("interaction_mode", "TEXT NOT NULL DEFAULT 'craft'"),
        ("input_message_id", "TEXT NOT NULL DEFAULT ''"),
        ("plan_id", "TEXT NOT NULL DEFAULT ''"),
    ):
        try:
            db.execute(f"SELECT {column} FROM background_tasks LIMIT 1")
        except sqlite3.OperationalError:
            db.execute(f"ALTER TABLE background_tasks ADD COLUMN {column} {definition}")


def _migrate_to_v2(db: sqlite3.Connection) -> None:
    """Persist the web-search switch per conversation."""
    try:
        db.execute("SELECT web_search_enabled FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute(
            "ALTER TABLE conversations ADD COLUMN web_search_enabled "
            "INTEGER NOT NULL DEFAULT 0"
        )
    # Harness Job 字段（增量迁移，沿用现有 background_tasks 表，不新建平行表）
    for column, definition in (
        ("parent_job_id", "TEXT NOT NULL DEFAULT ''"),
        ("owner_session_id", "TEXT NOT NULL DEFAULT ''"),
        ("progress", "REAL NOT NULL DEFAULT 0"),
        ("current_step", "TEXT NOT NULL DEFAULT ''"),
        ("attempt", "INTEGER NOT NULL DEFAULT 0"),
        ("checkpoint", "TEXT NOT NULL DEFAULT '{}'"),
        ("result", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        try:
            db.execute(f"SELECT {column} FROM background_tasks LIMIT 1")
        except sqlite3.OperationalError:
            db.execute(f"ALTER TABLE background_tasks ADD COLUMN {column} {definition}")


def _migrate_to_v3(db: sqlite3.Connection) -> None:
    """Persist the deep-reasoning switch per conversation."""
    try:
        db.execute("SELECT deep_reasoning_enabled FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute(
            "ALTER TABLE conversations ADD COLUMN deep_reasoning_enabled "
            "INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_to_v4(db: sqlite3.Connection) -> None:
    """Persist the lightweight text-chat switch per conversation."""
    try:
        db.execute("SELECT lightweight_mode FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute(
            "ALTER TABLE conversations ADD COLUMN lightweight_mode "
            "INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_to_v5(db: sqlite3.Connection) -> None:
    """Persist the user-selected features disabled by lightweight mode."""
    try:
        db.execute("SELECT lightweight_disabled_features FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute(
            "ALTER TABLE conversations ADD COLUMN lightweight_disabled_features "
            "TEXT NOT NULL DEFAULT '[\"skills_tools\", \"vision\"]'"
        )


def _migrate_to_v6(db: sqlite3.Connection) -> None:
    """Make all four lightweight capabilities explicit for existing chats."""
    rows = db.execute(
        "SELECT id, lightweight_disabled_features FROM conversations"
    ).fetchall()
    allowed = ("skills_tools", "vision", "web_search", "deep_reasoning")
    for row in rows:
        try:
            current = json.loads(row[1] or "[]")
        except (json.JSONDecodeError, TypeError):
            current = []
        features = [item for item in current if item in allowed]
        for feature in allowed:
            if feature not in features:
                features.append(feature)
        db.execute(
            "UPDATE conversations SET lightweight_disabled_features = ? WHERE id = ?",
            (json.dumps(features, ensure_ascii=False), row[0]),
        )


def _migrate_to_v7(db: sqlite3.Connection) -> None:
    """Split the old combined lightweight switch into independent tools/skills flags."""
    rows = db.execute(
        "SELECT id, lightweight_disabled_features FROM conversations"
    ).fetchall()
    for row in rows:
        try:
            current = json.loads(row[1] or "[]")
        except (json.JSONDecodeError, TypeError):
            current = []
        features: list[str] = []
        if "skills_tools" in current:
            features.extend(("tools", "skills"))
        for feature in ("tools", "skills"):
            if feature in current and feature not in features:
                features.append(feature)
        db.execute(
            "UPDATE conversations SET lightweight_disabled_features = ? WHERE id = ?",
            (json.dumps(features, ensure_ascii=False), row[0]),
        )


def _migrate_to_v8(db: sqlite3.Connection) -> None:
    """Persist per-conversation workspace and reasoning intensity."""
    for column, definition in (
        ("workspace_dir", "TEXT NOT NULL DEFAULT ''"),
        ("reasoning_effort", "TEXT NOT NULL DEFAULT 'off'"),
    ):
        try:
            db.execute(f"SELECT {column} FROM conversations LIMIT 1")
        except sqlite3.OperationalError:
            db.execute(f"ALTER TABLE conversations ADD COLUMN {column} {definition}")
    # Preserve the old boolean switch for existing conversations.
    db.execute(
        "UPDATE conversations SET reasoning_effort = 'medium' "
        "WHERE deep_reasoning_enabled = 1 AND (reasoning_effort = '' OR reasoning_effort IS NULL OR reasoning_effort = 'off')"
    )


def _migrate_to_v9(db: sqlite3.Connection) -> None:
    """Persist per-session baked tool set (enabled_tool_ids) on conversations.

    会话启动时按当前 Agent 的工具集固化一次，之后不可改，用于实现“每 Agent 硬限制 +
    缓存稳定”。旧值默认空串，表示“未固化”。
    """
    try:
        db.execute("SELECT enabled_tool_ids FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute(
            "ALTER TABLE conversations ADD COLUMN enabled_tool_ids TEXT NOT NULL DEFAULT ''"
        )


def _migrate_to_v10(db: sqlite3.Connection) -> None:
    """Persist per-conversation workspace group for sidebar workspace folders.

    workspace_group 曾在 1.4.2 被误加入 v8 迁移，导致旧库（v8 已执行过）
    缺失该列而报 no such column。此处独立为 v10，确保所有存量库都能补列。
    """
    try:
        db.execute("SELECT workspace_group FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute(
            "ALTER TABLE conversations ADD COLUMN workspace_group TEXT NOT NULL DEFAULT ''"
        )


def _migrate_to_v11(db: sqlite3.Connection) -> None:
    """Persist a conversation's frozen Skill policy (the turn-1 referenced skill ids).

    首轮 /ref 引用的技能集会被冻结并持久化到会话，保证后续轮“首轮注入 system、
    后续只追加末尾”能跨轮/跨重启稳定（为前缀缓存与 ref 路由稳定）。旧会话默认空串。
    """
    try:
        db.execute("SELECT skill_policy FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute(
            "ALTER TABLE conversations ADD COLUMN skill_policy TEXT NOT NULL DEFAULT ''"
        )


def _migrate_to_v12(db: sqlite3.Connection) -> None:
    """Persist a conversation's frozen image-support capability.

    模型是否支持图片（brain_supports_images）按会话固化一次，避免每轮随“本轮是否带图”
    重新探测导致结果漂移、进而改变视觉工具集并破坏前缀缓存。旧会话默认 -1（未固化）。
    """
    try:
        db.execute("SELECT chat_supports_images FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute(
            "ALTER TABLE conversations ADD COLUMN chat_supports_images INTEGER NOT NULL DEFAULT -1"
        )


def _migrate_to_v13(db: sqlite3.Connection) -> None:
    """移除已退役的 tool_runs 表（工具结果改由 message.metadata.tool_runs 承载）。

    该表的写入管线（log_tool_run）已删除，且全库无任何读取者；老库直接 DROP 释放空间。
    """
    db.execute("DROP TABLE IF EXISTS tool_runs")


# 推理流合流的窗口常量（存量压缩与写入端《run/stream.py》保持一致口径）。
_REASONING_MIGRATE_FLUSH_CHARS = 2048
_REASONING_MIGRATE_FLUSH_MS = 1000


def _coalesce_reasoning_deltas(db: sqlite3.Connection, run_id: str | None = None) -> int:
    """把 ``reasoning_delta`` 事件按窗口合流为整段 ``reasoning``（幂等）。

    与写入端 sink 相同口径：缓冲累计 ≥2048 字符、或距上一段 ≥1s 时切段；
    每段以原首条 sequence 落库（行序保留、允许 sequence 空洞），删除被合流行。
    ``run_id`` 为空时对全库执行（迁移 v14）；指定时只压缩该 run（运行时终态合流）。
    返回处理的行数；无可合流行时返回 0。
    """
    if run_id:
        rows = db.execute(
            "SELECT run_id, sequence, payload, created_at FROM run_events "
            "WHERE event_type = 'reasoning_delta' AND run_id = ? ORDER BY run_id, sequence",
            (run_id,),
        )
    else:
        rows = db.execute(
            "SELECT run_id, sequence, payload, created_at FROM run_events "
            "WHERE event_type = 'reasoning_delta' ORDER BY run_id, sequence"
        )
    pending_run: str | None = None
    pending: list[str] = []
    pending_bytes = 0
    pending_seq = 0
    pending_created = 0
    pending_last = 0
    segments: list[tuple[str, int, int, str]] = []
    processed = 0

    def flush_segment() -> None:
        nonlocal pending, pending_bytes
        if pending:
            segments.append((pending_run, pending_seq, pending_created, "".join(pending)))
        pending = []
        pending_bytes = 0

    for row in rows:
        run_id = str(row[0])
        if run_id != pending_run:
            flush_segment()
            pending_run = run_id
            pending_seq = int(row[1])
            pending_created = int(row[3] or 0)
            pending_last = pending_created
        try:
            text = str(json.loads(row[2] or "{}").get("content") or "")
        except (json.JSONDecodeError, TypeError):
            text = ""
        processed += 1
        if not text:
            continue
        created = int(row[3] or 0)
        if pending and (pending_bytes + len(text) >= _REASONING_MIGRATE_FLUSH_CHARS
                        or (pending_last and created - pending_last >= _REASONING_MIGRATE_FLUSH_MS)):
            flush_segment()
            pending_seq = int(row[1])
            pending_created = created
        pending.append(text)
        pending_bytes += len(text)
        pending_last = created
    flush_segment()

    if not segments:
        return 0
    # 按 run 分组：删除 delta 行 + 插入合流行（同事务，原子）。
    by_run: dict[str, list[tuple[str, int, int, str]]] = {}
    for run_id, seq, created, text in segments:
        by_run.setdefault(run_id, []).append((run_id, seq, created, text))
    for run_id, items in by_run.items():
        db.execute(
            "DELETE FROM run_events WHERE run_id = ? AND event_type = 'reasoning_delta'",
            (run_id,),
        )
        for run_id2, seq, created, text in items:
            db.execute(
                "INSERT INTO run_events(run_id, sequence, event_type, payload, created_at) "
                "VALUES (?, ?, 'reasoning', ?, ?)",
                (run_id2, seq, json.dumps({"type": "reasoning", "content": text}, ensure_ascii=False), created),
            )
    return processed


# 各终态事件里"与 messages 表逐字重复的完整消息副本"键（按 type 分开，不能一刀切 pop）：
#   - done：`message` 就是整份 assistant 消息对象（存量里也见过错挂在 done 上的
#     `aborted_message`，一并收掉）；
#   - cancelled：`message` 是"任务已取消"文案（老版本还可能是对象）+ `aborted_message` 是
#     整份"已中止"消息；
#   - error：只有 `partial_message` 是重复副本——`message` 是**错误原因短文案**，前端要显示，
#     重放时更不能丢（所以它不在收缩之列）。
_SLIM_TERMINAL_PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    "done": ("message", "aborted_message"),
    "cancelled": ("message", "aborted_message"),
    "error": ("partial_message",),
}


def _slim_terminal_event_payload_rows(
    db: sqlite3.Connection, run_id: str = ""
) -> int:
    """终态事件载荷去掉与 messages 表重复的完整消息副本（幂等）。

    收缩键按事件类型分开（见 `_SLIM_TERMINAL_PAYLOAD_KEYS`）：这些对象（content + metadata
    + trace…）只在运行结束的即时渲染里用得到，run 终态后再无读取方，却会一直堆在库里。
    v14 的旧口径只覆盖 done/cancelled，且 `error.partial_message` 至今没人收（失败事件会
    永久带着整条 partial 消息）。返回处理行数。

    ``run_id`` 为空表示全库（迁移用）；给定时只处理该 run（运行期收尾用）。
    """
    conditions = [
        "event_type IN (" + ", ".join(f"'{kind}'" for kind in TERMINAL_RUN_EVENT_TYPES) + ")"
    ]
    parameters: list[Any] = []
    if run_id:
        conditions.append("run_id = ?")
        parameters.append(run_id)
    updated = 0
    for row_run_id, sequence, event_type, payload in db.execute(
        f"SELECT run_id, sequence, event_type, payload FROM run_events "
        f"WHERE {' AND '.join(conditions)}",
        tuple(parameters),
    ).fetchall():
        keys = _SLIM_TERMINAL_PAYLOAD_KEYS.get(str(event_type or ""), ())
        if not keys:
            continue
        try:
            obj = json.loads(payload or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        if not any(key in obj for key in keys):
            continue
        for key in keys:
            obj.pop(key, None)
        db.execute(
            "UPDATE run_events SET payload = ? WHERE run_id = ? AND sequence = ?",
            (json.dumps(obj, ensure_ascii=False), row_run_id, sequence),
        )
        updated += 1
    return updated


def _slim_terminal_event_payloads(db: sqlite3.Connection) -> int:
    """迁移口径：全库瘦身终态事件载荷（done/cancelled/error）。"""
    return _slim_terminal_event_payload_rows(db)


def _slim_terminal_snapshot_rows(db: sqlite3.Connection, task_id: str = "") -> int:
    """终态（completed/failed/cancelled）Run 的 snapshot 去掉 conversation_messages（幂等）。

    该键只在运行期与 interrupted 恢复期被读取（快照语义：run 线程与 HTTP 线程隔离），
    终态后无读取方；interrupted 保留。返回处理行数。

    ``task_id`` 为空表示全库（迁移用）；给定时只处理该 run（运行期收尾用）。
    """
    conditions = ["status IN ('completed', 'failed', 'cancelled')"]
    parameters: list[Any] = []
    if task_id:
        conditions.append("id = ?")
        parameters.append(task_id)
    updated = 0
    for row_id, snapshot_text in db.execute(
        f"SELECT id, snapshot FROM background_tasks WHERE {' AND '.join(conditions)}",
        tuple(parameters),
    ).fetchall():
        try:
            obj = json.loads(snapshot_text or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(obj, dict) or "conversation_messages" not in obj:
            continue
        obj.pop("conversation_messages", None)
        db.execute(
            "UPDATE background_tasks SET snapshot = ? WHERE id = ?",
            (json.dumps(obj, ensure_ascii=False), row_id),
        )
        updated += 1
    return updated


def _slim_terminal_snapshots(db: sqlite3.Connection) -> int:
    """迁移口径：全库收缩终态 Run 的 snapshot。"""
    return _slim_terminal_snapshot_rows(db)


def _warn_data_migration(db: sqlite3.Connection, message: str) -> None:
    """迁移警告双通道输出（窗口版 stdout/stderr 均为 None，print 会 AttributeError）。

    ① stderr 可用时打印（源码模式/控制台）；② 同时追加写入数据库所在目录的
    data-migration-warning.log（冻结版无控制台场景的诊断都走文件，见维护说明 §九13）。
    """
    try:
        if sys.stderr is not None:
            print(message, file=sys.stderr)
    except Exception:
        pass  # 诊断通道失败不阻断迁移；文件通道兜底
    try:
        row = db.execute("PRAGMA database_list").fetchone()
        db_file = str(row[2]) if row and len(row) > 2 and row[2] else ""
        if db_file:
            target = Path(db_file).parent / "data-migration-warning.log"
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(f"{message}\n")
    except Exception:
        pass  # 数据目录不可写：无更优通道，放弃（不影响迁移结果）


def _migrate_to_v14(db: sqlite3.Connection) -> None:
    """存量历史数据压缩（三个重复存储源，全部幂等）+ VACUUM 物理收缩。

    - reasoning_delta 逐 token 事件（存量 87 万行、run_events 行数 96.6%）→ 窗口合流；
    - done/cancelled 事件携带的完整消息对象与 messages 表重复 → 载荷瘦身；
    - 终态 snapshot 固化的完整会话消息列表（O(N²) 累积，实测 81 MB）→ 键收缩；
    - VACUUM 释放物理空间。内容改写失败会让迁移整体失败（用户可见、可重试）；
      VACUUM 属空间优化，失败仅记录诊断（stderr + 警告文件）并允许迁移继续。
    """
    _coalesce_reasoning_deltas(db)
    _slim_terminal_event_payloads(db)
    _slim_terminal_snapshots(db)
    db.commit()
    try:
        db.execute("VACUUM")
    except sqlite3.OperationalError as exc:  # 空间不足/文件锁等：内容迁移已成功
        _warn_data_migration(db, f"[naiba-storage] 迁移 v14 内容完成，VACUUM 未执行：{exc}")


def _migrate_to_v15(db: sqlite3.Connection) -> None:
    """会话收藏标记（侧栏「已收藏」分组）。

    纯增量列：默认 0（未收藏），不影响任何既有读取路径；列已存在时跳过（幂等）。
    """
    try:
        db.execute("SELECT favorite FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute("ALTER TABLE conversations ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")


def _migrate_to_v16(db: sqlite3.Connection) -> None:
    """会话级首轮上下文（「首轮上下文」折叠卡的数据落地处）。

    该数据原本只存在该会话**最早 chat run 的 snapshot** 里，于是两处会丢：
    ① 分支对话只复制消息与设置、不复制 run 行 → 新会话读不到，卡片不显示（用户报障）；
    ② 「清空已结束任务」会删掉 chat run 行（`clear_terminal_background_tasks` 无 kind 过滤）。
    纯增量列：默认空串（老会话读时回退到 run 快照），列已存在时跳过（幂等）。
    """
    try:
        db.execute("SELECT first_turn FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute("ALTER TABLE conversations ADD COLUMN first_turn TEXT NOT NULL DEFAULT ''")


def _migrate_to_v17(db: sqlite3.Connection) -> None:
    """会话级模型覆盖名。

    ``model_key`` 继续指向 API profile（地址、密钥、协议与生成参数），``model_name``
    只覆盖该会话实际请求的模型。空串表示使用 API profile 的默认模型。
    """
    try:
        db.execute("SELECT model_name FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute("ALTER TABLE conversations ADD COLUMN model_name TEXT NOT NULL DEFAULT ''")


def _migrate_to_v18(db: sqlite3.Connection) -> None:
    """分支来源登记（侧栏分支徽标 / 分支链面板的数据来源）。

    两列都是**纯增量、默认空串**：存量分支会话（v18 之前建的）没有来源记录，
    明确不回溯——按标题 ``(N)`` 弱推断不可靠，宁可显示为无关联。
    ``branched_from_id`` 悬空（源会话被删）不是错误：徽标降级为「源会话已删除」。
    """
    try:
        db.execute("SELECT branched_from_id FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute("ALTER TABLE conversations ADD COLUMN branched_from_id TEXT NOT NULL DEFAULT ''")
    try:
        db.execute("SELECT branch_message_id FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute("ALTER TABLE conversations ADD COLUMN branch_message_id TEXT NOT NULL DEFAULT ''")


def _migrate_to_v19(db: sqlite3.Connection) -> None:
    """会话归档标记（侧栏「分组与排序」的筛选三档数据来源）。

    纯增量列：默认 0（未归档），不影响任何既有读取路径；列已存在时跳过（幂等）。
    归档语义与 DeepSeek Harness 对齐：**仅从侧栏与全文搜索隐藏，不删任何数据**，
    会话仍可打开继续聊，可随时取消归档。
    """
    try:
        db.execute("SELECT archived FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute("ALTER TABLE conversations ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")


def _migrate_to_v20(db: sqlite3.Connection) -> None:
    """会话手动排序位（「手动排序」模式 + 单列表拖拽的数据落地处）。

    ``sort_order`` 为 0 表示「从未手动排过序」：手动模式下未排序的会话按
    ``updated_at`` 倒序排在最前（新会话自然出现顶部）；拖拽落序后整体重排为
    1..N（``set_conversation_sort_order``）。纯增量列，幂等。
    """
    try:
        db.execute("SELECT sort_order FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute("ALTER TABLE conversations ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")


def _migrate_to_v21(db: sqlite3.Connection) -> None:
    """API Token 用量台账（「设置 → 用量统计」的数据落地处）。

    一个 run/job 完成时落**一条**汇总记录（``record_usage``，run_id 主键幂等：
    重跑/中断恢复重记是 REPLACE 而不是翻倍）。token 口径是「Σ 全部模型请求」
    （requests_detail 求和），与上下文圆环的「最后一次请求」口径不同——后者
    继续由消息 metadata.usage 承担，本表只服务聚合统计。``created_at`` 是
    完成时刻（毫秒）；不落价格快照，费用在查询时按当前单价重算。
    ``CREATE TABLE IF NOT EXISTS`` 自带幂等。
    """
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_records (
            run_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL DEFAULT '',
            message_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'chat',
            model_key TEXT NOT NULL DEFAULT '',
            model_name TEXT NOT NULL DEFAULT '',
            agent_id TEXT NOT NULL DEFAULT '',
            requests INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            cached_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_records_created ON usage_records(created_at)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_records_model ON usage_records(model_key, created_at)"
    )


def _migrate_to_v22(db: sqlite3.Connection) -> None:
    """清掉 first_turn 里**只写不读**的 `full_messages` 键（幂等）。

    该键是「system + 整轮 trace」的又一份副本，同时写进 `background_tasks.snapshot`
    与 `conversations.first_turn` 两处，而全仓库没有任何读取方（前端折叠卡只用
    system/tools/skills/options/agent_name/model_key）。实测占 first_turn 的 76%
    （4.42 / 5.8 MB），删掉是纯减法。

    只动这一个键：其余键原样保留、JSON 不可解析的行跳过（坏数据不因本迁移被放大）。
    内容改写失败会让迁移整体失败（用户可见、可重试）。
    """
    updated = 0
    for conversation_id, raw in db.execute(
        "SELECT id, first_turn FROM conversations WHERE first_turn LIKE '%full_messages%'"
    ).fetchall():
        try:
            payload = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict) or "full_messages" not in payload:
            continue
        payload.pop("full_messages", None)
        db.execute(
            "UPDATE conversations SET first_turn = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), conversation_id),
        )
        updated += 1
    # run 快照里的 first_turn 是老会话的**兜底读源**，同样要清；终态 run 的快照在收尾时
    # 被 `_slim_run_snapshot` 收缩，这里只处理还留着该键的行。
    for task_id, raw in db.execute(
        "SELECT id, snapshot FROM background_tasks WHERE snapshot LIKE '%full_messages%'"
    ).fetchall():
        try:
            snapshot = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(snapshot, dict):
            continue
        first_turn = snapshot.get("first_turn")
        if not isinstance(first_turn, dict) or "full_messages" not in first_turn:
            continue
        first_turn.pop("full_messages", None)
        db.execute(
            "UPDATE background_tasks SET snapshot = ? WHERE id = ?",
            (json.dumps(snapshot, ensure_ascii=False), task_id),
        )
        updated += 1
    if updated:
        print(f"[naiba-storage] v22：清理 {updated} 行 first_turn.full_messages（只写不读的副本）")


def _migrate_to_v23(db: sqlite3.Connection) -> None:
    """把 `metadata.trace` 抽成 `message_traces` 独立表，按内容哈希只存一份（幂等）。

    **为什么要抽**：`trace` 是「本轮发给模型的完整字节序列」，按 assistant 消息各存一份。
    实测 messages.metadata 221.7MB 里它占 167.4MB；而按整块内容去重后，其中 62.2MB
    （36%）是**逐字节重复的同一份 blob**（同一 trace 被多条消息各存一遍）。
    抽表后：① 重复的只留一份；② 22 万行 run_events 之外，缓存引用扫描
    （`referenced_cache_paths`，上传时同步跑）不必再遍历混着大 blobs 的 metadata 文本。

    **口径**：`messages.trace_hash` 指向 `message_traces.trace_hash`，行内不再留 `trace`；
    读取侧由 `_message_dict` 透明补回 `metadata["trace"]`，所以 `history.py`、前端与
    所有既有消费方**不需要任何改动**。

    **路径引用风险（必须一并处理）**：trace 里会出现宿主缓存路径（实测 131 条消息），
    而 `upload_path_referenced` / `referenced_cache_paths` 原先只扫 messages.metadata 与
    background_tasks.snapshot。不把本表接入这两处，缓存清理就会把仍在对话里引用的图片
    当成"没人用"删掉。已在两个扫描点加入 message_traces.data。

    幂等：已经抽过的行（trace_hash 已填且 metadata 无 trace）跳过；`CREATE TABLE`/
    `ALTER TABLE` 均已存在时跳过。内容改写失败会让迁移整体失败（用户可见、可重试）。
    """
    try:
        db.execute("SELECT trace_hash FROM messages LIMIT 1")
    except sqlite3.OperationalError:
        db.execute("ALTER TABLE messages ADD COLUMN trace_hash TEXT NOT NULL DEFAULT ''")
    db.execute(
        "CREATE TABLE IF NOT EXISTS message_traces ("
        "trace_hash TEXT PRIMARY KEY, data TEXT NOT NULL)"
    )
    # 按「真正会写盘的 JSON 文本」去重：文本相同 = 内容相同，正是运行期 `add_message`
    # 存进去的那份字节。用户实测 `metadata` 里的 trace 文本重复率 36%，这一轮就能收敛。
    payload_by_sort_key: dict[str, str] = {}
    pending: list[tuple[str, str]] = []  # (message_id, sort_key)
    for message_id, raw in db.execute(
        "SELECT id, metadata FROM messages "
        "WHERE metadata LIKE '%\"trace\"%' AND trace_hash = ''"
    ).fetchall():
        try:
            metadata = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(metadata, dict) or "trace" not in metadata:
            continue
        trace = metadata.get("trace")
        if not isinstance(trace, list) or not trace:
            # 空 trace 与"没有 trace"等价：只摘键，不建表行（省掉一张空行）。
            metadata.pop("trace", None)
            db.execute(
                "UPDATE messages SET metadata = ? WHERE id = ?",
                (json.dumps(metadata, ensure_ascii=False), message_id),
            )
            continue
        try:
            payload = json.dumps(trace, ensure_ascii=False)
            sort_key = json.dumps(trace, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            continue  # 不可序列化（理论上不会发生）：原样保留，不冒险改写
        payload_by_sort_key.setdefault(sort_key, payload)
        pending.append((message_id, sort_key))

    hash_by_sort_key: dict[str, str] = {}
    written = 0
    for sort_key, payload in payload_by_sort_key.items():
        trace_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        hash_by_sort_key[sort_key] = trace_hash
        cursor = db.execute(
            "INSERT OR IGNORE INTO message_traces(trace_hash, data) VALUES (?, ?)",
            (trace_hash, payload),
        )
        written += cursor.rowcount
    updates: list[tuple[str, str, str]] = []
    for message_id, sort_key in pending:
        trace_hash = hash_by_sort_key.get(sort_key)
        if not trace_hash:
            continue
        row = db.execute("SELECT metadata FROM messages WHERE id = ?", (message_id,)).fetchone()
        try:
            metadata = json.loads((row[0] if row else "") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(metadata, dict):
            continue
        metadata.pop("trace", None)
        updates.append((json.dumps(metadata, ensure_ascii=False), trace_hash, message_id))
    db.executemany("UPDATE messages SET metadata = ?, trace_hash = ? WHERE id = ?", updates)
    if pending:
        print(
            f"[naiba-storage] v23：trace 抽表完成，{len(pending)} 条消息 → "
            f"{written} 份唯一 blob（{len(updates)} 行已改写）"
        )
    db.commit()
    try:
        db.execute("VACUUM")
    except sqlite3.OperationalError as exc:
        # 空间回收失败不影响内容迁移结果（与 v14 同口径）。
        _warn_data_migration(db, f"[naiba-storage] 迁移 v23 内容完成，VACUUM 未执行：{exc}")


def _migrate_to_v24(db: sqlite3.Connection) -> None:
    """补齐终态事件的重复副本收缩口径：把 done/cancelled/**error** 的完整消息对象全库清掉。

    **为什么要再来一次**：v14 只收缩 done/cancelled 的 `message`/`aborted_message`，两条口子
    一直漏着——① `error.partial_message` 压根不在口径内（存量的失败事件永久带着整条 partial
    消息）；② v14 之后、运行期瘦身（9.155）之前落库的 done 事件仍是"带整份消息"的形态
    （实测本机 201 行 / 36.0MB，占 run_events 全部载荷的 82%）。这些副本在终态后都没有读取方
    （前端收尾时重载会话拿完整消息，见 run/chat.py 的终态发射点）。

    幂等：已收缩过的行没有这三个键，直接跳过；内容改写失败会让迁移整体失败（用户可见、可重试）。
    物理回收同 v14/v23 口径（VACUUM 失败只记诊断，不影响内容迁移结果）。
    """
    updated = _slim_terminal_event_payload_rows(db)
    db.commit()
    print(f"[naiba-storage] v24：终态事件消息副本收缩完成，{updated} 行已改写")
    try:
        db.execute("VACUUM")
    except sqlite3.OperationalError as exc:
        _warn_data_migration(db, f"[naiba-storage] 迁移 v24 内容完成，VACUUM 未执行：{exc}")


# 目标版本 -> 迁移函数。新增版本时在此追加并提升 CURRENT_SCHEMA_VERSION。
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migrate_to_v1,
    2: _migrate_to_v2,
    3: _migrate_to_v3,
    4: _migrate_to_v4,
    5: _migrate_to_v5,
    6: _migrate_to_v6,
    7: _migrate_to_v7,
    8: lambda db: _migrate_to_v8(db),
    9: _migrate_to_v9,
    10: _migrate_to_v10,
    11: _migrate_to_v11,
    12: _migrate_to_v12,
    13: _migrate_to_v13,
    14: _migrate_to_v14,
    15: _migrate_to_v15,
    16: _migrate_to_v16,
    17: _migrate_to_v17,
    18: _migrate_to_v18,
    19: _migrate_to_v19,
    20: _migrate_to_v20,
    21: _migrate_to_v21,
    22: _migrate_to_v22,
    23: _migrate_to_v23,
    24: _migrate_to_v24,
}


# ---- SQLite 瞬时故障重试 ----
# WAL 下「每操作开一条连接」+「主线程轮询与 worker 线程并发写」会撞上瞬时故障：
# 最后一个连接关闭时 checkpoint 并删除 -shm/-wal，此刻另一个刚打开的连接写库会报
# ``attempt to write a readonly database``（SQLITE_READONLY_CANTINIT）。它不是「库真的
# 只读」，重连即成功——但 ``jobs._emit`` 的容错会把它吞成「静默丢一行事件」，
# 实测任务面板因此偶发少一行过程日志（22 次循环复现 7 次异常、1 次断言失败）。
# 事件写入是追加语义（sequence 在事务内重算）、Job 更新是幂等 UPDATE，
# 因此重试不会产生重复或断号副作用。
_TRANSIENT_SQLITE_MARKERS: tuple[str, ...] = (
    "attempt to write a readonly database",
    "database is locked",
    "database table is locked",
    "unable to open database file",
)
_SQLITE_WRITE_ATTEMPTS = 4
_SQLITE_WRITE_RETRY_DELAY = 0.05


def _is_transient_sqlite_error(exc: BaseException) -> bool:
    """是否「重连即好」的瞬时 SQLite 故障（非瞬时错误一律原样抛出，不得掩盖）。"""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_SQLITE_MARKERS)


def _retry_transient_write(method: Callable[..., Any]) -> Callable[..., Any]:
    """把**整个方法体**当作一次写操作重试（判据同 ``ChatStorage._write_with_retry``）。

    为什么整方法重试是安全的：被装饰的方法全部满足两条——
    (1) 副作用都在**单个** ``BEGIN IMMEDIATE`` 事务里，失败即随 ``with`` 上下文回滚，
        不会留下半截写入；
    (2) 返回的 id 要么在方法内生成、要么写后回读 ⇒ 重试不会让调用方拿到一个
        「写进去又没了的 id」。
    业务错误（``LookupError`` / ``RuntimeError("ACTIVE_RUN:…")`` / ``ValueError``）都不是
    ``OperationalError``，一次都不会重试。

    2026-09-25 起因：上一轮修复只覆盖了 ``append_run_event`` / ``update_job`` 两处，
    其余写路径裸奔。拍教程截图时 ``update_run_snapshot`` 撞上 WAL 瞬时
    ``SQLITE_READONLY_CANTINIT``，**整个回合直接被打死**，用户看到的是
    「请求失败：attempt to write a readonly database」——写路径的重试覆盖必须是
    「全部」而不是「记得的那几处」，故收敛成一个装饰器 + 结构性守门用例。
    """

    @functools.wraps(method)
    def wrapper(self: "ChatStorage", *args: Any, **kwargs: Any) -> Any:
        return self._write_with_retry(lambda: method(self, *args, **kwargs))

    return wrapper


def _json_escaped_literal(text: str) -> str:
    """字符串在 ``json.dumps(..., ensure_ascii=False)`` 输出里的**字面量**形态（不含首尾引号）。

    metadata/snapshot 的路径值就是这么存的（``"path": "D:\\data\\uploads\\x.png"``），
    所以「在原文里找路径」= 找这个转义后的片段。
    """
    return json.dumps(str(text), ensure_ascii=False)[1:-1]


def _unescape_json_literal(literal: str) -> str:
    """把 JSON 字符串字面量（不含首尾引号）还原成原文；坏转义时退回朴素替换。"""
    try:
        return json.loads('"' + literal + '"')
    except (json.JSONDecodeError, ValueError):
        return literal.replace("\\\\", "\\")


def _iter_escaped_literals(text: str, needle: str) -> Iterator[str]:
    """在 JSON 原文里找出所有以 ``needle`` 开头的字符串字面量并还原成原文。

    边界：从 needle 命中处向后扫到**未转义的** ``"``（路径里不会出现裸引号，
    转义序列 ``\\x`` 整体跳过），得到字面量片段后交给 ``json.loads`` 还原。
    needle 本身已是转义形态，故命中位置起 ``len(needle)`` 个字符一定是原文的转义写法。
    """
    start = 0
    size = len(text)
    while True:
        index = text.find(needle, start)
        if index < 0:
            return
        end = index + len(needle)
        while end < size:
            char = text[end]
            if char == "\\":
                end += 2
                continue
            if char == '"':
                break
            end += 1
        yield _unescape_json_literal(text[index:min(end, size)])
        start = index + len(needle)


# `update_job` 幂等短路允许比对的**标量**列：值是简单类型、比较代价与读一次行相当。
# checkpoint/result/detail/error 明确排除——它们是 JSON 大字段，为比较而反序列化不划算。
_JOB_SCALAR_FIELDS: frozenset[str] = frozenset(
    {"status", "progress", "current_step", "attempt", "cancel_requested", "started_at", "finished_at"}
)


def _job_values_unchanged(values: dict[str, Any], current: dict[str, Any] | None) -> bool:
    """`update_job` 要写的值是否与库里当前值逐字相同（相同则这次 UPDATE 无信息量）。

    除 `updated_at`（每次都带、必然不同）外的字段若全在标量白名单里且没变，就判定为
    no-op。任何读不到的行（current 为 None）都按"不相等"处理，让调用方照常走写路径，
    沿用它既有的"任务不存在 → 返回 None"语义。
    """
    if not current:
        return False
    compared = False
    for key, new_value in values.items():
        if key == "updated_at":
            continue
        if key not in _JOB_SCALAR_FIELDS:
            return False
        old_value = current.get(key)
        if isinstance(new_value, float) or isinstance(old_value, float):
            try:
                if math.isclose(float(old_value or 0), float(new_value or 0), abs_tol=1e-6):
                    compared = True
                    continue
            except (TypeError, ValueError):
                return False
            return False
        if old_value != new_value:
            return False
        compared = True
    return compared


# ---- trace 的内容寻址存储（v23 起）----
# `metadata.trace` 是「本轮发给模型的完整字节序列」，按 assistant 消息各存一份；实测
# messages.metadata 221.7MB 里它占 167.4MB，且 36% 是逐字节重复的同一份 blob。
# 抽到 `message_traces(trace_hash, data)` 后：同一 blob 全库一行；读取侧由
# `_hydrate_trace` 透明补回 `metadata["trace"]`，**所有消费方（history/前端/导出）零改动**。
def _trace_blob(trace: Any) -> str | None:
    """把一条消息的 trace 序列化成落库文本；空 trace 与"没有 trace"等价，返回 None。"""
    if not isinstance(trace, list) or not trace:
        return None
    try:
        return json.dumps(trace, ensure_ascii=False)
    except (TypeError, ValueError):
        # 不可序列化（理论上不会发生）：按"没有 trace"处理，绝不让它打断消息落库。
        return None


def _split_trace_payload(metadata: dict[str, Any]) -> tuple[str, str] | None:
    """从 metadata 里**摘出** trace 并返回 (trace_hash, payload)；无 trace 返回 None。

    就地修改入参（调用方传的是自己的副本）。trace 为空列表时只摘键、不建表行。
    """
    if "trace" not in metadata:
        return None
    blob = _trace_blob(metadata.pop("trace"))
    if blob is None:
        return None
    return hashlib.sha256(blob.encode("utf-8")).hexdigest(), blob


def _hydrate_trace(db: sqlite3.Connection, trace_hash: str, cache: dict[str, str]) -> list[Any] | None:
    """按 hash 取回 trace 列表；取不到（行缺失/损坏）返回 None（调用方保持无 trace）。"""
    if not trace_hash:
        return None
    if trace_hash not in cache:
        row = db.execute(
            "SELECT data FROM message_traces WHERE trace_hash = ?", (trace_hash,)
        ).fetchone()
        cache[trace_hash] = str(row[0]) if row and row[0] else ""
    blob = cache.get(trace_hash) or ""
    if not blob:
        return None
    try:
        value = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, list) else None


class ChatStorage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # 本次启动被判为「服务重启中断」的 in-flight 任务（id/conversation_id/kind）。
        # 只做记录，不做恢复：正文重建要用 run_events，那属于运行层（见
        # ConversationRunMixin.recover_interrupted_runs）。
        self.interrupted_tasks: list[dict[str, str]] = []
        self._initialize()

    def upload_path_referenced(self, target: Path) -> bool:
        """目标上传文件是否已被引用（messages.metadata / background_tasks.snapshot / traces）。

        删除保护：上传文件被任何消息附件或 run 快照引用后不可删除，
        避免移除 chip 的 DELETE 误删"已发送/已引用"的文件（防御双端竞态）。

        `message_traces.data` 也必须扫：v23 起 `metadata.trace` 搬进了独立表，而 trace 里
        会出现宿主缓存路径（实测 131 条消息）。漏掉它就会把仍被 trace 引用的图片当"没人用"
        删掉（模型下一轮 vision_analyze 直接报"未找到图片文件"）。
        """
        # metadata/snapshot 以 json.dumps(ensure_ascii=False) 存储：路径值形如
        # "path": "C:\\...\\x.pdf"。用 JSON 转义后的片段做 LIKE 子串匹配。
        escaped = _json_escaped_literal(str(target))
        like = "%" + escaped.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM messages WHERE metadata LIKE ? ESCAPE '\\' LIMIT 1", (like,)
            ).fetchone()
            if row:
                return True
            row = db.execute(
                "SELECT 1 FROM background_tasks WHERE snapshot LIKE ? ESCAPE '\\' LIMIT 1", (like,)
            ).fetchone()
            if row:
                return True
            row = db.execute(
                "SELECT 1 FROM message_traces WHERE data LIKE ? ESCAPE '\\' LIMIT 1", (like,)
            ).fetchone()
            return bool(row)

    def referenced_cache_paths(self, roots: Iterable[str | Path]) -> set[str]:
        """一次扫描读出「被引用的」缓存文件路径集合（替代逐文件 LIKE 全表扫）。

        背景（2026-09-29 实测）：缓存分组 509 组、messages.metadata 合计 118MB 时，
        逐组 2 条 ``LIKE '%<转义路径>%'`` 全表扫描一轮要 58 秒——而这轮扫描被同步挂在
        POST /api/uploads 的响应里，用户看到的是"上传 78 秒"。改成**一次流式扫描**、
        把结果建成集合后按组查表，成本从 O(组数 × 全表) 降到 O(全表) 一次。

        口径与 ``upload_path_referenced`` **保持一致**（同样只认 messages.metadata、
        background_tasks.snapshot 与 message_traces.data，同样按"JSON 转义后的字面路径"
        匹配），只是把"逐文件问一次"换成"整表找一遍"。

        实现：按**转义后的目录前缀**在原文里做 C 级 ``str.find``，比逐行 ``json.loads``
        （118MB 级）快一个数量级，而且某一行 JSON 损坏也不会让整批判定失败
        （那种情况下该行的路径进不了集合，删除前还有 ``upload_path_referenced`` 复核兜底）。

        v23 起 trace 在 `message_traces.data`：漏扫这一表 = 缓存清理看不见 trace 里的引用
        （实测 131 条消息），会把仍在对话里用的图片当"没人用"删掉。

        返回值为 ``normalized_path_key`` 规范化后的比较键集合（绝对路径 + 大小写归一）。
        """
        needles: set[str] = set()
        for root in roots or ():
            text = str(root or "").strip()
            if not text:
                continue
            needles.add(_json_escaped_literal(text))
            forward = text.replace("\\", "/")
            if forward != text:
                # 少数记录里的路径以正斜杠落库（跨平台复制/手工改过的 metadata）。
                needles.add(_json_escaped_literal(forward))
        if not needles:
            return set()
        found: set[str] = set()
        with self._connect() as db:
            for table, column in (
                ("messages", "metadata"),
                ("background_tasks", "snapshot"),
                ("message_traces", "data"),
            ):
                cursor = db.execute(f"SELECT {column} FROM {table}")
                for row in cursor:
                    text = row[0]
                    if not isinstance(text, str) or not text:
                        continue
                    for needle in needles:
                        for raw in _iter_escaped_literals(text, needle):
                            key = normalized_path_key(raw)
                            if key:
                                found.add(key)
        return found

    def _write_with_retry(self, operation: Callable[[], Any]) -> Any:
        """执行写操作；遇瞬时 SQLite 故障自动重试（判据见 ``_is_transient_sqlite_error``）。

        只重试已知的瞬时形态；其余异常（含 ``LookupError``）立即原样抛出，
        绝不掩盖真实错误。次数用尽后抛出最后一次的瞬时异常。
        """
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(_SQLITE_WRITE_ATTEMPTS):
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                if not _is_transient_sqlite_error(exc):
                    raise
                last_error = exc
                time.sleep(_SQLITE_WRITE_RETRY_DELAY * (attempt + 1))
        raise last_error if last_error is not None else sqlite3.OperationalError("写操作重试耗尽")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'online',
                    permission_mode TEXT NOT NULL DEFAULT 'confirm',
                    web_search_enabled INTEGER NOT NULL DEFAULT 0,
                    deep_reasoning_enabled INTEGER NOT NULL DEFAULT 0,
                    lightweight_mode INTEGER NOT NULL DEFAULT 0,
                    lightweight_disabled_features TEXT NOT NULL DEFAULT '[]',
                    title_customized INTEGER NOT NULL DEFAULT 0,
                    system_prompt TEXT NOT NULL DEFAULT '',
                    stream_enabled INTEGER NOT NULL DEFAULT 1,
                    workspace_dir TEXT NOT NULL DEFAULT '',
                    reasoning_effort TEXT NOT NULL DEFAULT 'off',
                    enabled_tool_ids TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                    ON messages(conversation_id, created_at);
                CREATE TABLE IF NOT EXISTS message_traces (
                    trace_hash TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS background_tasks (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'chat',
                    interaction_mode TEXT NOT NULL DEFAULT 'craft',
                    input_message_id TEXT NOT NULL DEFAULT '',
                    plan_id TEXT NOT NULL DEFAULT '',
                    agent_id TEXT NOT NULL DEFAULT '',
                    agent_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    snapshot TEXT NOT NULL DEFAULT '{}',
                    detail TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    started_at INTEGER,
                    updated_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_background_tasks_status
                    ON background_tasks(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_background_tasks_conversation
                    ON background_tasks(conversation_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS run_events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(run_id, sequence),
                    FOREIGN KEY(run_id) REFERENCES background_tasks(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_run_events_run
                    ON run_events(run_id, sequence);
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'prepare',
                    question TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    steps TEXT NOT NULL DEFAULT '[]',
                    error TEXT NOT NULL DEFAULT '',
                    archive_path TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    started_at INTEGER,
                    finished_at INTEGER,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_plans_conversation
                    ON plans(conversation_id, created_at DESC);
                -- 已清理的 Job 痕迹：清理终结记录时保留 job_id，便于跨对话查询
                -- 区分「从未创建」与「记录已被清理」，避免含糊的“无权访问”。
                -- 独立于 background_tasks 存在（无外键），清理后仍可查询。
                CREATE TABLE IF NOT EXISTS cleaned_jobs (
                    job_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL DEFAULT '',
                    cleaned_at INTEGER NOT NULL
                );
                """
            )
            # 增量迁移（列新增 / 旧数据回填）由 apply_pending_migrations() 在重启清理之后统一执行。
            now = int(time.time() * 1000)
            # ACTIVE_TASK_STATUSES 必须覆盖全部未结束状态：漏掉 stopping 会让
            # 「已取消但线程没退出去」的子任务存活过重启，成为永久僵尸。
            active_clause = _status_in_clause(ACTIVE_TASK_STATUSES)
            interrupted = db.execute(
                "SELECT id, conversation_id, kind FROM background_tasks "
                f"WHERE status IN {active_clause}"
            ).fetchall()
            # 记录本次被判为中断的任务，供运行层把「已经吐出来的正文」重建落库
            # （见 ConversationRunMixin.recover_interrupted_runs）。
            self.interrupted_tasks = [
                {
                    "id": str(row["id"]),
                    "conversation_id": str(row["conversation_id"] or ""),
                    "kind": str(row["kind"] or ""),
                }
                for row in interrupted
            ]
            # Harness 对齐：运行中任务在服务重启后变为 interrupted，而非静默丢失
            db.execute(
                "UPDATE background_tasks SET status = 'interrupted', error = ?, updated_at = ?, finished_at = ? "
                f"WHERE status IN {active_clause}",
                (INTERRUPTED_TASK_REASON, now, now),
            )
            for row in interrupted:
                sequence = db.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id = ?",
                    (row["id"],),
                ).fetchone()[0]
                payload = json.dumps(
                    {"type": "error", "message": INTERRUPTED_TASK_REASON},
                    ensure_ascii=False,
                )
                db.execute(
                    "INSERT INTO run_events(run_id, sequence, event_type, payload, created_at) "
                    "VALUES (?, ?, 'error', ?, ?)",
                    (row["id"], sequence, payload, now),
                )
            # 服务重启时，仍在执行的计划标记为已取消，running 步骤回退为 pending
            stuck_plans = db.execute("SELECT id, steps FROM plans WHERE status = 'building'").fetchall()
            for plan_row in stuck_plans:
                try:
                    steps = json.loads(plan_row["steps"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    steps = []
                for step in steps:
                    if isinstance(step, dict) and step.get("status") == "running":
                        step["status"] = "pending"
                db.execute(
                    "UPDATE plans SET status = 'cancelled', error = '服务重启，执行已中断', steps = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(steps, ensure_ascii=False), now, plan_row["id"]),
                )
        # Schema 创建 + 重启清理完成后，应用尚未执行的版本化迁移。
        self.apply_pending_migrations()
        # Keep legacy data repair idempotent after the schema reaches v1.
        # Older builds may have written rows after the version was recorded.
        self._repair_legacy_model_keys()
        self._disable_legacy_plan_mode()

    @_retry_transient_write
    def _repair_legacy_model_keys(self) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE conversations SET model_key = 'online:' || provider_id "
                "WHERE model_key = '' AND provider_id != ''"
            )

    @_retry_transient_write
    def _disable_legacy_plan_mode(self) -> None:
        """Keep historical plans, but make every conversation use normal chat."""
        with self._connect() as db:
            db.execute("UPDATE conversations SET interaction_mode = 'craft' WHERE interaction_mode != 'craft'")

    @_retry_transient_write
    def synchronize_workspace_bindings(self, workspace_dirs: dict[str, str]) -> int:
        """Repair conversations whose sidebar group and workspace directory disagree.

        ``workspace_group`` is a presentation field, but for a registered
        workspace it must always resolve to that workspace's directory.  This
        repair intentionally leaves ungrouped and no-longer-registered groups
        untouched: ungrouping a conversation must not move its files.
        """
        bindings = {
            str(name).strip(): str(directory).strip()
            for name, directory in workspace_dirs.items()
            if str(name).strip() and str(directory).strip()
        }
        if not bindings:
            return 0
        now = int(time.time() * 1000)
        changed = 0
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, workspace_group, workspace_dir FROM conversations "
                "WHERE TRIM(workspace_group) != ''"
            ).fetchall()
            for row in rows:
                group = str(row["workspace_group"] or "").strip()
                directory = bindings.get(group)
                if not directory or str(row["workspace_dir"] or "").strip() == directory:
                    continue
                db.execute(
                    "UPDATE conversations SET workspace_dir = ?, updated_at = ? WHERE id = ?",
                    (directory, now, row["id"]),
                )
                changed += 1
        return changed

    @_retry_transient_write
    def apply_pending_migrations(self) -> None:
        """依次应用尚未执行的迁移，直到 user_version == CURRENT_SCHEMA_VERSION。"""
        # 数据改写型迁移（v14 起：合并/收缩存量行）执行前自动整库备份到 data/backups，
        # 保证可回滚；全新库（user_version=0）无存量数据无需备份。备份失败仅记录诊断
        # 并继续（被删除数据均有等价替代：合流保留全文、done 消息与 snapshot 历史
        # 均可在 messages 表重建语义等价内容）。
        current_version = self.get_user_version()
        if 0 < current_version < FIRST_DATA_WRITING_MIGRATION:
            backup = self.backup_for_migration(self.data_dir / "backups")
            if backup.get("error"):
                print(
                    f"[naiba-storage] 迁移前备份失败（继续迁移）：{backup['error']}",
                    file=sys.stderr,
                )
        with self._connect() as db:
            while int(db.execute("PRAGMA user_version").fetchone()[0]) < CURRENT_SCHEMA_VERSION:
                target = int(db.execute("PRAGMA user_version").fetchone()[0]) + 1
                migration = MIGRATIONS.get(target)
                if migration is None:
                    # 没有对应迁移定义则向前跳版本，避免死循环。
                    db.execute(f"PRAGMA user_version = {int(target)}")
                    continue
                migration(db)
                db.execute(f"PRAGMA user_version = {int(target)}")

    def get_user_version(self) -> int:
        with self._connect() as db:
            return int(db.execute("PRAGMA user_version").fetchone()[0])

    @_retry_transient_write
    def set_user_version(self, version: int) -> None:
        with self._connect() as db:
            db.execute(f"PRAGMA user_version = {int(version)}")

    def check_integrity(self) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute("PRAGMA integrity_check").fetchall()
        details = [str(row[0]) for row in rows]
        return {"ok": all(d == "ok" for d in details), "details": details}

    def storage_usage(self) -> dict[str, Any]:
        """历史数据统计（设置页「历史数据管理」展示用）：数据库大小与关键表规模。"""
        size = self.db_path.stat().st_size if self.db_path.exists() else 0
        with self._connect() as db:
            events = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(payload)), 0) FROM run_events"
            ).fetchone()
            tasks = db.execute(
                "SELECT COUNT(*), "
                "COALESCE(SUM(CASE WHEN status IN ('completed','failed','cancelled','interrupted') "
                "THEN 1 ELSE 0 END), 0) FROM background_tasks"
            ).fetchone()
            snapshots = db.execute(
                "SELECT COALESCE(SUM(LENGTH(snapshot)), 0) FROM background_tasks"
            ).fetchone()
            messages = db.execute("SELECT COUNT(*) FROM messages").fetchone()
        return {
            "db_bytes": int(size),
            "event_count": int(events[0]),
            "event_payload_chars": int(events[1] or 0),
            "task_count": int(tasks[0]),
            "terminal_task_count": int(tasks[1] or 0),
            "snapshot_chars": int(snapshots[0] or 0),
            "message_count": int(messages[0]),
        }

    @_retry_transient_write
    def compress_run_events(self, run_id: str) -> int:
        """run 终态后压缩该 run 的事件流：流式期逐块落库的 reasoning_delta 合流为整段。

        与迁移 v14 同口径（2048 字符 / 1s 窗口）；幂等。合流发生在终态事件
        （done/cancelled/error）落库之后，前端收到终态即停止轮询，安全。
        不执行 VACUUM（空间回收走设置页「压缩数据库」）。返回处理行数。
        """
        with self._connect() as db:
            return _coalesce_reasoning_deltas(db, run_id=run_id)

    @_retry_transient_write
    def slim_terminal_run(self, run_id: str) -> dict[str, int]:
        """run 终态收尾：给事件流与快照**同时**瘦身（幂等，可重复调用）。

        为什么要在运行期做（而不是像 v14 那样只在迁移里做）：`done`/`cancelled` 事件带着
        完整消息对象（content + metadata + **含 trace 的 metadata**），与 `messages` 表、
        `metadata.trace` 完全重复；`create_chat_run` 又会把整段会话固化进 snapshot。两者在
        run 终态后都没有读取方，却会一直堆在库里。实测存量：done/cancelled **35.0MB / 228 行
        全部仍带 message 对象**，终态快照 `conversation_messages` 曾经累积到 81MB。

        调用时机：必须在**终态事件已经 emit 之后**——前端是唯一读者，它收到终态即停止轮询；
        早于此调用会让前端拿不到即时渲染所需的消息对象。

        返回 `{"events": 处理行数, "snapshots": 处理行数}`；失败由调用方旁路（收尾不阻断）。
        """
        with self._connect() as db:
            events = _slim_terminal_event_payload_rows(db, run_id)
            snapshots = _slim_terminal_snapshot_rows(db, run_id)
        return {"events": events, "snapshots": snapshots}

    @_retry_transient_write
    def compact_database(self) -> dict[str, Any]:
        """VACUUM 物理收缩数据库（回收已清理历史数据占用的磁盘空间）。

        执行期间短暂独占数据库；应用为单实例（server.lock 互斥），同步执行安全。
        失败（磁盘空间不足/文件锁）抛 OperationalError，由调用方明确报错。
        """
        before = self.db_path.stat().st_size if self.db_path.exists() else 0
        with self._connect() as db:
            db.commit()
            db.execute("VACUUM")
        after = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {"before_bytes": int(before), "after_bytes": int(after)}

    def backup_for_migration(self, backup_dir: Path) -> dict[str, Any]:
        files: list[str] = []
        error: str | None = None
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            for suffix in ("", "-wal", "-shm"):
                source = Path(str(self.db_path) + suffix)
                if source.exists():
                    destination = backup_dir / source.name
                    shutil.copy2(source, destination)
                    files.append(str(destination))
        except Exception as exc:  # noqa: BLE001 - 备份失败需要以 error 形式返回
            error = str(exc)
        return {"backup_dir": str(backup_dir), "files": files, "error": error}

    @property
    def data_dir(self) -> Path:
        return self.db_path.parent

    @property
    def health(self) -> dict[str, Any]:
        db_version = self.get_user_version()
        healthy = self.check_integrity()["ok"]
        applied = [v for v in sorted(MIGRATIONS) if v <= CURRENT_SCHEMA_VERSION]
        return {
            "db_version": db_version,
            "data_dir": str(self.data_dir),
            "healthy": healthy,
            "migrations": applied,
        }

    @_retry_transient_write
    def create_conversation(
        self,
        title: str = "新对话",
        provider_id: str = "",
        agent_id: str = "",
        interaction_mode: str = "craft",
        model_key: str = "",
        model_name: str = "",
        permission_mode: str = "auto",
        web_search_enabled: bool = False,
        deep_reasoning_enabled: bool = False,
        workspace_dir: str = "",
        workspace_group: str = "",
        reasoning_effort: str = "auto",
    ) -> dict[str, Any]:
        now = int(time.time() * 1000)
        conversation_id = uuid.uuid4().hex
        interaction_mode = "craft"
        if permission_mode not in ("confirm", "auto", "full"):
            permission_mode = "auto"
        resolved_model_key = str(model_key or "").strip()
        resolved_model_name = str(model_name or "").strip()[:256]
        if not resolved_model_key and provider_id:
            resolved_model_key = f"online:{provider_id}"
        with self._connect() as db:
            db.execute(
                "INSERT INTO conversations(id, title, mode, permission_mode, web_search_enabled, deep_reasoning_enabled, title_customized, system_prompt, stream_enabled, workspace_dir, workspace_group, reasoning_effort, enabled_tool_ids, provider_id, model_key, model_name, agent_id, interaction_mode, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    title.strip() or "新对话",
                    "online",
                    permission_mode,
                    1 if web_search_enabled else 0,
                    1 if deep_reasoning_enabled else 0,
                    0,
                    "",
                    1,
                    str(workspace_dir or ""),
                    str(workspace_group or ""),
                    str(reasoning_effort or ("medium" if deep_reasoning_enabled else "auto")),
                    "",
                    provider_id or "",
                    resolved_model_key,
                    resolved_model_name,
                    agent_id or "",
                    interaction_mode,
                    now,
                    now,
                ),
            )
        return self.get_conversation(conversation_id, include_messages=False)

    def list_conversations(self, mode: str | None = None) -> list[dict[str, Any]]:
        columns = (
            "id, title, mode, permission_mode, web_search_enabled, deep_reasoning_enabled, "
            "lightweight_mode, lightweight_disabled_features, title_customized, system_prompt, "
            "stream_enabled, workspace_dir, workspace_group, reasoning_effort, enabled_tool_ids, "
            "skill_policy, chat_supports_images, provider_id, model_key, model_name, agent_id, "
            "interaction_mode, favorite, archived, sort_order, branched_from_id, "
            "branch_message_id, created_at, updated_at"
        )
        with self._connect() as db:
            if mode:
                rows = db.execute(
                    f"SELECT {columns} FROM conversations WHERE mode = ? ORDER BY updated_at DESC",
                    (mode,),
                ).fetchall()
            else:
                rows = db.execute(
                    f"SELECT {columns} FROM conversations ORDER BY updated_at DESC"
                ).fetchall()
            items = [self._conversation_dict(row) for row in rows]
            # 分支计数与「源标题」一次带出（列表里已有全部标题，缺失的才回查一次），
            # 侧栏徽标因此不需要任何额外请求，也不会退化成逐行 COUNT 的 N+1。
            counts = self._branch_counts(db, [item["id"] for item in items])
            titles = {item["id"]: str(item.get("title") or "") for item in items}
            missing = sorted({
                str(item.get("branched_from_id") or "")
                for item in items
                if str(item.get("branched_from_id") or "") not in titles
            } - {""})
            if missing:
                titles.update(self._conversation_titles(db, missing))
        for item in items:
            item["branch_count"] = counts.get(item["id"], 0)
            item["branch_source_title"] = titles.get(str(item.get("branched_from_id") or ""), "")
        return items

    def _branch_counts(self, db: sqlite3.Connection, conversation_ids: list[str]) -> dict[str, int]:
        """这批会话各自被分支了多少次（一次 GROUP BY 解决，避免逐行 COUNT）。"""
        ids = [str(value) for value in conversation_ids if str(value)]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = db.execute(
            "SELECT branched_from_id AS src, COUNT(*) AS n FROM conversations "
            f"WHERE branched_from_id IN ({placeholders}) GROUP BY branched_from_id",
            ids,
        ).fetchall()
        return {str(row["src"]): int(row["n"] or 0) for row in rows}

    def _conversation_titles(
        self, db: sqlite3.Connection, conversation_ids: list[str]
    ) -> dict[str, str]:
        """id → 标题（只为「分支徽标 tooltip」取的轻量查询；不存在的 id 自然缺席）。"""
        ids = sorted({str(value) for value in conversation_ids if str(value)})
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = db.execute(
            f"SELECT id, title FROM conversations WHERE id IN ({placeholders})", ids
        ).fetchall()
        return {str(row["id"]): str(row["title"] or "") for row in rows}

    def find_conversations(self, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        """按标题关键词找会话（空 query = 最近会话），带消息条数与最后一条消息预览。

        标题是**首条用户消息前 36 字**自动生成的（`title_customized=0`，见 add_message），
        所以"按标题搜"本质是"搜第一条消息的开头"——调用方必须把最后一条消息预览一起
        交给模型，并在标题无命中时引导改用正文检索（`search_history`）。
        """
        limit = max(1, min(int(limit if limit is not None else 20), 50))
        needle = str(query or "").strip()
        params: list[Any] = []
        where = ""
        if needle:
            where = "WHERE instr(lower(c.title), lower(?)) > 0 "
            params.append(needle)
        params.append(limit)
        with self._connect() as db:
            rows = db.execute(
                "SELECT c.id, c.title, c.workspace_group, c.title_customized, "
                "       c.created_at, c.updated_at, "
                "       (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count, "
                "       (SELECT m.content FROM messages m WHERE m.conversation_id = c.id "
                "        ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1) AS last_content "
                "FROM conversations c " + where + "ORDER BY c.updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "title": str(row["title"] or "（无标题）"),
                "workspace_group": str(row["workspace_group"] or ""),
                "title_customized": bool(row["title_customized"]),
                "created_at": int(row["created_at"] or 0),
                "updated_at": int(row["updated_at"] or 0),
                "message_count": int(row["message_count"] or 0),
                "last_content": str(row["last_content"] or ""),
            }
            for row in rows
        ]

    def conversation_context_boundary(self, conversation_id: str) -> tuple[int, int] | None:
        """某会话最新「新会话」分割线的排序键 ``(created_at, rowid)``；没有分割线返回 None。

        与 ``core.history.build_model_history`` 同口径：只有 metadata 带 ``session_start``
        的消息才是边界（早期遗留的 ``role=session`` 标记行也带该键，一条 SQL 覆盖两种形态）。
        **边界那条消息本身不在上下文里**（重放时被跳过），所以"在上下文内"的判定是
        ``(created_at, rowid) > boundary``。
        """
        with self._connect() as db:
            row = db.execute(
                "SELECT created_at, rowid AS rid FROM messages "
                "WHERE conversation_id = ? AND json_extract(metadata, '$.session_start') IS NOT NULL "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        if not row:
            return None
        return int(row["created_at"] or 0), int(row["rid"] or 0)

    def _history_scope_counts(self, db: sqlite3.Connection) -> dict[str, int]:
        row = db.execute(
            "SELECT (SELECT COUNT(*) FROM conversations) AS conversations, "
            "       (SELECT COUNT(*) FROM messages) AS messages"
        ).fetchone()
        return {
            "conversations": int(row["conversations"] or 0),
            "messages": int(row["messages"] or 0),
        }

    def _history_ordinals(self, db: sqlite3.Connection, conversation_ids: list[str]) -> dict[str, int]:
        """会话内消息序号（1 起始，按 ``(created_at, rowid)`` 与 read 接口同口径）。"""
        if not conversation_ids:
            return {}
        placeholders = ",".join("?" for _ in conversation_ids)
        rows = db.execute(
            "SELECT id, ordinal FROM ("
            "  SELECT id, ROW_NUMBER() OVER (PARTITION BY conversation_id "
            "         ORDER BY created_at, rowid) AS ordinal "
            f"  FROM messages WHERE conversation_id IN ({placeholders})"
            ")",
            list(conversation_ids),
        ).fetchall()
        return {str(row["id"]): int(row["ordinal"]) for row in rows}

    def _conversation_history_meta(
        self, db: sqlite3.Connection, conversation_id: str
    ) -> dict[str, Any] | None:
        row = db.execute(
            "SELECT id, title, created_at, updated_at, "
            "       (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count "
            "FROM conversations c WHERE c.id = ?",
            (conversation_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": str(row["id"]),
            "title": str(row["title"] or "（无标题）"),
            "created_at": int(row["created_at"] or 0),
            "updated_at": int(row["updated_at"] or 0),
            "message_count": int(row["message_count"] or 0),
        }

    def search_history(
        self,
        query: str,
        *,
        conversation_id: str = "",
        current_conversation_id: str = "",
        max_conversations: int = 5,
        per_conversation: int = 3,
        max_hits: int = 20,
    ) -> dict[str, Any]:
        """历史会话检索（正文子串匹配，大小写不敏感）。

        - ``conversation_id`` 为空 = 全库模式：每个会话最多 ``per_conversation`` 条最新命中，
          会话之间按 ``updated_at`` 倒序取前 ``max_conversations`` 个；**不设隐性上限**，
          检索范围（会话数/消息数）原样返回给调用方明示给模型。
        - 给了 ``conversation_id`` = 限定模式：只搜该会话，按时间正序返回最多 ``max_hits`` 条，
          并给出该会话命中总数（截断必须显式）。
        - ``current_conversation_id`` 用于标注「这条命中在不在模型当前上下文里」：
          与 ``conversation_context_boundary`` 比较，分割线及以上 = 已划出上下文。
        """
        needle = str(query or "").strip()
        if not needle:
            raise ValueError("query 不能为空")
        with self._connect() as db:
            scope = self._history_scope_counts(db)
            if conversation_id:
                meta = self._conversation_history_meta(db, conversation_id)
                if meta is None:
                    return {"mode": "conversation", "query": needle,
                            "conversation_id": conversation_id, "missing": True,
                            "scope": scope, "conversations": [], "total_hits": 0, "truncated": False}
                total = int(db.execute(
                    "SELECT COUNT(*) FROM messages WHERE conversation_id = ? "
                    "AND instr(lower(content), lower(?)) > 0",
                    (conversation_id, needle),
                ).fetchone()[0] or 0)
                rows = db.execute(
                    "SELECT id, role, content, created_at, rowid AS rid FROM messages "
                    "WHERE conversation_id = ? AND instr(lower(content), lower(?)) > 0 "
                    "ORDER BY created_at, rowid LIMIT ?",
                    (conversation_id, needle, max(1, min(int(max_hits or 20), 50))),
                ).fetchall()
                ordinals = self._history_ordinals(db, [conversation_id])
                boundary = self.conversation_context_boundary(conversation_id)
                hits = [self._history_hit(row, ordinals, boundary) for row in rows]
                meta = dict(meta)
                meta["is_current"] = conversation_id == current_conversation_id
                return {
                    "mode": "conversation", "query": needle, "conversation_id": conversation_id,
                    "missing": False, "scope": scope, "conversations": [meta],
                    "hits": hits, "total_hits": total,
                    "truncated": total > len(hits),
                }
            rows = db.execute(
                "WITH hits AS ("
                "  SELECT m.conversation_id AS cid, m.id AS mid, m.role AS role, "
                "         m.content AS content, m.created_at AS created_at, m.rowid AS rid, "
                "         ROW_NUMBER() OVER (PARTITION BY m.conversation_id "
                "                            ORDER BY m.created_at DESC, m.rowid DESC) AS rn "
                "  FROM messages m WHERE instr(lower(m.content), lower(?)) > 0"
                ") "
                "SELECT h.cid, h.mid, h.role, h.content, h.created_at, h.rid, h.rn, "
                "       c.title, c.updated_at AS conv_updated, "
                "       (SELECT COUNT(*) FROM messages m2 WHERE m2.conversation_id = h.cid) AS message_count "
                "FROM hits h JOIN conversations c ON c.id = h.cid "
                "WHERE h.rn <= ? "
                "ORDER BY c.updated_at DESC, h.created_at DESC, h.rid DESC",
                (needle, max(1, min(int(per_conversation or 3), 10))),
            ).fetchall()
            total_hits = int(db.execute(
                "SELECT COUNT(*) FROM messages WHERE instr(lower(content), lower(?)) > 0",
                (needle,),
            ).fetchone()[0] or 0)
            buckets: dict[str, dict[str, Any]] = {}
            order: list[str] = []
            for row in rows:
                cid = str(row["cid"])
                if cid not in buckets:
                    if len(order) >= max(1, min(int(max_conversations or 5), 20)):
                        continue
                    order.append(cid)
                    buckets[cid] = {
                        "id": cid,
                        "title": str(row["title"] or "（无标题）"),
                        "updated_at": int(row["conv_updated"] or 0),
                        "message_count": int(row["message_count"] or 0),
                        "is_current": cid == current_conversation_id,
                        "hits": [],
                    }
                buckets[cid]["hits"].append({
                    "message_id": str(row["mid"]),
                    "role": str(row["role"] or ""),
                    "content": str(row["content"] or ""),
                    "created_at": int(row["created_at"] or 0),
                    "rowid": int(row["rid"] or 0),
                })
            ordinals = self._history_ordinals(db, order)
            boundary = (self.conversation_context_boundary(current_conversation_id)
                        if current_conversation_id else None)
            for cid in order:
                for hit in buckets[cid]["hits"]:
                    hit["ordinal"] = ordinals.get(hit["message_id"], 0)
                    rowid = int(hit.pop("rowid", 0))
                    hit["in_context"] = boundary is None or (hit["created_at"], rowid) > boundary
        return {
            "mode": "global", "query": needle, "conversation_id": "", "missing": False,
            "scope": scope, "conversations": [buckets[cid] for cid in order],
            "total_hits": total_hits, "truncated": total_hits > sum(len(b["hits"]) for b in buckets.values()),
        }

    @staticmethod
    def _history_hit(row: sqlite3.Row, ordinals: dict[str, int], boundary: tuple[int, int] | None) -> dict[str, Any]:
        created_at = int(row["created_at"] or 0)
        rid = int(row["rid"] or 0)
        return {
            "message_id": str(row["id"]),
            "role": str(row["role"] or ""),
            "content": str(row["content"] or ""),
            "created_at": created_at,
            "ordinal": ordinals.get(str(row["id"]), 0),
            "in_context": boundary is None or (created_at, rid) > boundary,
        }

    # ---- 侧栏「全文搜索」的只读查询 ----
    # 与 search_history 共用同一套匹配口径（instr(lower()) 子串、同一条
    # conversation_context_boundary / _history_ordinals 判定），但**返回形态不同**：
    # search_history 是给模型看的（按会话分桶、每会话限 N 条、偏「有哪些相关会话」），
    # 而侧栏要的是「按最近使用排下来的一串命中」——分桶会把它压成每会话 3 条，
    # 用户搜同一个词时看不到完整结果。所以这里单开一条扁平查询，不改 search_history。
    SNIPPET_CONTEXT_CHARS = 60

    def search_messages(
        self,
        query: str,
        *,
        conversation_id: str = "",
        limit: int = 30,
    ) -> dict[str, Any]:
        """全文搜索（UI 口径）：一次给出一串按「会话最近使用 + 消息时间」排序的命中。

        - ``limit`` 已在调用方夹到 [1, 50]；``truncated`` 显式标注是否被截断，绝不静默丢弃。
        - 每条的 ``snippet`` 是**原文的连续子串**（命中词前后各 ``SNIPPET_CONTEXT_CHARS`` 字），
          前端据此做高亮时不需要再回查正文。
        - ``in_context`` 按**每条命中自己那条会话**的分割线判定：命中在最新分割线之前 = 已划出上下文。
        ``conversation_id`` 指定但不存在时抛 ``LookupError``（路由层转 404）。
        """
        needle = str(query or "").strip()
        if not needle:
            raise ValueError("query 不能为空")
        size = max(1, min(int(limit or 30), 50))
        with self._connect() as db:
            scope = self._history_scope_counts(db)
            if conversation_id:
                meta = self._conversation_history_meta(db, conversation_id)
                if meta is None:
                    raise LookupError("对话不存在")
                total = int(db.execute(
                    "SELECT COUNT(*) FROM messages WHERE conversation_id = ? "
                    "AND instr(lower(content), lower(?)) > 0",
                    (conversation_id, needle),
                ).fetchone()[0] or 0)
                rows = db.execute(
                    "SELECT m.id AS mid, m.role AS role, m.content AS content, "
                    "       m.created_at AS created_at, m.rowid AS rid, "
                    "       c.id AS cid, c.title AS title, c.updated_at AS conv_updated "
                    "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                    "WHERE m.conversation_id = ? AND instr(lower(m.content), lower(?)) > 0 "
                    "ORDER BY m.created_at, m.rowid LIMIT ?",
                    (conversation_id, needle, size),
                ).fetchall()
            else:
                # 全局口径默认**排除已归档会话**（照 DeepSeek Harness：归档=从搜索与
                # 列表同时隐藏）。指定会话 id 的站内搜索不排除——归档会话仍可打开，
                # 在它内部搜自己的历史必须照常工作。
                total = int(db.execute(
                    "SELECT COUNT(*) FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                    "WHERE instr(lower(m.content), lower(?)) > 0 AND c.archived = 0",
                    (needle,),
                ).fetchone()[0] or 0)
                rows = db.execute(
                    "SELECT m.id AS mid, m.role AS role, m.content AS content, "
                    "       m.created_at AS created_at, m.rowid AS rid, "
                    "       c.id AS cid, c.title AS title, c.updated_at AS conv_updated "
                    "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                    "WHERE instr(lower(m.content), lower(?)) > 0 AND c.archived = 0 "
                    "ORDER BY c.updated_at DESC, m.created_at DESC, m.rowid DESC LIMIT ?",
                    (needle, size),
                ).fetchall()
            cids = list(dict.fromkeys(str(row["cid"]) for row in rows))
            ordinals = self._history_ordinals(db, cids)
            boundaries = {cid: self.conversation_context_boundary(cid) for cid in cids}
        hits = []
        for row in rows:
            cid = str(row["cid"])
            created_at = int(row["created_at"] or 0)
            rid = int(row["rid"] or 0)
            boundary = boundaries.get(cid)
            hits.append({
                "conversation_id": cid,
                "conversation_title": str(row["title"] or "（无标题）"),
                "conversation_updated_at": int(row["conv_updated"] or 0),
                "message_id": str(row["mid"]),
                "role": str(row["role"] or ""),
                "ordinal": ordinals.get(str(row["mid"]), 0),
                "snippet": self.snippet_around(str(row["content"] or ""), needle),
                "created_at": created_at,
                "in_context": boundary is None or (created_at, rid) > boundary,
            })
        return {
            "query": needle,
            "conversation_id": conversation_id,
            "total_hits": total,
            "returned": len(hits),
            "truncated": total > len(hits),
            "limit": size,
            "scope": scope,
            "hits": hits,
        }

    @classmethod
    def snippet_around(cls, content: str, needle: str) -> str:
        """命中词前后各 ``SNIPPET_CONTEXT_CHARS`` 字的**连续原文**片段（两端按需加省略号）。"""
        text = str(content or "")
        if not needle:
            return text[: cls.SNIPPET_CONTEXT_CHARS * 2]
        at = text.lower().find(needle.lower())
        if at < 0:
            return text[: cls.SNIPPET_CONTEXT_CHARS * 2]
        span = cls.SNIPPET_CONTEXT_CHARS
        start = max(0, at - span)
        end = min(len(text), at + len(needle) + span)
        snippet = text[start:end]
        if start > 0:
            snippet = "…" + snippet
        if end < len(text):
            snippet = snippet + "…"
        return snippet

    def read_conversation_messages(
        self, conversation_id: str, *, start: int = 1, count: int = 20
    ) -> dict[str, Any] | None:
        """按序号区间读某会话原文（序号 1 起始，按 ``(created_at, rowid)``）；不存在返回 None。"""
        start = max(1, int(start or 1))
        count = max(1, min(int(count or 20), 50))
        with self._connect() as db:
            meta = self._conversation_history_meta(db, conversation_id)
            if meta is None:
                return None
            rows = db.execute(
                "SELECT id, role, content, created_at, metadata, ordinal FROM ("
                "  SELECT id, role, content, created_at, metadata, "
                "         ROW_NUMBER() OVER (ORDER BY created_at, rowid) AS ordinal "
                "  FROM messages WHERE conversation_id = ?"
                ") WHERE ordinal >= ? AND ordinal < ? ORDER BY ordinal",
                (conversation_id, start, start + count),
            ).fetchall()
        messages: list[dict[str, Any]] = []
        for row in rows:
            attachments: list[str] = []
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            for item in (metadata.get("attachments") or []) if isinstance(metadata, dict) else []:
                if isinstance(item, dict) and item.get("name"):
                    attachments.append(str(item["name"]))
            messages.append({
                "ordinal": int(row["ordinal"] or 0),
                "role": str(row["role"] or ""),
                "content": str(row["content"] or ""),
                "created_at": int(row["created_at"] or 0),
                "attachments": attachments,
            })
        meta["messages"] = messages
        meta["start"] = start
        meta["count"] = count
        return meta

    def get_conversation(self, conversation_id: str, include_messages: bool = True) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id, title, mode, permission_mode, web_search_enabled, deep_reasoning_enabled, lightweight_mode, lightweight_disabled_features, title_customized, system_prompt, stream_enabled, workspace_dir, workspace_group, reasoning_effort, enabled_tool_ids, skill_policy, chat_supports_images, provider_id, model_key, model_name, agent_id, interaction_mode, favorite, archived, sort_order, branched_from_id, branch_message_id, created_at, updated_at "
                "FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if not row:
                return None
            result = self._conversation_dict(row)
            # 详情也要带分支字段：分支链面板点开后要立刻显示「源是谁」，不该再打一次列表接口。
            source_id = str(result.get("branched_from_id") or "")
            result["branch_count"] = self._branch_counts(db, [conversation_id]).get(conversation_id, 0)
            result["branch_source_title"] = (
                self._conversation_titles(db, [source_id]).get(source_id, "") if source_id else ""
            )
            if include_messages:
                messages = db.execute(
                    "SELECT id, role, content, metadata, trace_hash, created_at FROM messages "
                    "WHERE conversation_id = ? ORDER BY created_at, rowid",
                    (conversation_id,),
                ).fetchall()
                # 同一批次内复用 trace blob（长会话里同一份 trace 可能被多条消息引用）。
                trace_cache: dict[str, str] = {}
                result["messages"] = [
                    self._message_dict(db, message, trace_cache) for message in messages
                ]
            return result

    @_retry_transient_write
    def set_conversation_skill_policy(
        self, conversation_id: str, policy: dict[str, Any] | None
    ) -> None:
        """Persist a conversation's frozen Skill policy (turn-1 referenced ids)."""
        with self._connect() as db:
            db.execute(
                "UPDATE conversations SET skill_policy = ? WHERE id = ?",
                (json.dumps(policy or {}, ensure_ascii=False), conversation_id),
            )

    @_retry_transient_write
    def set_conversation_chat_supports_images(self, conversation_id: str, value: bool) -> None:
        """Persist a conversation's frozen image-support capability (avoid re-probing per turn)."""
        with self._connect() as db:
            db.execute(
                "UPDATE conversations SET chat_supports_images = ? WHERE id = ?",
                (int(bool(value)), conversation_id),
            )

    @_retry_transient_write
    def branch_conversation(
        self, source_id: str, message_id: str, reset_agent: bool = False
    ) -> dict[str, Any]:
        """从源会话的分支点复制“之前”的历史到新会话，并完整复制源会话设置与冻结 Skill 策略。

        非破坏性：源会话保持原样。新会话历史为分支点之前的全部消息（含 metadata，
        使 build_model_history 能重建一致上下文）；返回新会话与分支消息（用于预填输入框）。

        ``reset_agent``（默认 False，存量行为不变）：为 True 时不继承源会话的**固化态**，
        让新会话一出生就是「首轮之前」的样子——用户在点「分支」时可选择换个 Agent 再问：

        - ``enabled_tool_ids`` 留空 ⇒ 前端 Agent 下拉解锁（`07-models-agents.js` 的
          ``locked`` 判据是「工具集非空」），首轮发送时由 ``bake_session_tool_ids`` 按
          （可能换过的）当前 Agent 重新固化；
        - ``skill_policy`` / ``first_turn`` 一并留空：这两列都是「按当时 Agent 冻结」的
          派生物，继承下来会让新会话的 system 前缀与新 Agent 对不上，顶部折叠卡还会展示
          旧 Agent 的固化上下文。
        - ``agent_id`` **仍然继承**：下拉预选同一个 Agent，用户可换可不换。
        """
        now = int(time.time() * 1000)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            src = db.execute("SELECT * FROM conversations WHERE id = ?", (source_id,)).fetchone()
            if not src:
                raise LookupError("源对话不存在")
            rows = db.execute(
                "SELECT id, role, content, metadata, trace_hash, created_at FROM messages "
                "WHERE conversation_id = ? ORDER BY created_at, rowid",
                (source_id,),
            ).fetchall()
            branch_rows = [dict(item) for item in rows]
            branch_idx = next((i for i, m in enumerate(branch_rows) if m["id"] == message_id), None)
            if branch_idx is None:
                raise LookupError("消息不存在")
            if branch_rows[branch_idx]["role"] != "user":
                raise ValueError("只能从用户消息分支")
            # 有历史时才继承源会话冻结技能集（保证新会话 system 前缀与复制的一致）；
            # 分支点是首条消息时留空，让首轮按现有规则重新冻结。
            # reset_agent（用户选了「更换 Agent」）时同样留空：技能集是按旧 Agent 冻结的。
            inherited_skill_policy = src["skill_policy"] if (branch_idx > 0 and not reset_agent) else ""
            # 首轮上下文（会话顶部折叠卡）同样只在有历史时继承：分支点之前的首轮与源会话
            # 是同一轮（消息前缀一致）；否则新会话没有 chat run，卡片永远不显示。
            # 老会话（v16 之前）列里为空，回退读源会话最早 chat run 的快照。
            inherited_first_turn = ""
            if branch_idx > 0 and not reset_agent:
                inherited_first_turn = str(src["first_turn"] or "")
                if not inherited_first_turn:
                    legacy = (self._first_chat_run_snapshot(db, source_id) or {}).get("first_turn")
                    if isinstance(legacy, dict) and legacy:
                        inherited_first_turn = json.dumps(legacy, ensure_ascii=False)
            # 标题用递增序号，避免“（分支）（分支）”叠加：去掉源标题末尾的 (N)，再取已有同名标题的最大序号 +1。
            src_title = str(src["title"] or "新对话").strip() or "新对话"
            base_title = re.sub(r"\s*\(\d+\)\s*$", "", src_title).rstrip()
            existing_titles = [str(r[0] or "") for r in db.execute("SELECT title FROM conversations").fetchall()]
            nums: list[int] = []
            for t in existing_titles:
                ts = str(t).strip()
                if ts == base_title:
                    nums.append(0)
                    continue
                m = re.search(r"\s*\((\d+)\)\s*$", ts)
                if m and ts[: m.start()].rstrip() == base_title:
                    nums.append(int(m.group(1)))
            new_title = f"{base_title} ({max(nums) + 1 if nums else 1})"
            new_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO conversations("
                "id, title, mode, permission_mode, web_search_enabled, deep_reasoning_enabled, "
                "lightweight_mode, lightweight_disabled_features, title_customized, system_prompt, "
                "stream_enabled, workspace_dir, workspace_group, reasoning_effort, enabled_tool_ids, "
                "skill_policy, chat_supports_images, provider_id, model_key, model_name, agent_id, interaction_mode, "
                "branched_from_id, branch_message_id, "
                "first_turn, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id, new_title, src["mode"], src["permission_mode"],
                    src["web_search_enabled"], src["deep_reasoning_enabled"], src["lightweight_mode"],
                    src["lightweight_disabled_features"], 1, src["system_prompt"], src["stream_enabled"],
                    src["workspace_dir"], src["workspace_group"], src["reasoning_effort"],
                    # reset_agent：不继承固化工具集（留空 ⇒ 前端下拉解锁 ⇒ 首轮按新 Agent 重固化）。
                    "" if reset_agent else src["enabled_tool_ids"],
                    inherited_skill_policy, src["chat_supports_images"], src["provider_id"], src["model_key"], src["model_name"],
                    src["agent_id"], src["interaction_mode"],
                    # 分支来源登记（v18）：侧栏徽标与分支链面板都读这两列。
                    source_id, message_id,
                    inherited_first_turn, now, now,
                ),
            )
            for m in branch_rows[:branch_idx]:
                # trace_hash 一并复制：v23 起 trace 在 message_traces 表（内容寻址），
                # 分支只带引用、共享同一份 blob，零复制成本。漏掉它 = 分支历史失去 trace
                # 权威回放，重放退回 message 兜底路径，前缀缓存从分支点起与源会话分叉。
                db.execute(
                    "INSERT INTO messages(id, conversation_id, role, content, metadata, trace_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (uuid.uuid4().hex, new_id, m["role"], m["content"], m["metadata"],
                     str(m["trace_hash"] or ""), m["created_at"]),
                )
            branch_meta = json.loads(branch_rows[branch_idx]["metadata"] or "{}")
            branch_message = {
                "id": branch_rows[branch_idx]["id"],
                "role": "user",
                "content": branch_rows[branch_idx]["content"],
                "metadata": branch_meta,
                "display_content": branch_meta.get("display_content") or branch_rows[branch_idx]["content"],
                "attachments": branch_meta.get("attachments") or [],
                # 分支会把这条提问的文件夹索引一起带回输入区（否则"分支后重发"会丢清单）。
                "folder_indexes": branch_meta.get("folder_indexes") or [],
            }
        return {
            "conversation": self.get_conversation(new_id, include_messages=True),
            "branch_message": branch_message,
        }

    def branch_chain(self, conversation_id: str) -> dict[str, Any]:
        """该会话所属的「分支链」：源会话 + 兄弟分支（自己是分支）或其全部分支（自己是源）。

        侧栏只有平铺列表 + 徽标，不画树（树与 ``updated_at`` 排序根本冲突，多级分支还会
        打乱虚拟列表的行索引），所以这条链只为「认得出、跳得过去」服务：

        - ``role == "branch"``：自己由 ``branched_from_id`` 指向某个源；链 = 源 + 所有兄弟
          （含自己），按创建时间正序。源会话已删时 ``source_deleted`` 为真、链里没有源项。
        - ``role == "source"``：链 = 自己的全部分支（按创建时间正序；自己不在链里——
          用户已经在它上面了）。
        - ``role == "none"``：既不是分支、也没有分支 → 空链。

        ``branched_from_id`` 悬空（源被删）不算错误，只是 ``source_deleted``；会话本身不存在
        才抛 ``LookupError``（路由层转 404）。
        """
        with self._connect() as db:
            own = db.execute(
                "SELECT id, title, branched_from_id, branch_message_id, created_at, updated_at "
                "FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if not own:
                raise LookupError("对话不存在")
            source_id = str(own["branched_from_id"] or "")
            if source_id:
                source = db.execute(
                    "SELECT id, title, branched_from_id, branch_message_id, created_at, updated_at "
                    "FROM conversations WHERE id = ?",
                    (source_id,),
                ).fetchone()
                siblings = db.execute(
                    "SELECT id, title, branched_from_id, branch_message_id, created_at, updated_at "
                    "FROM conversations WHERE branched_from_id = ? "
                    "ORDER BY created_at, rowid",
                    (source_id,),
                ).fetchall()
                chain = ([source] if source else []) + list(siblings)
                role = "branch"
            else:
                source = None
                branches = db.execute(
                    "SELECT id, title, branched_from_id, branch_message_id, created_at, updated_at "
                    "FROM conversations WHERE branched_from_id = ? "
                    "ORDER BY created_at, rowid",
                    (conversation_id,),
                ).fetchall()
                chain = list(branches)
                role = "source" if branches else "none"
        items = [
            {
                "id": str(row["id"]),
                "title": str(row["title"] or "（无标题）"),
                "created_at": int(row["created_at"] or 0),
                "updated_at": int(row["updated_at"] or 0),
                "branch_message_id": str(row["branch_message_id"] or ""),
                "is_source": bool(source) and str(row["id"]) == str(source["id"]),
                "is_current": str(row["id"]) == str(conversation_id),
            }
            for row in chain
        ]
        return {
            "conversation_id": conversation_id,
            "role": role,
            "source_deleted": role == "branch" and source is None,
            "source": next((item for item in items if item["is_source"]), None),
            "items": items,
        }

    @_retry_transient_write
    def update_conversation_settings(
        self,
        conversation_id: str,
        title: str | None = None,
        system_prompt: str | None = None,
        stream_enabled: bool | None = None,
        provider_id: str | None = None,
        model_key: str | None = None,
        model_name: str | None = None,
        agent_id: str | None = None,
        interaction_mode: str | None = None,
        permission_mode: str | None = None,
        web_search_enabled: bool | None = None,
        deep_reasoning_enabled: bool | None = None,
        workspace_dir: str | None = None,
        workspace_group: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any] | None:
        """Update settings owned by one conversation and return its summary.

        ``favorite`` 不走这里：收藏只是侧栏归类标记，**不能推进 ``updated_at``**
        （侧栏按更新时间排序，推进会把会话顶到工作区最前、打乱顺序），
        改用 ``set_conversation_favorite``。
        """
        values: dict[str, Any] = {}
        if title is not None:
            clean_title = " ".join(str(title).strip().split())[:120]
            if clean_title:
                values["title"] = clean_title
            else:
                with self._connect() as db:
                    first_user_message = db.execute(
                        "SELECT content FROM messages WHERE conversation_id = ? AND role = 'user' "
                        "ORDER BY created_at, rowid LIMIT 1",
                        (conversation_id,),
                    ).fetchone()
                values["title"] = (
                    " ".join(str(first_user_message[0]).strip().split())[:36]
                    if first_user_message and str(first_user_message[0]).strip()
                    else "新对话"
                )
            values["title_customized"] = 1 if clean_title else 0
        if system_prompt is not None:
            values["system_prompt"] = str(system_prompt).strip()[:20000]
        if stream_enabled is not None:
            values["stream_enabled"] = 1 if bool(stream_enabled) else 0
        if provider_id is not None:
            values["provider_id"] = str(provider_id or "")
        if model_key is not None:
            # 切换 API 只更新该会话，不影响其他会话与正在运行的 Run。
            values["model_key"] = str(model_key or "")
        if model_name is not None:
            values["model_name"] = str(model_name or "").strip()[:256]
        if agent_id is not None:
            values["agent_id"] = str(agent_id or "")
        if interaction_mode is not None:
            if not isinstance(interaction_mode, str):
                raise ValueError("interaction_mode 必须是文本")
            normalized = interaction_mode.strip().lower()
            if normalized not in {"plan", "craft", "ask"}:
                raise ValueError("interaction_mode 必须是 plan 或普通模式")
            values["interaction_mode"] = "craft"
        if permission_mode is not None:
            if permission_mode not in ("confirm", "auto", "full"):
                raise ValueError("permission_mode 必须是 confirm / auto / full")
            values["permission_mode"] = permission_mode
        if web_search_enabled is not None:
            values["web_search_enabled"] = 1 if bool(web_search_enabled) else 0
        if deep_reasoning_enabled is not None:
            values["deep_reasoning_enabled"] = 1 if bool(deep_reasoning_enabled) else 0
            if reasoning_effort is None:
                values["reasoning_effort"] = "medium" if bool(deep_reasoning_enabled) else "off"
        if reasoning_effort is not None:
            effort = str(reasoning_effort or "off").strip().lower()
            if effort not in {"off", "low", "medium", "high", "auto"}:
                raise ValueError("reasoning_effort 必须是 off / low / medium / high / auto")
            values["reasoning_effort"] = effort
            values["deep_reasoning_enabled"] = 0 if effort == "off" else 1
        if workspace_dir is not None:
            values["workspace_dir"] = str(workspace_dir or "").strip()
        if workspace_group is not None:
            values["workspace_group"] = str(workspace_group or "").strip()
        if not values:
            return self.get_conversation(conversation_id, include_messages=False)
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._connect() as db:
            if model_key is not None or model_name is not None:
                current = db.execute(
                    "SELECT model_key, model_name FROM conversations WHERE id = ?", (conversation_id,)
                ).fetchone()
                if current and (
                    (model_key is not None and str(current["model_key"] or "") != values.get("model_key"))
                    or (model_name is not None and str(current["model_name"] or "") != values.get("model_name"))
                ):
                    # 图像能力由实际模型决定；API 或模型名变化后必须重新探测。
                    values["chat_supports_images"] = -1
                    assignments = ", ".join(f"{key} = ?" for key in values)
            parameters = [*values.values(), int(time.time() * 1000), conversation_id]
            cursor = db.execute(
                f"UPDATE conversations SET {assignments}, updated_at = ? WHERE id = ?",
                parameters,
            )
            if cursor.rowcount == 0:
                return None
        return self.get_conversation(conversation_id, include_messages=False)

    @_retry_transient_write
    def clear_conversation_model_overrides(self, model_key: str, model_name: str) -> int:
        """清空「仍保存着该 API 旧默认模型名」的会话覆盖，返回受影响会话数。

        会话 ``model_name`` 是会话级覆盖，为空表示跟随 API 供应商的默认模型。用户在输入区
        没主动选过模型时，历史实现会把供应商当时的默认模型名一并落库成覆盖——供应商之后
        改了默认模型，这些旧会话仍会继续发送旧模型名。这里只清空「覆盖值恰好等于旧默认
        模型名」的会话（它们本来就在隐式跟随），显式固定成其它模型的会话保持不变。

        不推进 ``updated_at``：这是静默修正，按更新时间排序的侧栏不应因此被重排。
        ``chat_supports_images`` 归零为未知（-1），下次运行按新模型重新探测。
        """
        key = str(model_key or "").strip()
        name = str(model_name or "").strip()
        if not key or not name:
            return 0
        model_id = key.split(":", 1)[1] if ":" in key else key
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE conversations SET model_name = '', chat_supports_images = -1 "
                "WHERE model_name = ? AND (model_key = ? OR (model_key = '' AND provider_id = ?))",
                (name, key, model_id),
            )
            return int(cursor.rowcount or 0)

    @_retry_transient_write
    def set_enabled_tool_ids(self, conversation_id: str, tool_ids: list[str] | tuple[str, ...] | set[str]) -> None:
        """固化某会话的启用工具集（会话启动时写入，之后不可改）。"""
        with self._connect() as db:
            db.execute(
                "UPDATE conversations SET enabled_tool_ids = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps(list(dict.fromkeys(str(item) for item in tool_ids)), ensure_ascii=False),
                    int(time.time() * 1000),
                    conversation_id,
                ),
            )

    @_retry_transient_write
    def set_conversation_favorite(self, conversation_id: str, favorite: bool) -> dict[str, Any] | None:
        """只改收藏标记，**不动 ``updated_at``**。

        侧栏工作区分组按 ``updated_at`` 倒序（且默认只显示最新 5 条）：收藏若推进时间，
        会话会被顶到工作区最前、打乱用户熟悉的顺序，启动时"最新 5 条"也会被收藏项挤占。
        """
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE conversations SET favorite = ? WHERE id = ?",
                (1 if bool(favorite) else 0, conversation_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_conversation(conversation_id, include_messages=False)

    @_retry_transient_write
    def set_conversation_archived(self, conversation_id: str, archived: bool) -> dict[str, Any] | None:
        """只改归档标记，**不动 ``updated_at``**。

        与 ``set_conversation_favorite`` 同一约束：归档是侧栏可见性标记，若推进时间，
        「隐藏已归档」切到「全部对话」时整列顺序会被打乱，且手动排序位也会被
        「最近更新」口径覆盖。归档语义照 DeepSeek Harness：仅隐藏，不删数据。
        """
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE conversations SET archived = ? WHERE id = ?",
                (1 if bool(archived) else 0, conversation_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_conversation(conversation_id, include_messages=False)

    @_retry_transient_write
    def set_conversation_sort_order(self, ordered_ids: list[str]) -> int:
        """按给定顺序整体重排手动排序位（1..N），返回实际写入的行数。

        拖拽落序一次提交**全量可见顺序**：一个事务里逐条写 1..N，中途失败整体回滚，
        不会出现「半截排序」。列表里不存在的 id（期间被删的会话）先被剔除、
        **不消耗序号**，剩余 id 按提交顺序拿到连续的 1..N；之后再新建的会话
        ``sort_order`` 为 0，手动模式下排在最前（新会话置顶）。
        不推进 ``updated_at``：重排本身不是会话活动，不能反过来改变时间排序。
        """
        unique: list[str] = []
        seen: set[str] = set()
        for raw in ordered_ids or []:
            conversation_id = str(raw).strip()
            if conversation_id and conversation_id not in seen:
                seen.add(conversation_id)
                unique.append(conversation_id)
        if not unique:
            return 0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in unique)
            existing = {
                str(row["id"])
                for row in db.execute(
                    f"SELECT id FROM conversations WHERE id IN ({placeholders})", unique
                ).fetchall()
            }
            written = 0
            index = 0
            for conversation_id in unique:
                if conversation_id not in existing:
                    continue
                index += 1
                cursor = db.execute(
                    "UPDATE conversations SET sort_order = ? WHERE id = ?",
                    (index, conversation_id),
                )
                written += int(cursor.rowcount or 0)
        return written

    @_retry_transient_write
    def clear_workspace_group(self, workspace_group: str) -> int:
        """删除工作区时把其下对话归档到「未分组」（workspace_group 置空），返回受影响行数。"""
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE conversations SET workspace_group = '' WHERE workspace_group = ?",
                (str(workspace_group or "").strip(),),
            )
            return cursor.rowcount

    def message_count(self, conversation_id: str) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    @_retry_transient_write
    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = int(time.time() * 1000)
        message_id = uuid.uuid4().hex
        payload = dict(metadata or {})
        # trace 走内容寻址独立表：同一份 blob 只存一行，消息行只留 trace_hash。
        # 调用方接口不变——返回值里照旧带完整 `metadata["trace"]`。
        trace_hash = _split_trace_payload(payload)
        metadata_json = json.dumps(payload, ensure_ascii=False)
        with self._connect() as db:
            if trace_hash:
                db.execute(
                    "INSERT OR IGNORE INTO message_traces(trace_hash, data) VALUES (?, ?)",
                    (trace_hash[0], trace_hash[1]),
                )
            db.execute(
                "INSERT INTO messages(id, conversation_id, role, content, metadata, trace_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (message_id, conversation_id, role, content, metadata_json,
                 trace_hash[0] if trace_hash else "", now),
            )
            db.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
            message_count = db.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            customized = db.execute(
                "SELECT title_customized FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()[0]
            if role == "user" and message_count <= 2 and not customized:
                title = " ".join(content.strip().split())[:36] or _attachment_title(
                    (metadata or {}).get("attachments")
                )
                db.execute(
                    "UPDATE conversations SET title = ? WHERE id = ?",
                    (title, conversation_id),
                )
        return {
            "id": message_id,
            "role": role,
            "content": content,
            "metadata": metadata or {},
            "created_at": now,
        }

    @_retry_transient_write
    def set_session_start(
        self,
        conversation_id: str,
        message_id: str,
        *,
        source: str = "manual",
        handoff_path: str = "",
        note: str = "",
    ) -> dict[str, Any] | None:
        """在指定消息上标记「新会话从这条消息之后开始」（metadata 形态，不新增行）。

        为什么不用独立标记行：`messages` 的排序键是 `(created_at, rowid)`，想在中间插一行
        必须构造/回移时间戳（同毫秒相邻时会插错位）。写在**被点击的那条消息**的 metadata 上
        位置天然精确，重放侧遇到该标记就清空此前历史（该消息本身也不进新上下文）。
        允许同一会话存在多条标记（最新的那条决定当前上下文起点，见 `build_model_history`）。
        """
        marker = {
            MetadataKeys.SESSION_START: {
                "at": int(time.time() * 1000),
                "source": str(source or "manual")[:20],
                "handoff_path": str(handoff_path or ""),
                "note": str(note or "")[:200],
            }
        }
        now = int(time.time() * 1000)
        # 读一次只为「消息是否存在」与返回值；**写不再整块覆盖**——同行还有图片降级旗标等
        # 写入方，「读整块→整块写回」会让先写者的键消失（丢 session_start ⇒ 上下文清空）。
        with self._connect() as db:
            row = db.execute(
                "SELECT metadata FROM messages WHERE id = ? AND conversation_id = ?",
                (message_id, conversation_id),
            ).fetchone()
        if not row:
            return None
        if not self.merge_message_metadata(conversation_id, message_id, marker):
            return None
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        metadata.update(marker)
        return {"id": message_id, "metadata": metadata, "created_at": now}

    @_retry_transient_write
    def clear_session_start(self, message_id: str) -> bool:
        """撤销某条消息上的「新会话」标记（消息本身与其余 metadata 一律保留）。"""
        now = int(time.time() * 1000)
        with self._connect() as db:
            row = db.execute(
                "SELECT conversation_id, metadata FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if not row:
                return False
            metadata = json.loads(row["metadata"] or "{}")
            if not isinstance(metadata, dict) or MetadataKeys.SESSION_START not in metadata:
                return False
            # 只摘掉自己这个键（`json_remove`），不整块写回：同行的其他写入方（图片降级
            # 旗标等）并发落下的键必须原样保留。
            db.execute(
                "UPDATE messages SET metadata = "
                f"json_remove(COALESCE(NULLIF(metadata, ''), '{{}}'), '$.{MetadataKeys.SESSION_START}') "
                "WHERE id = ?",
                (message_id,),
            )
            db.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, row["conversation_id"]),
            )
        return True

    @_retry_transient_write
    def delete_session_start(self, message_id: str) -> bool:
        """删除**遗留形态**的边界标记行（role=session；只允许删该角色，避免误删对话消息）。"""
        now = int(time.time() * 1000)
        with self._connect() as db:
            row = db.execute(
                "SELECT conversation_id FROM messages WHERE id = ? AND role = 'session'",
                (message_id,),
            ).fetchone()
            if not row:
                return False
            db.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            db.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, row["conversation_id"]),
            )
        return True

    @_retry_transient_write
    def update_message_metadata(
        self, conversation_id: str, message_id: str, metadata: dict[str, Any]
    ) -> bool:
        """就地更新一条消息的 metadata（异步 Job 产物写回用），并推进会话 updated_at。

        只改 metadata 与 conversations.updated_at：content/role/created_at 一律不动，
        因此消息顺序契约 `(created_at, rowid)` 不受影响；updated_at 变化会让前端既有
        轮询（syncCurrentConversation 的 snapshot）检测到并重渲染该会话。
        返回是否命中该消息（消息已被删除时 False，调用方记录后放弃）。

        **整块替换**是本方法唯一的合法形态（各写入方只传自己拥有的键，见契约），
        但 trace 例外：调用方给的 metadata 常是 `get_conversation` 回读的整块（已含
        hydrate 回来的 trace），照原样写回就会把大 blob 重新塞进 metadata 列。这里统一
        摘出来走内容寻址表（`add_message` 同款口径），保持"trace 只在 message_traces"。
        """
        now = int(time.time() * 1000)
        payload = dict(metadata or {})
        trace = _split_trace_payload(payload)
        metadata_json = json.dumps(payload, ensure_ascii=False)
        with self._connect() as db:
            assignments = "metadata = ?"
            values: list[Any] = [metadata_json]
            if trace:
                db.execute(
                    "INSERT OR IGNORE INTO message_traces(trace_hash, data) VALUES (?, ?)",
                    (trace[0], trace[1]),
                )
                assignments += ", trace_hash = ?"
                values.append(trace[0])
            # 不带 trace 时**不碰** trace_hash：调用方没给 trace 是常规形态，
            # 不能因为"这次 metadata 里没有 trace"就把该行已有的 trace 抹掉。
            values.extend([message_id, conversation_id])
            cursor = db.execute(
                f"UPDATE messages SET {assignments} WHERE id = ? AND conversation_id = ?",
                tuple(values),
            )
            if cursor.rowcount:
                db.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )
        return cursor.rowcount > 0

    @_retry_transient_write
    def merge_message_metadata(
        self,
        conversation_id: str,
        message_id: str,
        patch: dict[str, Any],
        *,
        remove: tuple[str, ...] | list[str] = (),
    ) -> bool:
        """**按键原子合并**一条消息的 metadata（单条 ``json_set``/``json_remove`` UPDATE），并推进会话 updated_at。

        为什么不能用「先读整块 metadata、改完再整块写回」：同一条消息上有多个写入方
        （图片降级旗标 `local_images_capped`、会话边界 `session_start`、异步 Job 产物写回的
        `tool_runs`/`attachments`、插话状态…），各自读到的都是**旧**整块，后写者会把先写者
        刚落的键整块抹掉。丢 `session_start` 的后果不是"少个标记"——`build_model_history`
        会把整段上下文清空（用户视角＝模型突然失忆）。这里把写入降成"只改自己那几个键"，
        其他键由数据库按**当前值**原样保留，两个写入方并发也不再互相覆盖。

        ``remove``：需要**删掉**的键（例如产物不再截断时要清 `attachments_truncated`）。
        删除不存在的键是静默无操作，调用方不必先确认它在不在。

        契约：键必须是一层、且只含标识符字符（键名直接进 JSON 路径，不接受调用方任意
        字符串）；值以 JSON 文本经 ``json(?)`` 传入。返回是否命中该消息（消息不存在时
        False——SQLite 对 UPDATE 的 rowcount 只统计命中的行，即使新值等于旧值也计 1，
        所以 0 一定意味着"没这行"）。
        """
        keys = [str(key) for key in (patch or {})]
        remove_keys = [str(key) for key in (remove or ())]
        if not keys and not remove_keys:
            return False
        for key in (*keys, *remove_keys):
            if not _METADATA_KEY_RE.match(key):
                raise ValueError(f"metadata 键名非法（只允许一层标识符）：{key!r}")
        # 键名来自代码常量（MetadataKeys），此处已做形状校验；SQLite 的 JSON 路径
        # 又不能写占位符，所以只能拼接——那句校验就是这条拼接的安全边界。
        expression = "COALESCE(NULLIF(metadata, ''), '{}')"
        if keys:
            assignments = ", ".join(f"'$.{key}', json(?)" for key in keys)
            expression = f"json_set({expression}, {assignments})"
        if remove_keys:
            paths = ", ".join(f"'$.{key}'" for key in remove_keys)
            expression = f"json_remove({expression}, {paths})"
        values = [json.dumps(patch[key], ensure_ascii=False) for key in keys]
        now = int(time.time() * 1000)
        with self._connect() as db:
            cursor = db.execute(
                f"UPDATE messages SET metadata = {expression} WHERE id = ? AND conversation_id = ?",
                (*values, message_id, conversation_id),
            )
            if cursor.rowcount:
                db.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )
        return cursor.rowcount > 0

    @_retry_transient_write
    def delete_conversation(self, conversation_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        return cursor.rowcount > 0

    @_retry_transient_write
    def clear_conversation_messages(self, conversation_id: str) -> int:
        """Clear persisted chat/tool history while retaining the conversation settings."""
        with self._connect() as db:
            cursor = db.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            db.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (int(time.time() * 1000), conversation_id),
            )
        return cursor.rowcount

    @_retry_transient_write
    def truncate_from_message(self, conversation_id: str, message_id: str) -> int:
        """删除某条消息及其之后（同一会话内按 (created_at, rowid) 排序不早于它的）所有消息。

        返回删除的消息条数。用于"编辑历史消息后从该处重新开始"。

        使用 SQLite 隐式 rowid 做复合排序定位编辑点：同毫秒时间戳的多条消息
        也保证只删编辑点及之后，前缀消息（含 metadata.trace / reasoning）字节级不变，
        从而使编辑重发时能命中 OpenAI/DeepSeek 等提供商的自动前缀缓存。
        """
        with self._connect() as db:
            target = db.execute(
                "SELECT created_at, rowid FROM messages WHERE id = ? AND conversation_id = ?",
                (message_id, conversation_id),
            ).fetchone()
            if not target:
                return 0
            created_at = target[0]
            rowid = target[1]
            # 精确删除编辑点及其之后的消息：按 (created_at, rowid) 复合条件，
            # 前缀消息一律保留。
            cursor = db.execute(
                "DELETE FROM messages WHERE conversation_id = ? "
                "AND (created_at > ? OR (created_at = ? AND rowid >= ?))",
                (conversation_id, created_at, created_at, rowid),
            )
            db.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (int(time.time() * 1000), conversation_id),
            )
        return cursor.rowcount

    # ---- 删除单条/整轮消息 + 撤销（快照插回） ----
    # 「删除」= 从模型上下文剔除 + 界面移除，**不动任何附件文件**：仍被其它消息引用的附件
    # 不受影响，不再被引用的那些交给既有缓存自动清理（其 15 分钟保护窗口 + 引用保护逻辑不变）。
    MESSAGE_DELETE_MODES: tuple[str, ...] = ("single", "turn")

    @_retry_transient_write
    def delete_message(
        self, conversation_id: str, message_id: str, mode: str = "single"
    ) -> dict[str, Any]:
        """删除一条消息（``single``）或一整轮（``turn``），返回被删消息的完整快照供撤销插回。

        - ``single``：只删该 id 那一行。``role='session'`` 的遗留标记行**拒绝**
          （那是「新会话分割线」的旧形态，语义属于 ``delete_session_start``；误删会把上下文
          起点悄悄往前挪）。
        - ``turn``：仅对 user 消息可用 —— 删该 user 消息 + 其后**紧随的连续 assistant 消息**
          （遇下一条 user 即停；途中的 session 标记行一并带走，它属于这一轮的痕迹）。
        - 快照按原始顺序返回，含 ``metadata_json``（原始文本）：撤销时原样插回，content 与
          metadata 逐字节不变，因此「删了又撤销」不会让模型请求的前缀缓存失效。
        - 事务内删除并推进 ``conversations.updated_at``（与 ``truncate_from_message`` 同口径，
          前端既有轮询自然感知）。
        """
        kind = str(mode or "single")
        if kind not in self.MESSAGE_DELETE_MODES:
            raise ValueError("mode 必须是 single 或 turn")
        with self._connect() as db:
            target = db.execute(
                "SELECT id, role, content, metadata, trace_hash, created_at, rowid AS rid FROM messages "
                "WHERE id = ? AND conversation_id = ?",
                (message_id, conversation_id),
            ).fetchone()
            if not target:
                raise LookupError("消息不存在")
            if str(target["role"]) == "session":
                raise ValueError("分割线标记行不能用这个接口删除，请用撤销分割线")
            victims = [target]
            if kind == "turn":
                if str(target["role"]) != "user":
                    raise ValueError("只有用户消息支持整轮删除")
                following = db.execute(
                    "SELECT id, role, content, metadata, trace_hash, created_at, rowid AS rid FROM messages "
                    "WHERE conversation_id = ? "
                    "AND (created_at > ? OR (created_at = ? AND rowid > ?)) "
                    "ORDER BY created_at, rowid",
                    (conversation_id, target["created_at"], target["created_at"], target["rid"]),
                ).fetchall()
                for row in following:
                    if str(row["role"]) not in ("assistant", "session"):
                        break  # 遇到下一条 user（或任何非本轮角色）即停
                    victims.append(row)
            db.execute(
                "DELETE FROM messages WHERE id IN (%s)"
                % ",".join("?" for _ in victims),
                [str(row["id"]) for row in victims],
            )
            now = int(time.time() * 1000)
            db.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "mode": kind,
            "removed": [self._message_snapshot(row) for row in victims],
            "updated_at": now,
        }

    @_retry_transient_write
    def restore_messages(self, conversation_id: str, snapshots: list[dict[str, Any]]) -> dict[str, Any]:
        """按快照把被删消息原样插回（删除的撤销）。返回实际插回条数与跳过的 id。

        逐字节还原 ``id`` / ``content`` / ``metadata`` / ``created_at``；``metadata_json``
        存在时按原文本写回，否则用 ``json.dumps`` 重新序列化。**已存在的 id 跳过**——
        重复点撤销不会报错，也不会覆盖现有内容。

        **顺序锚定（不是可选项）**：会话排序契约是 ``(created_at, rowid)``，而隐式 rowid 在插回时
        必然重新分配（新的总是最大）。快照带原 rowid 时按 ``(created_at, 原 rowid)`` 算出目标插入位，
        再把「排在该批之后、却与它 created_at 并列或更早」的现存行**从后继起整体往后挪**（挪后缀，
        后缀内部相对次序不变，量取「批次里最晚的 created_at − 后继 created_at + 1」）⇒ 撤销后顺序
        与删除前逐条一致。整批只定位**一次**：``delete_message`` 的快照本来就是连续块，逐条重新定位
        会拿"已插回行的新 rowid"去和"批次里下一条的原 rowid"比大小（两个域混用），批次内部反而翻序。
        不这么做会发生什么：同毫秒并列时新 rowid 会把被撤销的消息挤到最后——探针实测
        「u1 a1 u2 a2 同毫秒、删 a1 再撤销」变成 u1 u2 a2 **a1**；a1 上带着「新会话分割线」标记时
        ``build_model_history`` 会把整段上下文清空（用户视角＝模型突然失忆），前缀缓存也从那条起
        整体重写。缺 ``rowid`` 的老快照退化为修复前行为（排到最后）并记 info 日志。

        两个已处理的边界：① SQLite 无 AUTOINCREMENT，删掉当时 rowid 最大的行后下一条会**复用**
        该 rowid，所以找后继用 ``>=``（``>`` 会在复用时误判"没有后继"⇒ 又不腾位）；
        ② 快照顺序按调用方给的来，但公开入口不能假定有序——原 rowid 齐全时先按排序契约还原。
        """
        rows_in = [item for item in (snapshots or []) if isinstance(item, dict)]
        if not rows_in:
            raise ValueError("messages 不能为空")
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone():
                raise LookupError("对话不存在")
            existing = {
                str(row[0]) for row in db.execute(
                    "SELECT id FROM messages WHERE conversation_id = ?", (conversation_id,)
                ).fetchall()
            }

            def load_positions() -> list[tuple[int, int]]:
                """现存行的排序键 ``(created_at, rowid)``——即当前排序契约下的真实顺序。"""
                return [
                    (int(row["created_at"] or 0), int(row["rid"]))
                    for row in db.execute(
                        "SELECT created_at, rowid AS rid FROM messages "
                        "WHERE conversation_id = ? ORDER BY created_at, rowid",
                        (conversation_id,),
                    ).fetchall()
                ]

            pending: list[dict[str, Any]] = []
            skipped: list[str] = []
            for item in rows_in:
                snapshot = self._normalize_snapshot(item)
                if not snapshot["id"] or snapshot["id"] in existing:
                    skipped.append(snapshot["id"])
                    continue
                pending.append(snapshot)
            if len(pending) > 1 and all(item.get("rowid") is not None for item in pending):
                # 调用方（前端）按删除顺序回传，但**不能假定**：`/api/messages/restore` 是公开
                # 入口，手写乱序数组会让"整批一次定位"取错头、批次内部也按传入顺序拿递增新
                # rowid 而翻序。原 rowid 齐全时先按排序契约还原删除前的顺序。
                pending.sort(key=lambda item: (int(item["created_at"]), int(item["rowid"])))
            restored: list[str] = []
            if pending:
                # **整批一次定位**：``delete_message`` 的快照本来就是连续块（single 一行、
                # turn 是「user + 紧随的连续 assistant」），所以整批插在同一个位置。
                # 不能逐条重新定位——已经插回的批次行拿到的是新 rowid（必然最大），拿它的
                # 新 rowid 去和批次里下一条的**原 rowid** 比大小是两个域混用，批次内部会翻序。
                head = pending[0]
                head_rid = head.get("rowid")
                positions = load_positions()
                if head_rid is None:
                    if positions:
                        logger.info(
                            "撤销快照缺少原 rowid，只能按插入顺序排到末尾：conversation=%s count=%d",
                            conversation_id, len(pending),
                        )
                else:
                    key = (int(head["created_at"]), int(head_rid))
                    # 用 `>=` 而不是 `>`：SQLite 没有 AUTOINCREMENT，删掉"当时 rowid 最大"的
                    # 那一行之后，下一条新消息会**复用**这个 rowid（撤销前又发了一条同毫秒消息
                    # 就能命中）。这时现存行里存在与 key **完全相同**的 (created_at, rowid)，
                    # `>` 会直接跳过它 ⇒ successor=None ⇒ 不腾位 ⇒ 被撤销的消息又排到最后，
                    # 正是本次修复要消灭的那类故障。`>=` 在无复用时与 `>` 完全等价。
                    successor = next((pos for pos in positions if pos >= key), None)
                    if successor is not None:
                        # 后继与批次**并列或更早**时，新 rowid 会把整批挤到后继之后 ⇒
                        # 从后继起把后缀整体推后，直到它超过批次里最晚的那条（后缀内部次序不变）。
                        latest = max(int(item["created_at"]) for item in pending)
                        if successor[0] <= latest:
                            bump = latest - successor[0] + 1
                            db.execute(
                                "UPDATE messages SET created_at = created_at + ? "
                                "WHERE conversation_id = ? "
                                "AND (created_at > ? OR (created_at = ? AND rowid >= ?))",
                                (bump, conversation_id, successor[0], successor[0], successor[1]),
                            )
                            logger.info(
                                "撤销消息顺序锚定：自后继起后缀时间戳 +%dms（同毫秒并列腾位）"
                                "conversation=%s count=%d", bump, conversation_id, len(pending),
                            )
                for snapshot in pending:
                    db.execute(
                        "INSERT INTO messages(id, conversation_id, role, content, metadata, trace_hash, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            snapshot["id"],
                            conversation_id,
                            snapshot["role"],
                            snapshot["content"],
                            snapshot["metadata_json"],
                            snapshot["trace_hash"],
                            int(snapshot["created_at"]),
                        ),
                    )
                    existing.add(snapshot["id"])
                    restored.append(snapshot["id"])
            now = int(time.time() * 1000)
            db.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "restored": restored,
            "skipped": skipped,
            "count": len(restored),
            "updated_at": now,
        }

    @staticmethod
    def _message_snapshot(row: sqlite3.Row) -> dict[str, Any]:
        """被删消息的完整快照（撤销插回的唯一依据）。

        额外带上 **原 rowid**（查询里别名 ``rid``）：隐式 rowid 在插回时会重新分配，而会话的
        排序契约是 ``(created_at, rowid)``——同毫秒的多条消息一旦丢掉原 rowid，撤销就会把这条
        消息排到同毫秒邻居**之后**（实测：分割线标记落在这一组里时，`build_model_history`
        会把整段上下文清空，界面上就是"模型突然失忆"）。带原 rowid 才能算出正确的插入位。
        """
        raw = row["metadata"]
        text = raw if isinstance(raw, str) else json.dumps(raw or {}, ensure_ascii=False)
        try:
            original_rowid: int | None = int(row["rid"])
        except (IndexError, KeyError, TypeError, ValueError):
            original_rowid = None
        # v23 起 trace 不在 metadata 文本里，而在 message_traces 表（消息行只留 trace_hash
        # 引用）。快照必须带上这个引用，否则「删除→撤销」恢复出的行会永久丢掉 trace——
        # 重放退回 message 兜底路径，字节与原来不同，前缀缓存从这条起断链。
        try:
            trace_hash = str(row["trace_hash"] or "")
        except (IndexError, KeyError):
            trace_hash = ""
        return {
            "id": str(row["id"]),
            "role": str(row["role"] or ""),
            "content": str(row["content"] or ""),
            "metadata_json": text,
            "trace_hash": trace_hash,
            "created_at": int(row["created_at"] or 0),
            "rowid": original_rowid,
        }

    @staticmethod
    def _normalize_snapshot(item: dict[str, Any]) -> dict[str, Any]:
        """把前端回传的快照归一成插回所需的字段（缺 ``metadata_json`` 时重新序列化）。

        ``rowid``（原 SQLite rowid）**可选**：老快照/手改快照没有它就退化为"按插入顺序排到最后"
        （即修复前的行为），有它才能把消息锚回原位。
        """
        message_id = str(item.get("id") or "")
        role = str(item.get("role") or "")
        if not message_id or role not in ("user", "assistant", "session"):
            raise ValueError("快照缺少有效的 id / role")
        raw = item.get("metadata_json")
        if isinstance(raw, str) and raw.strip():
            text = raw
        else:
            metadata = item.get("metadata")
            text = json.dumps(metadata if isinstance(metadata, dict) else {}, ensure_ascii=False)
        try:
            created_at = int(item.get("created_at") or 0)
        except (TypeError, ValueError):
            raise ValueError("快照的 created_at 必须是整数")
        raw_rid = item.get("rowid", item.get("rid"))
        try:
            original_rowid = int(raw_rid) if raw_rid not in (None, "") else None
        except (TypeError, ValueError):
            original_rowid = None
        return {
            "id": message_id,
            "role": role,
            "content": str(item.get("content") or ""),
            "metadata_json": text,
            # v23 起 trace 在 message_traces 表（内容寻址，blob 不随删除消失），
            # 快照只需带回引用；老快照没有这个键，按"无 trace"处理。
            "trace_hash": str(item.get("trace_hash") or ""),
            "created_at": created_at,
            "rowid": original_rowid,
        }

    def create_background_task(
        self,
        conversation_id: str,
        message: str,
        agent: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        return self.create_run(
            conversation_id,
            message,
            agent,
            snapshot,
            interaction_mode=str(snapshot.get("interaction_mode") or "craft"),
            plan_id=str(snapshot.get("plan_id") or ""),
        )

    def active_run(self, conversation_id: str) -> dict[str, Any] | None:
        """该对话当前的**顶层**运行中 Run（子 Job 不算）。

        与 create_run / create_chat_run 的 ACTIVE_RUN 互斥语义保持一致：只有顶层 Run
        占用对话，后台子 Job（parent_job_id 非空）不阻塞用户继续对话。
        """
        with self._connect() as db:
            row = db.execute(
                "SELECT id, conversation_id, kind, interaction_mode, input_message_id, plan_id, "
                "agent_id, agent_name, status, message, detail, error, cancel_requested, "
                "created_at, started_at, updated_at, finished_at "
                "FROM background_tasks WHERE conversation_id = ? "
                f"AND status IN {_status_in_clause(ACTIVE_TASK_STATUSES)} "
                "AND (parent_job_id IS NULL OR parent_job_id = '') "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return self._task_dict(row) if row else None

    # ---- 插话（interjection）：运行中排队的新指令 ----
    # 落库形态 = 一条 role=user 的普通消息 + metadata 标记（键见 MetadataKeys.INTERJECTION*）。
    # 之所以复用 messages 表而不是另开队列表：插话被消费后**就是**这轮对话里一条真实的
    # 用户消息（模型看到的消息序列与库内顺序天然一致），另存一份反而要处理"什么时候并回"。
    # 代价是查询侧必须按标记过滤：`build_model_history` 只放行 consumed 的插话，
    # `list_run_interjections` 只取 guided 未消费的，前端只把未消费的渲染进队列面板。

    @_retry_transient_write
    def add_run_interjection(
        self,
        conversation_id: str,
        run_id: str,
        content: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """为活动中的 Run 建一条待发指令（pending）。

        刻意不让 agent 看到：用户明确点「引导」之后（``guide_run_interjection``）
        才进入可拉取队列。
        """
        now = int(time.time() * 1000)
        message_id = uuid.uuid4().hex
        metadata = {
            MetadataKeys.ATTACHMENTS: attachments or [],
            MetadataKeys.RUN_ID: run_id,
            MetadataKeys.INTERJECTION: True,
            MetadataKeys.INTERJECTION_GUIDED: False,
        }
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT id FROM background_tasks WHERE id = ? AND conversation_id = ? "
                f"AND status IN {_status_in_clause(ACTIVE_TASK_STATUSES)}",
                (run_id, conversation_id),
            ).fetchone()
            if not active:
                raise LookupError("运行不存在或已结束")
            db.execute(
                "INSERT INTO messages(id, conversation_id, role, content, metadata, created_at) "
                "VALUES (?, ?, 'user', ?, ?, ?)",
                (message_id, conversation_id, content, json.dumps(metadata, ensure_ascii=False), now),
            )
            db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
        return {
            "id": message_id,
            "role": "user",
            "content": content,
            "metadata": metadata,
            "created_at": now,
        }

    def list_run_interjections(self, run_id: str) -> list[dict[str, Any]]:
        """该 Run 下**已被引导、尚未消费**的插话（agent 消费点的唯一输入）。

        pending（未引导）与 stopped 不在此列——前者等用户显式操作，后者永不发送。
        """
        run = self.get_background_task(run_id)
        if not run:
            return []
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, role, content, metadata, trace_hash, created_at FROM messages "
                "WHERE conversation_id = ? AND role = 'user' ORDER BY created_at, rowid",
                (str(run.get("conversation_id") or ""),),
            ).fetchall()
            messages = [self._message_dict(db, row) for row in rows]
        return [
            message for message in messages
            if bool(message.get("metadata", {}).get(MetadataKeys.INTERJECTION))
            and str(message.get("metadata", {}).get(MetadataKeys.RUN_ID) or "") == run_id
            and bool(message.get("metadata", {}).get(MetadataKeys.INTERJECTION_GUIDED))
            and not bool(message.get("metadata", {}).get(MetadataKeys.INTERJECTION_CONSUMED))
            and not bool(message.get("metadata", {}).get(MetadataKeys.INTERJECTION_STOPPED))
        ]

    @_retry_transient_write
    def guide_run_interjection(
        self, conversation_id: str, run_id: str, message_id: str
    ) -> dict[str, Any]:
        """把一条 pending 插话放行给活动中的 Run（agent 下一步取走）。"""
        now = int(time.time() * 1000)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT id FROM background_tasks WHERE id = ? AND conversation_id = ? "
                f"AND status IN {_status_in_clause(ACTIVE_TASK_STATUSES)}",
                (run_id, conversation_id),
            ).fetchone()
            if not active:
                raise LookupError("运行不存在或已结束")
            row = db.execute(
                "SELECT id, role, content, metadata, trace_hash, created_at FROM messages "
                "WHERE id = ? AND conversation_id = ? AND role = 'user'",
                (message_id, conversation_id),
            ).fetchone()
            if not row:
                raise LookupError("待引导消息不存在")
            message = self._message_dict(db, row)
            metadata = dict(message.get("metadata") or {})
            if (not metadata.get(MetadataKeys.INTERJECTION)
                    or str(metadata.get(MetadataKeys.RUN_ID) or "") != run_id
                    or metadata.get(MetadataKeys.INTERJECTION_GUIDED)
                    or metadata.get(MetadataKeys.INTERJECTION_CONSUMED)):
                raise LookupError("待引导消息不存在或已被处理")
            metadata[MetadataKeys.INTERJECTION_GUIDED] = True
            # 同事务内**只改自己这一个键**（json_set）：整块写回会抹掉同行其他写入方
            # （图片降级旗标等）并发落下的键。不能换成 merge_message_metadata——那会另开
            # 连接，"读→判断→写"就不再原子，而本函数的幂等性正建立在这段原子性上。
            db.execute(
                "UPDATE messages SET metadata = "
                f"json_set(COALESCE(NULLIF(metadata, ''), '{{}}'), "
                f"'$.{MetadataKeys.INTERJECTION_GUIDED}', json('true')) "
                "WHERE id = ?",
                (message_id,),
            )
            db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
        message["metadata"] = metadata
        return message

    @_retry_transient_write
    def mark_run_interjections_consumed(self, run_id: str, message_ids: list[str]) -> None:
        """标记插话已被 agent 取走（此后它才允许进模型历史）。"""
        ids = [str(message_id).strip() for message_id in message_ids if str(message_id).strip()]
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        with self._connect() as db:
            rows = db.execute(
                f"SELECT id, metadata FROM messages WHERE id IN ({placeholders})", ids
            ).fetchall()
            for row in rows:
                try:
                    metadata = json.loads(row["metadata"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    metadata = {}
                if (str(metadata.get(MetadataKeys.RUN_ID) or "") != run_id
                        or not metadata.get(MetadataKeys.INTERJECTION)):
                    continue
                metadata[MetadataKeys.INTERJECTION_CONSUMED] = True
                db.execute(
                    "UPDATE messages SET metadata = "
                    f"json_set(COALESCE(NULLIF(metadata, ''), '{{}}'), "
                    f"'$.{MetadataKeys.INTERJECTION_CONSUMED}', json('true')) "
                    "WHERE id = ?",
                    (row["id"],),
                )

    @_retry_transient_write
    def stop_pending_interjections(self, run_id: str) -> int:
        """冻结该 Run 的队列：保留可见，但永久阻止派发（绝不自动发送）。

        两处调用，缺一不可：
        - `ConversationRunManager.cancel`：取消那一刻就冻结——run 线程可能正卡在模型流上，
          等它自己观察到取消信号时，用户点「引导」还会把指令补进一条将死的 run；
        - `ConversationRunManager._finish`：任何终态（含正常完成）都冻结——本版没有自动
          follow-up，run 一结束队列就没有消费者了，留着 pending 只会让用户以为还能引导。
        幂等：已消费/已 stopped 的行不动，重复调用不重复计数。
        """
        stopped = 0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # SQLite 不认 Python 常量，键路径只能写字面量——用 f-string 从 MetadataKeys
            # 插值，保证 SQL 与 Python 侧读的是同一个键（改常量不会漏改这里）。
            j = "$."
            rows = db.execute(
                "SELECT id, metadata FROM messages WHERE role = 'user' "
                f"AND json_extract(metadata, '{j}{MetadataKeys.RUN_ID}') = ? "
                f"AND json_extract(metadata, '{j}{MetadataKeys.INTERJECTION}') = 1",
                (run_id,),
            ).fetchall()
            for row in rows:
                try:
                    metadata = json.loads(row["metadata"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    metadata = {}
                if metadata.get(MetadataKeys.INTERJECTION_CONSUMED):
                    continue
                if not metadata.get(MetadataKeys.INTERJECTION_STOPPED):
                    metadata[MetadataKeys.INTERJECTION_STOPPED] = True
                    db.execute(
                        "UPDATE messages SET metadata = "
                        f"json_set(COALESCE(NULLIF(metadata, ''), '{{}}'), "
                        f"'$.{MetadataKeys.INTERJECTION_STOPPED}', json('true')) "
                        "WHERE id = ?",
                        (row["id"],),
                    )
                    stopped += 1
        return stopped

    @_retry_transient_write
    def delete_run_interjection(
        self, conversation_id: str, run_id: str, message_id: str
    ) -> bool:
        """删除一条插话（已消费的不给删——它已经是本轮真实上下文的一部分）。

        对「已引导」的行只放行**队列已冻结**的那些：引导过的插话正排在 agent 的取用队列里，
        在 Run 还活着时删掉会让「模型看到的」与「用户界面显示的」直接对不上；而 Run 一旦
        结束（cancel 或 `_finish` 都会标 stopped），它就永远等不到派发了，此时必须允许删除
        ——否则用户排进去的字既发不出、也删不掉，只能看着它烂在面板里。
        """
        with self._connect() as db:
            j = "$."
            cursor = db.execute(
                "DELETE FROM messages WHERE id = ? AND conversation_id = ? AND role = 'user' "
                f"AND json_extract(metadata, '{j}{MetadataKeys.RUN_ID}') = ? "
                f"AND json_extract(metadata, '{j}{MetadataKeys.INTERJECTION}') = 1 "
                f"AND COALESCE(json_extract(metadata, '{j}{MetadataKeys.INTERJECTION_CONSUMED}'), 0) = 0 "
                f"AND (COALESCE(json_extract(metadata, '{j}{MetadataKeys.INTERJECTION_GUIDED}'), 0) = 0 "
                f"     OR COALESCE(json_extract(metadata, '{j}{MetadataKeys.INTERJECTION_STOPPED}'), 0) = 1)",
                (message_id, conversation_id, run_id),
            )
            if cursor.rowcount:
                db.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (int(time.time() * 1000), conversation_id),
                )
        return bool(cursor.rowcount)

    @_retry_transient_write
    def edit_run_interjection(
        self, conversation_id: str, run_id: str, message_id: str, content: str
    ) -> dict[str, Any]:
        """就地改写一条仍在排队的插话（不删记录，保持消息 id 稳定，前端可原位刷新）。"""
        content = str(content or "").strip()
        if not content:
            raise ValueError("插话内容不能为空")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id, role, content, metadata, trace_hash, created_at FROM messages "
                "WHERE id = ? AND conversation_id = ? AND role = 'user'",
                (message_id, conversation_id),
            ).fetchone()
            if not row:
                raise LookupError("插话不存在")
            message = self._message_dict(db, row)
            metadata = dict(message.get("metadata") or {})
            if (not metadata.get(MetadataKeys.INTERJECTION)
                    or str(metadata.get(MetadataKeys.RUN_ID) or "") != run_id
                    or metadata.get(MetadataKeys.INTERJECTION_GUIDED)
                    or metadata.get(MetadataKeys.INTERJECTION_CONSUMED)):
                raise LookupError("插话不存在或已被引导")
            db.execute("UPDATE messages SET content = ? WHERE id = ?", (content, message_id))
            db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (int(time.time() * 1000), conversation_id))
        return {**message, "content": content}

    @_retry_transient_write
    def create_chat_run(
        self,
        conversation_id: str,
        message: str,
        attachments: list[dict[str, Any]],
        agent: dict[str, Any],
        snapshot: dict[str, Any],
        interaction_mode: str,
        plan_id: str = "",
        parent_job_id: str = "",
        owner_session_id: str = "",
        display_message: str = "",
        title_text: str = "",
        folder_indexes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Atomically append the user message and create its owning run.

        ``title_text`` 仅用于首轮标题（用户原文，可能含 @ 引用）；留空时回退用 ``message``。
        ``folder_indexes``：拖入文件夹的路径索引快照（发送那一刻生成，见 app.folder_indexes_for_send）——
        与 ``attachments`` 一样写在消息 metadata 上，气泡与模型上下文各读自己的一份。

        **只返回 run**：本函数曾额外返回一份"整段会话"（``SELECT ... WHERE conversation_id``
        + 逐行 hydrate 的全部历史）供调用方核对，但生产唯一调用点 ``run/chat.py::_run_chat``
        是拿 ``_`` 丢弃它的，而运行期紧接着又会按 ``input_message_id`` 游标读一次会话——
        等于每轮白读一整段会话（含每条 metadata）。要历史的调用方自己读
        ``get_conversation``；冻结历史继续由 ``input_message_id`` 游标表达。
        """
        now = int(time.time() * 1000)
        run_id = uuid.uuid4().hex
        message_id = uuid.uuid4().hex
        metadata = {
            "attachments": attachments,
            "run_id": run_id,
            "agent_id": str(agent.get("id") or ""),
        }
        if folder_indexes:
            metadata[MetadataKeys.FOLDER_INDEXES] = folder_indexes
        if str(display_message or "").strip():
            # 气泡展示原样（含 /ref 蓝色），而 content 存模型看到的剥离版本。
            metadata["display_content"] = str(display_message)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # A parent chat Run is allowed to spawn child Jobs.  The previous
            # global conversation lock rejected those children with
            # ACTIVE_RUN, leaving the parent Agent stuck after it attempted a
            # background operation.
            # 同一把锁只由**顶层** Run 持有：后台子 Job（parent_job_id 非空）不得占用
            # 对话的 ACTIVE_RUN 互斥位，否则异步 Job 一旦长时间运行/卡住，用户在该
            # 对话发新消息会被 409 拒绝（消息不入库）。
            if not parent_job_id:
                active = db.execute(
                    "SELECT id FROM background_tasks WHERE conversation_id = ? "
                    f"AND status IN {_status_in_clause(ACTIVE_TASK_STATUSES)} "
                    "AND (parent_job_id IS NULL OR parent_job_id = '') LIMIT 1",
                    (conversation_id,),
                ).fetchone()
                if active:
                    raise RuntimeError(f"ACTIVE_RUN:{active['id']}")
            conversation = db.execute(
                "SELECT title_customized FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if not conversation:
                raise LookupError("对话不存在")
            db.execute(
                "INSERT INTO messages(id, conversation_id, role, content, metadata, created_at) "
                "VALUES (?, ?, 'user', ?, ?, ?)",
                (message_id, conversation_id, message, json.dumps(metadata, ensure_ascii=False), now),
            )
            # 本轮消息数（含刚插入的这条用户消息）：既给首轮标题判定用，也是快照的
            # `history_size` 体量标记。用 COUNT 而不是"读整段会话再 len()"——后者每轮白读
            # 整段历史（含每条 metadata），见方法注释。
            message_count = db.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            # **不把整段会话固化进快照**：那份副本每轮要写一遍、终态再整块删一遍，实测
            # 一轮 6 条历史就是 2×1.8MB 物理写入，随会话变长线性增长。运行期要的"冻结那一刻
            # 的历史"由 `input_message_id`（本轮用户消息 id，就在上面 INSERT 的同一事务里
            # 填入）当**游标**表达：它（含）之前的消息前缀就是冻结历史——顺序契约
            # (created_at, rowid) 保证并发写入只会追加在其后。`history_size` 仅用于核对。
            frozen = dict(snapshot)
            frozen["history_size"] = int(message_count)
            db.execute(
                "INSERT INTO background_tasks("
                "id, conversation_id, kind, interaction_mode, input_message_id, plan_id, "
                "agent_id, agent_name, status, message, snapshot, detail, created_at, updated_at, "
                "parent_job_id, owner_session_id"
                ") VALUES (?, ?, 'chat', ?, ?, ?, ?, ?, 'queued', ?, ?, '{}', ?, ?, ?, ?)",
                (
                    run_id,
                    conversation_id,
                    interaction_mode,
                    message_id,
                    plan_id,
                    str(agent.get("id") or ""),
                    str(agent.get("name") or "Agent"),
                    message,
                    json.dumps(frozen, ensure_ascii=False),
                    now,
                    now,
                    parent_job_id,
                    owner_session_id or conversation_id,
                ),
            )
            db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
            if message_count <= 2 and not conversation["title_customized"]:
                # 纯附件轮次（无文字）没有可用的标题文本：回退到首个附件名，避免所有
                # 图片/文件首轮都叫"新对话"而无法区分。@ 引用轮次取用户原文（非解析后的路径）。
                title_source = str(title_text or message)
                title = " ".join(title_source.strip().split())[:36] or _attachment_title(attachments)
                db.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id))
        return self.get_background_task(run_id) or {}

    @_retry_transient_write
    def create_run(
        self,
        conversation_id: str,
        message: str,
        agent: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        kind: str = "chat",
        interaction_mode: str = "craft",
        input_message_id: str = "",
        plan_id: str = "",
        parent_job_id: str = "",
        owner_session_id: str = "",
    ) -> dict[str, Any]:
        now = int(time.time() * 1000)
        task_id = uuid.uuid4().hex
        owner = owner_session_id or conversation_id
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not parent_job_id:
                active = db.execute(
                    "SELECT id FROM background_tasks WHERE conversation_id = ? "
                    f"AND status IN {_status_in_clause(ACTIVE_TASK_STATUSES)} "
                    "AND (parent_job_id IS NULL OR parent_job_id = '') LIMIT 1",
                    (conversation_id,),
                ).fetchone()
                if active:
                    raise RuntimeError(f"ACTIVE_RUN:{active['id']}")
            db.execute(
                "INSERT INTO background_tasks("
                "id, conversation_id, kind, interaction_mode, input_message_id, plan_id, "
                "agent_id, agent_name, status, message, snapshot, detail, created_at, updated_at, "
                "parent_job_id, owner_session_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, '{}', ?, ?, ?, ?)",
                (
                    task_id,
                    conversation_id,
                    kind,
                    interaction_mode,
                    input_message_id,
                    plan_id,
                    str(agent.get("id") or ""),
                    str(agent.get("name") or "Agent"),
                    message,
                    json.dumps(snapshot, ensure_ascii=False),
                    now,
                    now,
                    parent_job_id,
                    owner,
                ),
            )
        return self.get_background_task(task_id) or {}

    def get_run_snapshot(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT snapshot FROM background_tasks WHERE id = ?", (run_id,)
            ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row["snapshot"] or "{}")
        except (json.JSONDecodeError, TypeError):
            value = {}
        return value if isinstance(value, dict) else {}

    @_retry_transient_write
    def update_run_snapshot(self, run_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        """合并更新 run 快照（读取-合并-写回；快照是冻结基线的运行时补充键，如 first_turn）。"""
        current = self.get_run_snapshot(run_id) or {}
        if not isinstance(updates, dict) or not updates:
            return current
        merged = {**current, **updates}
        with self._connect() as db:
            db.execute(
                "UPDATE background_tasks SET snapshot = ?, updated_at = ? WHERE id = ?",
                (json.dumps(merged, ensure_ascii=False), int(time.time() * 1000), run_id),
            )
        return merged

    def first_chat_run_snapshot(self, conversation_id: str) -> dict[str, Any] | None:
        """该会话最早的 chat run 快照（首轮固化的 first_turn 上下文来源）。"""
        with self._connect() as db:
            return self._first_chat_run_snapshot(db, conversation_id)

    @staticmethod
    def _first_chat_run_snapshot(
        db: sqlite3.Connection, conversation_id: str,
    ) -> dict[str, Any] | None:
        row = db.execute(
            "SELECT id, snapshot FROM background_tasks "
            "WHERE conversation_id = ? AND kind = 'chat' "
            "ORDER BY created_at, rowid LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row["snapshot"] or "{}")
        except (json.JSONDecodeError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    @_retry_transient_write
    def set_conversation_first_turn(self, conversation_id: str, info: dict[str, Any]) -> None:
        """落盘会话级首轮上下文（分支对话继承、清空已结束任务后仍可读）。

        与 run 快照同时写：快照是「那次运行」的记录，本列是「这个会话」的持久记录
        （分支不复制 run 行、清空已结束任务会删掉 chat run，两者都读不到快照）。
        """
        if not conversation_id:
            return
        payload = json.dumps(info or {}, ensure_ascii=False) if info else ""
        with self._connect() as db:
            db.execute(
                "UPDATE conversations SET first_turn = ? WHERE id = ?",
                (payload, conversation_id),
            )

    def conversation_first_turn(self, conversation_id: str) -> dict[str, Any] | None:
        """会话级首轮上下文；无则 None（调用方回退读最早 chat run 快照）。"""
        with self._connect() as db:
            row = db.execute(
                "SELECT first_turn FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row["first_turn"] or "{}")
        except (json.JSONDecodeError, TypeError):
            return None
        return value if isinstance(value, dict) and value else None

    def append_run_event(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time() * 1000)
        event_type = str(payload.get("type") or "event")

        def _write() -> int:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                exists = db.execute(
                    "SELECT 1 FROM background_tasks WHERE id = ?", (run_id,)
                ).fetchone()
                if not exists:
                    raise LookupError("运行不存在")
                sequence = db.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]
                db.execute(
                    "INSERT INTO run_events(run_id, sequence, event_type, payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (run_id, sequence, event_type, json.dumps(payload, ensure_ascii=False), now),
                )
                return int(sequence)

        # 瞬时故障重试：sequence 在事务内重算，重试不会写出重复或断号的事件。
        sequence = int(self._write_with_retry(_write))
        return {**payload, "run_id": run_id, "sequence": sequence, "created_at": now}

    def list_run_events(self, run_id: str, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT sequence, payload, created_at FROM run_events "
                "WHERE run_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
                (run_id, max(0, int(after)), max(1, min(int(limit), 2000))),
            ).fetchall()
        events = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (json.JSONDecodeError, TypeError):
                payload = {"type": "error", "message": "运行事件损坏"}
            if not isinstance(payload, dict):
                payload = {"type": "error", "message": "运行事件损坏"}
            events.append(
                {**payload, "run_id": run_id, "sequence": row["sequence"], "created_at": row["created_at"]}
            )
        return events

    def terminal_event_sequence(self, run_id: str) -> int:
        """该 run 已落库的终态事件（done/cancelled/error）的最大 sequence；没有则 0。

        给 HTTP 流关流用：状态置终态与终态事件落库不是一次原子写（收尾路径先
        ``update_background_task`` 再 emit），只看状态就关流会让客户端整轮收不到终态事件。
        判据的三种情形见 ``naiba/http.py::_stream_run``。
        """
        with self._connect() as db:
            row = db.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM run_events "
                "WHERE run_id = ? AND event_type IN ("
                + ", ".join(f"'{kind}'" for kind in TERMINAL_RUN_EVENT_TYPES)
                + ")",
                (run_id,),
            ).fetchone()
        return int(row[0] or 0)

    @staticmethod
    def _normalize_usage_record(record: dict[str, Any]) -> tuple[Any, ...]:
        """把调用方给的 dict 收敛成 usage_records 的列元组（缺省 0/空串）。

        run_id 为空直接拒绝（它是幂等主键，没有它 REPLACE 语义就不成立）；
        token/请求数全部夹紧为非负整数，坏数据宁可归零也不让聚合查询翻车。
        """
        run_id = str(record.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("usage 记录缺少 run_id")
        created_at = record.get("created_at")
        try:
            created_ms = int(created_at) if created_at is not None else int(time.time() * 1000)
        except (TypeError, ValueError):
            created_ms = int(time.time() * 1000)

        def _count(key: str) -> int:
            try:
                return max(0, int(record.get(key) or 0))
            except (TypeError, ValueError):
                return 0

        return (
            run_id,
            str(record.get("conversation_id") or ""),
            str(record.get("message_id") or ""),
            str(record.get("kind") or "chat"),
            str(record.get("model_key") or ""),
            str(record.get("model_name") or ""),
            str(record.get("agent_id") or ""),
            _count("requests"),
            _count("input_tokens"),
            _count("cached_tokens"),
            _count("output_tokens"),
            _count("total_tokens"),
            created_ms,
        )

    @_retry_transient_write
    def record_usage(self, record: dict[str, Any]) -> bool:
        """落一条 run 级用量汇总（INSERT OR REPLACE：同 run 重记不翻倍）。

        返回是否新写入（False = 该 run_id 已有记录且本次数据无变化语义上是重放）。
        任何调用方（chat 收尾 / Job 收尾 / 中断恢复）都以本方法为唯一入口。
        """
        values = self._normalize_usage_record(record)
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO usage_records("
                "run_id, conversation_id, message_id, kind, model_key, model_name, "
                "agent_id, requests, input_tokens, cached_tokens, output_tokens, "
                "total_tokens, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        return True

    _USAGE_AGG_COLUMNS = (
        "COUNT(*) AS turns",
        "COALESCE(SUM(requests), 0) AS requests",
        "COALESCE(SUM(input_tokens), 0) AS input_tokens",
        "COALESCE(SUM(cached_tokens), 0) AS cached_tokens",
        "COALESCE(SUM(output_tokens), 0) AS output_tokens",
        "COALESCE(SUM(total_tokens), 0) AS total_tokens",
    )

    def usage_stats(
        self,
        since_ms: int,
        until_ms: int = 0,
        model_key: str = "",
        bucket: str = "day",
    ) -> dict[str, Any]:
        """按时间窗（可按 model_key 收窄）聚合 usage_records（只读，无重试语义）。

        返回 totals / by_model / by_day / by_bucket / first_record_at；by_day 按
        **本地时区**归日（strftime + 'localtime'）。until_ms<=0 表示不设上界。

        注意 by_day / by_bucket 的粒度是**桶×模型**（同一模型键一行）：费用按
        模型单价计算，聚合到「桶」必须保留模型维度才能精确计价，折叠成桶级单行
        是调用方（app 层）算完费用之后的职责。by_day 恒为天级；``bucket="hour"``
        时 by_bucket 为小时级（``%Y-%m-%d %H:00``，本地时区），否则与 by_day
        同内容（共用一次查询，不多花一条 SQL）。
        """
        window = "created_at >= ?"
        params: list[Any] = [max(0, int(since_ms))]
        if until_ms and int(until_ms) > 0:
            window += " AND created_at < ?"
            params.append(int(until_ms))
        key = str(model_key or "").strip()
        if key:
            window += " AND model_key = ?"
            params.append(key)
        agg = ", ".join(self._USAGE_AGG_COLUMNS)
        with self._connect() as db:
            totals = dict(db.execute(
                f"SELECT {agg} FROM usage_records WHERE {window}", tuple(params)
            ).fetchone())
            by_model = [
                dict(row)
                for row in db.execute(
                    f"SELECT model_key, model_name, {agg} FROM usage_records "
                    f"WHERE {window} GROUP BY model_key, model_name ORDER BY input_tokens DESC",
                    tuple(params),
                ).fetchall()
            ]
            by_day = [
                dict(row)
                for row in db.execute(
                    "SELECT strftime('%Y-%m-%d', created_at / 1000, 'unixepoch', 'localtime') AS day, "
                    "model_key, model_name, "
                    f"{agg} FROM usage_records WHERE {window} "
                    "GROUP BY day, model_key, model_name ORDER BY day DESC",
                    tuple(params),
                ).fetchall()
            ]
            if str(bucket or "day").strip().lower() == "hour":
                # 小时级分桶（分析视图「按小时」粒度）：桶键带当天小时段，
                # 仍是「桶×模型」粒度——计价折叠是 app 层的职责。
                by_bucket = [
                    dict(row)
                    for row in db.execute(
                        "SELECT strftime('%Y-%m-%d %H:00', created_at / 1000, 'unixepoch', 'localtime') AS day, "
                        "model_key, model_name, "
                        f"{agg} FROM usage_records WHERE {window} "
                        "GROUP BY day, model_key, model_name ORDER BY day DESC",
                        tuple(params),
                    ).fetchall()
                ]
            else:
                by_bucket = by_day
            first = db.execute(
                f"SELECT MIN(created_at) FROM usage_records WHERE {window}", tuple(params)
            ).fetchone()[0]
        for bucket_row in [totals, *by_model, *by_day, *by_bucket]:
            bucket_row["turns"] = max(0, int(bucket_row.get("turns") or 0))
            bucket_row["requests"] = max(0, int(bucket_row.get("requests") or 0))
            for name in ("input_tokens", "cached_tokens", "output_tokens", "total_tokens"):
                bucket_row[name] = max(0, int(bucket_row.get(name) or 0))
            bucket_row["uncached_tokens"] = max(0, bucket_row["input_tokens"] - bucket_row["cached_tokens"])
        return {
            "totals": totals,
            "by_model": by_model,
            "by_day": by_day,
            "by_bucket": by_bucket,
            "first_record_at": int(first or 0),
        }

    @_retry_transient_write
    def update_background_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        detail: dict[str, Any] | None = None,
        error: str | None = None,
        cancel_requested: bool | None = None,
        started: bool = False,
        finished: bool = False,
    ) -> dict[str, Any] | None:
        now = int(time.time() * 1000)
        values: dict[str, Any] = {"updated_at": now}
        if status is not None:
            if status not in {"queued", "running", "waiting", "stopping", "cancelling", "completed", "failed", "cancelled", "interrupted"}:
                raise ValueError("非法的 Run 状态")
            values["status"] = status
        if detail is not None:
            values["detail"] = json.dumps(detail, ensure_ascii=False)
        if error is not None:
            values["error"] = error[:50000]
        if cancel_requested is not None:
            values["cancel_requested"] = 1 if cancel_requested else 0
        if started:
            values["started_at"] = now
        if finished:
            values["finished_at"] = now
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._connect() as db:
            cursor = db.execute(
                f"UPDATE background_tasks SET {assignments} WHERE id = ?",
                (*values.values(), task_id),
            )
            if cursor.rowcount == 0:
                return None
            if finished and str(values.get("status") or "") in {"completed", "failed", "cancelled"}:
                # 终态收口：收缩 snapshot 的 conversation_messages（存量累积 O(N²) 的主因）。
                self._slim_run_snapshot(db, task_id)
        return self.get_background_task(task_id)

    def _slim_run_snapshot(self, db: sqlite3.Connection, task_id: str) -> None:
        """终态 Run 收缩 snapshot：去掉 conversation_messages（仅运行期/中断恢复需要）。

        每轮 Run 提交时会把完整会话消息列表固化进 snapshot（快照语义：run 线程与
        HTTP 主线程隔离、防中途改会话造成历史漂移）；该键只在运行期
        （chat.py build_model_history）与 interrupted 恢复期被读取。任务进入终态后
        再无读取方，收缩可避免历史累积 O(N²) 重复存储（实测存量库该键占 81 MB）。
        interrupted 保留（恢复重建需要）。
        """
        row = db.execute(
            "SELECT snapshot FROM background_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            return
        try:
            snapshot = json.loads(row["snapshot"] or "{}")
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(snapshot, dict) or "conversation_messages" not in snapshot:
            return
        snapshot.pop("conversation_messages", None)
        db.execute(
            "UPDATE background_tasks SET snapshot = ? WHERE id = ?",
            (json.dumps(snapshot, ensure_ascii=False), task_id),
        )

    def update_job(
        self,
        task_id: str,
        *,
        status: str | None = None,
        progress: float | None = None,
        current_step: str | None = None,
        attempt: int | None = None,
        checkpoint: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        detail: dict[str, Any] | None = None,
        cancel_requested: bool | None = None,
        started: bool = False,
        finished: bool = False,
    ) -> dict[str, Any] | None:
        """更新 Harness Job 专用字段（沿用 background_tasks 表）。"""
        now = int(time.time() * 1000)
        values: dict[str, Any] = {"updated_at": now}
        if status is not None:
            if status not in {"queued", "running", "waiting", "stopping", "cancelling", "completed", "failed", "cancelled", "interrupted"}:
                raise ValueError("非法的 Job 状态")
            values["status"] = status
        if progress is not None:
            values["progress"] = max(0.0, min(100.0, float(progress)))
        if current_step is not None:
            values["current_step"] = str(current_step)[:2000]
        if attempt is not None:
            values["attempt"] = int(attempt)
        if checkpoint is not None:
            values["checkpoint"] = json.dumps(checkpoint, ensure_ascii=False)
        if result is not None:
            values["result"] = json.dumps(result, ensure_ascii=False)
        if error is not None:
            values["error"] = error[:50000]
        if detail is not None:
            values["detail"] = json.dumps(detail, ensure_ascii=False)
        if cancel_requested is not None:
            values["cancel_requested"] = 1 if cancel_requested else 0
        if started:
            values["started_at"] = now
        if finished:
            values["finished_at"] = now
        if not values:
            return self.get_background_task(task_id)
        # 幂等短路：`updated_at` 之外的字段若与库里**逐字相同**，这条 UPDATE 不带来任何
        # 状态变化，却仍要开一条连接、写 WAL 帧并推进 checkpoint。Job 轮询（进度百分比
        # 随 elapsed 反复算）、shell 每读一行日志都调一次进度更新，命中率很高。
        # 只比对**标量列**：checkpoint/result/detail/error 是 JSON 大字段，反序列化来比
        # 会让"省一次写"变成"多几次读"，得不偿失（这些调用方也确实在改内容）。
        #
        # 判据的读必须和写一起放进 `_write_with_retry` 里：`_connect` 的瞬时故障
        # （readonly/locked）在白名单里，短路读若留在重试包装外，一次瞬时故障就会把
        # 整个调用打死——这正是本模块第 675 行记录的旧事故形态。
        assignments = ", ".join(f"{key} = ?" for key in values)
        def _write() -> bool:
            with self._connect() as db:
                current = db.execute(
                    "SELECT status, progress, current_step, attempt, cancel_requested, "
                    "started_at, finished_at FROM background_tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if current is None:
                    return False
                if _job_values_unchanged(values, self._task_dict(current)):
                    # 无信息量：不执行 UPDATE，报告"命中"以沿用"行存在"的返回语义。
                    return True
                cursor = db.execute(
                    f"UPDATE background_tasks SET {assignments} WHERE id = ?",
                    (*values.values(), task_id),
                )
                # rowcount 必须在连接关闭前取值（重试包装返回的是普通值，不是游标）。
                return cursor.rowcount != 0

        if not self._write_with_retry(_write):
            return None
        return self.get_background_task(task_id)

    def get_background_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id, conversation_id, kind, interaction_mode, input_message_id, plan_id, "
"agent_id, agent_name, status, message, detail, error, "
"cancel_requested, created_at, started_at, updated_at, finished_at, "
"parent_job_id, owner_session_id, progress, current_step, attempt, checkpoint, result "
                "FROM background_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        return self._task_dict(row) if row else None

    def list_background_tasks(
        self,
        conversation_id: str = "",
        active_only: bool = False,
        limit: int = 50,
        exclude_kinds: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        conditions = []
        parameters: list[Any] = []
        if conversation_id:
            conditions.append("conversation_id = ?")
            parameters.append(conversation_id)
        if active_only:
            conditions.append(f"status IN {_status_in_clause(ACTIVE_TASK_STATUSES)}")
        if exclude_kinds:
            # 任务面板只要后台作业。过滤必须在 SQL 层：默认 limit 是 50，而 chat 行
            # 占绝大多数，先捞出来再筛会把较早的作业挤出窗口（面板只显示最近几条）。
            conditions.append(f"kind NOT IN {_kinds_not_in_clause(tuple(exclude_kinds))}")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.append(max(1, min(int(limit), 200)))
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, conversation_id, kind, interaction_mode, input_message_id, plan_id, "
"agent_id, agent_name, status, message, detail, error, "
"cancel_requested, created_at, started_at, updated_at, finished_at, "
"parent_job_id, owner_session_id, progress, current_step, attempt, checkpoint, result "
                f"FROM background_tasks {where} ORDER BY created_at DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [self._task_dict(row) for row in rows]

    @_retry_transient_write
    def clear_terminal_background_tasks(self) -> int:
        """Remove completed task records and their cascaded run events, never active runs.

        删除前把 job_id 记入 ``cleaned_jobs``，使跨对话查询能区分
        「Job 记录已被清理」与「Job ID 从未存在」。
        """
        now = int(time.time() * 1000)
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, kind FROM background_tasks "
                "WHERE status IN ('completed', 'failed', 'cancelled', 'interrupted')"
            ).fetchall()
            for row in rows:
                db.execute(
                    "INSERT OR REPLACE INTO cleaned_jobs(job_id, kind, cleaned_at) VALUES (?, ?, ?)",
                    (str(row["id"]), str(row["kind"] or ""), now),
                )
            cursor = db.execute(
                "DELETE FROM background_tasks WHERE status IN ('completed', 'failed', 'cancelled', 'interrupted')"
            )
        return cursor.rowcount

    def is_job_cleaned(self, job_id: str) -> bool:
        """判断某个 Job ID 是否曾存在但记录已被清理（用于区分错误信息）。"""
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM cleaned_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return row is not None

    # ---- 计划（Plan 模式） ----
    @_retry_transient_write
    def create_plan(self, conversation_id: str, question: str) -> dict[str, Any]:
        now = int(time.time() * 1000)
        plan_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                "INSERT INTO plans(id, conversation_id, title, status, question, content, steps, created_at, updated_at) "
                "VALUES (?, ?, '', 'prepare', ?, '', '[]', ?, ?)",
                (plan_id, conversation_id, (question or "")[:20000], now, now),
            )
        return self.get_plan(plan_id) or {}

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id, conversation_id, title, status, question, content, steps, error, archive_path, detail, "
                "created_at, updated_at, started_at, finished_at FROM plans WHERE id = ?",
                (plan_id,),
            ).fetchone()
        return self._plan_dict(row) if row else None

    def latest_plan(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id, conversation_id, title, status, question, content, steps, error, archive_path, detail, "
                "created_at, updated_at, started_at, finished_at FROM plans "
                "WHERE conversation_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return self._plan_dict(row) if row else None

    def list_plans(self, conversation_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, conversation_id, title, status, question, content, steps, error, archive_path, detail, "
                "created_at, updated_at, started_at, finished_at FROM plans "
                "WHERE conversation_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (conversation_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [self._plan_dict(row) for row in rows]

    @_retry_transient_write
    def update_plan(
        self,
        plan_id: str,
        *,
        title: str | None = None,
        status: str | None = None,
        question: str | None = None,
        content: str | None = None,
        steps: list[dict[str, Any]] | None = None,
        error: str | None = None,
        archive_path: str | None = None,
        detail: dict[str, Any] | None = None,
        started: bool = False,
        finished: bool = False,
    ) -> dict[str, Any] | None:
        now = int(time.time() * 1000)
        values: dict[str, Any] = {"updated_at": now}
        if title is not None:
            values["title"] = str(title).strip()[:200]
        if status is not None:
            if status not in ("prepare", "ready", "building", "finished", "failed", "cancelled"):
                raise ValueError("非法的计划状态")
            values["status"] = status
        if question is not None:
            values["question"] = str(question)[:20000]
        if content is not None:
            values["content"] = str(content)[:100000]
        if steps is not None:
            values["steps"] = json.dumps(steps, ensure_ascii=False)
        if error is not None:
            values["error"] = str(error)[:20000]
        if archive_path is not None:
            values["archive_path"] = str(archive_path)[:2000]
        if detail is not None:
            values["detail"] = json.dumps(detail, ensure_ascii=False)
        if started:
            values["started_at"] = now
        if finished:
            values["finished_at"] = now
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._connect() as db:
            cursor = db.execute(
                f"UPDATE plans SET {assignments} WHERE id = ?",
                (*values.values(), plan_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_plan(plan_id)

    @staticmethod
    def _conversation_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["interaction_mode"] = "craft"
        # 轻量模式已退役（2026-09：工具/Skill 一律按 Agent 固化集，富文本恒开）：
        # 历史列仍留在表里，但不再对外暴露，避免旧值被前端误用。
        result.pop("lightweight_mode", None)
        result.pop("lightweight_disabled_features", None)
        # 收藏标记统一成 0/1 整数（列可能来自旧库迁移前的行对象，避免 None/字符串）。
        result["favorite"] = 1 if int(result.get("favorite") or 0) else 0
        # 归档 / 手动排序位：同样统一成整数；老库（v19/v20 之前）行对象缺列时补默认值，
        # 前端只按 0/1 与数值大小判断，不必再判 undefined。
        result["archived"] = 1 if int(result.get("archived") or 0) else 0
        result["sort_order"] = int(result.get("sort_order") or 0)
        # 分支来源列：老库（v18 之前）行对象里可能没有这两列，统一补空串，
        # 前端只按「非空 = 是分支」判断，不必再判 undefined。
        result["branched_from_id"] = str(result.get("branched_from_id") or "")
        result["branch_message_id"] = str(result.get("branch_message_id") or "")
        result.setdefault("branch_count", 0)
        try:
            parsed = json.loads(result.get("enabled_tool_ids") or "[]")
            if not isinstance(parsed, list):
                parsed = []
        except (json.JSONDecodeError, TypeError):
            parsed = []
        # run_command 已并入 pwsh：历史会话固化工具集可能含死工具名，读时统一映射，
        # 避免该会话的模型声明里既没有 pwsh 也没有 run_command 而丢失命令执行。
        result["enabled_tool_ids"] = [
            "pwsh" if str(item) == "run_command" else item for item in parsed
        ]
        try:
            result["skill_policy"] = json.loads(
                result.get("skill_policy") or "{}"
            )
        except (json.JSONDecodeError, TypeError):
            result["skill_policy"] = {}
        return result

    @staticmethod
    def _plan_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in ("steps", "detail"):
            try:
                result[key] = json.loads(result.get(key) or ("[]" if key == "steps" else "{}"))
            except (json.JSONDecodeError, TypeError):
                result[key] = [] if key == "steps" else {}
        return result

    def _message_dict(
        self,
        db: sqlite3.Connection,
        row: sqlite3.Row,
        trace_cache: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """把 messages 行转成消息字典，并把 trace 从独立表**透明补回** ``metadata``。

        为什么必须补回：`history.py` 与前端都按 `metadata["trace"]` 读取，抽表不能改变
        这个契约。调用方拿不到 trace 时保持"无 trace"（与"这条消息本来就没有 trace"同形），
        `build_model_history` 的既有兜底分支照旧成立。

        ``trace_cache`` 由一次批量读取（如 `get_conversation`）传入以复用同 blob，
        缺省时每次查询一次——同一 hash 在同一批次内极少重复，不额外挂全局缓存。
        """
        result = dict(row)
        try:
            metadata = json.loads(result.get("metadata") or "{}")
        except json.JSONDecodeError:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        trace_hash = str(result.get("trace_hash") or "")
        if trace_hash and "trace" not in metadata:
            trace = _hydrate_trace(
                db, trace_hash, trace_cache if trace_cache is not None else {}
            )
            if trace is not None:
                metadata["trace"] = trace
        result["metadata"] = metadata
        # trace_hash 是实现细节，不透给消费方（避免它被当成 metadata 契约的一部分）。
        result.pop("trace_hash", None)
        return result

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        try:
            result["detail"] = json.loads(result.get("detail") or "{}")
        except json.JSONDecodeError:
            result["detail"] = {}
        for key in ("checkpoint", "result"):
            try:
                result[key] = json.loads(result.get(key) or "{}")
            except (json.JSONDecodeError, TypeError):
                result[key] = {}
        try:
            result["progress"] = float(result.get("progress") or 0)
        except (TypeError, ValueError):
            result["progress"] = 0.0
        result["attempt"] = int(result.get("attempt") or 0)
        result["cancel_requested"] = bool(result.get("cancel_requested"))
        return result
