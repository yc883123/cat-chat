"""宿主图片缓存/缩略图处理与上传管理（原 server.py 图片缓存族）。

来源：server.py 的 _thumb_webp_path/_fit_image_pixels/_ensure_webp_thumb/_process_uploaded_image
（112-216）与 _image_cache_dirs/_uploads_total_bytes/_clean_uploads_cache（332-420）。
vision_runtime 的 _cache_folder_images 缓存写入逻辑随阶段 2 视觉模块拆分归位。

上传管理族（2026-09 上传系统优化）：
- store_uploaded_file：内容级去重 + 分日目录落盘 + 图片压缩/缩略图（幂等、原子）；
- remove_uploaded_file：安全删除（uploads 内 + 未被消息/快照引用）；
- auto_clean_uploads：上传后超限自动清理（阈值与手动清理分离，避免频繁误清）。

设计约束：所有函数显式接收 data_dir / imaging 参数，不读取任何模块级全局
（原 _ensure_webp_thumb 经 APP.config 取 imaging 配置，现改为参数注入；
原 *目录族经模块级 DATA_DIR 取目录，现改为 data_dir 参数）。
"""

from __future__ import annotations

import hashlib
import io
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from naiba.core.media_types import IMAGE_PROCESS_EXTS
from naiba.core.paths import path_within


# 上传压缩/缩略图支持的图片格式：唯一定义在 core/media_types.py
# （GIF 保持原图与动画、不压缩；但同样生成首帧静态缩略图，见 THUMB_SOURCE_SUFFIXES）。
IMAGE_SUFFIXES = set(IMAGE_PROCESS_EXTS)
# 可生成缩略图的来源格式：静止图（含压缩）+ GIF（取首帧）。
# GIF 若不出缩略图，前端按"<主图 stem>_thumb.webp"推导必然 404（破图）。
THUMB_SOURCE_SUFFIXES = set(IMAGE_PROCESS_EXTS) | {".gif"}


# 上传上限：与 app._upload 时代一致的 80MB（multipart 流式也在此拦截）。
UPLOAD_MAX_BYTES = 80 * 1024 * 1024
# 上传完成后的自动清理阈值：比手动清理（128MB）宽松，避免频繁误清用户近期引用。
UPLOAD_AUTO_CLEAN_LIMIT = 256 * 1024 * 1024
# 自动清理的保护窗口（秒）：最近这段时间内落盘/改动过的缓存组一律不参与自动删除。
# 起因（用户报障：附件区破图 / 点击无法放大 / 模型 vision_analyze 报"未找到图片文件"）：
# 被引用缓存（uploads + generated）本身已超阈值时，"降到上限"这个目标永远不可达，
# 自动清理会退化成"删光所有未引用组"——其中就包括刚上传、还在输入框待发
# （尚未落库，upload_path_referenced 必然为 False）的那个附件。
# 窗口同时覆盖"工具刚产出、消息 metadata 还没写回"的临时态；手动清理不适用本窗口。
UPLOAD_CLEAN_GRACE_SECONDS = 15 * 60
# 上传目标文件名清洗（保留字母数字、点、横线、下划线、中文）。
_UPLOAD_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]")


def upload_target_dir(data_dir: Path, when: datetime | None = None) -> Path:
    """上传落盘目录：data/uploads/YYYY-MM-DD/（按日分桶，历史根目录文件保持可引用）。"""
    day = (when or datetime.now()).strftime("%Y-%m-%d")
    return (data_dir / "uploads" / day).resolve()


def store_uploaded_file(
    data: bytes,
    filename: str,
    data_dir: Path,
    imaging: dict[str, Any] | None = None,
) -> dict[str, str | int]:
    """存储一次上传：图片处理 → 内容级去重 → 分日目录原子落盘。

    幂等语义：相同内容（处理后字节）已存在于 uploads 时，不重复写盘，
    直接返回既有文件信息（同内容不同来源复用同一份缓存）。

    返回 {name, path, size, thumb_path, deduped}（deduped=True 表示命中既有缓存）。
    """
    data_dir = data_dir.resolve()
    uploads_root = (data_dir / "uploads").resolve()
    imaging = dict(imaging or {})
    name = Path(str(filename or "upload.bin")).name
    safe_name = _UPLOAD_SAFE_NAME_RE.sub("_", name) or "upload.bin"
    main_bytes, thumb_name, thumb_bytes = _process_uploaded_image(data, name, imaging)
    digest = hashlib.sha256(main_bytes).hexdigest()
    # 内容级去重：同大小候选再比内容哈希（避免全目录哈希开销）。
    existing = _find_duplicate(uploads_root, len(main_bytes), digest)
    if existing is not None:
        return {
            "name": existing.name,
            "path": str(existing),
            "size": existing.stat().st_size,
            # 缩略图按"主图 stem + _thumb.webp"推导；不存在时返回空串（前端回退主图）。
            "thumb_path": _thumb_path_for(existing),
            "deduped": True,
        }
    target_dir = upload_target_dir(data_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"naiba_chat_{int(time.time())}_{secrets_hex(3)}_{safe_name}"
    target.write_bytes(main_bytes)
    thumb_path = ""
    if thumb_name and thumb_bytes:
        # 缩略图必须与主图同 stem（<主图 stem>_thumb.webp）：前端兜底、去重复用（_thumb_path_for）
        # 与删除成组（remove_uploaded_file）都按这一约定推导；用原始文件名会导致三处全部找不到。
        thumb_file = target_dir / f"{target.stem}_thumb.webp"
        thumb_file.write_bytes(thumb_bytes)
        thumb_path = str(thumb_file)
    return {
        "name": target.name,
        "path": str(target),
        "size": len(main_bytes),
        "thumb_path": thumb_path,
        "deduped": False,
    }


def rotate_uploaded_image(
    source: Path,
    data_dir: Path,
    imaging: dict[str, Any] | None = None,
    turns: int = 1,
) -> dict[str, Any]:
    """把图片顺时针旋转 90°×turns，另存成 uploads 里的新文件并返回其记录。

    **为什么落到文件、而不是在每个渲染路径叠一层 transform**：旋转的正解就是把竖图变成横图
    ——改完之后它就是一张普通横图，取景、模糊填充、缩略图、内置/自定义背景、缓存回收全都自动
    成立；若改成视图态旋转，`.chat-bg-surface` 就得再分出"旋转/未旋转"两套几何，正好破坏
    「渲染单一事实来源」（见维护说明 §九.70）。

    复用 ``store_uploaded_file``：分日目录、内容级去重、压缩、缩略图生成一次到位；
    统一输出 PNG（无损，避免 JPEG 二次压缩掉画质），文件名为 ``<原名>_rot<角度>.png``。
    """
    from PIL import Image, ImageOps

    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise ValueError("图片文件不存在，无法旋转")
    try:
        with Image.open(path) as probe:
            probe.load()
            # 上传管线对 png/jpg/webp 已经做过 exif_transpose；这里再兜一次是为了
            # .gif/.bmp/.avif 这些"原样落盘"的格式（EXIF 方向不能被忽略两次）。
            image = ImageOps.exif_transpose(probe)
            if image is None:  # 极老版本 Pillow 的返回值防御
                image = probe.copy()
            else:
                image = image.copy()
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - 坏图一律按"无法识别"报错，不吞
        raise ValueError(f"无法识别的图片：{exc}") from None

    steps = int(turns) % 4
    for _ in range(steps):
        image = image.transpose(Image.Transpose.ROTATE_270)   # PIL 的 270 = 顺时针 90°
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    name = f"{path.stem}_rot{steps * 90}.png"
    return store_uploaded_file(buffer.getvalue(), name, data_dir, imaging)


def _thumb_path_for(main_path: Path) -> str:
    """既有图片的缩略图路径（可能与主图分日同桶；不存在时返回空串）。"""
    thumb = _thumb_webp_path(main_path)
    if thumb.is_file() and thumb.stat().st_size > 0:
        return str(thumb)
    # 兼容旧结构：uploads 根目录的 <stem>_thumb.webp。
    legacy = main_path.parent.parent / f"{main_path.stem}_thumb.webp"
    if legacy.is_file() and legacy.stat().st_size > 0:
        return str(legacy)
    return ""


def _find_duplicate(uploads_root: Path, size: int, digest: str) -> Path | None:
    """在 uploads 树内查找同大小且内容哈希一致的文件（内容级去重）。"""
    if not uploads_root.is_dir():
        return None
    candidates: list[Path] = []
    for path in uploads_root.rglob("*"):
        if not path.is_file() or path.name.endswith(".part"):
            continue
        try:
            if path.stat().st_size == size:
                candidates.append(path)
        except OSError:
            continue
    for path in candidates:
        try:
            with path.open("rb") as handle:
                chunk = hashlib.sha256()
                while True:
                    block = handle.read(65536)
                    if not block:
                        break
                    chunk.update(block)
                if chunk.hexdigest() == digest:
                    return path
        except OSError:
            continue
    return None


def resolve_attachment_file(data_dir: Path, raw_path: str | Path) -> Path | None:
    """把附件记录里的本地路径解析为真实文件；不存在返回 None。

    与 ``/api/file``（http.py ``_serve_local_file``）同口径：先按记录里的路径找，
    失效时再按文件名在 ``data/uploads`` 下兜底（先根目录、再分日目录递归）——
    数据目录迁移过的旧记录只有文件名仍然有效。只做存在性判断，不读内容。
    """
    text = str(raw_path or "").strip()
    if not text:
        return None
    try:
        candidate = Path(text).expanduser()
        resolved = candidate.resolve()
    except (OSError, ValueError):
        return None
    if resolved.is_file():
        return resolved
    name = candidate.name or resolved.name
    if not name:
        return None
    uploads_root = (Path(data_dir) / "uploads").resolve()
    direct = uploads_root / name
    try:
        if direct.is_file():
            return direct
        if uploads_root.is_dir():
            for found in uploads_root.rglob(name):
                if found.is_file():
                    return found
    except OSError:
        return None
    return None


def is_uploads_path(data_dir: Path, raw_path: str | Path) -> bool:
    """raw_path 是否位于宿主 uploads 缓存树内（安全删除/清理前置校验）。"""
    try:
        resolved = Path(raw_path).expanduser().resolve()
    except (OSError, ValueError):
        return False
    root = (data_dir / "uploads").resolve()
    return path_within(resolved, root) and resolved.is_file()


def remove_uploaded_file(data_dir: Path, raw_path: str | Path) -> bool:
    """安全删除一个没有被引用的上传文件（主图+缩略图成组删除）。

    仅在 uploads 树内且未被引用时删除；有引用则返回 False（调用方应收起删除）。
    引用检查在当前实现中由调用方（app 层）负责传递 referenced=True/False，
    本函数只做物理删除与树内校验。
    """
    if not is_uploads_path(data_dir, raw_path):
        raise ValueError("只允许删除宿主 uploads 缓存目录内的文件")
    target = Path(raw_path).expanduser().resolve()
    removal: list[Path] = [target]
    thumb = _thumb_webp_path(target)
    if thumb.is_file():
        removal.append(thumb)
    legacy_thumb = target.parent.parent / f"{target.stem}_thumb.webp"
    if legacy_thumb.is_file() and legacy_thumb not in removal:
        removal.append(legacy_thumb)
    removed, freed = 0, 0
    for path in removal:
        try:
            size = path.stat().st_size
            path.unlink()
            removed += 1
            freed += size
            # 目录若空则顺手清理（不报错）。
            try:
                path.parent.rmdir()
            except OSError:
                pass
        except OSError:
            continue
    return removed > 0


def auto_clean_uploads(
    data_dir: Path,
    limit: int = UPLOAD_AUTO_CLEAN_LIMIT,
    referenced_checker: Callable[[Path], bool] | None = None,
    protect_paths: Iterable[str | Path] | None = None,
) -> dict[str, Any] | None:
    """上传后超限自动清理：仅超过 limit 时触发；默认带引用保护（B1）。

    referenced_checker 提供时只删未被引用的组（历史消息引用永久保留），
    未提供时退化为手动清理的按时间保留语义（调用方应始终提供）。
    protect_paths 是本次调用必须无条件保留的路径（上传入口传本次刚落盘的主图/缩略图）——
    与 ``UPLOAD_CLEAN_GRACE_SECONDS`` 保护窗口互为双保险。
    """
    try:
        total = _uploads_total_bytes(data_dir)
    except OSError:
        return None
    if total <= limit:
        return None
    result = _clean_uploads_cache(
        limit=limit,
        data_dir=data_dir,
        referenced_checker=referenced_checker,
        protect_paths=protect_paths,
    )
    result["trigger"] = "auto"
    return result


def secrets_hex(nbytes: int) -> str:
    """安全随机十六进制串（默认 secrets 不可用时降级 uuid4——标准库必可用）。"""
    import secrets

    return secrets.token_hex(nbytes)


def _thumb_webp_path(main_path: Path) -> Path:
    """Given a cached main image path, derive the WebP thumbnail path."""
    return main_path.with_name(main_path.stem + "_thumb.webp")


def _safe_resolve(path: Path) -> Path | None:
    """尽力解析为绝对路径；失败返回 None（清理时的路径比较用，不抛错）。"""
    try:
        return path.expanduser().resolve()
    except (OSError, ValueError):
        return None


def _fit_image_pixels(img: Any, max_pixels: int) -> Any:
    """Scale ``img`` down with Lanczos so width*height <= max_pixels."""
    from PIL import Image

    width, height = img.width, img.height
    if width * height <= max_pixels:
        return img.copy()
    ratio = (max_pixels / (width * height)) ** 0.5
    nw = max(1, int(width * ratio))
    nh = max(1, int(height * ratio))
    return img.resize((nw, nh), Image.LANCZOS)


def _ensure_webp_thumb(main_path: Path, imaging: dict[str, Any] | None = None) -> str:
    """Generate a ``<stem>_thumb.webp`` next to ``main_path`` if missing.

    Best-effort: returns the thumb path on success, else ``""`` so the caller can
    fall back (e.g. to the main image). Used by generated-media caching so every
    ComfyUI image has a served thumbnail in the history. GIF 取首帧（原图动画不动）。
    """
    try:
        from PIL import Image, ImageOps

        if main_path.suffix.lower() not in THUMB_SOURCE_SUFFIXES:
            return ""
        if not main_path.is_file():
            return ""
        thumb_path = _thumb_webp_path(main_path)
        if thumb_path.is_file() and thumb_path.stat().st_size > 0:
            return str(thumb_path)
        img = Image.open(main_path)
        img.load()
        img = ImageOps.exif_transpose(img)
        imaging = dict(imaging or {})
        thumb_px = max(1, int(imaging.get("thumbnail_max_pixels", 500000) or 500000))
        thumb_bytes = _encode_webp_thumb(img, thumb_px)
        if not thumb_bytes:
            return ""
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        thumb_path.write_bytes(thumb_bytes)
        return str(thumb_path)
    except Exception:  # noqa: BLE001 - thumbnail is best-effort
        return ""


def _encode_webp_thumb(img: Any, thumb_px: int) -> bytes:
    """把 PIL 图像缩到 thumb_px 并编码为 WebP 字节；失败返回空字节（best-effort）。"""
    try:
        thumb_img = _fit_image_pixels(img, thumb_px)
        buf = io.BytesIO()
        out = thumb_img.convert("RGBA") if thumb_img.mode in ("P", "RGBA") else thumb_img
        out.save(buf, format="WEBP", quality=82)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - thumbnail is best-effort
        return b""


def _process_uploaded_image(
    data: bytes, filename: str, imaging: dict[str, Any]
) -> tuple[bytes, str | None, bytes]:
    """Optionally compress an image and always emit a WebP thumbnail.

    Returns ``(main_bytes, thumb_filename, thumb_bytes)``. Non-image formats are
    passed through untouched with no thumbnail. GIF keeps its original bytes
    (animation preserved) but still gets a **first-frame** WebP thumbnail —
    otherwise the frontend's ``<stem>_thumb.webp`` fallback 404s (broken image).
    Compression keeps the source format and preserves alpha; thumbnails are always WebP.
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in THUMB_SOURCE_SUFFIXES:
        return data, None, b""
    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        fmt = (img.format or "").upper()
    except Exception:  # noqa: BLE001 - malformed image -> keep original bytes
        return data, None, b""

    thumb_px = max(1, int(imaging.get("thumbnail_max_pixels", 500000) or 500000))
    if fmt == "GIF":
        thumb_bytes = _encode_webp_thumb(img, thumb_px)
        if not thumb_bytes:
            return data, None, b""
        return data, Path(filename).stem + "_thumb.webp", thumb_bytes

    img = ImageOps.exif_transpose(img)
    original = bool(imaging.get("image_upload_original", False))
    max_px = max(1, int(imaging.get("image_max_pixels", 2000000) or 2000000))

    main_bytes = data
    if not original and img.width * img.height > max_px:
        img = _fit_image_pixels(img, max_px)
        try:
            buf = io.BytesIO()
            out_fmt = fmt if fmt in {"PNG", "JPEG", "WEBP"} else "PNG"
            save_img = img
            if out_fmt == "JPEG" and save_img.mode not in ("RGB", "L"):
                save_img = save_img.convert("RGB")
            save_img.save(buf, format=out_fmt)
            main_bytes = buf.getvalue()
        except Exception:  # noqa: BLE001 - fall back to original bytes
            main_bytes = data

    thumb_bytes = _encode_webp_thumb(img, thumb_px)
    if not thumb_bytes:
        return main_bytes, None, b""
    return main_bytes, Path(filename).stem + "_thumb.webp", thumb_bytes


IMAGE_CACHE_CLEAN_LIMIT = 128 * 1024 * 1024  # 128 MB


def _image_cache_dirs(data_dir: Path) -> list[Path]:
    """返回宿主图片缓存的两个目录：用户上传/视觉缓存（uploads）与生成产物缓存（generated）。"""
    return [(data_dir / "uploads").resolve(), (data_dir / "generated").resolve()]


def missing_cache_attachment(data_dir: Path, raw_path: str | Path) -> bool:
    """宿主托管缓存（uploads / generated）里的附件是否已不在磁盘上。

    只对**缓存树内**的路径判定，其余一律返回 False：URL 由 /api/file 代理；工作区文件、
    可移动盘/网络盘上的文件不属于宿主缓存管理范围，瞬时读不到不该拦下用户发送
    （交给工具层如实报错）。缓存树内的文件由清理机制管理，丢失即模型必然读不到
    （vision_analyze 报"未找到图片文件"），必须在发送前拦下并提示重新上传。
    """
    text = str(raw_path or "").strip()
    if not text or text.lower().startswith(("http://", "https://")):
        return False
    resolved = _safe_resolve(Path(text))
    if resolved is None:
        return False
    try:
        in_cache_tree = any(path_within(resolved, root) for root in _image_cache_dirs(Path(data_dir)))
    except (OSError, ValueError):
        return False
    if not in_cache_tree:
        return False
    return resolve_attachment_file(data_dir, text) is None


def _uploads_total_bytes(data_dir: Path) -> int:
    """Total size of all cached images (uploads + generated, main + thumbnails)."""
    total = 0
    for cache_dir in _image_cache_dirs(data_dir):
        if not cache_dir.is_dir():
            continue
        for path in cache_dir.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
    return total


def _clean_uploads_cache(
    limit: int = IMAGE_CACHE_CLEAN_LIMIT,
    data_dir: Path | None = None,
    referenced_checker: Callable[[Path], bool] | None = None,
    protect_paths: Iterable[str | Path] | None = None,
    grace_seconds: int = UPLOAD_CLEAN_GRACE_SECONDS,
) -> dict[str, Any]:
    """清理旧图片缓存（uploads + generated）。

    ``referenced_checker=None``（手动清理，设置页按钮）：按组（主图+缩略图）× 时间戳
    从新到旧，保留总大小不超过 limit 的最新的（旧行为，**不感知引用、也不受保护窗口约束**）；
    ``referenced_checker`` 提供（自动清理 B1）：从最旧开始逐组删除**未被引用**的组
    （checker 返回 True=被消息/快照引用，永久保留），直到剩余 ≤ limit；引用文件
    过多时允许超限（宁可缓存超限也不删用户历史引用的图）。自动清理另有两道保护：
    - ``grace_seconds`` 保护窗口：最近落盘/改动的组一律不删（刚上传待发送、工具刚产出）；
    - ``protect_paths``：本次必须无条件保留的具体路径（上传入口传刚落盘的文件）。

    返回 {removed: 删除文件数, freed: 释放字节数, size: 清理后剩余字节数,
    skipped_recent: 因保护窗口跳过的组数, skipped_protected: 因 protect_paths 跳过的组数}。
    """
    if data_dir is None:
        raise ValueError("data_dir 必须显式传入")
    cache_dirs = [d for d in _image_cache_dirs(data_dir) if d.is_dir()]
    if not cache_dirs:
        return {"removed": 0, "freed": 0, "size": 0, "skipped_recent": 0, "skipped_protected": 0}
    # 以"主图 + 其缩略图"成组（主图名 X.ext 与其缩略图 X_thumb.webp 归为一组）。
    # 组键 = "目录名/相对路径"（相对 cache_dir，含分日子目录），
    # 避免不同日期/不同目录下同名前缀被合并（上传分日目录 2026-09 起）。
    groups: dict[str, list[Path]] = {}
    for cache_dir in cache_dirs:
        for path in cache_dir.rglob("*"):
            if not path.is_file() or path.name.endswith(".part"):
                continue
            try:
                rel = path.relative_to(cache_dir).as_posix()
            except ValueError:
                continue
            name = path.name
            if name.endswith("_thumb.webp"):
                key = rel[: -len("_thumb.webp")]
            else:
                key = rel[: -len(path.suffix)] if path.suffix else rel
            groups.setdefault(f"{cache_dir.name}/{key}", []).append(path)

    def _group_mtime(paths: list[Path]) -> int:
        latest = 0
        for p in paths:
            try:
                latest = max(latest, int(p.stat().st_mtime))
            except OSError:
                continue
        return latest

    def _group_size(paths: list[Path]) -> int:
        total = 0
        for p in paths:
            try:
                total += p.stat().st_size
            except OSError:
                continue
        return total

    entries: list[tuple[int, str, list[Path]]] = [
        (_group_mtime(paths), key, paths) for key, paths in groups.items()
    ]
    entries.sort(key=lambda item: item[0], reverse=True)  # 新 -> 旧

    if referenced_checker is not None:
        # B1 自动清理：从最旧开始删未引用组，直到 ≤ limit；引用组永远保留。
        # 保护窗口 + protect_paths 见函数文档：被引用缓存本身超 limit 时"降到上限"
        # 不可达，没有这两道保护就会退化成"删光所有未引用组"（用户报障：刚上传、
        # 还没发送的附件被当场删掉 → 附件区破图 / 点击无法放大 / 模型读不到图）。
        protected: set[Path] = set()
        for raw in protect_paths or ():
            if not str(raw or "").strip():
                continue
            resolved_protect = _safe_resolve(Path(str(raw)))
            if resolved_protect is not None:
                protected.add(resolved_protect)
        now_ts = int(time.time())
        remaining = sum(_group_size(paths) for _, _, paths in entries)
        removed = 0
        freed = 0
        skipped_recent = 0
        skipped_protected = 0
        for mtime, key, paths in sorted(entries, key=lambda item: item[0]):  # 旧 -> 新
            if remaining <= limit:
                break
            if protected and any(_safe_resolve(p) in protected for p in paths):
                skipped_protected += 1
                continue
            if grace_seconds > 0 and (now_ts - mtime) < grace_seconds:
                skipped_recent += 1
                continue
            main_file = next(
                (p for p in paths if not p.name.endswith("_thumb.webp")), paths[0]
            )
            try:
                if referenced_checker(main_file):
                    continue  # 被消息/快照引用：永久保留（允许超限）
            except (OSError, ValueError):
                continue
            group_size = _group_size(paths)
            for p in paths:
                try:
                    size = p.stat().st_size
                    p.unlink()
                    removed += 1
                    freed += size
                except OSError:
                    continue
            remaining -= group_size
        return {
            "removed": removed,
            "freed": freed,
            "size": _uploads_total_bytes(data_dir),
            "skipped_recent": skipped_recent,
            "skipped_protected": skipped_protected,
        }

    kept_keys: set[str] = set()
    kept_size = 0
    for mtime, key, paths in entries:
        group_size = _group_size(paths)
        if kept_size + group_size <= limit:
            kept_size += group_size
            kept_keys.add(key)
    removed = 0
    freed = 0
    for mtime, key, paths in entries:
        if key in kept_keys:
            continue
        for p in paths:
            try:
                size = p.stat().st_size
                p.unlink()
                removed += 1
                freed += size
            except OSError:
                continue
    # 手动清理不做保护（用户显式操作，语义即"按时间保留最短"）：跳过计数恒为 0。
    return {
        "removed": removed,
        "freed": freed,
        "size": _uploads_total_bytes(data_dir),
        "skipped_recent": 0,
        "skipped_protected": 0,
    }
