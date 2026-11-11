"""贴签匠 TXT 输出：结构化工具调用 → 与图片同名的 ``.txt``。

口径（2026-11-10 计划）：
- 工具 ``write_caption_txt`` 只对**当前这一张图**开放：``item`` 必须匹配本次上下文里的
  图片（文件名 / 相对路径 / item_id），模型无法借参数写到别的图片或别的任务；
- 落盘位置固定为 ``data/labeler/outputs/<task_id>/<图片相对路径去扩展名>.txt``，
  相对路径沿用任务里的 ``relative_path``（已由 ``images.py`` 清洗）；这里再做一次
  纵深防御（去掉 ``..`` / 盘符 / 绝对前缀），并断言最终路径仍在任务输出目录内；
- ``mode=write`` 整份覆盖，``mode=append`` 按调用顺序追加——最终内容就是各段
  原样拼接，不偷偷插入分隔符（分隔符由模型自己写进段里）；
- 模型没调用工具时，runner 用标注结果回填一份（状态 ``auto``），保证每张成功图片
  都有同名 TXT；模型写过（状态 ``written``）就保留模型分段输出的内容。

本模块不碰数据库与事件流：落库/事件通过构造函数注入的回调完成，便于单测。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

TXT_TOOL_NAME = "write_caption_txt"
# 单次工具调用最多接受 2 万字符（caption 正常只有几百字；异常长文本直接拒绝，
# 不让一次调用把输出目录写爆）。
MAX_SEGMENT_CHARS = 20000
# 单张图片的 TXT 总量上限（分段追加累计）。
MAX_FILE_CHARS = 400000

TXT_STATE_WRITTEN = "written"   # 模型通过工具写入（可能分多段）
TXT_STATE_AUTO = "auto"         # 模型没调用工具，由标注结果回填
TXT_STATE_FAILED = "failed"     # 写入失败（磁盘/路径异常）

_TOOL_DESCRIPTION = (
    "把当前这张图片的打标文本写入与图片同名的 .txt 文件（保存在本批次输出目录）。"
    "内容一次写完用 mode=write；内容多、想分段写时先用 write 写第一段，再用 append 追加后续段。"
    "每张图只能写自己的文件：item 填当前图片的文件名或相对路径（不要填别的图）。"
    "写入只影响 TXT 文件，打标结果仍按原有格式正常输出。"
)

_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "item": {
            "type": "string",
            "description": "当前图片标识：文件名、相对路径或任务项 ID（必须与当前这张图一致）",
        },
        "mode": {
            "type": "string",
            "enum": ["write", "append"],
            "description": "write=创建或整份覆盖；append=把 content 追加到文件末尾",
        },
        "content": {
            "type": "string",
            "description": "要写入的文本段（原样写入，不自动补换行或分隔符）",
        },
        "segment": {
            "type": "integer",
            "description": "可选：段序号，从 1 开始；仅用于回执与排查，不影响写入顺序",
        },
    },
    "required": ["item", "mode", "content"],
}

_path_lock = threading.Lock()


def tool_schema() -> dict[str, Any]:
    """模型可见的工具声明的精简行（形状与 ToolRegistry.schemas() 一致）。"""
    return {
        "name": TXT_TOOL_NAME,
        "description": _TOOL_DESCRIPTION,
        "parameters": _TOOL_PARAMETERS,
        "side_effect": True,
        "retryable": False,
        "timeout": 30,
        "permission": "auto",
        "annotations": {},
    }


def _clean_parts(raw: str) -> list[str]:
    parts: list[str] = []
    for part in str(raw or "").replace("\\", "/").split("/"):
        part = part.strip()
        if not part or part in (".", "..") or ":" in part:
            continue
        parts.append(part)
    return parts


def caption_txt_relative(relative_path: str, name: str = "") -> str:
    """图片相对路径 → 同名 ``.txt`` 相对路径（保留子目录，与导出同一口径）。"""
    parts = _clean_parts(relative_path)
    if not parts:
        parts = _clean_parts(name)
    if not parts:
        parts = ["image"]
    leaf = Path(parts[-1])
    parts[-1] = (leaf.stem or "image") + ".txt"
    return "/".join(parts)


def build_txt_targets(items: list[dict[str, Any]]) -> dict[str, str]:
    """为任务内全部图片分配互不冲突的 TXT 相对路径（按 ordinal 顺序）。"""
    used: set[str] = set()
    out: dict[str, str] = {}
    for item in items:
        relative = caption_txt_relative(
            str(item.get("relative_path") or ""), str(item.get("name") or "")
        )
        candidate = relative
        stem, _, suffix = candidate.rpartition(".")
        counter = 2
        while candidate.lower() in used:
            candidate = f"{stem}-{counter}.{suffix or 'txt'}"
            counter += 1
        used.add(candidate.lower())
        out[str(item.get("item_id") or "")] = candidate
    return out


def _normalize_ref(value: Any) -> str:
    return "/".join(_clean_parts(str(value or "")))


def item_matches(value: Any, item: dict[str, Any]) -> bool:
    """``item`` 参数是否指向本次上下文的这张图（宽松：文件名/相对路径/ID/主干）。"""
    wanted = _normalize_ref(value)
    if not wanted:
        return False
    relative = _normalize_ref(str(item.get("relative_path") or ""))
    name = str(item.get("name") or "").strip().replace("\\", "/")
    item_id = str(item.get("item_id") or "").strip()
    candidates = {item_id, relative, name}
    if relative:
        candidates.add(relative.rsplit("/", 1)[-1])
    if name:
        candidates.add(Path(name).stem)
    if relative:
        candidates.add(Path(relative).stem)
    return wanted in candidates or Path(wanted).stem in candidates


def _error(message: str) -> tuple[bool, str]:
    return False, json.dumps({"ok": False, "error": message}, ensure_ascii=False)


class TxtToolContext:
    """单张图片的 TXT 工具执行上下文（两次重试之间用 ``reset()`` 清干净）。"""

    def __init__(
        self,
        *,
        task_id: str,
        output_dir: Path,
        item: dict[str, Any],
        relative: str = "",
        on_record: Callable[[str, str, int], None] | None = None,
        on_event: Callable[[str], None] | None = None,
        staged: bool = False,
    ):
        self.task_id = str(task_id or "")
        self.output_dir = Path(output_dir)
        self.item = dict(item or {})
        self.item_id = str(self.item.get("item_id") or "")
        self.relative = relative or caption_txt_relative(
            str(self.item.get("relative_path") or ""), str(self.item.get("name") or "")
        )
        self._on_record = on_record
        self._on_event = on_event
        # 并发监督档仅在内存中暂存；批准前既无公开文件，也不写可下载的逐图状态。
        self.staged = bool(staged)
        self._content = ""
        self._published = False
        self.wrote = False
        self.segments = 0
        self.total_chars = 0
        # 最近一次落库状态（"" / written / auto / failed），runner 回执事件直接读它。
        self.state = ""

    # ---- 路径 ----

    def target_path(self) -> Path | None:
        """输出文件绝对路径；越出任务输出目录时返回 None。"""
        root = self.output_dir.resolve()
        path = (root / self.relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return None
        return path

    def _ensure_parent(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)

    def _record(self, state: str, chars: int) -> None:
        self.state = str(state or "")
        if (not self.staged or self._published) and callable(self._on_record):
            try:
                self._on_record(state, self.relative if state else "", int(chars))
            except Exception:  # noqa: BLE001 - 状态记录失败不影响已落盘的 TXT
                pass

    def _event(self, message: str) -> None:
        if callable(self._on_event):
            try:
                self._on_event(message)
            except Exception:  # noqa: BLE001 - 事件失败不影响写入
                pass

    # ---- 生命周期 ----

    def reset(self) -> None:
        """重试前清掉上一次尝试留下的半成品（避免重复追加）。"""
        path = self.target_path()
        if path is not None and (not self.staged or self._published):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        self.wrote = False
        self.segments = 0
        self.total_chars = 0
        self._record("", 0)
        self._content = ""
        self._published = False

    def candidate_text(self) -> str:
        """返回最终交付的完整原文，供监督逐字审核；不截断。"""
        return self._content if self.staged else self._read_text()

    def publish(self) -> None:
        """监督批准后原子发布同一份已审文本；落盘失败必须向调用方显式报错。"""
        if not self.staged:
            return
        if not self.wrote or self.state == TXT_STATE_FAILED:
            raise ValueError("TXT 未完整生成，不能发布")
        path = self.target_path()
        if path is None:
            raise ValueError("TXT 目标路径越出输出目录")
        with _path_lock:
            self._ensure_parent(path)
            self._write_text(path, self._content)
        self._published = True
        self._record(self.state, self.total_chars)
        self._event(f"{self._label()}：监督通过，已发布 TXT")

    def write_fallback(self, text: str) -> bool:
        """模型没调用工具时，用最终标注文本回填一份（状态 ``auto``）。"""
        content = str(text or "")
        if self.wrote or not content.strip():
            return False
        path = self.target_path()
        if path is None:
            self._record(TXT_STATE_FAILED, 0)
            return False
        try:
            if self.staged:
                self._content = content
            else:
                self._ensure_parent(path)
                self._write_text(path, content)
        except OSError:
            self._record(TXT_STATE_FAILED, 0)
            return False
        self.total_chars = len(content)
        self.wrote = True
        self._record(TXT_STATE_AUTO, self.total_chars)
        self._event(f"{self._label()}：已按标注结果{'暂存待审' if self.staged else '写入'} TXT")
        return True

    # ---- 工具执行 ----

    def execute(self, arguments: dict[str, Any]) -> tuple[bool, str]:
        """校验并执行一次写入；返回 ``(success, JSON 文本)`` 供模型继续决策。"""
        if not isinstance(arguments, dict):
            return _error("参数必须是 JSON 对象")
        if not item_matches(arguments.get("item"), self.item):
            name = str(self.item.get("name") or self.item.get("relative_path") or "")
            return _error(f"item 必须指向当前图片（{name or '当前图片'}），不能写别的图片")
        mode = str(arguments.get("mode") or "").strip().lower()
        if mode not in {"write", "append"}:
            return _error("mode 必须是 write 或 append")
        content = arguments.get("content")
        if not isinstance(content, str) or not content.strip():
            return _error("content 必须是非空字符串")
        if len(content) > MAX_SEGMENT_CHARS:
            return _error(f"content 超过单次 {MAX_SEGMENT_CHARS} 字符上限，请分段写入")
        segment = arguments.get("segment")
        if segment is not None and (not isinstance(segment, int) or isinstance(segment, bool)
                                    or segment < 1):
            return _error("segment 必须是大于等于 1 的整数")
        if mode == "append":
            existing = self._read_text()
            if len(existing) + len(content) > MAX_FILE_CHARS:
                return _error(f"TXT 超过 {MAX_FILE_CHARS} 字符上限，已拒绝本次追加")
        else:
            if len(content) > MAX_FILE_CHARS:
                return _error(f"TXT 超过 {MAX_FILE_CHARS} 字符上限，已拒绝本次写入")

        path = self.target_path()
        if path is None:
            self._record(TXT_STATE_FAILED, self.total_chars)
            return _error("目标路径越出本批次输出目录，已拒绝写入")
        try:
            with _path_lock:
                if self.staged:
                    self._content = content if mode == "write" else self._content + content
                    self.total_chars = len(self._content)
                elif mode == "write":
                    self._ensure_parent(path)
                    self._write_text(path, content)
                    self.total_chars = len(content)
                else:
                    self._ensure_parent(path)
                    with path.open("a", encoding="utf-8", newline="\n") as handle:
                        handle.write(content)
                    self.total_chars += len(content)
        except OSError as exc:
            self._record(TXT_STATE_FAILED, self.total_chars)
            return _error(f"写入失败：{exc}")

        self.wrote = True
        self.segments += 1
        self._record(TXT_STATE_WRITTEN, self.total_chars)
        self._event(
            f"{self._label()}：已{'暂存待审' if self.staged else '写入'} TXT（{'覆盖' if mode == 'write' else '追加'}"
            f"，第 {self.segments} 段，共 {self.total_chars} 字）"
        )
        return True, json.dumps(
            {
                "ok": True,
                "file": self.relative,
                "action": mode,
                "segment": segment if isinstance(segment, int) else self.segments,
                "segments": self.segments,
                "chars": len(content),
                "total_chars": self.total_chars,
            },
            ensure_ascii=False,
        )

    # ---- 内部 ----

    def _label(self) -> str:
        return str(self.item.get("relative_path") or self.item.get("name") or "图片")

    def _read_text(self) -> str:
        if self.staged:
            return self._content
        path = self.target_path()
        if path is None or not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    @staticmethod
    def _write_text(path: Path, content: str) -> None:
        """整份覆盖：先写临时文件再替换，避免轮询读到半个文件。"""
        tmp = path.with_name(f".{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
