"""打标器 SQLite：标注库 + 逐图运行状态（恢复权威）。

约定（与 ``naiba/storage/store.py`` 同源，但**独立库** ``data/labeler/labeler.db``）：
- 每次操作开一条短连接（``timeout=30`` + ``PRAGMA busy_timeout``），不跨线程共享；
  WAL 让读不挡写。写操作遇到瞬时锁冲突按退避重试（``_retry_transient``）。
- **逐图完成/失败是一次事务**：标注行 + ``task_items`` 状态 + 用量 + 计数依据一起提交。
  事件 JSONL 只是通知日志，恢复一律以本文件为准。
- ``running`` 状态在模型请求**之前**提交；进程在请求返回后、事务提交前崩溃时，
  该行留在 ``running``，启动恢复把它标成 ``uncertain``（可能已计费，不谎称 0 成本）。
"""

from __future__ import annotations

import functools
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from naiba.labeler import LABELER_ITEM_STATES

# 单次连接拿锁的等待上限与写重试策略（与主库同一思路，数值按打标器的小事务收紧）。
_BUSY_TIMEOUT_MS = 5000
_WRITE_RETRY_ATTEMPTS = 4
_WRITE_RETRY_DELAY = 0.08
_TRANSIENT_MARKERS = ("database is locked", "database is busy", "database table is locked")


class LabelerDBError(RuntimeError):
    """标注库操作失败（非瞬时），由上层转成用户可读错误。"""


def now_ms() -> int:
    return int(time.time() * 1000)


def _is_transient(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _retry_transient(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def wrapper(self: "LabelerDB", *args: Any, **kwargs: Any) -> Any:
        last: sqlite3.OperationalError | None = None
        for attempt in range(_WRITE_RETRY_ATTEMPTS):
            try:
                return method(self, *args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not _is_transient(exc):
                    raise
                last = exc
                time.sleep(_WRITE_RETRY_DELAY * (attempt + 1))
        raise LabelerDBError(f"标注库繁忙，请稍后重试：{last}") from last

    return wrapper


def _loads(text: Any, fallback: Any) -> Any:
    try:
        value = json.loads(str(text or ""))
    except (json.JSONDecodeError, TypeError, ValueError):
        return fallback
    return value


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


class LabelerDB:
    def __init__(self, data_dir: Path):
        self._root = Path(data_dir) / "labeler"
        self.db_path = self._root / "labeler.db"
        self._initialize()

    # ---- 连接与建表 ----

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self._root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS task_items (
                    task_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL DEFAULT 0,
                    path TEXT NOT NULL,
                    path_key TEXT NOT NULL,
                    content_version TEXT NOT NULL,
                    relative_path TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    size INTEGER NOT NULL DEFAULT 0,
                    mtime_ns INTEGER NOT NULL DEFAULT 0,
                    original_sha256 TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'queued',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    error_category TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    usage_json TEXT NOT NULL DEFAULT '{}',
                    annotation_id INTEGER,
                    config_fingerprint TEXT NOT NULL DEFAULT '',
                    started_at INTEGER,
                    finished_at INTEGER,
                    PRIMARY KEY (task_id, item_id)
                );
                CREATE INDEX IF NOT EXISTS idx_labeler_items_state
                    ON task_items(task_id, state);

                CREATE TABLE IF NOT EXISTS annotations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT NOT NULL,
                    source_task_id TEXT NOT NULL,
                    path_key TEXT NOT NULL,
                    content_version TEXT NOT NULL,
                    path TEXT NOT NULL,
                    relative_path TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    original_sha256 TEXT NOT NULL DEFAULT '',
                    engine TEXT NOT NULL,
                    output_mode TEXT NOT NULL,
                    preset TEXT,
                    prompt_version INTEGER NOT NULL DEFAULT 1,
                    model_key TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    config_fingerprint TEXT NOT NULL,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    confidences_json TEXT NOT NULL DEFAULT '[]',
                    description TEXT NOT NULL DEFAULT '',
                    usage_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_labeler_ann_identity
                    ON annotations(path_key, content_version, config_fingerprint);
                CREATE INDEX IF NOT EXISTS idx_labeler_ann_task
                    ON annotations(source_task_id);
                """
            )
            # 旧库迁移：original_sha256 是训练包原图留存（2026-10-23）新增列，
            # 老库 ALTER 补列，历史行留空串（导出训练包时按「没有原图记录」拒绝）。
            for table in ("task_items", "annotations"):
                columns = {
                    str(row["name"])
                    for row in db.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if "original_sha256" not in columns:
                    db.execute(
                        f"ALTER TABLE {table} ADD COLUMN original_sha256 TEXT NOT NULL DEFAULT ''"
                    )

    # ---- 逐图状态 ----

    @_retry_transient
    def add_items(self, task_id: str, items: list[dict[str, Any]], config_fingerprint: str) -> int:
        """登记一批待处理项（任务提交时一次写入，状态 queued）。"""
        if not items:
            return 0
        rows = []
        for ordinal, item in enumerate(items):
            state = str(item.get("state") or "queued")
            if state not in LABELER_ITEM_STATES:
                state = "queued"
            rows.append((
                task_id, item["item_id"], ordinal, item["path"], item["path_key"],
                item["content_version"], item.get("relative_path") or "", item.get("name") or "",
                int(item.get("size") or 0), int(item.get("mtime_ns") or 0),
                str(item.get("original_sha256") or ""),
                state, 0, "", "", "{}", item.get("annotation_id"),
                config_fingerprint,
                int(item.get("started_at") or 0) or None,
                int(item.get("finished_at") or 0) or None,
            ))
        with self._connect() as db:
            db.executemany(
                """
                INSERT INTO task_items (
                    task_id, item_id, ordinal, path, path_key, content_version,
                    relative_path, name, size, mtime_ns, original_sha256, state,
                    attempt_count, error_category, error, usage_json, annotation_id,
                    config_fingerprint, started_at, finished_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(task_id, item_id) DO NOTHING
                """,
                rows,
            )
        return len(rows)

    def list_items(self, task_id: str, states: tuple[str, ...] | None = None,
                   limit: int = 2000) -> list[dict[str, Any]]:
        sql = "SELECT * FROM task_items WHERE task_id=?"
        params: list[Any] = [task_id]
        if states:
            placeholders = ",".join("?" for _ in states)
            sql += f" AND state IN ({placeholders})"
            params.extend(states)
        sql += " ORDER BY ordinal ASC LIMIT ?"
        params.append(int(limit))
        with self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [self._item_row(row) for row in rows]

    def pending_items(self, task_id: str) -> list[dict[str, Any]]:
        return self.list_items(task_id, states=("queued",))

    @_retry_transient
    def mark_running(self, task_id: str, item_id: str) -> None:
        """模型请求**之前**提交：崩溃后这一行就是 ``uncertain`` 的证据。"""
        with self._connect() as db:
            db.execute(
                """
                UPDATE task_items
                   SET state='running', attempt_count=attempt_count+1, started_at=?,
                       error='', error_category=''
                 WHERE task_id=? AND item_id=?
                   AND state IN ('queued','running','failed','cancelled','uncertain')
                """,
                (now_ms(), task_id, item_id),
            )

    @_retry_transient
    def complete_item(
        self,
        task_id: str,
        item_id: str,
        *,
        annotation: dict[str, Any],
        usage: dict[str, Any],
        config_fingerprint: str,
    ) -> tuple[int, bool]:
        """一次事务提交「标注行 + 逐图成功状态 + 用量」。

        返回 ``(annotation_id, 是否命中既有结果)``。命中时复用既有行（保留 created_at），
        但把最新一次的值与 updated_at 更新掉——同一配置重跑以最近一次结果为准。
        """
        stamp = now_ms()
        with self._connect() as db:
            existing = db.execute(
                "SELECT id FROM annotations WHERE path_key=? AND content_version=? AND config_fingerprint=?",
                (annotation["path_key"], annotation["content_version"], config_fingerprint),
            ).fetchone()
            reused = existing is not None
            if reused:
                annotation_id = int(existing["id"])
                # 复用同一行时**不改 source_task_id / item_id**：标注归属于第一个产出它的
                # 任务；后续任务通过 task_items.annotation_id 关联到它（查询走 JOIN），
                # 历史记录不会因为重跑而「搬走」。
                db.execute(
                    """
                    UPDATE annotations SET
                        tags_json=?, confidences_json=?, description=?, usage_json=?,
                        model_key=?, model_name=?, relative_path=?, name=?,
                        original_sha256=CASE WHEN original_sha256='' THEN ? ELSE original_sha256 END,
                        updated_at=?
                      WHERE id=?
                    """,
                    (
                        _dumps(annotation.get("tags") or []),
                        _dumps(annotation.get("confidences") or []),
                        str(annotation.get("description") or ""),
                        _dumps(usage or {}),
                        str(annotation.get("model_key") or ""),
                        str(annotation.get("model_name") or ""),
                        str(annotation.get("relative_path") or ""),
                        str(annotation.get("name") or ""),
                        str(annotation.get("original_sha256") or ""),
                        stamp, annotation_id,
                    ),
                )
            else:
                cursor = db.execute(
                    """
                    INSERT INTO annotations (
                        item_id, source_task_id, path_key, content_version, path,
                        relative_path, name, original_sha256, engine, output_mode,
                        preset, prompt_version, model_key, model_name,
                        config_fingerprint, tags_json, confidences_json, description,
                        usage_json, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        item_id, task_id, annotation["path_key"], annotation["content_version"],
                        annotation["path"], annotation.get("relative_path") or "",
                        annotation.get("name") or "", str(annotation.get("original_sha256") or ""),
                        annotation.get("engine") or "provider",
                        annotation.get("output_mode") or "keywords", annotation.get("preset") or "",
                        int(annotation.get("prompt_version") or 1),
                        annotation.get("model_key") or "", annotation.get("model_name") or "",
                        config_fingerprint, _dumps(annotation.get("tags") or []),
                        _dumps(annotation.get("confidences") or []),
                        str(annotation.get("description") or ""), _dumps(usage or {}),
                        stamp, stamp,
                    ),
                )
                annotation_id = int(cursor.lastrowid or 0)
            db.execute(
                """
                UPDATE task_items
                   SET state='succeeded', annotation_id=?, usage_json=?, error='',
                       error_category='', finished_at=?
                 WHERE task_id=? AND item_id=?
                """,
                (annotation_id, _dumps(usage or {}), stamp, task_id, item_id),
            )
        return annotation_id, reused

    @_retry_transient
    def fail_item(self, task_id: str, item_id: str, *, error: str, category: str,
                  usage: dict[str, Any] | None = None) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE task_items
                   SET state='failed', error=?, error_category=?, usage_json=?, finished_at=?
                 WHERE task_id=? AND item_id=?
                """,
                (str(error or "")[:2000], str(category or "")[:40], _dumps(usage or {}),
                 now_ms(), task_id, item_id),
            )

    @_retry_transient
    def cancel_item(self, task_id: str, item_id: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE task_items SET state='cancelled', finished_at=? WHERE task_id=? AND item_id=?",
                (now_ms(), task_id, item_id),
            )

    @_retry_transient
    def cancel_pending(self, task_id: str) -> int:
        """取消尚未开始（queued）的项；请求中/已完成项不动。返回受影响条数。"""
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE task_items SET state='cancelled', finished_at=? WHERE task_id=? AND state='queued'",
                (now_ms(), task_id),
            )
            return int(cursor.rowcount or 0)

    @_retry_transient
    def delete_items(self, task_id: str) -> int:
        """删除一个任务的逐图运行行（任务删除用，2026-10-26 计划 §3）。

        ``annotations`` 不动：标注按「内容 + 配置指纹」全局复用、归属第一个产出
        它的任务，删除任务后仍可通过标注库查询与导出。
        """
        with self._connect() as db:
            cursor = db.execute("DELETE FROM task_items WHERE task_id=?", (task_id,))
            return int(cursor.rowcount or 0)

    @_retry_transient
    def mark_running_as_uncertain(self, task_id: str = "") -> int:
        """启动恢复：``running`` 说明请求已发出但结果没落库（可能已计费）。"""
        with self._connect() as db:
            if task_id:
                cursor = db.execute(
                    "UPDATE task_items SET state='uncertain', finished_at=? "
                    "WHERE task_id=? AND state='running'",
                    (now_ms(), task_id),
                )
            else:
                cursor = db.execute(
                    "UPDATE task_items SET state='uncertain', finished_at=? WHERE state='running'",
                    (now_ms(),),
                )
            return int(cursor.rowcount or 0)

    def counts(self, task_id: str) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT state, COUNT(*) AS n FROM task_items WHERE task_id=? GROUP BY state",
                (task_id,),
            ).fetchall()
        by_state = {str(row["state"]): int(row["n"]) for row in rows}
        total = sum(by_state.values())
        return {
            "total": total,
            "done": by_state.get("succeeded", 0),
            "failed": by_state.get("failed", 0),
            "uncertain": by_state.get("uncertain", 0),
            "cancelled": by_state.get("cancelled", 0),
            "queued": by_state.get("queued", 0),
            "running": by_state.get("running", 0),
        }

    # ---- 复用与查询 ----

    def find_reusable(self, path_key: str, content_version_value: str,
                      config_fingerprint: str) -> dict[str, Any] | None:
        """同图同内容同配置的既有成功结果（提交前先查，命中就不重复调用模型）。"""
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM annotations WHERE path_key=? AND content_version=? AND config_fingerprint=?",
                (path_key, content_version_value, config_fingerprint),
            ).fetchone()
        return self._annotation_row(row) if row else None

    def annotations(self, *, path_key: str = "", task_id: str = "", query: str = "",
                    limit: int = 500) -> list[dict[str, Any]]:
        # task_id 走 task_items JOIN：标注行归属「第一个产出它的任务」（复用不改归属），
        # 但每个任务仍要能看到自己用到的那份结果，导出也只按该任务的实际项。
        if task_id:
            sql = ("SELECT a.* FROM annotations a "
                   "JOIN task_items i ON i.annotation_id = a.id WHERE i.task_id=?")
            params: list[Any] = [task_id]
            prefix = "a."
        else:
            sql = "SELECT * FROM annotations WHERE 1=1"
            params = []
            prefix = ""
        if path_key:
            sql += f" AND {prefix}path_key=?"
            params.append(path_key)
        if query:
            like = f"%{query.lower()}%"
            sql += (f" AND (lower({prefix}name) LIKE ? OR lower({prefix}relative_path) LIKE ?"
                    f" OR lower({prefix}tags_json) LIKE ? OR lower({prefix}description) LIKE ?)")
            params.extend([like, like, like, like])
        if task_id:
            sql += " ORDER BY i.ordinal ASC LIMIT ?"
        else:
            sql += f" ORDER BY {prefix}updated_at DESC, {prefix}id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 2000)))
        with self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [self._annotation_row(row) for row in rows]

    def annotations_by_ids(self, ids: list[int]) -> list[dict[str, Any]]:
        clean = [int(value) for value in ids if str(value).strip().lstrip("-").isdigit()]
        if not clean:
            return []
        placeholders = ",".join("?" for _ in clean)
        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM annotations WHERE id IN ({placeholders}) ORDER BY id ASC",
                clean,
            ).fetchall()
        return [self._annotation_row(row) for row in rows]

    def annotation_ids_for_task(self, task_id: str) -> list[int]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT annotation_id FROM task_items WHERE task_id=? AND annotation_id IS NOT NULL "
                "ORDER BY ordinal ASC",
                (task_id,),
            ).fetchall()
        return [int(row["annotation_id"]) for row in rows]

    def tag_counts(self, limit: int = 200) -> list[dict[str, Any]]:
        """按标签聚合频次（跨全部标注；前端「按标签」视图）。"""
        counter: dict[str, int] = {}
        with self._connect() as db:
            rows = db.execute("SELECT tags_json FROM annotations").fetchall()
        for row in rows:
            for tag in _loads(row["tags_json"], []):
                text = str(tag or "").strip()
                if text:
                    counter[text] = counter.get(text, 0) + 1
        ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        return [{"tag": tag, "count": count} for tag, count in ordered[: max(1, int(limit))]]

    def referenced_paths(self, task_ids: list[str]) -> set[str]:
        """活跃/可重试任务引用的图片路径（缓存清理的引用保护）。"""
        clean = [str(value).strip() for value in task_ids if str(value or "").strip()]
        if not clean:
            return set()
        placeholders = ",".join("?" for _ in clean)
        with self._connect() as db:
            rows = db.execute(
                f"SELECT DISTINCT path FROM task_items WHERE task_id IN ({placeholders})",
                clean,
            ).fetchall()
        return {str(row["path"]) for row in rows if row["path"]}

    def referenced_original_digests(self) -> set[str]:
        """原图留存引用集：所有标注与逐图任务项记录的原图摘要。

        原图清理（``originals.prune_unreferenced``）据此保留在用文件；
        任务项行含未完成任务，所以「刚上传还没跑完」的原图也不会被清。
        """
        out: set[str] = set()
        with self._connect() as db:
            rows = db.execute(
                "SELECT original_sha256 AS digest FROM annotations "
                "UNION SELECT original_sha256 FROM task_items"
            ).fetchall()
        for row in rows:
            text = str(row["digest"] or "").strip().lower()
            if text:
                out.add(text)
        return out

    # ---- 行 → dict ----

    @staticmethod
    def _item_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "task_id": str(row["task_id"]),
            "item_id": str(row["item_id"]),
            "ordinal": int(row["ordinal"] or 0),
            "path": str(row["path"]),
            "path_key": str(row["path_key"]),
            "content_version": str(row["content_version"]),
            "relative_path": str(row["relative_path"] or ""),
            "name": str(row["name"] or ""),
            "size": int(row["size"] or 0),
            "mtime_ns": int(row["mtime_ns"] or 0),
            "original_sha256": str(row["original_sha256"] or ""),
            "state": str(row["state"]),
            "attempt_count": int(row["attempt_count"] or 0),
            "error_category": str(row["error_category"] or ""),
            "error": str(row["error"] or ""),
            "usage": _loads(row["usage_json"], {}),
            "annotation_id": int(row["annotation_id"]) if row["annotation_id"] is not None else None,
            "config_fingerprint": str(row["config_fingerprint"] or ""),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    @staticmethod
    def _annotation_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "item_id": str(row["item_id"]),
            "source_task_id": str(row["source_task_id"]),
            "path_key": str(row["path_key"]),
            "content_version": str(row["content_version"]),
            "path": str(row["path"]),
            "relative_path": str(row["relative_path"] or ""),
            "name": str(row["name"] or ""),
            "original_sha256": str(row["original_sha256"] or ""),
            "engine": str(row["engine"]),
            "output_mode": str(row["output_mode"]),
            "preset": str(row["preset"] or ""),
            "prompt_version": int(row["prompt_version"] or 1),
            "model_key": str(row["model_key"]),
            "model_name": str(row["model_name"]),
            "config_fingerprint": str(row["config_fingerprint"]),
            "tags": _loads(row["tags_json"], []),
            "confidences": _loads(row["confidences_json"], []),
            "description": str(row["description"] or ""),
            "usage": _loads(row["usage_json"], {}),
            "created_at": int(row["created_at"] or 0),
            "updated_at": int(row["updated_at"] or 0),
        }
