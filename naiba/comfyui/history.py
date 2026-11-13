"""Rebuildable metadata index and stable cursors for file-backed history.

Only small summaries remain in memory. Directory signatures detect writes by
other store instances/processes; unchanged workflow snapshots are not parsed
again on each list refresh. Full records are loaded only for the requested page.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from naiba.comfyui import COMFYUI_TASK_STATUSES
from naiba.core.contracts import (
    COMFYUI_HISTORY_HAS_MORE_KEY,
    COMFYUI_HISTORY_NEXT_CURSOR_KEY,
)

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def history_record_missing(path: Path) -> bool:
    """Only confirmed absence may evict a browser's retained history row."""
    try:
        path.stat()
    except FileNotFoundError:
        return True
    except OSError:
        # A sharing/permission failure is not evidence of deletion.
        pass
    return False


def parse_history_ids(value: Any, prefix: str) -> list[str]:
    if not isinstance(value, str) or len(value) > 20100:
        raise ValueError("ids 必须是逗号分隔的任务或批次编号，最多 100 个")
    ids = value.split(",")
    if (not 1 <= len(ids) <= 100
            or any(not re.fullmatch(re.escape(prefix) + r"-[A-Za-z0-9_-]{1,196}", key) for key in ids)):
        raise ValueError("ids 编号无效或超过 100 个")
    return list(dict.fromkeys(ids))


def parse_page_size(value: Any) -> int:
    if value is None:
        return DEFAULT_PAGE_SIZE
    text = str(value)
    if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= MAX_PAGE_SIZE:
        raise ValueError(f"page_size 必须是 1–{MAX_PAGE_SIZE} 的整数")
    return int(text)


def _timestamp(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _cursor_key(cursor: Any, scope: str) -> tuple[int, str] | None:
    if cursor is None or cursor == "":
        return None
    if not isinstance(cursor, str) or len(cursor) > 2048:
        raise ValueError("cursor 格式无效，请重新读取第一页")
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw.decode("utf-8"))
        if (not isinstance(value, list) or len(value) != 4 or type(value[0]) is not int or value[0] != 1
                or value[1] != scope or type(value[2]) is not int or value[2] < 0
                or not isinstance(value[3], str) or not value[3]
                or len(value[3]) > 200 or "/" in value[3] or "\\" in value[3]):
            raise ValueError
        return value[2], value[3]
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error, RecursionError):
        raise ValueError("cursor 格式无效或不属于当前列表，请重新读取第一页") from None


def _encode_cursor(key: tuple[int, str], scope: str) -> str:
    raw = json.dumps([1, scope, *key], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class HistoryIndex:
    def __init__(self, directory: Path, prefix: str, lock: Any, *, tasks: bool = False):
        self.directory = directory
        self.prefix = prefix
        self.lock = lock
        self.tasks = tasks
        self._entries: dict[str, tuple[tuple[int, ...], dict[str, Any] | None]] = {}

    def summaries(self) -> list[dict[str, Any]]:
        with self.lock:
            seen: set[str] = set()
            try:
                entries = os.scandir(self.directory)
            except FileNotFoundError:
                self._entries.clear()
                return []
            with entries:
                for entry in entries:
                    name = entry.name
                    if (not name.startswith(f"{self.prefix}-") or not name.endswith(".json")
                            or name.endswith(".schemes.json")):
                        continue
                    seen.add(name)
                    try:
                        stat = entry.stat()
                        signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
                        cached = self._entries.get(name)
                        if cached is not None and cached[0] == signature:
                            continue
                        payload = json.loads(Path(entry.path).read_text(encoding="utf-8"))
                        summary = None
                        if isinstance(payload, dict) and payload.get("id") == name[:-5]:
                            summary = {"id": payload["id"], "created_at": _timestamp(payload.get("created_at"))}
                            if self.tasks:
                                status = payload.get("status")
                                source = payload.get("input")
                                summary.update({
                                    "status": status if status in COMFYUI_TASK_STATUSES else "interrupted",
                                    "batch": bool(isinstance(source, dict) and source.get("batch_id")),
                                })
                        self._entries[name] = signature, summary
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                        # Do not cache transient read failures: the next request retries.
                        self._entries.pop(name, None)
            for name in self._entries.keys() - seen:
                del self._entries[name]
            return [dict(summary) for _, summary in self._entries.values() if summary is not None]


def history_page(index: HistoryIndex, page_size: Any, cursor: Any, *, scope: str,
                 load: Callable[[str], dict[str, Any] | None],
                 include: Callable[[dict[str, Any]], bool] | None = None,
                 still_include: Callable[[dict[str, Any]], bool] | None = None) -> dict[str, Any]:
    size = parse_page_size(page_size)
    before = _cursor_key(cursor, scope)
    candidates = sorted(
        (row for row in index.summaries() if include is None or include(row)),
        key=lambda row: (row["created_at"], row["id"]), reverse=True,
    )
    rows: list[dict[str, Any]] = []
    keys: list[tuple[int, str]] = []
    for summary in candidates:
        key = summary["created_at"], summary["id"]
        if before is not None and key >= before:
            continue
        record = load(summary["id"])
        # A record can disappear or change status after the directory snapshot.
        if record is None or (still_include is not None and not still_include(record)):
            continue
        rows.append(record)
        keys.append(key)
        if len(rows) > size:
            break
    more = len(rows) > size
    return {"rows": rows[:size], "count": min(len(rows), size),
            COMFYUI_HISTORY_NEXT_CURSOR_KEY: _encode_cursor(keys[size - 1], scope) if more else "",
            COMFYUI_HISTORY_HAS_MORE_KEY: more}
