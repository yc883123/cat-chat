"""多媒体类型与媒体采集声明的**唯一定义**（图片 / 视频 / 音频）。

背景（维护说明 §九.30「按工具名/扩展名硬编码的集合会各写各的」）：
后端曾有三份手写同源名单——`core/attachments.py::MEDIA_PRODUCT_EXTS`、
`extract_attachments` 内联的 `extensions`、`storage/media.py::IMAGE_SUFFIXES`；
前端又各写一份正则。任何一处漏改都会造成"产物在消息里静默消失"或"破图"
（真实事故：`.bmp`/`.svg` 被前端从"修改文件"里滤掉、后端又不认，产物完全不可见）。
本模块是这些名单的唯一来源：后端各处 import 它，前端经 `/api/bootstrap.media_exts`
取同一份数据（见 `media_exts_payload`），不再各写一份。

分层：本模块只含常量与纯函数（core 层），不做任何 IO/缓存——采集与落盘属
`storage/media.py`，声明表属 `tools/registry.py`。
"""

from __future__ import annotations

import urllib.parse
from typing import Any

# ---- 类型名单（唯一来源）----
# 顺序即展示/文档顺序；判定一律走 media_kind_of()，不要在新代码里再写扩展名正则。
MEDIA_EXTS_BY_KIND: dict[str, tuple[str, ...]] = {
    "image": (".png", ".jpg", ".jpeg", ".webp", ".gif"),
    "video": (".mp4", ".webm", ".mov", ".m4v", ".ogv"),
    "audio": (".wav", ".mp3", ".m4a", ".ogg", ".flac"),
}
MEDIA_KINDS: tuple[str, ...] = tuple(MEDIA_EXTS_BY_KIND)
MEDIA_EXTS: frozenset[str] = frozenset(
    ext for exts in MEDIA_EXTS_BY_KIND.values() for ext in exts
)
_KIND_BY_EXT: dict[str, str] = {
    ext: kind for kind, exts in MEDIA_EXTS_BY_KIND.items() for ext in exts
}

# 上传**压缩**支持的图片格式：GIF 保持原图与动画（不压缩），但仍生成首帧缩略图
# （可出缩略图的格式集见 storage/media.THUMB_SOURCE_SUFFIXES）。
IMAGE_PROCESS_EXTS: frozenset[str] = frozenset(MEDIA_EXTS_BY_KIND["image"]) - {".gif"}

# 单条消息的媒体分桶上限（宿主缓存与前端展示共用口径）：
# 超出部分**先截断再落盘**（避免列一次目录就把几百张图拷进 data 目录 + 生成缩略图卡住），
# 并向前端标注"共 N 张，仅显示前 M 张"（静默截断 = 误导源，维护说明 §九.24）。
MEDIA_BUCKET_LIMITS: dict[str, int] = {"image": 20, "video": 8, "audio": 8}

# 单个媒体文件的体积上限（**唯一来源**）：上传与 ``show_media`` 共用同一口径。
# 为什么必须有它：``show_media`` 允许模型按用户指名的路径展示工作区里任意已有媒体，
# 而首次展示会经 ``MediaCollector`` 完整读取并复制到 data/generated 再生成缩略图。
# 没有上限就等于"用户说一句「放出来」，宿主就可能整份读完一个几十 GB 的视频"。
# 超限只在 ``stat`` 阶段拒绝（too_large），不读全文、不复制、不建缩略图、不写 generated。
MEDIA_SHOW_MAX_BYTES = 80 * 1024 * 1024

# 扩展名 → MIME：系统 mimetypes 未注册时由 /api/file 兜底（否则 <img>/<video> 因
# application/octet-stream + nosniff 被拒渲染）。.m4v 曾漏登记导致视频可能不播。
MEDIA_MIME_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".ogv": "video/ogg",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}
# 非"媒体卡"但仍需正确 MIME 的扩展名（文件 chip 点开、旧数据兜底）。
EXTRA_MIME_TYPES: dict[str, str] = {
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
    ".oga": "audio/ogg",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
MIME_BY_EXT: dict[str, str] = {**MEDIA_MIME_TYPES, **EXTRA_MIME_TYPES}


def media_kind_of(source: str | Any) -> str | None:
    """判定来源是否多媒体，返回 ``"image" | "video" | "audio"``；否则 None。

    同时支持三类来源：
    - 本地/绝对路径（含查询串，如 ``C:\\a.png?x=1``）；
    - 直链 URL（``https://host/a.mp4``）；
    - ComfyUI 产物 URL（扩展名在查询参数里：``http://127.0.0.1:8188/view?filename=a.png``）。
    """
    text = str(source or "").strip()
    if not text:
        return None
    lowered = text.lower()
    # 扩展名在查询参数里（ComfyUI /view?filename=…）。
    if "?" in lowered:
        parsed = urllib.parse.urlsplit(lowered)
        filename = (urllib.parse.parse_qs(parsed.query).get("filename") or [""])[0]
        if filename:
            kind = _KIND_BY_EXT.get(_ext_of(filename))
            if kind:
                return kind
        lowered = lowered.split("?", 1)[0]
    return _KIND_BY_EXT.get(_ext_of(lowered))


def is_media_path(raw: str | Any) -> bool:
    """是否多媒体产物路径/URL（替代旧的 ``_is_media_product_path``）。"""
    return media_kind_of(raw) is not None


def _ext_of(value: str) -> str:
    """取小写扩展名（含点）；无扩展名返回空串。"""
    text = str(value or "").strip()
    dot = text.rfind(".")
    if dot <= 0:
        return ""
    ext = text[dot:]
    return ext if len(ext) > 1 else ""


def truncate_by_kind(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """按类型分桶截断（图 20 / 视频 8 / 音频 8），保持出现顺序。

    返回 ``(kept, truncated)``：``truncated is None`` 表示未截断；否则为
    ``{"total": N, "shown": M, "kinds": {kind: {"total": t, "shown": s}}}``——
    截断必须可自述（静默截断 = 误导源，维护说明 §九.24），前端据此渲染提示块。
    采集侧用它做**落盘前预截断**（避免一次列目录拷贝几百张图），消息侧用它做
    单条消息的汇总上限，两处同一口径。
    """
    counts: dict[str, int] = {}
    shown_counts: dict[str, int] = {}
    kept: list[dict[str, Any]] = []
    for record in records or []:
        kind = str(record.get("kind") or media_kind_of(record.get("source")) or "")
        counts[kind] = counts.get(kind, 0) + 1
        limit = MEDIA_BUCKET_LIMITS.get(kind)
        if limit is not None and shown_counts.get(kind, 0) >= limit:
            continue
        shown_counts[kind] = shown_counts.get(kind, 0) + 1
        kept.append(record)
    if len(kept) == len(records or []):
        return kept, None
    return kept, {
        "total": len(records or []),
        "shown": len(kept),
        "kinds": {
            kind: {"total": total, "shown": shown_counts.get(kind, 0)}
            for kind, total in counts.items()
        },
    }


def media_exts_payload() -> dict[str, Any]:
    """前端消费的媒体名单（``/api/bootstrap.media_exts``）。

    前端不再各写扩展名正则，一律经 ``mediaKind()`` 查这份名单，从根上消除
    "前后端名单漂移"（`.bmp`/`.svg` 产物曾因此彻底不可见）。
    """
    return {
        "kinds": list(MEDIA_KINDS),
        "exts": {kind: list(exts) for kind, exts in MEDIA_EXTS_BY_KIND.items()},
        "limits": dict(MEDIA_BUCKET_LIMITS),
    }


# ---- 媒体采集声明契约（工具侧）----
# 每个工具在 ``tools/registry.py::MEDIA_DECLARATIONS`` 里显式声明：
# - policy：inline（结果里的媒体是本轮产物，就地显示）/ intent_gated（仅当用户本轮
#   明确要求看图时才显示，如目录枚举）/ never（不产出可显示媒体）；
# - extract：none（不提取）/ structured（结果必是结构化媒体记录，只认 JSON 键）/
#   scan（结构化优先 + 文本路径兜底；与旧 extract_attachments 口径一致）。
# 声明表由守门测试反查（每个内置工具必须声明、取值合法、无未知名）。
MEDIA_POLICIES: tuple[str, ...] = ("inline", "intent_gated", "never")
MEDIA_EXTRACTORS: tuple[str, ...] = ("none", "scan", "structured")
DEFAULT_MEDIA_DECLARATION: dict[str, str] = {"policy": "inline", "extract": "scan"}

# 异步 Job 的媒体采集声明（按 Job kind）：Job 在工具返回**之后**才产出媒体，
# 由 storage/job_media.py 在终态时写回发起它的那条助手消息。
# - comfyui：result.completed_shots[].files 是产物 URL（结构化）；
# - shell：stdout 里可能打印产物路径（文本扫描）；
# - check/http_poll：只出状态，不产媒体；
# - subagent：子 Agent 的结论已随 subagent 工具结果同步返回，写回会重复展示。
JOB_MEDIA_DECLARATIONS: dict[str, dict[str, str]] = {
    "comfyui": {"policy": "inline", "extract": "structured"},
    "shell": {"policy": "inline", "extract": "scan"},
    "check": {"policy": "never", "extract": "none"},
    "http_poll": {"policy": "never", "extract": "none"},
    "subagent": {"policy": "never", "extract": "none"},
}


def job_media_declaration(kind: str) -> dict[str, str]:
    """取 Job kind 的媒体采集声明（未知 kind 按不采集处理，宁可不显示也不误报）。"""
    return normalize_media_declaration(JOB_MEDIA_DECLARATIONS.get(str(kind or ""), {"policy": "never", "extract": "none"}))


def normalize_media_declaration(value: Any) -> dict[str, str]:
    """归一化媒体声明；缺省/非法值回落到默认（未声明的 MCP/第三方工具走默认）。

    非法值不静默吞掉：抛 ValueError（由声明表守门测试与装配期捕获，避免"写错了
    但看起来没坏"）。
    """
    if value is None:
        return dict(DEFAULT_MEDIA_DECLARATION)
    if not isinstance(value, dict):
        raise ValueError(f"media 声明必须是对象：{value!r}")
    policy = str(value.get("policy") or DEFAULT_MEDIA_DECLARATION["policy"])
    extract = str(value.get("extract") or DEFAULT_MEDIA_DECLARATION["extract"])
    if policy not in MEDIA_POLICIES:
        raise ValueError(f"media.policy 非法：{policy!r}（可选 {MEDIA_POLICIES}）")
    if extract not in MEDIA_EXTRACTORS:
        raise ValueError(f"media.extract 非法：{extract!r}（可选 {MEDIA_EXTRACTORS}）")
    return {"policy": policy, "extract": extract}
