"""打标器输入校验（服务端权威，不信任客户端给的路径）。

与 ``naiba/studio/images.py`` 同一套安全口径，但批量上限不同：
- 只接受落在 ``data_dir/uploads`` 受管缓存树内的文件（``resolve()`` 会展开 junction /
  symlink，再判包含关系，绕出 uploads 的路径一律拒绝）；
- 扩展名白名单 + Pillow 真正解码一次（改后缀骗不过去）；
- 单张 20 MiB、每批最多 500 张、每批合计 1 GiB；超限**拒绝启动批次**，不静默截断。

输出统一为 ``{path, path_key, name, relative_path, size, mtime_ns, content_version}``；
``relative_path`` 只来自前端枚举出来的相对名称，服务端绝不拿它拼落盘路径。
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Any

from naiba.core.paths import normalized_path_key
from naiba.storage.media import is_uploads_path

ALLOWED_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
MAX_BATCH_IMAGES = 500
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_BATCH_BYTES = 1024 * 1024 * 1024
NAME_MAX = 200
RELATIVE_PATH_MAX = 400


class LabelerImageError(ValueError):
    """图片引用非法（越权路径 / 不存在 / 非图片 / 超限）。"""


def _clean_relative_path(raw: Any) -> str:
    """只保留「正斜杠分隔的相对名称」：去掉盘符/绝对前缀，拒绝 ``..`` 段。

    这个值只用于**分组与导出命名**，永远不参与落盘路径拼接；这里再收一遍是为了
    避免导出文件名里出现 ``..`` 或反斜杠造成混乱。
    """
    text = str(raw or "").replace("\\", "/").strip()
    if not text:
        return ""
    # 去掉浏览器 webkitRelativePath 可能带的绝对前缀，只留相对段。
    parts = [part for part in PurePosixPath(text).parts if part not in ("", ".", "/")]
    safe: list[str] = []
    for part in parts:
        if part == ".." or ":" in part:
            continue
        safe.append(part)
    return "/".join(safe)[:RELATIVE_PATH_MAX]


def _verify_image(path: Path, label: str) -> None:
    suffix = path.suffix.lower()
    if suffix not in ALLOWED_IMAGE_EXTS:
        raise LabelerImageError(f"{label}格式不支持（只接受 PNG、JPG、WEBP、GIF）")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise LabelerImageError(f"{label}读取失败：{exc}") from exc
    if size <= 0:
        raise LabelerImageError(f"{label}是空文件")
    if size > MAX_IMAGE_BYTES:
        limit_mb = MAX_IMAGE_BYTES // (1024 * 1024)
        raise LabelerImageError(f"{label}超过 {limit_mb}MB 上限")
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
    except ImportError as exc:  # pragma: no cover - 打包版必带 Pillow
        raise LabelerImageError("图片校验组件不可用") from exc
    except (OSError, ValueError) as exc:
        raise LabelerImageError(f"{label}不是可读取的图片：{exc}") from exc


def content_version(size: int, mtime_ns: int) -> str:
    """内容版本：大小 + 修改时间（不读全文哈希，批量场景下够用且便宜）。"""
    return f"{int(size)}:{int(mtime_ns)}"


def validate_image_refs(data_dir: Path, refs: Any) -> list[dict[str, Any]]:
    """校验并规范化批量图片引用；保持提交顺序（= 处理顺序）。"""
    if refs is None:
        return []
    if not isinstance(refs, list):
        raise LabelerImageError("image_refs 必须是数组")
    if not refs:
        raise LabelerImageError("请先选择要打标的图片")
    if len(refs) > MAX_BATCH_IMAGES:
        raise LabelerImageError(
            f"每批最多 {MAX_BATCH_IMAGES} 张图片，请拆成多批后重试（本次 {len(refs)} 张）"
        )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_bytes = 0
    for index, ref in enumerate(refs, 1):
        label = f"图{index}"
        if not isinstance(ref, dict):
            raise LabelerImageError(f"{label}引用结构非法")
        raw_path = str(ref.get("path") or "").strip()
        if not raw_path:
            raise LabelerImageError(f"{label}缺少文件路径")
        try:
            path = Path(raw_path).expanduser().resolve()
        except (OSError, ValueError) as exc:
            raise LabelerImageError(f"{label}路径无效：{exc}") from exc
        if not is_uploads_path(data_dir, path):
            raise LabelerImageError(f"{label}不在受管上传目录内，已拒绝")
        _verify_image(path, label)
        key = normalized_path_key(path)
        if not key:
            raise LabelerImageError(f"{label}路径无法归一")
        # 同一批里重复引用同一文件只处理一次（文件夹去重/同内容上传都会撞到这里）。
        if key in seen:
            continue
        seen.add(key)
        try:
            stat = path.stat()
        except OSError as exc:
            raise LabelerImageError(f"{label}读取失败：{exc}") from exc
        total_bytes += int(stat.st_size)
        if total_bytes > MAX_BATCH_BYTES:
            limit_gb = MAX_BATCH_BYTES / (1024 * 1024 * 1024)
            raise LabelerImageError(
                f"本批图片总大小超过 {limit_gb:g} GiB 上限，请拆成多批后重试"
            )
        name = str(ref.get("name") or path.name).strip()[:NAME_MAX] or path.name
        relative = _clean_relative_path(ref.get("relative_path")) or name
        out.append({
            "path": str(path),
            "path_key": key,
            "name": name,
            "relative_path": relative[:RELATIVE_PATH_MAX],
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
            "content_version": content_version(stat.st_size, stat.st_mtime_ns),
        })
    if not out:
        raise LabelerImageError("没有可用的图片")
    return out


def stable_item_id(task_id: str, path_key: str, content_version_value: str) -> str:
    """逐图稳定 ID：同一任务同一图片同一内容版本只算一项。"""
    digest = hashlib.sha1(
        f"{task_id}\0{path_key}\0{content_version_value}".encode("utf-8")
    ).hexdigest()
    return f"it-{digest[:20]}"
