"""Manual editor snapshots are independent of generated result artifacts."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from naiba.core.contracts import WRITER_MANUAL_DRAFT_KEY
from naiba.writer.writeback import digest, durable_write, json_bytes


def save(store: Any, book_id: str, chapter_no: int, *, content: Any,
         expected_revision: Any = None) -> dict[str, Any]:
    from naiba.writer.store import CHAPTER_CONTENT_MAX_CHARS, WriterConflictError, WriterStoreError
    with store._lock:
        manifest = store._load_manifest(book_id)
        if chapter_no not in {int(no) for no in manifest.get("chapter_ids") or []}:
            raise LookupError("章节不存在")
        if not isinstance(content, str) or not content.strip():
            raise WriterStoreError("章节正文不能为空")
        if len(content) > CHAPTER_CONTENT_MAX_CHARS:
            raise WriterStoreError("章节正文超过字数上限，已拒绝")
        book_dir = store._book_dir(book_id)
        chapter = store._load_chapter_index(book_dir, chapter_no)
        store._assert_chapter_editable(chapter)
        revision = int(chapter.get("revision") or 0)
        if expected_revision is not None and expected_revision != "":
            if isinstance(expected_revision, bool):
                raise WriterStoreError("revision 必须是整数")
            try:
                expected = int(expected_revision)
            except (ValueError, TypeError):
                raise WriterStoreError("revision 必须是整数") from None
            if expected != revision:
                raise WriterConflictError("章节已被修改过，请重新读取后再保存；编辑副本仍保留")
        directory = (book_dir / "manual-drafts" / f"{chapter_no:04d}").resolve()
        if not directory.is_relative_to(book_dir):
            raise WriterStoreError("草稿路径越界")
        draft_id = uuid.uuid4().hex
        path = directory / f"{draft_id}.txt"
        meta = directory / f"{draft_id}.json"
        data = content.encode("utf-8")
        store._check_book_capacity(book_dir, extra_bytes=len(data))
        record = {"version": 1, "id": draft_id, "book_id": book_id,
                  "chapter_no": chapter_no, "revision": revision,
                  "chars": len(content), "sha256": digest(data), "status": "pending"}
        try:
            durable_write(path, data)
            durable_write(meta, json_bytes(record))
            verified = path.read_bytes()
            if digest(verified) != record["sha256"] or len(verified.decode("utf-8")) != record["chars"]:
                raise WriterStoreError("草稿读回校验失败，未创建正文版本")
            with store.writeback_transaction(book_id):
                result = store.save_chapter_version(
                    book_id, chapter_no, content=verified.decode("utf-8"), kind="manual",
                    expected_revision=revision, manual_draft=record,
                )
                committed = {**record, "status": "committed",
                             "version_id": str((result.get("version") or {}).get("id") or "")}
                store._write_json(meta, committed)
        except OSError as exc:
            raise WriterStoreError("草稿或正文写入失败，编辑副本保留，请稍后重试") from exc
        # The immutable chapter version now owns the full text and provenance.
        # Successful transient copies need not accumulate; failed snapshots stay.
        try:
            path.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
        except OSError:
            pass
        result[WRITER_MANUAL_DRAFT_KEY] = {"id": draft_id, "chars": record["chars"],
                                          "sha256": record["sha256"], "status": "committed"}
        return result
