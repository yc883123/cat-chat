from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from html.parser import HTMLParser
import http.client
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from naiba import net as net_io
from naiba.llm import thinking as thinking_module
from naiba.llm.protocols import ProtocolMixins
from naiba.llm.stream import (
    ContextOverflowError,
    EmptyModelStreamError,
    StreamMixins,
    StreamTotalTimeout,
    _STREAM_SOURCE_LABELS,
    is_context_overflow,
)
from naiba.core.diagnostics import (
    _debug_complete_marker,
    _debug_payload_dump,
    _debug_wire_digest,
    _sanitize_payload,
)

logger = logging.getLogger("naiba.model_runtime")

StatusCallback = Callable[[dict[str, Any]], None]

# Some OpenAI-compatible gateways sit behind Cloudflare rules that reject
# urllib's default ``Python-urllib/...`` signature before authentication is
# evaluated. A normal browser-compatible UA keeps the API request protocol
# unchanged while allowing model-list and inference requests through.
API_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
ONLINE_MODEL_TIMEOUT_SECONDS = 180
LOCAL_MODEL_TIMEOUT_SECONDS = 1800
# A0：思考时的**默认输出上限**。`max_tokens` 本就是「思考 + 正文」的总量闸门，事故缺口
# 不在「填得太大」，而在「不填 = 不发字段 = 上游默认值近乎无限」——只有 Claude 有 4096
# 硬兜底，其余协议不发字段就等于把输出额度交给上游默认值（DeepSeek/Moonshot 的思考
# token 按输出计费）。实测：MiMo 会话单条回复思考 43 万字符 ≈ 10 万+ token，正是这么烧出来的。
# 真正落地的值是 ``min(本值, 预设表的 max_output_ceiling)``；用户显式设了
# ``max_output_tokens`` 时**永不覆盖**（含故意设很大的值），只靠 400 自愈兜底。
DEFAULT_THINKING_MAX_OUTPUT_TOKENS = 32768
# Claude 的历史兼容兜底：Anthropic 的 max_tokens 是协议必填，A0 只把默认值从 4096 抬到
# min(32768, 预设 ceiling)，不得改坏「必填」语义。
CLAUDE_MAX_TOKENS_FLOOR = 4096
# 输出上限被端点拒绝时的错误体关键词（A0 注入值自愈的判据；用户显式值不吞错）。
_MAX_TOKENS_REJECTION_MARKERS = (
    "max_tokens", "max_output_tokens", "maxoutputtokens", "max output tokens", "num_predict",
)
# 在线单次流式请求的**总时长兜底**（秒）。urllib 的 timeout 是「每次读操作的空闲超时」，
# 推理 delta 持续到达就会不停重置它——一段 43 万字符的思考循环可以无限流下去（见
# _iter_stream_lines 的注释）。900 秒远超市面最长合法思考，只用于把「合法长思考」与
# 「死循环」分开。可用 options["stream_total_timeout_seconds"] 覆盖（0 = 关闭）。
ONLINE_STREAM_TOTAL_TIMEOUT_SECONDS = 900
# 「思考异常冗长」熔断阈值（字符）：正常 high 档思考罕见超过（约 1.2~2 万 token 的推理量），
# 而实测失控循环是 10 万+ 字符。只在**正文仍为空**时熔断，正文一旦出现即解除。
# 可用 options["reasoning_stream_break_chars"] 覆盖（0 = 关闭）。
REASONING_STREAM_BREAK_CHARS = 48000
PROVIDER_TEST_TIMEOUT_SECONDS = 30
# 等本地锁期间的等待时长回报间隔（秒）。原实现是 `lock.acquire()`：无超时、不看
# cancel_event，另一个调用占着全局本地锁时主对话线程会**无限静默**地等下去。
LOCAL_LOCK_WAIT_NOTICE_SECONDS = 5.0
# 本地后端「首字节超时」（秒）：prefill 期间一个字节都不吐是本地模型的常态，但
# 「正在 prefill」与「永远做不完」在界面上无法区分。本地请求的总超时是 30 分钟
# （LOCAL_MODEL_TIMEOUT_SECONDS），意味着请求体一旦超过真实 n_ctx，用户要干等半小时
# 才看到报错（客户机实测：整条会话看起来「永久卡死」，重启无效）。这里对**首个字节**
# 另设一个短超时：超过即断开连接，并给出可行动的错误。0 = 关闭本层（回到旧行为）。
# 可用 options["first_byte_timeout_seconds"] 覆盖（运行设置 → 本地首字节超时）。
LOCAL_FIRST_BYTE_TIMEOUT_SECONDS = 120
# 本地锁占用者的默认标签：调用方可用 options["lock_label"] 换成更具体的角色
# （视觉识别 / 子代理 / 计划整理 …），等锁的一方就能在状态里指名道姓。
DEFAULT_LOCK_LABEL = "对话回复"
FAST_RETRY_NETWORK_ERRORS = {10053, 10054, 10061}
# 失败请求体落盘（取证）：思考回传类 400/422 与全部 5xx 都写这个文件（覆盖式，只留最近一次）。
ERROR_DUMP_FILENAME = "naiba-model-error-payload.json"
ERROR_DUMP_STRING_LIMIT = 4000
ERROR_DUMP_MAX_BYTES = 512 * 1024
# 本地推理后端对应的请求格式；与 server.LOCAL_REQUEST_FORMATS 保持一致。
LOCAL_REQUEST_FORMATS = {"ollama", "lm_studio", "llama_cpp", "unsloth"}
# 在线请求体预算（字节）：超过即在**发送前**把最旧的图片换成文本占位。默认 8MB——低于
# 市面上多数中继/网关的 10MB 体量闸门，又远大于正常一轮对话（含 4 张 900KB 图片约 5MB）。
# 可用 options["online_payload_budget_bytes"] 覆盖（0/负数 = 关闭预检查，只保留 413 自愈）。
ONLINE_PAYLOAD_BUDGET_BYTES = 8 * 1024 * 1024
# 图片被省略后写回模型可见文本的占位行（不静默删图：模型要知道「这里本来有图」，
# 与本地视觉路径的同款口径一致，见 naiba/vision/runtime.py 的图片省略提示）。
IMAGE_OMITTED_PLACEHOLDER = "[已省略一张较早的图片：请求体超出供应商上限]"
_AGENT_BUFFER_LIMIT = 1024

# 最近一次模型请求的终止原因（thread-local）：`_complete_online` 是 staticmethod、返回 4-tuple
# 是既有契约（加一个元素要动所有调用点），故沿用 `last_usage` 的「旁路记录」思路——
# 静态层把值写进线程局部，`complete()` 拿到返回值后立即抄进实例属性 `last_finish_reason`。
# 取值语义见 `ProtocolMixins._online_finish_reason`（"length" = 撞输出上限，"" = 供应商没给）。
_FINISH_REASON = threading.local()


def _record_finish_reason(value: Any) -> None:
    _FINISH_REASON.value = str(value or "")


def _last_finish_reason() -> str:
    return str(getattr(_FINISH_REASON, "value", "") or "")


class LocalModelFirstByteTimeout(RuntimeError):
    """本地后端在首字节超时内没有吐出任何内容。

    与「网络超时」「连接失败」区分开：这一条几乎总是「本轮请求体超过真实上下文
    窗口，prefill 做不完」或「模型仍在加载」。本地请求的总超时是
    ``LOCAL_MODEL_TIMEOUT_SECONDS``(1800s)，不加这一层的话用户要等半小时才看到
    报错，期间界面只有一个「等待本地模型资源」——客户机实测整条会话看起来永久
    卡死（重启后端、重启电脑都无效：病根在每轮构造的请求体里，不在进程里）。
    """


def local_first_byte_timeout_error(seconds: float) -> LocalModelFirstByteTimeout:
    """首字节超时的统一文案（生成器与调用方共用，避免两处措辞漂移）。"""
    return LocalModelFirstByteTimeout(
        f"本地模型 {max(1, int(round(max(0.0, float(seconds)))))} 秒内没有输出任何内容（prefill 未完成或模型仍在加载）。"
        "常见原因是本轮请求超出了本地模型的真实上下文长度："
        "可以开一个新会话、减少 /引用 与附件后重试，"
        "或在「设置 → API 供应商」里确认该本地模型的上下文长度。"
        "（该超时可在「设置 → 运行设置 → 本地首字节超时」调整，0 = 关闭）"
    )


def _stream_total_timeout_error(seconds: float) -> StreamTotalTimeout:
    """单次请求总时长超时的统一文案（生成器与调用方共用，避免两处措辞漂移）。"""
    minutes = max(1.0, float(seconds)) / 60
    return StreamTotalTimeout(
        f"单次请求超过 {minutes:g} 分钟仍未结束，已停止等待。"
        "常见原因是模型陷入了超长思考循环：可以降低思考强度、精简上下文后重试，"
        "或开一个新会话。"
        "（该上限可在 options[\"stream_total_timeout_seconds\"] 调整，0 = 关闭）"
    )


def _empty_stream_label(configured_format: str, is_local: bool) -> str:
    """「空流」报错的**来源标签**：本地后端不许自称「在线模型」。

    实测踩过：LM Studio（kind=local）跑到「有推理零正文」时，最终诊断却写
    「在线模型流式响应中没有文本内容」——本机模型报错说自己是云端，用户按这句话
    根本找不到该去哪个设置页排查（真实流式路径复测发现，见 verify/_probe_reasoning_break.py）。
    """
    fmt = str(configured_format or "").strip().lower()
    if fmt == "lm_studio":
        return "LM Studio"
    if fmt == "ollama":
        return "Ollama"
    # llama.cpp / unsloth / 未知本地后端都归「本地模型」；其余才是在线。
    return "本地模型" if is_local else "在线模型"


def _empty_stream_phrase(configured_format: str, is_local: bool) -> str:
    """拼出「<来源>流式响应中没有文本内容」（Latin 标签后留空格，中文标签不留）。"""
    label = _empty_stream_label(configured_format, is_local)
    gap = " " if label[-1:].isascii() else ""
    return f"{label}{gap}流式响应中没有文本内容"


def _mentions_max_tokens(detail: str) -> bool:
    """错误体是否在抱怨「输出上限字段」（A0 注入值自愈的判据）。"""
    text = str(detail or "").lower()
    return any(marker in text for marker in _MAX_TOKENS_REJECTION_MARKERS)


def _drop_injected_max_tokens(payload: dict[str, Any]) -> None:
    """剥掉 A0 注入的输出上限字段（四种协议落点 + 两种本地形态）。

    只在 ``injected_max_tokens`` 为真时调用（值是我们填的，不是用户设的），
    所以不需要区分来源；用户显式设的值触发的 400 走「不吞错」分支。
    """
    payload.pop("max_tokens", None)
    payload.pop("max_output_tokens", None)
    generation_config = payload.get("generationConfig")
    if isinstance(generation_config, dict):
        generation_config.pop("maxOutputTokens", None)
    ollama_options = payload.get("options")
    if isinstance(ollama_options, dict):
        ollama_options.pop("num_predict", None)


def _window_from_error_detail(detail: str) -> int:
    """从服务端错误体里抠出它自报的上下文窗口值（抠不到返回 0）。

    后端说的窗口是**真值**，优先级高于本机探测/配置：llama.cpp 回
    ``"n_ctx":4096``，LM Studio 回 ``n_ctx: 4096``，DeepSeek/OpenAI 回
    ``maximum context length is 65536 tokens``。用户看到「当前窗口 4096」再去设置里
    对照，比看到配置里那个偏大的数字有用得多。
    """
    text = str(detail or "")
    for pattern in (
        r"n[_\s-]?ctx[\"']?\s*[:=]\s*(\d+)",
        r"maximum context length is\s*(\d+)",
        r"context length (?:is|of)\s*(\d+)",
        r"context window (?:is|of)\s*(\d+)",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            value = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def context_overflow_message(provider: str, window: int, *, is_local: bool) -> str:
    """上下文溢出的**唯一**用户可见文案（本地/在线共用一套口径）。

    为什么必须说清「窗口」与「下一步」：溢出在旧实现里表现为
    ① 本地后端不做完 prefill，界面静止在「等待本地模型资源」直到超时；
    ② 报错时只透传 ``HTTP 400: {原始 JSON}``。两者都让用户无从下手——真正有效的
    动作只有两个：开新会话（不再重发整段历史），或把真实窗口填进设置。
    """
    who = str(provider or "").strip() or ("本地模型" if is_local else "当前模型")
    try:
        window_value = max(0, int(window or 0))
    except (TypeError, ValueError):
        window_value = 0
    window_text = f"{window_value} tokens" if window_value > 0 else "未知"
    if is_local:
        return (
            f"本地模型上下文窗口不足（{who}，当前窗口 {window_text}）：本轮请求已超出窗口，"
            "模型无法继续处理。请点本条回复上的「新会话」，或新建对话后继续（聊天记录一条不删）；"
            "若窗口值与实际不符，可在 设置 → 模型 里填写该模型的真实「上下文窗口」后重试。"
        )
    return (
        f"模型上下文窗口不足（{who}，当前窗口 {window_text}）：本轮请求已超出窗口，"
        "模型无法继续处理。请点本条回复上的「新会话」，或新建对话后继续（聊天记录一条不删）。"
    )


def _error_body_evidence(raw: str) -> str:
    """把服务端错误体里**与错误本身相关**的部分拼成一段文本（供溢出判定）。

    只看 `error`（含它的兄弟字段，如 llama.cpp 的 `n_ctx`/`n_prompt_tokens`）与顶层
    `message`/`detail`，绝不把整段原文纳入判定——部分网关会把请求体原样回显，扫全文
    就有把用户正文里的词当成溢出证据的风险。
    """
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    parts: list[str] = []
    for value in (parsed.get("error"), parsed.get("message"), parsed.get("detail")):
        if isinstance(value, dict):
            try:
                parts.append(json.dumps(value, ensure_ascii=False))
            except (TypeError, ValueError):
                continue
        elif isinstance(value, str):
            parts.append(value)
    return "\n".join(parts)


def _context_overflow_error(
    exc: ContextOverflowError,
    profile: dict[str, Any],
    *,
    is_local: bool,
    provider: str,
) -> ContextOverflowError:
    """给原始溢出错误补上「后端名 + 当前窗口」，生成用户可见文案。"""
    try:
        profile_window = int(profile.get("context_window") or profile.get("context_size") or 0)
    except (TypeError, ValueError):
        profile_window = 0
    # 后端自报的窗口是**真值**，优先于本机探测/配置：用户对着真值才知道该往设置里填什么。
    reported = exc.window or _window_from_error_detail(exc.detail or str(exc))
    window = reported or profile_window
    backend = provider or exc.backend
    return ContextOverflowError(
        context_overflow_message(backend, window, is_local=is_local),
        window=window,
        backend=backend,
        detail=exc.detail or str(exc),
    )


def _has_stream_payload(line: Any) -> bool:
    """这一行是否算「模型真的开始输出了」。

    **关键**：SSE 的注释行（``: keepalive``）与空行是链路保活噪音——网关、
    反向代理、部分推理服务都会在等模型时周期性发它们。若把注释行当成「已有输出」，
    首字节超时就被静默解除，而请求其实一个正文字节都没有（实测：假端点只发保活注释时，
    超时闸门完全不触发，客户端一直等到对端自己断开）。所以进度只认**有效载荷行**。
    """
    if isinstance(line, (bytes, bytearray)):
        raw = bytes(line).strip()
        return bool(raw) and not raw.startswith(b":")
    text = str(line).strip()
    return bool(text) and not text.startswith(":")


class _ErrorHTMLParser(HTMLParser):
    """Extract readable text from an upstream HTML error page."""

    _IGNORED_TAGS = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.parts: list[str] = []
        self._ignored_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._IGNORED_TAGS:
            self._ignored_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        value = " ".join(data.split())
        if not value:
            return
        if self._in_title:
            self.title = f"{self.title} {value}".strip()
        self.parts.append(value)

def _summarize_http_error(raw: str, content_type: str = "", host: str = "") -> str:
    """Keep upstream failures readable and actionable in the chat UI."""
    text = str(raw or "").strip()
    if not text:
        return "空响应"

    is_html = "html" in str(content_type).lower() or re.search(
        r"<!doctype\s+html|<html\b|<head\b|<body\b", text, flags=re.IGNORECASE
    )
    if is_html:
        parser = _ErrorHTMLParser()
        try:
            parser.feed(text)
        except Exception:
            parser = None
        if parser:
            title = " ".join(parser.title.split())
            visible = " ".join(parser.parts)
            if title and visible.lower().startswith(title.lower()):
                visible = visible[len(title):].lstrip(" :—-")
            summary = f"{title}: {visible}" if title and visible else title or visible
        else:
            summary = ""
        summary = summary or "上游返回了 HTML 错误页"
        lowered_host = str(host or "").lower()
        if "deepseek.com" in lowered_host and not lowered_host.startswith("api."):
            summary += "；请将 API URL 改为 https://api.deepseek.com，不要填写 deepseek.com 网页地址"
        else:
            summary += "；请检查 API URL 是否为模型接口地址，而不是网页地址或被拦截的代理地址"
    else:
        summary = re.sub(r"\s+", " ", text)
        try:
            parsed = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict):
                summary = str(error.get("message") or error.get("detail") or error.get("type") or summary)
            elif error:
                summary = str(error)
            elif parsed.get("message"):
                summary = str(parsed["message"])

    # Cloudflare error 1010 is a gateway policy decision, not a bad model
    # name or API protocol. Keep the upstream detail but add a concise action
    # so users know to try the browser-compatible client signature or ask the
    # provider to allow this endpoint.
    lowered = summary.lower()
    raw_lowered = str(raw or "").lower()
    if (
        "browser_signature_banned" in lowered
        or "browser_signature_banned" in raw_lowered
        or "cloudflare_error\":true" in lowered
        or "cloudflare_error\":true" in raw_lowered
    ):
        summary += "；上游 Cloudflare 拦截了当前客户端签名，请让服务方放行 API 请求，或暂时手动填写模型名称"

    return summary[:800]

def _network_error_code(error: BaseException) -> int | None:
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    value = getattr(reason, "winerror", None) or getattr(reason, "errno", None)
    return int(value) if isinstance(value, int) else None


# ---- 在线请求体过大（HTTP 413）治理 ----
# 图片进模型上下文前已归一化到 ≤900KB/张，base64 后约 1.2MB/张；视觉会话里每调用一次
# vision_analyze 就往 messages 尾部追加一批（≤4 张）图片，**批次只增不减**，审核十张图
# 就能把请求体推到 12MB+，中继/网关直接回 HTTP 413。413 是「HTTP 载荷字节超限」，
# 与「上下文窗口溢出」（is_context_overflow）完全是两回事，不能共用一条自愈链。

def _json_payload_bytes(value: Any) -> int:
    """按实际发包口径估算字节数（``ensure_ascii=False`` 与请求体一致）。"""
    try:
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _payload_message_list(payload: Any, request_format: str) -> list[Any] | None:
    """取该请求格式里承载对话消息的那个数组（瘦身只在其中动手，绝不碰 tools 等字段）。"""
    if not isinstance(payload, dict):
        return None
    key = {
        "openai_chat": "messages",
        "claude": "messages",
        "lm_studio": "messages",
        "codex_responses": "input",
        "gemini": "contents",
    }.get(request_format)
    value = payload.get(key) if key else None
    return value if isinstance(value, list) else None


def _image_slot_kind(part: Any, request_format: str) -> bool:
    """该 content part 是否是「图片载荷」槽位。"""
    if not isinstance(part, dict):
        return False
    if request_format in {"openai_chat", "lm_studio"}:
        return str(part.get("type") or "") == "image_url"
    if request_format == "claude":
        return str(part.get("type") or "") == "image"
    if request_format == "codex_responses":
        return str(part.get("type") or "") == "input_image"
    if request_format == "gemini":
        return "inlineData" in part or "inline_data" in part
    return False


def _image_omitted_part(request_format: str, role: str) -> dict[str, Any]:
    """图片槽位的替换件：一段自述文本（模型据此知道「这里本来有图，被省略了」）。"""
    if request_format == "gemini":
        return {"text": IMAGE_OMITTED_PLACEHOLDER}
    if request_format == "codex_responses":
        # responses 协议区分 assistant/user 文本块类型，写错会被服务端拒。
        return {
            "type": "output_text" if role == "assistant" else "input_text",
            "text": IMAGE_OMITTED_PLACEHOLDER,
        }
    return {"type": "text", "text": IMAGE_OMITTED_PLACEHOLDER}


def _iter_image_slots(payload: Any, request_format: str):
    """按「从旧到新」产出图片槽位 ``(parts_list, index, role)``。

    文档序即时间序：messages/input/contents 由旧到新排列，各消息内部 parts 同理。
    """
    messages = _payload_message_list(payload, request_format)
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        parts = message.get("content")
        if not isinstance(parts, list):
            parts = message.get("parts")
        if not isinstance(parts, list):
            continue
        for index, part in enumerate(parts):
            if _image_slot_kind(part, request_format):
                yield parts, index, role


def _slim_payload_images(payload: Any, request_format: str, target_bytes: int) -> int:
    """把最旧的图片依次换成文本占位，直到估算字节降到 ``target_bytes`` 之下。

    返回省略的张数（0 = 没有可省的图片 / 目标非法）。**至少省一张**：调用方要么已经
    确认超预算（守卫），要么刚被 413 拒（此时说明我们自己的估算与供应商上限不一致，
    必须真的缩小请求体）。文本一律不动，对话语义不受影响；每步的字节增量按「被换掉
    那个 part 的序列化长度差」精确扣减，不重复整包序列化。
    """
    if target_bytes <= 0:
        return 0
    slots = list(_iter_image_slots(payload, request_format))
    if not slots:
        return 0
    total = _json_payload_bytes(payload)
    removed = 0
    for parts, index, role in slots:
        if removed and total <= target_bytes:
            break
        before = _json_payload_bytes(parts[index])
        replacement = _image_omitted_part(request_format, role)
        parts[index] = replacement
        total += _json_payload_bytes(replacement) - before
        removed += 1
    return removed


def online_payload_budget_bytes(options: dict[str, Any] | None) -> int:
    """本次请求的载荷预算：常量默认，options 可覆盖（0/负数 = 关闭预检查）。"""
    override = (options or {}).get("online_payload_budget_bytes")
    if isinstance(override, (int, float)) and not isinstance(override, bool):
        return int(override)
    return ONLINE_PAYLOAD_BUDGET_BYTES


def _payload_too_large_message(target_detail: str, *, slimmed: bool) -> str:
    """413 的用户可读文案：不回显供应商原始 JSON（那对用户没有可行动信息）。"""
    action = "已自动省略较早的图片后仍被拒绝" if slimmed else "图片/附件总量超出供应商上限"
    return (
        f"{target_detail}返回 HTTP 413：请求体过大（{action}）。"
        "请减少本轮携带的图片数量、改用单张识别，或换一个允许更大请求体的供应商后重试。"
    )


def _dump_failed_payload(
    endpoint: str,
    status_code: int,
    detail: str,
    payload: Any,
    reason: str,
) -> None:
    """把失败请求体落盘（超长字符串截断），供复现定位。

    payload 只含请求正文——API Key 在请求头里，不会落盘；超长字段（base64 图片、
    超大工具结果）由 ``_sanitize_payload`` 压成 ``<str:N>…`` 占位，文件总量再设硬上限。
    落盘失败只记日志，绝不影响主流程。
    """
    try:
        dump_dir = os.environ.get("NAIBA_ERROR_DUMP_DIR") or os.getcwd()
        dump_path = Path(dump_dir) / ERROR_DUMP_FILENAME
        text = json.dumps(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "endpoint": endpoint,
                "status": status_code,
                "reason": reason,
                "detail": detail,
                "payload": _sanitize_payload(payload, limit=ERROR_DUMP_STRING_LIMIT),
            },
            ensure_ascii=False,
            indent=1,
        )
        if len(text.encode("utf-8")) > ERROR_DUMP_MAX_BYTES:
            text = text[:ERROR_DUMP_MAX_BYTES] + "\n…<落盘内容超过上限，已截断>"
        dump_path.write_text(text, encoding="utf-8")
    except OSError:
        logger.error(
            "失败请求体落盘失败：endpoint=%s status=%s", endpoint, status_code, exc_info=True,
        )
        return
    logger.error("失败请求体已落盘：%s（HTTP %s，%s）", dump_path, status_code, reason)

# 所有出站 HTTP/HTTPS 统一走 net_io 入口：外部请求按「运行设置 → 代理」策略路由
# （关闭代理=强制直连；开启=使用手动代理地址；未填地址时按设置回退系统代理），
# 本地服务（127.0.0.1/localhost/私有网段，覆盖 Ollama、LM Studio、ComfyUI 等）
# 始终直连。代理设置保存后热生效，无需重启。
# 旧版本在系统代理发生瞬时拒连/重置时会“偷偷”改用直连重试一次；该隐式行为已
# 移除——若系统代理或 TUN 仍在接管流量，那次重试本就无效且违背用户配置意图。
def _urlopen_proxy_resilient(
    request: urllib.request.Request,
    timeout: float,
) -> Any:
    """Open ``request`` through the unified network policy (see net_io.py).

    Kept under the historical name so existing call sites stay unchanged; it no
    longer performs an implicit direct-connection fallback.
    """
    return net_io.open(request, timeout=timeout)

class _NullLock:
    def acquire(self):
        return True

    def release(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

class ModelRuntime(StreamMixins, ProtocolMixins):
    """在线模型调用。"""

    _local_execution_lock = threading.RLock()
    # 本地锁占用者标签。本地锁是全进程唯一的类属性，而占用它的调用未必有 status 通道
    # （视觉识别、子代理都传 status=None）——于是界面只看得到「等待本地模型资源」，
    # 既不知道是谁在用、也不知道要等多久，只能重启。这里记一个标签，让等锁的一方
    # 在状态里**指名道姓**：调用方通过 options["lock_label"] 声明自己的角色。
    _local_holder_guard = threading.Lock()
    _local_holder_label = ""

    @classmethod
    def _set_local_holder(cls, label: str) -> None:
        with cls._local_holder_guard:
            cls._local_holder_label = str(label or "")

    @classmethod
    def _clear_local_holder(cls, label: str) -> None:
        with cls._local_holder_guard:
            if cls._local_holder_label == str(label or ""):
                cls._local_holder_label = ""

    @classmethod
    def _local_holder_name(cls) -> str:
        with cls._local_holder_guard:
            return cls._local_holder_label

    def __init__(self) -> None:
        # 每个 HTTP 请求线程独立保存最近一次模型调用信息，避免并发对话互相覆盖。
        self._local = threading.local()

    @property
    def last_diagnostics(self) -> dict[str, Any]:
        value = getattr(self._local, "last_diagnostics", {})
        return dict(value) if isinstance(value, dict) else {}

    @last_diagnostics.setter
    def last_diagnostics(self, value: dict[str, Any]) -> None:
        self._local.last_diagnostics = dict(value or {})

    @property
    def last_reasoning(self) -> str:
        return str(getattr(self._local, "last_reasoning", ""))

    @last_reasoning.setter
    def last_reasoning(self, value: str) -> None:
        self._local.last_reasoning = value

    @property
    def last_reasoning_id(self) -> str:
        """最近一次模型响应的 reasoning item 服务端 id（思考模式回传必需）。"""
        return str(getattr(self._local, "last_reasoning_id", ""))

    @last_reasoning_id.setter
    def last_reasoning_id(self, value: str) -> None:
        self._local.last_reasoning_id = value

    @property
    def last_usage(self) -> dict[str, int]:
        value = getattr(self._local, "last_usage", {})
        return dict(value) if isinstance(value, dict) else {}

    @last_usage.setter
    def last_usage(self, value: dict[str, int]) -> None:
        self._local.last_usage = dict(value)

    @property
    def last_finish_reason(self) -> str:
        """最近一次模型请求的终止原因（"stop"/"length"/"tool_calls"/""）。

        消费方（Agent 循环）用 ``getattr(model_runtime, "last_finish_reason", "")`` 读取，
        与 ``last_usage`` 完全同构；空字符串表示供应商未提供该字段（流被掐断或中继吞字段）。
        """
        return str(getattr(self._local, "last_finish_reason", "") or "")

    @last_finish_reason.setter
    def last_finish_reason(self, value: str) -> None:
        self._local.last_finish_reason = str(value or "")

    def complete(
        self,
        profile: dict[str, Any],
        messages: list[dict[str, Any]],
        options: dict[str, Any],
        status: StatusCallback | None = None,
    ) -> str:
        # 按 profile.kind 路由，禁止在线/本地跨模式 fallback。
        kind = str(profile.get("kind") or "").strip().lower()
        request_format = str(profile.get("request_format") or "openai_chat").strip().lower()
        _debug_complete_marker(kind, request_format, messages, status)
        if not kind:
            # 旧 profile 未携带 kind 时按请求格式推断，保持兼容。
            kind = "local" if request_format in LOCAL_REQUEST_FORMATS else "online"
            profile = {**profile, "kind": kind}
        if kind == "local":
            if request_format not in LOCAL_REQUEST_FORMATS:
                raise ValueError(f"本地模型配置使用了非本地请求格式：{request_format}")
        elif kind == "online":
            if request_format in LOCAL_REQUEST_FORMATS:
                raise ValueError(f"在线模型配置使用了本地请求格式：{request_format}")
        else:
            raise ValueError(f"不支持的模型类型：{kind}")
        reasoning_enabled = bool(options.get("reasoning_enabled", True))
        effective_status = status
        if status is not None and not reasoning_enabled:
            def effective_status(payload: dict[str, Any]) -> None:
                if not str(payload.get("type") or "").startswith("reasoning"):
                    status(payload)
        is_local = kind == "local"
        # 本地锁占用者标签：调用方声明角色，等锁的一方能指名道姓地知道「谁在用」。
        holder_label = str(options.get("lock_label") or "").strip() or DEFAULT_LOCK_LABEL
        if is_local and status:
            status({"type": "status", "message": "等待本地模型资源"})
        # Local backends share GPU/RAM and commonly expose one active model.
        # Serialize requests so a vision call, sub-agent, and main turn cannot
        # make the local server compete with itself.
        lock = self._local_execution_lock if is_local else _NullLock()
        diagnostics: dict[str, Any] = {
            "provider": str(profile.get("name") or ""),
            "model": str(profile.get("model") or ""),
            "request_format": request_format,
            "local": is_local,
            "stream": bool(options.get("stream", False)),
            "image_count": sum(
                1 for item in messages
                for part in self._content_parts(item.get("content"))
                if isinstance(part, dict) and part.get("type") == "image"
            ),
            "tool_count": len(options.get("tools") or []),
            "context_window": int(profile.get("context_window") or profile.get("context_size") or 0),
            "max_output_tokens": int(options.get("max_tokens") or profile.get("max_output_tokens") or 0),
            "reasoning_effort": str(profile.get("reasoning_effort") or "auto"),
        }
        lock_started = time.perf_counter()
        if is_local:
            self._acquire_local_lock(lock, options, effective_status, holder_label)
        else:
            lock.acquire()
        diagnostics["lock_wait_ms"] = round((time.perf_counter() - lock_started) * 1000, 1)
        if is_local:
            self._set_local_holder(holder_label)
        total_started = time.perf_counter()
        try:
            content, reasoning, reasoning_id, usage = self._complete_online(
                profile, messages, options, effective_status, diagnostics
            )
        finally:
            lock.release()
            if is_local:
                self._clear_local_holder(holder_label)
            diagnostics["total_ms"] = round((time.perf_counter() - total_started) * 1000, 1)
            self.last_diagnostics = diagnostics
        # DeepSeek thinking mode REQUIRES assistant reasoning_content to be passed
        # back on every tool-call message ("The reasoning_content in the thinking
        # mode must be passed back to the API"). Even when the UI has reasoning
        # display off, we must keep the captured reasoning so `last_reasoning`
        # feeds `reasoning_content` into the agent loop's assistant messages.
        # The streaming display is already suppressed by `effective_status`.
        if not reasoning_enabled and not ModelRuntime._is_deepseek_profile(profile):
            reasoning = ""
        self.last_reasoning = reasoning
        self.last_reasoning_id = reasoning_id
        self.last_usage = usage
        # 终止原因由 _complete_online 在返回前写入线程局部（静态层拿不到 self），此处抄到实例上。
        self.last_finish_reason = _last_finish_reason()
        return content

    @classmethod
    def _busy_notice(cls, waited: float) -> str:
        """等本地锁时的可见状态文案：**指名道姓**说出占用者。"""
        holder = cls._local_holder_name()
        who = f"{holder} 正在使用" if holder else "另一处调用仍在使用"
        return (
            f"本地模型忙：{who}，已排队等待 {max(0, int(waited))} 秒"
            "（本地后端同一时刻只处理一个请求）"
        )

    @staticmethod
    def _acquire_local_lock(
        lock: Any,
        options: dict[str, Any],
        status: StatusCallback | None = None,
        holder: str = "",
    ) -> None:
        """等本地锁期间**可取消**且**可见**。

        原实现 `lock.acquire()` 无超时也不看 ``cancel_event``：本地锁是类属性（全进程唯一），
        被另一个调用（子代理、或 ``status=None`` 因而在界面上完全隐形的视觉调用）占住时，
        主对话线程会无限静默地等下去 ——「停止」按钮只能把 DB 里的 run 行置为 cancelled，
        停不掉这个线程，界面就永久停在「等待本地模型资源」。改为 0.5 秒粒度的可中断等待：
        取消信号到达即抛（与重试路径同一约定文案），并每 LOCAL_LOCK_WAIT_NOTICE_SECONDS
        秒回报一次真实等待时长，让「卡住了」和「在排队」在界面上可分辨。

        ``holder`` 是本次调用自己的角色名，用于登记「现在是谁在占锁」；等锁的一侧会
        通过 ``_busy_notice`` 把占用者名字打出来（首次发现忙碌时立刻回报一次，之后每
        5 秒一次），这样「隐形占锁者」（视觉识别、子代理）在界面上不再不可归因。
        """
        cancel_event = options.get("cancel_event")
        if not isinstance(cancel_event, threading.Event):
            cancel_event = None
        if lock.acquire(timeout=0.5):
            return
        waited = 0.0
        announced = 0.0
        if status is not None:
            # 首次发现被占住就立刻说明「谁在用」，不要先沉默 5 秒。
            status({"type": "status", "message": ModelRuntime._busy_notice(waited)})
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("任务已取消")
            if lock.acquire(timeout=0.5):
                return
            waited += 0.5
            if status is not None and waited - announced >= LOCAL_LOCK_WAIT_NOTICE_SECONDS:
                announced = waited
                status({"type": "status", "message": ModelRuntime._busy_notice(waited)})

    @staticmethod
    def _urlopen_cancelable(
        request: urllib.request.Request,
        timeout: float,
        cancel_event: threading.Event | None = None,
        opener: urllib.request.OpenerDirector | None = None,
    ):
        """Run the network request in a daemon worker so a cancelled vision call returns promptly.

        Requests always go through the unified net_io policy (proxy settings /
        local direct connect). ``opener`` is retained for callers that need an
        explicit opener override; when None the policy applies.
        """
        if opener is not None:
            open_function = opener.open
        else:
            open_function = lambda req, **kw: net_io.open(req, **kw)
        if cancel_event is None:
            return open_function(request, timeout=timeout)
        if cancel_event.is_set():
            raise RuntimeError("任务已取消")
        done = threading.Event()
        abandoned = threading.Event()
        result: dict[str, Any] = {}

        def worker() -> None:
            try:
                response = open_function(request, timeout=timeout)
                if abandoned.is_set():
                    # Main thread already gave up (user cancelled). Close the
                    # fresh connection right away so sockets are not leaked over
                    # a long session (which eventually makes the whole process
                    # unable to connect — to remote or local hosts).
                    try:
                        response.close()
                    except Exception:
                        pass
                else:
                    result["response"] = response
            except BaseException as exc:  # noqa: BLE001 - propagate worker errors
                result["error"] = exc
            finally:
                done.set()

        threading.Thread(target=worker, name="naiba-http-request", daemon=True).start()
        while not done.wait(0.1):
            if cancel_event.is_set():
                abandoned.set()
                raise RuntimeError("任务已取消")
        if cancel_event.is_set():
            response = result.get("response")
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            raise RuntimeError("任务已取消")
        error = result.get("error")
        if error is not None:
            raise error
        return result["response"]

    @staticmethod
    def _read_response_cancelable(response: Any, cancel_event: threading.Event | None = None) -> bytes:
        if cancel_event is None:
            return response.read()
        if cancel_event.is_set():
            raise RuntimeError("任务已取消")
        done = threading.Event()
        result: dict[str, Any] = {}

        def worker() -> None:
            try:
                result["data"] = response.read()
            except BaseException as exc:  # noqa: BLE001
                result["error"] = exc
            finally:
                done.set()

        threading.Thread(target=worker, name="naiba-http-read", daemon=True).start()
        while not done.wait(0.1):
            if cancel_event.is_set():
                try:
                    response.close()
                except Exception:
                    pass
                raise RuntimeError("任务已取消")
        if cancel_event.is_set():
            raise RuntimeError("任务已取消")
        error = result.get("error")
        if error is not None:
            raise error
        return result.get("data", b"")

    @staticmethod
    def _iter_stream_lines(
        response: Any,
        cancel_event: threading.Event | None = None,
        first_byte_timeout: float = 0.0,
        total_timeout: float = 0.0,
    ) -> Any:
        """逐行产出流式响应，在取消 / 首字节超时 / 总时长超时时尽快中断读取。

        读取线程阻塞在 ``response.readline()``（socket 读）里，循环体根本没有机会
        看到取消请求——本地模型 prefill 期间一个字节都不吐，此前「停止」对它完全
        无效，只能等满 30 分钟超时。这里让一个轻量看门狗线程做三件事：

        1. **取消**置位时关闭连接，把阻塞的读打断，再把随之而来的 I/O 异常翻译成
           统一的「任务已取消」，避免被误判成网络故障而进入重试/报错分支；
        2. ``first_byte_timeout > 0`` 时，若**连续**该秒数没有收到任何**有效载荷行**
           （SSE 注释与空行不算，见 ``_has_stream_payload``），同样关闭连接并抛
           ``LocalModelFirstByteTimeout``。

        第 2 条是给本地后端用的：本地请求总超时 1800 秒，「prefill 做不完」与
        「正在 prefill」在界面上无法区分，用户要干等半小时才知道这一轮根本没戏。
        计时从**进入生成器**开始（此时 HTTP 头已收到），只约束「模型有没有在出内容」；
        一有正文字节就重新起算，因此它同时能抓住「中途长时间静默」。

        3. ``total_timeout > 0`` 时按**墙钟**计总时长，超时抛 ``StreamTotalTimeout``。
           这一条是给在线请求的：``ONLINE_MODEL_TIMEOUT_SECONDS`` 是 urllib **每次读操作
           的空闲超时**，推理 delta 持续到达就不断重置它——一段 43 万字符的思考循环可以
           无限流下去（DeepSeek/Moonshot 的思考 token 还按输出计费），界面上「合法长思考」
           与「死循环」无法区分，只能靠用户手动停止。
        """
        timeout_seconds = max(0.0, float(first_byte_timeout or 0.0))
        total_seconds = max(0.0, float(total_timeout or 0.0))
        if cancel_event is None and timeout_seconds <= 0 and total_seconds <= 0:
            yield from response
            return
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("任务已取消")
        stop = threading.Event()
        state = {"started": False, "timed_out": False, "total_timed_out": False}
        progress = {"last": time.perf_counter()}
        started_at = time.perf_counter()

        def abort_watchdog() -> None:
            # 只做一件事：取消或长时间没有有效载荷时关闭连接，让阻塞中的 readline 立刻抛错返回。
            while not stop.wait(0.1):
                if cancel_event is not None and cancel_event.is_set():
                    try:
                        response.close()
                    except Exception:  # noqa: BLE001
                        pass
                    return
                if timeout_seconds > 0 and time.perf_counter() - progress["last"] >= timeout_seconds:
                    state["timed_out"] = True
                    try:
                        response.close()
                    except Exception:  # noqa: BLE001
                        pass
                    return
                if total_seconds > 0 and time.perf_counter() - started_at >= total_seconds:
                    state["total_timed_out"] = True
                    try:
                        response.close()
                    except Exception:  # noqa: BLE001
                        pass
                    return

        threading.Thread(
            target=abort_watchdog, name="naiba-stream-abort", daemon=True
        ).start()
        try:
            for line in response:
                if _has_stream_payload(line):
                    state["started"] = True
                    progress["last"] = time.perf_counter()
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("任务已取消")
                yield line
            # 超时有时表现为「连接被关掉 → 迭代干净结束」，这里补一次判定，
            # 避免退化成下游那句含糊的「流式响应中没有文本内容」。
            if state["total_timed_out"]:
                raise _stream_total_timeout_error(total_seconds)
            if state["timed_out"]:
                raise local_first_byte_timeout_error(timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            # 连接被看门狗关掉时会抛 ValueError/OSError，按关掉它的原因归因。
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("任务已取消") from exc
            if state["total_timed_out"]:
                raise _stream_total_timeout_error(total_seconds) from exc
            if state["timed_out"]:
                raise local_first_byte_timeout_error(timeout_seconds) from exc
            raise
        finally:
            stop.set()

    @staticmethod
    def list_online_models(profile: dict[str, Any]) -> list[dict[str, Any]]:
        base_url = str(profile.get("base_url") or "").rstrip("/")
        api_key = str(profile.get("api_key") or "").strip()
        configured_format = str(profile.get("request_format") or "openai_chat").strip().lower()
        # llama.cpp and Unsloth servers expose the OpenAI Chat API, but they
        # are local inference backends. Normalize only the wire protocol;
        # retain the profile kind below so timeout/retry routing stays local.
        request_format = "openai_chat" if configured_format in {"llama_cpp", "unsloth"} else configured_format
        if not base_url:
            raise ValueError("请先填写 API URL")

        headers = {"Accept": "application/json", "User-Agent": API_USER_AGENT}
        if request_format == "gemini":
            endpoint = ModelRuntime._with_endpoint(base_url, "/v1beta/models")
            if api_key:
                headers["x-goog-api-key"] = api_key
        elif request_format == "ollama":
            endpoint = ModelRuntime._local_endpoint(base_url, "/api/tags")
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        elif request_format == "lm_studio":
            endpoint = ModelRuntime._local_endpoint(base_url, "/api/v1/models")
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        else:
            endpoint = ModelRuntime._with_endpoint(base_url, "/v1/models")
            if request_format == "claude":
                if api_key:
                    headers["x-api-key"] = api_key
                headers["anthropic-version"] = "2023-06-01"
            elif api_key:
                headers["Authorization"] = f"Bearer {api_key}"

        request = urllib.request.Request(endpoint, headers=headers, method="GET")
        try:
            with _urlopen_proxy_resilient(request, 60) as response:
                raw = response.read().decode("utf-8", errors="replace")
                content_type = response.headers.get("Content-Type", "") if hasattr(response, "headers") else ""
                try:
                    result = json.loads(raw)
                except json.JSONDecodeError as exc:
                    detail = _summarize_http_error(raw, content_type, urllib.parse.urlsplit(endpoint).hostname or "")
                    raise RuntimeError(f"模型接口返回的不是 JSON：{detail}") from exc
        except urllib.error.HTTPError as exc:
            detail = _summarize_http_error(
                exc.read().decode("utf-8", errors="replace"),
                exc.headers.get("Content-Type", "") if exc.headers else "",
                urllib.parse.urlsplit(endpoint).hostname or "",
            )
            raise RuntimeError(f"模型列表返回 HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接模型列表接口 {endpoint}：{exc.reason}") from exc
        except OSError as exc:
            raise RuntimeError(f"模型列表接口 {endpoint} 的连接被本机或远端中止：{exc}") from exc

        items = []
        if isinstance(result, dict):
            items = result.get("data") or result.get("models") or []
        models = []
        seen = set()
        for item in items:
            if isinstance(item, str):
                model_id = item
                display_name = item
            elif isinstance(item, dict):
                if request_format == "gemini":
                    methods = item.get("supportedGenerationMethods") or []
                    if methods and not any("generateContent" in str(method) for method in methods):
                        continue
                model_id = str(
                    item.get("id") or item.get("key") or item.get("name")
                    or (item.get("model") if request_format == "ollama" else "") or ""
                )
                if request_format == "gemini" and model_id.startswith("models/"):
                    model_id = model_id[7:]
                display_name = str(
                    item.get("display_name") or item.get("displayName") or item.get("name") or model_id
                )
            else:
                continue
            model_id = model_id.strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            capability: dict[str, Any] = {}
            if isinstance(item, dict):
                capabilities = item.get("capabilities")
                vision = capabilities.get("vision") if isinstance(capabilities, dict) else None
                if not isinstance(vision, bool):
                    loaded = item.get("loaded_instances")
                    if isinstance(loaded, list):
                        for instance in loaded:
                            instance_caps = instance.get("capabilities") if isinstance(instance, dict) else None
                            candidate = instance_caps.get("vision") if isinstance(instance_caps, dict) else None
                            if isinstance(candidate, bool):
                                vision = candidate
                                break
                if isinstance(vision, bool):
                    capability["supports_images"] = vision
                for target, keys in {
                    "context_window": (
                        "context_window", "contextWindow", "context_length", "contextLength",
                        "max_context_length", "maxContextLength", "inputTokenLimit",
                    ),
                    "max_output_tokens": (
                        "max_output_tokens", "maxOutputTokens", "outputTokenLimit",
                        "max_completion_tokens", "maxCompletionTokens",
                    ),
                }.items():
                    for key in keys:
                        raw = item.get(key)
                        if raw not in (None, ""):
                            try:
                                parsed = int(raw)
                            except (TypeError, ValueError):
                                continue
                            if parsed > 0:
                                capability[target] = parsed
                                break
            models.append({"id": model_id, "name": display_name.strip() or model_id, **capability})
        return models[:500]

    @staticmethod
    def unload_local_model(profile: dict[str, Any]) -> dict[str, str]:
        """Ask a supported local model server to unload its active model."""
        base_url = str(profile.get("base_url") or "").rstrip("/")
        model = str(profile.get("model") or "").strip()
        api_key = str(profile.get("api_key") or "").strip()
        request_format = str(profile.get("request_format") or "").strip().lower()
        local_kind = str(profile.get("local_kind") or "").strip().lower()
        if not base_url or not model:
            raise ValueError("本地模型需要 Base URL 和模型名称")

        if request_format == "ollama" or local_kind == "ollama":
            endpoint = ModelRuntime._local_endpoint(base_url, "/api/generate")
            payload = {"model": model, "keep_alive": 0}
            provider_name = "Ollama"
        elif request_format == "lm_studio" or local_kind == "lm_studio":
            endpoint = ModelRuntime._local_endpoint(base_url, "/api/v1/models/unload")
            payload = {"instance_id": model}
            provider_name = "LM Studio"
        else:
            raise ValueError("当前供应商不是支持手动卸载的本地模型服务")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": API_USER_AGENT,
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with net_io.open(request, timeout=15) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            detail = _summarize_http_error(
                exc.read().decode("utf-8", errors="replace"),
                exc.headers.get("Content-Type", "") if exc.headers else "",
                urllib.parse.urlsplit(endpoint).hostname or "",
            )
            raise RuntimeError(f"{provider_name} 卸载模型失败 HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接 {provider_name} 卸载接口：{exc.reason}") from exc
        except OSError as exc:
            raise RuntimeError(f"{provider_name} 卸载连接被本机或服务中止：{exc}") from exc
        return {"provider": provider_name, "model": model}

    @staticmethod
    def _complete_online(
        profile: dict[str, Any],
        messages: list[dict[str, Any]],
        options: dict[str, Any],
        status: StatusCallback | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> tuple[str, str, str, dict[str, int]]:
        base_url = str(profile.get("base_url") or "").rstrip("/")
        model = str(profile.get("model") or "").strip()
        api_key = str(profile.get("api_key") or "").strip()
        if not base_url or not model:
            raise ValueError("在线模型需要 Base URL 和模型名称")
        configured_format = str(profile.get("request_format") or "openai_chat").strip().lower()
        # llama.cpp and Unsloth servers expose the OpenAI Chat API, but they
        # are local inference backends. Normalize only the wire protocol;
        # retain the profile kind below so timeout/retry routing stays local.
        request_format = "openai_chat" if configured_format in {"llama_cpp", "unsloth"} else configured_format
        temperature_raw = options.get("temperature", profile.get("temperature"))
        temperature = None if temperature_raw in (None, "") else float(temperature_raw)
        max_tokens_raw = options.get("max_tokens", profile.get("max_output_tokens"))
        max_tokens = None if max_tokens_raw in (None, "") else int(max_tokens_raw)
        # DeepSeek rejects zero/negative values and any value above its API
        # ceiling.  UI/router defaults can be larger than a provider's limit,
        # so clamp only the wire value while keeping the profile unchanged.
        if max_tokens is not None:
            # A legacy profile can persist zero as "unset".  Never send that
            # sentinel to an API: DeepSeek answers it with HTTP 400.
            if max_tokens <= 0:
                max_tokens = None
            elif ModelRuntime._is_deepseek_profile(profile):
                max_tokens = min(max_tokens, 393216)
        stream_enabled = bool(options.get("stream", False))
        reasoning_effort = str(profile.get("reasoning_effort") or "auto").strip().lower()
        reasoning_enabled = bool(
            options.get("reasoning_enabled", reasoning_effort in {"low", "medium", "high"})
        )
        # 思考预设（卡片 thinking > 模型名 > request_format 默认）：思考强度的 wire 落点、
        # 该端点接受的输出上限、以及「现在这一次是否确认会思考」的判据全从这里来。
        thinking_preset = ModelRuntime._thinking_preset(profile)
        # A0：思考时的默认输出上限。只在「未设置 + 确认会思考 + 预设声明了 ceiling」时才填，
        # 且判据必须是**模型级**（见 thinking.thinking_active）——协议族预设命中不构成依据，
        # 否则 gpt-4o 这类不思考的模型每个请求都会重演「填 32768 → 400 → 剥字段」。
        injected_max_tokens = False
        if max_tokens is None:
            ceiling = thinking_module.max_output_ceiling(thinking_preset)
            if ceiling > 0 and thinking_module.thinking_active(
                profile, reasoning_effort, preset=thinking_preset
            ):
                max_tokens = min(DEFAULT_THINKING_MAX_OUTPUT_TOKENS, ceiling)
                injected_max_tokens = True
        headers = {"Content-Type": "application/json", "User-Agent": API_USER_AGENT}
        native_tools = ModelRuntime._tool_schemas(options.get("tools"), request_format)
        response_format = request_format

        if request_format == "openai_chat":
            endpoint = ModelRuntime._with_endpoint(base_url, "/v1/chat/completions")
            payload = {
                "model": model,
                "messages": ModelRuntime._openai_messages(
                    messages,
                    # DeepSeek-compatible gateways commonly reject an empty
                    # reasoning_content field on ordinary assistant history.
                    # Real persisted reasoning is still preserved by
                    # _openai_messages; only synthetic empty backfills are
                    # disabled for DeepSeek.
                    include_reasoning_content=(
                        reasoning_enabled and not ModelRuntime._is_deepseek_profile(profile)
                    ),
                ),
                "stream": stream_enabled,
            }
            if temperature is not None:
                payload["temperature"] = temperature
            if max_tokens is not None:
                payload["max_tokens"] = max_tokens
            if native_tools:
                payload["tools"] = native_tools
                payload["tool_choice"] = "auto"
                payload["parallel_tool_calls"] = True
            # 思考强度按 provider 画像的预设解析（卡片 thinking > 模型名 > request_format
            # 默认）。Kimi K2.x 落到「不发字段」的预设，不再「首请求必然先 400 一次」。
            reasoning_params = ModelRuntime._thinking_patch(profile, reasoning_effort)
            # DeepSeek selects thinking behavior from the model itself; its
            # OpenAI-compatible endpoint does not accept OpenAI's
            # `reasoning_effort` request field.（预设已声明全 null，这条显式抑制是兜底。）
            if ModelRuntime._is_deepseek_profile(profile):
                reasoning_params = {}
            if reasoning_params:
                payload.update(reasoning_params)
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        elif request_format == "codex_responses":
            endpoint = ModelRuntime._with_endpoint(base_url, "/v1/responses")
            instructions = "\n\n".join(
                ModelRuntime._content_text(item.get("content"))
                for item in messages if item.get("role") == "system"
            )
            payload = {
                "model": model,
                "input": ModelRuntime._responses_input([
                    item for item in messages if item.get("role") != "system"
                ]),
                "stream": stream_enabled,
            }
            if temperature is not None:
                payload["temperature"] = temperature
            if max_tokens is not None:
                payload["max_output_tokens"] = max_tokens
            if instructions:
                payload["instructions"] = instructions
            if native_tools:
                payload["tools"] = native_tools
                payload["tool_choice"] = "auto"
                payload["parallel_tool_calls"] = True
            reasoning_params = ModelRuntime._thinking_patch(profile, reasoning_effort)
            if reasoning_params:
                payload.update(reasoning_params)
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        elif request_format == "gemini":
            encoded_model = urllib.parse.quote(model, safe="")
            endpoint = ModelRuntime._with_endpoint(base_url, f"/v1beta/models/{encoded_model}:streamGenerateContent")
            system_parts = [
                {"text": ModelRuntime._content_text(item.get("content"))}
                for item in messages if item.get("role") == "system"
            ]
            contents = [
                ModelRuntime._gemini_message(item)
                for item in messages if item.get("role") != "system"
            ]
            generation_config = {}
            if temperature is not None:
                generation_config["temperature"] = temperature
            if max_tokens is not None:
                generation_config["maxOutputTokens"] = max_tokens
            payload = {"contents": contents}
            if generation_config:
                payload["generationConfig"] = generation_config
            if system_parts:
                payload["systemInstruction"] = {"parts": system_parts}
            if native_tools:
                payload["tools"] = [{"functionDeclarations": native_tools}]
                payload["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
            if api_key:
                headers["x-goog-api-key"] = api_key
        elif request_format == "claude":
            endpoint = ModelRuntime._with_endpoint(base_url, "/v1/messages")
            system = "\n\n".join(
                ModelRuntime._content_text(item.get("content"))
                for item in messages if item.get("role") == "system"
            )
            # Anthropic requires max_tokens; use its compatibility floor only
            # when the provider did not expose a limit and the user left it blank.
            # Anthropic 必须填 max_tokens（协议必填）：A0.b 的「off/auto 不填」对它不适用——
            # 本项只把默认值从 4096 抬到 min(32768, 预设 ceiling=8192)，不改「必填」语义。
            claude_max_tokens = max_tokens
            if claude_max_tokens is None:
                claude_max_tokens = min(
                    DEFAULT_THINKING_MAX_OUTPUT_TOKENS,
                    max(CLAUDE_MAX_TOKENS_FLOOR, thinking_module.max_output_ceiling(thinking_preset)),
                )
                injected_max_tokens = True
            payload = {
                "model": model,
                "messages": [
                    ModelRuntime._claude_message(item)
                    for item in messages if item.get("role") != "system"
                ],
                "max_tokens": claude_max_tokens,
                "stream": stream_enabled,
            }
            if temperature is not None:
                payload["temperature"] = temperature
            if system:
                # Anthropic 的前缀缓存必须显式标记 cache_control 才生效；
                # system 改为 content block 数组并打上缓存断点。
                payload["system"] = [{
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }]
            if native_tools:
                payload["tools"] = native_tools
                payload["tool_choice"] = {"type": "auto"}
            # 在最后一条非 tool_result 的 user 消息末尾追加缓存断点，
            # 让编辑重开时编辑点之前的前缀命中 Anthropic prompt cache。
            ModelRuntime._claude_apply_cache_control(payload["messages"])
            if api_key:
                headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        elif request_format == "ollama":
            endpoint = ModelRuntime._local_endpoint(base_url, "/api/chat")
            ollama_options = {}
            if temperature is not None:
                ollama_options["temperature"] = temperature
            if max_tokens is not None:
                ollama_options["num_predict"] = max_tokens
            payload = {
                "model": model,
                "messages": ModelRuntime._ollama_messages(messages),
                "stream": stream_enabled,
            }
            if native_tools:
                payload["tools"] = native_tools
            context_window = profile.get("context_window") or profile.get("context_size")
            if context_window:
                ollama_options["num_ctx"] = int(context_window)
            if ollama_options:
                payload["options"] = ollama_options
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            reasoning_params = ModelRuntime._thinking_patch(profile, reasoning_effort)
            if reasoning_params:
                payload.update(reasoning_params)
        elif request_format == "lm_studio":
            if native_tools:
                # LM Studio exposes an OpenAI-compatible endpoint for native
                # function calling. Keep its custom endpoint for tool-free chat.
                response_format = "openai_chat"
                endpoint = ModelRuntime._with_endpoint(base_url, "/v1/chat/completions")
                payload = {
                    "model": model,
                    "messages": ModelRuntime._openai_messages(
                        messages,
                        include_reasoning_content=(
                            reasoning_enabled and not ModelRuntime._is_deepseek_profile(profile)
                        ),
                    ),
                    "stream": stream_enabled,
                    "tools": native_tools,
                    "tool_choice": "auto",
                    "parallel_tool_calls": True,
                }
                if temperature is not None:
                    payload["temperature"] = temperature
                if max_tokens is not None:
                    payload["max_tokens"] = max_tokens
            else:
                endpoint = ModelRuntime._local_endpoint(base_url, "/api/v1/chat")
                system_prompt, lm_input = ModelRuntime._lm_studio_messages(messages)
                payload = {
                    "model": model,
                    "system_prompt": system_prompt,
                    "input": lm_input,
                    "stream": stream_enabled,
                }
                if temperature is not None:
                    payload["temperature"] = temperature
                if max_tokens is not None:
                    payload["max_output_tokens"] = max_tokens
                context_window = profile.get("context_window") or profile.get("context_size")
                if context_window:
                    payload["context_length"] = int(context_window)
                reasoning_params = ModelRuntime._thinking_patch(profile, reasoning_effort)
                if reasoning_params:
                    payload.update(reasoning_params)
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        else:
            raise ValueError(f"不支持的在线请求格式：{request_format}")

        _wire_msgs = payload.get("messages")
        if not isinstance(_wire_msgs, list):
            _wire_msgs = payload.get("input")
        if isinstance(_wire_msgs, list):
            _debug_wire_digest(_wire_msgs, status)
        else:
            _debug_wire_digest(messages, status)  # 兜底：任何格式都打原始 messages
        _debug_payload_dump(payload, status)

        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        stream_options_requested = bool(stream_enabled and response_format == "openai_chat")
        if stream_options_requested:
            payload["stream_options"] = {"include_usage": True}
            request = urllib.request.Request(
                endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )

        is_local = (
            str(profile.get("kind") or "").strip().lower() == "local"
            or configured_format in LOCAL_REQUEST_FORMATS
        )
        connection_test = bool(options.get("connection_test", False))
        cancel_event = options.get("cancel_event")
        if not isinstance(cancel_event, threading.Event):
            cancel_event = None
        provider_name = str(profile.get("name") or "").strip()
        parsed_endpoint = urllib.parse.urlsplit(endpoint)
        endpoint_host = parsed_endpoint.hostname or parsed_endpoint.netloc or endpoint
        endpoint_port = parsed_endpoint.port or (443 if parsed_endpoint.scheme == "https" else 80)
        target = "本地模型" if is_local else "在线模型"
        # 「空流」报错的来源标签：LM Studio / Ollama 必须自报家门，不许自称「在线模型」。
        empty_stream_phrase = _empty_stream_phrase(configured_format, is_local)
        target_detail = f"{target}“{provider_name}”" if provider_name else target
        # Include the endpoint PATH (not only host:port) so a mis-routed request
        # (e.g. an unexpected /v1/models) is immediately visible in the error.
        endpoint_path = parsed_endpoint.path or "/"
        target_detail += f"（{endpoint_host}:{endpoint_port}{endpoint_path}）"
        # 溢出文案里的「后端名」：优先用户给供应商起的名字，其次后端类型标签。
        # llama_cpp/unsloth 在 wire 层统一成 openai_chat，故先查 configured_format。
        overflow_source = (
            provider_name
            or _STREAM_SOURCE_LABELS.get(configured_format, "")
            or _STREAM_SOURCE_LABELS.get(request_format, "")
            or request_format
        )
        request_timeout = (
            LOCAL_MODEL_TIMEOUT_SECONDS
            if is_local
            else PROVIDER_TEST_TIMEOUT_SECONDS if connection_test else ONLINE_MODEL_TIMEOUT_SECONDS
        )
        timeout_override = options.get("request_timeout_seconds")
        if isinstance(timeout_override, (int, float)) and timeout_override > 0:
            request_timeout = max(1, min(int(timeout_override), LOCAL_MODEL_TIMEOUT_SECONDS))
        # 本地流式请求另加「首字节超时」：prefill 做不完时本地后端一个字节都不吐，
        # 只靠 1800 秒总超时等于让用户干等半小时（客户机实测整条会话看似永久卡死）。
        # 在线请求不加首字节超时：云端排队/长时间思考是合法的，真正的兜底是下面的
        # **墙钟总时长**（ONLINE_STREAM_TOTAL_TIMEOUT_SECONDS）。
        # 注意 180 秒**不是**总超时：它是 urllib 每次读操作的空闲超时，delta 持续到达
        # 就会不停重置它——只靠它拦不住推理死循环（旧注释与此相反，已修正）。
        first_byte_timeout = 0.0
        if is_local:
            first_byte_timeout = float(LOCAL_FIRST_BYTE_TIMEOUT_SECONDS)
            override = options.get("first_byte_timeout_seconds")
            if isinstance(override, (int, float)) and not isinstance(override, bool):
                first_byte_timeout = max(0.0, float(override))
        if diagnostics is not None:
            diagnostics["first_byte_timeout_s"] = round(first_byte_timeout, 3)
        # 「思考异常冗长」熔断阈值（字符；0 = 关闭）。纯解析层不读 options，由这里透传。
        reasoning_break_chars = float(REASONING_STREAM_BREAK_CHARS)
        break_override = options.get("reasoning_stream_break_chars")
        if isinstance(break_override, (int, float)) and not isinstance(break_override, bool):
            reasoning_break_chars = max(0.0, float(break_override))
        # 在线单请求总时长兜底（秒；0 = 关闭）。urllib 的 timeout 是「每次读操作的空闲超时」，
        # 推理 delta 持续到达就不断重置它——只靠 180s 空闲超时，一段思考死循环可以无限流下去。
        stream_total_timeout = 0.0
        if not is_local:
            stream_total_timeout = float(ONLINE_STREAM_TOTAL_TIMEOUT_SECONDS)
            total_override = options.get("stream_total_timeout_seconds")
            if isinstance(total_override, (int, float)) and not isinstance(total_override, bool):
                stream_total_timeout = max(0.0, float(total_override))
        # LM Studio 是本地后端（is_local ⇒ attempts=1、空流默认不重试），但它的「有推理零正文」
        # 与在线同病（思考烧光输出额度），§C 要求补齐降档重试，故单独开这个口子。
        # ollama 不动：它有自己的 think=False 内联重试，且本地重发要多付一次 prefill。
        lm_studio_retry = configured_format == "lm_studio"
        if diagnostics is not None:
            diagnostics["reasoning_break_chars"] = int(reasoning_break_chars)
            diagnostics["stream_total_timeout_s"] = round(stream_total_timeout, 3)
        attempts = 1 if is_local else 3
        attempts_override = options.get("request_attempts")
        if isinstance(attempts_override, int) and attempts_override > 0:
            attempts = min(attempts_override, 5)
        if native_tools:
            attempts = max(attempts, 2)
        if stream_options_requested:
            attempts = max(attempts, 2)
        if stream_options_requested and native_tools:
            attempts = max(attempts, 3)
        if lm_studio_retry:
            # LM Studio 的空流降档重试至少要两次尝试才有意义。
            attempts = max(attempts, 2)
        # A0 注入型兜底启用时 attempts 下限 +1：effort=high 打在不支持思考的端点上会连锁
        # 触发两个剥字段自愈（既有 reasoning_fallback 剥 reasoning_effort → A0 自愈剥注入的
        # max_tokens），各消耗一次 attempt；默认 attempts=3 刚好够但很紧，再撞上
        # stream_options / tools 回退就会耗尽，于是「剥两类字段 + 一次成功」将没有余量。
        if injected_max_tokens and attempts < 5:
            attempts += 1
        tool_fallback_used = False
        stream_options_fallback_used = False
        reasoning_fallback_used = False
        reasoning_passback_fallback_used = False
        max_tokens_fallback_used = False
        # 在线载荷瘦身（HTTP 413 自愈 / 发送前预算守卫）：**单次、禁循环**——同一轮里
        # 无论触发的是守卫还是 413，都只允许省略一次较早图片，之后照常按既有链路上报。
        payload_slim_used = False
        # 「有推理零正文」空流重试时的降档状态：current_effort 只影响本次请求的
        # 重试负载（不改会话设置）；empty_stream_reason_chars 记录失败尝试里最长的
        # 一次推理长度，给最终报错提供诊断。
        current_effort = reasoning_effort
        effort_lowered_on_retry = False
        empty_stream_reason_chars = 0
        if diagnostics is not None:
            parsed = urllib.parse.urlsplit(endpoint)
            try:
                proxy_note = (net_io.proxy_state().get("note") or "").strip()
            except Exception:  # noqa: BLE001
                proxy_note = ""
            diagnostics.update({
                "endpoint": f"{parsed.hostname or parsed.netloc}{parsed.path}",
                "attempts": 0,
                "http_ms": 0.0,
                "proxy_mode": proxy_note or "跟随系统代理",
            })
        # B3 发送前预算守卫：超过预算就先把最旧的图片换成文本占位再发，省掉一次
        # 「上传几十 MB、必然 413」的往返。只对在线请求生效（本地模型有自己的图片
        # 上限逻辑，且本地重发要多付一次 prefill）；连接测试不带图片，一并跳过。
        payload_budget = online_payload_budget_bytes(options)
        if not is_local and not connection_test and payload_budget > 0:
            if _json_payload_bytes(payload) > payload_budget:
                omitted = _slim_payload_images(payload, response_format, payload_budget)
                if omitted:
                    payload_slim_used = True
                    request = urllib.request.Request(
                        endpoint,
                        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        headers=headers,
                        method="POST",
                    )
                    if status:
                        status({
                            "type": "status",
                            "message": (
                                f"本轮请求体过大，已省略 {omitted} 张较早的图片后发送"
                                "（原始图片仍保留在会话记录中）"
                            ),
                        })
        for attempt in range(attempts):
            request_started = time.perf_counter()
            # 每次尝试重置进度袋：总时长超时后要靠它区分「全程只有推理」与「正文已出来」。
            stream_progress: dict[str, Any] = {}
            if diagnostics is not None:
                diagnostics["attempts"] = attempt + 1
            try:
                with ModelRuntime._urlopen_cancelable(request, request_timeout, cancel_event) as response:
                    if stream_enabled and response_format == "ollama":
                        try:
                            streamed = ModelRuntime._read_ollama_stream(
                                ModelRuntime._iter_stream_lines(
                                    response, cancel_event, first_byte_timeout, stream_total_timeout
                                ),
                                status, reasoning_break_chars, stream_progress,
                            )
                        except EmptyModelStreamError as exc:
                            # 思考预算熔断（§A）：本地模型也会思考失控（如 Qwen3 循环）。
                            # 不直接失败，走下面的「关掉思考重试一次」既有路径。
                            empty_stream_reason_chars = max(
                                empty_stream_reason_chars,
                                int(getattr(exc, "reasoning_chars", 0) or 0),
                            )
                            streamed = {"content": "", "reasoning": "", "usage": {}, "finish_reason": ""}
                        content = ModelRuntime._clean_content(streamed["content"])
                        reasoning = streamed["reasoning"]
                        if not content:
                            content = ModelRuntime._reasoning_action(reasoning)
                            if content:
                                reasoning = ""
                            elif payload.get("think") is not False:
                                if status:
                                    status({"type": "status", "message": "Ollama 未返回正文，正在关闭思考后重试"})
                                retry_payload = dict(payload)
                                retry_payload["think"] = False
                                retry_request = urllib.request.Request(
                                    endpoint,
                                    data=json.dumps(retry_payload, ensure_ascii=False).encode("utf-8"),
                                    headers=headers,
                                    method="POST",
                                )
                                with ModelRuntime._urlopen_cancelable(
                                    retry_request, request_timeout, cancel_event
                                ) as retry_response:
                                    streamed = ModelRuntime._read_ollama_stream(
                                        ModelRuntime._iter_stream_lines(
                                            retry_response, cancel_event, first_byte_timeout, stream_total_timeout
                                        ),
                                        status, reasoning_break_chars, stream_progress,
                                    )
                                content = ModelRuntime._clean_content(streamed["content"])
                                reasoning = streamed["reasoning"]
                                if not content:
                                    content = ModelRuntime._reasoning_action(reasoning)
                                    if content:
                                        reasoning = ""
                            if not content:
                                raise RuntimeError("Ollama 流式响应中没有文本内容")
                        _record_finish_reason(streamed.get("finish_reason"))
                        return content, reasoning, "", streamed["usage"]
                    if stream_enabled and response_format == "lm_studio":
                        streamed = ModelRuntime._read_lm_studio_stream(
                            ModelRuntime._iter_stream_lines(
                                response, cancel_event, first_byte_timeout, stream_total_timeout
                            ),
                            status, reasoning_break_chars, stream_progress,
                        )
                        content = ModelRuntime._clean_content(streamed["content"])
                        reasoning = streamed["reasoning"]
                        if not content:
                            content = ModelRuntime._reasoning_action(reasoning)
                            if content:
                                reasoning = ""
                            elif reasoning:
                                # 「有推理零正文」= 思考烧光了输出额度（实测本地模型同理）。
                                # 对齐在线路径语义：抛 EmptyModelStreamError 走降档重试，
                                # 而不是把一次可自愈的抖动升级成整轮失败（原实现直接 RuntimeError）。
                                empty_stream_reason_chars = max(empty_stream_reason_chars, len(reasoning))
                                raise EmptyModelStreamError(empty_stream_phrase)
                            else:
                                raise RuntimeError(empty_stream_phrase)
                        _record_finish_reason(streamed.get("finish_reason"))
                        return content, reasoning, "", streamed["usage"]
                    if stream_enabled and response_format != "gemini":
                        streamed = ModelRuntime._read_sse_response(
                            ModelRuntime._iter_stream_lines(
                                response, cancel_event, first_byte_timeout, stream_total_timeout
                            ),
                            response_format,
                            status,
                            reasoning_break_chars,
                            stream_progress,
                        )
                        content = ModelRuntime._clean_content(streamed["content"])
                        reasoning = streamed["reasoning"]
                        if not content:
                            content = ModelRuntime._reasoning_action(reasoning)
                            if content:
                                reasoning = ""
                            elif response_format == "codex_responses" or reasoning:
                                # 空正文按「可重试空流」（EmptyModelStreamError）处理，分两种情况：
                                # ① codex_responses：中继可能只回聚合事件，正文在聚合事件里补不回来；
                                # ② 有推理无正文：模型确实在生成、正文却丢失/被上游截断
                                #    （实测 mimo-v2.5 推理循环刷屏后正文为空；deepseek-v4.1-flash
                                #    高档思考陷入约 10 万字符推理循环、正文/工具全空，上游以
                                #    零计费截断——思考烧光了输出额度）。退避后再试一次大概率
                                #    恢复；若是②，重试时还会自动降低思考强度打破推理循环
                                #    （见下方 EmptyModelStreamError 捕获处），不把一次上游
                                #    抖动/思考失控升级成整轮对话失败。
                                # 既无正文也无推理的空流仍保留原 RuntimeError、不重试。
                                empty_stream_reason_chars = max(empty_stream_reason_chars, len(reasoning))
                                raise EmptyModelStreamError(empty_stream_phrase)
                            else:
                                raise RuntimeError(empty_stream_phrase)
                        _record_finish_reason(streamed.get("finish_reason"))
                        return content, reasoning, str(streamed.get("reasoning_id") or ""), streamed["usage"]
                    raw_response = ModelRuntime._read_response_cancelable(
                        response, cancel_event
                    ).decode("utf-8", errors="replace")
                    content_type = response.headers.get("Content-Type", "") if hasattr(response, "headers") else ""
                    try:
                        result = json.loads(raw_response)
                    except json.JSONDecodeError as exc:
                        chunks = []
                        for line in raw_response.splitlines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if not data or data == "[DONE]":
                                continue
                            try:
                                chunks.append(json.loads(data))
                            except json.JSONDecodeError:
                                chunks = []
                                break
                        if chunks:
                            result = chunks
                        else:
                            preview = raw_response.strip()[:500] or "空响应"
                            raise RuntimeError(
                                f"在线模型返回的不是 JSON（{content_type or '未知类型'}）：{preview}"
                            ) from exc
                break
            except urllib.error.HTTPError as exc:
                raw_body = exc.read().decode("utf-8", errors="replace")
                detail = _summarize_http_error(
                    raw_body,
                    exc.headers.get("Content-Type", "") if exc.headers else "",
                    endpoint_host,
                )
                # 上下文溢出必须**最先**判定：它是 4xx、重发同一份请求必然再失败，而且绝不能
                # 被下面「剥离字段后重试」的兼容链当成协议不兼容（那只会白打一次注定失败的
                # 请求，再把错误冲淡成「请求失败：HTTP 400: {原始 JSON}」）。
                evidence = _error_body_evidence(raw_body)
                if is_context_overflow(detail) or (evidence and is_context_overflow(evidence)):
                    # 注意：在 except 处理块里抛出的异常**不会被同一个 try 的其它 except
                    # 子句接住**，所以这里必须自己拼好用户文案（不能指望下面的统一出口）。
                    # detail 带上完整响应体，供解析后端自报的 n_ctx。
                    raise _context_overflow_error(
                        ContextOverflowError(detail, detail=raw_body),
                        profile,
                        is_local=is_local,
                        provider=overflow_source,
                    ) from exc
                # HTTP 413 = 请求体字节超限（图片/附件太多），与「上下文窗口溢出」是两回事：
                # 重发同一份请求必然再 413，所以先做一次**载荷瘦身自愈**（只省图片、不动文本、
                # 单次禁循环），仍 413 才抛用户可读文案（不回显供应商原始 openai_error JSON）。
                if exc.code == 413:
                    if not is_local and not payload_slim_used and attempt + 1 < attempts:
                        omitted = _slim_payload_images(
                            payload, response_format, online_payload_budget_bytes(options)
                        )
                        if omitted:
                            payload_slim_used = True
                            request = urllib.request.Request(
                                endpoint,
                                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                headers=headers,
                                method="POST",
                            )
                            if status:
                                status({
                                    "type": "status",
                                    "message": f"请求体过大，已省略 {omitted} 张较早的图片后重试",
                                })
                            continue
                    raise RuntimeError(
                        _payload_too_large_message(target_detail, slimmed=payload_slim_used)
                    ) from exc
                # A0 自愈（**只对注入值生效**）：思考时我们主动填的输出上限兜底值若被端点拒绝
                # （模型真实上限比预设表小），去掉该字段重试一次——用户没要求这个值，不能让
                # 兜底把一轮对话打死。**用户显式设的值触发的 400 不吞错**（照常抛出，暴露
                # 真实问题）。只重试一次，禁循环。
                if (
                    injected_max_tokens
                    and not max_tokens_fallback_used
                    and exc.code in {400, 422}
                    and _mentions_max_tokens(detail)
                ):
                    if request_format == "claude":
                        # Anthropic 的 max_tokens 是协议必填：去掉必然再 400。
                        # 回退到旧的 4096 兼容值才是有意义的自愈（不得改坏「必填」语义）。
                        payload["max_tokens"] = CLAUDE_MAX_TOKENS_FLOOR
                        note = "当前接口不接受较大的输出上限，已回退兼容值重试"
                    else:
                        _drop_injected_max_tokens(payload)
                        note = "当前接口不接受该输出上限字段，已去掉后重试"
                    request = urllib.request.Request(
                        endpoint,
                        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        headers=headers,
                        method="POST",
                    )
                    max_tokens_fallback_used = True
                    if status:
                        status({"type": "status", "message": note})
                    continue
                stream_option_rejection = any(
                    marker in detail.lower()
                    for marker in ("stream_options", "include_usage")
                )
                if (
                    stream_options_requested
                    and not stream_options_fallback_used
                    and exc.code in {400, 422}
                    and stream_option_rejection
                ):
                    payload.pop("stream_options", None)
                    request = urllib.request.Request(
                        endpoint,
                        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        headers=headers,
                        method="POST",
                    )
                    stream_options_fallback_used = True
                    if status:
                        status({"type": "status", "message": "当前接口不支持流式 usage 参数，已切换兼容请求"})
                    continue
                tool_rejection = bool(
                    re.search(
                        r"(tool_choice|test_tools|function\s?calling|unknown field|invalid field|"
                        r"does not support|do not support|unsupported\s+tools|tools?\s+(are|is)\s+not\s+supported|"
                        r"no such tool|invalid tool)",  # 精确词组，避免匹配用户/历史文本中的普通 "tools" 词
                        detail.lower(),
                    )
                )
                if native_tools and not tool_fallback_used and exc.code in {400, 404, 422} and tool_rejection:
                    fallback_payload = dict(payload)
                    for key in ("tools", "tool_choice", "parallel_tool_calls", "toolConfig"):
                        fallback_payload.pop(key, None)
                    # 回写到 payload：**剥掉的字段必须累积**。否则下一个兜底分支
                    # （A0 的去 max_tokens 自愈）会从旧的 payload 重建请求，把刚刚
                    # 剥掉的字段又贴回去，「剥两类字段 + 一次成功」的余量就白给了。
                    payload = fallback_payload
                    request = urllib.request.Request(
                        endpoint,
                        data=json.dumps(fallback_payload, ensure_ascii=False).encode("utf-8"),
                        headers=headers,
                        method="POST",
                    )
                    tool_fallback_used = True
                    if status:
                        status({"type": "status", "message": "当前接口不支持原生 Tool Calling，已切换兼容工具协议"})
                    continue
                # DeepSeek thinking mode rejects an assistant history message that
                # is missing reasoning_content ("must be passed back to the API").
                # That is the OPPOSITE of "this field is not accepted", so we must
                # NOT trigger the strip-fields fallback — otherwise the retry drops
                # reasoning_content and is guaranteed to fail with the same 400.
                lower_detail = str(detail).lower()
                reasoning_required = any(
                    marker in lower_detail
                    for marker in (
                        "must be passed", "must be provided", "must be included",
                        "must be returned", "is required", "required to be",
                    )
                )
                reasoning_rejection = (
                    not reasoning_required
                    and any(
                        marker in lower_detail
                        for marker in (
                            "reasoning_content", "reasoning_effort", "thinking",
                            "unknown field", "unrecognized", "does not support",
                            "unsupported", "invalid field",
                        )
                    )
                )
                if (
                    not is_local
                    and not reasoning_fallback_used
                    and exc.code in {400, 422}
                    and reasoning_rejection
                ):
                    # OpenAI-compatible gateways disagree on whether they
                    # accept optional thinking fields. Retry once with those
                    # fields removed; never duplicate a tool submission beyond
                    # this protocol-only retry.
                    fallback_payload = dict(payload)
                    fallback_messages = []
                    for message in fallback_payload.get("messages", []):
                        if isinstance(message, dict):
                            message = dict(message)
                            message.pop("reasoning_content", None)
                        fallback_messages.append(message)
                    fallback_payload["messages"] = fallback_messages
                    fallback_payload.pop("reasoning_effort", None)
                    # 同上：回写 payload，让后续兜底分支在「已剥掉思考字段」的基底上继续。
                    payload = fallback_payload
                    request = urllib.request.Request(
                        endpoint,
                        data=json.dumps(fallback_payload, ensure_ascii=False).encode("utf-8"),
                        headers=headers,
                        method="POST",
                    )
                    reasoning_fallback_used = True
                    if status:
                        status({"type": "status", "message": "当前网关不接受思考字段，已自动切换兼容请求"})
                    continue
                # DeepSeek 思考模式：携带 tools 的请求必须回传全部历史 reasoning_text
                # （官方规范，缺失即 400 "must be passed back"）。若回传逻辑缺失/被网关拒绝，
                # 兜底为去掉 tools 重试一次——官方明示未携带 tools 的请求无需回传 reasoning，
                # 对话可继续（代价：本轮失去原生工具调用）。
                reasoning_passback_rejected = (
                    "must be passed" in lower_detail
                    or "reasoning_text" in lower_detail
                )
                if (
                    not is_local
                    and native_tools
                    and not reasoning_passback_fallback_used
                    and exc.code in {400, 422}
                    and reasoning_passback_rejected
                ):
                    fallback_payload = dict(payload)
                    for key in ("tools", "tool_choice", "parallel_tool_calls", "toolConfig"):
                        fallback_payload.pop(key, None)
                    # 回写到 payload：**剥掉的字段必须累积**。否则下一个兜底分支
                    # （A0 的去 max_tokens 自愈）会从旧的 payload 重建请求，把刚刚
                    # 剥掉的字段又贴回去，「剥两类字段 + 一次成功」的余量就白给了。
                    payload = fallback_payload
                    request = urllib.request.Request(
                        endpoint,
                        data=json.dumps(fallback_payload, ensure_ascii=False).encode("utf-8"),
                        headers=headers,
                        method="POST",
                    )
                    reasoning_passback_fallback_used = True
                    if status:
                        status({"type": "status", "message": "思考模式下回传 reasoning 被网关拒绝，已切换为无工具重试"})
                    continue
                # 5xx 是供应商内部错误（响应体通常没有结构化错误信息，只回一句
                # "Internal Server Error"），按瞬时故障退避重试：2026-09-09 实测一次
                # HTTP 500 直接终止整轮，18 秒后重发同样的请求即成功——该 API 无状态、
                # 重发模型请求是安全的，因此 5xx 与 429/502/503/504 同等对待。
                server_error = 500 <= exc.code < 600
                # 失败取证：思考回传类 400/422 与全部 5xx 都把请求体落盘（截断后），
                # 便于复现定位（payload 不含 API Key；仅含对话内容，写本机文件）。
                if server_error or (
                    exc.code in {400, 422}
                    and ("reasoning" in str(detail).lower() or "must be passed" in str(detail).lower())
                ):
                    _dump_failed_payload(
                        endpoint,
                        exc.code,
                        detail,
                        payload,
                        "server-error" if server_error else "reasoning-passback",
                    )
                if (
                    not is_local
                    and not connection_test
                    and (exc.code == 429 or server_error)
                    and attempt + 1 < attempts
                ):
                    if exc.code == 429:
                        retry_after = ""
                        if exc.headers:
                            retry_after = str(exc.headers.get("Retry-After") or "").strip()
                        try:
                            delay = max(1.0, min(float(retry_after), 60.0))
                        except (TypeError, ValueError):
                            delay = min(10.0 * (2 ** attempt), 60.0)
                        retry_message = (
                            f"供应商限流，{delay:g} 秒后重试"
                            f"（{attempt + 1}/{attempts - 1}）"
                        )
                    else:
                        delay = min(1.5 * (attempt + 1), 5.0)
                        retry_message = (
                            f"供应商暂时不可用（HTTP {exc.code}），{delay:g} 秒后重试"
                            f"（{attempt + 1}/{attempts - 1}）"
                        )
                    if status:
                        status({"type": "status", "message": retry_message})
                    if cancel_event:
                        if cancel_event.wait(delay):
                            raise RuntimeError("任务已取消")
                    else:
                        time.sleep(delay)
                    continue
                raise RuntimeError(f"{target_detail}返回 HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                error_code = _network_error_code(exc)
                # 响应读取中途被切断（IncompleteRead / RemoteDisconnected 等 HTTPException）
                # 属于瞬时传输故障：与连接失败一样可重试，而不是让整个 run 直接报错。
                is_http_exception = isinstance(exc, http.client.HTTPException)
                # 出站路由已由 net_io 统一决定（见其注释）：此处仅保留“连接瞬时
                # 故障时按次数重试”的能力，不再隐式地直连/代理互切。
                retryable_test_error = connection_test and error_code in FAST_RETRY_NETWORK_ERRORS
                if (
                    not is_local
                    and attempt + 1 < attempts
                    and (not connection_test or retryable_test_error or is_http_exception)
                ):
                    delay = (0.5 if connection_test else 1.5) * (attempt + 1)
                    if cancel_event and cancel_event.wait(delay):
                        raise RuntimeError("任务已取消")
                    continue
                if isinstance(reason, TimeoutError):
                    duration = f"{request_timeout // 60} 分钟" if request_timeout >= 60 else f"{request_timeout} 秒"
                    raise RuntimeError(f"{target_detail}响应超过 {duration}，已停止等待") from exc
                hint = ""
                if error_code == 10061:
                    hint = "；目标端口拒绝连接，请检查 API URL、代理/TUN 或服务是否已启动"
                elif error_code in {10053, 10054}:
                    hint = "；连接被中止，请检查代理/TUN、防火墙或服务状态"
                elif is_http_exception:
                    hint = "；响应读取不完整（连接中断），请检查网络/代理后重试"
                raise RuntimeError(f"无法连接{target_detail}：{reason}{hint}") from exc
            except ContextOverflowError as exc:
                # 溢出的统一出口：本地 attempts 本就不重试；在线也不重试（重发同一份超长
                # 请求必然再失败）。这里补上「后端名 + 当前窗口」的可行动文案后原样抛出，
                # 由 run/chat.py 的既有失败路径落成一条消息（ContextOverflowError 继承
                # RuntimeError，那边无需改动）。
                raise _context_overflow_error(
                    exc, profile, is_local=is_local, provider=overflow_source,
                ) from exc
            except StreamTotalTimeout as exc:
                # 单次请求超过墙钟总时长（§D）。900 秒远超市面最长合法思考，所以走到这里
                # 要么是上游卡死、要么是思考死循环——两者都不能继续等下去。
                # 归因靠解析层回传的进度袋：**全程只有推理、零正文** ⇒ 与空流同病，
                # 重试时降档打破循环；否则按瞬时故障退避重发（与 URLError 分支同档）。
                if attempt + 1 >= attempts:
                    raise
                delay = min(1.5 * (attempt + 1), 5.0)
                reasoning_only = bool(stream_progress.get("reasoning_chars")) and not bool(
                    stream_progress.get("has_content")
                )
                lowered_note = ""
                if reasoning_only:
                    lowered = ModelRuntime._lower_reasoning_effort(current_effort, profile)
                    if lowered:
                        current_effort = lowered
                        effort_lowered_on_retry = True
                        payload = thinking_module.strip_thinking_fields(payload)
                        payload.update(ModelRuntime._thinking_patch(profile, current_effort))
                        request = urllib.request.Request(
                            endpoint,
                            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                            headers=headers,
                            method="POST",
                        )
                        lowered_note = "并自动降低思考强度"
                if status:
                    status({"type": "status", "message": (
                        f"单次请求超过 {stream_total_timeout / 60:g} 分钟仍未结束，已停止等待，"
                        f"{delay:g} 秒后{lowered_note}重试（{attempt + 1}/{attempts - 1}）"
                    )})
                if cancel_event:
                    if cancel_event.wait(delay):
                        raise RuntimeError("任务已取消")
                else:
                    time.sleep(delay)
                continue
            except EmptyModelStreamError as exc:
                if getattr(exc, "reasoning_chars", 0):
                    # 思考预算熔断：把熔断时的截断长度记进诊断（文案已兼容）。
                    empty_stream_reason_chars = max(
                        empty_stream_reason_chars, int(exc.reasoning_chars)
                    )
                # 在线的流式空响应走此分支（连接测试不重试）；本地只有 LM Studio 走
                # （is_local ⇒ attempts 默认 1，它的空流降档重试是 §C 明确补齐的口子）。
                # 「有推理零正文」的空流极可能是思考烧光了输出额度——原样重发会重现同一个
                # 推理循环（实测三次尝试各思考约 10 万字符、正文全空），所以重试沿**该 provider
                # 预设的词表顺序**逐级降低思考强度，打破循环而不是重复循环；无档可降
                # （off/low/auto 或该协议本就不发思考字段）时保持原有的退避重发，
                # 应对真正的上游瞬时抖动。
                if (not is_local or lm_studio_retry) and not connection_test and attempt + 1 < attempts:
                    delay = min(1.5 * (attempt + 1), 5.0)
                    lowered_note = ""
                    # 按该 provider 预设的词表顺序下探一档（见 thinking.lower_effort）：
                    # K3 的 low/high/max 退到「声明顺序里的上一档」，而不是「应用四档减一」
                    # 退到不存在的中档；不再按协议族写白名单（LM Studio 等本地方言同样可降）。
                    # openai_chat + DeepSeek 预设不发任何思考字段，降档无从谈起。
                    lowered = ModelRuntime._lower_reasoning_effort(current_effort, profile)
                    if lowered:
                        current_effort = lowered
                        effort_lowered_on_retry = True
                        # 先剥掉旧方言字段再补新档，避免 reasoning / reasoning_effort 两种
                        # 落点同时残留在请求体里（strip 返回副本，不动上面那份 payload）。
                        payload = thinking_module.strip_thinking_fields(payload)
                        payload.update(ModelRuntime._thinking_patch(profile, current_effort))
                        request = urllib.request.Request(
                            endpoint,
                            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                            headers=headers,
                            method="POST",
                        )
                        lowered_note = "自动降低思考强度"
                    suffix = f"{lowered_note}重试（{attempt + 1}/{attempts - 1}）"
                    message = (
                        f"在线模型返回空响应（思考未产出正文），{delay:g} 秒后{suffix}"
                        if lowered_note
                        else f"在线模型返回空响应，{delay:g} 秒后{suffix}"
                    )
                    if status:
                        status({
                            "type": "status",
                            "message": message,
                        })
                    if cancel_event:
                        if cancel_event.wait(delay):
                            raise RuntimeError("任务已取消")
                    else:
                        time.sleep(delay)
                    continue
                if effort_lowered_on_retry:
                    diagnosis = (
                        f"（模型思考后未产出正文，最长一次思考约 {empty_stream_reason_chars // 10000} 万字；"
                        if empty_stream_reason_chars >= 10000
                        else "（模型思考后未产出正文，"
                    )
                    raise RuntimeError(
                        f"{empty_stream_phrase}"
                        f"{diagnosis}已自动降低思考强度重试仍失败；"
                        "请把思考强度调低或精简上下文后重新生成）"
                    )
                raise

            finally:
                if diagnostics is not None:
                    diagnostics["http_ms"] = round(
                        float(diagnostics.get("http_ms") or 0.0)
                        + (time.perf_counter() - request_started) * 1000,
                        1,
                    )

        usage = ModelRuntime._online_usage(response_format, result)
        try:
            content, reasoning = ModelRuntime._online_response(response_format, result)
        except RuntimeError:
            reasoning = ModelRuntime._online_reasoning(response_format, result)
            if connection_test and reasoning:
                _record_finish_reason("")
                return "接口已返回有效响应", reasoning, "", usage
            raise
        reasoning_id = ModelRuntime._responses_reasoning_id(response_format, result)
        _record_finish_reason(ModelRuntime._online_finish_reason(response_format, result))
        return content, reasoning, reasoning_id, usage

    @staticmethod
    def _online_response(request_format: str, result: Any) -> tuple[str, str]:
        reasoning = ModelRuntime._online_reasoning(request_format, result)
        # Native OpenAI tool_calls (non-streaming) are converted to the internal
        # action structure so the Agent Loop can consume them directly.
        action = ModelRuntime._openai_tool_calls_action(result, request_format)
        if action is None:
            action = ModelRuntime._responses_tool_calls_action(result, request_format)
        if action is None:
            action = ModelRuntime._gemini_tool_calls_action(result, request_format)
        if action is None:
            action = ModelRuntime._claude_tool_calls_action(result, request_format)
        if action is not None:
            return action, reasoning
        content = ModelRuntime._online_content(request_format, result)
        if not content:
            content = ModelRuntime._reasoning_action(reasoning)
            if content:
                # 这是模型的 Agent 协议动作，不作为思考过程展示给用户。
                reasoning = ""
            else:
                raise RuntimeError(f"在线模型响应中没有文本内容：{str(result)[:1000]}")
        return ModelRuntime._clean_content(content), reasoning
