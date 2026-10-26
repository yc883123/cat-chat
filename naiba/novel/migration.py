# -*- coding: utf-8 -*-
"""老作品（schema 1）升级到 schema 2：把单一 `content` 拆成独立稿件记录。

升级必须诚实（《创作场原文、改编稿与记录管理整改计划》§4.3）：

- 旧章节文件里的 `content` 是「最后一次保存的正文」，它可能已经被用户改过，
  所以默认只能标成 `legacy`（升级前保存的正文），**不能伪称是原文**。
- 只有当导入暂存的源文件还在、且能用原有拆章参数重新拆出**逐字一致**的对应章节时，
  才把该章恢复成真正的原文。
- 源文件缺失或对不上时不猜、不覆盖，只把该章标成 `legacy` 并记录原因。
- 升级失败不破坏旧作品：先写计划文件，再写稿件文件与章节索引，最后原子替换
  `book.json`（切换点）。中途失败时 `book.json` 仍是 schema 1，重跑会复用同一份
  计划（含同样的稿件 ID），因此幂等。

计划文件 `_upgrade.json` 是重试的唯一依据：它记录了每章的旧标题、旧正文和要写入的
稿件记录。重跑时若章节文件已经被写成 schema 2（没有 `content`），也仍然能从计划里
拿到正文，不会丢内容。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from naiba.core import novel_split
from naiba.novel import manuscripts
from naiba.tools.providers.novel import NovelError, read_text_auto

UPGRADE_PLAN = "_upgrade.json"


class MigrationError(ValueError):
    """升级失败（用户可读；不破坏旧数据）。"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _read_import_meta(imports_dir: Path) -> list[dict[str, Any]]:
    """枚举仍然存在的导入暂存（按修改时间新→旧）。"""
    rows: list[dict[str, Any]] = []
    if not imports_dir.is_dir():
        return rows
    for path in imports_dir.iterdir():
        meta_path = path / "meta.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(meta, dict):
            rows.append({"dir": path, "meta": meta})
    rows.sort(key=lambda row: float(row["meta"].get("created_at") or 0), reverse=True)
    return rows


def _recover_originals(source: dict[str, Any], chapters: dict[int, dict[str, Any]],
                       imports_dir: Path) -> tuple[dict[int, str], dict[str, Any]]:
    """尝试从导入暂存恢复真正的原文。

    返回 ``(可恢复的章号 → 正文, 说明)``。匹配条件必须是：
    同名源文件、同一拆章参数、重新拆分后**序号+标题+正文**都逐字一致。
    任何一条对不上就整章不恢复，绝不猜。
    """
    name = str(source.get("name") or "")
    try:
        max_len = int(source.get("max_len") or novel_split.DEFAULT_MAX_LEN)
    except (TypeError, ValueError):
        max_len = novel_split.DEFAULT_MAX_LEN
    note: dict[str, Any] = {"checked_imports": 0, "matched_import": "", "reason": ""}
    if not chapters:
        # 没有待核对章节（上次升级已经写完），不用给理由。
        return {}, note
    if not name:
        note["reason"] = "作品没有记录源文件名，无法核对原文"
        return {}, note
    candidates = [
        row for row in _read_import_meta(imports_dir)
        if str(row["meta"].get("name") or "") == name
        and int(row["meta"].get("max_len") or 0) == max_len
    ]
    if not candidates:
        note["reason"] = "导入暂存已过期或被清理，无法核对原文"
        return {}, note
    recovered: dict[int, str] = {}
    for candidate in candidates:
        note["checked_imports"] += 1
        source_path = candidate["dir"] / "source.txt"
        if not source_path.is_file():
            continue
        try:
            text, _encoding = read_text_auto(source_path)
        except NovelError:
            continue
        try:
            split_rows = novel_split.split_chapters(text, max_len)
        except (ValueError, TypeError):
            continue
        hits: dict[int, str] = {}
        for chapter_no, chapter in chapters.items():
            index = chapter_no - 1
            if index < 0 or index >= len(split_rows):
                continue
            row = split_rows[index]
            if (str(row.get("title") or "") == str(chapter.get("title") or "")
                    and str(row.get("body") or "") == str(chapter.get("content") or "")):
                hits[chapter_no] = str(row.get("body") or "")
        if len(hits) == len(chapters) and hits:
            note["matched_import"] = str(candidate["meta"].get("import_id") or candidate["dir"].name)
            return hits, note
    note["reason"] = "源文件重新拆分后与已保存章节不完全一致，无法确认为原文"
    return recovered, note


def _read_chapter_source(book_dir: Path, chapter_no: int) -> dict[str, Any] | None:
    """读一章的「升级前正文」。

    正常是 schema 1 的 `content`；但如果上次升级已经写了章节索引、只是清单没切
    过来（崩溃点正好在中间），就要从已写入的稿件里取正文，不能让这一章被当成
    损坏数据 —— 否则升级永远重试不过去。
    """
    path = book_dir / "chapters" / f"{chapter_no:04d}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    title = str(payload.get("title") or "")
    if isinstance(payload.get("content"), str):
        return {
            "title": title,
            "content": str(payload.get("content") or ""),
            "revision": int(payload.get("revision") or 0),
            "existing_manuscripts": [],
        }
    ids = payload.get("manuscript_ids")
    if not isinstance(ids, list) or not ids:
        return None
    existing: list[dict[str, Any]] = []
    for manuscript_id in ids:
        manuscript_path = book_dir / "manuscripts" / f"{chapter_no:04d}" / f"{manuscript_id}.json"
        try:
            record = json.loads(manuscript_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(record, dict) or not isinstance(record.get("content"), str):
            return None
        existing.append(record)
    base_id = str(payload.get("base_manuscript_id") or "")
    base = next((row for row in existing if str(row.get("id") or "") == base_id), existing[0])
    return {
        "title": title,
        "content": str(base.get("content") or ""),
        "revision": int(payload.get("revision") or 0),
        "existing_manuscripts": existing,
        "base_kind": str(base.get("kind") or ""),
    }


def build_plan(book_dir: Path, manifest: dict[str, Any],
               imports_dir: Path) -> dict[str, Any]:
    """生成升级计划（纯读，不写盘）。"""
    chapters: dict[int, dict[str, Any]] = {}
    broken: list[int] = []
    for chapter_no in (int(n) for n in manifest.get("chapter_ids") or []):
        row = _read_chapter_source(book_dir, chapter_no)
        if row is None:
            broken.append(chapter_no)
            continue
        chapters[chapter_no] = row
    if broken:
        # 坏章节不补空正文：直接拒绝升级，让用户先处理数据异常。
        raise MigrationError(
            "这些章节的文件损坏或缺少正文，无法升级：" + "、".join(f"第 {n} 章" for n in broken)
        )
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    # 已经迁移过的章不再参与原文核对：它们的稿件记录已经是权威结果。
    pending = {no: row for no, row in chapters.items() if not row.get("existing_manuscripts")}
    recovered, note = _recover_originals(source, pending, imports_dir)
    now = _now_ms()
    plan_chapters: list[dict[str, Any]] = []
    for chapter_no in sorted(chapters):
        chapter = chapters[chapter_no]
        existing = chapter.get("existing_manuscripts") or []
        if existing:
            # 上次升级已经写过稿件：原样复用，不重新生成 ID。
            plan_chapters.append({
                "i": chapter_no,
                "title": chapter["title"],
                "revision": max(1, chapter["revision"]),
                "manuscripts": existing,
                "already_migrated": True,
            })
            continue
        original_content = recovered.get(chapter_no)
        if original_content is not None:
            record = manuscripts.build(
                chapter_no=chapter_no, kind=manuscripts.KIND_ORIGINAL,
                content=original_content, created_at=now,
            )
        else:
            record = manuscripts.build(
                chapter_no=chapter_no, kind=manuscripts.KIND_LEGACY,
                content=chapter["content"], created_at=now,
            )
        plan_chapters.append({
            "i": chapter_no,
            "title": chapter["title"],
            "revision": max(1, chapter["revision"]),
            "manuscripts": [record],
        })
    restored = sorted(recovered)
    legacy = [row["i"] for row in plan_chapters
              if row["manuscripts"]
              and str(row["manuscripts"][0].get("kind") or "") == manuscripts.KIND_LEGACY]
    # 已迁移章节的 kind 以落盘记录为准，不因为这次没核对原文就改标。
    for row in plan_chapters:
        if row.get("already_migrated"):
            kind = str(row["manuscripts"][0].get("kind") or "")
            if kind == manuscripts.KIND_ORIGINAL and row["i"] not in restored:
                restored.append(row["i"])
            elif kind == manuscripts.KIND_LEGACY and row["i"] not in legacy:
                legacy.append(row["i"])
    return {
        "schema_version": manuscripts.SCHEMA_VERSION,
        "created_at": now,
        "origin_recovery": {
            "restored": sorted(restored),
            "legacy": sorted(legacy),
            **note,
        },
        "chapters": plan_chapters,
    }


def load_plan(book_dir: Path) -> dict[str, Any] | None:
    path = book_dir / UPGRADE_PLAN
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def clear_plan(book_dir: Path) -> None:
    try:
        (book_dir / UPGRADE_PLAN).unlink(missing_ok=True)
    except OSError:
        pass
