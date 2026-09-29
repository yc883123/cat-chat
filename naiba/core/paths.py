"""路径判定原语（层级 0/1）。阶段 3 的 PathContext 将以此为基础扩展。

判定口径（唯一，不得放宽）：
- ``path_within`` 以**调用方 resolve 后的路径**为准（符号链接/junction 一律按真实目标判定），
  越界即越界——不做"词法路径也算命中"的旁路（那会让工作区内指向外部的链接被误判为界内）；
- Windows 大小写不敏感由 ``pathlib`` 的 Windows 语义天然保证（``PureWindowsPath.relative_to``
  按不区分大小写比较），无需额外 normcase；
- 需要信任工作区外的链接目标时，必须把该**真实目标**显式加入允许根，而不是放宽本函数。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable


def path_within(path: Path, root: Path) -> bool:
    """判断 ``path`` 是否位于 ``root`` 内（resolve 后的语义，符号/相对路径由调用方处理）。"""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def normalized_path_key(value: Any) -> str:
    """路径比较键：绝对化 + 大小写归一（Windows 不区分大小写）。

    只做**词法**归一（``abspath`` 而非 ``resolve``）：不触盘、对不存在的文件同样可用，
    适合给「缓存文件是否被引用」这类集合判定做键。两侧（集合里的候选路径、待判定的
    真实文件）必须走同一个函数，否则会出现"同一条路径两种写法各算一份"的漏判。
    解析失败（非法盘符/超长路径等）返回空串，调用方按"判定不了"处理。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return os.path.normcase(os.path.abspath(os.path.expanduser(text)))
    except (OSError, ValueError):
        return ""


def path_within_any(path: Path, roots: Iterable[Path]) -> bool:
    """``path`` 是否位于任一 ``root`` 内（多允许根的统一判定入口）。"""
    return any(path_within(path, root) for root in roots)


def _safe_resolve(value: Any) -> Path:
    """解析失败（非法盘符/权限等）时退回 absolute()，保证诊断不抛异常。"""
    path = Path(value).expanduser()
    try:
        return path.resolve()
    except (OSError, ValueError):
        return path.absolute()


def within_detail(
    path: Any,
    roots: Iterable[Any],
    *,
    raw: Any = None,
    workspace: Any = None,
) -> dict[str, Any]:
    """权限判定诊断明细（只读，不改变判定结果）。

    返回 ``{raw, request, request_resolved, workspace, workspace_resolved, roots[], inside, hit}``；
    供"为什么被判越界"的日志与确认理由使用：``roots`` 里逐项给出每个允许根的原值与解析值
    以及是否命中（``hit`` 为命中下标，-1 表示全部未命中）。
    """
    resolved = _safe_resolve(path)
    rows: list[dict[str, Any]] = []
    hit = -1
    for index, root in enumerate(roots):
        resolved_root = _safe_resolve(root)
        inside = path_within(resolved, resolved_root)
        if inside and hit < 0:
            hit = index
        rows.append({"root": str(root), "resolved": str(resolved_root), "hit": inside})
    return {
        "raw": str(raw if raw is not None else path),
        "request": str(path),
        "request_resolved": str(resolved),
        "workspace": "" if workspace is None else str(workspace),
        "workspace_resolved": "" if workspace is None else str(_safe_resolve(workspace)),
        "roots": rows,
        "inside": hit >= 0,
        "hit": hit,
    }
