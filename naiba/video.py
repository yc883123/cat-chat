# -*- coding: utf-8 -*-
"""视频服务：元信息读取（probe）与抽帧（times / interval / keyframes + 联系表）。

设计约束（与 storage.media / naiba.pdf 同构）：所有函数显式接收 data_dir（宿主数据根目录），
不读取模块级全局；缓存一律写入宿主托管的
``<data_dir>/uploads/video_frames/<video_sha16>/<params_key>/``，与用户上传/缩略图
同一清理与统计体系（/api/imaging/clean / _uploads_total_bytes 覆盖整棵 uploads 树）。

工具职责：本模块只做解码与缓存产物，schema/策略绑定见 tools/providers/video.py。

依赖纪律（与 pdf.py 的教训同构，维护说明 §九「缺依赖 → 测试全 error」）：
``av``（PyAV）**顶层不 import**——它是最重的可选依赖，顶层导入会让"缺依赖"变成
"整组测试导入期 error"。改为函数内惰性导入，缺依赖时抛明确业务文案（见 `_load_av`）。

上限口径（评审 B4：两个常量缺一不可）：
- 单参数目录 ≤ VIDEO_CACHE_MAX_IMAGES=200 张（抄 PDF_CACHE_MAX_IMAGES 的口型）；
- 同一视频跨参数目录合计 ≤ VIDEO_CACHE_MAX_PER_VIDEO=600 张
  —— 因为**参数一变就是新目录**（同一视频换 times 试十次 = 十个目录），
  只按目录计数拦不住总膨胀。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from naiba.core.media_types import MEDIA_EXTS_BY_KIND
from naiba.core.paths import path_within  # noqa: F401  (re-export for callers)

VIDEO_PROBE_TIMEOUT = 30          # 秒，单次打开容器/读流信息
VIDEO_FRAME_DEFAULT_MAX = 8       # 单次抽帧默认上限（与 MEDIA_BUCKET_LIMITS.video 同口径）
VIDEO_FRAME_HARD_MAX = 12         # 单次抽帧硬上限（模型显式要求时）
VIDEO_FRAME_MAX_EDGE = 1600       # 单帧长边上限（与 PDF_RENDER_MAX_EDGE 同口径）
VIDEO_FRAME_MIN_EDGE = 320
VIDEO_KEYFRAME_THRESHOLD = 0.3    # 默认场景切换灵敏度（平均绝对差，非 ffmpeg scene score）
VIDEO_KEYFRAME_MIN_GAP = 0.5      # 关键帧最小间隔（秒），防连发
VIDEO_KEYFRAME_PROBE_FPS = 5.0    # 关键帧候选采样率
VIDEO_CACHE_MAX_IMAGES = 200      # 【口径】单个参数目录内的帧图上限
VIDEO_CACHE_MAX_PER_VIDEO = 600   # 【口径】同一视频（按路径+size+mtime hash）跨参数目录的合计上限
VIDEO_CACHE_REL = "uploads/video_frames"
VIDEO_SHEET_SLOT_WIDTH = 320      # 联系表每格宽度
VIDEO_SHEET_MAX_EDGE = 1600       # 联系表长边上限
VIDEO_SHEET_GAP = 6
VIDEO_SHEET_LABEL_HEIGHT = 18

# 视频扩展名：唯一定义在 core/media_types.py（不另立名单）
VIDEO_EXTS: tuple[str, ...] = tuple(MEDIA_EXTS_BY_KIND["video"])

MODES: tuple[str, ...] = ("times", "interval", "keyframes")

_AV_HINT = (
    "视频解码依赖未安装（av）；冻结版请重装最新版，"
    "源码模式执行 .venv\\Scripts\\python.exe -m pip install av"
)


def _load_av() -> Any:
    """惰性导入 PyAV；缺失/损坏时抛**明确业务文案**（不是裸 ImportError）。"""
    try:
        import av  # noqa: PLC0415 - 惰性导入是本模块的硬要求（见模块 docstring）
    except ImportError as exc:  # 含 DLL load failed（同为 ImportError）
        raise ValueError(_AV_HINT) from exc
    return av


def _av_error_types(av: Any) -> tuple[type, ...]:
    """PyAV 的解码异常类。

    关键坑：``av.error.FFmpegError`` 在 PyAV 9+ 的继承链是
    ``FFmpegError → ValueError``（**不是** OSError）——所以"先 ``except ValueError: raise``
    放行业务错误"的写法会把 PyAV 的原始报错（含 Errno 与裸路径）原样漏给模型。
    必须先捕获这几类，再放行业务 ValueError。
    """
    found: list[type] = []
    for scope in (getattr(av, "error", None), av):
        for name in ("FFmpegError", "AVError", "InvalidDataError"):
            candidate = getattr(scope, name, None)
            if isinstance(candidate, type) and candidate not in found:
                found.append(candidate)
    return tuple(found) or (OSError,)


def _decode_error(exc: Exception) -> ValueError:
    return ValueError(f"无法解析视频（文件已损坏或不是有效视频）：{type(exc).__name__}: {exc}")


# ---- 校验与归一 ----

def _ensure_video_file(video_path: Path) -> Path:
    path = Path(video_path).expanduser()
    if not path.is_file():
        raise ValueError(f"视频文件不存在：{path}")
    if path.suffix.lower() not in VIDEO_EXTS:
        raise ValueError(
            f"不是支持的视频文件（扩展名 {path.suffix or '（无）'}；"
            f"支持 {'、'.join(VIDEO_EXTS)}）：{path}"
        )
    return path


def _float_param(value: Any, default: float, label: str) -> float:
    if value in (None, ""):
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{label} 必须是数字")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须是数字") from exc


def _int_param(value: Any, default: int, label: str) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label} 必须是整数")
    try:
        return int(float(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须是整数") from exc


def _parse_times(value: Any) -> list[float]:
    """解析时间点参数："3,15,42.5" / [3, 15] / 3 → 升序去重的时间列表（秒）。"""
    if value in (None, ""):
        return []
    raw: list[Any]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        raw = [value]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raw = [part for part in re.split(r"[,，;；\s]+", str(value)) if part]
    times: list[float] = []
    for item in raw:
        try:
            seconds = float(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"times 含非数字：{item!r}") from exc
        if seconds < 0:
            raise ValueError(f"times 不能为负数：{seconds}")
        if seconds not in times:
            times.append(seconds)
    times.sort()
    return times


def _clamp_int(value: Any, default: int, low: int, high: int, label: str) -> int:
    number = _int_param(value, default, label)
    if number < low:
        raise ValueError(f"{label} 必须 ≥ {low}（收到 {number}）")
    if number > high:
        raise ValueError(f"{label} 必须 ≤ {high}（收到 {number}）")
    return number


# ---- 缓存键与目录（与 PDF 的关键差异：不做全文件哈希）----

def _video_cache_key(video_path: Path) -> str:
    """按「绝对路径 + 大小 + mtime_ns」键控：视频动辄几 GB，读全文哈希会拖慢每次调用。

    文件真正被替换时 mtime/size 变化即失效；同名同大小同 mtime 但内容不同属可忽略的
    碰撞，最坏后果只是"复用了旧帧"，不产生错误行为（维护说明 §九.24 同口径：宁可保守）。
    """
    resolved = video_path.resolve()
    try:
        stat = resolved.stat()
        stamp = f"{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        stamp = "0:0"
    digest = hashlib.sha256(f"{resolved}|{stamp}".encode("utf-8")).hexdigest()
    return digest[:16]


def _params_key(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _video_root_dir(data_dir: Path, video_path: Path) -> Path:
    return (Path(data_dir) / VIDEO_CACHE_REL).resolve() / _video_cache_key(video_path)


def _video_cache_dir(data_dir: Path, video_path: Path, params_key: str) -> Path:
    return _video_root_dir(data_dir, video_path) / params_key


def _count_png(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(1 for item in directory.rglob("*") if item.is_file() and item.suffix.lower() == ".png")


def _manifest_path(cache_dir: Path) -> Path:
    return cache_dir / "frames.json"


# ---- 解码辅助 ----

def _stream_time_base(stream: Any) -> Any:
    return getattr(stream, "time_base", None)


def _frame_seconds(frame: Any, time_base: Any) -> float | None:
    pts = getattr(frame, "pts", None)
    if pts is None or time_base is None:
        return None
    return float(pts * time_base)


def _stream_duration(container: Any, stream: Any) -> float:
    """时长（秒）：优先视频流 duration×time_base，退回容器 duration（微秒）。"""
    time_base = _stream_time_base(stream)
    duration = getattr(stream, "duration", None)
    if duration is not None and time_base is not None:
        try:
            value = float(duration * time_base)
            if value > 0:
                return value
        except (TypeError, ValueError, OverflowError):
            pass
    container_duration = getattr(container, "duration", None)
    if container_duration:
        try:
            return float(container_duration) / 1_000_000
        except (TypeError, ValueError, OverflowError):
            return 0.0
    return 0.0


def _decode_frame_at(container: Any, stream: Any, seconds: float) -> Any | None:
    """seek 到指定时间点并取该点上/之后的第一帧（PyAV 原生 seek，无外部 ffmpeg）。"""
    time_base = _stream_time_base(stream)
    if time_base is None:
        return None
    try:
        container.seek(int(seconds / time_base), stream=stream, backward=True)
    except Exception:  # noqa: BLE001 - 部分容器/流不支持 seek：退回顺序解码
        try:
            container.seek(0)
        except Exception:  # noqa: BLE001
            return None
    fallback = None
    for frame in container.decode(stream):
        current = _frame_seconds(frame, time_base)
        if current is None:
            fallback = frame
            continue
        if current >= seconds - 1e-6:
            return frame
        fallback = frame
    return fallback


def _frame_to_image(frame: Any) -> Any:
    from PIL import Image  # noqa: PLC0415 - 与 _load_av 同口径的惰性导入

    image = frame.to_image()
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    return image


def _fit_max_edge(image: Any, max_edge: int) -> Any:
    width, height = image.size
    longest = max(width, height)
    if longest <= max_edge or longest <= 0:
        return image
    from PIL import Image  # noqa: PLC0415

    scale = max_edge / longest
    return image.resize(
        (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
        Image.LANCZOS,
    )


def _gray_signature(image: Any) -> Any:
    return image.convert("L").resize((64, 64))


def _scene_diff(current: Any, previous: Any) -> float:
    """平均绝对差（归一化 0-1）。**不是** ffmpeg 的 scene score，量纲不同。"""
    from PIL import ImageChops, ImageStat  # noqa: PLC0415

    return float(ImageStat.Stat(ImageChops.difference(current, previous)).mean[0]) / 255.0


# ---- 采样计划 ----

def _interval_times(start: float, end: float, interval: float) -> list[float]:
    if interval <= 0:
        raise ValueError("interval 必须大于 0")
    if end <= start:
        raise ValueError(f"end（{end:g}）必须大于 start（{start:g}）")
    count = int(math.floor((end - start) / interval)) + 1
    return [round(start + index * interval, 3) for index in range(max(1, count))]


def _trim_evenly(items: list[Any], max_count: int) -> list[Any]:
    """候选多于上限时按间隔均匀裁剪（保留首尾）。"""
    total = len(items)
    if total <= max_count:
        return list(items)
    if max_count <= 1:
        return [items[0]]
    indexes = [int(round(index * (total - 1) / (max_count - 1))) for index in range(max_count)]
    picked: list[Any] = []
    for index in indexes:
        item = items[min(max(0, index), total - 1)]
        if item not in picked:
            picked.append(item)
    return picked


def _pick_keyframes(
    container: Any,
    stream: Any,
    *,
    start: float,
    end: float,
    threshold: float,
    max_count: int,
) -> list[float]:
    """顺序解码 + 与上一**保留**帧降采样比对，差异超阈值即保留（真正的"抽关键帧"）。

    对"界面切换 / 翻页 / 切镜"这类关键信息节点，比按秒均匀抽准得多；结果超过上限时
    按差异值从大到小取前 N 个，再按时间升序排列。
    """
    time_base = _stream_time_base(stream)
    if time_base is None:
        return []
    step = 1.0 / max(1.0, VIDEO_KEYFRAME_PROBE_FPS)
    grid = start
    last_signature = None
    last_kept = None
    picks: list[tuple[float, float]] = []
    for frame in container.decode(stream):
        seconds = _frame_seconds(frame, time_base)
        if seconds is None:
            continue
        if seconds < start - 1e-6:
            continue
        if end is not None and seconds > end + 1e-6:
            break
        if seconds + 1e-9 < grid:
            continue
        grid = seconds + step
        try:
            signature = _gray_signature(_frame_to_image(frame))
        except Exception:  # noqa: BLE001 - 单帧转换失败不阻断整趟扫描
            continue
        if last_signature is None:
            picks.append((1.0, seconds))
            last_signature, last_kept = signature, seconds
            continue
        diff = _scene_diff(signature, last_signature)
        if diff >= threshold and (seconds - float(last_kept or 0.0)) >= VIDEO_KEYFRAME_MIN_GAP:
            picks.append((diff, seconds))
            last_signature, last_kept = signature, seconds
    if len(picks) > max_count:
        picks = sorted(sorted(picks, key=lambda item: item[0], reverse=True)[:max_count], key=lambda item: item[1])
    return [seconds for _diff, seconds in picks]


# ---- 联系表（省 token 的关键：1 张图概览全片）----

def _sheet_font() -> Any:
    from PIL import ImageFont  # noqa: PLC0415

    try:
        return ImageFont.load_default(size=14)
    except Exception:  # noqa: BLE001 - 老版本 Pillow 无 size 参数
        return ImageFont.load_default()


def _build_contact_sheet(entries: list[tuple[float, Any]], target: Path) -> tuple[Path, int, int]:
    """把多帧拼成网格图（每格下方标注时间），长边 ≤ VIDEO_SHEET_MAX_EDGE。"""
    from PIL import Image, ImageDraw  # noqa: PLC0415

    cols = min(4, max(1, len(entries)))
    rows = int(math.ceil(len(entries) / cols))
    slot_width = VIDEO_SHEET_SLOT_WIDTH
    cells: list[tuple[float, Any]] = []
    for seconds, image in entries:
        width, height = image.size
        scale = slot_width / max(1, width)
        cells.append((seconds, image.resize((slot_width, max(1, int(round(height * scale)))), Image.LANCZOS)))
    row_height = max(cell.size[1] for _seconds, cell in cells)
    width = cols * slot_width + (cols + 1) * VIDEO_SHEET_GAP
    height = rows * (row_height + VIDEO_SHEET_LABEL_HEIGHT) + (rows + 1) * VIDEO_SHEET_GAP
    sheet = Image.new("RGB", (width, height), (24, 24, 28))
    draw = ImageDraw.Draw(sheet)
    font = _sheet_font()
    for index, (seconds, cell) in enumerate(cells):
        row, col = divmod(index, cols)
        left = VIDEO_SHEET_GAP + col * (slot_width + VIDEO_SHEET_GAP)
        top = VIDEO_SHEET_GAP + row * (row_height + VIDEO_SHEET_LABEL_HEIGHT + VIDEO_SHEET_GAP)
        sheet.paste(cell, (left, top))
        draw.text(
            (left + 2, top + cell.size[1] + 2),
            f"t={seconds:.1f}s",
            fill=(232, 232, 238),
            font=font,
        )
    sheet = _fit_max_edge(sheet, VIDEO_SHEET_MAX_EDGE)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".png.part")
    sheet.save(str(temp), format="PNG")
    temp.replace(target)
    return target, sheet.size[0], sheet.size[1]


# ---- 对外服务函数 ----

def probe_video(video_path: Path) -> dict[str, Any]:
    """读视频元信息：时长/帧率/分辨率/总帧数/编码/音轨。

    总帧数用 ``duration × fps`` 估算并在返回值里**如实标注** ``frame_count_estimated``
    （很多容器没有 nb_frames，静默给估算值 = 误导源，维护说明 §九.24）。
    """
    path = _ensure_video_file(video_path)
    av = _load_av()
    try:
        with av.open(str(path)) as container:
            streams = list(container.streams.video)
            if not streams:
                raise ValueError(f"文件不含视频轨道：{path}")
            stream = streams[0]
            duration = _stream_duration(container, stream)
            codec_context = stream.codec_context
            fps = 0.0
            rate = getattr(stream, "average_rate", None) or getattr(stream, "base_rate", None)
            if rate:
                try:
                    fps = float(rate)
                except (TypeError, ValueError, ZeroDivisionError):
                    fps = 0.0
            width = int(getattr(codec_context, "width", 0) or 0)
            height = int(getattr(codec_context, "height", 0) or 0)
            declared = int(getattr(stream, "frames", 0) or 0)
            if declared > 0:
                frame_count, estimated = declared, False
            else:
                frame_count, estimated = int(round(duration * fps)), True
            audio_streams = list(container.streams.audio)
            audio_codec = ""
            if audio_streams:
                audio_codec = str(getattr(audio_streams[0].codec_context, "name", "") or "")
            return {
                "duration": round(duration, 3),
                "fps": round(fps, 3),
                "width": width,
                "height": height,
                "frame_count": frame_count,
                "frame_count_estimated": estimated,
                "video_codec": str(getattr(codec_context, "name", "") or ""),
                "has_audio": bool(audio_streams),
                "audio_codec": audio_codec,
            }
    except _av_error_types(av) as exc:  # PyAV 解码异常（注意：它是 ValueError 的子类）
        raise _decode_error(exc) from exc
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - 兜底：不给模型抛裸异常
        raise _decode_error(exc) from exc


def extract_frames(
    video_path: Path,
    data_dir: Path,
    *,
    mode: str = "interval",
    times: Any = None,
    interval: Any = 2,
    threshold: Any = VIDEO_KEYFRAME_THRESHOLD,
    start: Any = None,
    end: Any = None,
    max_count: Any = VIDEO_FRAME_DEFAULT_MAX,
    max_edge: Any = VIDEO_FRAME_MAX_EDGE,
    contact_sheet: bool = True,
) -> dict[str, Any]:
    """从视频抽取画面帧并落托管缓存（幂等复用），可选择附带一张联系表。

    返回 {duration, mode, count, truncated, frames:[{t, path, thumb_path, width, height}],
    sheet, next_step}；同键（视频 + 参数）目录已存在时直接复用，不重新解码。
    """
    path = _ensure_video_file(video_path)
    mode = str(mode or "interval").strip().lower()
    if mode not in MODES:
        raise ValueError(f"mode 非法：{mode!r}（可选 {'/'.join(MODES)}）")
    max_count = _clamp_int(max_count, VIDEO_FRAME_DEFAULT_MAX, 1, VIDEO_FRAME_HARD_MAX, "max")
    max_edge = _clamp_int(max_edge, VIDEO_FRAME_MAX_EDGE, VIDEO_FRAME_MIN_EDGE, 4096, "max_edge")
    interval_value = _float_param(interval, 2.0, "interval")
    threshold_value = _float_param(threshold, VIDEO_KEYFRAME_THRESHOLD, "threshold")
    if not 0 < threshold_value <= 1:
        raise ValueError("threshold 必须在 0-1 之间（画面切换灵敏度，越小越敏感）")
    requested = _parse_times(times)
    if mode == "times" and not requested:
        raise ValueError('mode=times 需要 times 参数，例如 times="3,15,42.5"（秒）')
    want_sheet = bool(contact_sheet)

    av = _load_av()
    try:
        with av.open(str(path)) as container:
            streams = list(container.streams.video)
            if not streams:
                raise ValueError(f"文件不含视频轨道：{path}")
            stream = streams[0]
            duration = _stream_duration(container, stream)
            if duration <= 0:
                raise ValueError(f"无法读取视频时长（可能是损坏文件或无有效视频轨）：{path}")
            start_value = 0.0 if start in (None, "") else _float_param(start, 0.0, "start")
            end_value = duration if end in (None, "") else _float_param(end, duration, "end")
            if start_value < 0:
                raise ValueError("start 不能为负数")
            if end_value <= start_value:
                raise ValueError(f"end（{end_value:g}）必须大于 start（{start_value:g}）")
            if end_value > duration + 0.05:
                raise ValueError(f"end 越界：视频时长 {duration:.3f} 秒，end={end_value:g}")
            if requested and requested[-1] > duration + 0.05:
                raise ValueError(
                    f"times 越界：视频时长 {duration:.3f} 秒，times 含 {requested[-1]:g}"
                )

            if mode == "times":
                plan = [seconds for seconds in requested if start_value - 1e-6 <= seconds <= end_value + 1e-6]
                if not plan:
                    raise ValueError(
                        f"times 全部落在 [start={start_value:g}, end={end_value:g}] 之外"
                    )
                params_key = _params_key({
                    "v": 1, "mode": mode, "times": plan, "max": max_count,
                    "max_edge": max_edge, "sheet": want_sheet,
                })
            elif mode == "interval":
                plan = _interval_times(start_value, end_value, interval_value)
                # 丢掉超出"最后一帧时间"的候选（默认 end=duration 时，末尾那个点
                # 没有对应画面，只能回退到上一帧 —— 会得到一个被标错时间的帧）。
                fps = 0.0
                rate = getattr(stream, "average_rate", None) or getattr(stream, "base_rate", None)
                if rate:
                    try:
                        fps = float(rate)
                    except (TypeError, ValueError, ZeroDivisionError):
                        fps = 0.0
                if fps > 0:
                    last_frame = duration - (1.0 / fps) / 2.0
                    plan = [seconds for seconds in plan if seconds <= last_frame]
                if not plan:
                    plan = [start_value]
                params_key = _params_key({
                    "v": 1, "mode": mode, "interval": interval_value,
                    "start": start_value, "end": end_value, "max": max_count,
                    "max_edge": max_edge, "sheet": want_sheet,
                })
            else:
                plan = []
                params_key = _params_key({
                    "v": 1, "mode": mode, "threshold": threshold_value,
                    "start": start_value, "end": end_value, "max": max_count,
                    "max_edge": max_edge, "sheet": want_sheet,
                })

            cache_dir = _video_cache_dir(data_dir, path, params_key)
            cached = _read_manifest(cache_dir, mode)
            if cached is not None:
                cached["duration"] = round(duration, 3)
                return cached

            if plan:
                picked = _trim_evenly(plan, max_count)
                truncated = len(picked) < len(plan)
            else:
                picked = _pick_keyframes(
                    container, stream,
                    start=start_value, end=end_value,
                    threshold=threshold_value, max_count=max_count,
                )
                truncated = False
                if not picked:
                    raise ValueError("没有抽到任何帧：可尝试降低 threshold 或扩大 start/end 区间")
                # 关键帧模式：候选数量本身由 max 截断，按差异排序后截断 → 不标 truncated
                # （truncated 只表示"按间隔均匀裁剪掉了候选"，语义见返回说明）。

            # 两级上限（目录级 / 视频级）：均在**渲染前**判定，避免写到一半才发现超限。
            cache_root = _video_root_dir(data_dir, path)
            planned = len(picked) + (1 if want_sheet else 0)
            existing_dir = _count_png(cache_dir)
            if existing_dir + planned > VIDEO_CACHE_MAX_IMAGES:
                raise ValueError(
                    f"该视频这组抽帧参数的缓存已达上限（{VIDEO_CACHE_MAX_IMAGES} 张）；"
                    f"请先用 /api/imaging/clean 清理 uploads/video_frames，或改用更少的帧数。"
                )
            existing_video = _count_png(cache_root)
            if existing_video + planned > VIDEO_CACHE_MAX_PER_VIDEO:
                raise ValueError(
                    f"该视频的抽帧缓存合计已达上限（{VIDEO_CACHE_MAX_PER_VIDEO} 张，"
                    "不同参数各占一个子目录）；请指定 start/end 区间缩小范围，或先清理帧图缓存。"
                )

            cache_dir.mkdir(parents=True, exist_ok=True)
            frames: list[dict[str, Any]] = []
            entries: list[tuple[float, Any]] = []
            for index, seconds in enumerate(picked):
                target = cache_dir / f"frame_{index:03d}_t{seconds:.1f}.png"
                image = None
                if not target.is_file() or target.stat().st_size <= 0:
                    frame = _decode_frame_at(container, stream, seconds)
                    if frame is None:
                        continue
                    image = _fit_max_edge(_frame_to_image(frame), max_edge)
                    _save_png(image, target)
                if image is None:
                    from PIL import Image  # noqa: PLC0415

                    image = Image.open(target)
                    image.load()
                entries.append((seconds, image))
                frames.append({
                    "t": round(seconds, 3),
                    "path": str(target),
                    "thumb_path": _thumb(target),
                    "width": image.size[0],
                    "height": image.size[1],
                })
            if not frames:
                raise ValueError("没有抽到任何帧：可能是损坏文件，或时间点全部无法解码")

            sheet_path = ""
            if want_sheet:
                sheet_cols = min(4, len(entries))
                sheet_rows = int(math.ceil(len(entries) / sheet_cols))
                sheet_target = cache_dir / f"sheet_{sheet_cols}x{sheet_rows}.png"
                sheet, _w, _h = _build_contact_sheet(entries, sheet_target)
                sheet_path = str(sheet)
                # 联系表也要出 WebP 缩略图：采集器对 uploads 内的产物不会补缩略图，
                # 而媒体记录里 sheet 是**裸路径**（无 thumb_path 键）→ 前端按
                # `<stem>_thumb.webp` 推导，不出这张图必然是 404 破图
                # （与 storage/media.py 对 GIF 的同一条教训）。
                _thumb(sheet_target)

            result = {
                "duration": round(duration, 3),
                "mode": mode,
                "count": len(frames),
                "truncated": bool(truncated),
                "frames": frames,
                "sheet": sheet_path,
                "next_step": (
                    "把帧图路径（或联系表）交给 vision_analyze 识别内容；"
                    "联系表用于先概览全片，可疑片段再抽单帧细看。"
                ),
            }
            _write_manifest(cache_dir, result)
            return result
    except _av_error_types(av) as exc:  # PyAV 解码异常（注意：它是 ValueError 的子类）
        raise _decode_error(exc) from exc
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - 兜底：不给模型抛裸异常
        raise _decode_error(exc) from exc


def _save_png(image: Any, target: Path) -> None:
    temp = target.with_suffix(".png.part")
    image.save(str(temp), format="PNG")
    temp.replace(target)


def _thumb(target: Path) -> str:
    from naiba.storage.media import _ensure_webp_thumb

    return _ensure_webp_thumb(target, None) or str(target)


def _read_manifest(cache_dir: Path, mode: str) -> dict[str, Any] | None:
    """幂等读回：清单存在、帧图与联系表都在 → 直接复用（不重新解码）。"""
    manifest = _manifest_path(cache_dir)
    if not manifest.is_file():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or str(payload.get("mode") or "") != mode:
        return None
    frames = payload.get("frames")
    if not isinstance(frames, list) or not frames:
        return None
    for item in frames:
        if not isinstance(item, dict) or not Path(str(item.get("path") or "")).is_file():
            return None
        thumb = str(item.get("thumb_path") or "")
        if thumb and not Path(thumb).is_file():
            item["thumb_path"] = ""
    sheet = str(payload.get("sheet") or "")
    if sheet and not Path(sheet).is_file():
        payload["sheet"] = ""
    payload["cached"] = True
    return payload


def _write_manifest(cache_dir: Path, result: dict[str, Any]) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        _manifest_path(cache_dir).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        # 清单只是幂等加速器：写失败不影响本次结果（下次重抽即可）
        pass
