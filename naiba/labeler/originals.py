"""打标器原图留存：上传时把用户给的原始字节单独存一份，供训练包导出校验。

为什么需要：``store_uploaded_file`` 会按图片设置缩放/压缩后再落 uploads 缓存，
原始字节随请求结束即被丢弃——训练包要求「图片字节与用户上传原文件一致」，
必须有可校验的原图来源（打标器计划 §1.2 差异 1）。

方式与生命周期：
- 内容寻址：``data/labeler/originals/<sha256 前 2 位>/<sha256>``，同内容只存一份，
  原子写（临时文件 + os.replace）。样本身份不靠这里——同内容不同来源在任务里
  是两条独立样本（打标上传的缓存副本不做内容去重），这里只是一个字节池。
- 引用：原图 SHA-256 随上传响应返回，随 image_refs 进 task_items 与 annotations；
  导出训练包按摘要取字节，取不到就整包拒绝，绝不回退缩放后的缓存图。
- 清理：只删「没有任何标注/任务项引用、且落盘超过保护窗口」的原图
  （``prune_unreferenced``，启动恢复时调用）；刚上传还没提交任务的原图不受影响。
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any

# 原图清理保护窗口：刚上传、还没提交任务的原图在这段时间内一律不删。
PRUNE_MIN_AGE_SECONDS = 24 * 60 * 60
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def originals_root(data_dir: Path) -> Path:
    return Path(data_dir) / "labeler" / "originals"


def normalize_digest(raw: Any) -> str:
    """把外部传入的摘要归一成 64 位小写十六进制；非法一律返回空串。"""
    text = str(raw or "").strip().lower()
    return text if _DIGEST_RE.match(text) else ""


def archive_original(data_dir: Path, data: bytes) -> dict[str, Any]:
    """把原始字节存进内容寻址池；已存在时直接复用（幂等）。

    返回 ``{"sha256", "size", "path"}``；写盘失败抛 OSError 由调用方转成用户可读错误。
    """
    digest = hashlib.sha256(data).hexdigest()
    target = originals_root(data_dir) / digest[:2] / digest
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{digest}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, target)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return {"sha256": digest, "size": len(data), "path": str(target)}


def original_path(data_dir: Path, digest: Any) -> Path | None:
    """按摘要解析留存的原图文件；摘要非法或文件不存在返回 None。"""
    text = normalize_digest(digest)
    if not text:
        return None
    candidate = originals_root(data_dir) / text[:2] / text
    return candidate if candidate.is_file() else None


def prune_unreferenced(
    data_dir: Path,
    referenced: Any,
    *,
    min_age_seconds: int = PRUNE_MIN_AGE_SECONDS,
    now: float | None = None,
) -> dict[str, int]:
    """删除没有被任何标注/任务项引用、且超过保护窗口的原图。

    只删除文件名是合法摘要、且落在内容寻址结构里的文件——目录里出现其它形状的
    文件一律不动（宁多留不误删）。返回 ``{"removed", "kept", "freed"}``。
    """
    keep = {normalize_digest(item) for item in (referenced or ())}
    keep.discard("")
    root = originals_root(data_dir)
    if not root.is_dir():
        return {"removed": 0, "kept": 0, "freed": 0}
    current = time.time() if now is None else float(now)
    removed = kept = freed = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        digest = normalize_digest(path.name)
        if not digest or path.parent.name != digest[:2]:
            continue
        if digest in keep:
            kept += 1
            continue
        try:
            age = current - path.stat().st_mtime
            size = path.stat().st_size
        except OSError:
            kept += 1
            continue
        if age < min_age_seconds:
            kept += 1
            continue
        try:
            path.unlink()
            removed += 1
            freed += size
        except OSError:
            kept += 1
    return {"removed": removed, "kept": kept, "freed": freed}
