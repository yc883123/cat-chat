"""造梦机存储：工作流库（``data/comfyui/workflows/``）+ 任务记录（``data/comfyui/tasks/``）。

与打标器/Studio 的 store 同构（原子写 = 临时文件 + os.replace；事件 JSONL 是可重建的
通知日志，恢复一律以任务 JSON 的 checkpoint 为准）。工作流不进 SQLite：没有标注库
那种查询需求，按 id 的 JSON 文件足够。

任务 JSON 保存工作流**快照**（prompt 副本 + 名称），所以删除工作流不破坏历史任务
（计划 §12 风险 4 的处理方式）；checkpoint（prompt_ids / completed / errors /
resubmits）在 update_task 白名单里，runner 逐段原子推进。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

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
from naiba.labeler.store import now_ms

# 缓存引用保护要覆盖的状态（本期造梦机不引用 uploads，P5 图片输入接入后生效）。
PROTECTED_STATUSES: tuple[str, ...] = tuple(
    dict.fromkeys((*COMFYUI_ACTIVE_STATUSES, *COMFYUI_RESUMABLE_STATUSES))
)
# 任务历史上限（与工作台 60 条口径一致；批量删除清更早的）。
MAX_TASKS = 60


class ComfyUIStoreError(ValueError):
    """工作流/任务数据非法。"""


class ComfyUIStore:
    """线程安全（锁内写 JSON / 追加事件）。"""

    def __init__(self, data_dir: Path):
        root = Path(data_dir) / "comfyui"
        self._workflows_dir = root / "workflows"
        self._tasks_dir = root / "tasks"
        self._outputs_dir = root / "outputs"
        self._lock = threading.RLock()

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

    def task_output_dir(self, task_id: str) -> Path:
        return self._outputs_dir / task_id

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

    def create_task(self, input_data: dict[str, Any], *, source_task_id: str = "") -> dict[str, Any]:
        task_id = self.new_task_id()
        stamp = now_ms()
        shots = int((input_data or {}).get("shots") or 1)
        record = {
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
            # checkpoint：runner 唯一的恢复权威（事件流只是通知）。
            "checkpoint": {"prompt_ids": [], "completed": [], "errors": [], "resubmits": {}},
            "created_at": stamp,
            "updated_at": stamp,
            "started_at": None,
            "finished_at": None,
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
        return payload

    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "status", "detail", "progress", "result", "error", "usage",
            "started_at", "finished_at", "checkpoint",
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

    def list_tasks(self, active_only: bool = False, limit: int = MAX_TASKS) -> list[dict[str, Any]]:
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
            rows.append(payload)
            if len(rows) >= limit:
                break
        return rows

    def delete_task(self, task_id: str) -> bool:
        """删除一条任务记录与它的产物目录（调用方保证任务不在执行中）。"""
        with self._lock:
            record = self.task_record_path(task_id)
            existed = record.is_file()
            record.unlink(missing_ok=True)
            self.task_events_path(task_id).unlink(missing_ok=True)
        output_dir = self.task_output_dir(task_id)
        if output_dir.is_dir():
            import shutil

            shutil.rmtree(output_dir, ignore_errors=True)
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
