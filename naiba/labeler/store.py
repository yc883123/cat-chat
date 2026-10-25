"""打标器任务外层记录与事件流：``data/labeler/tasks/<id>.json`` + ``<id>.events.jsonl``。

与 Studio 的 store 同构，但**状态机与字段是打标器自己的**：
- 任务 JSON 保存输入快照（受管图片引用 + 白名单 config）与展示用进度/结果；
- 逐图完成/失败状态的**权威在 SQLite**（``naiba/labeler/db.py``），事件流只是通知日志，
  损坏时可从数据库重建展示，绝不反向覆盖数据库事实；
- 同一目录里 ``tg-*.json`` 才是任务记录，临时文件用点号前缀，崩溃残留不会误读。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from naiba.core.paths import normalized_path_key
from naiba.labeler import (
    LABELER_ACTIVE_STATUSES,
    LABELER_EVENT_TYPES,
    LABELER_MODULE,
    LABELER_ORIGIN_LABEL,
    LABELER_RESUMABLE_STATUSES,
    LABELER_TASK_STATUSES,
)

# 缓存引用保护要覆盖的状态：活跃 + 还能「继续未完成/重试」的。
PROTECTED_STATUSES: tuple[str, ...] = tuple(
    dict.fromkeys((*LABELER_ACTIVE_STATUSES, *LABELER_RESUMABLE_STATUSES))
)


class LabelerStoreError(ValueError):
    """任务数据非法。"""


def now_ms() -> int:
    return int(time.time() * 1000)


class LabelerStore:
    """线程安全（锁内写 JSON / 追加事件）。"""

    def __init__(self, data_dir: Path):
        self._dir = Path(data_dir) / "labeler" / "tasks"
        self._lock = threading.RLock()

    # ---- 路径 ----

    def _record_path(self, task_id: str) -> Path:
        return self._dir / f"{task_id}.json"

    def _events_path(self, task_id: str) -> Path:
        return self._dir / f"{task_id}.events.jsonl"

    def new_task_id(self) -> str:
        return f"tg-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:12]}"

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{uuid.uuid4().hex}.tmp")
        try:
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            # Windows 上目标文件正被读取时 os.replace 会抛 PermissionError（共享冲突）。
            # 界面在轮询任务详情，这种并发读是常态，短暂退避重试即可，不能让它把
            # 正在跑的任务打成 failed。
            for attempt in range(6):
                try:
                    os.replace(tmp_path, path)
                    return
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.02 * (attempt + 1))
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    # ---- 任务记录 ----

    def create_task(self, input_data: dict[str, Any], *, source_task_id: str = "") -> dict[str, Any]:
        task_id = self.new_task_id()
        stamp = now_ms()
        total = len((input_data or {}).get("image_refs") or [])
        record = {
            "id": task_id,
            "status": "queued",
            "module": LABELER_MODULE,
            "origin_label": LABELER_ORIGIN_LABEL,
            "detail": "",
            "input": input_data,
            "progress": {"done": 0, "total": total, "failed": 0, "uncertain": 0, "cancelled": 0},
            "result": {"annotation_ids": []},
            "error": "",
            "usage": {},
            "source_task_id": str(source_task_id or ""),
            "created_at": stamp,
            "updated_at": stamp,
            "started_at": None,
            "finished_at": None,
        }
        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._write_json(self._record_path(task_id), record)
            self._events_path(task_id).touch(exist_ok=True)
        return record

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        task_id = str(task_id or "").strip()
        if not task_id:
            return None
        text = ""
        for attempt in range(4):
            try:
                text = self._record_path(task_id).read_text(encoding="utf-8")
                break
            except FileNotFoundError:
                return None
            except (OSError, UnicodeDecodeError):
                # 与写入端 os.replace 撞车时也会走到这里：退避重读，别把任务当不存在。
                if attempt == 3:
                    return None
                time.sleep(0.02 * (attempt + 1))
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return self._sanitize(payload)

    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "status", "detail", "progress", "result", "error", "usage",
            "started_at", "finished_at",
        }
        with self._lock:
            current = self.get_task(task_id)
            if current is None:
                return None
            for key, value in fields.items():
                if key in allowed:
                    current[key] = value
            current["updated_at"] = now_ms()
            self._write_json(self._record_path(task_id), current)
        return current

    def delete_task(self, task_id: str) -> bool:
        """删除任务记录与事件文件（2026-10-26 删除计划 §3）。

        标注库不受影响：标注按「内容 + 配置指纹」全局复用，删除任务后仍可
        查询与导出。只认 ``tg-`` 前缀 id，避免误删目录里其他文件。
        """
        task_id = str(task_id or "").strip()
        if not task_id.startswith("tg-"):
            return False
        removed = False
        with self._lock:
            for path in (self._record_path(task_id), self._events_path(task_id)):
                for attempt in range(4):
                    try:
                        path.unlink(missing_ok=True)
                        removed = True
                        break
                    except PermissionError:
                        # 界面轮询详情时的共享冲突：短退避重试（同 _write_json 的口径）。
                        if attempt == 3:
                            raise
                        time.sleep(0.02 * (attempt + 1))
        return removed

    @staticmethod
    def _sanitize(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("status") not in LABELER_TASK_STATUSES:
            payload["status"] = "interrupted"
        payload.setdefault("module", LABELER_MODULE)
        payload.setdefault("origin_label", LABELER_ORIGIN_LABEL)
        payload.setdefault("progress", {"done": 0, "total": 0, "failed": 0, "uncertain": 0, "cancelled": 0})
        payload.setdefault("result", {"annotation_ids": []})
        return payload

    def list_tasks(self, active_only: bool = False, limit: int = 60) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self._dir.exists():
            return rows
        paths = sorted(self._dir.glob("tg-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or not str(payload.get("id") or "").startswith("tg-"):
                continue
            payload = self._sanitize(payload)
            if active_only and payload["status"] not in LABELER_ACTIVE_STATUSES:
                continue
            rows.append(payload)
            if len(rows) >= limit:
                break
        return rows

    def active_task_ids(self) -> list[str]:
        """缓存引用保护范围：活跃 + 可继续/重试的任务。"""
        return [
            str(task.get("id") or "")
            for task in self.list_tasks(limit=5000)
            if str(task.get("status") or "") in PROTECTED_STATUSES
        ]

    def referenced_upload_paths(self) -> set[str]:
        keys: set[str] = set()
        for task in self.list_tasks(limit=5000):
            if str(task.get("status") or "") not in PROTECTED_STATUSES:
                continue
            input_data = task.get("input") if isinstance(task.get("input"), dict) else {}
            for ref in input_data.get("image_refs") or []:
                if not isinstance(ref, dict):
                    continue
                for field in ("path", "thumb_path"):
                    text = str(ref.get(field) or "").strip()
                    if not text:
                        continue
                    key = normalized_path_key(Path(text).expanduser())
                    if key:
                        keys.add(key)
        return keys

    # ---- 事件流 ----

    def append_event(self, task_id: str, payload: dict[str, Any]) -> int:
        kind = str(payload.get("type") or "")
        if kind not in LABELER_EVENT_TYPES:
            raise LabelerStoreError(f"未知打标器事件类型：{kind}")
        with self._lock:
            seq = self._last_seq(task_id) + 1
            row = {"seq": seq, "created_at": now_ms(), **payload}
            path = self._events_path(task_id)
            self._dir.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return seq

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

    def _last_seq(self, task_id: str) -> int:
        path = self._events_path(task_id)
        if not path.exists():
            return 0
        last = 0
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
        return last

    def terminal_event_seq(self, task_id: str) -> int:
        return self._last_seq(task_id)
