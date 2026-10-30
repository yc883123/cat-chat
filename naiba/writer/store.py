# -*- coding: utf-8 -*-
"""织书台存储层：原创作品、设定/大纲/正文版本、导入暂存与原子持久化。

设计要点（实施计划 §5）：

1. **作品独立目录**：`data/studio/writer/<book-id>/`，与拆书匠（`studio/novel/`）
   互不混库；删除、导出、导入的作用范围都严格限定在本域根目录内。
2. **单章一文件**：`chapters/0001.json` 只存索引（标题、梗概、版本指针），
   正文在 `chapter-versions/0001/<id>.json`——改一章不重写整本，坏文件不拖累其它章。
3. **版本不可变**：AI 写回、人工保存、恢复旧版本都产生新版本文件并切换当前指针，
   原版本内容绝不原地改写；重写先跑任务，成功后才切换，禁止先清空旧正文。
4. **原子写**：同目录临时文件 + `os.replace`（照 `naiba/novel/service.py` 的范式）。
5. **revision 乐观并发**：作品级 `revision` 保护设定、大纲与章节顺序；每章另有
   `chapter.revision`，写某一章不因用户编辑别的章而产生假冲突。
6. **导入暂存**：启元 `作品存档.json` 先落 `_imports/<import-id>/`，预览确认后才
   创建正式作品目录；导入不修改原文件、不迁入旧任务历史。

目录布局::

    studio/writer/<book-id>/
      book.json                         作品索引、当前版本指针、章节顺序、活动流水线
      setup/<version-id>.json           设定版本
      outlines/<version-id>.json        大纲版本
      chapters/0001.json                章节索引与版本元数据，不存整本正文
      chapter-versions/0001/<id>.json   单章正文版本
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

# 作品清单版本：首版为 1。
SCHEMA_VERSION = 1
# 作品 / 导入 ID：前缀 + 日期 + 随机后缀，禁止用户输入参与路径（与拆书匠同构）。
BOOK_ID_PATTERN = re.compile(r"^wb-[0-9]{8}-[0-9a-f]{12}$")
IMPORT_ID_PATTERN = re.compile(r"^wi-[0-9]{8}-[0-9a-f]{12}$")
# 版本记录 ID：`su-` 设定 / `ol-` 大纲 / `cv-` 正文 / `pl-` 流水线，删除后不复用。
VERSION_ID_PATTERN = re.compile(r"^(su|ol|cv)-[0-9a-f]{12}$")
PIPELINE_ID_PATTERN = re.compile(r"^pl-[0-9a-f]{12}$")
CHAPTER_FILE_PATTERN = re.compile(r"^([0-9]{4})\.json$")

TITLE_MAX = 200
# 导入上传上限：存档是完整应用状态 JSON，正常远小于 20 MiB，超了如实拒绝。
MAX_IMPORT_BYTES = 20 * 1024 * 1024
# 作品目录总量上限：导入、编辑、AI 写回共用这一条，超了整项拒绝。
MAX_BOOK_BYTES = 256 * 1024 * 1024
# 导入暂存保留时长：确认入库前一直有效，失败导入不长期占盘。
IMPORT_TTL_SECONDS = 24 * 3600
# 单章正文字数上限（防御性）：模型单章产出远小于这个值，超过说明输入被污染。
CHAPTER_CONTENT_MAX_CHARS = 200_000
# 设定 / 大纲单版本文本槽上限（防御性，与 prompts 的槽位预算分层）。
SETUP_TEXT_MAX_CHARS = 60_000

# 流水线状态：running 推进中；done/failed/cancelled/interrupted/conflict 为终态。
PIPELINE_STATUSES = ("running", "done", "failed", "cancelled", "interrupted", "conflict")


class WriterStoreError(ValueError):
    """织书台域的输入/状态错误（用户可读，由 API 边界转成 4xx）。"""


class WriterConflictError(WriterStoreError):
    """乐观并发冲突：revision 对不上，调用方应重新读取后再改（对应 409）。"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _hex12() -> str:
    return uuid.uuid4().hex[:12]


def new_version_id(prefix: str) -> str:
    return f"{prefix}-{_hex12()}"


def clean_title(name: str) -> str:
    """书名落文件名时清洗：只用于导出文件名等展示场景，不作路径主键。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t\x00-\x1f]', "_", str(name or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:80] or "未命名"


def _clamp_text(value: Any, limit: int, *, what: str) -> str:
    text = str(value or "")
    if len(text) > limit:
        raise WriterStoreError(f"{what}超过 {limit} 字符上限")
    return text


def normalize_setup(payload: Any, *, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """把任意输入收敛成一份合法的设定内容（版本落盘前统一过这里）。

    未知字段丢弃、类型不对回落空值——**不猜、不编**；数量上限（角色 1–10、
    关键词 5）与提示词契约一致。
    """
    raw = dict(payload) if isinstance(payload, dict) else {}
    genre_raw = raw.get("genre") if isinstance(raw.get("genre"), dict) else {}
    tags_raw = raw.get("genre_tags") if isinstance(raw.get("genre_tags"), list) else []
    tags: list[str] = []
    for item in tags_raw:
        text = str(item or "").strip()
        if text and text not in tags:
            tags.append(text)
        if len(tags) >= 5:
            break
    characters: list[dict[str, str]] = []
    chars_raw = raw.get("characters") if isinstance(raw.get("characters"), list) else []
    for item in chars_raw:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        characters.append({
            "name": name[:60],
            "role": str(item.get("role") or "").strip()[:40],
            "identity": str(item.get("identity") or "").strip()[:200],
            "goal": str(item.get("goal") or "").strip()[:200],
            "traits": str(item.get("traits") or "").strip()[:200],
            "speech": str(item.get("speech") or "").strip()[:200],
            "arc": str(item.get("arc") or "").strip()[:200],
            "background": str(item.get("background") or "").strip()[:1000],
        })
        if len(characters) >= 10:
            break
    base = base if isinstance(base, dict) else {}
    merged = {
        "idea": _clamp_text(raw.get("idea") if raw.get("idea") is not None else base.get("idea"),
                            SETUP_TEXT_MAX_CHARS, what="创作创意"),
        "genre": {
            "channel": str(genre_raw.get("channel") or "").strip()[:20],
            "cat": str(genre_raw.get("cat") or "").strip()[:40],
            "sub_cat": str(genre_raw.get("sub_cat") or "").strip()[:40],
            "tone": str(genre_raw.get("tone") or "").strip()[:40],
            "hook": str(genre_raw.get("hook") or "").strip()[:200],
        },
        "genre_tags": tags,
        "title": _clamp_text(raw.get("title") if raw.get("title") is not None else base.get("title"),
                             120, what="书名"),
        "title_candidates": [
            str(item or "").strip()[:120]
            for item in (raw.get("title_candidates") if isinstance(raw.get("title_candidates"), list) else [])
            if str(item or "").strip()
        ][:10],
        "blurb": _clamp_text(raw.get("blurb") if raw.get("blurb") is not None else base.get("blurb"),
                             2000, what="简介"),
        "blurb_candidates": [
            str(item or "").strip()[:2000]
            for item in (raw.get("blurb_candidates") if isinstance(raw.get("blurb_candidates"), list) else [])
            if str(item or "").strip()
        ][:6],
        "worldview": _clamp_text(
            raw.get("worldview") if raw.get("worldview") is not None else base.get("worldview"),
            SETUP_TEXT_MAX_CHARS, what="世界观"),
        "characters": characters,
        "style": str(raw.get("style") if raw.get("style") is not None else base.get("style") or "").strip()[:20],
        "constraints": {
            "vibe": _clamp_text((raw.get("constraints") or {}).get("vibe")
                                if isinstance(raw.get("constraints"), dict) else "",
                                200, what="参考气质"),
            "avoid": _clamp_text((raw.get("constraints") or {}).get("avoid")
                                 if isinstance(raw.get("constraints"), dict) else "",
                                 200, what="回避项"),
        },
    }
    return merged


def normalize_outline_chapters(raw: Any, *, min_chapters: int = 1,
                               max_chapters: int = 9999) -> list[dict[str, Any]]:
    """把大纲章节数组收敛成 `{no,title,beat,hook}` 列表；章号必须唯一且连续。"""
    if not isinstance(raw, list):
        raise WriterStoreError("大纲章节必须是数组")
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise WriterStoreError("大纲章节必须是对象数组")
        no_raw = item.get("no", len(rows) + 1)
        if isinstance(no_raw, bool) or not isinstance(no_raw, int):
            raise WriterStoreError("章节号必须是整数")
        title = str(item.get("title") or "").strip()
        if not title:
            raise WriterStoreError("章节标题不能为空")
        rows.append({
            "no": no_raw,
            "title": title[:200],
            "beat": str(item.get("beat") or "").strip()[:2000],
            "hook": str(item.get("hook") or "").strip()[:500],
        })
    if not min_chapters <= len(rows) <= max_chapters:
        raise WriterStoreError(f"大纲章节数需要在 {min_chapters}–{max_chapters} 之间（当前 {len(rows)}）")
    nos = [row["no"] for row in rows]
    if len(set(nos)) != len(nos):
        raise WriterStoreError("大纲章节号有重复")
    if nos != list(range(1, len(rows) + 1)):
        raise WriterStoreError("大纲章节号必须从 1 开始连续")
    return rows


class WriterStore:
    """作品、章节与版本的落盘（线程锁串行化写路径；读为单章独立小文件）。"""

    def __init__(self, data_dir: Path):
        self._root = Path(data_dir) / "studio" / "writer"
        self._imports = self._root / "_imports"
        self._lock = threading.RLock()

    # ---- 路径与安全 ----

    def root(self) -> Path:
        return self._root

    def _book_dir(self, book_id: str) -> Path:
        bid = str(book_id or "").strip()
        if not BOOK_ID_PATTERN.match(bid):
            raise WriterStoreError("作品 ID 非法")
        path = (self._root / bid).resolve()
        root = self._root.resolve()
        if not path.is_relative_to(root) or path == root:
            raise WriterStoreError("作品路径越界")
        return path

    def _import_dir(self, import_id: str) -> Path:
        iid = str(import_id or "").strip()
        if not IMPORT_ID_PATTERN.match(iid):
            raise WriterStoreError("导入 ID 非法")
        path = (self._imports / iid).resolve()
        if not path.is_relative_to(self._imports.resolve()) or path == self._imports.resolve():
            raise WriterStoreError("导入路径越界")
        return path

    def _setup_path(self, book_dir: Path, version_id: str) -> Path:
        vid = str(version_id or "").strip()
        if not VERSION_ID_PATTERN.match(vid) or not vid.startswith("su-"):
            raise WriterStoreError("设定版本 ID 非法")
        folder = (book_dir / "setup").resolve()
        path = (folder / f"{vid}.json").resolve()
        if not path.is_relative_to(folder):
            raise WriterStoreError("设定版本路径越界")
        return path

    def _outline_path(self, book_dir: Path, version_id: str) -> Path:
        vid = str(version_id or "").strip()
        if not VERSION_ID_PATTERN.match(vid) or not vid.startswith("ol-"):
            raise WriterStoreError("大纲版本 ID 非法")
        folder = (book_dir / "outlines").resolve()
        path = (folder / f"{vid}.json").resolve()
        if not path.is_relative_to(folder):
            raise WriterStoreError("大纲版本路径越界")
        return path

    def _chapter_dir(self, book_dir: Path, chapter_no: int) -> Path:
        if isinstance(chapter_no, bool) or not isinstance(chapter_no, int):
            raise WriterStoreError("章节号必须是整数")
        if chapter_no < 1 or chapter_no > 9999:
            raise WriterStoreError("章节号必须在 1–9999 之间")
        root = (book_dir / "chapters").resolve()
        path = (root / f"{chapter_no:04d}.json").resolve()
        if not path.is_relative_to(root):
            raise WriterStoreError("章节路径越界")
        return path

    def _chapter_version_dir(self, book_dir: Path, chapter_no: int) -> Path:
        if isinstance(chapter_no, bool) or not isinstance(chapter_no, int):
            raise WriterStoreError("章节号必须是整数")
        if chapter_no < 1 or chapter_no > 9999:
            raise WriterStoreError("章节号必须在 1–9999 之间")
        root = (book_dir / "chapter-versions" / f"{chapter_no:04d}").resolve()
        versions_root = (book_dir / "chapter-versions").resolve()
        if not root.is_relative_to(versions_root):
            raise WriterStoreError("章节版本路径越界")
        return root

    def _chapter_version_path(self, book_dir: Path, chapter_no: int, version_id: str) -> Path:
        vid = str(version_id or "").strip()
        if not VERSION_ID_PATTERN.match(vid) or not vid.startswith("cv-"):
            raise WriterStoreError("正文版本 ID 非法")
        folder = self._chapter_version_dir(book_dir, chapter_no)
        path = (folder / f"{vid}.json").resolve()
        if not path.is_relative_to(folder):
            raise WriterStoreError("正文版本路径越界")
        return path

    def _new_book_id(self) -> str:
        return f"wb-{time.strftime('%Y%m%d')}-{_hex12()}"

    def _new_import_id(self) -> str:
        return f"wi-{time.strftime('%Y%m%d')}-{_hex12()}"

    # ---- 原子读写 ----

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        """同目录临时文件 + 原子替换；失败不覆盖已有内容。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{uuid.uuid4().hex}.tmp")
        try:
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp_path, path)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _read_json(path: Path, *, what: str) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise LookupError(f"{what}不存在") from exc
        except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
            raise WriterStoreError(f"{what}文件损坏：{path.name}") from exc
        if not isinstance(payload, dict):
            raise WriterStoreError(f"{what}文件损坏：{path.name}")
        return payload

    @staticmethod
    def _dir_bytes(path: Path) -> int:
        total = 0
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
        return total

    def _check_book_capacity(self, book_dir: Path, extra_bytes: int = 0) -> None:
        if not book_dir.exists():
            return
        used = self._dir_bytes(book_dir)
        if used + extra_bytes > MAX_BOOK_BYTES:
            raise WriterStoreError(
                f"作品目录会超过 {MAX_BOOK_BYTES // 1024 // 1024} MB 上限"
                f"（当前 {used / 1024 / 1024:.1f} MB）。请删除多余章节或版本后再试。"
            )

    # ---- 清单 ----

    def _load_manifest(self, book_id: str) -> dict[str, Any]:
        book_dir = self._book_dir(book_id)
        manifest = self._read_json(book_dir / "book.json", what="作品清单")
        version = manifest.get("schema_version")
        if version != SCHEMA_VERSION:
            raise WriterStoreError(f"作品数据版本不支持：{version}（当前支持 {SCHEMA_VERSION}）")
        if str(manifest.get("id") or "") != book_id:
            raise WriterStoreError("作品清单与目录不一致")
        if not isinstance(manifest.get("chapter_ids"), list):
            raise WriterStoreError("作品清单缺少章节列表")
        return manifest

    def _save_manifest(self, book_dir: Path, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = _now_ms()
        self._write_json(book_dir / "book.json", manifest)

    def _bump_manifest(self, book_dir: Path, manifest: dict[str, Any]) -> None:
        manifest["revision"] = int(manifest.get("revision") or 0) + 1
        self._save_manifest(book_dir, manifest)

    @staticmethod
    def _check_revision(manifest: dict[str, Any], expected: Any, *, what: str = "作品") -> None:
        if expected is None or expected == "":
            return
        if isinstance(expected, bool):
            raise WriterStoreError("revision 必须是整数")
        try:
            expected_no = int(expected)
        except (TypeError, ValueError):
            raise WriterStoreError("revision 必须是整数") from None
        actual = int(manifest.get("revision") or 0)
        if expected_no != actual:
            raise WriterConflictError(
                f"{what}已被修改过（你看到的是第 {expected_no} 版，现在是第 {actual} 版），请重新读取后再操作"
            )

    # ---- 对外视图 ----

    @staticmethod
    def _public_manifest(manifest: dict[str, Any], *, include_versions: bool = True) -> dict[str, Any]:
        setup_versions = [row for row in (manifest.get("setup_versions") or []) if isinstance(row, dict)]
        outline_versions = [row for row in (manifest.get("outline_versions") or []) if isinstance(row, dict)]
        pipeline = manifest.get("active_pipeline")
        published_setup = [
            {"id": str(row.get("id") or ""), "version": int(row.get("version") or 0),
             "kind": str(row.get("kind") or ""), "updated_at": int(row.get("updated_at") or 0),
             "task_id": str(row.get("task_id") or ""), "len": int(row.get("len") or 0),
             "title": str(row.get("title") or "")}
            for row in setup_versions
        ]
        published_outline = [
            {"id": str(row.get("id") or ""), "version": int(row.get("version") or 0),
             "kind": str(row.get("kind") or ""), "updated_at": int(row.get("updated_at") or 0),
             "task_id": str(row.get("task_id") or ""), "len": int(row.get("len") or 0),
             "chapter_count": int(row.get("chapter_count") or 0),
             "source_setup_version_id": str(row.get("source_setup_version_id") or "")}
            for row in outline_versions
        ]
        payload = {
            "id": str(manifest.get("id") or ""),
            "title": str(manifest.get("title") or ""),
            "revision": int(manifest.get("revision") or 0),
            "schema_version": int(manifest.get("schema_version") or 0),
            "chapter_count": len([n for n in manifest.get("chapter_ids") or []]),
            "current_setup_version_id": str(manifest.get("current_setup_version_id") or ""),
            "current_outline_version_id": str(manifest.get("current_outline_version_id") or ""),
            "setup_versions": published_setup if include_versions else [],
            "outline_versions": published_outline if include_versions else [],
            "active_pipeline": dict(pipeline) if isinstance(pipeline, dict) else None,
            "created_at": int(manifest.get("created_at") or 0),
            "updated_at": int(manifest.get("updated_at") or 0),
        }
        return payload

    # ---- 作品 CRUD ----

    def create_book(self, *, title: str, setup: Any = None,
                    kind: str = "manual") -> dict[str, Any]:
        """新建原创作品：先写临时目录，全部成功后原子提交为正式目录。

        `setup` 给了就同时落一份设定版本（kind 形如 manual/imported/template）。
        """
        book_title = str(title or "").strip()
        if not book_title:
            book_title = "未命名作品"
        if len(book_title) > TITLE_MAX:
            raise WriterStoreError(f"书名不能超过 {TITLE_MAX} 字符")
        book_id = self._new_book_id()
        final_dir = self._root / book_id
        staging_dir = self._root / f".{uuid.uuid4().hex}.tmp"
        now = _now_ms()
        try:
            (staging_dir / "setup").mkdir(parents=True, exist_ok=False)
            (staging_dir / "outlines").mkdir(parents=True, exist_ok=True)
            (staging_dir / "chapters").mkdir(parents=True, exist_ok=True)
            (staging_dir / "chapter-versions").mkdir(parents=True, exist_ok=True)
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "revision": 1,
                "id": book_id,
                "title": book_title,
                "current_setup_version_id": "",
                "current_outline_version_id": "",
                "setup_versions": [],
                "outline_versions": [],
                "chapter_ids": [],
                "active_pipeline": None,
                "created_at": now,
                "updated_at": now,
            }
            setup_content = normalize_setup(setup) if setup else None
            if setup_content is not None:
                self._write_setup_locked(staging_dir, manifest, setup_content, kind=kind, task_id="")
            self._write_json(staging_dir / "book.json", manifest)
            with self._lock:
                os.replace(staging_dir, final_dir)
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise
        return self._public_manifest(manifest)

    def list_books(self) -> dict[str, Any]:
        """作品列表：只读清单，不读设定与正文。"""
        corrupt: list[str] = []
        rows: list[dict[str, Any]] = []
        if self._root.exists():
            for path in sorted(self._root.iterdir()):
                if not path.is_dir() or not BOOK_ID_PATTERN.match(path.name):
                    continue
                try:
                    manifest = self._load_manifest(path.name)
                except (LookupError, WriterStoreError):
                    corrupt.append(path.name)
                    continue
                rows.append(self._public_manifest(manifest, include_versions=False))
        rows.sort(key=lambda row: int(row.get("updated_at") or 0), reverse=True)
        return {"books": rows, "corrupt": corrupt}

    def get_book(self, book_id: str) -> dict[str, Any]:
        """作品详情：清单 + 设定当前版内容摘要 + 章节索引（不含正文）。"""
        manifest = self._load_manifest(book_id)
        book_dir = self._book_dir(book_id)
        chapters: list[dict[str, Any]] = []
        broken: list[int] = []
        for chapter_no in (int(n) for n in manifest.get("chapter_ids") or []):
            try:
                index = self._load_chapter_index(book_dir, chapter_no)
            except (LookupError, WriterStoreError):
                broken.append(chapter_no)
                continue
            current_id = str(index.get("current_version_id") or "")
            current_len = 0
            current_kind = ""
            for row in index.get("version_ids") or []:
                vid = str(row.get("id") or "") if isinstance(row, dict) else ""
                if vid == current_id:
                    current_len = int(row.get("len") or 0) if isinstance(row, dict) else 0
                    current_kind = str(row.get("kind") or "") if isinstance(row, dict) else ""
                    break
            chapters.append({
                "no": chapter_no,
                "title": str(index.get("title") or ""),
                "beat": str(index.get("beat") or ""),
                "hook": str(index.get("hook") or ""),
                "revision": int(index.get("revision") or 0),
                "current_version_id": current_id,
                "current_kind": current_kind,
                "len": current_len,
                "version_count": len([r for r in index.get("version_ids") or [] if isinstance(r, dict)]),
            })
        setup = self.load_setup(book_id, manifest=manifest)
        outline = self.load_outline(book_id, manifest=manifest)
        return {
            "book": self._public_manifest(manifest),
            "setup": setup,
            "setup_current": {
                "id": str(manifest.get("current_setup_version_id") or ""),
            },
            "outline": outline,
            "outline_current": {
                "id": str(manifest.get("current_outline_version_id") or ""),
            },
            "chapters": chapters,
            "broken": broken,
            "location": str(book_dir),
        }

    def rename_book(self, book_id: str, *, title: str, expected_revision: Any = None) -> dict[str, Any]:
        manifest = self._load_manifest(book_id)
        self._check_revision(manifest, expected_revision)
        book_dir = self._book_dir(book_id)
        new_title = str(title or "").strip()
        if not new_title:
            raise WriterStoreError("书名不能为空")
        if len(new_title) > TITLE_MAX:
            raise WriterStoreError(f"书名不能超过 {TITLE_MAX} 字符")
        manifest["title"] = new_title
        with self._lock:
            self._bump_manifest(book_dir, manifest)
        return self._public_manifest(manifest)

    def delete_book(self, book_id: str) -> dict[str, Any]:
        """删作品：作用范围严格限定为该作品目录，不波及拆书匠。"""
        book_dir = self._book_dir(book_id)
        if not book_dir.exists():
            raise LookupError("作品不存在")
        with self._lock:
            shutil.rmtree(book_dir, ignore_errors=False)
        return {"ok": True}

    # ---- 设定版本 ----

    def _write_setup_locked(self, book_dir: Path, manifest: dict[str, Any],
                            content: dict[str, Any], *, kind: str, task_id: str) -> str:
        versions = manifest.get("setup_versions") if isinstance(manifest.get("setup_versions"), list) else []
        version_no = (max((int(row.get("version") or 0) for row in versions if isinstance(row, dict)),
                          default=0) + 1)
        vid = new_version_id("su")
        record = {"id": vid, "version": version_no, "kind": str(kind or "manual"),
                  "updated_at": _now_ms(), "task_id": str(task_id or ""), **content}
        self._write_json(self._setup_path(book_dir, vid), record)
        title = str(content.get("title") or "")
        versions.append({"id": vid, "version": version_no, "kind": str(kind or "manual"),
                         "updated_at": record["updated_at"], "task_id": str(task_id or ""),
                         "len": len(json.dumps(content, ensure_ascii=False)), "title": title})
        manifest["setup_versions"] = versions
        manifest["current_setup_version_id"] = vid
        return vid

    def save_setup(self, book_id: str, *, content: Any, kind: str = "manual",
                   task_id: str = "", expected_revision: Any = None,
                   base_setup: dict[str, Any] | None = None) -> dict[str, Any]:
        """写一份新设定版本并切换当前指针；原版本内容不改动。"""
        manifest = self._load_manifest(book_id)
        self._check_revision(manifest, expected_revision)
        book_dir = self._book_dir(book_id)
        merged_base = base_setup if isinstance(base_setup, dict) else None
        normalized = normalize_setup(content, base=merged_base)
        with self._lock:
            vid = self._write_setup_locked(book_dir, manifest, normalized, kind=kind, task_id=task_id)
            self._bump_manifest(book_dir, manifest)
        return {"book": self._public_manifest(manifest), "setup": normalized,
                "setup_version_id": vid}

    def load_setup(self, book_id: str, *, manifest: dict[str, Any] | None = None,
                   version_id: str = "") -> dict[str, Any]:
        manifest = manifest or self._load_manifest(book_id)
        book_dir = self._book_dir(book_id)
        vid = str(version_id or "").strip() or str(manifest.get("current_setup_version_id") or "")
        if not vid:
            return {"id": "", "version": 0, "kind": "", "updated_at": 0, "task_id": "",
                    **normalize_setup({})}
        record = self._read_json(self._setup_path(book_dir, vid), what="设定版本")
        return record

    # ---- 大纲版本 ----

    def _write_outline_locked(self, book_dir: Path, manifest: dict[str, Any],
                              chapters: list[dict[str, Any]], *, kind: str, task_id: str,
                              source_setup_version_id: str) -> str:
        versions = manifest.get("outline_versions") if isinstance(manifest.get("outline_versions"), list) else []
        version_no = (max((int(row.get("version") or 0) for row in versions if isinstance(row, dict)),
                          default=0) + 1)
        vid = new_version_id("ol")
        record = {
            "id": vid, "version": version_no, "kind": str(kind or "generated"),
            "updated_at": _now_ms(), "task_id": str(task_id or ""),
            "source_setup_version_id": str(source_setup_version_id or ""),
            "chapters": chapters,
        }
        self._write_json(self._outline_path(book_dir, vid), record)
        versions.append({"id": vid, "version": version_no, "kind": str(kind or "generated"),
                         "updated_at": record["updated_at"], "task_id": str(task_id or ""),
                         "len": sum(len(row["title"] + row["beat"]) for row in chapters),
                         "chapter_count": len(chapters),
                         "source_setup_version_id": str(source_setup_version_id or "")})
        manifest["outline_versions"] = versions
        manifest["current_outline_version_id"] = vid
        return vid

    def save_outline(self, book_id: str, *, chapters: Any, kind: str = "generated",
                     task_id: str = "", source_setup_version_id: str = "",
                     expected_revision: Any = None,
                     sync: bool = False, allow_drop: bool = False) -> dict[str, Any]:
        """写一份新大纲版本；`sync=True` 时同步章节索引（计划 §5.3）。"""
        manifest = self._load_manifest(book_id)
        self._check_revision(manifest, expected_revision)
        book_dir = self._book_dir(book_id)
        rows = normalize_outline_chapters(chapters)
        with self._lock:
            vid = self._write_outline_locked(book_dir, manifest, rows, kind=kind,
                                             task_id=task_id,
                                             source_setup_version_id=source_setup_version_id)
            synced: list[int] = []
            if sync:
                synced = self._sync_chapters_locked(book_dir, manifest, rows, allow_drop=allow_drop)
            self._bump_manifest(book_dir, manifest)
        return {"book": self._public_manifest(manifest), "outline_version_id": vid,
                "synced_chapter_ids": synced}

    def load_outline(self, book_id: str, *, manifest: dict[str, Any] | None = None,
                     version_id: str = "") -> dict[str, Any]:
        manifest = manifest or self._load_manifest(book_id)
        book_dir = self._book_dir(book_id)
        vid = str(version_id or "").strip() or str(manifest.get("current_outline_version_id") or "")
        if not vid:
            return {"id": "", "version": 0, "kind": "", "updated_at": 0, "task_id": "",
                    "source_setup_version_id": "", "chapters": []}
        return self._read_json(self._outline_path(book_dir, vid), what="大纲版本")

    # ---- 章节索引同步（大纲 → 章节） ----

    def _load_chapter_index(self, book_dir: Path, chapter_no: int) -> dict[str, Any]:
        payload = self._read_json(self._chapter_dir(book_dir, chapter_no), what=f"第 {chapter_no} 章")
        if int(payload.get("no") or 0) != chapter_no:
            raise WriterStoreError(f"第 {chapter_no} 章文件编号不一致")
        return payload

    def _write_chapter_index(self, book_dir: Path, index: dict[str, Any]) -> None:
        self._write_json(self._chapter_dir(book_dir, int(index["no"])), index)

    def _ensure_chapter_index_locked(self, book_dir: Path, manifest: dict[str, Any],
                                     chapter_no: int, *, title: str, beat: str,
                                     hook: str) -> dict[str, Any]:
        """取章节索引；不存在（如导入的正文在大纲之外）时先建一个空索引。"""
        try:
            return self._load_chapter_index(book_dir, chapter_no)
        except LookupError:
            now = _now_ms()
            index = {
                "no": chapter_no, "title": title or f"第 {chapter_no} 章", "beat": beat,
                "hook": hook, "revision": 1, "version_ids": [], "current_version_id": "",
                "created_at": now, "updated_at": now,
            }
            self._write_chapter_index(book_dir, index)
            ids = [int(n) for n in manifest.get("chapter_ids") or []]
            if chapter_no not in ids:
                ids = sorted({*ids, chapter_no})
                manifest["chapter_ids"] = ids
            return index

    def _sync_chapters_locked(self, book_dir: Path, manifest: dict[str, Any],
                              rows: list[dict[str, Any]], *, allow_drop: bool) -> list[int]:
        """按大纲重建章节索引：更新标题/梗概/顺序，保留已有正文版本。

        大纲删除了已有正文的章节时：`allow_drop=False` 报错（要求用户单独确认），
        `allow_drop=True` 连同版本目录一起移除。
        """
        existing = {int(n) for n in manifest.get("chapter_ids") or []}
        wanted_nos = [int(row["no"]) for row in rows]
        by_no = {int(row["no"]): row for row in rows}
        # 删除保护：只有梗概、没有正文的章可以直接按大纲重建。
        for no in sorted(existing - set(wanted_nos)):
            versions = self._chapter_version_dir(book_dir, no)
            has_content = versions.exists() and any(versions.iterdir())
            if has_content and not allow_drop:
                raise WriterStoreError(
                    f"第 {no} 章已有正文，大纲里却没有它；请单独确认后重试（或先导出备份）"
                )
            with self._lock:
                shutil.rmtree(self._chapter_version_dir(book_dir, no), ignore_errors=True)
                self._chapter_dir(book_dir, no).unlink(missing_ok=True)
        ordered: list[int] = []
        now = _now_ms()
        for no in wanted_nos:
            row = by_no[no]
            if no in existing:
                index = self._load_chapter_index(book_dir, no)
                index["title"] = row["title"]
                index["beat"] = row["beat"]
                index["hook"] = row["hook"]
                self._write_chapter_index(book_dir, index)
            else:
                index = {
                    "no": no, "title": row["title"], "beat": row["beat"], "hook": row["hook"],
                    "revision": 1, "version_ids": [], "current_version_id": "",
                    "created_at": now, "updated_at": now,
                }
                self._write_chapter_index(book_dir, index)
            ordered.append(no)
        manifest["chapter_ids"] = ordered
        return ordered

    def sync_chapters(self, book_id: str, *, expected_revision: Any = None,
                      allow_drop: bool = False) -> dict[str, Any]:
        """用当前大纲重新同步章节索引（独立按钮入口，与生成时同步共用实现）。"""
        manifest = self._load_manifest(book_id)
        self._check_revision(manifest, expected_revision)
        outline = self.load_outline(book_id, manifest=manifest)
        rows = normalize_outline_chapters(outline.get("chapters") or [])
        if not rows:
            raise WriterStoreError("当前作品还没有大纲版本，无法同步章节")
        book_dir = self._book_dir(book_id)
        with self._lock:
            ordered = self._sync_chapters_locked(book_dir, manifest, rows, allow_drop=allow_drop)
            self._bump_manifest(book_dir, manifest)
        return {"book": self._public_manifest(manifest), "chapter_ids": ordered}

    # ---- 章节 ----

    def get_chapter(self, book_id: str, chapter_no: int, *,
                    manifest: dict[str, Any] | None = None) -> dict[str, Any]:
        """单章当前正文与版本清单（两段式：正文只有这里给）。"""
        manifest = manifest or self._load_manifest(book_id)
        if chapter_no not in {int(n) for n in manifest.get("chapter_ids") or []}:
            raise LookupError("章节不存在")
        book_dir = self._book_dir(book_id)
        index = self._load_chapter_index(book_dir, chapter_no)
        versions: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for row in index.get("version_ids") or []:
            if not isinstance(row, dict):
                continue
            versions.append(dict(row))
            if str(row.get("id") or "") == str(index.get("current_version_id") or ""):
                current = dict(row)
        content = ""
        if current:
            record = self._read_json(
                self._chapter_version_path(book_dir, chapter_no, current["id"]), what="正文版本",
            )
            content = str(record.get("content") or "")
        return {
            "no": chapter_no,
            "title": str(index.get("title") or ""),
            "beat": str(index.get("beat") or ""),
            "hook": str(index.get("hook") or ""),
            "revision": int(index.get("revision") or 0),
            "current_version_id": str(index.get("current_version_id") or ""),
            "content": content,
            "versions": versions,
        }

    def save_chapter_version(self, book_id: str, chapter_no: int, *, content: Any,
                             kind: str = "manual", task_id: str = "",
                             source_setup_version_id: str = "",
                             source_outline_version_id: str = "",
                             finish_reason: str = "", note: str = "",
                             expected_revision: Any = None,
                             manifest: dict[str, Any] | None = None) -> dict[str, Any]:
        """新增一份正文版本并切换当前指针；与旧当前版本逐字相同则不重复建版本。

        幂等：同一 `task_id` 已写过版本时直接返回既有版本（重复写回/崩溃恢复安全）。
        """
        manifest = manifest or self._load_manifest(book_id)
        if chapter_no not in {int(n) for n in manifest.get("chapter_ids") or []}:
            raise LookupError("章节不存在")
        if not isinstance(content, str) or not content.strip():
            raise WriterStoreError("章节正文不能为空")
        if len(content) > CHAPTER_CONTENT_MAX_CHARS:
            raise WriterStoreError(f"单章正文超过 {CHAPTER_CONTENT_MAX_CHARS} 字上限，已拒绝")
        book_dir = self._book_dir(book_id)
        index = self._load_chapter_index(book_dir, chapter_no)
        if expected_revision is not None and expected_revision != "":
            if isinstance(expected_revision, bool):
                raise WriterStoreError("revision 必须是整数")
            try:
                expected = int(expected_revision)
            except (TypeError, ValueError):
                raise WriterStoreError("revision 必须是整数") from None
            if expected != int(index.get("revision") or 0):
                raise WriterConflictError(
                    f"第 {chapter_no} 章已被修改过（你看到的是第 {expected} 版，"
                    f"现在是第 {int(index.get('revision') or 0)} 版），请重新读取后再保存"
                )
        with self._lock:
            if task_id:
                for row in index.get("version_ids") or []:
                    if isinstance(row, dict) and str(row.get("task_id") or "") == task_id:
                        existing = self._read_json(
                            self._chapter_version_path(book_dir, chapter_no, row["id"]),
                            what="正文版本",
                        )
                        return {"chapter_no": chapter_no, "version": existing,
                                "duplicate": True,
                                "chapter_revision": int(index.get("revision") or 0)}
            current_id = str(index.get("current_version_id") or "")
            if current_id:
                current_record = self._read_json(
                    self._chapter_version_path(book_dir, chapter_no, current_id), what="正文版本",
                )
                if str(current_record.get("content") or "") == content:
                    return {"chapter_no": chapter_no, "version": current_record,
                            "duplicate": True,
                            "chapter_revision": int(index.get("revision") or 0)}
            self._check_book_capacity(book_dir, extra_bytes=len(content.encode("utf-8")))
            versions = [row for row in (index.get("version_ids") or []) if isinstance(row, dict)]
            version_no = (max((int(row.get("version") or 0) for row in versions), default=0) + 1)
            vid = new_version_id("cv")
            record = {
                "id": vid, "version": version_no, "no": chapter_no,
                "kind": str(kind or "manual"), "updated_at": _now_ms(),
                "task_id": str(task_id or ""),
                "source_setup_version_id": str(source_setup_version_id or ""),
                "source_outline_version_id": str(source_outline_version_id or ""),
                "finish_reason": str(finish_reason or ""),
                "note": str(note or ""),
                "content": content, "len": len(content),
            }
            self._write_json(self._chapter_version_path(book_dir, chapter_no, vid), record)
            versions.append({"id": vid, "version": version_no, "kind": str(kind or "manual"),
                             "updated_at": record["updated_at"], "task_id": str(task_id or ""),
                             "len": len(content), "finish_reason": str(finish_reason or "")})
            index["version_ids"] = versions
            index["current_version_id"] = vid
            index["revision"] = int(index.get("revision") or 0) + 1
            index["updated_at"] = _now_ms()
            self._write_chapter_index(book_dir, index)
            self._bump_manifest(book_dir, manifest)
        return {"chapter_no": chapter_no, "version": record, "duplicate": False,
                "chapter_revision": int(index["revision"])}

    def select_chapter_version(self, book_id: str, chapter_no: int, *,
                               version_id: str, expected_revision: Any = None) -> dict[str, Any]:
        """恢复旧版本：切换当前指针，不移动文件。"""
        manifest = self._load_manifest(book_id)
        if chapter_no not in {int(n) for n in manifest.get("chapter_ids") or []}:
            raise LookupError("章节不存在")
        book_dir = self._book_dir(book_id)
        index = self._load_chapter_index(book_dir, chapter_no)
        if expected_revision is not None and expected_revision != "":
            self._check_revision({"revision": index.get("revision")}, expected_revision,
                                 what=f"第 {chapter_no} 章")
        vid = str(version_id or "").strip()
        known = [str(row.get("id") or "") for row in (index.get("version_ids") or []) if isinstance(row, dict)]
        if vid not in known:
            raise WriterStoreError(f"第 {chapter_no} 章没有这份正文版本")
        with self._lock:
            index["current_version_id"] = vid
            index["revision"] = int(index.get("revision") or 0) + 1
            index["updated_at"] = _now_ms()
            self._write_chapter_index(book_dir, index)
            self._bump_manifest(book_dir, manifest)
        return {"chapter_no": chapter_no, "current_version_id": vid,
                "chapter_revision": int(index["revision"])}

    def select_setup_version(self, book_id: str, *, version_id: str,
                             expected_revision: Any = None) -> dict[str, Any]:
        manifest = self._load_manifest(book_id)
        self._check_revision(manifest, expected_revision)
        vid = str(version_id or "").strip()
        known = [str(row.get("id") or "") for row in (manifest.get("setup_versions") or [])
                 if isinstance(row, dict)]
        if vid not in known:
            raise WriterStoreError("设定版本不存在")
        book_dir = self._book_dir(book_id)
        with self._lock:
            manifest["current_setup_version_id"] = vid
            self._bump_manifest(book_dir, manifest)
        return {"book": self._public_manifest(manifest)}

    def select_outline_version(self, book_id: str, *, version_id: str,
                               expected_revision: Any = None) -> dict[str, Any]:
        manifest = self._load_manifest(book_id)
        self._check_revision(manifest, expected_revision)
        vid = str(version_id or "").strip()
        known = [str(row.get("id") or "") for row in (manifest.get("outline_versions") or [])
                 if isinstance(row, dict)]
        if vid not in known:
            raise WriterStoreError("大纲版本不存在")
        book_dir = self._book_dir(book_id)
        with self._lock:
            manifest["current_outline_version_id"] = vid
            self._bump_manifest(book_dir, manifest)
        return {"book": self._public_manifest(manifest)}

    def delete_chapter(self, book_id: str, chapter_no: int) -> dict[str, Any]:
        """删章节（含全部正文版本）；至少保留一章。"""
        manifest = self._load_manifest(book_id)
        ids = [int(n) for n in manifest.get("chapter_ids") or []]
        if chapter_no not in ids:
            raise LookupError("章节不存在")
        if len(ids) <= 1:
            raise WriterStoreError("作品至少要保留一章；要整本删除请删作品")
        book_dir = self._book_dir(book_id)
        with self._lock:
            shutil.rmtree(self._chapter_version_dir(book_dir, chapter_no), ignore_errors=True)
            try:
                self._chapter_dir(book_dir, chapter_no).unlink(missing_ok=True)
            except OSError as exc:
                raise WriterStoreError(f"删除章节失败：{exc}") from exc
            manifest["chapter_ids"] = [n for n in ids if n != chapter_no]
            self._bump_manifest(book_dir, manifest)
        return {"ok": True, "chapter_count": len(manifest["chapter_ids"])}

    # ---- 流水线状态（book.json 内的活动记录） ----

    def get_active_pipeline(self, book_id: str, *, manifest: dict[str, Any] | None = None) -> dict[str, Any] | None:
        manifest = manifest or self._load_manifest(book_id)
        pipeline = manifest.get("active_pipeline")
        return dict(pipeline) if isinstance(pipeline, dict) else None

    def update_pipeline(self, book_id: str, pipeline_id: str,
                        mutate: Any, *, expected_attempt: Any = None) -> dict[str, Any] | None:
        """锁内读取-修改-写回活动流水线；返回更新后的记录。

        `mutate(pipeline) -> None` 就地修改；`expected_attempt` 给了就做 attempt
        一致性校验（旧 attempt 的迟到结果不能写回新 attempt 的流水线）。
        mutate 内部可能通过 save_setup / save_outline / save_chapter_version 再写盘
        （推进作品 revision）——因此写回前**重读**清单，把流水线记录合并进最新状态
        再落盘，避免用旧对象覆盖掉 mutate 的写入（丢失更新）。
        流水线已被清除 / ID 不符时返回 None（调用方据此停止推进）。
        """
        with self._lock:
            manifest = self._load_manifest(book_id)
            book_dir = self._book_dir(book_id)
            current = manifest.get("active_pipeline")
            if not isinstance(current, dict) or str(current.get("id") or "") != str(pipeline_id):
                return None
            if expected_attempt is not None and int(current.get("attempt") or 0) != int(expected_attempt):
                return None
            mutate(current)
            # mutate 可能已通过 store 方法写过 book.json（版本写回推进 revision）：
            # 重读磁盘上的最新清单，把流水线记录合并进去再保存。
            fresh = self._load_manifest(book_id)
            fresh["active_pipeline"] = current
            self._save_manifest(book_dir, fresh)
            return dict(current)

    def clear_pipeline(self, book_id: str, pipeline_id: str) -> bool:
        manifest = self._load_manifest(book_id)
        book_dir = self._book_dir(book_id)
        with self._lock:
            current = manifest.get("active_pipeline")
            if not isinstance(current, dict) or str(current.get("id") or "") != str(pipeline_id):
                return False
            manifest["active_pipeline"] = None
            self._save_manifest(book_dir, manifest)
            return True

    def iter_active_pipelines(self) -> list[dict[str, Any]]:
        """扫描全部作品，返回仍在 running 的活动流水线（含 book_id）。"""
        rows: list[dict[str, Any]] = []
        if not self._root.exists():
            return rows
        for path in sorted(self._root.iterdir()):
            if not path.is_dir() or not BOOK_ID_PATTERN.match(path.name):
                continue
            try:
                manifest = self._load_manifest(path.name)
            except (LookupError, WriterStoreError):
                continue
            pipeline = manifest.get("active_pipeline")
            if isinstance(pipeline, dict) and str(pipeline.get("status") or "") == "running":
                rows.append({"book_id": str(manifest.get("id") or ""), "pipeline": dict(pipeline)})
        return rows

    def recover_pipelines(self) -> int:
        """启动收敛：running 的活动流水线标为 interrupted（保留已完成步骤，不自动续跑）。"""
        recovered = 0
        if not self._root.exists():
            return 0
        for path in sorted(self._root.iterdir()):
            if not path.is_dir() or not BOOK_ID_PATTERN.match(path.name):
                continue
            try:
                manifest = self._load_manifest(path.name)
            except (LookupError, WriterStoreError):
                continue
            pipeline = manifest.get("active_pipeline")
            if isinstance(pipeline, dict) and str(pipeline.get("status") or "") == "running":
                pipeline["status"] = "interrupted"
                pipeline["error"] = pipeline.get("error") or "服务重启，流水线已中断"
                pipeline["updated_at"] = _now_ms()
                manifest["active_pipeline"] = pipeline
                with self._lock:
                    self._save_manifest(path, manifest)
                recovered += 1
        return recovered

    # ---- 导入暂存（启元 作品存档.json） ----

    def stage_import(self, *, filename: str, data: bytes) -> dict[str, Any]:
        """把上传的启元存档 JSON 落暂存并解析预览。只读上传内容，不碰启元目录。"""
        if not data:
            raise WriterStoreError("文件是空的")
        if len(data) > MAX_IMPORT_BYTES:
            raise WriterStoreError(
                f"文件太大（{len(data) / 1024 / 1024:.1f} MB），上限 {MAX_IMPORT_BYTES // 1024 // 1024} MB"
            )
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WriterStoreError("不是有效的 JSON 文件，请选择启元的「作品存档.json」") from exc
        if not isinstance(payload, dict):
            raise WriterStoreError("存档结构无法识别：顶层必须是 JSON 对象")
        parsed = parse_qiyuan_archive(payload)
        import_id = self._new_import_id()
        import_dir = self._imports / import_id
        with self._lock:
            import_dir.mkdir(parents=True, exist_ok=False)
            tmp_path = import_dir / f".{uuid.uuid4().hex}.tmp"
            try:
                tmp_path.write_bytes(data)
                os.replace(tmp_path, import_dir / "archive.json")
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            meta = {
                "schema_version": SCHEMA_VERSION,
                "import_id": import_id,
                "name": clean_title(Path(filename or "作品存档.json").name),
                "bytes": len(data),
                "created_at": _now_ms(),
                "preview": parsed["preview"],
            }
            self._write_json(import_dir / "meta.json", meta)
        return {"import_id": import_id, **parsed["preview"], "name": meta["name"]}

    def load_import(self, import_id: str) -> dict[str, Any]:
        """读导入暂存（meta + 解析后的作品内容）。确认入库与预览共用。"""
        import_dir = self._import_dir(import_id)
        meta = self._read_json(import_dir / "meta.json", what="导入暂存")
        archive_path = import_dir / "archive.json"
        if not archive_path.is_file():
            raise WriterStoreError("导入暂存的存档文件已丢失，请重新上传")
        payload = self._read_json(archive_path, what="导入存档")
        parsed = parse_qiyuan_archive(payload)
        return {"meta": meta, "parsed": parsed}

    def commit_import(self, import_id: str) -> dict[str, Any]:
        """确认入库：解析暂存存档并创建一个**新的**原创作品。"""
        staged = self.load_import(import_id)
        parsed = staged["parsed"]
        content = parsed["setup"]
        outline_chapters = parsed["outline_chapters"]
        chapters = parsed["chapters"]
        title = str(content.get("title") or "").strip() or "启元导入作品"
        book = self.create_book(title=title, setup=content, kind="imported")
        book_id = str(book["id"])
        manifest = self._load_manifest(book_id)
        book_dir = self._book_dir(book_id)
        with self._lock:
            setup_version_id = str(manifest.get("current_setup_version_id") or "")
            outline_version_id = ""
            if outline_chapters:
                outline_version_id = self._write_outline_locked(
                    book_dir, manifest, outline_chapters, kind="imported", task_id="",
                    source_setup_version_id=setup_version_id,
                )
                self._sync_chapters_locked(book_dir, manifest, outline_chapters, allow_drop=False)
            for row in chapters:
                self._ensure_chapter_index_locked(
                    book_dir, manifest, int(row["no"]),
                    title=str(row.get("title") or ""), beat="", hook="",
                )
                self.save_chapter_version(
                    book_id, int(row["no"]), content=str(row["content"]), kind="imported",
                    note="启元存档导入", manifest=manifest,
                    source_setup_version_id=setup_version_id,
                    source_outline_version_id=outline_version_id,
                )
            self._bump_manifest(book_dir, manifest)
        shutil.rmtree(self._import_dir(import_id), ignore_errors=True)
        return {"book": self._public_manifest(manifest), "imported": parsed["preview"]}

    def purge_imports(self, *, ttl_seconds: int = IMPORT_TTL_SECONDS) -> int:
        if not self._imports.exists():
            return 0
        deadline = time.time() - ttl_seconds
        removed = 0
        for path in self._imports.iterdir():
            if not path.is_dir() or not IMPORT_ID_PATTERN.match(path.name):
                continue
            try:
                if path.stat().st_mtime > deadline:
                    continue
            except OSError:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        return removed

    # ---- 导出 ----

    def export_book(self, book_id: str, *, fmt: str = "txt", with_extras: bool = False) -> dict[str, Any]:
        """按当前版本导出全书；`with_extras=True` 附设定与大纲。"""
        manifest = self._load_manifest(book_id)
        book_dir = self._book_dir(book_id)
        title = str(manifest.get("title") or "未命名作品")
        parts: list[str] = []
        if fmt == "md":
            parts.append(f"# {title}\n")
        missing: list[int] = []
        for chapter_no in (int(n) for n in manifest.get("chapter_ids") or []):
            try:
                index = self._load_chapter_index(book_dir, chapter_no)
                current_id = str(index.get("current_version_id") or "")
                body = ""
                if current_id:
                    record = self._read_json(
                        self._chapter_version_path(book_dir, chapter_no, current_id), what="正文版本",
                    )
                    body = str(record.get("content") or "")
            except (LookupError, WriterStoreError):
                missing.append(chapter_no)
                continue
            if not body.strip():
                missing.append(chapter_no)
                continue
            chapter_title = str(index.get("title") or f"第 {chapter_no} 章")
            if fmt == "md":
                parts.append(f"## {chapter_title}\n\n{body}\n")
            else:
                parts.append(f"{chapter_title}\n\n{body}\n\n")
        if with_extras:
            parts.append(self._extras_block(book_id, manifest, fmt))
        text = "".join(parts)
        return {"text": text, "missing_chapters": missing,
                "filename": f"{clean_title(title)}.{fmt}"}

    def _extras_block(self, book_id: str, manifest: dict[str, Any], fmt: str) -> str:
        setup = self.load_setup(book_id, manifest=manifest)
        outline = self.load_outline(book_id, manifest=manifest)
        parts: list[str] = []
        setup_lines: list[str] = []
        if str(setup.get("idea") or "").strip():
            setup_lines.append(f"创意：{setup['idea'].strip()}")
        genre = setup.get("genre") if isinstance(setup.get("genre"), dict) else {}
        genre_bits = [str(genre.get("cat") or ""), str(genre.get("sub_cat") or ""),
                      str(genre.get("tone") or "")]
        genre_bits = [bit for bit in genre_bits if bit]
        if genre_bits:
            setup_lines.append("题材：" + " · ".join(genre_bits))
        if str(setup.get("title") or "").strip():
            setup_lines.append(f"书名：{setup['title'].strip()}")
        if str(setup.get("blurb") or "").strip():
            setup_lines.append(f"简介：{setup['blurb'].strip()}")
        if str(setup.get("worldview") or "").strip():
            setup_lines.append(f"世界观：{setup['worldview'].strip()}")
        for row in setup.get("characters") or []:
            if isinstance(row, dict) and str(row.get("name") or "").strip():
                role = str(row.get("role") or "").strip()
                setup_lines.append(
                    f"角色：{row['name']}" + (f"（{role}）" if role else "")
                    + f" — {str(row.get('identity') or '').strip()}"
                )
        if setup_lines:
            if fmt == "md":
                parts.append("\n# 附录：设定\n\n" + "\n\n".join(setup_lines) + "\n")
            else:
                parts.append("\n==== 附录：设定 ====\n\n" + "\n\n".join(setup_lines) + "\n\n")
        rows = outline.get("chapters") if isinstance(outline.get("chapters"), list) else []
        if rows:
            lines = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                line = f"第{row.get('no')}章 {row.get('title')}"
                if str(row.get("beat") or "").strip():
                    line += f" —— {row['beat'].strip()}"
                lines.append(line)
            if lines:
                if fmt == "md":
                    parts.append("\n# 附录：大纲\n\n" + "\n".join(lines) + "\n")
                else:
                    parts.append("\n==== 附录：大纲 ====\n\n" + "\n".join(lines) + "\n\n")
        return "".join(parts)


# ---- 启元存档解析（纯函数，便于单测） ----

def parse_qiyuan_archive(payload: dict[str, Any]) -> dict[str, Any]:
    """解析启元 `作品存档.json`（完整应用 state）：取 `novel` 与 `chapters` 作品字段。

    只读这两类字段；`settings`、API Key、baseUrl、供应商信息等一律不读不迁。
    判定规则（实施计划 §10）：旧档章节正文与对应大纲梗概**逐字相同**的按
    「只有梗概、尚未写正文」处理；其余保存为 imported 正文版本。
    """
    novel = payload.get("novel") if isinstance(payload.get("novel"), dict) else {}
    if not novel and not isinstance(payload.get("chapters"), list):
        raise WriterStoreError("存档里没有识别到原创小说数据（缺少 novel / chapters 字段）")
    meta = novel.get("meta") if isinstance(novel.get("meta"), dict) else {}
    tags = [str(t or "").strip() for t in (meta.get("tags") or []) if str(t or "").strip()][:5]
    chars: list[dict[str, str]] = []
    for item in novel.get("chars") or []:
        if isinstance(item, dict) and str(item.get("name") or "").strip():
            chars.append({
                "name": str(item.get("name") or "").strip()[:60],
                "role": str(item.get("role") or "").strip()[:40],
                "identity": str(item.get("identity") or "").strip()[:200],
                "goal": str(item.get("goal") or "").strip()[:200],
                "traits": str(item.get("traits") or "").strip()[:200],
                "speech": str(item.get("speech") or "").strip()[:200],
                "arc": str(item.get("arc") or "").strip()[:200],
                "background": str(item.get("background") or "").strip()[:1000],
            })
    outline_rows: list[dict[str, Any]] = []
    for index, item in enumerate(novel.get("outlineMeta") or [], start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        outline_rows.append({
            "no": int(item.get("no") or index),
            "title": title[:200],
            "beat": str(item.get("beat") or "").strip()[:2000],
            "hook": str(item.get("hook") or "").strip()[:500],
        })
    nos = [row["no"] for row in outline_rows]
    if len(set(nos)) != len(nos) or any(no < 1 for no in nos):
        outline_rows = [{**row, "no": pos} for pos, row in enumerate(outline_rows, start=1)]
    else:
        outline_rows.sort(key=lambda row: row["no"])
    setup = normalize_setup({
        "idea": str(novel.get("prompt") or ""),
        "genre": {
            "channel": str(meta.get("lane") or ""),
            "cat": str(meta.get("subLane") or ""),
            "sub_cat": "",
            "tone": "",
            "hook": "",
        },
        "genre_tags": tags,
        "title": str(meta.get("title") or payload.get("bookTitle") or ""),
        "blurb": str(meta.get("blurb") or ""),
        "worldview": str(novel.get("worldview") or ""),
        "characters": chars,
        "style": str(novel.get("style") or ""),
    })
    beats_by_no = {int(row["no"]): row["beat"] for row in outline_rows}
    chapter_rows: list[dict[str, Any]] = []
    sketch_only = 0
    for index, item in enumerate(payload.get("chapters") or [], start=1):
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "")
        title = str(item.get("title") or "").strip()
        if not title and not content.strip():
            continue
        no = index
        beat = beats_by_no.get(no, "")
        if beat and content.strip() == beat.strip():
            sketch_only += 1
            continue
        if not content.strip():
            continue
        chapter_rows.append({"no": no, "title": title, "content": content})
    if chapter_rows and outline_rows:
        # 正文比大纲多出的章（大纲没登记的）：保留内容，编号顺延到大纲之后。
        known = set(beats_by_no)
        extra = [row for row in chapter_rows if row["no"] not in known]
        if extra:
            next_no = (max(beats_by_no) if beats_by_no else 0)
            for row in extra:
                next_no += 1
                row["no"] = next_no
    warnings: list[str] = []
    if not setup.get("idea") and not outline_rows and not chapter_rows:
        warnings.append("存档里没有识别到有效的创意、大纲或正文，导入的作品会是空架子")
    if chars and len(chars) >= 10:
        warnings.append("角色超过 10 个，只保留前 10 个")
    unknown_top = sorted(
        key for key in payload.keys()
        if key not in {"novel", "chapters", "characters", "scenes", "props", "elements",
                       "outline", "summaries", "bookTitle", "_t", "settings", "updatedAt"}
    )
    if unknown_top:
        warnings.append("以下字段与本功能无关，未迁移：" + "、".join(unknown_top[:8]))
    preview = {
        "title": str(setup.get("title") or "") or "未命名",
        "idea_len": len(str(setup.get("idea") or "")),
        "genre": str((setup.get("genre") or {}).get("cat") or ""),
        "character_count": len(chars),
        "outline_count": len(outline_rows),
        "chapter_count": len(chapter_rows),
        "sketch_only_count": sketch_only,
        "warnings": warnings,
    }
    return {"setup": setup, "outline_chapters": outline_rows,
            "chapters": chapter_rows, "preview": preview}
