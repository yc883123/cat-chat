"""提取 ComfyUI 采样步级进度（只读内存态，不落盘、不进任务快照）。

ComfyUI 的 /history、/queue 在执行期没有步级字段；官方内部端点
``GET /internal/logs/raw`` 返回日志缓冲（``{"entries": [{"t": 毫秒, "m": 文本}]}``），
tqdm 的采样进度行（``25%|███| 2/8 [00:05<00:14, 6.94it/s]``）就在其中。
runner 在既有的 1.5s 轮询里顺带 GET 一次，解析最后一条进度行更新内存态。

归属与降级口径：
- runner 是单 worker 串行，同一时刻至多一个在途 prompt；提交时把 prompt_id
  绑进状态，``snapshot_for`` 只对绑定的 prompt 生效，不猜测归属。
- 步级数据带新鲜期（超出视为段间/已结束），过期不回快照。
- 端点缺失（404）、格式变化、tqdm 被关闭都表现为「解析不到」→ 状态保持旧值/
  过期 → 轮询回包自然不带 step 字段，前端不渲染进度条，一切回到现状。
- ``rate`` 存 tqdm 原文（如 ``6.94it/s`` / ``3.22s/it``），不做数值化。
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from typing import Any
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

from naiba.comfyui.client import normalize_base_url

# 空代理表强制直连，与 client._DIRECT_OPENER 一致，不读取系统/环境代理。
_DIRECT_OPENER = build_opener(ProxyHandler({}))

# tqdm 进度行：竖条分隔的 N/M 后紧跟方括号计时块（区别于日志里普通 "1/3" 计数）。
_PROGRESS_RE = re.compile(r"\|\s*(\d+)\s*/\s*(\d+)\s*\[([^\]]*)\]")
# 速率在计时块内：",  6.94it/s" 或慢速形态 ", 3.22s/it"；tqdm 也可能没打出来。
_RATE_RE = re.compile(r"[,\s](\d+(?:\.\d+)?)\s*(it/s|s/it)")

# 新鲜期：超过说明当前段已采样完成（段间）或日志已滚动，不再展示旧条。
FRESH_WINDOW_MS = 15000
# 日志缓冲最大读取字节数（/internal/logs/raw 上限 300 条，常规远小于此）。
_MAX_BODY = 200000

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {}


def _fetch_raw(base: str) -> dict[str, Any] | None:
    try:
        with _DIRECT_OPENER.open(f"{normalize_base_url(base)}/internal/logs/raw", timeout=5) as resp:
            return json.loads(resp.read(_MAX_BODY).decode("utf-8", errors="replace"))
    except HTTPError:
        # 内部端点可能 404（老版本 / 关闭）：静默降级为「没有步级数据」。
        return None
    except Exception:  # noqa: BLE001 - 网络抖动视为本轮没有数据
        return None


def _entry_stamp_ms(stamp: Any) -> int:
    """日志条目时间戳 → epoch 毫秒；解析不动返回 0（视为无时间，不参与新鲜期）。

    ComfyUI logger 的 ``t`` 是 ``datetime.now().isoformat()`` 字符串；也兼容
    秒/毫秒数字（不同版本实现有差异）。
    """
    if isinstance(stamp, (int, float)) and stamp > 0:
        # 秒级时间戳（< 1e11）转毫秒；毫秒级原样。
        return int(stamp) if stamp > 1e11 else int(stamp * 1000)
    text = str(stamp or "").strip()
    if not text:
        return 0
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


def parse_progress(text: str) -> dict[str, Any] | None:
    """解析一条 tqdm 进度文本 → ``{value, max, rate}``；不匹配返回 None。

    多行文本（含 \\r 合并行）取最后一个匹配；rate 缺省为空串。
    """
    cleaned = str(text or "")
    match = None
    for match in _PROGRESS_RE.finditer(cleaned):
        pass
    if match is None:
        return None
    try:
        value = int(match.group(1))
        max_steps = int(match.group(2))
    except ValueError:
        return None
    if max_steps <= 0 or value > max_steps:
        return None
    rate_match = None
    for rate_match in _RATE_RE.finditer(match.group(3)):
        pass
    rate = f"{rate_match.group(1)}{rate_match.group(2)}" if rate_match else ""
    return {"value": value, "max": max_steps, "rate": rate}


def observe(base: str, prompt_id: str) -> None:
    """轮询循环里调用：拉一次日志缓冲，解析最后一条进度行更新内存态。

    任何失败都保持旧状态（由新鲜期兜底），绝不抛出影响打图主链路。
    """
    with _LOCK:
        if str(_STATE.get("prompt_id") or "") != str(prompt_id or ""):
            _STATE.clear()
            _STATE["prompt_id"] = str(prompt_id or "")
    payload = _fetch_raw(base)
    if not isinstance(payload, dict):
        return
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return
    for entry in reversed(entries):
        message = entry.get("m") if isinstance(entry, dict) else None
        if not isinstance(message, str):
            continue
        parsed = parse_progress(message)
        if parsed is None:
            continue
        # 时间戳解析不动（updated_at=0）时不用该行：旧行会被误判成「永远新鲜」，
        # 新任务开局会把上一次生成留在缓冲里的进度当现势数据显示。
        updated_at = _entry_stamp_ms(entry.get("t") if isinstance(entry, dict) else None)
        if updated_at <= 0:
            continue
        with _LOCK:
            _STATE.update({
                "prompt_id": str(prompt_id or ""),
                "value": parsed["value"],
                "max": parsed["max"],
                "rate": parsed["rate"],
                "updated_at": updated_at,
            })
        return


def snapshot_for(prompt_ids: Any) -> dict[str, Any] | None:
    """轮询回包透传用：prompt_ids 含绑定 prompt 且数据新鲜时返回步级状态。"""
    with _LOCK:
        if not _STATE:
            return None
        bound = str(_STATE.get("prompt_id") or "")
        if not bound:
            return None
        known = {str(item or "") for item in (prompt_ids or [])}
        if bound not in known:
            return None
        try:
            updated_at = int(_STATE.get("updated_at") or 0)
        except (TypeError, ValueError):
            return None
        if updated_at <= 0 or (int(time.time() * 1000) - updated_at) > FRESH_WINDOW_MS:
            return None
        value = _STATE.get("value")
        max_steps = _STATE.get("max")
        if not isinstance(value, int) or not isinstance(max_steps, int):
            return None
        if max_steps <= 0 or value > max_steps:
            return None
        return {
            "value": value,
            "max": max_steps,
            "rate": str(_STATE.get("rate") or ""),
            "updated_at": updated_at,
        }


def reset_for_tests() -> None:
    with _LOCK:
        _STATE.clear()
