# -*- coding: utf-8 -*-
"""内置背景图：程序化生成 → 幂等播种到 ``data/backgrounds/``，供设置面板一键选用。

**为什么是"生成"而不是随包发几张照片**：
- 仓库保持纯文本（无二进制大文件、无图片版权与体积问题），也不需要动 PyInstaller spec；
- 内置图与设计令牌同源（色板取自已有的 accents），换肤/换主题时不会有"图跟界面不搭"的错位；
- 生成只跑一次：文件已存在就不重写——用户手动替换/删除某张之后不会被每次启动覆盖回来
  （删掉的那张在下一次访问清单时补回来，这正是"内置"的含义）。

**目录口径**：``data_dir/backgrounds/``（与 uploads 同级、同属受管数据目录）：
- 落在 data_dir 内 → ``/api/file`` 的允许根已经覆盖它，不需要新开静态根；
- **不在** uploads 内 → 缓存统计与清理（``_image_cache_dirs`` 只认 uploads/generated）
  不会碰它：内置图绝不能被"清理缓存"删掉。
背景图路径校验（``config._validated_chat_background_image``）按同一口径放行这两个目录；
config 层不能 import storage，改口径时两处一起改（与 ``is_uploads_path`` 同一约定）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("naiba.backgrounds")

# 生成尺寸：1600×1000 在 1080p/2K 屏上按 cover 铺对话区够用，PNG 只有一两百 KB。
# 测试会把它改小以省时间，所以写成模块常量、函数里现取。
PRESET_SIZE = (1600, 1000)
# 缩略图上限像素（与 imaging.thumbnail_max_pixels 同量级；内置图只用于设置卡那一排小图）。
PRESET_THUMB_PIXELS = 45000
PRESET_FILE_SUFFIX = ".png"

# 6 张内置背景：4 张跟随四套皮肤的主色调，2 张中性（深/浅各一，配明暗主题都不违和）。
# stops = 竖向多段渐变的锚点 [(位置 0-1, RGB)]；glow = 叠加的柔光斑（位置为 0-1 相对坐标）。
BACKGROUND_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "violet-dawn",
        "name": "紫雾晨曦",
        "description": "紫藤色柔光，与默认皮肤同源",
        "stops": ((0.0, (42, 35, 80)), (0.55, (109, 90, 230)), (1.0, (169, 155, 255))),
        "glow": ((0.18, 0.16, (201, 188, 245), 0.42, 0.55), (0.86, 0.82, (58, 46, 110), 0.34, 0.6)),
    },
    {
        "id": "ocean-mist",
        "name": "海雾",
        "description": "冷调青蓝，适合长时间阅读",
        "stops": ((0.0, (18, 58, 90)), (0.5, (22, 138, 173)), (1.0, (127, 216, 232))),
        "glow": ((0.78, 0.2, (214, 240, 250), 0.4, 0.6), (0.2, 0.85, (12, 44, 70), 0.3, 0.55)),
    },
    {
        "id": "rose-quartz",
        "name": "玫瑰石英",
        "description": "暖粉调，柔和不刺眼",
        "stops": ((0.0, (74, 34, 51)), (0.5, (212, 93, 121)), (1.0, (245, 198, 208))),
        "glow": ((0.75, 0.18, (255, 226, 234), 0.4, 0.6), (0.22, 0.88, (66, 30, 45), 0.3, 0.55)),
    },
    {
        "id": "forest-fog",
        "name": "林间薄雾",
        "description": "低饱和绿，安静耐看",
        "stops": ((0.0, (30, 58, 43)), (0.55, (76, 149, 108)), (1.0, (168, 213, 186))),
        "glow": ((0.24, 0.2, (216, 241, 229), 0.36, 0.6), (0.85, 0.86, (26, 52, 38), 0.32, 0.6)),
    },
    {
        "id": "graphite-night",
        "name": "石墨夜色",
        "description": "近黑中性调，夜里不刺眼",
        "stops": ((0.0, (10, 12, 18)), (0.5, (29, 33, 48)), (1.0, (61, 69, 92))),
        "glow": ((0.72, 0.24, (109, 90, 230), 0.26, 0.7), (0.2, 0.8, (18, 21, 34), 0.3, 0.6)),
    },
    {
        "id": "paper-light",
        "name": "素纸",
        "description": "极浅中性色，白天最清爽",
        "stops": ((0.0, (244, 245, 242)), (0.5, (230, 232, 240)), (1.0, (255, 255, 255))),
        "glow": ((0.22, 0.18, (255, 255, 255), 0.55, 0.65), (0.8, 0.85, (210, 214, 227), 0.4, 0.6)),
    },
)


def backgrounds_dir(data_dir: Path) -> Path:
    """内置背景图目录（受管数据目录之一，跟随 data_dir 迁移）。"""
    return (Path(data_dir) / "backgrounds").resolve()


def is_backgrounds_path(data_dir: Path, raw_path: str | Path) -> bool:
    """raw_path 是否位于内置背景图目录内（旋转等"只读源图"操作的前置校验）。

    与 ``storage.media.is_uploads_path`` 同款语义（resolve 后判定 + 必须是文件）。
    """
    from naiba.core.paths import path_within

    try:
        resolved = Path(raw_path).expanduser().resolve()
    except (OSError, ValueError):
        return False
    return path_within(resolved, backgrounds_dir(data_dir)) and resolved.is_file()


def ensure_background_presets(data_dir: Path) -> list[dict[str, Any]]:
    """确保内置背景图（含缩略图）都在盘上，返回清单。

    幂等：主图/缩略图各自存在就跳过生成。单张生成失败只记日志并跳过那一张——
    宁可少一张可选背景，也不能让设置面板整体打不开。
    返回项：``{id, name, description, width, height, path, thumb_path}``（路径为绝对字符串）。
    """
    directory = backgrounds_dir(data_dir)
    presets: list[dict[str, Any]] = []
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("内置背景图目录创建失败（%s）：%s", directory, exc)
        return presets

    for spec in BACKGROUND_PRESETS:
        main = directory / f"{spec['id']}{PRESET_FILE_SUFFIX}"
        thumb = directory / f"{spec['id']}_thumb.webp"
        try:
            if not main.is_file():
                _render_preset(spec).save(main, format="PNG", optimize=True)
            if not thumb.is_file():
                _write_thumbnail(main, thumb)
        except (OSError, ValueError) as exc:
            logger.warning("内置背景图「%s」生成失败：%s", spec.get("name") or spec.get("id"), exc)
            continue
        if not main.is_file():
            continue
        presets.append({
            "id": str(spec["id"]),
            "name": str(spec["name"]),
            "description": str(spec.get("description") or ""),
            "width": PRESET_SIZE[0],
            "height": PRESET_SIZE[1],
            "path": str(main),
            # 缩略图缺失时回落主图：前端只认"有图就画"，不依赖缩略图一定存在。
            "thumb_path": str(thumb) if thumb.is_file() else str(main),
        })
    return presets


def _render_preset(spec: dict[str, Any]) -> Any:
    """按预设规格画一张背景图：竖向多段渐变 + 若干柔光斑（全部走 PIL 的 C 实现）。"""
    from PIL import Image, ImageDraw, ImageFilter

    size = (int(PRESET_SIZE[0]), int(PRESET_SIZE[1]))
    image = _vertical_gradient(size, spec.get("stops") or ())
    for glow in spec.get("glow") or ():
        image = _paste_glow(image, glow)
    return image


def _vertical_gradient(size: tuple[int, int], stops: tuple) -> Any:
    """多段竖向渐变：先在 1×256 的小图上打点再放大——避免逐像素的 Python 循环。"""
    from PIL import Image

    strip_height = 256
    strip = Image.new("RGB", (1, strip_height))
    pixels = strip.load()
    for y in range(strip_height):
        pixels[0, y] = _sample_stops(stops, y / (strip_height - 1))
    return strip.resize(size, Image.BICUBIC)


def _sample_stops(stops: tuple, ratio: float) -> tuple[int, int, int]:
    """在锚点之间做线性插值；锚点为空时回落到中性灰（不让异常图阻断生成）。"""
    if not stops:
        return (128, 128, 128)
    ordered = sorted(((float(pos), tuple(color)) for pos, color in stops), key=lambda item: item[0])
    if ratio <= ordered[0][0]:
        return ordered[0][1]  # type: ignore[return-value]
    if ratio >= ordered[-1][0]:
        return ordered[-1][1]  # type: ignore[return-value]
    for (left_pos, left_color), (right_pos, right_color) in zip(ordered, ordered[1:]):
        if left_pos <= ratio <= right_pos:
            span = right_pos - left_pos or 1.0
            weight = (ratio - left_pos) / span
            return tuple(  # type: ignore[return-value]
                round(left_color[i] + (right_color[i] - left_color[i]) * weight) for i in range(3)
            )
    return ordered[-1][1]  # type: ignore[return-value]


def _paste_glow(image: Any, glow: tuple) -> Any:
    """叠一团柔光：椭圆 → 高斯模糊 → 按 mask 合成（radius 取长边的比例，随图尺寸自然缩放）。"""
    from PIL import Image, ImageDraw, ImageFilter

    # 光斑比图更大，模糊后是"从画外照进来"的自然过渡，而不是一枚圆点。
    ratio_x, ratio_y, color, strength, spread = glow[:5]
    ratio_x, ratio_y, strength, spread = float(ratio_x), float(ratio_y), float(strength), float(spread)
    width, height = image.size
    radius = max(width, height) * spread
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).ellipse(
        [width * ratio_x - radius, height * ratio_y - radius,
         width * ratio_x + radius, height * ratio_y + radius],
        fill=round(255 * min(1.0, max(0.0, strength))),
    )
    mask = mask.filter(ImageFilter.GaussianBlur(radius * 0.45))
    layer = Image.new("RGB", (width, height), tuple(int(value) for value in color[:3]))
    image.paste(layer, mask=mask)
    return image


def _write_thumbnail(main: Path, thumb: Path) -> bool:
    """生成 WebP 缩略图（与上传管线同一套编码，命名沿用 ``<stem>_thumb.webp`` 约定）。"""
    from PIL import Image

    from naiba.storage.media import _encode_webp_thumb

    with Image.open(main) as image:
        image.load()
        payload = _encode_webp_thumb(image, PRESET_THUMB_PIXELS)
    if not payload:
        return False
    thumb.write_bytes(payload)
    return True
