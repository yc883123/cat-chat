# -*- coding: utf-8 -*-
"""应用图标（托盘 + 桌面窗口）：上传归一化 → PNG/ICO 双文件落盘 → 启动期加载。

设计要点：
- **位置是 `app_dir` 而不是 `data_dir`**：图标属于应用外观（与 `app_dir/webview` 同性质），
  不是会话数据；用户迁移数据目录时不该把图标一起搬走，反向同理。
- 两份文件必须**成对**存在才算生效（`custom-icon.png` 给托盘/PIL 读，`custom-icon.ico` 给
  pywebview 窗口图标——Windows 只认 .ico）：只落一份就会出现"托盘换了、窗口没换"的半截状态，
  因此缺任一份一律按"没有自定义图标"处理（`has_custom_app_icon`）。写入也按"先写临时文件、
  两份都写成功再一起替换"的顺序，失败只留 .part 且会被清掉。
- 归一化 = **居中留白贴方形，不裁切**：用户传的不一定是方图，裁掉内容会被当成"上传错了"。
- **不改仓库默认 `icon.ico`**（§2.1）：本模块只负责"用户自定义覆盖层"，恢复默认 = 只删
  `custom-icon.*`，默认资产一笔都不动。
- 纯 IO + PIL，不依赖上层模块（storage 层可被 app/launcher 直接调用）。
"""
from __future__ import annotations

import io
from pathlib import Path

APP_ICON_PNG_NAME = "custom-icon.png"
APP_ICON_ICO_NAME = "custom-icon.ico"
# 单张上传上限（图标本身很小，5MB 足够覆盖手机直出照片的裁剪件）。
APP_ICON_MAX_BYTES = 5 * 1024 * 1024
# 短边下限：再小的图放到 256px 托盘/任务栏上只会是一团糊。
APP_ICON_MIN_EDGE = 64
# 归一化后的最大边长（同时是 ICO 里最大一帧的边长）。
APP_ICON_MAX_EDGE = 1024
# ICO 多尺寸档位：Windows 任务栏/托盘/资源管理器各取所需，缺档会让系统自己降采样。
APP_ICON_ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def app_icon_paths(app_dir: Path) -> tuple[Path, Path]:
    """返回 (PNG 路径, ICO 路径)；只算路径，不判断存在。"""
    root = Path(app_dir)
    return root / APP_ICON_PNG_NAME, root / APP_ICON_ICO_NAME


def has_custom_app_icon(app_dir: Path) -> bool:
    """自定义图标是否生效：两份文件都在**且非空**（零字节 = 上次写入半途失败，按没有处理）。"""
    png_path, ico_path = app_icon_paths(app_dir)
    for path in (png_path, ico_path):
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                return False
        except OSError:
            return False
    return True


def normalize_app_icon(raw: bytes) -> tuple[bytes, bytes]:
    """把上传图片归一化成方形图标，返回 (PNG 字节, ICO 字节)；不合法抛 ValueError。

    校验顺序：空文件 → 体积 → 可解码 → 短边 → 缩放 → 贴方形 → 编码。
    报错文案面向用户（http 层直接回给前端），不暴露堆栈。
    """
    if not raw:
        raise ValueError("图标文件为空")
    if len(raw) > APP_ICON_MAX_BYTES:
        raise ValueError(f"图片不能超过 {APP_ICON_MAX_BYTES // (1024 * 1024)} MB")
    try:
        from PIL import Image, ImageOps
    except Exception as exc:  # noqa: BLE001 - 依赖缺失要明确报错，不静默降级
        raise ValueError(f"缺少图像处理依赖，无法处理图标：{exc}") from exc
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as exc:  # noqa: BLE001 - 用户上传的坏图属于可预期错误
        raise ValueError("无法解析这张图片，请换一张常见格式（PNG / JPG / WebP）") from exc

    img = ImageOps.exif_transpose(img)
    if min(img.size) < APP_ICON_MIN_EDGE:
        raise ValueError(
            f"图片太小，至少 {APP_ICON_MIN_EDGE}×{APP_ICON_MIN_EDGE} 像素"
            f"（当前 {img.size[0]}×{img.size[1]}）"
        )
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    if max(img.size) > APP_ICON_MAX_EDGE:
        # thumbnail 就地等比缩到框内（不放大），保持长宽比。
        img = img.copy()
        img.thumbnail((APP_ICON_MAX_EDGE, APP_ICON_MAX_EDGE), Image.LANCZOS)

    side = max(img.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    # alpha_composite 而不是 paste(…, mask=img)：前者对半透明像素是标准的 over 合成，
    # 后者会按掩码通道直接拷贝，边缘半透明像素会变脏。
    canvas.alpha_composite(img, ((side - img.size[0]) // 2, (side - img.size[1]) // 2))

    png_buf = io.BytesIO()
    canvas.save(png_buf, format="PNG")
    ico_buf = io.BytesIO()
    canvas.save(ico_buf, format="ICO", sizes=[(size, size) for size in APP_ICON_ICO_SIZES])
    return png_buf.getvalue(), ico_buf.getvalue()


def store_app_icon(app_dir: Path, raw: bytes) -> None:
    """归一化并落盘两份文件；失败抛 ValueError（调用方转 400）。

    写入顺序：先写两个 `.part`（此时磁盘上还没有任何生效文件），全部成功后依次 replace。
    中途失败只清理临时文件并保留**原来那对**图标——"上传失败把已有图标弄丢"比"没换成"更难接受。
    """
    png_bytes, ico_bytes = normalize_app_icon(raw)
    png_path, ico_path = app_icon_paths(app_dir)
    tmp_png = png_path.with_name(png_path.name + ".part")
    tmp_ico = ico_path.with_name(ico_path.name + ".part")
    try:
        Path(app_dir).mkdir(parents=True, exist_ok=True)
        tmp_png.write_bytes(png_bytes)
        tmp_ico.write_bytes(ico_bytes)
        tmp_png.replace(png_path)
        tmp_ico.replace(ico_path)
    except OSError as exc:
        for tmp in (tmp_png, tmp_ico):
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        raise ValueError(f"图标保存失败：{exc}") from exc


def clear_app_icon(app_dir: Path) -> bool:
    """删除自定义图标（= 恢复默认）；返回是否真的删掉了文件。**不触碰内置 icon.ico**。"""
    removed = False
    for path in app_icon_paths(app_dir):
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(f"删除图标失败：{exc}") from exc
    return removed


def _read_png_if_valid(path: Path) -> bytes:
    """读一张图并确认可解码，返回它的 PNG 字节；打不开/不是图（含被外部改坏）返回空字节。

    原文件本身就是 PNG 时**原样返回**（不重编码）：既省一次编码，也让调用方能拿它跟
    磁盘字节逐字比对。预览接口用它来兜"自定义文件被手工改坏"的情形——坏文件不该被
    原样吐给浏览器变成破图，而应回退内置图标。
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return b""
    if not raw:
        return b""
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - 预览不是必需路径
        return b""
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            if img.format == "PNG":
                return raw
            buf = io.BytesIO()
            img.convert("RGBA").save(buf, format="PNG")
            return buf.getvalue()
    except (OSError, ValueError):
        return b""


def read_app_icon_png(app_dir: Path, resource_dir: Path) -> bytes:
    """返回当前生效图标的 PNG 字节（自定义优先，否则内置 icon.ico 转 PNG）；都没有则空字节。"""
    png_path, _ico_path = app_icon_paths(app_dir)
    if has_custom_app_icon(app_dir):
        data = _read_png_if_valid(png_path)
        if data:
            return data
    return _read_png_if_valid(Path(resource_dir) / "icon.ico")
