# -*- coding: utf-8 -*-
"""拆书匠工具层：把 TXT 小说按章拆开，供模型在会话里直接调用。

设计要点（评审 §2 的六条补齐）：
1. **权限策略与 read_pdf 同构**：读文件走 ``_make_read_policy``，工作区/Skill 根内免确认，
   越界必确认；判定与执行都基于当前 Run 工作区（``ctx_for_run``）。
2. **硬预算**：outline 分页（``offset``/``limit``），chunks 限制单次章节数与字符数；
   超预算返回「可续取」信息，绝不静默截断（模型侧工具结果上限 60000 字符）。
3. **AI 生成文本入口**：``novel_split_text`` 直接吃字符串，不吃文件，复用同一套算法
   ——这是「AI 写长篇 → 自动拆章」闭环的前提。
4. **算法与 IO 分离**：算法在 ``naiba.core.novel_split``（纯字符串），本层只做读盘、
   编码嗅探、缓存与预算。
5. **索引参数严格校验**：拒绝布尔值、非整数、负值、超范围；重复索引去重保序。
6. 缓存按 ``(规范化绝对路径, mtime_ns, size)`` 签名，单槽 LRU（最多 4 本），
   避免多文件共用一槽导致错误复用。
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from naiba.core import novel_split

# 单次返回的正文上限（字符）：远低于模型工具结果的 60000 字符上限，
# 留出 JSON 结构与其他字段的余量。
MAX_CHUNK_CHARS = 40000
# 单次最多取几章正文。
MAX_CHUNK_COUNT = 40
# outline 单页最多返回多少条（章节数可能上千，一次全发会撑爆结果预算）。
OUTLINE_PAGE_DEFAULT = 200
OUTLINE_PAGE_MAX = 1000
# 文本入口的单次输入上限（字符）：AI 生成的长文也该在这个量级内。
MAX_TEXT_CHARS = 400000
# 读盘上限（字节）：与算法层常量同源，超了直接拒。
MAX_FILE_BYTES = novel_split.MAX_FILE_BYTES
# 编码嗅探顺序：utf-8-sig → utf-8 → gb18030 → big5（gb18030 是中文盗版 TXT 标配）。
ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5")
# 缓存槽数：同一进程里最多记住几本书，避免多文件共用一槽错误复用。
CACHE_SLOTS = 4

_cache_lock = threading.Lock()
_cache: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


class NovelError(ValueError):
    """拆书匠的输入/读取错误（用户可读，由工具边界转成失败文本）。"""


def _signature(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError as exc:
        raise NovelError(f"读不到文件：{path}（{exc}）") from exc
    raw = f"{path}|{stat.st_mtime_ns}|{stat.st_size}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def read_text_auto(path: Path) -> tuple[str, str]:
    """读文本并嗅探编码。返回 ``(text, encoding)``。

    注意：``encoding`` 是**实际选中的解码方式**，不是对源编码的确定性判定——
    gb18030 能解码大量其他编码的字节，所以「用 gb18030 读成功」不等于原文就是
    gb18030（评审 §1 的提醒）。
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise NovelError(f"读不到文件：{path}（{exc}）") from exc
    if size <= 0:
        raise NovelError("文件是空的")
    if size > MAX_FILE_BYTES:
        raise NovelError(f"文件太大（{size / 1024 / 1024:.1f} MB），上限 60 MB")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise NovelError(f"读取失败：{exc}") from exc
    for encoding in ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    # 全试失败：按 UTF-8 替换错误读出来（乱码也不崩，让用户看到内容而不是报错）。
    return data.decode("utf-8", errors="replace"), "utf-8(replace)"


def load_book(path: Path, max_len: int = novel_split.DEFAULT_MAX_LEN) -> dict[str, Any]:
    """读一本书并切章；按 ``(路径, mtime, 大小, max_len)`` 缓存，反复取章不重读盘。

    ``max_len`` 进签名：同一文件用不同的切块上限会得到不同章节表，
    共用一个缓存槽会串味（评审点名的「缓存错误复用」）。
    """
    resolved = path.expanduser().resolve()
    key = f"{_signature(resolved)}|{int(max_len)}"
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    text, encoding = read_text_auto(resolved)
    chapters = novel_split.split_chapters(text, int(max_len))
    book = {
        "path": str(resolved),
        "name": resolved.name,
        "encoding": encoding,
        "max_len": int(max_len),
        "total": len(chapters),
        "chapters": chapters,
    }
    with _cache_lock:
        _cache[key] = book
        _cache.move_to_end(key)
        while len(_cache) > CACHE_SLOTS:
            _cache.popitem(last=False)
    return book


def clear_cache() -> None:
    """测试与「换文件后重读」用。"""
    with _cache_lock:
        _cache.clear()


# ---- 参数校验 ----

def _int_arg(raw: Any, name: str, *, default: int | None = None,
             low: int = 0, high: int | None = None) -> int:
    """严格整数：拒绝布尔值（Python 里 True 是 int）、非整数、越界。"""
    if raw is None or raw == "":
        if default is None:
            raise NovelError(f"{name} 不能为空")
        return default
    if isinstance(raw, bool):
        raise NovelError(f"{name} 必须是整数（不接受 true/false）")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise NovelError(f"{name} 必须是整数") from None
    if value < low:
        raise NovelError(f"{name} 不能小于 {low}")
    if high is not None and value > high:
        raise NovelError(f"{name} 不能大于 {high}")
    return value


def _max_len_arg(raw: Any) -> int:
    """切块上限：默认 6000，范围 200–200000（太小会把正文切碎，太大失去意义）。"""
    return _int_arg(raw, "max_len", default=novel_split.DEFAULT_MAX_LEN, low=200, high=200000)


def parse_indexes(raw: Any, total: int) -> list[int]:
    """解析章节索引：去重保序、拒绝越界与非法值。"""
    if raw is None:
        raise NovelError("idx 不能为空：要取哪几章的正文")
    if isinstance(raw, (str, bytes)) or isinstance(raw, bool):
        raise NovelError("idx 必须是整数数组")
    if not isinstance(raw, (list, tuple)):
        raise NovelError("idx 必须是整数数组")
    seen: set[int] = set()
    out: list[int] = []
    for item in raw:
        value = _int_arg(item, "idx", low=0)
        if value >= total:
            raise NovelError(f"idx 超出范围：{value}（这本书共 {total} 章，索引从 0 开始）")
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    if not out:
        raise NovelError("idx 不能为空")
    if len(out) > MAX_CHUNK_COUNT:
        raise NovelError(f"一次最多取 {MAX_CHUNK_COUNT} 章；要更多请分批调用")
    return out


# ---- 三个工具的实现 ----

def novel_outline_impl(path: Path, *, offset: Any = 0, limit: Any = None,
                       max_len: Any = None) -> str:
    """章节索引（**不含正文**）：分页返回，附续取信息。"""
    book = load_book(path, _max_len_arg(max_len))
    total = int(book["total"])
    start = _int_arg(offset, "offset", default=0, low=0)
    page = _int_arg(limit, "limit", default=OUTLINE_PAGE_DEFAULT, low=1, high=OUTLINE_PAGE_MAX)
    end = min(total, start + page)
    rows = [
        {"i": index, "title": book["chapters"][index]["title"],
         "len": len(str(book["chapters"][index]["body"]))}
        for index in range(start, end)
    ]
    payload: dict[str, Any] = {
        "name": book["name"],
        "encoding": book["encoding"],
        "total": total,
        "offset": start,
        "returned": len(rows),
        "chapters": rows,
    }
    if end < total:
        payload["next_offset"] = end
        payload["note"] = (f"还有 {total - end} 章未列出：用 offset={end} 继续读索引，"
                           f"或用 novel_chunks 直接取指定章的正文。")
    elif total == 0:
        payload["note"] = "这个文件里没有可拆的章节（可能是空文件或不支持的格式）。"
    return _json(payload)


def novel_chunks_impl(path: Path, *, idx: Any = None, max_len: Any = None) -> str:
    """指定章节的正文（有序）；超预算时给可续取信息，不静默截断。"""
    book = load_book(path, _max_len_arg(max_len))
    indexes = parse_indexes(idx, int(book["total"]))
    rows: list[dict[str, Any]] = []
    used = 0
    truncated_at: int | None = None
    for position, index in enumerate(indexes):
        chapter = book["chapters"][index]
        body = str(chapter["body"])
        if used + len(body) > MAX_CHUNK_CHARS:
            truncated_at = position
            break
        rows.append({"i": index, "title": chapter["title"], "content": body})
        used += len(body)
    if not rows:
        first = book["chapters"][indexes[0]]
        raise NovelError(
            f"第 {indexes[0]} 章正文有 {len(str(first['body']))} 字符，"
            f"超过单次上限 {MAX_CHUNK_CHARS}；请先调大 max_len 重新拆章，或分批处理"
        )
    payload: dict[str, Any] = {
        "name": book["name"],
        "encoding": book["encoding"],
        "total": int(book["total"]),
        "returned": len(rows),
        "chars": used,
        "chapters": rows,
    }
    if truncated_at is not None:
        payload["remaining_idx"] = indexes[truncated_at:]
        payload["note"] = (f"本次返回 {len(rows)} 章（{used} 字符），达到单次上限；"
                           f"剩余 {len(indexes) - len(rows)} 章请用 remaining_idx 再取。")
    return _json(payload)


def novel_split_text_impl(text: Any, *, max_len: Any = None, outline_only: Any = False) -> str:
    """AI 生成的长文本直接拆章（不吃文件），复用同一套算法。

    ``outline_only=True`` 只回索引（长文推荐先看索引，再按需取正文）。
    """
    raw = str(text or "")
    if not raw.strip():
        raise NovelError("text 不能为空")
    if len(raw) > MAX_TEXT_CHARS:
        raise NovelError(f"文本太长（{len(raw)} 字符），上限 {MAX_TEXT_CHARS}；请分段处理")
    length = _int_arg(max_len, "max_len", default=novel_split.DEFAULT_MAX_LEN,
                      low=200, high=200000)
    if outline_only:
        rows = novel_split.outline(raw, length)
        return _json({
            "total": len(rows),
            "chars": len(raw),
            "chapters": rows,
            "note": "只返回索引；要正文请调 novel_split_text 并把 outline_only 设为 false。",
        })
    chapters = novel_split.split_chapters(raw, length)
    total_chars = sum(len(str(row["body"])) for row in chapters)
    if total_chars > MAX_CHUNK_CHARS:
        return _json({
            "total": len(chapters),
            "chars": total_chars,
            "chapters": [
                {"i": index, "title": row["title"], "len": len(str(row["body"]))}
                for index, row in enumerate(chapters)
            ],
            "note": (f"拆出 {len(chapters)} 章、共 {total_chars} 字符，超过单次返回上限 "
                     f"{MAX_CHUNK_CHARS}；这里只给索引，要正文请把 outline_only 设为 true 后"
                     f"按章处理，或缩小 max_len。"),
        })
    return _json({
        "total": len(chapters),
        "chars": total_chars,
        "chapters": [
            {"i": index, "title": row["title"], "content": str(row["body"])}
            for index, row in enumerate(chapters)
        ],
    })


def _json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


# ---- Provider：把三个实现接进统一工具表 ----

class NovelToolProvider:
    """拆书匠：3 个工具单一定义（schema 在 registry.build_novel_tool_specs）。

    权限策略：读文件的两个工具（novel_outline / novel_chunks）与 read_pdf 同构——
    工作区/Skill 根内免确认，越界必确认；novel_split_text 只吃字符串、不碰文件系统，
    无需确认。
    """

    def __init__(self, context: Any = None) -> None:
        self._context = context

    def tools(self) -> list[Any]:
        import dataclasses

        from naiba.tools.providers.core import _make_read_policy
        from naiba.tools.registry import build_novel_tool_specs

        specs = {spec.name: spec for spec in build_novel_tool_specs()}
        rows: list[Any] = []
        for name, impl in (
            ("novel_outline", self._outline),
            ("novel_chunks", self._chunks),
            ("novel_split_text", self._split_text),
        ):
            spec = specs[name]
            policy = (
                _make_read_policy(self._context)
                if (self._context is not None and name != "novel_split_text")
                else _never_confirm
            )
            rows.append(dataclasses.replace(spec, execute=self._wrap(impl), policy=policy))
        return rows

    def _wrap(self, impl: Any) -> Any:
        def _execute(
            arguments: dict[str, Any],
            active_skills: list[dict[str, Any]],
            run_context: dict[str, Any] | None = None,
        ) -> tuple[bool, str]:
            try:
                return True, impl(arguments or {}, active_skills, run_context)
            except NovelError as exc:
                return False, str(exc)
            except OSError as exc:
                return False, f"文件操作失败：{exc}"
            except Exception as exc:  # noqa: BLE001 - 工具边界：不吞，转明确失败文本
                return False, f"拆章失败：{type(exc).__name__}: {exc}"

        return _execute

    def _resolve(self, raw: Any, active_skills: list[dict[str, Any]],
                 run_context: dict[str, Any] | None) -> Path:
        """路径解析与 read_pdf 同源（相对路径按当前 Run 工作区）。"""
        from naiba.tools.providers.core import _resolve_read_path, ctx_for_run

        ctx = ctx_for_run(self._context, run_context) if self._context is not None else None
        if ctx is None:
            path = Path(str(raw or "")).expanduser()
            if not path.is_absolute():
                raise NovelError("请给出文件的绝对路径")
            return path
        return _resolve_read_path(ctx, raw, active_skills)

    def _outline(self, args: dict[str, Any], active_skills: list[dict[str, Any]],
                 run_context: dict[str, Any] | None) -> str:
        path = self._resolve(args.get("path"), active_skills, run_context)
        return novel_outline_impl(
            path, offset=args.get("offset", 0), limit=args.get("limit"),
            max_len=args.get("max_len"),
        )

    def _chunks(self, args: dict[str, Any], active_skills: list[dict[str, Any]],
                run_context: dict[str, Any] | None) -> str:
        path = self._resolve(args.get("path"), active_skills, run_context)
        return novel_chunks_impl(path, idx=args.get("idx"), max_len=args.get("max_len"))

    def _split_text(self, args: dict[str, Any], active_skills: list[dict[str, Any]],
                    run_context: dict[str, Any] | None) -> str:
        return novel_split_text_impl(
            args.get("text"),
            max_len=args.get("max_len"),
            outline_only=bool(args.get("outline_only")),
        )


def _never_confirm(
    tool: str,
    arguments: dict[str, Any],
    active_skills: list[dict[str, Any]],
    permission_mode: str,
    run_context: dict[str, Any] | None,
    workspace: Path | None = None,
) -> str:
    """novel_split_text 只处理传入的字符串，不碰文件系统，无需确认。"""
    return ""
