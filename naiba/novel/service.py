# -*- coding: utf-8 -*-
"""小说域服务：作品与章节持久化 + `/api/novel/*` 的业务门面。

阶段一（文件处理，零 AI）：导入、拆章、勾选、保存、编辑、导出。
阶段二（AI 生成）：要素 / 改编大纲——组装输入交给 Studio 任务队列，结果写回作品。
阶段三（AI 生成）：剧本场次（见 `script.py`）。

设计要点（《小说改编工作台总计划》§5.2 + 《接入工作台实施计划》§五）：

1. **单章一文件**：改一章不重写整本，坏文件不拖累其它章。
2. **book.json 是清单的权威来源**：`chapter_ids` 决定哪些章节属于作品。保存章节时
   先原子写章节文件、再原子更新 book.json；中间崩溃留下的孤儿章节忽略并可清理。
3. **原子写**：同目录临时文件 + `os.replace`，照 `naiba/studio/custom.py` 的范式。
4. **导入暂存**：导入先落 `_imports/<import-id>/`，勾选入库时才提交为正式作品目录。
   失败的暂存不出现在作品列表里，可由 `purge_imports` 清理。
5. **两段式**：导入与详情只回索引；正文只有单章接口给。
6. **体积闸门**：单文件沿用 60 MiB；作品目录总量上限 256 MiB，超了整项拒绝，
   不自动截断、不删历史章节。
7. **AI 结果写回**（阶段二起）：`revision` 乐观并发 + 任务 id 核对，避免 A 未完又
   生成 B 时互相覆盖。
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

from naiba.core import novel_split
from naiba.novel import prompts
from naiba.tools.providers.novel import NovelError, read_text_auto

# 版本历史是 schema 1 的可选追加字段；读取旧清单时保留现有成果，缺失的历史来源不推断。
SCHEMA_VERSION = 1
# 作品 ID：时间戳 + 随机后缀，禁止用户文件名穿越目录（与 custom.py 同构）。
BOOK_ID_PATTERN = re.compile(r"^book-[0-9]{8}-[0-9a-f]{12}$")
IMPORT_ID_PATTERN = re.compile(r"^import-[0-9]{8}-[0-9a-f]{12}$")
# 章节号：4 位十进制文件名，同时限制单本最多 9999 章。
CHAPTER_NO_PATTERN = re.compile(r"^[0-9]{1,4}$")
CHAPTER_FILE_PATTERN = re.compile(r"^([0-9]{4})\.json$")

TITLE_MAX = 200
# 单文件上限沿用算法层常量（60 MiB），超了如实拒绝。
MAX_FILE_BYTES = novel_split.MAX_FILE_BYTES
# 作品目录总量上限：导入、编辑、后续 AI 写回共用这一条。
MAX_BOOK_BYTES = 256 * 1024 * 1024
# 导入暂存的保留时长：勾选入库前一直有效，但避免失败导入长期占盘。
IMPORT_TTL_SECONDS = 24 * 3600

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


class NovelStore:
    """作品与章节的落盘（线程锁串行化写路径；读为每章独立小文件，坏章不拖累他人）。"""

    def __init__(self, data_dir: Path):
        self._root = Path(data_dir) / "studio" / "novel"
        self._imports = self._root / "_imports"
        self._lock = threading.Lock()

    # ---- 路径与安全 ----

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
        """章节文件：4 位编号，校验后仍在 chapters/ 内。"""
        if isinstance(chapter_no, bool) or not isinstance(chapter_no, int):
            raise NovelServiceError("章节号必须是整数")
        if chapter_no < 1 or chapter_no > 9999:
            raise NovelServiceError("章节号必须在 1–9999 之间")
        chapters_dir = (book_dir / "chapters").resolve()
        path = (chapters_dir / f"{chapter_no:04d}.json").resolve()
        if not path.is_relative_to(chapters_dir):
            raise NovelServiceError("章节路径越界")
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
                f"（当前 {used / 1024 / 1024:.1f} MB）。请删除多余章节后再试。"
            )

    # ---- 清单 ----

    def _load_manifest(self, book_id: str) -> dict[str, Any]:
        book_dir = self._book_dir(book_id)
        manifest = self._read_json(book_dir / "book.json", what="作品清单")
        version = manifest.get("schema_version")
        if version != SCHEMA_VERSION:
            raise NovelServiceError(f"作品数据版本不支持：{version}（当前支持 {SCHEMA_VERSION}）")
        if str(manifest.get("id") or "") != book_id:
            raise NovelServiceError("作品清单与目录不一致")
        if not isinstance(manifest.get("chapter_ids"), list):
            raise NovelServiceError("作品清单缺少章节列表")
        return manifest

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
        return {
            "id": str(manifest.get("id") or ""),
            "title": str(manifest.get("title") or ""),
            "revision": int(manifest.get("revision") or 0),
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

    # ---- 章节 ----

    def _load_chapter(self, book_dir: Path, chapter_no: int) -> dict[str, Any]:
        path = self._chapter_path(book_dir, chapter_no)
        payload = self._read_json(path, what=f"第 {chapter_no} 章")
        if int(payload.get("i") or 0) != chapter_no:
            raise NovelServiceError(f"第 {chapter_no} 章文件编号不一致")
        content = payload.get("content")
        if not isinstance(content, str):
            raise NovelServiceError(f"第 {chapter_no} 章缺少正文")
        return payload

    def _chapter_index(self, book_dir: Path, manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[int]]:
        """章节索引（**不含正文**）。已登记但缺失/损坏的章节进 `broken`，不静默补空。"""
        rows: list[dict[str, Any]] = []
        broken: list[int] = []
        for chapter_no in (int(n) for n in manifest.get("chapter_ids") or []):
            try:
                chapter = self._load_chapter(book_dir, chapter_no)
            except (LookupError, NovelServiceError):
                broken.append(chapter_no)
                continue
            rows.append({
                "i": chapter_no,
                "title": str(chapter.get("title") or ""),
                "len": int(chapter.get("len") or len(str(chapter.get("content") or ""))),
            })
        return rows, broken

    # ---- 导入暂存 ----

    def stage_import(self, *, filename: str, data: bytes, max_len: int) -> dict[str, Any]:
        """把上传的字节落到导入暂存并拆章，返回**索引**（不含正文）。

        源文件副本必须留着：勾选入库时要按同一份字节、同一参数重拆一次，
        这样「脚本拆的结果 = 工作台拆的结果」有确定依据。
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
            for chapter_no in picked_nos:
                row = chapters[chapter_no - 1]
                body = str(row["body"])
                payload = {
                    "schema_version": SCHEMA_VERSION,
                    "revision": 1,
                    "i": chapter_no,
                    "title": str(row["title"]),
                    "len": len(body),
                    "content": body,
                }
                self._write_json(staging_dir / "chapters" / f"{chapter_no:04d}.json", payload)
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
        """作品列表：只读清单，不读章节正文。损坏的作品不算记录、只报 id。"""
        corrupt: list[str] = []
        rows: list[dict[str, Any]] = []
        if self._root.exists():
            for path in sorted(self._root.iterdir()):
                if not path.is_dir() or not BOOK_ID_PATTERN.match(path.name):
                    continue
                try:
                    manifest = self._load_manifest(path.name)
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
        }

    def get_chapter(self, book_id: str, chapter_no: int) -> dict[str, Any]:
        """单章正文（两段式的第二段：正文只按需取单章）。"""
        manifest = self._load_manifest(book_id)
        if chapter_no not in {int(n) for n in manifest.get("chapter_ids") or []}:
            raise LookupError("章节不存在")
        chapter = self._load_chapter(self._book_dir(book_id), chapter_no)
        return {
            "i": int(chapter.get("i") or chapter_no),
            "title": str(chapter.get("title") or ""),
            "len": int(chapter.get("len") or 0),
            "content": str(chapter.get("content") or ""),
            # 乐观并发：前端改完写回时带上它，对不上就说明别处已经改过。
            "revision": int(chapter.get("revision") or 0),
        }

    def update_chapter(self, book_id: str, chapter_no: int,
                       *, title: Any = None, content: Any = None,
                       expected_revision: Any = None) -> dict[str, Any]:
        """改章节：先原子写章节文件，再原子更新 book.json（revision 单调递增）。

        `expected_revision` 给了就做乐观并发校验：前端读到的是第 N 版，写回时必须
        仍是第 N 版，否则说明别处已经改过（例如 AI 结果写回），拒绝并让调用方重读。
        """
        manifest = self._load_manifest(book_id)
        if chapter_no not in {int(n) for n in manifest.get("chapter_ids") or []}:
            raise LookupError("章节不存在")
        book_dir = self._book_dir(book_id)
        current = self._load_chapter(book_dir, chapter_no)
        if expected_revision is not None:
            if isinstance(expected_revision, bool):
                raise NovelServiceError("revision 必须是整数")
            try:
                expected = int(expected_revision)
            except (TypeError, ValueError):
                raise NovelServiceError("revision 必须是整数") from None
            actual = int(current.get("revision") or 0)
            if expected != actual:
                raise NovelConflictError(
                    f"这一章已被修改过（你看到的是第 {expected} 版，现在是第 {actual} 版），请重新读取后再改"
                )
        next_title = str(current.get("title") or "")
        next_content = str(current.get("content") or "")
        if title is not None:
            next_title = str(title).strip()
            if not next_title:
                raise NovelServiceError("章节标题不能为空")
            if len(next_title) > TITLE_MAX:
                raise NovelServiceError(f"章节标题不能超过 {TITLE_MAX} 字符")
        if content is not None:
            if not isinstance(content, str):
                raise NovelServiceError("章节正文必须是文本")
            if not content.strip():
                raise NovelServiceError("章节正文不能为空")
            next_content = content
        delta = len(next_content.encode("utf-8")) - len(str(current.get("content") or "").encode("utf-8"))
        if delta > 0:
            self._check_book_capacity(book_dir, extra_bytes=delta)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "revision": int(current.get("revision") or 0) + 1,
            "i": chapter_no,
            "title": next_title,
            "len": len(next_content),
            "content": next_content,
        }
        with self._lock:
            self._write_json(self._chapter_path(book_dir, chapter_no), payload)
            self._bump_manifest(book_dir, manifest)
        return {
            "i": chapter_no,
            "title": next_title,
            "len": len(next_content),
            "content": next_content,
            "revision": payload["revision"],
        }

    def delete_chapter(self, book_id: str, chapter_no: int) -> dict[str, Any]:
        """删章节：先删文件再更新清单；中断留下的孤儿章节下次保存时忽略。"""
        manifest = self._load_manifest(book_id)
        ids = [int(n) for n in manifest.get("chapter_ids") or []]
        if chapter_no not in ids:
            raise LookupError("章节不存在")
        if len(ids) <= 1:
            raise NovelServiceError("作品至少要保留一章；要整本删除请删作品")
        book_dir = self._book_dir(book_id)
        with self._lock:
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

    def stage_material(self, book_id: str, chapter_ids: list[int]) -> dict[str, Any]:
        """取一次生成要用的素材：清单 + 选中章节正文 + 已有摘要链。

        正文**只在服务端组装 prompt 时读**，绝不整本回传给前端（两段式）。
        """
        manifest = self._load_manifest(book_id)
        book_dir = self._book_dir(book_id)
        registered = {int(n) for n in manifest.get("chapter_ids") or []}
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
            chapter = self._load_chapter(book_dir, number)
            chapters.append({
                "i": number,
                "title": str(chapter.get("title") or ""),
                "content": str(chapter.get("content") or ""),
            })
        return {"manifest": manifest, "chapters": chapters, "chapter_ids": picked}

    def apply_elements(self, book_id: str, *, task_id: str, raw: str,
                       expected_revision: Any = None, chapter_ids: list[int] | None = None) -> dict[str, Any]:
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
                "version": 1, "raw": str(previous.get("raw") or ""),
                "updated_at": int(previous.get("updated_at") or 0),
                "task_id": str(previous.get("task_id") or ""),
            }]
        version = len(versions) + 1
        entry = {"version": version, "raw": text, "updated_at": _now_ms(), "task_id": str(task_id or "")}
        entry["chapter_ids"] = list(chapter_ids or [])
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
        }
        with self._lock:
            self._bump_manifest(book_dir, manifest)
        return self._public_manifest(manifest)

    def apply_outline(self, book_id: str, *, task_id: str, text: str,
                      expected_revision: Any = None, chapter_ids: list[int] | None = None) -> dict[str, Any]:
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
                "version": 1, "text": str(manifest.get("outline") or ""),
                "updated_at": int(manifest.get("outline_updated_at") or 0),
                "task_id": str(manifest.get("outline_task_id") or ""),
            }]
        version = len(versions) + 1
        versions.append({"version": version, "text": body, "updated_at": _now_ms(), "task_id": str(task_id or ""), "chapter_ids": list(chapter_ids or [])})
        manifest["outline_versions"] = versions
        manifest["outline"] = body
        manifest["outline_version"] = version
        manifest["outline_task_id"] = str(task_id or "")
        manifest["outline_updated_at"] = _now_ms()
        with self._lock:
            self._bump_manifest(book_dir, manifest)
        return self._public_manifest(manifest)

    def apply_script(self, book_id: str, *, task_id: str, text: str,
                     from_chapter: int = 0, chapter_ids: list[int] | None = None,
                     expected_revision: Any = None) -> dict[str, Any]:
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
            versions = [{"version": 1, "script": existing, "updated_at": previous_updated_at,
                        "task_id": previous_task_id, "chapter_ids": []}]
        version = len(versions) + 1
        versions.append({"version": version, "script": manifest["script"], "updated_at": _now_ms(),
                         "task_id": str(task_id or ""), "chapter_ids": source_chapters})
        manifest["script_versions"] = versions
        manifest["script_version"] = version
        with self._lock:
            self._bump_manifest(book_dir, manifest)
        result = self._public_manifest(manifest)
        result["script_parsed"] = bool(parsed)
        return result

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
        """合并每章摘要（滚动摘要链的原料，零额外推理：首尾片段）。"""
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

    # ---- 导出 ----

    def export_book(self, book_id: str, *, fmt: str = "txt", with_extras: bool = False) -> str:
        """整本导出：按清单顺序拼正文。

        `with_extras=True` 时在正文后附要素与大纲（阶段三的全书导出）。
        标题只写一次：`book.json` 的清单是权威顺序，不重复也不漏章。
        """
        manifest = self._load_manifest(book_id)
        book_dir = self._book_dir(book_id)
        title = str(manifest.get("title") or "未命名作品")
        parts: list[str] = []
        if fmt == "md":
            parts.append(f"# {title}\n")
        for chapter_no in (int(n) for n in manifest.get("chapter_ids") or []):
            try:
                chapter = self._load_chapter(book_dir, chapter_no)
            except (LookupError, NovelServiceError):
                continue
            chapter_title = str(chapter.get("title") or f"第 {chapter_no} 章")
            body = str(chapter.get("content") or "")
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

    # ---- API ----

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

    # ---- 阶段二：要素 / 大纲（走 Studio 任务队列） ----

    def api_stage(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """提交一次阶段生成：组装 content → 交给 Studio 任务队列。

        任务字段照总计划 §5.3 的契约：`module="novel"`、`origin_label="创作场"`（必填，
        否则历史来源显示不出来）、`novel={book_id, stage, chapter_ids}`（retry 原样带回）。

        长文策略全在 `prompts.build_stage_content`：分章节 + 分槽上限 + 摘要链；
        超限说明原样回给前端展示，不静默截断。
        """
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        stage = str(body.get("stage") or "").strip()
        if stage not in prompts.STAGES:
            return {"error": f"未知阶段：{stage or '（空）'}"}, 400
        book_id = str(body.get("book_id") or "").strip()
        model_name = str(body.get("model_name") or "").strip()
        if not model_name:
            return {"error": "请选择模型；可用模型来自设置页的模型检查结果"}, 400
        raw_ids = body.get("chapter_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return {"error": "至少选择一章作为素材"}, 400
        try:
            material = self.store.stage_material(book_id, raw_ids)
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
            built = prompts.build_stage_content(
                stage=stage,
                book_title=str(manifest.get("title") or "未命名作品"),
                chapters=material["chapters"],
                chapter_ids=material["chapter_ids"],
                summaries=manifest.get("summaries") or {},
                extra_note=str(body.get("extra_note") or ""),
                # 剧本必带大纲与人物槽：不带就会同一个角色换个名字（阶段三 §三/§五）。
                outline=str(selected_outline.get("text") if selected_outline else manifest.get("outline") or ""),
                characters=characters,
            )
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except (NovelServiceError, prompts.PromptError) as exc:
            return {"error": str(exc)}, 400

        title = prompts.task_title(str(material["manifest"].get("title") or "未命名作品"), stage)
        input_data = {
            "content": built["content"],
            "model_key": str(body.get("model_key") or ""),
            "model_name": model_name,
            "title": title,
            "module": "novel",
            # 必填：缺了历史列表的「来源」就落回「来源未记录」。
            "origin_label": "创作场",
            "user_request": str(body.get("extra_note") or ""),
            "novel": {
                "book_id": book_id,
                "stage": stage,
                "chapter_ids": material["chapter_ids"],
                # 提交时的作品版本：写回时据此判「A 未完又生成 B」的竞态。
                "book_revision": int(material["manifest"].get("revision") or 0),
                "elements_version": elements_version,
                "outline_version": outline_version,
            },
        }
        try:
            task = self.app.studio.jobs.submit(input_data)
        except ValueError as exc:
            return {"error": str(exc)}, 400
        except LookupError as exc:
            return {"error": str(exc)}, 404
        # 摘要链原料：每章首尾片段（零额外推理），供后续批次维持跨章一致性。
        summaries = {
            str(row["i"]): prompts.summarize_chapter(str(row["content"]))
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
            book_revision = int(material["manifest"].get("revision") or 0)
        return {"task": task, "warnings": built["warnings"], "book_revision": book_revision}, 201

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
        """把任务结果写回作品（要素 / 大纲）。

        写回前核对任务 id 与阶段：任务里的 `novel.book_id` / `novel.stage` 必须与请求
        一致，`revision` 对不上就 409——这是「A 未完又生成 B」互相覆盖的防线。
        """
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        stage = str(body.get("stage") or "").strip()
        task_id = str(body.get("task_id") or "").strip()
        text = body.get("text")
        if stage not in prompts.STAGES:
            return {"error": f"未知阶段：{stage or '（空）'}"}, 400
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
        try:
            if stage == "elements":
                book = self.store.apply_elements(
                    book_id, task_id=task_id, raw=text,
                    expected_revision=body.get("revision"),
                    chapter_ids=novel_input.get("chapter_ids") or [],
                )
            elif stage == "script":
                chapter_ids = novel_input.get("chapter_ids") or []
                from_chapter = int(chapter_ids[0]) if chapter_ids else 0
                book = self.store.apply_script(
                    book_id, task_id=task_id, text=text,
                    from_chapter=from_chapter, chapter_ids=chapter_ids,
                    expected_revision=body.get("revision"),
                )
            else:
                book = self.store.apply_outline(
                    book_id, task_id=task_id, text=text,
                    expected_revision=body.get("revision"),
                    chapter_ids=novel_input.get("chapter_ids") or [],
                )
            return {"book": book}, 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelConflictError as exc:
            return {"error": str(exc)}, 409
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    def api_get_book(self, book_id: str) -> tuple[dict[str, Any], int]:
        try:
            return self.store.get_book(book_id), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    # ---- 阶段三：剧本场次 ----

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

    def api_get_chapter(self, book_id: str, chapter_no: Any) -> tuple[dict[str, Any], int]:
        try:
            return self.store.get_chapter(book_id, self._chapter_no(chapter_no)), 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

    def api_update_chapter(self, book_id: str, chapter_no: Any,
                           body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if not isinstance(body, dict):
            return {"error": "请求体必须是对象"}, 400
        try:
            chapter = self.store.update_chapter(
                book_id, self._chapter_no(chapter_no),
                title=body.get("title"), content=body.get("content"),
                expected_revision=body.get("revision"),
            )
            return {"chapter": chapter}, 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelConflictError as exc:
            return {"error": str(exc)}, 409
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

    def api_export_book(self, book_id: str, fmt: str = "txt",
                        with_extras: Any = "") -> tuple[dict[str, Any], int]:
        """全书导出。`with_extras` 为真时附要素与大纲（阶段三的全书导出）。"""
        if fmt not in ("txt", "md"):
            return {"error": "导出格式只支持 txt 或 md"}, 400
        extras = str(with_extras).strip().lower() in ("1", "true", "yes", "on")
        try:
            return {"format": fmt, "text": self.store.export_book(book_id, fmt=fmt, with_extras=extras)}, 200
        except LookupError as exc:
            return {"error": str(exc)}, 404
        except NovelServiceError as exc:
            return {"error": str(exc)}, 400

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
