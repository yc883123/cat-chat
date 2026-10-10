"""诊断开关与调试辅助（唯一实现）。

来源：原 server.py:2488-2494（CACHE_DEBUG_ON/_cache_debug_enabled）、
model_runtime.py:21-87（_debug_wire_digest/_debug_complete_marker/_sanitize_payload/_debug_payload_dump）、
skill_runtime.py:28-57（_debug_message_digest）。三处行为曾经不一致（ImportError 兜底
False/True 相反），已于阶段 0 统一，本模块为合并后的唯一实现。

开关语义：默认关闭；CACHE_DEBUG_ON=True 或环境变量 NAIBA_DEBUG_CACHE=1 时开启。
各 debug_* 函数在未开启或回调缺失时直接返回，不产生任何副作用。

权限判定诊断（PERMISSION_DEBUG_ON / NAIBA_DEBUG_PERMISSION=1）同构：用于排查
"工作区内路径被判越界"这类问题，输出当前执行器身份、当前工作区、请求路径与允许根。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any, Callable

# 前缀缓存诊断总开关：默认关闭。需要调试时改为 True（或设 NAIBA_DEBUG_CACHE=1）。
CACHE_DEBUG_ON = False

# 权限判定诊断总开关：默认关闭。需要调试时改为 True（或设 NAIBA_DEBUG_PERMISSION=1）。
PERMISSION_DEBUG_ON = False

# 空流证据诊断总开关：默认关闭。需要调试时改为 True（或设 NAIBA_DEBUG_STREAM=1）。
STREAM_DEBUG_ON = False

StatusCallback = Callable[[dict[str, Any]], None] | None


def _cache_debug_enabled() -> bool:
    """诊断总开关：默认关闭；设 CACHE_DEBUG_ON=True 或 NAIBA_DEBUG_CACHE=1 开启。"""
    return bool(CACHE_DEBUG_ON) or os.environ.get("NAIBA_DEBUG_CACHE") == "1"


def _permission_debug_enabled() -> bool:
    """权限判定诊断开关：默认关闭；设 PERMISSION_DEBUG_ON=True 或 NAIBA_DEBUG_PERMISSION=1 开启。

    开启后只写日志（``naiba.tools.*`` logger），不改变任何判定结果。
    """
    return bool(PERMISSION_DEBUG_ON) or os.environ.get("NAIBA_DEBUG_PERMISSION") == "1"


def _stream_debug_enabled() -> bool:
    """空流证据诊断开关：默认关闭；设 STREAM_DEBUG_ON=True 或 NAIBA_DEBUG_STREAM=1 开启。"""
    return bool(STREAM_DEBUG_ON) or os.environ.get("NAIBA_DEBUG_STREAM") == "1"


def _debug_empty_stream_evidence(
    request_format: str,
    chunks: list[Any],
    status: StatusCallback = None,
) -> None:
    """空流（正文为空）时留存**脱敏**响应证据，区分「上游真的没回」与「解析丢失」。

    默认关闭（STREAM_DEBUG_ON）或设 NAIBA_DEBUG_STREAM=1 时触发。输出：分片总数、
    事件类型分布、最后若干个原始分片（超长字符串压成占位符，杜绝 base64/长文本刷屏）。
    外部排查（2026-10 报告）指出空流报错「尚无原始响应证据区分上游空响应与流解析
    兼容问题」——本诊断即为此补证据：分片里有文本而我们没解析出来 = 解析侧问题；
    分片本来就空/只有错误事件 = 上游问题。优先经 ``status`` 回调推给前端
    （``debug_stream`` 事件），无回调时写 stderr。只读不改流结果。
    """
    if not _stream_debug_enabled():
        return
    kinds: dict[str, int] = {}
    for chunk in chunks or []:
        if isinstance(chunk, dict):
            kind = str(chunk.get("type") or ("choices" if "choices" in chunk else "?"))
        else:
            kind = type(chunk).__name__
        kinds[kind] = kinds.get(kind, 0) + 1
    lines = [
        f"[STREAM] empty-stream evidence format={request_format} chunks={len(chunks or [])}",
        f"[STREAM] kinds: {json.dumps(kinds, ensure_ascii=False, sort_keys=True)}",
    ]
    tail = list(chunks or [])[-30:]
    if tail:
        lines.append(f"[STREAM] last {len(tail)} chunks (sanitized):")
        for index, chunk in enumerate(tail):
            try:
                text = json.dumps(_sanitize_payload(chunk, limit=120), ensure_ascii=False, default=str)
            except Exception:
                text = "<chunk dump failed>"
            lines.append(f"    [{index}] {text[:2000]}")
    if callable(status):
        status({"type": "debug_stream", "label": "empty-stream", "lines": lines})
    else:
        print("\n".join(lines), file=sys.stderr, flush=True)


def ensure_utf8_stdio(
    streams=None, *, line_buffering: bool = False, write_through: bool = False
) -> None:
    """把 stdout/stderr 切成 UTF-8（errors="replace"）。

    背景：Windows 英文控制台（cp1252 等）下打印中文会抛 ``UnicodeEncodeError``——
    源码 ``python server.py`` 的启动横幅、诊断输出会直接崩（GitHub Actions windows
    runner 实测）。冻结版 ``runw`` 无控制台不受影响；子进程侧另有
    ``launcher._force_utf8_stdio``（冻结版 Skill 脚本入口）。

    ``line_buffering``/``write_through`` 供命令行主入口开启（日志即时刷出）；
    库内调用保持默认，避免擅自改动宿主进程的缓冲策略。
    """
    for stream in (streams if streams is not None else (sys.stdout, sys.stderr)):
        try:
            stream.reconfigure(
                encoding="utf-8",
                errors="replace",
                line_buffering=line_buffering,
                write_through=write_through,
            )
        except (AttributeError, OSError, ValueError):
            continue


def _sanitize_payload(obj: Any, limit: int = 500) -> Any:
    """把 payload 里的超长字符串（通常是 base64 图片）压成占位符，便于逐字段比对。"""
    if isinstance(obj, str):
        return obj if len(obj) <= limit else f"<str:{len(obj)}>{obj[:40]}…"
    if isinstance(obj, list):
        return [_sanitize_payload(item, limit) for item in obj]
    if isinstance(obj, dict):
        return {key: _sanitize_payload(value, limit) for key, value in obj.items()}
    return obj


def _debug_wire_digest(messages: list[dict[str, Any]], status: StatusCallback) -> None:
    """缓存诊断：对**真正发给模型**（经 _openai_messages 转换后）的消息逐条求哈希，
    用来对比"第 N 轮请求"与"第 N+1 轮历史"对应消息的 wire 字节是否一致。
    与 skill_runtime 的 `step-*-request`（原始消息）对齐，可分辨"原始 base64 相同
    但 wire 图片不同 / DeepSeek 不缓存 image 区域"。
    """
    if not _cache_debug_enabled() or not callable(status):
        return
    lines = [f"[CACHE] wire digest ({len(messages)} msgs):"]
    for i, m in enumerate(messages[:40]):
        try:
            j = json.dumps(m, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            j = ""
        lines.append(f"    [{i}:{m.get('role')}:{len(j)}:{hashlib.sha256(j.encode('utf-8')).hexdigest()[:10]}]")
    status({"type": "debug_cache", "label": "wire", "lines": lines})


def _debug_complete_marker(
    kind: str,
    request_format: str,
    messages: list[dict[str, Any]],
    status: StatusCallback,
) -> None:
    """无条件标记：只要进入 ModelRuntime.complete 就打，用于确认 wire 代码确实在
    真正调用路径上（排除"EXE 里是旧模块/没走到 complete/格式不对"三种可能）。"""
    if not _cache_debug_enabled() or not callable(status):
        return
    what = (
        f"[CACHE] complete-entry kind={kind} format={request_format} "
        f"messages={len(messages) if isinstance(messages, list) else None} "
        f"msgs_type={type(messages).__name__}"
    )
    status({"type": "debug_cache", "label": "complete-entry", "lines": [what]})


def _debug_payload_dump(payload: dict[str, Any], status: StatusCallback) -> None:
    """把**发给模型**的完整 payload 打出来（图片 base64 压成占位符，其他字段全保留），
    逐字段对比"第 N 轮请求 vs 第 N+1 轮请求"，定位是哪部分（instructions/tools/messages/…）
    在每轮变化导致缓存断链。"""
    if not _cache_debug_enabled() or not callable(status):
        return
    try:
        txt = json.dumps(_sanitize_payload(payload), ensure_ascii=False, sort_keys=True)
    except Exception:
        txt = "<payload dump failed>"
    status({"type": "debug_cache", "label": "payload-dump", "lines": [txt]})


def _debug_message_digest(messages: list[Any], label: str, event=None) -> None:
    """缓存诊断辅助：逐条输出组装后消息的 [索引:角色:字节数:哈希]。

    默认关闭（CACHE_DEBUG_ON）或设 NAIBA_DEBUG_CACHE=1 时触发，用来对比"第 N 轮请求"
    与"第 N+1 轮历史"中对应消息是否字节一致，定位前缀缓存的分叉点。优先通过 ``event``
    回调以 ``debug_cache`` 事件推给前端（用户在浏览器控制台可见）；无回调时兜底写 stderr。
    """
    if not _cache_debug_enabled():
        return
    lines = [f"[CACHE] {label} digest ({len(messages)} msgs):"]
    for i, m in enumerate(messages[:40]):
        try:
            j = json.dumps(m, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            j = ""
        lines.append(f"    [{i}:{m.get('role')}:{len(j)}:{hashlib.sha256(j.encode('utf-8')).hexdigest()[:10]}]")
    if callable(event):
        event({"type": "debug_cache", "label": label, "lines": lines})
    else:
        print("\n".join(lines), file=sys.stderr, flush=True)
