"""打标器域门面：``app.labeler.*`` 的调用面，返回 ``(payload, HTTPStatus)``。

HTTP 层只解析请求与转发；输入校验、状态机判定、导出落点都在这里与下层模块。
所有会写盘的路径都由服务端决定：接受的是记录 ID / 查询条件 / 格式，绝不接受
客户端提供的文件系统路径。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from naiba.labeler.db import LabelerDB, LabelerDBError
from naiba.labeler.export import LabelerExportError, write_export
from naiba.labeler.images import (
    MAX_BATCH_BYTES,
    MAX_BATCH_IMAGES,
    MAX_IMAGE_BYTES,
    LabelerImageError,
)
from naiba.labeler.jobs import LabelerJobManager, LabelerTaskError
from naiba.labeler.presets import preset_list
from naiba.labeler.runner import DEFAULT_CONCURRENCY, VALIDATED_CONCURRENCY, LabelerRunner
from naiba.labeler.sentences import sentence_stats
from naiba.labeler.store import LabelerStore

EXPORT_MAX_RECORDS = 5000


class LabelerService:
    def __init__(self, app: Any):
        self.app = app
        data_dir = Path(app.paths.data_dir)
        self.db = LabelerDB(data_dir)
        self.store = LabelerStore(data_dir)
        self.runner = LabelerRunner(app, self.store, self.db)
        self.jobs = LabelerJobManager(app, self.store, self.db, self.runner)
        self._exports_dir = data_dir / "labeler" / "exports"

    # ---- 生命周期 ----

    def recover(self) -> int:
        return self.jobs.recover_interrupted()

    def shutdown(self) -> None:
        self.runner.shutdown()

    def referenced_upload_paths(self) -> set[str]:
        """缓存清理的引用保护：活跃/可继续任务引用的图片。

        任务 JSON 里有完整引用；SQLite 里有相同集合。两处取并集，
        即使其中一个文件损坏也不会误删正在用的图（方向只会是「多留」）。
        """
        keys = self.store.referenced_upload_paths()
        try:
            from naiba.core.paths import normalized_path_key

            for path in self.db.referenced_paths(self.store.active_task_ids()):
                key = normalized_path_key(Path(path))
                if key:
                    keys.add(key)
        except (OSError, ValueError, LabelerDBError):
            pass
        return keys

    # ---- 配置 ----

    def api_presets(self) -> tuple[dict[str, Any], int]:
        return {
            "presets": preset_list(),
            "output_modes": [
                {"id": "keywords", "label": "只要标签"},
                {"id": "description", "label": "只要描述"},
                {"id": "both", "label": "标签 + 描述"},
            ],
            "limits": {
                "max_batch_images": MAX_BATCH_IMAGES,
                "max_image_bytes": MAX_IMAGE_BYTES,
                "max_batch_bytes": MAX_BATCH_BYTES,
                "default_concurrency": DEFAULT_CONCURRENCY,
                "validated_concurrency": list(VALIDATED_CONCURRENCY),
            },
        }, 200

    # ---- 任务 ----

    def api_tasks(self, active_only: str = "") -> tuple[dict[str, Any], int]:
        rows = self.jobs.tasks(active_only=str(active_only or "") in ("1", "true"))
        return {"tasks": rows}, 200

    def api_recent(self, limit: str = "5") -> tuple[dict[str, Any], int]:
        try:
            parsed = max(1, min(20, int(str(limit or "5"))))
        except (TypeError, ValueError):
            return {"error": "limit 必须是整数"}, 400
        return {"tasks": self.jobs.recent(limit=parsed)}, 200

    def api_task(self, task_id: str) -> tuple[dict[str, Any], int]:
        task = self.jobs.task(task_id)
        if task is None:
            return {"error": "任务不存在"}, 404
        return {"task": task}, 200

    def api_task_events(self, task_id: str, after: Any = 0) -> tuple[dict[str, Any], int]:
        task = self.store.get_task(task_id)
        if task is None:
            return {"error": "任务不存在"}, 404
        try:
            parsed_after = max(0, int(str(after or "0")))
        except (TypeError, ValueError):
            return {"error": "after 必须是非负整数"}, 400
        rows = self.jobs.events(task_id, after=parsed_after)
        last_seq = int(rows[-1]["seq"]) if rows else parsed_after
        return {
            "events": rows,
            "count": len(rows),
            "after": parsed_after,
            "last_seq": max(last_seq, parsed_after),
            "status": task.get("status"),
        }, 200

    def api_submit(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        try:
            task = self.jobs.submit(body)
        except LabelerImageError as exc:
            return {"error": str(exc)}, 400
        except LabelerTaskError as exc:
            return {"error": str(exc)}, 400
        except LabelerDBError as exc:
            return {"error": str(exc)}, 503
        detail = self.jobs.task(str(task.get("id") or "")) or task
        return {"task": detail}, 201

    def api_cancel(self, task_id: str) -> tuple[dict[str, Any], int]:
        task = self.store.get_task(task_id)
        if task is None:
            return {"error": "任务不存在"}, 404
        if str(task.get("status") or "") in {"done", "failed", "cancelled", "interrupted"}:
            return {"error": "任务已结束，无需取消"}, 409
        if not self.jobs.cancel(task_id):
            return {"error": "任务无法取消（可能已结束）"}, 409
        return {"ok": True}, 200

    def api_retry(self, task_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        body = body if isinstance(body, dict) else {}
        mode = str(body.get("mode") or "unfinished")
        confirm = bool(body.get("confirm_uncertain"))
        try:
            task = self.jobs.continue_task(task_id, mode=mode, confirm_uncertain=confirm)
        except LookupError:
            return {"error": "任务不存在"}, 404
        except (LabelerTaskError, LabelerImageError) as exc:
            return {"error": str(exc)}, 400
        except LabelerDBError as exc:
            return {"error": str(exc)}, 503
        detail = self.jobs.task(str(task.get("id") or "")) or task
        return {
            "task": detail,
            "skipped_succeeded": int(task.get("skipped_succeeded") or 0),
            "skipped_uncertain": int(task.get("skipped_uncertain") or 0),
        }, 201

    # ---- 标注库 ----

    def api_annotations(self, *, path_key: str = "", q: str = "", task_id: str = "",
                        limit: Any = 500) -> tuple[dict[str, Any], int]:
        try:
            parsed_limit = max(1, min(2000, int(str(limit or "500"))))
        except (TypeError, ValueError):
            return {"error": "limit 必须是整数"}, 400
        try:
            rows = self.db.annotations(
                path_key=str(path_key or "").strip(),
                task_id=str(task_id or "").strip(),
                query=str(q or "").strip()[:200],
                limit=parsed_limit,
            )
        except LabelerDBError as exc:
            return {"error": str(exc)}, 503
        return {"annotations": rows, "count": len(rows)}, 200

    def api_tags(self, limit: Any = 200) -> tuple[dict[str, Any], int]:
        try:
            parsed = max(1, min(1000, int(str(limit or "200"))))
        except (TypeError, ValueError):
            return {"error": "limit 必须是整数"}, 400
        try:
            rows = self.db.tag_counts(limit=parsed)
        except LabelerDBError as exc:
            return {"error": str(exc)}, 503
        return {"tags": rows, "count": len(rows)}, 200

    def api_sentence_stats(self, q: str = "", limit: Any = 300) -> tuple[dict[str, Any], int]:
        try:
            parsed = max(1, min(2000, int(str(limit or "300"))))
        except (TypeError, ValueError):
            return {"error": "limit 必须是整数"}, 400
        try:
            rows = self.db.annotations(query=str(q or "").strip()[:200], limit=EXPORT_MAX_RECORDS)
        except LabelerDBError as exc:
            return {"error": str(exc)}, 503
        return sentence_stats(rows, query=str(q or ""), limit=parsed), 200

    # ---- 导出 ----

    def api_export(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        body = body if isinstance(body, dict) else {}
        fmt = str(body.get("format") or "").strip().lower()
        task_id = str(body.get("task_id") or "").strip()
        query = str(body.get("q") or "").strip()[:200]
        raw_ids = body.get("annotation_ids")
        try:
            if isinstance(raw_ids, list) and raw_ids:
                ids: list[int] = []
                for value in raw_ids[:EXPORT_MAX_RECORDS]:
                    try:
                        ids.append(int(value))
                    except (TypeError, ValueError):
                        return {"error": "annotation_ids 必须是整数数组"}, 400
                records = self.db.annotations_by_ids(ids)
                label = "选中标注"
            elif task_id:
                if self.store.get_task(task_id) is None:
                    return {"error": "任务不存在"}, 404
                records = self.db.annotations(task_id=task_id, limit=EXPORT_MAX_RECORDS)
                label = task_id
            else:
                records = self.db.annotations(query=query, limit=EXPORT_MAX_RECORDS)
                label = "全部标注"
        except LabelerDBError as exc:
            return {"error": str(exc)}, 503
        if not records:
            return {"error": "没有可导出的标注"}, 400
        try:
            artifact = write_export(self._exports_dir, records, fmt, label=label)
        except LabelerExportError as exc:
            return {"error": str(exc)}, 400
        except OSError as exc:
            return {"error": f"导出文件写入失败：{exc}"}, 500
        return {"ok": True, **artifact}, 201
