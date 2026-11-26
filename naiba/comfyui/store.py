"""造梦机存储：工作流库（``data/comfyui/workflows/``）+ 任务记录（``data/comfyui/tasks/``）。

与打标器/Studio 的 store 同构（原子写 = 临时文件 + os.replace；事件 JSONL 是可重建的
通知日志，恢复一律以任务 JSON 的 checkpoint 为准）。工作流不进 SQLite：没有标注库
那种查询需求，按 id 的 JSON 文件足够。

任务 JSON 保存工作流**快照**（prompt 副本 + 名称），所以删除工作流不破坏历史任务
（计划 §12 风险 4 的处理方式）；checkpoint（prompt_ids / completed / errors /
resubmits）在 update_task 白名单里，runner 逐段原子推进。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from naiba.comfyui import (
    COMFYUI_ACTIVE_STATUSES,
    COMFYUI_EVENT_TYPES,
    COMFYUI_MODULE,
    COMFYUI_ORIGIN_LABEL,
    COMFYUI_RESUMABLE_STATUSES,
    COMFYUI_TASK_PREFIX,
    COMFYUI_TASK_STATUSES,
    COMFYUI_WORKFLOW_PREFIX,
)
from naiba.core.contracts import (
    COMFYUI_OUTPUT_DIR_CLASSIFY_PENDING_KEY,
    COMFYUI_OUTPUT_DIR_KEY,
    COMFYUI_OUTPUT_DIR_MOVE_KEY,
    COMFYUI_OUTPUT_LAYOUT_MANAGED_VERSION,
    COMFYUI_OUTPUT_LAYOUT_VERSION_KEY,
)
from naiba.core.media_types import media_kind_of
from naiba.core.paths import path_within
from naiba.labeler.store import now_ms
from naiba.comfyui.history import HistoryIndex, history_page

logger = logging.getLogger(__name__)

_OUTPUT_KIND_PREFIX: dict[str, str] = {
    "image": "图片",
    "video": "视频",
    "audio": "音频",
}
_OUTPUT_MIXED_LABEL = "混合"
_OUTPUT_OTHER_LABEL = "其他"
_INVALID_DIR_CHARS = frozenset('<>:"|?*')


def _output_date_stamp(created_at: Any) -> str:
    try:
        ms = int(created_at or 0)
    except (TypeError, ValueError):
        ms = 0
    if ms <= 0:
        return datetime.now().strftime("%Y%m%d")
    seconds = ms / 1000.0
    try:
        return datetime.fromtimestamp(seconds).strftime("%Y%m%d")
    except (OSError, OverflowError, ValueError):
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y%m%d")

# 缓存引用保护要覆盖的状态（本期造梦机不引用 uploads，P5 图片输入接入后生效）。
PROTECTED_STATUSES: tuple[str, ...] = tuple(
    dict.fromkeys((*COMFYUI_ACTIVE_STATUSES, *COMFYUI_RESUMABLE_STATUSES))
)
# Legacy callers request the most recent page; records are never pruned by count.
MAX_TASKS = 60


class ComfyUIStoreError(ValueError):
    """工作流/任务数据非法。"""


# Multiple service/store instances in one process share the receipt creation lock.
_STORE_LOCKS: dict[str, Any] = {}
_STORE_LOCKS_GUARD = threading.Lock()


def _store_lock(directory: Path) -> Any:
    key = os.path.normcase(os.path.abspath(directory))
    with _STORE_LOCKS_GUARD:
        return _STORE_LOCKS.setdefault(key, threading.RLock())


class ComfyUIStore:
    """线程安全（锁内写 JSON / 追加事件）。"""

    def __init__(self, data_dir: Path):
        root = Path(data_dir) / "comfyui"
        self._workflows_dir = root / "workflows"
        self._tasks_dir = root / "tasks"
        self._outputs_dir = root / "outputs"
        self._lock = _store_lock(self._tasks_dir)
        self._history_index = HistoryIndex(self._tasks_dir, COMFYUI_TASK_PREFIX, self._lock, tasks=True)

    # ---- 路径 ----

    @property
    def outputs_dir(self) -> Path:
        return self._outputs_dir

    def workflow_path(self, workflow_id: str) -> Path:
        return self._workflows_dir / f"{workflow_id}.json"

    def task_record_path(self, task_id: str) -> Path:
        return self._tasks_dir / f"{task_id}.json"

    def task_events_path(self, task_id: str) -> Path:
        return self._tasks_dir / f"{task_id}.events.jsonl"

    def task_output_dir(self, task_or_id: str | dict[str, Any]) -> Path:
        return self.resolve_task_output_dir(task_or_id)

    @staticmethod
    def sanitize_output_dir_name(value: Any) -> str:
        """只接受 outputs_dir 下的单个合法子目录名，拒绝路径分隔与穿越。"""
        if not isinstance(value, str):
            return ""
        name = value.strip()
        if not name or name != value or name.endswith((".", " ")):
            return ""
        if name in {".", ".."} or "\x00" in name or "/" in name or "\\" in name:
            return ""
        if any(ch in _INVALID_DIR_CHARS for ch in name):
            return ""
        if Path(name).name != name or os.path.isabs(name):
            return ""
        return name

    def validate_output_dir_name(self, value: Any) -> str:
        name = self.sanitize_output_dir_name(value)
        if not name:
            raise ComfyUIStoreError(f"非法的任务产物目录名：{value!r}")
        target = (self._outputs_dir / name).resolve()
        if not path_within(target, self._outputs_dir.resolve()):
            raise ComfyUIStoreError(f"任务产物目录越界：{value!r}")
        return name

    @classmethod
    def _normalize_output_dir_move(cls, value: Any) -> dict[str, str] | None:
        if not isinstance(value, dict):
            return None
        from_name = cls.sanitize_output_dir_name(value.get("from"))
        to_name = cls.sanitize_output_dir_name(value.get("to"))
        if not from_name or not to_name or from_name == to_name:
            return None
        return {"from": from_name, "to": to_name}

    @classmethod
    def is_managed_output_task(cls, task: dict[str, Any] | None) -> bool:
        if not isinstance(task, dict):
            return False
        try:
            version = int(task.get(COMFYUI_OUTPUT_LAYOUT_VERSION_KEY) or 1)
        except (TypeError, ValueError):
            version = 1
        if version >= COMFYUI_OUTPUT_LAYOUT_MANAGED_VERSION:
            return True
        return bool(cls.sanitize_output_dir_name(task.get(COMFYUI_OUTPUT_DIR_KEY)))

    @classmethod
    def is_output_dir_mutable(cls, task: dict[str, Any] | None) -> bool:
        """运行中或存在未完成迁移意图的托管任务，其目录仍可能因混出而重命名。"""
        if not cls.is_managed_output_task(task):
            return False
        status = str((task or {}).get("status") or "")
        return (
            status in COMFYUI_ACTIVE_STATUSES
            or cls._normalize_output_dir_move((task or {}).get(COMFYUI_OUTPUT_DIR_MOVE_KEY)) is not None
        )

    @classmethod
    def classify_output_media_label(cls, filenames: Iterable[str]) -> str:
        recognized: set[str] = set()
        for name in filenames:
            text = str(name or "").strip()
            if not text:
                continue
            kind = media_kind_of(text)
            if kind in _OUTPUT_KIND_PREFIX:
                recognized.add(kind)
        if len(recognized) >= 2:
            return _OUTPUT_MIXED_LABEL
        if len(recognized) == 1:
            return _OUTPUT_KIND_PREFIX[next(iter(recognized))]
        return _OUTPUT_OTHER_LABEL

    @classmethod
    def format_task_output_dir_name(cls, task: dict[str, Any], filenames: Iterable[str]) -> str:
        task_id = cls.sanitize_output_dir_name(str((task or {}).get("id") or "").strip())
        if not task_id:
            raise ComfyUIStoreError("无效任务 ID，无法生成产物目录名")
        input_data = task.get("input") if isinstance(task.get("input"), dict) else {}
        prefix = "批量" if str(input_data.get("batch_id") or "").strip() else "单次"
        kind_label = cls.classify_output_media_label(filenames)
        stamp = _output_date_stamp(task.get("created_at"))
        name = f"{prefix}{kind_label}_{stamp}_{task_id}"
        if not cls.sanitize_output_dir_name(name):
            raise ComfyUIStoreError(f"非法的任务产物目录名：{name}")
        return name

    @staticmethod
    def _recorded_task_output_names(task: dict[str, Any]) -> list[str]:
        names: list[str] = []
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        for item in result.get("outputs") or []:
            text = str(item or "").strip()
            if text and text not in names:
                names.append(text)
        checkpoint = task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {}
        for done in checkpoint.get("completed") or []:
            if not isinstance(done, dict):
                continue
            for item in done.get("files") or []:
                text = str(item or "").strip()
                if text and text not in names:
                    names.append(text)
        return names

    def _reconcile_move_intent(self, task: dict[str, Any]) -> dict[str, Any]:
        """对未清除的目录迁移意图做最小双目录核验修复（不扫描根目录）。"""
        move = self._normalize_output_dir_move(task.get(COMFYUI_OUTPUT_DIR_MOVE_KEY))
        if move is None:
            return task
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            return task
        with self._lock:
            latest = self.get_task(task_id)
            if latest is None:
                return task
            move = self._normalize_output_dir_move(latest.get(COMFYUI_OUTPUT_DIR_MOVE_KEY))
            if move is None:
                return latest
            old_name, new_name = move["from"], move["to"]
            old_dir = self._outputs_dir / old_name
            new_dir = self._outputs_dir / new_name
            old_exists = old_dir.is_dir()
            new_exists = new_dir.is_dir()
            if new_exists and not old_exists:
                latest[COMFYUI_OUTPUT_DIR_KEY] = new_name
                latest.pop(COMFYUI_OUTPUT_DIR_MOVE_KEY, None)
                latest.pop(COMFYUI_OUTPUT_DIR_CLASSIFY_PENDING_KEY, None)
            elif old_exists and not new_exists:
                latest[COMFYUI_OUTPUT_DIR_KEY] = old_name
                latest.pop(COMFYUI_OUTPUT_DIR_MOVE_KEY, None)
            elif old_exists and new_exists:
                old_files = [p for p in old_dir.iterdir() if not p.name.startswith(".")]
                new_files = [p for p in new_dir.iterdir() if not p.name.startswith(".")]
                if not old_files and new_files:
                    try:
                        old_dir.rmdir()
                        latest[COMFYUI_OUTPUT_DIR_KEY] = new_name
                        latest.pop(COMFYUI_OUTPUT_DIR_MOVE_KEY, None)
                        latest.pop(COMFYUI_OUTPUT_DIR_CLASSIFY_PENDING_KEY, None)
                    except OSError:
                        latest[COMFYUI_OUTPUT_DIR_KEY] = old_name
                        latest.pop(COMFYUI_OUTPUT_DIR_MOVE_KEY, None)
                        latest[COMFYUI_OUTPUT_DIR_CLASSIFY_PENDING_KEY] = new_name
                else:
                    latest[COMFYUI_OUTPUT_DIR_KEY] = old_name
                    latest.pop(COMFYUI_OUTPUT_DIR_MOVE_KEY, None)
                    latest[COMFYUI_OUTPUT_DIR_CLASSIFY_PENDING_KEY] = new_name
            else:
                latest.pop(COMFYUI_OUTPUT_DIR_MOVE_KEY, None)
            latest["updated_at"] = now_ms()
            self._write_json(self.task_record_path(task_id), latest)
            return latest

    def resolve_task_output_dir(self, task_or_id: str | dict[str, Any], *, reconcile: bool = True) -> Path:
        if isinstance(task_or_id, dict):
            task: dict[str, Any] | None = task_or_id
            tid = str(task.get("id") or "").strip()
        else:
            tid = str(task_or_id or "").strip()
            task = self.get_task(tid) if tid else None
        if reconcile and isinstance(task, dict) and task.get(COMFYUI_OUTPUT_DIR_MOVE_KEY) is not None:
            task = self._reconcile_move_intent(task)
        if isinstance(task, dict):
            folder = self.sanitize_output_dir_name(task.get(COMFYUI_OUTPUT_DIR_KEY))
            if folder:
                return self._outputs_dir / folder
        safe_tid = self.validate_output_dir_name(tid)
        return self._outputs_dir / safe_tid

    def _transition_output_dir(
        self, task: dict[str, Any], *, current_folder: str, target_folder: str
    ) -> Path:
        """持久化迁移意图 -> 重命名目录 -> 提交新指针并清意图；被占用时回保旧目录。"""
        task_id = str(task.get("id") or "")
        current_name = self.validate_output_dir_name(current_folder)
        target_name = self.validate_output_dir_name(target_folder)
        old_dir = self._outputs_dir / current_name
        new_dir = self._outputs_dir / target_name
        task[COMFYUI_OUTPUT_DIR_MOVE_KEY] = {"from": current_name, "to": target_name}
        task["updated_at"] = now_ms()
        self._write_json(self.task_record_path(task_id), task)
        try:
            if new_dir.is_dir():
                if any(new_dir.iterdir()):
                    raise FileExistsError(f"目标产物目录已存在且非空：{target_name}")
                new_dir.rmdir()
            old_dir.rename(new_dir)
        except OSError as exc:
            logger.warning(
                "造梦机产物目录重命名失败，保留原目录：task=%s %s -> %s error=%s",
                task_id, current_name, target_name, exc,
            )
            if task.get(COMFYUI_OUTPUT_DIR_KEY):
                task[COMFYUI_OUTPUT_DIR_KEY] = current_name
            task.pop(COMFYUI_OUTPUT_DIR_MOVE_KEY, None)
            task[COMFYUI_OUTPUT_DIR_CLASSIFY_PENDING_KEY] = target_name
            task["updated_at"] = now_ms()
            self._write_json(self.task_record_path(task_id), task)
            old_dir.mkdir(parents=True, exist_ok=True)
            return old_dir
        task[COMFYUI_OUTPUT_DIR_KEY] = target_name
        task.pop(COMFYUI_OUTPUT_DIR_MOVE_KEY, None)
        task.pop(COMFYUI_OUTPUT_DIR_CLASSIFY_PENDING_KEY, None)
        task["updated_at"] = now_ms()
        self._write_json(self.task_record_path(task_id), task)
        return new_dir

    def prepare_task_output_dir(self, task_id: str, incoming_filenames: Iterable[str]) -> Path:
        """仅由执行器在落盘前调用：分配首次可读目录或升级混合目录。"""
        with self._lock:
            task = self.get_task(task_id)
            if task is None:
                raise ComfyUIStoreError(f"任务记录不存在：{task_id}")
            if task.get(COMFYUI_OUTPUT_DIR_MOVE_KEY) is not None:
                task = self._reconcile_move_intent(task)
            safe_tid = self.validate_output_dir_name(task_id)
            if not self.is_managed_output_task(task):
                legacy_dir = self._outputs_dir / safe_tid
                legacy_dir.mkdir(parents=True, exist_ok=True)
                return legacy_dir
            incoming = [str(name) for name in incoming_filenames if str(name or "").strip()]
            current_name = self.sanitize_output_dir_name(task.get(COMFYUI_OUTPUT_DIR_KEY))
            fallback_dir = self._outputs_dir / safe_tid
            active_dir: Path | None = (
                self._outputs_dir / current_name if current_name
                else fallback_dir if fallback_dir.is_dir() else None
            )
            all_names = self._recorded_task_output_names(task)
            if active_dir is not None and active_dir.is_dir():
                for item in active_dir.iterdir():
                    if item.is_file() and not item.name.startswith(".") and item.name not in all_names:
                        all_names.append(item.name)
            for name in incoming:
                if name not in all_names:
                    all_names.append(name)
            desired_name = self.format_task_output_dir_name(task, all_names)
            target_dir = self._outputs_dir / desired_name
            if not current_name:
                if active_dir is not None and active_dir.is_dir() and active_dir.name != desired_name:
                    return self._transition_output_dir(
                        task, current_folder=active_dir.name, target_folder=desired_name
                    )
                target_dir.mkdir(parents=True, exist_ok=True)
                task[COMFYUI_OUTPUT_DIR_KEY] = desired_name
                task.pop(COMFYUI_OUTPUT_DIR_CLASSIFY_PENDING_KEY, None)
                task["updated_at"] = now_ms()
                self._write_json(self.task_record_path(task_id), task)
                return target_dir
            if current_name == desired_name:
                target_dir.mkdir(parents=True, exist_ok=True)
                if COMFYUI_OUTPUT_DIR_CLASSIFY_PENDING_KEY in task:
                    task.pop(COMFYUI_OUTPUT_DIR_CLASSIFY_PENDING_KEY, None)
                    task["updated_at"] = now_ms()
                    self._write_json(self.task_record_path(task_id), task)
                return target_dir
            current_dir = self._outputs_dir / current_name
            if not current_dir.exists():
                target_dir.mkdir(parents=True, exist_ok=True)
                task[COMFYUI_OUTPUT_DIR_KEY] = desired_name
                task.pop(COMFYUI_OUTPUT_DIR_CLASSIFY_PENDING_KEY, None)
                task["updated_at"] = now_ms()
                self._write_json(self.task_record_path(task_id), task)
                return target_dir
            return self._transition_output_dir(
                task, current_folder=current_name, target_folder=desired_name
            )

    def new_workflow_id(self) -> str:
        return f"{COMFYUI_WORKFLOW_PREFIX}-{uuid.uuid4().hex[:12]}"

    def new_task_id(self) -> str:
        return f"{COMFYUI_TASK_PREFIX}-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{uuid.uuid4().hex}.tmp")
        try:
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                encoding="utf-8")
            # Windows 上目标正被读取时 os.replace 抛共享冲突：短暂退避重试。
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

    # ---- 工作流库 ----

    def save_workflow(self, entry: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._write_json(self.workflow_path(str(entry["id"])), entry)
        return entry

    def save_workflow_as(self, source_id: str, fingerprint: str, entry: dict[str, Any]) -> bool:
        """Commit a new immutable fork only while the original graph still matches.

        Shares the workflow mutation lock with save/delete, closing the final
        fingerprint-check/write race without holding a disk lock during schema HTTP.
        """
        with self._lock:
            source = self.get_workflow(source_id)
            if source is None or self.payload_hash(source.get("prompt") or {}) != fingerprint:
                return False
            if str(entry["id"]) == source_id or self.workflow_path(str(entry["id"])).exists():
                raise ComfyUIStoreError("另存必须使用新的工作流 id")
            self._write_json(self.workflow_path(str(entry["id"])), entry)
            return True

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        workflow_id = str(workflow_id or "").strip()
        if not workflow_id or "/" in workflow_id or "\\" in workflow_id:
            return None
        try:
            payload = json.loads(self.workflow_path(workflow_id).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def list_workflows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self._workflows_dir.exists():
            return rows
        paths = sorted(self._workflows_dir.glob(f"{COMFYUI_WORKFLOW_PREFIX}-*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or not str(payload.get("id") or ""):
                continue
            # 列表不带 prompt 本体（详情才有）：列表只给摘要。
            rows.append({
                "id": payload.get("id"),
                "name": payload.get("name") or "",
                "node_count": len(payload.get("prompt") or {}),
                "param_count": len(payload.get("params") or []),
                "created_at": payload.get("created_at") or 0,
                "updated_at": payload.get("updated_at") or 0,
            })
        return rows

    def delete_workflow(self, workflow_id: str) -> bool:
        with self._lock:
            path = self.workflow_path(workflow_id)
            if not path.is_file():
                return False
            path.unlink(missing_ok=True)
            # 同名临时残留（崩溃遗留）一并清。
            for leftover in self._workflows_dir.glob(f".{workflow_id}.json*.tmp"):
                leftover.unlink(missing_ok=True)
        return True

    # ---- 任务记录 ----

    @staticmethod
    def payload_hash(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()

    def lookup_request(self, request_key: str, *, owner_id: str = "") -> dict[str, Any] | None:
        if not request_key:
            return None
        with self._lock:
            for path in self._tasks_dir.glob(f"{COMFYUI_TASK_PREFIX}-*.json"):
                task = self.get_task(path.stem)
                if not task:
                    continue
                receipts = [task, *(task.get("request_receipts") or [])]
                if any(r.get("request_key") == request_key and r.get("owner_id", "") == owner_id
                       for r in receipts if isinstance(r, dict)):
                    return task
        return None

    def create_task(self, input_data: dict[str, Any], *, source_task_id: str = "",
                    request_key: str = "", owner_id: str = "") -> dict[str, Any]:
        frozen = json.loads(json.dumps(input_data, ensure_ascii=False, allow_nan=False))
        digest = self.payload_hash({"input": frozen, "source_task_id": source_task_id})
        with self._lock:
            existing = self.lookup_request(request_key, owner_id=owner_id)
            if existing:
                if existing.get("request_key") != request_key or existing.get("payload_hash") != digest:
                    raise ComfyUIStoreError("request_key 已绑定不同的生成输入")
                return existing
            return self._create_task(frozen, source_task_id=source_task_id,
                                     request_key=request_key, owner_id=owner_id,
                                     payload_hash=digest)

    def _create_task(self, input_data: dict[str, Any], *, source_task_id: str = "",
                     **receipt: Any) -> dict[str, Any]:
        task_id = self.new_task_id()
        stamp = now_ms()
        shots = int((input_data or {}).get("shots") or 1)
        record = {
            **receipt,
            "id": task_id,
            "status": "queued",
            "module": COMFYUI_MODULE,
            "origin_label": COMFYUI_ORIGIN_LABEL,
            "detail": "",
            "input": input_data,
            "progress": {"done": 0, "total": shots, "failed": 0},
            "result": {"completed": [], "outputs": []},
            "error": "",
            "usage": {},
            "source_task_id": str(source_task_id or ""),
            COMFYUI_OUTPUT_LAYOUT_VERSION_KEY: COMFYUI_OUTPUT_LAYOUT_MANAGED_VERSION,
            COMFYUI_OUTPUT_DIR_KEY: "",
            # checkpoint：runner 唯一的恢复权威（事件流只是通知）。
            "checkpoint": {"prompt_ids": [], "completed": [], "errors": [], "resubmits": {}},
            "created_at": stamp,
            "updated_at": stamp,
            "started_at": None,
            "finished_at": None,
            # 重试过的任务卡片显示最近一轮开始时刻而非创建时刻，靠它区分。
            "retry_count": 0,
        }
        with self._lock:
            self._write_json(self.task_record_path(task_id), record)
            self.task_events_path(task_id).touch(exist_ok=True)
        return record

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        task_id = str(task_id or "").strip()
        if not task_id or "/" in task_id or "\\" in task_id:
            return None
        text = ""
        for attempt in range(4):
            try:
                text = self.task_record_path(task_id).read_text(encoding="utf-8")
                break
            except FileNotFoundError:
                return None
            except (OSError, UnicodeDecodeError):
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

    @staticmethod
    def _sanitize(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("status") not in COMFYUI_TASK_STATUSES:
            payload["status"] = "interrupted"
        payload.setdefault("module", COMFYUI_MODULE)
        payload.setdefault("origin_label", COMFYUI_ORIGIN_LABEL)
        payload.setdefault("progress", {"done": 0, "total": 0, "failed": 0})
        payload.setdefault("result", {"completed": [], "outputs": []})
        checkpoint = payload.get("checkpoint")
        payload["checkpoint"] = checkpoint if isinstance(checkpoint, dict) else {
            "prompt_ids": [], "completed": [], "errors": [], "resubmits": {},
        }
        payload.setdefault("retry_count", 0)
        if COMFYUI_OUTPUT_DIR_KEY in payload:
            payload[COMFYUI_OUTPUT_DIR_KEY] = ComfyUIStore.sanitize_output_dir_name(
                payload.get(COMFYUI_OUTPUT_DIR_KEY)
            )
        if COMFYUI_OUTPUT_DIR_MOVE_KEY in payload:
            move = ComfyUIStore._normalize_output_dir_move(
                payload.get(COMFYUI_OUTPUT_DIR_MOVE_KEY)
            )
            if move is None:
                payload.pop(COMFYUI_OUTPUT_DIR_MOVE_KEY, None)
            else:
                payload[COMFYUI_OUTPUT_DIR_MOVE_KEY] = move
        if COMFYUI_OUTPUT_DIR_CLASSIFY_PENDING_KEY in payload:
            pending = ComfyUIStore.sanitize_output_dir_name(
                payload.get(COMFYUI_OUTPUT_DIR_CLASSIFY_PENDING_KEY)
            )
            if pending:
                payload[COMFYUI_OUTPUT_DIR_CLASSIFY_PENDING_KEY] = pending
            else:
                payload.pop(COMFYUI_OUTPUT_DIR_CLASSIFY_PENDING_KEY, None)
        return payload

    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "status", "detail", "progress", "result", "error", "usage",
            "started_at", "finished_at", "checkpoint", "request_receipts",
            "retry_count",
        }
        with self._lock:
            current = self.get_task(task_id)
            if current is None:
                return None
            for key, value in fields.items():
                if key in allowed:
                    current[key] = value
            current["updated_at"] = now_ms()
            self._write_json(self.task_record_path(task_id), current)
        return current

    def task_page(self, page_size: Any = None, cursor: Any = "", *,
                  scope: str = "all", active_only: bool = False) -> dict[str, Any]:
        if scope not in {"all", "single"}:
            raise ValueError("scope 必须是 single 或 all")

        def include(summary: dict[str, Any]) -> bool:
            return ((scope != "single" or not summary["batch"])
                    and (not active_only or summary["status"] in COMFYUI_ACTIVE_STATUSES))

        def still_include(task: dict[str, Any]) -> bool:
            source = task.get("input")
            return ((scope != "single" or not (isinstance(source, dict) and source.get("batch_id")))
                    and (not active_only or task["status"] in COMFYUI_ACTIVE_STATUSES))

        return history_page(self._history_index, page_size, cursor,
                            scope=f"tasks:{scope}:{int(active_only)}", load=self.get_task,
                            include=include, still_include=still_include)

    def iter_tasks(self, active_only: bool = False) -> Iterator[dict[str, Any]]:
        """Internal whole-history traversal, independent of UI pagination."""
        for summary in self._history_index.summaries():
            if active_only and summary["status"] not in COMFYUI_ACTIVE_STATUSES:
                continue
            task = self.get_task(summary["id"])
            if task is not None and (not active_only or task["status"] in COMFYUI_ACTIVE_STATUSES):
                yield task

    def list_tasks(self, active_only: bool = False, limit: int | None = MAX_TASKS,
                   scope: str = "all") -> list[dict[str, Any]]:
        if scope not in {"all", "single"}:
            raise ValueError("scope 必须是 single 或 all")
        rows: list[dict[str, Any]] = []
        if not self._tasks_dir.exists():
            return rows
        paths = sorted(self._tasks_dir.glob(f"{COMFYUI_TASK_PREFIX}-*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or not str(payload.get("id") or "").startswith(
                    f"{COMFYUI_TASK_PREFIX}-"):
                continue
            payload = self._sanitize(payload)
            if active_only and payload["status"] not in COMFYUI_ACTIVE_STATUSES:
                continue
            source = payload.get("input")
            if scope == "single" and isinstance(source, dict) and source.get("batch_id"):
                continue
            rows.append(payload)
            if limit is not None and len(rows) >= limit:
                break
        return rows

    def delete_task(self, task_id: str) -> bool:
        """先解析实际产物目录并删除，再删任务记录（目录删除失败时保留记录并上报异常）。"""
        safe_tid = self.sanitize_output_dir_name(str(task_id or "").strip())
        if not safe_tid:
            return False
        with self._lock:
            record_path = self.task_record_path(safe_tid)
            existed = record_path.is_file()
            task = self.get_task(safe_tid)
            candidates: list[Path] = []
            if isinstance(task, dict):
                folder = self.sanitize_output_dir_name(task.get(COMFYUI_OUTPUT_DIR_KEY))
                if folder:
                    candidates.append(self._outputs_dir / folder)
                move = self._normalize_output_dir_move(task.get(COMFYUI_OUTPUT_DIR_MOVE_KEY))
                if move is not None:
                    candidates.append(self._outputs_dir / move["from"])
                    candidates.append(self._outputs_dir / move["to"])
            candidates.append(self._outputs_dir / safe_tid)
            outputs_root = self._outputs_dir.resolve()
            seen: set[str] = set()
            for candidate in candidates:
                resolved = candidate.resolve()
                key = os.path.normcase(str(resolved))
                if key in seen:
                    continue
                seen.add(key)
                if not path_within(resolved, outputs_root) or resolved == outputs_root:
                    continue
                if candidate.is_dir():
                    try:
                        shutil.rmtree(candidate)
                    except OSError as exc:
                        logger.warning(
                            "删除造梦机产物目录失败，保留任务记录：task=%s dir=%s error=%s",
                            safe_tid, resolved, exc,
                        )
                        raise
            record_path.unlink(missing_ok=True)
            self.task_events_path(safe_tid).unlink(missing_ok=True)
        return existed

    # ---- 事件流 ----

    def append_event(self, task_id: str, payload: dict[str, Any]) -> int:
        kind = str(payload.get("type") or "")
        if kind not in COMFYUI_EVENT_TYPES:
            raise ComfyUIStoreError(f"未知造梦机事件类型：{kind}")
        with self._lock:
            seq = self._last_seq(task_id) + 1
            row = {"seq": seq, "created_at": now_ms(), **payload}
            path = self.task_events_path(task_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return seq

    def _last_seq(self, task_id: str) -> int:
        path = self.task_events_path(task_id)
        if not path.exists():
            return 0
        last = 0
        try:
            with path.open("rb") as handle:
                # 只看最后一个非空行（事件文件追加有序，不必全量解析）。
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                window = min(size, 8192)
                handle.seek(size - window)
                tail = handle.read().decode("utf-8", errors="replace")
            for line in reversed(tail.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    last = int(json.loads(line).get("seq") or 0)
                except (json.JSONDecodeError, ValueError):
                    break
                break
        except OSError:
            return 0
        return last

    def read_events(self, task_id: str, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        path = self.task_events_path(task_id)
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
