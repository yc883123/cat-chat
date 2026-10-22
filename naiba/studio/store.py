"""Studio 任务持久化：data_dir/studio/tasks/<task_id>.json + <task_id>.events.jsonl。

- `*.json`：任务记录（状态/输入摘要/结果/用量/交接指针），同目录临时文件 + os.replace 原子替换。
- `*.events.jsonl`：逐行事件流（结构见 contracts.STUDIO_TASK_EVENT_TYPES），追加写。
  行末可能因断电半截——读取端逐行容错，坏行跳过不回滚。
- 任务文件不进 SQLite：Studio 生成的结果/事件不打消息库的任何主意。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from naiba.core.contracts import (
    STUDIO_TERMINAL_TASK_EVENT_TYPES,
    STUDIO_TASK_EVENT_TYPES,
    STUDIO_TASK_STATUSES,
)


class StudioStoreError(ValueError):
    """任务数据非法。"""


def now_ms() -> int:
    return int(time.time() * 1000)


class StudioStore:
    """线程安全（锁内追加重放）；文件布局见模块注释。"""

    def __init__(self, data_dir: Path):
        self._dir = Path(data_dir) / "studio" / "tasks"
        self._lock = threading.RLock()
        # 引用保护缓存：(单调时间, 路径键集合)。清理器对每个候选文件都会问一次
        # 「这个文件还被 Studio 任务引用吗」，不能每次都重读全部任务 JSON。
        self._ref_cache: tuple[float, set[str]] | None = None

    # ---- 路径 ----

    def _record_path(self, task_id: str) -> Path:
        return self._dir / f"{task_id}.json"

    def _events_path(self, task_id: str) -> Path:
        return self._dir / f"{task_id}.events.jsonl"

    def new_task_id(self) -> str:
        return f"st-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:12]}"

    def _write_json(self, path: Path, payload: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{uuid.uuid4().hex}.tmp")
        try:
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp_path, path)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    # ---- 任务记录 ----

    def create_task(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """登记 queued 任务（输入实在落盘，避免服务器重启后无法重试）。"""
        task_id = self.new_task_id()
        record = {
            "id": task_id,
            "status": "queued",
            "detail": "",
            "input": input_data,
            "result": "",
            "result_partial": "",
            "reasoning": [],
            "error": "",
            "usage": {},
            "performance": {},
            "finish_reason": "",
            "handoff": None,
            "created_at": now_ms(),
            "updated_at": now_ms(),
            "started_at": None,
            "finished_at": None,
        }
        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._write_json(self._record_path(task_id), record)
            self._events_path(task_id).touch(exist_ok=True)
            self._ref_cache = None
        return record

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(self._record_path(task_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return self._sanitize(payload)

    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any] | None:
        with self._lock:
            current = self.get_task(task_id)
            if current is None:
                return None
            for key in ("status", "detail", "result", "result_partial", "reasoning", "error",
                        "usage", "performance", "finish_reason", "handoff", "started_at", "finished_at"):
                if key in fields:
                    current[key] = fields[key]
            current["updated_at"] = now_ms()
            self._write_json(self._record_path(task_id), current)
        return current

    @staticmethod
    def _sanitize(payload: dict[str, Any]) -> dict[str, Any]:
        """坏状态收敛：未知 status 视为 interrupted（可重试）。"""
        if payload.get("status") not in STUDIO_TASK_STATUSES:
            payload["status"] = "interrupted"
        return payload

    # ---- 事件流 ----

    def append_event(self, task_id: str, payload: dict[str, Any]) -> int:
        """追加事件（分配单调 seq）。终态事件类型按契约校验，非法直接拒。"""
        kind = str(payload.get("type") or "")
        if kind not in STUDIO_TASK_EVENT_TYPES:
            raise StudioStoreError(f"未知 Studio 事件类型：{kind}")
        with self._lock:
            seq, _ = self._last_seq(task_id)
            row = {
                "seq": seq + 1,
                "created_at": now_ms(),
                **payload,
            }
            path = self._events_path(task_id)
            self._dir.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return seq + 1

    def read_events(self, task_id: str, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        path = self._events_path(task_id)
        if not path.exists():
            return rows
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 断电半行：跳过，不回滚
                if int(row.get("seq") or 0) > int(after or 0):
                    rows.append(row)
                if len(rows) >= limit:
                    break
        return rows

    def _last_seq(self, task_id: str) -> tuple[int, bool]:
        """(已写最大 seq, 是否有终态事件)。"""

        path = self._events_path(task_id)
        if not path.exists():
            return 0, False
        last = 0
        has_terminal = False
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                last = max(last, int(row.get("seq") or 0))
                if str(row.get("type") or "") in STUDIO_TERMINAL_TASK_EVENT_TYPES:
                    has_terminal = True
        return last, has_terminal

    def terminal_event_seq(self, task_id: str) -> int:
        with self._lock:
            seq, _ = self._last_seq(task_id)
        return seq

    def has_terminal_event(self, task_id: str) -> bool:
        with self._lock:
            _, has = self._last_seq(task_id)
        return has

    # ---- 列表与恢复 ----

    def list_tasks(self, active_only: bool = False, limit: int = 60) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if self._dir.exists():
            for path in sorted(self._dir.glob("st-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict) or not str(payload.get("id") or "").startswith("st-"):
                    continue
                payload = self._sanitize(payload)
                if active_only and payload["status"] not in {"queued", "running"}:
                    continue
                rows.append(payload)
                if len(rows) >= limit:
                    break
        return rows

    def partial_from_events(self, task_id: str) -> str:
        """从事件流重建已产出的正文（重启恢复/重试展示用）。"""
        text_chunks: list[str] = []
        for row in self.read_events(task_id, after=0, limit=100000):
            if str(row.get("type") or "") == "delta" and row.get("content"):
                text_chunks.append(str(row["content"]))
            elif str(row.get("type") or "") == "done":
                break
        return "".join(text_chunks)

    # ---- 引用保护（缓存清理） ----

    def referenced_upload_paths(self, ttl: float = 2.0) -> set[str]:
        """任务里引用的上传图片路径键（主图 + 缩略图）。

        任务仍可查看/重试期间，图片不能被通用缓存清理误删；调用方（app 的
        ``_upload_path_in_use`` / ``_referenced_cache_keys``）只读本方法。
        """
        from naiba.studio.images import referenced_upload_paths

        now = time.monotonic()
        with self._lock:
            cached = self._ref_cache
            if cached and now - cached[0] < ttl:
                return set(cached[1])
        records: list[dict[str, Any]] = []
        if self._dir.exists():
            for path in sorted(self._dir.glob("st-*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict):
                    records.append(payload)
        keys = referenced_upload_paths(records)
        with self._lock:
            self._ref_cache = (now, set(keys))
        return set(keys)

    def invalidate_reference_cache(self) -> None:
        """任务新增/改写后立刻失效（创建任务时调用，避免刚提交的图被旧缓存误判）。"""
        with self._lock:
            self._ref_cache = None
