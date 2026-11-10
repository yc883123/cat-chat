"""从拆书匠只读取料，为末章后的新章创建独立写作副本。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from typing import Any

from naiba.core.contracts import WriterContinuationSummary, WriterNovelChapterSource
from naiba.writer.store import (
    BOOK_ID_PATTERN, CHAPTER_CONTENT_MAX_CHARS, SCHEMA_VERSION, TITLE_MAX,
    WriterConflictError, WriterStoreError, _now_ms, new_version_id, normalize_setup,
)

SUMMARY_KEYS = ("characters_state", "relations_state", "plot_state", "next_outline")
SUMMARY_LABELS = ("人物现状", "关系现状", "剧情现状", "后续大纲")
SUMMARY_MAX_CHARS = 2000


def normalize_summary(raw: Any) -> WriterContinuationSummary:
    if not isinstance(raw, dict):
        raise WriterStoreError("请人工确认人物、关系、剧情现状和后续大纲")
    result: WriterContinuationSummary = {}
    for key, label in zip(SUMMARY_KEYS, SUMMARY_LABELS):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise WriterStoreError(f"请填写并确认{label}")
        value = value.strip()
        if len(value) > SUMMARY_MAX_CHARS:
            raise WriterStoreError(f"{label}不能超过 {SUMMARY_MAX_CHARS} 字符，原内容未被截断")
        result[key] = value
    return result


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def preview_source(novel: Any, body: dict[str, Any]) -> dict[str, Any]:
    """取一个锁内只读快照；未选稿时只给候选，不默认挑任何改编版本。"""
    from naiba.novel.service import NovelServiceError

    book_id = str(body.get("book_id") or "").strip()
    if not book_id:
        raise WriterStoreError("请选择拆书匠作品")
    with novel._lock:
        manifest = novel._load_manifest(book_id, upgrade=False)
        if int(manifest.get("schema_version") or 0) < 2:
            raise WriterStoreError("此作品是旧格式，请先在拆书匠打开并完成升级，再导入续写")
        try:
            raw_nos = manifest.get("chapter_ids") or []
            if any(isinstance(no, bool) for no in raw_nos):
                raise ValueError("布尔值不是章号")
            all_nos = sorted({int(no) for no in raw_nos})
        except (TypeError, ValueError):
            raise WriterStoreError("源作品章节索引损坏，存在无法识别的章号") from None
        if any(no < 1 or no > 9999 for no in all_nos):
            raise WriterStoreError("源作品章节索引损坏，章号超出 1–9999 范围")
        if not all_nos:
            raise WriterStoreError("这部作品没有可导入章节")
        end = body.get("end_chapter_no")
        if end is None or end == "":
            end = all_nos[-1]
        if isinstance(end, bool) or not isinstance(end, int) or end not in all_nos:
            raise WriterStoreError("末章必须是作品中实际存在的章号")
        nos = [no for no in all_nos if no <= end]
        book_dir = novel._book_dir(book_id)
        sources: list[dict[str, Any]] = []
        source_kind = str(body.get("source_kind") or "").strip()
        if source_kind not in ("", "original", "manuscript"):
            raise WriterStoreError("请明确选择原文或指定改编稿")
        explicit = body.get("manuscripts_by_chapter")
        if explicit is not None and not isinstance(explicit, dict):
            raise WriterStoreError("manuscripts_by_chapter 必须是逐章稿件映射")
        mapping: dict[int, str] = {}
        selection_issues: list[dict[str, Any]] = []
        for no in nos:
            try:
                index = novel._load_chapter_index(book_dir, no)
                rows = novel._chapter_manuscripts(book_dir, index)
            except (LookupError, NovelServiceError, OSError) as exc:
                index, rows = {}, []
                selection_issues.append({"chapter_no": no, "reason": str(exc)})
            options = [{"id": str(row.get("id") or ""),
                        "kind": str(row.get("kind") or ""),
                        "label": str(row.get("label") or ""),
                        "version": int(row.get("no") or 0),
                        "broken": bool(row.get("broken")),
                        "error": str(row.get("error") or "")}
                       for row in rows if isinstance(row, dict)]
            sources.append({"no": no, "title": str(index.get("title") or f"第 {no} 章"),
                            "manuscripts": options})
            if source_kind == "original":
                originals = [row for row in options if row["kind"] == "original"]
                if len(originals) != 1:
                    selection_issues.append({"chapter_no": no, "reason": "没有唯一可确认的原文稿件；不会以旧正文或改编稿冒充原文"})
                else:
                    mapping[no] = originals[0]["id"]
            elif source_kind == "manuscript":
                selected = str((explicit or {}).get(str(no)) or (explicit or {}).get(no) or "").strip()
                candidate = next((row for row in options if row["id"] == selected), None)
                if candidate is None or candidate["kind"] in ("original", "legacy"):
                    selection_issues.append({"chapter_no": no, "reason": "请为本章明确选择一份改编稿"})
                else:
                    mapping[no] = selected
        result: dict[str, Any] = {
            "ready": False, "source_book_id": book_id, "source_kind": source_kind,
            "title": str(manifest.get("title") or ""), "end_chapter_no": end,
            "new_chapter_no": end + 1, "chapter_count": len(nos),
            "chapter_sources": sources, "issues": selection_issues,
        }
        if not source_kind:
            return result
        if end >= 9999:
            raise WriterStoreError("末章已到 9999，无法继续新建下一章")
        # 候选可先列出，让用户缩小截止范围；确认所选底稿时仍禁止缺章。
        if nos != list(range(1, end + 1)):
            missing = sorted(set(range(1, end + 1)) - set(nos))
            raise WriterStoreError("导入范围缺少章节：" + "、".join(map(str, missing)))
        if selection_issues:
            return result
        checked = novel.preflight_chapters(book_id, nos, mapping, require_explicit=True)
        chapters = checked["chapters"]
        fingerprint = _digest({"book_id": book_id, "title": manifest.get("title"), "source_kind": source_kind,
                               "chapters": chapters})
        changed = 0
        for row, source in zip(chapters, sources):
            original = next((option for option in source["manuscripts"] if option["kind"] == "original"), None)
            if source_kind == "manuscript" and original:
                try:
                    record = novel._load_manuscript(book_dir, int(row["i"]), original["id"])
                    changed += str(record.get("content") or "") != str(row.get("content") or "")
                except (LookupError, NovelServiceError, OSError):
                    # 原文损坏不替代用户所选的可读改编稿，差异数量标记未知。
                    changed = -1
                    break
        elements = manifest.get("elements") if isinstance(manifest.get("elements"), dict) else {}
        raw_characters = elements.get("characters") if isinstance(elements.get("characters"), list) else []
        names = set()
        for character in raw_characters:
            name = (str(character.get("name") or "").strip() if isinstance(character, dict)
                    else re.split(r"[：:]", str(character), maxsplit=1)[0].strip())
            if name:
                names.add(name)
        recent_text = "\n".join(str(row.get("content") or "") for row in chapters[-20:])
        relations = elements.get("relations")
        coverage = {"imported_chapters": len(chapters), "recent_chapters": min(20, len(chapters)),
                    "characters": len(names),
                    "recent_characters": sum(name in recent_text for name in names),
                    "relations": len(relations) if isinstance(relations, list) else None,
                    "plot_status": "needs_confirmation",
                    "comparison": "unknown" if changed < 0 else "available",
                    "divergent_chapters": None if changed < 0 else changed,
                    "note": "人物数量来自已保存要素，仅统计名字在末 20 章所选正文中的出现；关系与剧情现状仍需人工确认，数量不代表覆盖完整或续写质量。"}
        result.update(ready=True, fingerprint=fingerprint, coverage=coverage,
                      selected_chapters=[{"no": int(row["i"]), "title": row["title"],
                                          "manuscript_id": row["manuscript_id"],
                                          "manuscript_kind": row["manuscript_kind"],
                                          "manuscript_label": row.get("manuscript_label", ""),
                                          "version": row.get("manuscript_no", 0)} for row in chapters],
                      _chapters=chapters)
        last = chapters[-1]
        last_content = str(last["content"])
        result["last_chapter"] = {"no": int(last["i"]), "title": last["title"],
                                  "excerpt": last_content[-1500:], "total_chars": len(last_content),
                                  "truncated": len(last_content) > 1500}
        return result


def request_identity(body: dict[str, Any]) -> tuple[str, str, dict[str, str]]:
    if body.get("confirmed") is not True:
        raise WriterStoreError("请人工确认四项摘要后再导入")
    key = str(body.get("request_key") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", key):
        raise WriterStoreError("request_key 必须为 8–80 位字母、数字、下划线或连字符")
    summary = normalize_summary(body.get("summary"))
    fingerprint = str(body.get("fingerprint") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise WriterStoreError("请先预检所选稿件，再确认导入")
    identity = {key: body.get(key) for key in
                ("book_id", "source_kind", "end_chapter_no", "manuscripts_by_chapter", "title", "fingerprint")}
    identity["summary"] = summary
    return key, _digest(identity), summary


def find_existing(store: Any, key: str, request_hash: str) -> dict[str, Any] | None:
    with store._lock:
        if not store._root.exists():
            return None
        for path in store._root.iterdir():
            if not path.is_dir() or not BOOK_ID_PATTERN.fullmatch(path.name):
                continue
            try:
                manifest = store._load_manifest(path.name)
            except (LookupError, WriterStoreError):
                continue
            if manifest.get("novel_import_request_key") != key:
                continue
            if manifest.get("novel_import_request_hash") != request_hash:
                raise WriterConflictError("此导入请求已被使用，但输入不同；请重新预检后发起新请求")
            continuation = manifest.get("continuation") or {}
            return {"book": store._public_manifest(manifest),
                    "new_chapter_no": int(continuation["new_chapter_no"]), "duplicate": True}
    return None


def create_continuation(store: Any, preview: dict[str, Any], summary: dict[str, str], *,
                        title: str, request_key: str, request_hash: str) -> dict[str, Any]:
    """独立暂存目录全部写完才发布；失败只清理本轮未发布的目录。"""
    chapters = preview["_chapters"]
    book_title = title.strip() or (str(preview.get("title") or "小说") + " · 续写")
    if len(book_title) > TITLE_MAX:
        raise WriterStoreError(f"书名不能超过 {TITLE_MAX} 字符")
    with store._lock:
        existing = find_existing(store, request_key, request_hash)
        if existing is not None:
            return existing
        now = _now_ms()
        book_id = store._new_book_id()
        staging = store._root / f".{uuid.uuid4().hex}.tmp"
        final = store._root / book_id
        continuation = {"summary": summary, "source_book_id": preview["source_book_id"],
                        "source_kind": preview["source_kind"], "imported_at": now,
                        "end_chapter_no": preview["end_chapter_no"],
                        "new_chapter_no": preview["new_chapter_no"],
                        "fingerprint": preview["fingerprint"], "coverage": preview["coverage"]}
        manifest = {"schema_version": SCHEMA_VERSION, "revision": 1, "id": book_id,
                    "title": book_title, "chapter_ids": [], "setup_versions": [], "outline_versions": [],
                    "current_setup_version_id": "", "current_outline_version_id": "", "active_pipeline": None,
                    "created_at": now, "updated_at": now, "continuation": continuation,
                    "novel_import_request_key": request_key, "novel_import_request_hash": request_hash}
        try:
            for folder in ("setup", "outlines", "chapters", "chapter-versions"):
                (staging / folder).mkdir(parents=True, exist_ok=False)
            setup = normalize_setup({"title": book_title, "idea": "按人工确认的现状和后续大纲承接已有小说，不重演旧章。",
                                     "continuation_summary": summary})
            setup_id = store._write_setup_locked(staging, manifest, setup, kind="novel_import", task_id="")
            outline_rows = [{"no": int(row["i"]), "title": row["title"], "beat": "", "hook": ""}
                            for row in chapters]
            new_no = int(preview["new_chapter_no"])
            outline_rows.append({"no": new_no, "title": f"第 {new_no} 章", "beat": summary["next_outline"], "hook": ""})
            outline_id = store._write_outline_locked(staging, manifest, outline_rows, kind="novel_import",
                                                     task_id="", source_setup_version_id=setup_id)
            for row in chapters:
                no, content = int(row["i"]), str(row["content"])
                if not content.strip() or len(content) > CHAPTER_CONTENT_MAX_CHARS:
                    raise WriterStoreError(f"第 {no} 章正文为空或超过单章 {CHAPTER_CONTENT_MAX_CHARS} 字上限")
                source: WriterNovelChapterSource = {"source_book_id": preview["source_book_id"],
                          "source_manuscript_id": row["manuscript_id"], "source_chapter_no": no,
                          "source_kind": preview["source_kind"], "imported_at": now}
                vid = new_version_id("cv")
                record = {"id": vid, "version": 1, "no": no, "kind": "novel_import", "updated_at": now,
                          "task_id": "", "source_setup_version_id": setup_id, "source_outline_version_id": outline_id,
                          "finish_reason": "", "note": "拆书匠所选稿件只读副本", "content": content,
                          "len": len(content), "read_only": True, "source": source}
                store._write_json(store._chapter_version_path(staging, no, vid), record)
                metadata = {key: record[key] for key in ("id", "version", "kind", "updated_at", "task_id", "len", "finish_reason", "read_only", "source")}
                index = {"no": no, "title": row["title"], "beat": "", "hook": "", "revision": 1,
                         "version_ids": [metadata], "current_version_id": vid, "created_at": now,
                         "updated_at": now, "read_only": True, "source": source}
                store._write_chapter_index(staging, index)
                manifest["chapter_ids"].append(no)
            store._ensure_chapter_index_locked(staging, manifest, new_no, title=f"第 {new_no} 章",
                                               beat=summary["next_outline"], hook="")
            store._write_json(staging / "book.json", manifest)
            store._check_book_capacity(staging)
            os.replace(staging, final)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return {"book": store._public_manifest(manifest), "new_chapter_no": new_no, "duplicate": False}
