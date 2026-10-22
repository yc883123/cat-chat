"""Studio 图片引用校验（上传反推 / 带图生成共用）。

安全边界（服务端权威，不信任客户端给的路径）：
- 只接受落在宿主 ``data_dir/uploads`` 受管缓存树内的文件；任意路径一律拒绝；
- 扩展名白名单 + 用 Pillow 真正解码一次（改名骗不过去）；
- 单张大小、总张数都有上限。

输出统一为结构化引用 ``{path, thumb_path, name}``，顺序即用户在多图弹窗里的
列表顺序（图1、图2……）。任务记录原样持久化这份列表，重试沿用相同图号。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from naiba.storage.media import is_uploads_path

# 与视觉编码层（naiba/vision/images.py::IMAGE_MEDIA_TYPES）保持可解码格式的交集。
ALLOWED_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 60 * 1024 * 1024
NAME_MAX = 200


class StudioImageError(ValueError):
    """图片引用非法（越权路径 / 不存在 / 非图片 / 超限）。"""


def _resolve_managed(data_dir: Path, raw: Any, label: str) -> Path:
    text = str(raw or "").strip()
    if not text:
        raise StudioImageError(f"{label}缺少文件路径")
    try:
        path = Path(text).expanduser().resolve()
    except (OSError, ValueError) as exc:
        raise StudioImageError(f"{label}路径无效：{exc}") from exc
    if not is_uploads_path(data_dir, path):
        raise StudioImageError(f"{label}不在受管上传目录内，已拒绝")
    return path


def _verify_image(path: Path, label: str) -> None:
    suffix = path.suffix.lower()
    if suffix not in ALLOWED_IMAGE_EXTS:
        raise StudioImageError(f"{label}格式不支持（只接受 PNG、JPG、WEBP、GIF）")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise StudioImageError(f"{label}读取失败：{exc}") from exc
    if size <= 0:
        raise StudioImageError(f"{label}是空文件")
    if size > MAX_IMAGE_BYTES:
        limit_mb = MAX_IMAGE_BYTES // (1024 * 1024)
        raise StudioImageError(f"{label}超过 {limit_mb}MB 上限")
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
    except ImportError as exc:  # pragma: no cover - 打包版必带 Pillow
        raise StudioImageError("图片校验组件不可用") from exc
    except (OSError, ValueError) as exc:
        raise StudioImageError(f"{label}不是可读取的图片：{exc}") from exc


def validate_image_refs(data_dir: Path, refs: Any) -> list[dict[str, str]]:
    """校验并规范化图片引用列表；顺序原样保留（= 图号顺序）。"""
    if refs is None:
        return []
    if not isinstance(refs, list):
        raise StudioImageError("images 必须是数组")
    if len(refs) > MAX_IMAGES:
        raise StudioImageError(f"最多支持 {MAX_IMAGES} 张图片")
    out: list[dict[str, str]] = []
    total_bytes = 0
    for index, ref in enumerate(refs, 1):
        label = f"图{index}"
        if isinstance(ref, str):
            raw_path = ref
            raw_thumb = ""
            raw_name = ""
        elif isinstance(ref, dict):
            raw_path = ref.get("path")
            raw_thumb = ref.get("thumb_path")
            raw_name = ref.get("name")
        else:
            raise StudioImageError(f"{label}引用结构非法")
        path = _resolve_managed(data_dir, raw_path, label)
        _verify_image(path, label)
        try:
            total_bytes += path.stat().st_size
        except OSError as exc:
            raise StudioImageError(f"{label}读取失败：{exc}") from exc
        if total_bytes > MAX_TOTAL_BYTES:
            limit_mb = MAX_TOTAL_BYTES // (1024 * 1024)
            raise StudioImageError(f"图片总大小超过 {limit_mb}MB 上限")
        thumb_path = ""
        if str(raw_thumb or "").strip():
            thumb = _resolve_managed(data_dir, raw_thumb, f"{label}缩略图")
            if thumb.suffix.lower() == ".webp":
                thumb_path = str(thumb)
        name = str(raw_name or path.name).strip()[:NAME_MAX] or path.name
        out.append({"path": str(path), "thumb_path": thumb_path, "name": name})
    return out


def referenced_upload_paths(task_records: list[dict[str, Any]]) -> set[str]:
    """任务记录里引用的图片路径（主图 + 缩略图），供缓存清理做引用保护。"""
    from naiba.core.paths import normalized_path_key

    keys: set[str] = set()
    for record in task_records:
        input_data = record.get("input") if isinstance(record, dict) else None
        if not isinstance(input_data, dict):
            continue
        for ref in input_data.get("images") or []:
            if not isinstance(ref, dict):
                continue
            for field in ("path", "thumb_path"):
                text = str(ref.get(field) or "").strip()
                if not text:
                    continue
                key = normalized_path_key(Path(text).expanduser())
                if key:
                    keys.add(key)
    return keys
