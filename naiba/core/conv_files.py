"""会话文件查看/编辑保存（右侧文件面板后端，原 server.py _conv_file_* 族）。

安全边界：面板只允许"本会话改动过 或 位于会话工作区内"的文件；
保存写回必须同时满足两者；越界路径（../穿越、绝对路径、http 前缀）一律拒绝。
config 对象只要求 resolve_workspace_dir(raw) 契约（窄接口，见设计文档 §2.1）。

另含三项会话工作区能力（输入框 @ 引用文件/目录）：
- ``browse_workspace_tree``：会话工作区内的浅层目录浏览（只读、越界拒绝、隐藏项过滤）；
- ``resolve_file_references``：把用户消息里的 ``@相对路径`` 解析成工作区内绝对路径（模型可见文本）；
- ``folder_index``：把"拖进输入区的文件夹"扫成一份**相对路径清单**（给模型看它下面有什么，
  用户不必逐个复制文件路径），带深度/条数/超时三重闸门。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from naiba.core.media_types import media_kind_of
from naiba.core.paths import path_within

_CONV_FILE_SNIFF_BYTES = 4096          # 二进制嗅探长度
_CONV_FILE_READ_CAP = 2 * 1024 * 1024  # 单次读取/预览上限
_CONV_FILE_SAVE_CAP = 8 * 1024 * 1024  # 单次写回上限
_CONV_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"})

# 工作区浏览：单目录最多返回条目数与恒定隐藏项（VCS/宿主写入探测目录）。
WORKSPACE_BROWSE_LIMIT = 500
_ALWAYS_HIDDEN_NAMES = frozenset({".git", ".naiba_write_test"})

# ---- 文件夹索引闸门（三个值都会影响**送进模型的字符量**，不是体验参数）-----------
# 一次拖入可能是几百上千个文件的素材目录：清单原样拼进上下文能瞬间吃掉几万字符。
# 深度 / 条数 / 扫描超时三重闸门缺一不可——条数挡住"一层里塞几千个"，深度挡住
# "递归进 node_modules 式的深井"，超时挡住"网络盘/机械盘上慢到没边"。
FOLDER_INDEX_MAX_DEPTH = 3
FOLDER_INDEX_MAX_ENTRIES = 300
FOLDER_INDEX_SCAN_TIMEOUT = 5.0


class FolderConfirmRequired(ValueError):
    """文件夹在会话工作区之外，且本会话尚未确认过读取（口径二，见计划 B.5）。

    带上 ``path`` 让 HTTP 层回一个可操作的结构（前端据此弹一次确认），而不是一句干巴巴的 403。
    """

    def __init__(self, path: str) -> None:
        super().__init__("该文件夹不在会话工作区内，需要先确认允许读取")
        self.path = str(path)

# @ 引用 token：@ 必须位于行首或空白之后；支持 @"含 空格 的路径" 引号形态。
_FILE_REF_RE = re.compile(r'(?<!\S)@(?:"(?P<quoted>[^"\n]+)"|(?P<plain>[^\s]+))')
# 用户常在引用后直接跟标点（「见 @a.md。」）：解析失败时逐个剥掉尾部标点重试。
_REF_TRAILING_PUNCT = "。，、；：！？）】》」』”’.,;:!?)]}>"


def _conv_touched_files(conversation: dict[str, Any] | None) -> list[str]:
    """该会话所有 assistant 消息 metadata.files 里的原始路径（去重）。"""
    result: list[str] = []
    seen: set[str] = set()
    for message in (conversation or {}).get("messages") or []:
        meta = (message or {}).get("metadata") or {}
        files = meta.get("files") if isinstance(meta, dict) else None
        if not isinstance(files, list):
            continue
        for item in files:
            raw = str((item if isinstance(item, dict) else {}).get("path") or "").strip()
            if not raw:
                continue
            key = raw.replace("\\", "/").rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(raw)
    return result


def _conv_workspace_root(conversation: dict[str, Any] | None, config: Any) -> Path | None:
    """会话生效的工作区根：优先会话 workspace_dir，否则配置默认工作区。"""
    raw = str((conversation or {}).get("workspace_dir") or "").strip()
    try:
        return config.resolve_workspace_dir(raw or None).resolve()
    except (OSError, ValueError):
        return None


def _conv_file_target(raw_path: Any, root: Path | None) -> Path | None:
    """把消息记录里的路径解析成磁盘绝对路径；相对路径按工作区根补全。"""
    raw = str(raw_path or "").strip()
    if not raw or "\x00" in raw or raw.startswith(("http://", "https://", "\\\\")):
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        if root is None:
            return None
        path = root / path
    return path.resolve()


def _conv_file_allow(
    conversation: dict[str, Any] | None, config: Any, raw_path: Any
) -> tuple[Path | None, bool, bool, Path | None]:
    """返回 (目标文件, listed, within_root, root)。listed=本会话改动过；within_root=在工作区内。"""
    raw = str(raw_path or "").strip()
    root = _conv_workspace_root(conversation, config)
    target = _conv_file_target(raw, root)
    touched_lower = {item.replace("\\", "/").rstrip("/").lower() for item in _conv_touched_files(conversation)}
    raw_key = raw.replace("\\", "/").rstrip("/").lower()
    listed = bool(target and str(target).replace("\\", "/").rstrip("/").lower() in touched_lower) or raw_key in touched_lower
    within_root = bool(target and root and path_within(target, root))
    return target, listed, within_root, root


def _conv_file_open(conversation: dict[str, Any] | None, config: Any, raw_path: Any) -> dict[str, Any]:
    """读取会话文件的预览信息（文本带内容；图片/二进制只给元数据）。"""
    target, listed, within_root, _root = _conv_file_allow(conversation, config, raw_path)
    if target is None or not (listed or within_root):
        raise ValueError("无权访问该文件：不在本会话改动记录中，也不在会话工作区内")
    if not target.is_file():
        raise FileNotFoundError(str(target))
    stat = target.stat()
    suffix = target.suffix.lower()
    name = target.name
    info: dict[str, Any] = {
        "path": str(target),
        "name": name,
        "size": stat.st_size,
        "mtime": round(stat.st_mtime * 1000),
        "savable": False,
        "kind": "binary",
        "truncated": False,
    }
    if suffix in _CONV_IMAGE_EXTS:
        info["kind"] = "image"
        return info
    with target.open("rb") as handle:
        head = handle.read(_CONV_FILE_SNIFF_BYTES)
    if b"\x00" in head:
        info["kind"] = "binary"
        return info
    info["kind"] = "text"
    info["savable"] = bool(listed and within_root)
    with target.open("rb") as handle:
        payload = handle.read(_CONV_FILE_READ_CAP + 1)
    info["truncated"] = len(payload) > _CONV_FILE_READ_CAP
    info["content"] = payload[:_CONV_FILE_READ_CAP].decode("utf-8", errors="replace")
    return info


def _conv_file_save(
    conversation: dict[str, Any] | None, config: Any, raw_path: Any, content: Any
) -> dict[str, Any]:
    """把右侧面板的编辑内容写回磁盘。

    安全门槛：目标必须同时在"本会话改动记录"与"会话工作区"内；拒绝其他任何路径。
    """
    target, listed, within_root, _root = _conv_file_allow(conversation, config, raw_path)
    if target is None or not (listed and within_root):
        raise ValueError("无权保存该文件：只允许保存本会话改动过且位于会话工作区内的文件")
    text = str(content if content is not None else "")
    if len(text.encode("utf-8", errors="replace")) > _CONV_FILE_SAVE_CAP:
        raise ValueError("保存内容过大，已超过上限")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.naiba-tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
    stat = target.stat()
    return {"path": str(target), "name": target.name, "size": stat.st_size, "mtime": round(stat.st_mtime * 1000)}


def _workspace_entries(
    target: Path, root: Path, *, hide_dotfiles: bool, limit: int
) -> tuple[list[dict[str, Any]], bool]:
    """列一层目录（目录优先、名称序）；返回 (entries, truncated)。"""
    try:
        children = sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    except OSError as exc:
        raise ValueError(f"无法读取目录：{exc}") from exc
    entries: list[dict[str, Any]] = []
    truncated = False
    for child in children:
        name = child.name
        if name in _ALWAYS_HIDDEN_NAMES:
            continue
        if hide_dotfiles and name.startswith("."):
            continue
        if len(entries) >= limit:
            truncated = True
            break
        try:
            is_dir = child.is_dir()
            size = None if is_dir else child.stat().st_size
        except OSError:
            continue
        entries.append(
            {
                "name": name,
                "path": str(child),
                "rel": child.relative_to(root).as_posix(),
                "kind": "directory" if is_dir else "file",
                "size": size,
            }
        )
    return entries, truncated


def browse_workspace_tree(
    root: Path,
    raw_path: Any = "",
    *,
    hide_dotfiles: bool = False,
    limit: int = WORKSPACE_BROWSE_LIMIT,
) -> dict[str, Any]:
    """会话工作区浅层浏览（只读）：当前目录条目 + 面包屑/返回上级所需相对路径。

    ``raw_path`` 支持绝对路径与相对工作区根的相对路径；越界与不存在一律报错。
    """
    root = Path(root).resolve()
    raw = str(raw_path or "").strip()
    target = Path(raw).expanduser() if raw else root
    if not target.is_absolute():
        target = root / raw
    target = target.resolve()
    if not path_within(target, root):
        raise ValueError("浏览路径必须位于会话工作区内")
    if not target.exists() or not target.is_dir():
        raise ValueError("目录不存在或已被移动")
    entries, truncated = _workspace_entries(
        target, root, hide_dotfiles=hide_dotfiles, limit=limit
    )
    at_root = target == root
    return {
        "root": str(root),
        "path": str(target),
        "rel": "" if at_root else target.relative_to(root).as_posix(),
        "parent": "" if at_root else str(target.parent),
        "parent_rel": "" if at_root else (
            "" if target.parent == root else target.parent.relative_to(root).as_posix()
        ),
        "truncated": truncated,
        "entries": entries,
    }


def _resolve_reference_token(raw: str, root: Path) -> tuple[str, str] | None:
    """把单个 @ token 解析成工作区内绝对路径。

    返回 ``(被替换的原文片段, 替换文本)``；目录保留尾部路径分隔符以示"这是目录"。
    解析失败（不存在 / 越界 / 空）返回 None —— token 原样保留，邮箱等误报因此不受影响。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    stripped = text.rstrip(_REF_TRAILING_PUNCT)
    candidates = [text] if (not stripped or stripped == text) else [text, stripped]
    for candidate in candidates:
        base = candidate.rstrip("/\\")
        if not base:
            continue
        path = Path(base).expanduser()
        if not path.is_absolute():
            path = root / base
        try:
            resolved = path.resolve()
        except (OSError, ValueError):
            continue
        if not path_within(resolved, root):
            continue
        try:
            if resolved.is_dir():
                return candidate, str(resolved) + os.sep
            if resolved.is_file():
                return candidate, str(resolved)
        except OSError:
            continue
    return None


def resolve_file_references(text: str, root: Path | None) -> str:
    """把用户消息里的 @ 工作区引用替换成绝对路径（模型可见文本）。

    只替换"能解析到会话工作区内真实存在的文件/目录"的 token；其余（邮箱、普通 @ 提及、
    工作区外路径、不存在的路径）原样保留，因此该替换对普通文本无副作用。
    """
    value = str(text or "")
    if not value or root is None:
        return value
    root = Path(root).resolve()
    out: list[str] = []
    pos = 0
    for match in _FILE_REF_RE.finditer(value):
        quoted = match.group("quoted")
        raw = quoted if quoted is not None else match.group("plain")
        resolved = _resolve_reference_token(raw, root)
        if resolved is None:
            continue
        consumed, replacement = resolved
        if quoted is not None:
            # 连同 @" 与收尾引号一起替换，避免残留引号/@ 前缀。
            start, end = match.start("quoted") - 2, match.end()
        else:
            # 连同 @ 一起替换（整段引用 → 绝对路径）。
            start = match.start("plain") - 1
            end = match.start("plain") + len(consumed)
        out.append(value[pos:start])
        out.append(replacement)
        pos = end
    out.append(value[pos:])
    return "".join(out)


# ---- 文件夹索引（拖文件夹进输入区：只给模型一份路径清单，不上传内容） --------------

def _scan_folder_tree(
    target: Path,
    *,
    max_entries: int,
    max_depth: int,
    deadline: float,
) -> dict[str, Any]:
    """深度优先扫一棵目录树，返回计数 + **相对该目录**的条目清单。

    - 计数（``total`` / ``image_count`` / ``dir_count``）是**真实值**：条目清单被截断了也照数，
      否则气泡上的"共 128 项"会随截断一起变小，用户会以为文件丢了；
    - 条目清单只在预算内累积（``max_entries``）——它要拼进模型上下文，条数必须硬顶；
    - 点号开头条目与 ``_ALWAYS_HIDDEN_NAMES`` 一律跳过（.git 之类对"这个目录里有什么图"
      毫无信息量，纯属噪音）；
    - 超时/超条数都置 ``truncated``，并给出 ``truncated_reason``，让前端与模型都说得清
      "这份清单不全，别当成目录的全部内容"。
    """
    entries: list[dict[str, Any]] = []
    total = 0
    image_count = 0
    dir_count = 0
    truncated_reason = ""
    # 栈里放 (目录, 深度)；深度口径：直接躺在 target 里的文件算第 1 层。
    stack: list[tuple[Path, int]] = [(target, 0)]
    while stack:
        if time.monotonic() > deadline:
            truncated_reason = "timeout"
            break
        directory, depth = stack.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            continue
        descend: list[tuple[Path, int]] = []
        for child in children:
            name = child.name
            if name.startswith(".") or name in _ALWAYS_HIDDEN_NAMES:
                continue
            try:
                if child.is_dir():
                    dir_count += 1
                    if depth + 1 < max_depth:
                        descend.append((child, depth + 1))
                    continue
                size = child.stat().st_size
            except OSError:
                continue
            total += 1
            kind = media_kind_of(str(child)) or "file"
            if kind == "image":
                image_count += 1
            if len(entries) >= max_entries:
                truncated_reason = truncated_reason or "count"
                continue
            try:
                rel = child.relative_to(target).as_posix()
            except ValueError:  # 理论上不会发生；真发生就退化成绝对路径而不是丢条目
                rel = str(child)
            entries.append({"rel": rel, "kind": kind, "size": size})
        # 逆序入栈 ⇒ 出栈后按名称升序处理，扫描结果与文件系统顺序无关（可复现）。
        stack.extend(reversed(descend))
    return {
        "total": total,
        "image_count": image_count,
        "dir_count": dir_count,
        "entries": entries,
        "truncated": bool(truncated_reason),
        "truncated_reason": truncated_reason,
    }


def folder_index(
    target: Path,
    *,
    max_entries: int = FOLDER_INDEX_MAX_ENTRIES,
    max_depth: int = FOLDER_INDEX_MAX_DEPTH,
    timeout: float = FOLDER_INDEX_SCAN_TIMEOUT,
) -> dict[str, Any]:
    """把 ``target`` 目录扫成一份索引（只读，不读文件内容）。

    ``target`` 必须已经是**调用方校验过的绝对路径**（存在性/权限口径见 ``app.folder_index``）。
    """
    path = Path(target)
    name = path.name or str(path)
    result: dict[str, Any] = {
        "name": name,
        "path": str(path),
        "max_depth": int(max_depth),
        "max_entries": int(max_entries),
    }
    result.update(
        _scan_folder_tree(
            path,
            max_entries=int(max_entries),
            max_depth=int(max_depth),
            deadline=time.monotonic() + max(0.1, float(timeout)),
        )
    )
    return result
