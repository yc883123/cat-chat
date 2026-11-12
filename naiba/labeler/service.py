"""打标器域门面：``app.labeler.*`` 的调用面，返回 ``(payload, HTTPStatus)``。

HTTP 层只解析请求与转发；输入校验、状态机判定、导出落点都在这里与下层模块。
所有会写盘的路径都由服务端决定：接受的是记录 ID / 查询条件 / 格式，绝不接受
客户端提供的文件系统路径。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from naiba.core.contracts import (
    LABELER_CONCURRENCY_SETTINGS_KEY,
    LABELER_PROVIDER_LIMITS_PATCH_KEY,
)
from naiba.labeler.db import LabelerDB, LabelerDBError
from naiba.labeler.export import LabelerExportError, open_export_dir, open_managed_dir, write_export
from naiba.labeler.gate import (
    DEFAULT_PROVIDER_CONCURRENCY,
    MAX_PROVIDER_CONCURRENCY,
    MAX_WORKERS,
    MIN_PROVIDER_CONCURRENCY,
    SUPERVISOR_MAX_CONCURRENCY,
    validate_concurrency_config,
    validate_provider_limits,
)
from naiba.labeler.images import (
    MAX_BATCH_BYTES,
    MAX_BATCH_IMAGES,
    MAX_IMAGE_BYTES,
    MAX_UPLOAD_BYTES,
    LabelerImageError,
    verify_image_bytes,
)
from naiba.labeler.jobs import LabelerJobManager, LabelerTaskError
from naiba.labeler.originals import archive_original, prune_unreferenced
from naiba.labeler.presets import preset_list
from naiba.labeler.runner import DEFAULT_CONCURRENCY, VALIDATED_CONCURRENCY, LabelerRunner
from naiba.labeler.sentences import sentence_stats
from naiba.labeler.store import LabelerStore, LabelerStoreError
from naiba.storage.media import store_uploaded_file

EXPORT_MAX_RECORDS = 9999


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
        recovered = self.jobs.recover_interrupted()
        self._prune_originals()
        return recovered

    def _prune_originals(self) -> dict[str, int]:
        """原图留存生命周期：没有任何标注/任务项引用且超过保护窗口的才删。

        查询或清理失败都按「不清理」处理：宁可多留，不能因为清理动作影响启动。
        """
        try:
            referenced = self.db.referenced_original_digests()
        except (OSError, ValueError, LabelerDBError):
            return {"removed": 0, "kept": 0, "freed": 0}
        try:
            return prune_unreferenced(Path(self.app.paths.data_dir), referenced)
        except OSError:
            return {"removed": 0, "kept": 0, "freed": 0}

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

    # ---- 上传（打标专用：原图留存 + 不去重） ----

    def api_upload(self, spool_path: str, name: str) -> tuple[dict[str, Any], int]:
        """打标专用上传的落库入口（multipart spool 已由 http 层落好）。

        - 原始字节按 SHA-256 内容寻址存进 ``data/labeler/originals/``：训练包导出时
          按摘要取字节校验，缩放/压缩后的缓存图不能替代原图；
        - 缓存副本不做内容级去重：同内容但来源不同的图片必须落成两个文件，
          否则两个样本会共享 path_key，提交批次时第二条就被合并掉了。
        """
        spool = Path(spool_path)
        try:
            data = spool.read_bytes()
        except OSError as exc:
            return {"error": f"上传临时文件读取失败：{exc}"}, 400
        finally:
            spool.unlink(missing_ok=True)
        try:
            verify_image_bytes(name, data, max_bytes=MAX_UPLOAD_BYTES)
        except LabelerImageError as exc:
            return {"error": str(exc)}, 400
        data_dir = Path(self.app.paths.data_dir)
        try:
            archived = archive_original(data_dir, data)
        except OSError as exc:
            return {"error": f"原图留存失败：{exc}"}, 500
        imaging = {}
        config = getattr(self.app, "config", None)
        if config is not None:
            imaging = dict((getattr(config, "data", {}) or {}).get("imaging") or {})
        try:
            result = store_uploaded_file(
                data, name, data_dir, imaging, dedupe=False, max_bytes=MAX_IMAGE_BYTES,
            )
        except (OSError, ValueError) as exc:
            return {"error": f"贴签缓存生成失败：{exc}"}, 400
        return {
            "name": result.get("name") or "",
            "path": result.get("path") or "",
            "size": result.get("size") or 0,
            "original_size": archived["size"],
            "thumb_path": result.get("thumb_path") or "",
            "deduped": False,
            "original_sha256": archived["sha256"],
            "original_size": archived["size"],
        }, 200

    # ---- 配置 ----

    def api_presets(self) -> tuple[dict[str, Any], int]:
        return {
            "presets": preset_list(),
            "output_modes": [
                {"id": "keywords", "label": "标签（逗号分隔的关键词）"},
                {"id": "phrases", "label": "词组短语（逗号分隔的短句）"},
                {"id": "description", "label": "自然语言（完整句子）"},
                {"id": "both", "label": "标签 + 自然语言"},
            ],
            "languages": [
                {"id": "zh", "label": "中文"},
                {"id": "en", "label": "英文"},
                {"id": "mixed", "label": "英文标签 + 中文描述"},
            ],
            # 语言选项按输出形式联动（2026-10-26 计划）：mixed 只在「标签 + 自然语言」
            # 下可选，单一形式的文案不带「全」字。前端静态选项与此同口径。
            "languages_by_mode": {
                "keywords": [
                    {"id": "zh", "label": "中文"},
                    {"id": "en", "label": "英文"},
                ],
                "phrases": [
                    {"id": "zh", "label": "中文"},
                    {"id": "en", "label": "英文"},
                ],
                "description": [
                    {"id": "zh", "label": "中文"},
                    {"id": "en", "label": "英文"},
                ],
                "both": [
                    {"id": "zh", "label": "全中文"},
                    {"id": "en", "label": "全英文"},
                    {"id": "mixed", "label": "英文标签 + 中文描述"},
                ],
            },
            "limits": {
                "max_batch_images": MAX_BATCH_IMAGES,
                "max_image_bytes": MAX_IMAGE_BYTES,
                "max_upload_bytes": MAX_UPLOAD_BYTES,
                "max_batch_bytes": MAX_BATCH_BYTES,
                "default_concurrency": DEFAULT_CONCURRENCY,
                "validated_concurrency": list(VALIDATED_CONCURRENCY),
                # 自定义并发监督档：槽位上限、供应商档案默认并发与监督队列上限。
                "max_workers": MAX_WORKERS,
                "default_provider_limit": DEFAULT_PROVIDER_CONCURRENCY,
                "min_provider_limit": MIN_PROVIDER_CONCURRENCY,
                "max_provider_limit": MAX_PROVIDER_CONCURRENCY,
                "supervisor_max_concurrency": SUPERVISOR_MAX_CONCURRENCY,
            },
        }, 200

    # ---- 设置（供应商并发上限） ----

    def api_settings(self) -> tuple[dict[str, Any], int]:
        settings = self.store.read_settings()
        return {
            "provider_limits": settings.get("provider_limits") or {},
            "concurrency": settings.get("concurrency") or {},
            "default_provider_limit": DEFAULT_PROVIDER_CONCURRENCY,
            "min_provider_limit": MIN_PROVIDER_CONCURRENCY,
            "max_provider_limit": MAX_PROVIDER_CONCURRENCY,
            "max_workers": MAX_WORKERS,
            "supervisor_max_concurrency": SUPERVISOR_MAX_CONCURRENCY,
        }, 200

    def api_update_settings(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """原子保存一类设置：供应商额度、或并发打标配置，二者不可混发。"""
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        allowed = {
            "provider_limits",
            LABELER_PROVIDER_LIMITS_PATCH_KEY,
            LABELER_CONCURRENCY_SETTINGS_KEY,
        }
        if set(body) - allowed or len(set(body) & allowed) != 1:
            return {"error": "请只提交 provider_limits、provider_limits_patch 或 concurrency 之一"}, 400
        if LABELER_CONCURRENCY_SETTINGS_KEY in body:
            try:
                config = validate_concurrency_config(body[LABELER_CONCURRENCY_SETTINGS_KEY])
            except ValueError as exc:
                return {"error": str(exc)}, 400
            try:
                saved = self.store.patch_concurrency(config)
            except OSError as exc:
                return {"error": f"并发配置保存失败：{exc}"}, 500
            return {"ok": True, **saved}, 200
        is_patch = LABELER_PROVIDER_LIMITS_PATCH_KEY in body
        raw = body[LABELER_PROVIDER_LIMITS_PATCH_KEY if is_patch else "provider_limits"]
        if not isinstance(raw, dict) or (is_patch and not raw):
            return {"error": "并发上限必须是对象，增量保存不能为空"}, 400
        if any(not isinstance(key, str) or not key.strip() for key in raw):
            return {"error": "模型配置标识不能为空"}, 400
        if len({key.strip() for key in raw}) != len(raw):
            return {"error": "模型配置标识不能重复"}, 400
        try:
            limits = validate_provider_limits(raw)
        except ValueError as exc:
            return {"error": str(exc)}, 400
        for key in limits:
            try:
                self.app.config.profile(key)
            except (ValueError, KeyError):
                return {"error": f"模型配置不存在：{key}"}, 400
        try:
            saved = (self.store.patch_provider_limits(limits) if is_patch
                     else self.store.write_settings({"provider_limits": limits}))
        except OSError as exc:
            return {"error": f"并发上限保存失败：{exc}"}, 500
        return {"ok": True, **saved}, 200

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

    def api_task_items(self, task_id: str, offset: Any = 0, limit: Any = 500) -> tuple[dict[str, Any], int]:
        try:
            parsed_offset = max(0, int(str(offset or "0")))
            parsed_limit = max(1, min(1000, int(str(limit or "500"))))
        except (TypeError, ValueError):
            return {"error": "offset / limit 必须是整数"}, 400
        result = self.jobs.task_items(task_id, offset=parsed_offset, limit=parsed_limit)
        if result is None:
            return {"error": "任务不存在"}, 404
        return result, 200

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

    # ---- 任务 TXT 输出（2026-11-10 计划） ----

    def api_task_txt_files(self, task_id: str) -> tuple[dict[str, Any], int]:
        """任务内每张图的 TXT 写入状态与可访问路径（结果区列表/下载入口）。"""
        task = self.store.get_task(task_id)
        if task is None:
            return {"error": "任务不存在"}, 404
        try:
            root = self.store.outputs_dir(task_id)
        except LabelerStoreError:  # 非法 ID 与「任务不存在」同口径
            return {"error": "任务不存在"}, 404
        files: list[dict[str, Any]] = []
        for row in self.db.list_items(task_id, limit=10000):
            relative = str(row.get("txt_path") or "")
            if not relative:
                continue
            path = (root / relative).resolve()
            try:
                path.relative_to(root.resolve())
            except ValueError:
                continue
            size = 0
            try:
                if path.is_file():
                    size = int(path.stat().st_size)
            except OSError:
                size = 0
            files.append({
                "item_id": str(row.get("item_id") or ""),
                "name": str(row.get("name") or ""),
                "relative_path": str(row.get("relative_path") or ""),
                "txt_path": relative,
                "state": str(row.get("txt_state") or ""),
                "chars": int(row.get("txt_chars") or 0),
                "size": size,
                "path": str(path) if size else "",
            })
        return {"files": files, "count": len(files), "dir": str(root)}, 200

    def api_task_txt_content(self, task_id: str, item_id: str) -> tuple[dict[str, Any], int]:
        """读取单张图片已写入的 TXT 文本（结果区「查看」）。"""
        if self.store.get_task(task_id) is None:
            return {"error": "任务不存在"}, 404
        item = self.db.get_item(task_id, item_id)
        if item is None:
            return {"error": "图片不在该任务里"}, 404
        relative = str(item.get("txt_path") or "")
        if not relative:
            return {"error": "这张图还没有写入 TXT"}, 404
        try:
            root = self.store.outputs_dir(task_id)
        except LabelerStoreError:  # 非法 ID 与「任务不存在」同口径
            return {"error": "任务不存在"}, 404
        path = (root / relative).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            return {"error": "TXT 路径越出任务输出目录"}, 403
        if not path.is_file():
            return {"error": "TXT 文件不存在（可能已被清理）"}, 404
        limit = 512 * 1024
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return {"error": f"TXT 读取失败：{exc}"}, 500
        truncated = len(raw) > limit
        text = raw[:limit].decode("utf-8", errors="replace")
        return {
            "item_id": item_id,
            "relative_path": str(item.get("relative_path") or item.get("name") or ""),
            "txt_path": relative,
            "content": text,
            "truncated": truncated,
            "path": str(path),
        }, 200

    def api_task_txt_open(self, task_id: str) -> tuple[dict[str, Any], int]:
        """在运行本应用的电脑上打开任务 TXT 输出目录（路径固定，不收客户端参数）。"""
        if self.store.get_task(task_id) is None:
            return {"error": "任务不存在"}, 404
        try:
            path = open_managed_dir(self.store.outputs_dir(task_id))
        except LabelerStoreError:
            return {"error": "任务不存在"}, 404
        except OSError as exc:
            return {"error": f"无法打开 TXT 文件夹：{exc}"}, 500
        return {"ok": True, "path": path}, 200

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

    def api_tasks_delete(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """批量删除已结束任务；不带 id = 清理全部已结束（对齐造梦机口径）。

        标注库与原图留存不受影响；排队/执行中的跳过并回报。
        """
        body = body if isinstance(body, dict) else {}
        raw = body.get("task_ids")
        task_ids = raw if isinstance(raw, list) and raw else None
        try:
            result = self.jobs.delete_tasks(task_ids)
        except LabelerDBError as exc:
            return {"error": f"标注库繁忙，请稍后重试：{exc}"}, 503
        except OSError as exc:
            return {"error": f"删除任务失败：{exc}"}, 500
        return {"ok": True, **result}, 200

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
                        limit: Any = 500, offset: Any = 0) -> tuple[dict[str, Any], int]:
        try:
            parsed_limit = max(1, min(1000, int(str(limit or "500"))))
            parsed_offset = max(0, int(str(offset or "0")))
        except (TypeError, ValueError):
            return {"error": "limit 必须是整数"}, 400
        if task_id and self.store.get_task(task_id) is None:
            return {"error": "任务不存在"}, 404
        try:
            rows = self.db.annotations(
                path_key=str(path_key or "").strip(),
                task_id=str(task_id or "").strip(),
                query=str(q or "").strip()[:200],
                limit=parsed_limit,
                offset=parsed_offset,
            )
        except LabelerDBError as exc:
            return {"error": str(exc)}, 503
        total = int(self.db.counts(task_id).get("done", 0)) if task_id else len(rows)
        return {"annotations": rows, "count": len(rows), "offset": parsed_offset,
                "total": total}, 200

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

    def api_annotations_clear(self) -> tuple[dict[str, Any], int]:
        """清空整个标注库（用户显式操作，前端强确认后才会调到这里）。

        有活跃批次时拒绝：runner 正在写标注，清空会有竞态。原图留存不在
        这里删：清空后引用集变空，由启动恢复的 prune 链路按保护窗口自然回收。
        """
        active = self.store.active_task_ids()
        if active:
            return {"error": "还有批次在排队或打标中，先取消或等批次结束后再清空标注库"}, 400
        try:
            deleted = self.db.clear_annotations()
        except LabelerDBError as exc:
            return {"error": f"标注库繁忙，请稍后重试：{exc}"}, 503
        except OSError as exc:
            return {"error": f"清空标注库失败：{exc}"}, 500
        return {"ok": True, "deleted": deleted}, 200

    # ---- 导出 ----

    def api_export(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        body = body if isinstance(body, dict) else {}
        fmt = str(body.get("format") or "").strip().lower()
        task_id = str(body.get("task_id") or "").strip()
        query = str(body.get("q") or "").strip()[:200]
        # 可选自定义包名：留空 = 自动命名（同一文件夹时用文件夹本来的名字）。
        custom_name = str(body.get("name") or "").strip()[:120]
        raw_ids = body.get("annotation_ids")
        try:
            if isinstance(raw_ids, list) and raw_ids:
                ids: list[int] = []
                for value in raw_ids:
                    try:
                        ids.append(int(value))
                    except (TypeError, ValueError):
                        return {"error": "annotation_ids 必须是整数数组"}, 400
                if len(ids) > EXPORT_MAX_RECORDS:
                    return {"error": f"一次最多导出 {EXPORT_MAX_RECORDS} 条标注"}, 400
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
            artifact = write_export(
                self._exports_dir, records, fmt, label=label,
                data_dir=Path(self.app.paths.data_dir), custom_name=custom_name,
            )
        except LabelerExportError as exc:
            return {"error": str(exc)}, 400
        except OSError as exc:
            return {"error": f"导出文件写入失败：{exc}"}, 500
        return {"ok": True, **artifact}, 201

    def api_reveal_exports(self) -> tuple[dict[str, Any], int]:
        """在运行本应用的电脑上打开受管导出目录（路径固定，不收客户端参数）。"""
        try:
            path = open_export_dir(self._exports_dir)
        except OSError as exc:
            return {"error": f"无法打开导出文件夹：{exc}"}, 500
        return {"ok": True, "path": path}, 200
