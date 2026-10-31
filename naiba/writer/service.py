# -*- coding: utf-8 -*-
"""织书台业务门面：`/api/writer/*` 的业务方法（返回 `(payload, status)` 二元组）。

HTTP 层只做转发与错误映射（照 `naiba/novel/service.py` 的口径）：
`WriterStoreError`/`WriterPipelineError`/`PromptError` → 400、
`WriterConflictError` → 409、`LookupError` → 404。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from naiba.writer import prompts
from naiba.writer.pipeline import WriterPipelineManager, WriterPipelineError
from naiba.writer.store import (
    WriterConflictError,
    WriterStore,
    WriterStoreError,
)

# 上下文容量未知时的大输入确认阈值（与拆书匠同口径）。
LARGE_INPUT_CONFIRM_TOKENS = prompts.LARGE_INPUT_CONFIRM_TOKENS


class WriterService:
    """组装根唯一装配点；`app.writer.*` 调用面。"""

    def __init__(self, app: Any):
        self.app = app
        self.store = WriterStore(app.paths.data_dir)
        self.pipeline = WriterPipelineManager(app, self.store)

    # ---- 生命周期 ----

    def recover(self) -> dict[str, int]:
        """启动恢复：流水线 interrupted + 过期导入暂存清理。"""
        recovered_pipelines = 0
        try:
            recovered_pipelines = self.pipeline.recover()
        except Exception:  # noqa: BLE001 - 恢复失败不阻断启动
            pass
        purged = 0
        try:
            purged = self.store.purge_imports()
        except Exception:  # noqa: BLE001
            pass
        return {"pipelines": recovered_pipelines, "imports": purged}

    def start(self) -> None:
        self.pipeline.start()

    def shutdown(self) -> None:
        self.pipeline.stop()

    # ---- 作品 ----

    def api_list_books(self) -> tuple[dict[str, Any], int]:
        return self.store.list_books(), 200

    def api_create_book(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        try:
            book = self.store.create_book(
                title=str(body.get("title") or ""),
                setup=body.get("setup"),
                kind=str(body.get("setup_kind") or "manual"),
            )
            return {"book": book}, 201
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400

    def api_get_book(self, book_id: str) -> tuple[dict[str, Any], int]:
        try:
            return self.store.get_book(book_id), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400

    def api_patch_book(self, book_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """修改作品：重命名、人工设定存新版本，或人工大纲存新版本（可选同步章节）。"""
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        try:
            if body.get("title") is not None:
                book = self.store.rename_book(
                    book_id, title=str(body.get("title") or ""),
                    expected_revision=body.get("revision"),
                )
                return {"book": book}, 200
            if "parts" in body:
                # 分部文字大纲（3.9.13）：不再同步章节，章节由拆章产出。
                return self.store.save_outline(
                    book_id, parts=body.get("parts"), kind="manual", task_id="",
                    expected_revision=body.get("revision"),
                ), 200
            if "chapter_plan" in body:
                plan = body.get("chapter_plan") if isinstance(body.get("chapter_plan"), dict) else {}
                return self.store.set_chapter_plan(
                    book_id, chars=plan.get("chars"), ending=plan.get("ending"),
                    expected_revision=body.get("revision"),
                ), 200
            if "outline" in body:
                allow_drop = str(body.get("allow_drop") or "").lower() in ("1", "true", "yes")
                return self.store.save_outline(
                    book_id, chapters=body.get("outline"), kind="manual", task_id="",
                    expected_revision=body.get("revision"),
                    sync=bool(body.get("sync")), allow_drop=allow_drop,
                ), 200
            if "setup" in body:
                payload = self.store.save_setup(
                    book_id, content=body.get("setup"), kind="manual", task_id="",
                    expected_revision=body.get("revision"),
                )
                return payload, 200
            return {"error": "没有可修改的内容"}, 400
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except WriterConflictError as exc:
            return {"error": str(exc)}, 409
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400

    def api_delete_book(self, book_id: str) -> tuple[dict[str, Any], int]:
        try:
            return self.store.delete_book(book_id), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400

    # ---- 章节 ----

    def api_get_chapter(self, book_id: str, chapter_no: Any) -> tuple[dict[str, Any], int]:
        try:
            return self.store.get_chapter(book_id, self._chapter_no(chapter_no)), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400

    def api_save_chapter(self, book_id: str, chapter_no: Any,
                         body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """人工保存为新正文版本（不覆盖旧版本）。"""
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        try:
            payload = self.store.save_chapter_version(
                book_id, self._chapter_no(chapter_no),
                content=body.get("content"), kind="manual", task_id="",
                expected_revision=body.get("revision"),
            )
            return payload, (200 if payload.get("duplicate") else 201)
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except WriterConflictError as exc:
            return {"error": str(exc)}, 409
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400

    def api_update_chapter_meta(self, book_id: str, chapter_no: Any,
                                body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """改章节标题/简述/钩子（章节页「章节简述」逐行可改）。"""
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        try:
            return self.store.update_chapter_meta(
                book_id, self._chapter_no(chapter_no),
                title=body.get("title"), beat=body.get("beat"), hook=body.get("hook"),
                expected_revision=body.get("revision"),
            ), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except WriterConflictError as exc:
            return {"error": str(exc)}, 409
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400

    def api_select_version(self, book_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """切换设定 / 大纲 / 章节的当前版本。"""
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        kind = str(body.get("kind") or "").strip()
        version_id = str(body.get("version_id") or "").strip()
        if kind not in ("setup", "outline", "chapter"):
            return {"error": "kind 必须是 setup / outline / chapter"}, 400
        try:
            if kind == "chapter":
                chapter_no = self._chapter_no(body.get("chapter_no"))
                payload = self.store.select_chapter_version(
                    book_id, chapter_no, version_id=version_id,
                    expected_revision=body.get("revision"),
                )
                return payload, 200
            if kind == "setup":
                return self.store.select_setup_version(
                    book_id, version_id=version_id,
                    expected_revision=body.get("revision"),
                ), 200
            return self.store.select_outline_version(
                book_id, version_id=version_id,
                expected_revision=body.get("revision"),
            ), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except WriterConflictError as exc:
            return {"error": str(exc)}, 409
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400

    def api_delete_chapter(self, book_id: str, chapter_no: Any) -> tuple[dict[str, Any], int]:
        try:
            return self.store.delete_chapter(book_id, self._chapter_no(chapter_no)), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400

    def api_sync_chapters(self, book_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """用当前大纲重新同步章节索引；删除有正文的章节需显式确认。"""
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        allow_drop = str(body.get("allow_drop") or "").lower() in ("1", "true", "yes")
        try:
            return self.store.sync_chapters(
                book_id, expected_revision=body.get("revision"), allow_drop=allow_drop,
            ), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except WriterConflictError as exc:
            return {"error": str(exc)}, 409
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400

    # ---- 预览与流水线 ----

    def _context_window(self, model_key: str) -> int:
        key = str(model_key or "").strip()
        if not key:
            return 0
        try:
            profiles = self.app.config.model_profiles()
        except Exception:  # noqa: BLE001 - 配置读不到按未知处理
            return 0
        row = next((item for item in profiles if str(item.get("model_key") or "") == key), None)
        if not row:
            return 0
        try:
            return max(0, int(row.get("context_window") or 0))
        except (TypeError, ValueError):
            return 0

    def api_preview(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """生成前预览：返回实际会发送的上下文槽位与预算，不建任务。

        与正式提交共用同一组 build 函数（实施计划 §6.2），右栏看到的来源就是
        实际发送的来源。
        """
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        kind = str(body.get("kind") or "chapter").strip()
        try:
            book_id = str(body.get("book_id") or "").strip()
            if not book_id:
                return {"error": "缺少 book_id"}, 400
            book = self.store.get_book(book_id)
            setup_version_id = str(body.get("setup_version_id") or "").strip() \
                or str(book["book"].get("current_setup_version_id") or "")
            setup = self.store.load_setup(book_id, version_id=setup_version_id)
            if kind in ("chapter", "chapter_segs", "chapter_continue", "chapter_rewrite",
                        "segs", "draft"):
                chapter_no = self._chapter_no(body.get("chapter_no"))
                index = next((row for row in book["chapters"] if row["no"] == chapter_no), None)
                if index is None:
                    return {"error": f"作品里没有第 {chapter_no} 章"}, 404
                chapter_view = {
                    "no": chapter_no, "title": index["title"],
                    "beat": index["beat"], "hook": index["hook"],
                }
                mode = "continue" if kind in ("chapter_continue",) else "whole"
                draft_tail = ""
                if mode == "continue":
                    current = self.store.get_chapter(book_id, chapter_no)
                    draft_tail = str(current.get("content") or "")[-prompts.CONTINUE_TAIL_CHARS:]
                built = prompts.build_draft_context(
                    setup, chapter_view, mode=mode, draft_tail=draft_tail,
                    extra_note=str(body.get("extra_note") or ""),
                )
                budget = prompts.budget_report(
                    content=built["content"], slots=built["slots"],
                    context_window=self._context_window(str(body.get("model_key") or "")),
                )
                return {
                    "kind": kind, "chapter_no": chapter_no,
                    "chapter": chapter_view,
                    "content": built["content"], "slots": built["slots"],
                    "budget": budget,
                    "system": prompts.step_system("draft"),
                }, 200
            step = kind if kind in prompts.SETUP_STEPS else ""
            if not step:
                return {"error": f"未知的预览类型：{kind or '（空）'}"}, 400
            content = prompts.build_step_user(
                step, setup,
                char_count=self._int_or(body.get("char_count"), 4),
                side_count=self._int_or(body.get("side_count"), 0),
                chapter_count=self._int_or(body.get("chapter_count"), 10),
                extra_note=str(body.get("extra_note") or ""),
            )
            budget = prompts.budget_report(
                content=content, slots=[{
                    "key": "idea", "label": "创作提示词与当前设定", "included": True,
                    "chars": len(content),
                }],
                context_window=self._context_window(str(body.get("model_key") or "")),
            )
            return {
                "kind": step, "content": content,
                "system": prompts.step_system(step),
                "budget": budget,
                "slots": budget["included"],
            }, 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except (WriterStoreError, prompts.PromptError) as exc:
            return {"error": str(exc)}, 400

    def api_start_pipeline(self, book_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        model_name = str(body.get("model_name") or "").strip()
        if not model_name:
            return {"error": "请选择模型；可用模型来自设置页的模型检查结果"}, 400
        try:
            pipeline = self.pipeline.start_pipeline(
                book_id,
                kind=str(body.get("kind") or ""),
                step=str(body.get("step") or ""),
                chapter_no=self._int_or(body.get("chapter_no"), 0),
                model_key=str(body.get("model_key") or ""),
                model_name=model_name,
                char_count=self._int_or(body.get("char_count"), 4),
                side_count=self._int_or(body.get("side_count"), 0),
                chapter_count=self._int_or(body.get("chapter_count"), 10),
                setup_version_id=str(body.get("setup_version_id") or ""),
                outline_version_id=str(body.get("outline_version_id") or ""),
                extra_note=str(body.get("extra_note") or ""),
                allow_drop=str(body.get("allow_drop") or "").lower() in ("1", "true", "yes"),
                part_no=self._int_or(body.get("part_no"), 0),
                part_title=str(body.get("part_title") or ""),
                target_chars=self._int_or(body.get("target_chars"), 0),
                want_count=self._int_or(body.get("want_count"), 0),
                from_no=self._int_or(body.get("from_no"), 0),
                part_plan=body.get("part_plan"),
                base_revision=body.get("revision"),
                chapter_revision=body.get("chapter_revision"),
            )
            return {"pipeline": pipeline}, 201
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except WriterConflictError as exc:
            return {"error": str(exc)}, 409
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400
        except WriterPipelineError as exc:
            return {"error": str(exc)}, 400

    def api_get_pipeline(self, book_id: str) -> tuple[dict[str, Any], int]:
        try:
            return self.pipeline.get_pipeline(book_id), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400

    def api_cancel_pipeline(self, book_id: str) -> tuple[dict[str, Any], int]:
        try:
            return {"pipeline": self.pipeline.cancel_pipeline(book_id)}, 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400
        except WriterPipelineError as exc:
            return {"error": str(exc)}, 409

    def api_retry_pipeline(self, book_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        rebase = isinstance(body, dict) and str(body.get("rebase") or "").lower() in ("1", "true", "yes")
        try:
            return {"pipeline": self.pipeline.retry_pipeline(book_id, rebase=rebase)}, 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400
        except WriterPipelineError as exc:
            return {"error": str(exc)}, 409

    # ---- 导入 ----

    def api_import(self, *, filename: str, data: bytes) -> tuple[dict[str, Any], int]:
        try:
            return self.store.stage_import(filename=filename, data=data), 200
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400

    def api_commit_import(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        try:
            return self.store.commit_import(str(body.get("import_id") or "")), 201
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400

    # ---- 版本删除 / 清空章节 / 设定集导入（第二次反馈） ----

    def api_delete_versions(self, book_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """删除设定或大纲版本（管理记录里勾选删除；删当前版本时指针落到最新一版）。"""
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        kind = str(body.get("kind") or "").strip()
        if kind not in ("setup", "outline"):
            return {"error": "kind 必须是 setup 或 outline"}, 400
        try:
            if kind == "setup":
                return self.store.delete_setup_versions(
                    book_id, body.get("version_ids"),
                    expected_revision=body.get("revision"),
                ), 200
            return self.store.delete_outline_versions(
                book_id, body.get("version_ids"),
                expected_revision=body.get("revision"),
            ), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except WriterConflictError as exc:
            return {"error": str(exc)}, 409
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400

    def api_delete_chapter_versions(self, book_id: str, chapter_no: Any,
                                    body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """删除某一章的正文版本（管理记录里展开章后勾选删除）。"""
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        try:
            return self.store.delete_chapter_versions(
                book_id, self._chapter_no(chapter_no), body.get("version_ids"),
                expected_revision=body.get("revision"),
            ), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except WriterConflictError as exc:
            return {"error": str(exc)}, 409
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400

    def api_clear_chapters(self, book_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """清空全部章节（覆盖重拆前让位；前端弹窗已让用户确认过）。"""
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        try:
            return self.store.clear_chapters(
                book_id, expected_revision=body.get("revision"),
            ), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except WriterConflictError as exc:
            return {"error": str(exc)}, 409
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400

    def api_import_settings(self, book_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """设定集文件夹导入：解析 .md 文件并把设定/大纲/章节并入当前作品。"""
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        files = body.get("files")
        if not isinstance(files, list) or not files:
            return {"error": "没有读到任何设定集文件"}, 400
        cleaned: list[dict[str, str]] = []
        total = 0
        for item in files:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            content = str(item.get("content") or "")
            if not name or not content.strip():
                continue
            suffix = name.lower().rsplit(".", 1)
            suffix = f".{suffix[-1]}" if len(suffix) == 2 else ""
            if suffix not in (".md", ".markdown", ".txt"):
                continue
            cleaned.append({"name": name[:200], "content": content})
            total += len(content)
        if not cleaned:
            return {"error": "文件夹里没有 .md / .txt 文件；请选择设定集文件夹"}, 400
        if total > 3_000_000:
            return {"error": "设定集内容超过 300 万字符上限；请删减后重试"}, 400
        try:
            return self.store.import_settings_folder(
                book_id, cleaned, expected_revision=body.get("revision"),
            ), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except WriterConflictError as exc:
            return {"error": str(exc)}, 409
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400

    # ---- 导出与位置 ----

    def api_export_book(self, book_id: str, fmt: str = "txt",
                        with_extras: Any = "") -> tuple[dict[str, Any], int]:
        if fmt not in ("txt", "md"):
            return {"error": "导出格式只支持 txt 或 md"}, 400
        extras = str(with_extras).strip().lower() in ("1", "true", "yes", "on")
        try:
            result = self.store.export_book(book_id, fmt=fmt, with_extras=extras)
            book = self.store.get_book(book_id)["book"]
            return {
                "format": fmt,
                "text": result["text"],
                "missing_chapters": result["missing_chapters"],
                "filename": result["filename"],
                "book": book,
                "location": self.data_location(),
            }, 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400

    def api_export_file(self, book_id: str, body: Any) -> tuple[dict[str, Any], int]:
        """导出成真实文件（POST：有落盘副作用，不能用 GET）。

        前端拿到 path 后交给桌面端「另存为」，浏览器/手机则走 `/api/file?download=1`。
        没有正文时不写「看起来成功」的空文件，直接把 has_content=False 交给前端提示。
        """
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        fmt = str(body.get("format") or "txt").strip().lower()
        if fmt not in ("txt", "md"):
            return {"error": "导出格式只支持 txt 或 md"}, 400
        extras = str(body.get("extras") or "").strip().lower() in ("1", "true", "yes", "on")
        try:
            result = self.store.export_to_file(book_id, fmt=fmt, with_extras=extras)
            result["book"] = self.store.get_book(book_id)["book"]
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except WriterStoreError as exc:
            return {"error": str(exc)}, 400
        except OSError as exc:
            return {"error": f"写导出文件失败：{exc}"}, 500
        result["format"] = fmt
        result["with_extras"] = extras
        result["location"] = self.data_location()
        return result, 200

    def data_location(self) -> dict[str, Any]:
        return {
            "data_dir": str(Path(self.app.paths.data_dir).resolve()),
            "writer_root": str(self.store.root().resolve()),
        }

    def api_location(self) -> tuple[dict[str, Any], int]:
        payload = self.data_location()
        payload["can_open_folder"] = os.name == "nt"
        payload["export_note"] = ("导出先在保存目录的 _exports 里生成文件，桌面端随后弹出「另存为」；"
                                  "手机/浏览器直接下载。")
        return payload, 200

    def api_reveal(self) -> tuple[dict[str, Any], int]:
        if os.name != "nt":
            return {"error": "只有运行应用的电脑上才能直接打开保存位置"}, 400
        target = self.store.root().resolve()
        try:
            target.mkdir(parents=True, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(str(target))  # type: ignore[attr-defined]  # 仅 Windows 有
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except OSError as exc:
            return {"error": f"无法打开保存位置：{exc}"}, 500
        return {"ok": True, "path": str(target)}, 200

    # ---- 校验 ----

    @staticmethod
    def _chapter_no(raw: Any) -> int:
        if isinstance(raw, bool):
            raise WriterStoreError("章节号必须是整数")
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            raise WriterStoreError("章节号必须是整数") from None
        if value < 1 or value > 9999:
            raise WriterStoreError("章节号必须在 1–9999 之间")
        return value

    @staticmethod
    def _int_or(raw: Any, default: int) -> int:
        if isinstance(raw, bool) or raw is None or raw == "":
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default
