# -*- coding: utf-8 -*-
"""小说域服务：作品、章节、稿件与成果的持久化 + `/api/novel/*` 的业务门面。

阶段一（文件处理，零 AI）：导入、拆章、勾选、保存、编辑、导出。
阶段二（AI 生成）：要素 / 改编大纲 / 单章改编——组装输入交给 Studio 任务队列，结果写回作品。
阶段三（AI 生成）：剧本场次。

设计要点：

1. **单章一文件**：改一章不重写整本，坏文件不拖累其它章。
2. **book.json 是清单的权威来源**：`chapter_ids` 决定哪些章节属于作品。保存章节时
   先原子写正文文件、再原子更新 book.json；中间崩溃留下的孤儿章节忽略并可清理。
3. **原子写**：同目录临时文件 + `os.replace`，照 `naiba/studio/custom.py` 的范式。
4. **导入暂存**：导入先落 `_imports/<import-id>/`，勾选入库时才提交为正式作品目录。
5. **两段式**：列表与详情只回索引；正文只有单章 / 单稿接口给。
6. **体积闸门**：单文件沿用 60 MiB；作品目录总量上限 256 MiB，超了整项拒绝。
7. **AI 结果写回**：`revision` 乐观并发 + 任务 id 核对，避免 A 未完又生成 B 时互相覆盖。
8. **稿件独立成记录**（schema 2）：原文、升级前正文、人工改编稿、AI 改编稿各是一份
   有稳定 ID 的独立正文。编辑不再覆盖原文，而是新增一份改编稿；后续生成按
   「章节 + 稿件」选择素材。

目录布局（schema 2）::

    studio/novel/<book-id>/
      book.json                       清单（章节顺序、成果版本、来源说明）
      chapters/0001.json              章节索引（标题、字数、稿件 ID 列表）
      manuscripts/0001/ms-xxxx.json   每份稿件的正文
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from naiba.core import novel_split
from naiba.novel import manuscripts, prompts
from naiba.novel import migration
from naiba.tools.providers.novel import NovelError, read_text_auto

# 作品清单版本：2 = 章节正文拆成独立稿件记录（1 = 单一 content 字段）。
SCHEMA_VERSION = manuscripts.SCHEMA_VERSION
# 作品 ID：时间戳 + 随机后缀，禁止用户文件名穿越目录（与 custom.py 同构）。
BOOK_ID_PATTERN = re.compile(r"^book-[0-9]{8}-[0-9a-f]{12}$")
IMPORT_ID_PATTERN = re.compile(r"^import-[0-9]{8}-[0-9a-f]{12}$")
# 章节号：4 位十进制文件名，同时限制单本最多 9999 章。
CHAPTER_NO_PATTERN = re.compile(r"^[0-9]{1,4}$")
CHAPTER_FILE_PATTERN = re.compile(r"^([0-9]{4})\.json$")
# 成果版本记录 ID：`el-` 要素 / `ol-` 大纲 / `sc-` 剧本，删除后不复用。
VERSION_ID_PATTERN = re.compile(r"^(el|ol|sc)-[0-9a-f]{12}$")

TITLE_MAX = 200
# 单文件上限沿用算法层常量（60 MiB），超了如实拒绝。
MAX_FILE_BYTES = novel_split.MAX_FILE_BYTES
# 作品目录总量上限：导入、编辑、后续 AI 写回共用这一条。
MAX_BOOK_BYTES = 256 * 1024 * 1024
# 导入暂存的保留时长：勾选入库前一直有效，但避免失败导入长期占盘。
IMPORT_TTL_SECONDS = 24 * 3600
# 容量未知时，估算输入超过这个量就要用户确认后再提交（不伪造模型上限）。
LARGE_INPUT_CONFIRM_TOKENS = 60000

# 拆分层级的中文说明：降级必须明示，不能让用户以为都是常规识别。
LEVEL_LABELS = {
    "heading": "按章节标题识别",
    "sep": "按符号分节（没有识别到章节标题）",
    "single": "只识别到 1 个标题，仍按它切分",
    "len": "没有识别到任何标题，按字数等分",
}


class NovelServiceError(ValueError):
    """小说域的输入/状态错误（用户可读，由 API 边界转成 4xx）。"""


class NovelConflictError(NovelServiceError):
    """乐观并发冲突：revision 对不上，调用方应重新读取后再改（对应 409）。"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_name(name: str) -> str:
    """书名落目录名/文件名时清洗：只用于展示与源文件副本，不作路径主键。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t\x00-\x1f]', "_", str(name or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:80] or "未命名"


# 要素分类的标题关键词：模型输出用「## 人物」「人物：」这类写法时据此归类。
_ELEMENT_SECTION_HINTS = {
    "characters": ("人物", "角色", "人物表"),
    "scenes": ("场景", "地点", "环境"),
    "props": ("道具", "物件", "物品"),
}


def _split_elements(text: str) -> dict[str, list[str]]:
    """把要素结果拆成人物/场景/道具三类。

    只按「标题行 + 列表项」这类结构归并；识别不了的整段留在 `raw`（由调用方保留），
    这里不猜、不编、不丢内容。
    """
    buckets: dict[str, list[str]] = {"characters": [], "scenes": [], "props": []}
    current = ""
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # 标题行：`## 人物` / `人物：` / `【人物】` / `**人物**`
        heading = re.sub(r"^[#>*\-\s【\[]+|[】\]:：*\s]+$", "", line)
        matched = ""
        if len(heading) <= 12:
            for key, hints in _ELEMENT_SECTION_HINTS.items():
                if any(hint in heading for hint in hints):
                    matched = key
                    break
        if matched:
            current = matched
            continue
        if current and re.match(r"^[·•\-*+]|^\d+[.、)]", line):
            item = re.sub(r"^[·•\-*+]|^\d+[.、)]", "", line).strip()
            if item:
                buckets[current].append(item)
    return buckets


def _new_version_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class NovelStore:
    """作品、章节与稿件的落盘（线程锁串行化写路径；读为每章独立小文件）。"""

    def __init__(self, data_dir: Path):
        self._root = Path(data_dir) / "studio" / "novel"
        self._imports = self._root / "_imports"
        self._lock = threading.Lock()

    # ---- 路径与安全 ----

    def root(self) -> Path:
        return self._root

    def _book_dir(self, book_id: str) -> Path:
        """作品目录：ID 必须匹配白名单，且解析后仍在根内（双重校验）。"""
        bid = str(book_id or "").strip()
        if not BOOK_ID_PATTERN.match(bid):
            raise NovelServiceError("作品 ID 非法")
        path = (self._root / bid).resolve()
        root = self._root.resolve()
        if not path.is_relative_to(root) or path == root:
            raise NovelServiceError("作品路径越界")
        return path

    def _import_dir(self, import_id: str) -> Path:
        iid = str(import_id or "").strip()
        if not IMPORT_ID_PATTERN.match(iid):
            raise NovelServiceError("导入 ID 非法")
        path = (self._imports / iid).resolve()
        if not path.is_relative_to(self._imports.resolve()) or path == self._imports.resolve():
            raise NovelServiceError("导入路径越界")
        return path

    def _chapter_path(self, book_dir: Path, chapter_no: int) -> Path:
        """章节索引文件：4 位编号，校验后仍在 chapters/ 内。"""
        if isinstance(chapter_no, bool) or not isinstance(chapter_no, int):
            raise NovelServiceError("章节号必须是整数")
        if chapter_no < 1 or chapter_no > 9999:
            raise NovelServiceError("章节号必须在 1–9999 之间")
        chapters_dir = (book_dir / "chapters").resolve()
        path = (chapters_dir / f"{chapter_no:04d}.json").resolve()
        if not path.is_relative_to(chapters_dir):
            raise NovelServiceError("章节路径越界")
        return path

    def _manuscript_dir(self, book_dir: Path, chapter_no: int) -> Path:
        if isinstance(chapter_no, bool) or not isinstance(chapter_no, int):
            raise NovelServiceError("章节号必须是整数")
        if chapter_no < 1 or chapter_no > 9999:
            raise NovelServiceError("章节号必须在 1–9999 之间")
        root = (book_dir / "manuscripts").resolve()
        path = (root / f"{chapter_no:04d}").resolve()
        if not path.is_relative_to(root):
            raise NovelServiceError("稿件路径越界")
        return path

    def _manuscript_path(self, book_dir: Path, chapter_no: int, manuscript_id: str) -> Path:
        mid = str(manuscript_id or "").strip()
        if not manuscripts.MANUSCRIPT_ID_PATTERN.match(mid):
            raise NovelServiceError("稿件 ID 非法")
        folder = self._manuscript_dir(book_dir, chapter_no)
        path = (folder / f"{mid}.json").resolve()
        if not path.is_relative_to(folder):
            raise NovelServiceError("稿件路径越界")
        return path

    def _new_book_id(self) -> str:
        return f"book-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:12]}"

    def _new_import_id(self) -> str:
        return f"import-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:12]}"

    # ---- 原子写 ----

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        """同目录临时文件 + 原子替换；失败不覆盖已有内容（照 custom.py:109-120）。"""
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
            raise NovelServiceError(f"{what}文件损坏：{path.name}") from exc
        if not isinstance(payload, dict):
            raise NovelServiceError(f"{what}文件损坏：{path.name}")
        return payload

    # ---- 体积闸门 ----

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
        """作品目录总量上限：超了整项拒绝，不自动截断、不删历史章节。"""
        if not book_dir.exists():
            return
        used = self._dir_bytes(book_dir)
        if used + extra_bytes > MAX_BOOK_BYTES:
            raise NovelServiceError(
                f"作品目录会超过 {MAX_BOOK_BYTES // 1024 // 1024} MB 上限"
                f"（当前 {used / 1024 / 1024:.1f} MB）。请删除多余章节或稿件后再试。"
            )

    # ---- 清单 ----

    def _load_manifest(self, book_id: str, *, upgrade: bool = True) -> dict[str, Any]:
        book_dir = self._book_dir(book_id)
        manifest = self._read_json(book_dir / "book.json", what="作品清单")
        version = manifest.get("schema_version")
        if version == 1 and upgrade:
            self._upgrade_book(book_dir, manifest)
            manifest = self._read_json(book_dir / "book.json", what="作品清单")
            version = manifest.get("schema_version")
        if version not in (1, SCHEMA_VERSION):
            raise NovelServiceError(f"作品数据版本不支持：{version}（当前支持 {SCHEMA_VERSION}）")
        if str(manifest.get("id") or "") != book_id:
            raise NovelServiceError("作品清单与目录不一致")
        if not isinstance(manifest.get("chapter_ids"), list):
            raise NovelServiceError("作品清单缺少章节列表")
        return manifest

    # ---- 老作品升级（schema 1 → 2）----

    def _upgrade_book(self, book_dir: Path, manifest: dict[str, Any]) -> None:
        """把 schema 1 的单一正文升级成独立稿件记录。

        幂等：先落升级计划（含确定的稿件 ID），再写稿件与章节索引，最后原子切换
        `book.json`。中途失败时清单仍是 schema 1，重跑复用同一份计划。
        """
        with self._lock:
            current = self._read_json(book_dir / "book.json", what="作品清单")
            if int(current.get("schema_version") or 0) == SCHEMA_VERSION:
                return
            plan = migration.load_plan(book_dir)
            if plan is None:
                try:
                    plan = migration.build_plan(book_dir, current, self._imports)
                except migration.MigrationError as exc:
                    raise NovelServiceError(str(exc)) from exc
                self._write_json(book_dir / migration.UPGRADE_PLAN, plan)
            for chapter in plan.get("chapters") or []:
                chapter_no = int(chapter.get("i") or 0)
                ids: list[str] = []
                for row in chapter.get("manuscripts") or []:
                    record = manuscripts.normalize(row, chapter_no=chapter_no)
                    self._write_json(
                        self._manuscript_path(book_dir, chapter_no, record["id"]), record,
                    )
                    ids.append(record["id"])
                if not ids:
                    raise NovelServiceError(f"第 {chapter_no} 章升级计划里没有稿件")
                index = {
                    "schema_version": SCHEMA_VERSION,
                    "revision": int(chapter.get("revision") or 1),
                    "i": chapter_no,
                    "title": str(chapter.get("title") or ""),
                    "len": len(str(chapter.get("manuscripts")[0].get("content") or "")),
                    "manuscript_ids": ids,
                    "base_manuscript_id": ids[0],
                    "next_adapted_no": 1,
                }
                self._write_json(self._chapter_path(book_dir, chapter_no), index)
            current["schema_version"] = SCHEMA_VERSION
            origin = plan.get("origin_recovery") if isinstance(plan.get("origin_recovery"), dict) else {}
            current["origin_recovery"] = origin
            current["upgraded_at"] = _now_ms()
            # 切换点：写完所有稿件与章节索引后才更新清单。
            self._write_json(book_dir / "book.json", current)
            migration.clear_plan(book_dir)

    @staticmethod
    def _public_manifest(manifest: dict[str, Any], *, include_versions: bool = True) -> dict[str, Any]:
        """对外清单视图：去掉内部字段，章节索引单独给。"""
        source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
        chapter_ids = [int(n) for n in manifest.get("chapter_ids") or []]
        # 阶段二起才有改编成果；老作品没有这些字段时给空值，前端不用做 undefined 判断。
        elements = manifest.get("elements") if isinstance(manifest.get("elements"), dict) else {}
        element_versions = manifest.get("elements_versions") if isinstance(manifest.get("elements_versions"), list) else []
        outline_versions = manifest.get("outline_versions") if isinstance(manifest.get("outline_versions"), list) else []
        script = manifest.get("script") if isinstance(manifest.get("script"), list) else []
        origin = manifest.get("origin_recovery") if isinstance(manifest.get("origin_recovery"), dict) else {}
        return {
            "id": str(manifest.get("id") or ""),
            "title": str(manifest.get("title") or ""),
            "revision": int(manifest.get("revision") or 0),
            "schema_version": int(manifest.get("schema_version") or 0),
            "chapter_count": len(chapter_ids),
            "created_at": int(manifest.get("created_at") or 0),
            "updated_at": int(manifest.get("updated_at") or 0),
            "source": {
                "name": str(source.get("name") or ""),
                "encoding": str(source.get("encoding") or ""),
                "bytes": int(source.get("bytes") or 0),
                "split_level": str(source.get("split_level") or ""),
                "split_level_label": str(
                    source.get("split_level_label")
                    or LEVEL_LABELS.get(str(source.get("split_level") or ""), "")
                ),
                "max_len": int(source.get("max_len") or novel_split.DEFAULT_MAX_LEN),
                "total_len": int(source.get("total_len") or 0),
            },
            # 升级说明：哪些章恢复了真正的原文、哪些只能标成「升级前保存的正文」。
            "origin_recovery": {
                "restored": [int(n) for n in (origin.get("restored") or [])],
                "legacy": [int(n) for n in (origin.get("legacy") or [])],
                "reason": str(origin.get("reason") or ""),
                "matched_import": str(origin.get("matched_import") or ""),
            },
            # 阶段二成果：要素分三类、大纲三幕、每章摘要。
            "elements": {
                "characters": list(elements.get("characters") or []),
                "scenes": list(elements.get("scenes") or []),
                "props": list(elements.get("props") or []),
                "raw": str(elements.get("raw") or ""),
                "updated_at": int(elements.get("updated_at") or 0),
                "task_id": str(elements.get("task_id") or ""),
                "version": int(elements.get("version") or (len(element_versions) or 1 if elements.get("raw") else 0)),
            },
            "elements_versions": [dict(row) for row in element_versions if isinstance(row, dict)] if include_versions else [],
            "outline": str(manifest.get("outline") or ""),
            "outline_version": int(manifest.get("outline_version") or (len(outline_versions) or 1 if manifest.get("outline") else 0)),
            "outline_versions": [dict(row) for row in outline_versions if isinstance(row, dict)] if include_versions else [],
            "outline_task_id": str(manifest.get("outline_task_id") or ""),
            "outline_updated_at": int(manifest.get("outline_updated_at") or 0),
            "summaries": {str(k): str(v) for k, v in (manifest.get("summaries") or {}).items()},
            # 阶段三成果：剧本场次。
            "script": list(script),
            "script_versions": [dict(row) for row in (manifest.get("script_versions") or []) if isinstance(row, dict)] if include_versions else [],
            "script_version": int(manifest.get("script_version") or 0),
        }

    # ---- 章节索引与稿件 ----

    def _load_chapter_index(self, book_dir: Path, chapter_no: int) -> dict[str, Any]:
        """读章节索引（**不含正文**）；schema 1 的旧文件由调用方先升级。"""
        payload = self._read_json(self._chapter_path(book_dir, chapter_no), what=f"第 {chapter_no} 章")
        if int(payload.get("i") or 0) != chapter_no:
            raise NovelServiceError(f"第 {chapter_no} 章文件编号不一致")
        version = int(payload.get("schema_version") or 0)
        if version == 1:
            # 清单说是 schema 2 但章节文件还是旧的：数据不一致，不猜着补。
            raise NovelServiceError(
                f"第 {chapter_no} 章的数据仍是旧格式，与作品清单不一致，请重新打开作品以完成升级"
            )
        if version != SCHEMA_VERSION:
            raise NovelServiceError(f"第 {chapter_no} 章数据版本不支持：{version}")
        ids = payload.get("manuscript_ids")
        if not isinstance(ids, list) or not ids:
            raise NovelServiceError(f"第 {chapter_no} 章缺少稿件")
        for raw in ids:
            if not manuscripts.MANUSCRIPT_ID_PATTERN.match(str(raw or "")):
                raise NovelServiceError(f"第 {chapter_no} 章的稿件 ID 非法")
        return payload

    def _load_manuscript(self, book_dir: Path, chapter_no: int, manuscript_id: str) -> dict[str, Any]:
        path = self._manuscript_path(book_dir, chapter_no, manuscript_id)
        payload = self._read_json(path, what=f"第 {chapter_no} 章稿件")
        try:
            record = manuscripts.normalize(payload, chapter_no=chapter_no)
        except ValueError as exc:
            raise NovelServiceError(f"第 {chapter_no} 章稿件损坏：{exc}") from exc
        if record["id"] != str(manuscript_id):
            raise NovelServiceError(f"第 {chapter_no} 章稿件 ID 不一致")
        return record

    def _chapter_manuscripts(self, book_dir: Path, index: dict[str, Any],
                             *, with_content: bool = False) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for manuscript_id in index.get("manuscript_ids") or []:
            try:
                record = self._load_manuscript(book_dir, int(index["i"]), str(manuscript_id))
            except (LookupError, NovelServiceError):
                # 单份稿件坏了不影响其它稿件阅读，但必须能被管理界面看见。
                rows.append({
                    "id": str(manuscript_id), "kind": "broken", "kind_label": "稿件损坏",
                    "no": 0, "label": "稿件损坏", "len": 0, "created_at": 0,
                    "source_manuscript_id": "", "task_id": "", "user_request": "",
                    "broken": True,
                })
                continue
            view = manuscripts.public(record, with_content=with_content)
            view["broken"] = False
            rows.append(view)
        return rows

    def _resolve_manuscript_id(self, index: dict[str, Any], manuscript_id: Any) -> str:
        """把请求里的稿件 ID 收敛到本章真实存在的稿件；空值取基础稿件。"""
        wanted = str(manuscript_id or "").strip()
        ids = [str(raw) for raw in index.get("manuscript_ids") or []]
        if not wanted:
            base = str(index.get("base_manuscript_id") or "")
            return base if base in ids else (ids[0] if ids else "")
        if wanted not in ids:
            # 显式给了无效 ID：报错，不静默换成原文（计划 §4.2）。
            raise NovelServiceError(f"第 {index.get('i')} 章没有这份稿件：{wanted}")
        return wanted

    def _next_adapted_no(self, book_dir: Path, index: dict[str, Any]) -> int:
        """改编稿编号只增不重排：读已记录的高水位，再与现存编号取最大。"""
        stored = int(index.get("next_adapted_no") or 0)
        highs = [stored]
        for manuscript_id in index.get("manuscript_ids") or []:
            try:
                record = self._load_manuscript(book_dir, int(index["i"]), str(manuscript_id))
            except (KeyError, LookupError, NovelServiceError):
                continue
            if record["kind"] in manuscripts.ADAPTED_KINDS:
                highs.append(int(record.get("no") or 0) + 1)
        return max(1, max(highs))

    def _chapter_row(self, book_dir: Path, manifest: dict[str, Any],
                     chapter_no: int) -> dict[str, Any]:
        index = self._load_chapter_index(book_dir, chapter_no)
        rows = self._chapter_manuscripts(book_dir, index)
        base_id = str(index.get("base_manuscript_id") or "")
        base = next((row for row in rows if row["id"] == base_id), rows[0] if rows else None)
        return {
            "i": chapter_no,
            "title": str(index.get("title") or ""),
            "len": int(index.get("len") or 0),
            "base_manuscript_id": base_id,
            "base_kind": str(base.get("kind") if base else ""),
            "base_kind_label": str(base.get("kind_label") if base else ""),
            "manuscript_count": len(rows),
            "adapted_count": sum(1 for row in rows if row.get("kind") in manuscripts.ADAPTED_KINDS),
        }

    def _chapter_index(self, book_dir: Path, manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[int]]:
        """章节索引（**不含正文**）。已登记但缺失/损坏的章节进 `broken`，不静默补空。"""
        rows: list[dict[str, Any]] = []
        broken: list[int] = []
        for chapter_no in (int(n) for n in manifest.get("chapter_ids") or []):
            try:
                rows.append(self._chapter_row(book_dir, manifest, chapter_no))
            except (LookupError, NovelServiceError):
                broken.append(chapter_no)
                continue
        return rows, broken

    # ---- 导入暂存 ----

    def stage_import(self, *, filename: str, data: bytes, max_len: int) -> dict[str, Any]:
        """把上传的字节落到导入暂存并拆章，返回**索引**（不含正文）。

        源文件副本必须留着：勾选入库时要按同一份字节、同一参数重拆一次，
        这样「脚本拆的结果 = 工作台拆的结果」有确定依据，也为老作品升级时
        核对原文留下可靠依据。
        """
        if not data:
            raise NovelServiceError("文件是空的")
        if len(data) > MAX_FILE_BYTES:
            raise NovelServiceError(
                f"文件太大（{len(data) / 1024 / 1024:.1f} MB），上限 {MAX_FILE_BYTES // 1024 // 1024} MB"
            )
        import_id = self._new_import_id()
        import_dir = self._imports / import_id
        with self._lock:
            import_dir.mkdir(parents=True, exist_ok=False)
            source_path = import_dir / "source.txt"
            tmp_path = import_dir / f".{uuid.uuid4().hex}.tmp"
            try:
                tmp_path.write_bytes(data)
                os.replace(tmp_path, source_path)
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
        try:
            text, encoding = read_text_auto(source_path)
        except NovelError as exc:
            shutil.rmtree(import_dir, ignore_errors=True)
            raise NovelServiceError(str(exc)) from exc
        chapters = novel_split.split_chapters(text, max_len)
        if not chapters:
            shutil.rmtree(import_dir, ignore_errors=True)
            raise NovelServiceError("这个文件里没有可拆的章节（可能是空文件或不支持的格式）")
        level = novel_split.detect_level(text, max_len)
        meta = {
            "schema_version": SCHEMA_VERSION,
            "import_id": import_id,
            "name": _safe_name(Path(filename or "未命名.txt").name),
            "encoding": encoding,
            "bytes": len(data),
            "max_len": int(max_len),
            "split_level": level,
            "split_level_label": LEVEL_LABELS.get(level, ""),
            "total_len": sum(len(str(row["body"])) for row in chapters),
            "created_at": _now_ms(),
        }
        self._write_json(import_dir / "meta.json", meta)
        return {
            "import_id": import_id,
            "name": meta["name"],
            "encoding": encoding,
            "bytes": meta["bytes"],
            "max_len": meta["max_len"],
            "split_level": level,
            "split_level_label": meta["split_level_label"],
            "total": len(chapters),
            "total_len": meta["total_len"],
            # 两段式：只给索引，正文等入库后按章取。
            "chapters": [
                {"i": index, "title": str(row["title"]), "len": len(str(row["body"]))}
                for index, row in enumerate(chapters, start=1)
            ],
        }

    def load_import(self, import_id: str) -> dict[str, Any]:
        """读导入暂存（meta + 重拆的章节）。勾选入库与「取暂存正文」共用。"""
        import_dir = self._import_dir(import_id)
        meta = self._read_json(import_dir / "meta.json", what="导入暂存")
        source_path = import_dir / "source.txt"
        if not source_path.is_file():
            raise NovelServiceError("导入暂存的源文件已丢失，请重新导入")
        try:
            text, _encoding = read_text_auto(source_path)
        except NovelError as exc:
            raise NovelServiceError(str(exc)) from exc
        chapters = novel_split.split_chapters(text, int(meta.get("max_len") or novel_split.DEFAULT_MAX_LEN))
        return {"meta": meta, "chapters": chapters}

    def purge_imports(self, *, ttl_seconds: int = IMPORT_TTL_SECONDS) -> int:
        """清理过期的导入暂存（失败的导入不长期占盘）。返回清掉的数量。"""
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

    # ---- 作品 CRUD ----

    def create_book(self, *, import_id: str, picked: list[int], title: str = "") -> dict[str, Any]:
        """勾选章节入库：先写临时作品目录，全部成功后才提交为正式目录。

        每章写一份 `original` 稿件——导入的章节正文就是这本书的原文，
        之后的手动编辑与 AI 润色都新增改编稿，不再覆盖它。
        中断的临时目录以 `.` 开头且不匹配 BOOK_ID_PATTERN，不会出现在列表里。
        """
        staged = self.load_import(import_id)
        meta = staged["meta"]
        chapters = staged["chapters"]
        total = len(chapters)
        if not isinstance(picked, list) or not picked:
            raise NovelServiceError("至少选择一章")
        picked_nos: list[int] = []
        seen: set[int] = set()
        for raw in picked:
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise NovelServiceError("章节号必须是整数")
            if raw < 1 or raw > total:
                raise NovelServiceError(f"章节号超出范围：{raw}（共 {total} 章）")
            if raw in seen:
                continue
            seen.add(raw)
            picked_nos.append(raw)
        picked_nos.sort()
        book_title = str(title or "").strip() or Path(str(meta.get("name") or "")).stem or "未命名作品"
        if len(book_title) > TITLE_MAX:
            raise NovelServiceError(f"书名不能超过 {TITLE_MAX} 字符")

        book_id = self._new_book_id()
        final_dir = self._root / book_id
        staging_dir = self._root / f".{uuid.uuid4().hex}.tmp"
        now = _now_ms()
        chapter_ids: list[int] = []
        total_len = 0
        try:
            (staging_dir / "chapters").mkdir(parents=True, exist_ok=False)
            (staging_dir / "manuscripts").mkdir(parents=True, exist_ok=False)
            for chapter_no in picked_nos:
                row = chapters[chapter_no - 1]
                body = str(row["body"])
                record = manuscripts.build(
                    chapter_no=chapter_no, kind=manuscripts.KIND_ORIGINAL,
                    content=body, created_at=now,
                )
                self._write_json(
                    staging_dir / "manuscripts" / f"{chapter_no:04d}" / f"{record['id']}.json",
                    record,
                )
                self._write_json(staging_dir / "chapters" / f"{chapter_no:04d}.json", {
                    "schema_version": SCHEMA_VERSION,
                    "revision": 1,
                    "i": chapter_no,
                    "title": str(row["title"]),
                    "len": len(body),
                    "manuscript_ids": [record["id"]],
                    "base_manuscript_id": record["id"],
                    "next_adapted_no": 1,
                })
                chapter_ids.append(chapter_no)
                total_len += len(body)
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "revision": 1,
                "id": book_id,
                "title": book_title,
                "source": {
                    "name": str(meta.get("name") or ""),
                    "encoding": str(meta.get("encoding") or ""),
                    "bytes": int(meta.get("bytes") or 0),
                    "split_level": str(meta.get("split_level") or ""),
                    "split_level_label": str(meta.get("split_level_label") or ""),
                    "max_len": int(meta.get("max_len") or novel_split.DEFAULT_MAX_LEN),
                    "total_len": total_len,
                },
                "chapter_ids": chapter_ids,
                "origin_recovery": {
                    "restored": list(chapter_ids), "legacy": [], "reason": "",
                    "matched_import": "",
                },
                "created_at": now,
                "updated_at": now,
            }
            self._write_json(staging_dir / "book.json", manifest)
            with self._lock:
                os.replace(staging_dir, final_dir)
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise
        return self._public_manifest(manifest)

    def list_books(self) -> dict[str, Any]:
        """作品列表：只读清单，不读章节正文（老作品在这里不触发升级）。"""
        corrupt: list[str] = []
        rows: list[dict[str, Any]] = []
        if self._root.exists():
            for path in sorted(self._root.iterdir()):
                if not path.is_dir() or not BOOK_ID_PATTERN.match(path.name):
                    continue
                try:
                    manifest = self._load_manifest(path.name, upgrade=False)
                except (LookupError, NovelServiceError):
                    corrupt.append(path.name)
                    continue
                rows.append(self._public_manifest(manifest, include_versions=False))
        rows.sort(key=lambda row: int(row.get("updated_at") or 0), reverse=True)
        return {"books": rows, "corrupt": corrupt}

    def get_book(self, book_id: str) -> dict[str, Any]:
        """作品详情：清单 + 章节索引（**不含正文**）。"""
        manifest = self._load_manifest(book_id)
        book_dir = self._book_dir(book_id)
        rows, broken = self._chapter_index(book_dir, manifest)
        return {
            "book": self._public_manifest(manifest),
            "chapters": rows,
            # 已登记但缺失/损坏：进入「数据异常」状态，禁止静默补空正文。
            "broken": broken,
            "location": str(book_dir),
        }

    def get_chapter(self, book_id: str, chapter_no: int) -> dict[str, Any]:
        """单章索引 + 稿件清单 + **基础稿件的正文**（两段式的第二段）。

        其它稿件的正文按需单独取：一份原作可能有十几份改编稿，
        全量下发会让每次点开一章都传输整章的多份副本。
        """
        manifest = self._load_manifest(book_id)
        if chapter_no not in {int(n) for n in manifest.get("chapter_ids") or []}:
            raise LookupError("章节不存在")
        book_dir = self._book_dir(book_id)
        index = self._load_chapter_index(book_dir, chapter_no)
        rows = self._chapter_manuscripts(book_dir, index)
        base_id = self._resolve_manuscript_id(index, "")
        base = self._load_manuscript(book_dir, chapter_no, base_id) if base_id else None
        return {
            "i": int(index.get("i") or chapter_no),
            "title": str(index.get("title") or ""),
            "len": int(index.get("len") or 0),
            # 乐观并发：前端改完写回时带上它，对不上就说明别处已经改过。
            "revision": int(index.get("revision") or 0),
            "base_manuscript_id": base_id,
            "manuscripts": rows,
            # 兼容字段：内容 = 基础稿件的正文（原文 / 升级前正文）。
            "content": str(base.get("content") if base else ""),
            "content_manuscript_id": base_id,
        }

    def get_manuscript(self, book_id: str, chapter_no: int, manuscript_id: str) -> dict[str, Any]:
        """单份稿件的正文（点开某张卡片时才取）。"""
        manifest = self._load_manifest(book_id)
        if chapter_no not in {int(n) for n in manifest.get("chapter_ids") or []}:
            raise LookupError("章节不存在")
        book_dir = self._book_dir(book_id)
        index = self._load_chapter_index(book_dir, chapter_no)
        resolved = self._resolve_manuscript_id(index, manuscript_id)
        record = self._load_manuscript(book_dir, chapter_no, resolved)
        view = manuscripts.public(record)
        view["i"] = chapter_no
        return view

    def update_chapter(self, book_id: str, chapter_no: int,
                       *, title: Any = None, content: Any = None,
                       expected_revision: Any = None,
                       base_manuscript_id: Any = None,
                       task_id: str = "", user_request: str = "") -> dict[str, Any]:
        """改章节：标题直接改索引；正文**新增一份改编稿**，不覆盖原文。

        `expected_revision` 给了就做乐观并发校验：前端读到的是第 N 版，写回时必须
        仍是第 N 版，否则说明别处已经改过（例如 AI 结果写回），拒绝并让调用方重读。

        正文与基础稿件完全一致时不新增记录（内容未改变不重复建卡）。
        """
        manifest = self._load_manifest(book_id)
        if chapter_no not in {int(n) for n in manifest.get("chapter_ids") or []}:
            raise LookupError("章节不存在")
        book_dir = self._book_dir(book_id)
        index = self._load_chapter_index(book_dir, chapter_no)
        if expected_revision is not None:
            if isinstance(expected_revision, bool):
                raise NovelServiceError("revision 必须是整数")
            try:
                expected = int(expected_revision)
            except (TypeError, ValueError):
                raise NovelServiceError("revision 必须是整数") from None
            actual = int(index.get("revision") or 0)
            if expected != actual:
                raise NovelConflictError(
                    f"这一章已被修改过（你看到的是第 {expected} 版，现在是第 {actual} 版），请重新读取后再改"
                )
        next_title = str(index.get("title") or "")
        if title is not None:
            next_title = str(title).strip()
            if not next_title:
                raise NovelServiceError("章节标题不能为空")
            if len(next_title) > TITLE_MAX:
                raise NovelServiceError(f"章节标题不能超过 {TITLE_MAX} 字符")
        created: dict[str, Any] | None = None
        if content is not None:
            if not isinstance(content, str):
                raise NovelServiceError("章节正文必须是文本")
            if not content.strip():
                raise NovelServiceError("章节正文不能为空")
            created = self.save_manuscript(
                book_id, chapter_no, content=content, kind=manuscripts.KIND_MANUAL,
                source_manuscript_id=base_manuscript_id, task_id=task_id,
                user_request=user_request, bump_manifest=False,
            )["manuscript"]
        # 标题变更或新增稿件后重读索引（save_manuscript 已经写过一次文件）。
        index = self._load_chapter_index(book_dir, chapter_no)
        index["title"] = next_title
        index["revision"] = int(index.get("revision") or 0) + 1
        with self._lock:
            self._write_json(self._chapter_path(book_dir, chapter_no), index)
            self._bump_manifest(book_dir, manifest)
        result = {
            "i": chapter_no,
            "title": next_title,
            "len": int(index.get("len") or 0),
            "revision": int(index["revision"]),
            "manuscripts": self._chapter_manuscripts(book_dir, index),
            "created_manuscript": created,
        }
        base_id = self._resolve_manuscript_id(index, "")
        base = self._load_manuscript(book_dir, chapter_no, base_id) if base_id else None
        result["base_manuscript_id"] = base_id
        result["content"] = str(base.get("content") if base else "")
        return result

    def save_manuscript(self, book_id: str, chapter_no: int, *,
                        content: Any, kind: str = manuscripts.KIND_MANUAL,
                        source_manuscript_id: Any = None, task_id: str = "",
                        user_request: str = "", label: str = "",
                        expected_revision: Any = None,
                        bump_manifest: bool = True) -> dict[str, Any]:
        """新增一份改编稿（人工编辑或 AI 改编）。

        内容与来源稿件逐字相同 → 不新增记录，直接返回来源稿件（前端不会出现
        重复卡片，重复点保存也不会叠加）。
        """
        manifest = self._load_manifest(book_id)
        if chapter_no not in {int(n) for n in manifest.get("chapter_ids") or []}:
            raise LookupError("章节不存在")
        if kind not in manuscripts.ADAPTED_KINDS:
            raise NovelServiceError("只能新增人工改编稿或 AI 改编稿")
        if not isinstance(content, str) or not content.strip():
            raise NovelServiceError("改编稿正文不能为空")
        book_dir = self._book_dir(book_id)
        index = self._load_chapter_index(book_dir, chapter_no)
        if expected_revision is not None and expected_revision != "":
            if isinstance(expected_revision, bool):
                raise NovelServiceError("revision 必须是整数")
            try:
                expected = int(expected_revision)
            except (TypeError, ValueError):
                raise NovelServiceError("revision 必须是整数") from None
            if expected != int(index.get("revision") or 0):
                raise NovelConflictError(
                    f"这一章已被修改过（你看到的是第 {expected} 版，现在是第 {int(index.get('revision') or 0)} 版），"
                    "请重新读取后再保存"
                )
        source_id = self._resolve_manuscript_id(index, source_manuscript_id)
        # 重复保存 / 重复点按钮：与同来源稿件逐字相同就不再建新记录。
        for existing_id in index.get("manuscript_ids") or []:
            try:
                existing = self._load_manuscript(book_dir, chapter_no, str(existing_id))
            except (LookupError, NovelServiceError):
                continue
            if str(existing.get("source_manuscript_id") or "") == source_id and existing["content"] == content:
                return {"manuscript": existing, "duplicate": True, "book": self._public_manifest(manifest)}
        no = self._next_adapted_no(book_dir, index)
        record = manuscripts.build(
            chapter_no=chapter_no, kind=kind, content=content, no=no,
            source_manuscript_id=source_id, task_id=task_id,
            user_request=user_request, label=label,
        )
        delta = len(content.encode("utf-8"))
        self._check_book_capacity(book_dir, extra_bytes=delta)
        index.setdefault("manuscript_ids", []).append(record["id"])
        index["next_adapted_no"] = no + 1
        index["len"] = len(content)
        index["revision"] = int(index.get("revision") or 0) + 1
        with self._lock:
            self._write_json(self._manuscript_path(book_dir, chapter_no, record["id"]), record)
            self._write_json(self._chapter_path(book_dir, chapter_no), index)
            if bump_manifest:
                self._bump_manifest(book_dir, manifest)
        return {
            "manuscript": manuscripts.public(record),
            "duplicate": False,
            "revision": int(index["revision"]),
            "book": self._public_manifest(manifest) if bump_manifest else None,
        }

    def delete_manuscript(self, book_id: str, chapter_no: int, manuscript_id: str) -> dict[str, Any]:
        """删一份稿件。删掉该章最后一份稿件 = 移除这一章（至少保留一章）。"""
        manifest = self._load_manifest(book_id)
        ids = [int(n) for n in manifest.get("chapter_ids") or []]
        if chapter_no not in ids:
            raise LookupError("章节不存在")
        book_dir = self._book_dir(book_id)
        index = self._load_chapter_index(book_dir, chapter_no)
        target = self._resolve_manuscript_id(index, manuscript_id)
        remaining = [str(raw) for raw in index.get("manuscript_ids") or [] if str(raw) != target]
        if not remaining:
            if len(ids) <= 1:
                raise NovelServiceError("作品至少要保留一章；要整本删除请删作品")
            # 该章已无稿件：按「移除章节」处理，连带清掉稿件目录。
            with self._lock:
                shutil.rmtree(self._manuscript_dir(book_dir, chapter_no), ignore_errors=True)
                self._chapter_path(book_dir, chapter_no).unlink(missing_ok=True)
                manifest["chapter_ids"] = [n for n in ids if n != chapter_no]
                self._bump_manifest(book_dir, manifest)
            return {"ok": True, "removed_chapter": True, "chapter_ids": manifest["chapter_ids"]}
        index["manuscript_ids"] = remaining
        if str(index.get("base_manuscript_id") or "") == target:
            # 原文被删：基础稿件顺延到剩下的第一份，不再谎称还有原文。
            index["base_manuscript_id"] = remaining[0]
        with self._lock:
            self._write_json(self._chapter_path(book_dir, chapter_no), index)
            try:
                self._manuscript_path(book_dir, chapter_no, target).unlink(missing_ok=True)
            except OSError as exc:
                # 清单已更新但文件没删掉：如实报错，重试可清理（不留下指向已删稿件之外的坏记录）。
                raise NovelServiceError(f"稿件文件删除失败：{exc}（清单已更新，可重试清理）") from exc
            self._bump_manifest(book_dir, manifest)
        return {"ok": True, "removed_chapter": False, "manuscript_ids": remaining}

    def delete_chapter(self, book_id: str, chapter_no: int) -> dict[str, Any]:
        """删章节：先删稿件与索引再更新清单；中断留下的孤儿章节下次保存时忽略。"""
        manifest = self._load_manifest(book_id)
        ids = [int(n) for n in manifest.get("chapter_ids") or []]
        if chapter_no not in ids:
            raise LookupError("章节不存在")
        if len(ids) <= 1:
            raise NovelServiceError("作品至少要保留一章；要整本删除请删作品")
        book_dir = self._book_dir(book_id)
        with self._lock:
            shutil.rmtree(self._manuscript_dir(book_dir, chapter_no), ignore_errors=True)
            try:
                self._chapter_path(book_dir, chapter_no).unlink(missing_ok=True)
            except OSError as exc:
                raise NovelServiceError(f"删除章节失败：{exc}") from exc
            manifest["chapter_ids"] = [n for n in ids if n != chapter_no]
            self._bump_manifest(book_dir, manifest)
        return {"ok": True, "chapter_count": len(manifest["chapter_ids"])}

    def delete_book(self, book_id: str) -> dict[str, Any]:
        """删作品：作用范围严格限定为该作品目录，不波及其它作品。"""
        book_dir = self._book_dir(book_id)
        if not book_dir.exists():
            raise LookupError("作品不存在")
        with self._lock:
            shutil.rmtree(book_dir, ignore_errors=False)
        return {"ok": True}

    def _bump_manifest(self, book_dir: Path, manifest: dict[str, Any]) -> None:
        manifest["revision"] = int(manifest.get("revision") or 0) + 1
        manifest["updated_at"] = _now_ms()
        self._write_json(book_dir / "book.json", manifest)

    # ---- 阶段素材与成果（阶段二起） ----

    def stage_material(self, book_id: str, chapter_ids: list[int],
                       manuscripts_by_chapter: dict[int, str] | None = None) -> dict[str, Any]:
        """取一次生成要用的素材：清单 + 选中章节的**选定稿件**正文 + 已有摘要链。

        正文**只在服务端组装 prompt 时读**，绝不整本回传给前端（两段式）。
        `manuscripts_by_chapter` 为空时按「每章的基础稿件」（原文）取；
        显式给了不属于该章的稿件 ID 会报错，不静默换成原文（计划 §4.2）。
        """
        manifest = self._load_manifest(book_id)
        book_dir = self._book_dir(book_id)
        registered = {int(n) for n in manifest.get("chapter_ids") or []}
        wanted = manuscripts_by_chapter if isinstance(manuscripts_by_chapter, dict) else {}
        picked: list[int] = []
        for raw in chapter_ids:
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise NovelServiceError("章节号必须是整数")
            if raw not in registered:
                raise LookupError(f"第 {raw} 章不在这个作品里")
            if raw in picked:
                continue
            picked.append(raw)
        picked.sort()
        chapters: list[dict[str, Any]] = []
        for number in picked:
            index = self._load_chapter_index(book_dir, number)
            manuscript_id = self._resolve_manuscript_id(index, wanted.get(number) or wanted.get(str(number)))
            record = self._load_manuscript(book_dir, number, manuscript_id)
            chapters.append({
                "i": number,
                "title": str(index.get("title") or ""),
                "content": str(record.get("content") or ""),
                "manuscript_id": manuscript_id,
                "manuscript_kind": str(record.get("kind") or ""),
                "manuscript_label": str(record.get("label") or ""),
                "manuscript_no": int(record.get("no") or 0),
                "content_len": int(record.get("len") or 0),
            })
        return {"manifest": manifest, "chapters": chapters, "chapter_ids": picked}

    def apply_elements(self, book_id: str, *, task_id: str, raw: str,
                       expected_revision: Any = None, chapter_ids: list[int] | None = None,
                       manuscripts_by_chapter: dict[int, str] | None = None) -> dict[str, Any]:
        """要素写回：原文整段留存 + 尽力拆成人物/场景/道具三类。

        拆分只按列表项前缀归并，识别不了就整段留在 `raw` 里——**不猜、不编**，
        用户在界面上看到的就是模型写的东西。
        """
        manifest = self._check_apply_revision(book_id, expected_revision)
        book_dir = self._book_dir(book_id)
        text = str(raw or "")
        if not text.strip():
            raise NovelServiceError("要素结果为空，没有可写回的内容")
        self._reject_duplicate_task(manifest.get("elements_versions"), task_id)
        buckets = _split_elements(text)
        versions = manifest.get("elements_versions") if isinstance(manifest.get("elements_versions"), list) else []
        if not versions and str((manifest.get("elements") or {}).get("raw") or "").strip():
            previous = manifest["elements"]
            versions = [{
                "id": _new_version_id("el"),
                "version": 1, "raw": str(previous.get("raw") or ""),
                "updated_at": int(previous.get("updated_at") or 0),
                "task_id": str(previous.get("task_id") or ""),
            }]
        versions = self._ensure_version_ids(versions, "el")
        version = len(versions) + 1
        entry = {"id": _new_version_id("el"), "version": version, "raw": text,
                 "updated_at": _now_ms(), "task_id": str(task_id or "")}
        entry["chapter_ids"] = list(chapter_ids or [])
        entry["manuscripts"] = self._manuscript_refs(manuscripts_by_chapter)
        versions.append(entry)
        manifest["elements_versions"] = versions
        manifest["elements"] = {
            "characters": buckets["characters"],
            "scenes": buckets["scenes"],
            "props": buckets["props"],
            "raw": text,
            "updated_at": _now_ms(),
            "task_id": str(task_id or ""),
            "version": version,
            "version_id": entry["id"],
        }
        with self._lock:
            self._bump_manifest(book_dir, manifest)
        return self._public_manifest(manifest)

    def apply_outline(self, book_id: str, *, task_id: str, text: str,
                      expected_revision: Any = None, chapter_ids: list[int] | None = None,
                      manuscripts_by_chapter: dict[int, str] | None = None) -> dict[str, Any]:
        """大纲写回（三幕结构整段留存，阶段三按它生成场次）。"""
        manifest = self._check_apply_revision(book_id, expected_revision)
        book_dir = self._book_dir(book_id)
        body = str(text or "")
        if not body.strip():
            raise NovelServiceError("大纲结果为空，没有可写回的内容")
        self._reject_duplicate_task(manifest.get("outline_versions"), task_id)
        versions = manifest.get("outline_versions") if isinstance(manifest.get("outline_versions"), list) else []
        if not versions and str(manifest.get("outline") or "").strip():
            versions = [{
                "id": _new_version_id("ol"),
                "version": 1, "text": str(manifest.get("outline") or ""),
                "updated_at": int(manifest.get("outline_updated_at") or 0),
                "task_id": str(manifest.get("outline_task_id") or ""),
            }]
        versions = self._ensure_version_ids(versions, "ol")
        version = len(versions) + 1
        entry = {"id": _new_version_id("ol"), "version": version, "text": body,
                 "updated_at": _now_ms(), "task_id": str(task_id or ""),
                 "chapter_ids": list(chapter_ids or []),
                 "manuscripts": self._manuscript_refs(manuscripts_by_chapter)}
        versions.append(entry)
        manifest["outline_versions"] = versions
        manifest["outline"] = body
        manifest["outline_version"] = version
        manifest["outline_version_id"] = entry["id"]
        manifest["outline_task_id"] = str(task_id or "")
        manifest["outline_updated_at"] = _now_ms()
        with self._lock:
            self._bump_manifest(book_dir, manifest)
        return self._public_manifest(manifest)

    def apply_script(self, book_id: str, *, task_id: str, text: str,
                     from_chapter: int = 0, chapter_ids: list[int] | None = None,
                     expected_revision: Any = None,
                     manuscripts_by_chapter: dict[int, str] | None = None) -> dict[str, Any]:
        """剧本写回：解析并保存场次快照，按所选章节替换现有场次。

        解析失败时 `scene` 留空、`parsed=False`，前端明示「未能识别场次结构，
        已按整段保存」——**绝不臆造场次切分**（《阶段三》§五 P1）。
        """
        manifest = self._check_apply_revision(book_id, expected_revision)
        book_dir = self._book_dir(book_id)
        body = str(text or "")
        if not body.strip():
            raise NovelServiceError("剧本结果为空，没有可写回的内容")
        versions = manifest.get("script_versions") if isinstance(manifest.get("script_versions"), list) else []
        self._reject_duplicate_task(versions, task_id)
        source_chapters = sorted({int(value) for value in (chapter_ids or [])})
        if not source_chapters and from_chapter:
            source_chapters = [int(from_chapter)]
        primary_chapter = source_chapters[0] if source_chapters else int(from_chapter or 0)
        previous_task_id = str(manifest.get("script_task_id") or "")
        previous_updated_at = int(manifest.get("script_updated_at") or 0)
        scenes, parsed = prompts.parse_scenes(body, from_chapter=primary_chapter)
        if not parsed:
            scenes = [{"scene": "", "content": body, "from_chapter": primary_chapter}]
        existing = manifest.get("script") if isinstance(manifest.get("script"), list) else []
        replacement_chapters = set(source_chapters)
        kept = [row for row in existing if int(row.get("from_chapter") or 0) not in replacement_chapters] if replacement_chapters else []
        merged = kept + scenes
        manifest["script"] = [
            {**row, "i": index}
            for index, row in enumerate(merged, start=1)
        ]
        manifest["script_task_id"] = str(task_id or "")
        manifest["script_updated_at"] = _now_ms()
        manifest["script_parsed"] = bool(parsed)
        if not versions and existing:
            versions = [{"id": _new_version_id("sc"), "version": 1, "script": existing,
                         "updated_at": previous_updated_at, "task_id": previous_task_id,
                         "chapter_ids": []}]
        versions = self._ensure_version_ids(versions, "sc")
        version = len(versions) + 1
        entry = {"id": _new_version_id("sc"), "version": version, "script": manifest["script"],
                 "updated_at": _now_ms(), "task_id": str(task_id or ""),
                 "chapter_ids": source_chapters,
                 "manuscripts": self._manuscript_refs(manuscripts_by_chapter)}
        versions.append(entry)
        manifest["script_versions"] = versions
        manifest["script_version"] = version
        manifest["script_version_id"] = entry["id"]
        with self._lock:
            self._bump_manifest(book_dir, manifest)
        result = self._public_manifest(manifest)
        result["script_parsed"] = bool(parsed)
        return result

    @staticmethod
    def _ensure_version_ids(versions: list[Any], prefix: str) -> list[Any]:
        """老版本记录补稳定 ID（只在内存里补，随下一次写回落盘）。"""
        for row in versions:
            if isinstance(row, dict) and not VERSION_ID_PATTERN.match(str(row.get("id") or "")):
                row["id"] = _new_version_id(prefix)
        return versions

    @staticmethod
    def _manuscript_refs(manuscripts_by_chapter: dict[int, str] | None) -> list[dict[str, Any]]:
        """成果的来源稿件说明（删除稿件后仍能显示「源记录已删除」）。"""
        if not isinstance(manuscripts_by_chapter, dict):
            return []
        rows: list[dict[str, Any]] = []
        for key, value in manuscripts_by_chapter.items():
            try:
                chapter_no = int(key)
            except (TypeError, ValueError):
                continue
            rows.append({"chapter_no": chapter_no, "manuscript_id": str(value or "")})
        rows.sort(key=lambda row: row["chapter_no"])
        return rows

    @staticmethod
    def _sync_latest_script_version(manifest: dict[str, Any]) -> None:
        versions = manifest.get("script_versions")
        current = int(manifest.get("script_version") or 0)
        if isinstance(versions, list):
            for row in versions:
                if isinstance(row, dict) and int(row.get("version") or 0) == current:
                    row["script"] = list(manifest.get("script") or [])
                    break

    @staticmethod
    def _reject_duplicate_task(versions: Any, task_id: str) -> None:
        if task_id and isinstance(versions, list) and any(
            isinstance(row, dict) and str(row.get("task_id") or "") == task_id for row in versions
        ):
            raise NovelConflictError("这个任务已经保存过，不能重复创建版本")

    def _check_apply_revision(self, book_id: str, expected_revision: Any) -> dict[str, Any]:
        """写回前核对作品版本：对不上说明别处已经改过（A 未完又生成 B），拒绝覆盖。"""
        manifest = self._load_manifest(book_id)
        if expected_revision is None or expected_revision == "":
            return manifest
        if isinstance(expected_revision, bool):
            raise NovelServiceError("revision 必须是整数")
        try:
            expected = int(expected_revision)
        except (TypeError, ValueError):
            raise NovelServiceError("revision 必须是整数") from None
        actual = int(manifest.get("revision") or 0)
        if expected != actual:
            raise NovelConflictError(
                f"作品已被修改过（你看到的是第 {expected} 版，现在是第 {actual} 版）；"
                "请重新读取作品后再写回，避免覆盖别人的修改"
            )
        return manifest

    # ---- 剧本场次（阶段三） ----

    def list_script(self, book_id: str) -> dict[str, Any]:
        manifest = self._load_manifest(book_id)
        rows = manifest.get("script") if isinstance(manifest.get("script"), list) else []
        return {
            "script": list(rows),
            "revision": int(manifest.get("revision") or 0),
            "parsed": bool(manifest.get("script_parsed", True)),
        }

    def update_scene(self, book_id: str, scene_no: int, *,
                     scene: Any = None, content: Any = None,
                     expected_revision: Any = None) -> dict[str, Any]:
        manifest = self._check_apply_revision(book_id, expected_revision)
        book_dir = self._book_dir(book_id)
        rows = manifest.get("script") if isinstance(manifest.get("script"), list) else []
        index = self._scene_index(rows, scene_no)
        row = dict(rows[index])
        if scene is not None:
            row["scene"] = str(scene).strip()
        if content is not None:
            if not isinstance(content, str):
                raise NovelServiceError("场次正文必须是文本")
            row["content"] = content
        if not str(row.get("scene") or "").strip() and not str(row.get("content") or "").strip():
            raise NovelServiceError("场景行与正文不能同时为空")
        rows[index] = row
        manifest["script"] = rows
        self._sync_latest_script_version(manifest)
        with self._lock:
            self._bump_manifest(book_dir, manifest)
        return {"scene": row, "revision": int(manifest.get("revision") or 0)}

    def delete_scene(self, book_id: str, scene_no: int) -> dict[str, Any]:
        manifest = self._load_manifest(book_id)
        book_dir = self._book_dir(book_id)
        rows = manifest.get("script") if isinstance(manifest.get("script"), list) else []
        index = self._scene_index(rows, scene_no)
        rows.pop(index)
        # 重排编号：i 从 1 连续，避免删掉中间一条后编号出现空洞。
        manifest["script"] = [{**row, "i": pos} for pos, row in enumerate(rows, start=1)]
        self._sync_latest_script_version(manifest)
        with self._lock:
            self._bump_manifest(book_dir, manifest)
        return {"ok": True, "count": len(manifest["script"])}

    def reorder_script(self, book_id: str, order: Any) -> dict[str, Any]:
        """按给定顺序重排场次：只改顺序，**保留 `from_chapter` 来源**。"""
        manifest = self._load_manifest(book_id)
        book_dir = self._book_dir(book_id)
        rows = manifest.get("script") if isinstance(manifest.get("script"), list) else []
        if not rows:
            raise NovelServiceError("这个作品还没有剧本场次")
        if not isinstance(order, list) or not order:
            raise NovelServiceError("order 必须是非空的场次号数组")
        wanted: list[int] = []
        for raw in order:
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise NovelServiceError("场次号必须是整数")
            wanted.append(raw)
        current = [int(row.get("i") or 0) for row in rows]
        if sorted(wanted) != sorted(current):
            raise NovelServiceError("order 必须恰好包含现有全部场次号，且不能重复")
        by_no = {int(row.get("i") or 0): row for row in rows}
        manifest["script"] = [
            {**by_no[no], "i": pos}
            for pos, no in enumerate(wanted, start=1)
        ]
        with self._lock:
            self._bump_manifest(book_dir, manifest)
        return {"script": manifest["script"], "revision": int(manifest.get("revision") or 0)}

    @staticmethod
    def _scene_index(rows: list[Any], scene_no: int) -> int:
        if isinstance(scene_no, bool) or not isinstance(scene_no, int):
            raise NovelServiceError("场次号必须是整数")
        for index, row in enumerate(rows):
            if int(row.get("i") or 0) == scene_no:
                return index
        raise LookupError(f"场次不存在：{scene_no}")

    def update_summaries(self, book_id: str, summaries: dict[str, str]) -> dict[str, Any]:
        """合并每章摘要（滚动摘要链的原料，零额外推理）。

        摘要按**具体稿件**记录：用改编稿生成的摘要不会顶掉原文的摘要
        （键 = `章号@稿件 ID`），否则换回原文时会拿改编稿的摘要当前文。
        """
        manifest = self._load_manifest(book_id)
        book_dir = self._book_dir(book_id)
        merged = dict(manifest.get("summaries") or {})
        for key, value in (summaries or {}).items():
            text = str(value or "").strip()
            if text:
                merged[str(key)] = text
        if merged == (manifest.get("summaries") or {}):
            return self._public_manifest(manifest)
        manifest["summaries"] = merged
        with self._lock:
            self._bump_manifest(book_dir, manifest)
        return self._public_manifest(manifest)

    # ---- 记录管理与批量删除 ----

    def list_records(self, book_id: str) -> dict[str, Any]:
        """管理记录用的全量清单（正文按需另取，不在这里下发）。"""
        manifest = self._load_manifest(book_id)
        book_dir = self._book_dir(book_id)
        chapters: list[dict[str, Any]] = []
        manuscripts: list[dict[str, Any]] = []
        broken: list[int] = []
        for chapter_no in (int(n) for n in manifest.get("chapter_ids") or []):
            try:
                index = self._load_chapter_index(book_dir, chapter_no)
            except (LookupError, NovelServiceError):
                broken.append(chapter_no)
                continue
            chapters.append({"i": chapter_no, "title": str(index.get("title") or "")})
            for row in self._chapter_manuscripts(book_dir, index):
                manuscripts.append({**row, "chapter_no": chapter_no,
                                    "chapter_title": str(index.get("title") or "")})
        elements = manifest.get("elements_versions") if isinstance(manifest.get("elements_versions"), list) else []
        outlines = manifest.get("outline_versions") if isinstance(manifest.get("outline_versions"), list) else []
        script_versions = manifest.get("script_versions") if isinstance(manifest.get("script_versions"), list) else []
        return {
            "book": self._public_manifest(manifest),
            "chapters": chapters,
            "broken": broken,
            "manuscripts": manuscripts,
            "elements": [self._version_row(row, "el") for row in elements if isinstance(row, dict)],
            "outlines": [self._version_row(row, "ol") for row in outlines if isinstance(row, dict)],
            "scripts": [self._script_version_row(row) for row in script_versions if isinstance(row, dict)],
            "location": str(book_dir),
        }

    @staticmethod
    def _version_row(row: dict[str, Any], prefix: str) -> dict[str, Any]:
        return {
            "id": str(row.get("id") or ""),
            "version": int(row.get("version") or 0),
            "len": len(str(row.get("raw") or row.get("text") or "")),
            "updated_at": int(row.get("updated_at") or 0),
            "task_id": str(row.get("task_id") or ""),
            "chapter_ids": [int(n) for n in (row.get("chapter_ids") or [])],
            "manuscripts": [dict(item) for item in (row.get("manuscripts") or []) if isinstance(item, dict)],
            "kind": "elements" if prefix == "el" else "outline",
        }

    @staticmethod
    def _script_version_row(row: dict[str, Any]) -> dict[str, Any]:
        scenes = row.get("script") if isinstance(row.get("script"), list) else []
        return {
            "id": str(row.get("id") or ""),
            "version": int(row.get("version") or 0),
            "scene_count": len(scenes),
            "updated_at": int(row.get("updated_at") or 0),
            "task_id": str(row.get("task_id") or ""),
            "chapter_ids": [int(n) for n in (row.get("chapter_ids") or [])],
            "manuscripts": [dict(item) for item in (row.get("manuscripts") or []) if isinstance(item, dict)],
            "kind": "script",
        }

    def delete_records(self, book_id: str, items: list[dict[str, Any]],
                       expected_revision: Any = None) -> dict[str, Any]:
        """批量删除管理记录（稿件 / 要素版本 / 大纲版本 / 剧本版本）。

        统一校验：作品归属、记录存在、允许的删除范围。无效项**不执行任何删除**，
        整批拒绝——避免「删了一半」这种半成品状态（计划 §6.3）。
        """
        if not isinstance(items, list) or not items:
            raise NovelServiceError("请选择要删除的记录")
        manifest = self._check_apply_revision(book_id, expected_revision)
        book_dir = self._book_dir(book_id)
        plans: list[tuple[str, dict[str, Any]]] = []
        for raw in items:
            if not isinstance(raw, dict):
                raise NovelServiceError("删除项必须是对象")
            kind = str(raw.get("type") or "").strip()
            if kind == "manuscript":
                chapter_no = raw.get("chapter_no")
                if isinstance(chapter_no, bool) or not isinstance(chapter_no, int):
                    raise NovelServiceError("删除稿件必须说明章节号")
                manuscript_id = str(raw.get("id") or "").strip()
                index = self._load_chapter_index(book_dir, chapter_no)
                resolved = self._resolve_manuscript_id(index, manuscript_id)
                plans.append(("manuscript", {"chapter_no": chapter_no, "id": resolved}))
            elif kind in ("elements", "outline", "script"):
                plans.append((kind, {"id": str(raw.get("id") or "").strip()}))
            else:
                raise NovelServiceError(f"不支持的删除类型：{kind or '（空）'}")
        # 全部校验通过后才开始改盘。
        removed: list[dict[str, Any]] = []
        if any(kind == "manuscript" for kind, _ in plans):
            for kind, payload in plans:
                if kind != "manuscript":
                    continue
                self.delete_manuscript(book_id, payload["chapter_no"], payload["id"])
                removed.append({"type": "manuscript", **payload})
        version_targets: dict[str, list[str]] = {"elements": [], "outline": [], "script": []}
        for kind, payload in plans:
            if kind in version_targets:
                version_targets[kind].append(payload["id"])
        manifest = self._load_manifest(book_id)
        changed = False
        if version_targets["elements"]:
            versions = [row for row in (manifest.get("elements_versions") or [])
                        if str(row.get("id") or "") not in set(version_targets["elements"])]
            if len(versions) != len(manifest.get("elements_versions") or []):
                manifest["elements_versions"] = versions
                changed = True
        if version_targets["outline"]:
            versions = [row for row in (manifest.get("outline_versions") or [])
                        if str(row.get("id") or "") not in set(version_targets["outline"])]
            if len(versions) != len(manifest.get("outline_versions") or []):
                manifest["outline_versions"] = versions
                changed = True
        if version_targets["script"]:
            versions = [row for row in (manifest.get("script_versions") or [])
                        if str(row.get("id") or "") not in set(version_targets["script"])]
            if len(versions) != len(manifest.get("script_versions") or []):
                manifest["script_versions"] = versions
                changed = True
        if changed:
            with self._lock:
                self._bump_manifest(book_dir, manifest)
        for kind in version_targets:
            for version_id in version_targets[kind]:
                removed.append({"type": kind, "id": version_id})
        return {"ok": True, "removed": removed, "book": self._public_manifest(self._load_manifest(book_id))}

    # ---- 导出 ----

    def export_book(self, book_id: str, *, fmt: str = "txt", with_extras: bool = False,
                    manuscript_ids: dict[int, str] | None = None) -> str:
        """整本导出：按清单顺序拼正文。

        `manuscript_ids` 给了就按「章号 → 稿件 ID」导出选定的稿件；否则每章用基础
        稿件（原文）。`with_extras=True` 时在正文后附要素与大纲。
        标题只写一次：`book.json` 的清单是权威顺序，不重复也不漏章。
        """
        manifest = self._load_manifest(book_id)
        book_dir = self._book_dir(book_id)
        wanted = manuscript_ids if isinstance(manuscript_ids, dict) else {}
        title = str(manifest.get("title") or "未命名作品")
        parts: list[str] = []
        if fmt == "md":
            parts.append(f"# {title}\n")
        for chapter_no in (int(n) for n in manifest.get("chapter_ids") or []):
            try:
                index = self._load_chapter_index(book_dir, chapter_no)
                manuscript_id = self._resolve_manuscript_id(index, wanted.get(chapter_no) or wanted.get(str(chapter_no)))
                record = self._load_manuscript(book_dir, chapter_no, manuscript_id)
            except (LookupError, NovelServiceError):
                continue
            chapter_title = str(index.get("title") or f"第 {chapter_no} 章")
            body = str(record.get("content") or "")
            if fmt == "md":
                parts.append(f"## {chapter_title}\n\n{body}\n")
            else:
                parts.append(f"{chapter_title}\n\n{body}\n\n")
        if with_extras:
            parts.append(self._extras_block(manifest, fmt))
        return "".join(parts)

    @staticmethod
    def _extras_block(manifest: dict[str, Any], fmt: str) -> str:
        """要素与大纲附录（只有存在时才写，不生成空标题）。"""
        elements = manifest.get("elements") if isinstance(manifest.get("elements"), dict) else {}
        outline = str(manifest.get("outline") or "")
        parts: list[str] = []
        if fmt == "md":
            if elements.get("raw") or elements.get("characters") or elements.get("scenes") or elements.get("props"):
                parts.append("\n# 附录：要素\n")
                parts.append(str(elements.get("raw") or "") + "\n")
            if outline.strip():
                parts.append("\n# 附录：改编大纲\n")
                parts.append(outline.strip() + "\n")
        else:
            if elements.get("raw") or elements.get("characters") or elements.get("scenes") or elements.get("props"):
                parts.append("\n==== 附录：要素 ====\n\n")
                parts.append(str(elements.get("raw") or "") + "\n\n")
            if outline.strip():
                parts.append("==== 附录：改编大纲 ====\n\n")
                parts.append(outline.strip() + "\n\n")
        return "".join(parts)


class NovelService:
    """工作台「创作场」页的业务门面：API 方法返回 `(payload, status)` 二元组。"""

    def __init__(self, app: Any):
        self.app = app
        self.store = NovelStore(app.paths.data_dir)

    def recover(self) -> int:
        """启动时清理过期导入暂存（失败导入不长期占盘）。"""
        return self.store.purge_imports()

    # ---- 路径与数据位置 ----

    def data_location(self) -> dict[str, Any]:
        """作品实际保存目录（后端解析路径，前端不猜）。"""
        return {
            "data_dir": str(Path(self.app.paths.data_dir).resolve()),
            "novel_root": str(self.store.root().resolve()),
        }

    # ---- API：导入与作品 ----

    def api_import(self, *, filename: str, data: bytes, max_len: Any = None) -> tuple[dict[str, Any], int]:
        try:
            length = self._max_len(max_len)
            return self.store.stage_import(filename=filename, data=data, max_len=length), 200
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    def api_import_chapter(self, import_id: str, chapter_no: Any) -> tuple[dict[str, Any], int]:
        """导入暂存里的单章正文（勾选前预览用；仍是单章按需取）。"""
        try:
            no = self._chapter_no(chapter_no)
            staged = self.store.load_import(import_id)
            chapters = staged["chapters"]
            if no < 1 or no > len(chapters):
                return {"error": f"章节号超出范围：{no}（共 {len(chapters)} 章）"}, 404
            row = chapters[no - 1]
            body = str(row["body"])
            return {"i": no, "title": str(row["title"]), "len": len(body), "content": body}, 200
        except LookupError:
            return {"error": "导入暂存不存在"}, 404
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    def api_create_book(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        try:
            book = self.store.create_book(
                import_id=str(body.get("import_id") or ""),
                picked=body.get("picked"),
                title=str(body.get("title") or ""),
            )
            return {"book": book}, 201
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    def api_list_books(self) -> tuple[dict[str, Any], int]:
        return self.store.list_books(), 200

    def api_get_book(self, book_id: str) -> tuple[dict[str, Any], int]:
        try:
            return self.store.get_book(book_id), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    # ---- API：章节与稿件 ----

    def api_get_chapter(self, book_id: str, chapter_no: Any) -> tuple[dict[str, Any], int]:
        try:
            return self.store.get_chapter(book_id, self._chapter_no(chapter_no)), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    def api_get_manuscript(self, book_id: str, chapter_no: Any,
                           manuscript_id: str) -> tuple[dict[str, Any], int]:
        try:
            return self.store.get_manuscript(
                book_id, self._chapter_no(chapter_no), manuscript_id,
            ), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    def api_update_chapter(self, book_id: str, chapter_no: Any,
                           body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """改章节标题；带 `content` 时**新增一份人工改编稿**，不覆盖原文。"""
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        try:
            chapter = self.store.update_chapter(
                book_id, self._chapter_no(chapter_no),
                title=body.get("title"), content=body.get("content"),
                expected_revision=body.get("revision"),
                base_manuscript_id=body.get("base_manuscript_id"),
                task_id=str(body.get("task_id") or ""),
                user_request=str(body.get("user_request") or ""),
            )
            return {"chapter": chapter}, 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelConflictError as exc:
            return {"error": str(exc)}, 409
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    def api_save_manuscript(self, book_id: str, chapter_no: Any,
                            body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """保存一份改编稿（人工编辑的显式入口 / AI 结果保存）。"""
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        kind = str(body.get("kind") or manuscripts.KIND_MANUAL).strip()
        try:
            payload = self.store.save_manuscript(
                book_id, self._chapter_no(chapter_no),
                content=body.get("content"),
                kind=kind,
                source_manuscript_id=body.get("source_manuscript_id"),
                task_id=str(body.get("task_id") or ""),
                user_request=str(body.get("user_request") or ""),
                label=str(body.get("label") or ""),
                expected_revision=body.get("revision"),
            )
            status = 200 if payload.get("duplicate") else 201
            return payload, status
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelConflictError as exc:
            return {"error": str(exc)}, 409
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    def api_delete_manuscript(self, book_id: str, chapter_no: Any,
                              manuscript_id: str) -> tuple[dict[str, Any], int]:
        try:
            return self.store.delete_manuscript(
                book_id, self._chapter_no(chapter_no), manuscript_id,
            ), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    def api_delete_chapter(self, book_id: str, chapter_no: Any) -> tuple[dict[str, Any], int]:
        try:
            return self.store.delete_chapter(book_id, self._chapter_no(chapter_no)), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    def api_delete_book(self, book_id: str) -> tuple[dict[str, Any], int]:
        try:
            return self.store.delete_book(book_id), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    # ---- API：阶段生成（要素 / 大纲 / 剧本 / 单章改编）----

    def api_stage_preview(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """提交前预检：真实读取被选稿件、按滑条值算引用范围与 token 估算。

        与正式提交共用同一套组装函数（计划 §8.2 第 7 条），因此界面上显示的
        实际输入就是提交时会发送的内容。
        """
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        try:
            built = self._build_stage(body)
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except (NovelServiceError, prompts.PromptError) as exc:
            return {"error": str(exc)}, 400
        return {
            "stage": built["stage"],
            "budget": built["budget"],
            "warnings": built["warnings"],
            "chapters": built["chapters"],
            "sources": built["sources"],
            "limits": built["limits"],
        }, 200

    def _build_stage(self, body: dict[str, Any]) -> dict[str, Any]:
        """组装一次阶段生成的输入（预检与正式提交共用）。"""
        stage = str(body.get("stage") or "").strip()
        if stage not in prompts.ALL_STAGES:
            raise NovelServiceError(f"未知阶段：{stage or '（空）'}")
        book_id = str(body.get("book_id") or "").strip()
        context_window = self._context_window(str(body.get("model_key") or ""))
        extra_note = str(body.get("extra_note") or "")
        if stage == prompts.STORYBOARD_STAGE:
            # 分镜生图吃的是已保存剧本场次，不吃章节素材，也不参与逐章选稿。
            return self._build_storyboard(
                book_id=book_id, body=body, extra_note=extra_note,
                context_window=context_window,
            )
        raw_ids = body.get("chapter_ids")
        if stage != prompts.ADAPT_STAGE:
            if not isinstance(raw_ids, list) or not raw_ids:
                raise NovelServiceError("至少选择一章作为素材")
        wanted = self._manuscripts_by_chapter(body.get("manuscripts"))
        body_limit = prompts.clamp_body_limit(body.get("body_chars"))
        max_chapters = prompts.clamp_chapter_limit(body.get("max_chapters"))
        if stage == prompts.ADAPT_STAGE:
            return self._build_adapt(
                book_id=book_id, body=body, wanted=wanted, body_limit=body_limit,
                max_chapters=max_chapters, extra_note=extra_note,
                context_window=context_window,
            )
        material = self.store.stage_material(book_id, raw_ids, wanted)
        manifest = material["manifest"]
        elements = manifest.get("elements") if isinstance(manifest.get("elements"), dict) else {}
        element_versions = manifest.get("elements_versions") if isinstance(manifest.get("elements_versions"), list) else []
        outline_versions = manifest.get("outline_versions") if isinstance(manifest.get("outline_versions"), list) else []
        elements_version = self._selected_version(body, "elements_version", element_versions, elements.get("version", 0))
        outline_version = self._selected_version(body, "outline_version", outline_versions, manifest.get("outline_version", 0))
        selected_elements = next((row for row in element_versions if int(row.get("version") or 0) == elements_version), None)
        selected_outline = next((row for row in outline_versions if int(row.get("version") or 0) == outline_version), None)
        characters = list(elements.get("characters") or [])
        if selected_elements:
            characters = _split_elements(str(selected_elements.get("raw") or ""))["characters"]
        manuscript_ids = {int(row["i"]): str(row.get("manuscript_id") or "") for row in material["chapters"]}
        # 图谱反哺（4B-1）：已保存要素的实体进系统提示词，保持人物与设定一致。
        kg_buckets = self._kg_elements(manifest, selected_elements)
        kg = prompts.build_kg_context(kg_buckets)
        built = prompts.build_stage_content(
            stage=stage,
            book_title=str(manifest.get("title") or "未命名作品"),
            chapters=material["chapters"],
            chapter_ids=material["chapter_ids"],
            summaries=manifest.get("summaries") or {},
            extra_note=extra_note,
            outline=str(selected_outline.get("text") if selected_outline else manifest.get("outline") or ""),
            characters=characters,
            body_limit=body_limit,
            max_chapters=max_chapters,
            manuscript_ids=manuscript_ids,
        )
        built["system"] = kg
        budget = prompts.budget_report(
            content=built["content"], chapters=built["chapters"], warnings=built["warnings"],
            body_limit=body_limit, max_chapters=max_chapters, context_window=context_window,
            system=kg, kg_count=len(prompts.kg_entities(kg_buckets)),
        )
        prepared = {
            "stage": stage,
            "book_id": book_id,
            "manifest": manifest,
            "material": material,
            "built": built,
            "chapters": built["chapters"],
            "warnings": built["warnings"],
            "budget": budget,
            "limits": {"body_chars": body_limit, "max_chapters": max_chapters},
            "manuscript_ids": manuscript_ids,
            "elements_version": elements_version,
            "outline_version": outline_version,
            "sources": self._source_rows(manifest, material, elements_version, outline_version,
                                         selected_elements, selected_outline, stage),
        }
        prepared["sources"]["kg_entities"] = int(budget.get("kg_entities") or 0)
        return prepared

    def _build_adapt(self, *, book_id: str, body: dict[str, Any],
                     wanted: dict[int, str], body_limit: int, max_chapters: int,
                     extra_note: str, context_window: int) -> dict[str, Any]:
        """单章 AI 改编：固定一份稿件来源，不接管多章选择。

        正文超上限时走分块续写（4A）：按段落切块顺序生成，绝不静默截断。
        """
        raw_chapter = body.get("chapter_ids")
        chapter_no = 0
        if isinstance(raw_chapter, list) and len(raw_chapter) == 1:
            chapter_no = self._chapter_no(raw_chapter[0])
        elif isinstance(body.get("chapter_no"), (int, str)):
            chapter_no = self._chapter_no(body.get("chapter_no"))
        if not chapter_no:
            raise NovelServiceError("单章改编必须指定一章")
        material = self.store.stage_material(book_id, [chapter_no], wanted)
        manifest = material["manifest"]
        row = material["chapters"][0]
        content_text = str(row.get("content") or "")
        kg_buckets = self._kg_elements(manifest, None)
        kg = prompts.build_kg_context(kg_buckets)
        overhead_chars = 0
        if len(content_text) > body_limit:
            built = prompts.build_adapt_chunks(
                book_title=str(manifest.get("title") or "未命名作品"),
                chapter_no=chapter_no,
                chapter_title=str(row.get("title") or ""),
                manuscript_label=str(row.get("manuscript_label") or ""),
                content=content_text,
                user_request=extra_note,
                body_limit=body_limit,
            )
            overhead_chars = int(built.get("overhead_chars") or 0)
        else:
            built = prompts.build_adapt_content(
                book_title=str(manifest.get("title") or "未命名作品"),
                chapter_no=chapter_no,
                chapter_title=str(row.get("title") or ""),
                manuscript_label=str(row.get("manuscript_label") or ""),
                content=content_text,
                user_request=extra_note,
                body_limit=body_limit,
            )
        built["system"] = kg
        budget = prompts.budget_report(
            content=built["content"], chapters=built["chapters"], warnings=built["warnings"],
            body_limit=body_limit, max_chapters=max_chapters, context_window=context_window,
            system=kg, kg_count=len(prompts.kg_entities(kg_buckets)),
            overhead_chars=overhead_chars,
        )
        manuscript_ids = {chapter_no: str(row.get("manuscript_id") or "")}
        prepared = {
            "stage": prompts.ADAPT_STAGE,
            "book_id": book_id,
            "manifest": manifest,
            "material": material,
            "built": built,
            "chapters": built["chapters"],
            "warnings": built["warnings"],
            "budget": budget,
            "limits": {"body_chars": body_limit, "max_chapters": max_chapters},
            "manuscript_ids": manuscript_ids,
            "elements_version": 0,
            "outline_version": 0,
            "sources": self._source_rows(manifest, material, 0, 0, None, None,
                                         prompts.ADAPT_STAGE),
        }
        prepared["sources"]["kg_entities"] = int(budget.get("kg_entities") or 0)
        return prepared

    def _build_storyboard(self, *, book_id: str, body: dict[str, Any],
                          extra_note: str, context_window: int) -> dict[str, Any]:
        """分镜生图（4C）：把已保存剧本的最新场次逐场转成出图提示词。

        吃的是剧本场次而不是章节素材；不写回作品——生成结果直接发给造梦机出图。
        `system_prompt` 是用户自定义写法（提词库模板或手动编辑），空 = 内置分镜写法；
        图谱反哺（KG）作为参考段拼在写法之后，同走系统消息。
        """
        # 公开清单视图就够用：场次、书名与要素都在里面，不必翻 store 私有层。
        manifest = self.store.get_book(book_id)["book"]
        scenes = manifest.get("script") if isinstance(manifest.get("script"), list) else []
        if not scenes:
            raise NovelServiceError("这个作品还没有剧本场次；请先在剧本阶段生成并保存场次，再来生成分镜")
        raw_ids = body.get("scene_ids")
        if raw_ids is None or raw_ids == "":
            scene_ids: list[int] = []
        else:
            if not isinstance(raw_ids, list):
                raise NovelServiceError("scene_ids 必须是场次号数组")
            scene_ids = [self._scene_no(raw) for raw in raw_ids]
        built = prompts.build_storyboard_content(
            book_title=str(manifest.get("title") or "未命名作品"),
            scenes=scenes, scene_ids=scene_ids, extra_note=extra_note,
            system_prompt=str(body.get("system_prompt") or ""),
        )
        kg_buckets = self._kg_elements(manifest, None)
        kg = prompts.build_kg_context(kg_buckets)
        system_text = str(built.get("system") or "")
        if kg:
            built["system"] = f"{system_text}\n\n{kg}" if system_text else kg
            system_text = built["system"]
        budget = prompts.budget_report(
            content=built["content"], chapters=[], warnings=built["warnings"],
            body_limit=prompts.BODY_SLOT_CHARS, max_chapters=1,
            context_window=context_window,
            system=system_text, kg_count=len(prompts.kg_entities(kg_buckets)),
        )
        wanted = set(built["scene_ids"])
        snapshot = [
            {"i": int(row.get("i") or 0), "scene": str(row.get("scene") or ""),
             "len": len(str(row.get("content") or ""))}
            for row in scenes if int(row.get("i") or 0) in wanted
        ]
        sources = {
            "chapters": [],
            "scene_ids": built["scene_ids"],
            "scene_count": len(built["scene_ids"]),
            "uses_outline": False,
            "uses_characters": False,
            "has_summaries": False,
            "elements_version": 0,
            "elements_label": "",
            "outline_version": 0,
            "outline_label": "",
            "system_custom": bool(built.get("system_custom")),
        }
        sources["kg_entities"] = int(budget.get("kg_entities") or 0)
        return {
            "stage": prompts.STORYBOARD_STAGE,
            "book_id": book_id,
            "manifest": manifest,
            "material": {"chapter_ids": [], "chapters": []},
            "built": built,
            "chapters": [],
            "warnings": built["warnings"],
            "budget": budget,
            "limits": {"body_chars": prompts.BODY_SLOT_CHARS, "max_chapters": 1},
            "manuscript_ids": {},
            "elements_version": 0,
            "outline_version": 0,
            "sources": sources,
            "scene_snapshot": snapshot,
        }

    @staticmethod
    def _kg_elements(manifest: dict[str, Any],
                     selected_elements: dict[str, Any] | None) -> dict[str, list[str]]:
        """图谱反哺的实体来源：优先用本次选定的要素版本，否则用当前要素。"""
        raw = ""
        if selected_elements is not None:
            raw = str(selected_elements.get("raw") or "")
        else:
            elements = manifest.get("elements") if isinstance(manifest.get("elements"), dict) else {}
            raw = str(elements.get("raw") or "")
        if not raw.strip():
            return {}
        return _split_elements(raw)

    @staticmethod
    def _source_rows(manifest: dict[str, Any], material: dict[str, Any],
                     elements_version: int, outline_version: int,
                     selected_elements: dict[str, Any] | None,
                     selected_outline: dict[str, Any] | None,
                     stage: str) -> dict[str, Any]:
        """本次实际引用了什么（右栏与任务快照共用）。"""
        rows = []
        for row in material["chapters"]:
            rows.append({
                "i": int(row["i"]),
                "title": str(row.get("title") or ""),
                "manuscript_id": str(row.get("manuscript_id") or ""),
                "manuscript_label": str(row.get("manuscript_label") or ""),
                "manuscript_kind": str(row.get("manuscript_kind") or ""),
            })
        elements = manifest.get("elements") if isinstance(manifest.get("elements"), dict) else {}
        outline_text = str(selected_outline.get("text") if selected_outline else manifest.get("outline") or "")
        character_text = str(selected_elements.get("raw") if selected_elements else elements.get("raw") or "")
        return {
            "chapters": rows,
            "elements_version": int(elements_version or 0),
            "elements_label": _version_label(selected_elements) if selected_elements else "",
            "outline_version": int(outline_version or 0),
            "outline_label": _version_label(selected_outline) if selected_outline else "",
            "uses_outline": stage == "script" and bool(outline_text.strip()),
            "uses_characters": stage == "script" and bool(character_text.strip()),
            "has_summaries": bool(manifest.get("summaries")),
        }

    def _context_window(self, model_key: str) -> int:
        """模型上下文容量（未知返回 0，界面如实标「无法确认」）。"""
        key = str(model_key or "").strip()
        if not key:
            return 0
        try:
            profiles = self.app.config.model_profiles()
        except Exception:  # noqa: BLE001 - 配置读不到按未知处理，不能因此挡住预检
            return 0
        row = next((item for item in profiles if str(item.get("model_key") or "") == key), None)
        if not row:
            return 0
        try:
            return max(0, int(row.get("context_window") or 0))
        except (TypeError, ValueError):
            return 0

    def api_stage(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """提交一次阶段生成：组装 content → 交给 Studio 任务队列。

        任务字段照总计划 §5.3 的契约：`module="novel"`、`origin_label="创作场"`（必填，
        否则历史来源显示不出来）、`novel={...}`（retry 原样带回）。

        长文策略全在 `prompts.build_stage_content`：分章节 + 分槽上限 + 摘要链；
        超限说明原样回给前端展示，不静默截断。
        """
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        model_name = str(body.get("model_name") or "").strip()
        if not model_name:
            return {"error": "请选择模型；可用模型来自设置页的模型检查结果"}, 400
        try:
            prepared = self._build_stage(body)
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except (NovelServiceError, prompts.PromptError) as exc:
            return {"error": str(exc)}, 400
        stage = prepared["stage"]
        book_id = prepared["book_id"]
        material = prepared["material"]
        built = prepared["built"]
        manifest = prepared["manifest"]
        budget = prepared["budget"]
        # 已知容量不足时在调用模型前就拦住：不排队、不计费（计划 §8.2 第 4 条）。
        if budget["fits"] is False:
            return {
                "error": "本次素材超出模型可用上下文，未提交：" + "；".join(budget["suggestions"]),
                "budget": budget,
                "warnings": budget["warnings"],
            }, 400
        if not budget["context_known"] and budget["estimated_tokens"] > LARGE_INPUT_CONFIRM_TOKENS:
            # 容量未知且输入很大：要求前端确认后再提交（不伪造模型上限）。
            if str(body.get("confirm_large") or "").lower() not in ("1", "true", "yes"):
                return {
                    "error": "无法确认这个模型能否容纳本次素材，请确认后再提交",
                    "needs_confirm": True,
                    "budget": budget,
                }, 409
        title = prompts.task_title(str(manifest.get("title") or "未命名作品"), stage)
        novel_payload = {
            "book_id": book_id,
            "stage": stage,
            "chapter_ids": material["chapter_ids"],
            "book_revision": int(manifest.get("revision") or 0),
            "elements_version": prepared["elements_version"],
            "outline_version": prepared["outline_version"],
            # 逐章稿件选择：任务重试/写回都按它，不因当前页面选择变化而换素材。
            "manuscripts": prepared["manuscript_ids"],
            "body_chars": prepared["limits"]["body_chars"],
            "max_chapters": prepared["limits"]["max_chapters"],
            "chapter_snapshot": prepared["chapters"],
            "sources": prepared["sources"],
        }
        if stage == prompts.STORYBOARD_STAGE:
            # 分镜生图：场次选择与快照进任务，重试按同一批场次重放。
            novel_payload["scene_ids"] = list(prepared["built"].get("scene_ids") or [])
            novel_payload["scene_snapshot"] = prepared.get("scene_snapshot") or []
        input_data = {
            "content": built["content"],
            "model_key": str(body.get("model_key") or ""),
            "model_name": model_name,
            "title": title,
            "module": "novel",
            "origin_label": "创作场",
            "user_request": str(body.get("extra_note") or ""),
            "novel": novel_payload,
        }
        system_text = str(built.get("system") or "")
        if system_text.strip():
            # 图谱反哺：系统提示词随任务落盘，重试原样重放（与素材快照同口径）。
            input_data["system"] = system_text
        chunked = built.get("chunked")
        if isinstance(chunked, dict) and chunked.get("blocks"):
            # 分块续写（4A）：多轮契约进任务；content 保留静态拼合作快照。
            input_data["chunked"] = chunked
        try:
            task = self.app.studio.jobs.submit(input_data)
        except ValueError as exc:
            return {"error": str(exc)}, 400
        except LookupError as exc:
            return {"error": str(exc)}, 404
        # 摘要链原料：每章首尾片段（零额外推理），按「章节 + 稿件」记。
        summaries = {
            prompts.summary_key(row["i"], str(row.get("manuscript_id") or "")):
                prompts.summarize_chapter(str(row["content"]))
            for row in material["chapters"]
        }
        try:
            self.store.update_summaries(book_id, summaries)
        except (LookupError, NovelServiceError):
            # 摘要只是辅助原料，写不进去不该让已经排上队的任务失败。
            pass
        # 摘要写入会递增作品 revision；把写入后的权威版本回给前端，
        # 否则用户完成任务后会拿生成前版本写回，触发由本次生成造成的假冲突。
        try:
            current = self.store.get_book(book_id)["book"]
            book_revision = int(current.get("revision") or 0)
        except (LookupError, NovelServiceError):
            book_revision = int(manifest.get("revision") or 0)
        return {
            "task": task,
            "warnings": built["warnings"],
            "budget": budget,
            "sources": prepared["sources"],
            "book_revision": book_revision,
        }, 201

    @staticmethod
    def _manuscripts_by_chapter(raw: Any) -> dict[int, str]:
        """把请求里的 `manuscripts` 收敛成 `{章号: 稿件ID}`（兼容 list/dict 两种写法）。"""
        result: dict[int, str] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                try:
                    result[int(key)] = str(value or "")
                except (TypeError, ValueError):
                    raise NovelServiceError(f"稿件选择的章号非法：{key}") from None
        elif isinstance(raw, list):
            for row in raw:
                if not isinstance(row, dict):
                    raise NovelServiceError("稿件选择必须是对象数组")
                chapter_no = row.get("chapter_no", row.get("i"))
                if isinstance(chapter_no, bool):
                    raise NovelServiceError("稿件选择的章号必须是整数")
                try:
                    result[int(chapter_no)] = str(row.get("manuscript_id") or "")
                except (TypeError, ValueError):
                    raise NovelServiceError("稿件选择的章号必须是整数") from None
        elif raw not in (None, ""):
            raise NovelServiceError("稿件选择必须是对象或数组")
        return result

    @staticmethod
    def _selected_version(body: dict[str, Any], key: str, versions: list[Any], current: Any) -> int:
        raw = body.get(key)
        if raw is None or raw == "":
            return int(versions[-1].get("version", 0) if versions else current or 0)
        if isinstance(raw, bool):
            raise NovelServiceError(f"{key} 必须是正整数")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise NovelServiceError(f"{key} 必须是正整数") from None
        known = any(isinstance(row, dict) and int(row.get("version") or 0) == value for row in versions)
        if value < 1 or (not known and value != int(current or 0)):
            raise NovelServiceError(f"{key} 指定的版本不存在")
        return value

    def api_apply(self, book_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """把任务结果写回作品（要素 / 大纲 / 剧本 / 单章改编）。

        写回前核对任务 id 与阶段：任务里的 `novel.book_id` / `novel.stage` 必须与请求
        一致，`revision` 对不上就 409——这是「A 未完又生成 B」互相覆盖的防线。
        同一个任务只能保存一次（改编稿按任务 ID 去重）。
        """
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        stage = str(body.get("stage") or "").strip()
        task_id = str(body.get("task_id") or "").strip()
        text = body.get("text")
        if stage not in prompts.ALL_STAGES:
            return {"error": f"未知阶段：{stage or '（空）'}"}, 400
        if stage == prompts.STORYBOARD_STAGE:
            # 分镜提示词不进作品：结果在任务里，直接发送到造梦机出图。
            return {"error": "分镜提示词不写回作品；请在结果里勾选后发送到造梦机"}, 400
        if not task_id:
            return {"error": "缺少 task_id：写回必须指明是哪个任务的结果"}, 400
        if not isinstance(text, str):
            return {"error": "text 必须是文本"}, 400
        try:
            task = self.app.studio.jobs.task(task_id)
        except Exception:  # noqa: BLE001 - 任务存储异常按「查不到」处理
            task = None
        if not task:
            return {"error": "任务不存在"}, 404
        input_data = task.get("input") if isinstance(task.get("input"), dict) else {}
        novel_input = input_data.get("novel") if isinstance(input_data.get("novel"), dict) else {}
        if str(novel_input.get("book_id") or "") != book_id:
            return {"error": "这个任务不属于当前作品，不能写回"}, 409
        if str(novel_input.get("stage") or "") != stage:
            return {"error": f"任务阶段不符：任务是「{novel_input.get('stage')}」，请求是「{stage}」"}, 409
        if str(task.get("status") or "") != "done":
            return {"error": "任务还没完成，不能写回结果"}, 409
        chapter_ids = novel_input.get("chapter_ids") or []
        manuscripts_map = novel_input.get("manuscripts") if isinstance(novel_input.get("manuscripts"), dict) else {}
        by_chapter = {int(key): str(value) for key, value in manuscripts_map.items() if str(key).isdigit()}
        try:
            if stage == prompts.ADAPT_STAGE:
                chapter_no = int(chapter_ids[0]) if chapter_ids else 0
                payload = self.store.save_manuscript(
                    book_id, chapter_no, content=text, kind=manuscripts.KIND_AI,
                    source_manuscript_id=by_chapter.get(chapter_no) or "",
                    task_id=task_id,
                    user_request=str(input_data.get("user_request") or ""),
                    expected_revision=body.get("revision"),
                )
                return {"book": self.store.get_book(book_id)["book"], **payload}, 200
            if stage == "elements":
                book = self.store.apply_elements(
                    book_id, task_id=task_id, raw=text,
                    expected_revision=body.get("revision"),
                    chapter_ids=chapter_ids, manuscripts_by_chapter=by_chapter,
                )
            elif stage == "script":
                from_chapter = int(chapter_ids[0]) if chapter_ids else 0
                book = self.store.apply_script(
                    book_id, task_id=task_id, text=text,
                    from_chapter=from_chapter, chapter_ids=chapter_ids,
                    expected_revision=body.get("revision"),
                    manuscripts_by_chapter=by_chapter,
                )
            else:
                book = self.store.apply_outline(
                    book_id, task_id=task_id, text=text,
                    expected_revision=body.get("revision"),
                    chapter_ids=chapter_ids, manuscripts_by_chapter=by_chapter,
                )
            return {"book": book}, 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelConflictError as exc:
            return {"error": str(exc)}, 409
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    # ---- API：记录管理与批量删除 ----

    def api_records(self, book_id: str) -> tuple[dict[str, Any], int]:
        try:
            return self.store.list_records(book_id), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    def api_delete_records(self, book_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        try:
            return self.store.delete_records(
                book_id, body.get("items"), expected_revision=body.get("revision"),
            ), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelConflictError as exc:
            return {"error": str(exc)}, 409
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    def api_delete_tasks(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """删除创作场的生成记录（只删任务记录，不删已保存的内容）。"""
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        raw = body.get("task_ids")
        if not isinstance(raw, list) or not raw:
            return {"error": "请选择要删除的生成记录"}, 400
        book_id = str(body.get("book_id") or "").strip()
        ids: list[str] = []
        for entry in raw:
            task_id = str(entry or "").strip()
            if not task_id:
                continue
            task = self._task(task_id)
            if not task:
                return {"error": f"任务不存在：{task_id}"}, 404
            input_data = task.get("input") if isinstance(task.get("input"), dict) else {}
            info = input_data.get("novel") if isinstance(input_data.get("novel"), dict) else {}
            if book_id and str(info.get("book_id") or "") != book_id:
                return {"error": "这些任务不属于当前作品，不能删除"}, 409
            if str(task.get("status") or "") in {"queued", "running"}:
                return {"error": "有任务还在执行，请先取消再删除"}, 409
            ids.append(task_id)
        try:
            return self.app.studio.jobs.delete(ids), 200
        except Exception as exc:  # noqa: BLE001 - 删除失败如实回报
            return {"error": f"删除生成记录失败：{exc}"}, 500

    def _task(self, task_id: str) -> dict[str, Any] | None:
        try:
            return self.app.studio.jobs.task(task_id)
        except Exception:  # noqa: BLE001 - 任务存储异常按「查不到」处理
            return None

    # ---- API：阶段三剧本场次 ----

    def api_list_script(self, book_id: str) -> tuple[dict[str, Any], int]:
        try:
            return self.store.list_script(book_id), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    def api_update_scene(self, book_id: str, scene_no: Any,
                         body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        try:
            return self.store.update_scene(
                book_id, self._scene_no(scene_no),
                scene=body.get("scene"), content=body.get("content"),
                expected_revision=body.get("revision"),
            ), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelConflictError as exc:
            return {"error": str(exc)}, 409
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    def api_delete_scene(self, book_id: str, scene_no: Any) -> tuple[dict[str, Any], int]:
        try:
            return self.store.delete_scene(book_id, self._scene_no(scene_no)), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    def api_reorder_script(self, book_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        try:
            return self.store.reorder_script(book_id, body.get("order")), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    # ---- API：导出与路径 ----

    def api_export_book(self, book_id: str, fmt: str = "txt",
                        with_extras: Any = "",
                        manuscripts_raw: Any = None) -> tuple[dict[str, Any], int]:
        """全书导出。`with_extras` 为真时附要素与大纲；可指定逐章导出的稿件。"""
        if fmt not in ("txt", "md"):
            return {"error": "导出格式只支持 txt 或 md"}, 400
        extras = str(with_extras).strip().lower() in ("1", "true", "yes", "on")
        try:
            wanted = self._manuscripts_by_chapter(manuscripts_raw)
            text = self.store.export_book(
                book_id, fmt=fmt, with_extras=extras, manuscript_ids=wanted,
            )
            book = self.store.get_book(book_id)["book"]
            base = _safe_name(str(book.get("title") or "作品"))
            return {
                "format": fmt,
                "text": text,
                "filename": f"{base}.{fmt}",
                "book": book,
                "location": self.data_location(),
            }, 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    def api_location(self) -> tuple[dict[str, Any], int]:
        """数据目录与导出说明（复制路径、打开文件夹、导出提示共用）。"""
        payload = self.data_location()
        payload["can_open_folder"] = os.name == "nt"
        payload["export_note"] = "导出由浏览器发起下载，通常保存到浏览器默认下载目录。"
        return payload, 200

    def api_reveal(self) -> tuple[dict[str, Any], int]:
        """在运行本应用的电脑上打开作品保存目录（路径由服务端决定）。"""
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

    # ---- 参数校验 ----

    @staticmethod
    def _max_len(raw: Any) -> int:
        """切块上限：默认 6000，范围与工具层一致（200–200000）。"""
        if raw is None or raw == "":
            return novel_split.DEFAULT_MAX_LEN
        if isinstance(raw, bool):
            raise NovelServiceError("max_len 必须是整数")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise NovelServiceError("max_len 必须是整数") from None
        if value < 200 or value > 200000:
            raise NovelServiceError("max_len 需要在 200–200000 之间")
        return value

    @staticmethod
    def _chapter_no(raw: Any) -> int:
        if isinstance(raw, bool):
            raise NovelServiceError("章节号必须是整数")
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            raise NovelServiceError("章节号必须是整数") from None
        if value < 1 or value > 9999:
            raise NovelServiceError("章节号必须在 1–9999 之间")
        return value

    @staticmethod
    def _scene_no(raw: Any) -> int:
        if isinstance(raw, bool):
            raise NovelServiceError("场次号必须是整数")
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            raise NovelServiceError("场次号必须是整数") from None
        if value < 1:
            raise NovelServiceError("场次号必须大于 0")
        return value


def _version_label(row: dict[str, Any] | None) -> str:
    """版本显示名：`第 N 版 · 时间 · 来源章节`（右栏引用详情用）。"""
    if not isinstance(row, dict):
        return ""
    version = int(row.get("version") or 0)
    stamp = int(row.get("updated_at") or 0)
    chapters = [int(n) for n in (row.get("chapter_ids") or [])]
    bits = [f"第 {version} 版"]
    if stamp:
        date = time.localtime(stamp / 1000)
        bits.append(time.strftime("%Y-%m-%d %H:%M", date))
    bits.append(f"第 {('、'.join(map(str, chapters)))} 章" if chapters else "来源未记录")
    return " · ".join(bits)
