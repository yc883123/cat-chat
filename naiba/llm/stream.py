"""模型流解析与推理流（自 naiba.llm.runtime 迁出）。

含：SSE/Ollama/LM Studio 流解析、推理 (<think>) 流事件、Agent 工具协议守卫
（判别书面前缀是正文还是 JSON/XML/Harmony 工具动作）与缓冲收尾。守卫是**围栏感知**的
有状态类 ``_ProtocolStreamGuard``（三条流式路径共用一份口径）：Markdown 围栏里的示例
不算协议，围栏标记被 SSE 拆成两包也能跟上——口径来自 ``naiba/core/text_fences.py``，
与 ``skills/agent.py`` 的终态解析必须一致。纯解析层：
不触碰网络/锁/状态；ModelRuntime 经继承 StreamMixins（并 ProtocolMixins）复用。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from naiba.core import text_fences
from naiba.core.text_fences import (
    is_fence_close_line,
    parse_fence_open,
    partial_fence_opener_length,
)
from naiba.llm.protocols import ProtocolMixins

StatusCallback = Callable[[dict[str, Any]], None]


def _iter_lines(text: str, start: int):
    """从 ``start`` 起逐行产出 ``(行起点, 行内容(去换行), 下一行起点, 行是否已完整)``。"""
    pos = start
    size = len(text)
    while pos < size:
        newline = text.find("\n", pos)
        if newline < 0:
            yield pos, text[pos:].rstrip("\r"), size, False
            return
        yield pos, text[pos:newline].rstrip("\r"), newline + 1, True
        pos = newline + 1


def _fence_open_after(buffer: str, start: int):
    """``start`` 之后第一个**完整行**的围栏开启行 → ``(起点, 下一行起点, char, 长度)``。"""
    for line_start, body, nxt, complete in _iter_lines(buffer, start):
        if not complete:
            return None
        opened = parse_fence_open(body)
        if opened:
            char, length, _info = opened
            return line_start, nxt, char, length
    return None


def _fence_close_after(buffer: str, start: int, char: str, length: int) -> int | None:
    """``start`` 之后第一个闭合行 → 闭合行之后的位置（未闭合返回 None）。"""
    for _line_start, body, nxt, complete in _iter_lines(buffer, start):
        if not complete:
            return None
        if is_fence_close_line(body, char, length):
            return nxt
    return None


def _mask_outside(buffer: str, regions: list[tuple[int, int]]) -> str:
    """把「围栏内」的部分换成等长空格（偏移不变），只留可扫描区间。

    流式场景不能用无状态的 ``fence_mask``：pending 可能是从围栏中间开始的（开启行早已
    作为正文下发并移出缓冲），只有本对象的区间清单知道哪一段仍在围栏里。
    """
    size = len(buffer)
    if not regions:
        return "".join("\n" if char == "\n" else ("\r" if char == "\r" else " ") for char in buffer)
    covered = bytearray(size)
    for start, end in regions:
        for position in range(max(0, start), min(size, end)):
            covered[position] = 1
    chars = list(buffer)
    for position in range(size):
        if not covered[position] and chars[position] not in "\r\n":
            chars[position] = " "
    return "".join(chars)


class _FenceGuard:
    """围栏**区间**状态机（跨 chunk 跟踪围栏开合）：``regions`` 返回「可以扫描协议」的区间。

    ⚠️ 生产路径不用它——三条流式路径统一走 :class:`_ProtocolStreamGuard`（它额外覆盖
    「闭合围栏被拆包」「闭合围栏与协议前缀同块」「status=None 也要推进状态」三个线上事故）。
    本类与 :meth:`StreamMixins._forward_guarded_text` 是**保留的兼容入口**：
    ``tests/test_fence_actions.py`` 直接钉死 ``regions`` / ``in_fence`` 的区间语义，
    改动前先看那份守门。围栏内的区间不在清单里，但调用方仍会把它们作为正文下发
    （掩码只影响检测，不影响可见文本）。未闭合的围栏一律按正文放行——它到流尾为止
    都不是协议候选，最终由 Agent 终态解析按「整文围栏」处理。
    """

    def __init__(self) -> None:
        self.in_fence = False
        self.char = ""
        self.length = 0

    def regions(self, buffer: str, final: bool = False) -> tuple[list[tuple[int, int]], int]:
        regions: list[tuple[int, int]] = []
        pos = 0
        size = len(buffer)
        while True:
            if self.in_fence:
                closed_at = _fence_close_after(buffer, pos, self.char, self.length)
                if closed_at is None:
                    return regions, 0
                self.in_fence = False
                pos = closed_at
                continue
            opened = _fence_open_after(buffer, pos)
            if opened is None:
                keep = 0 if final else partial_fence_opener_length(buffer)
                end = size - keep
                if end > pos:
                    regions.append((pos, end))
                return regions, keep
            start, after, char, length = opened
            if start > pos:
                regions.append((pos, start))
            self.in_fence = True
            self.char = char
            self.length = length
            pos = after


# 「本轮请求超出上下文窗口」的服务端特征串。只用来判定**服务端错误体**（4xx 的 detail、
# 流里内嵌的 error 事件），不碰用户正文，因此没有误伤面。各后端写法不同：
# llama.cpp / Unsloth 回 `exceed_context_size_error` 与 `n_ctx`；LM Studio 回
# `greater than the context length`；Ollama 把错误整块塞在流里；在线供应商多为
# `maximum context length`（DeepSeek/OpenAI）或 `prompt is too long`（Anthropic）。
_CONTEXT_OVERFLOW_MARKERS = (
    "exceed_context_size",
    "exceeds the available context",
    "exceeds the context",
    "context length",
    "context window",
    "context size",
    "maximum context",
    "max context length",
    "prompt is too long",
    "input is too long",
    "reduce the length",
    "n_ctx",
)

# 流内错误事件的来源标注（写进异常文案，让用户知道是哪个后端拒的）。
_STREAM_SOURCE_LABELS = {
    "ollama": "Ollama",
    "lm_studio": "LM Studio",
    "llama_cpp": "llama.cpp",
    "unsloth": "Unsloth",
}


def is_context_overflow(detail: Any) -> bool:
    """服务端错误体是否表示「上下文窗口不足」（大小写无关的纯字符串判定）。"""
    text = str(detail or "").lower()
    return any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS)


class StreamTotalTimeout(RuntimeError):
    """单次流式请求超过**总时长**兜底（不是空闲超时）。

    为什么需要它：``ONLINE_MODEL_TIMEOUT_SECONDS`` 是 urllib 每次读操作的空闲超时，
    推理 delta 持续到达就不断重置它。一段 43 万字符的思考循环可以无限流下去，界面上
    「合法长思考」与「死循环」完全无法区分，只能靠用户手动停止。本层给在线请求加一道
    **墙钟**总时长（默认 900 秒，远超市面最长合法思考）。

    ``reasoning_chars`` / ``had_content``：超时时刻的进度（由解析层回填），runtime 用它
    区分「全程只有推理、零正文」（⇒ 转空流语义、重试时降档）与「正文已经出来了」（⇒ 按
    瞬时故障退避重试）。
    """

    def __init__(self, message: str) -> None:
        super().__init__(str(message))
        self.reasoning_chars = 0
        self.had_content = False


class EmptyModelStreamError(RuntimeError):
    """流式响应消费完毕但既无正文也无有效 Agent action（或思考异常冗长被主动中断）。

    按**瞬时故障 / 可自愈**处理：在既有重试预算内退避重发；「有推理零正文」的空流还会
    沿预设词表自动降低思考强度（思考烧光输出额度的自愈），次数用尽后抛出。
    定义在解析层是因为**判定在这里**（三个流解析器都能抛），runtime 侧只负责重试策略。

    ``reasoning_chars``：熔断时已累计的推理字符数（诊断用；普通空流为 0）。
    """

    def __init__(self, message: str, *, reasoning_chars: int = 0) -> None:
        super().__init__(str(message))
        try:
            self.reasoning_chars = max(0, int(reasoning_chars or 0))
        except (TypeError, ValueError):
            self.reasoning_chars = 0


class ContextOverflowError(RuntimeError):
    """后端明确回报：本轮请求已超出模型的上下文窗口。

    与「网络故障」「供应商限流」分开：溢出是 4xx、重发同一份请求必然再失败，必须给
    用户**可操作的建议**（新会话 / 填写真实上下文窗口）而不是一段原始 JSON。
    用户可见文案由 `naiba.llm.runtime.context_overflow_message` 在「后端名 + 当前窗口」
    都已知时统一拼装（本层只管如实带上后端原文）。
    """

    def __init__(
        self,
        message: str,
        *,
        window: int = 0,
        backend: str = "",
        detail: str = "",
    ) -> None:
        super().__init__(str(message or "上下文窗口不足"))
        try:
            self.window = max(0, int(window or 0))
        except (TypeError, ValueError):
            self.window = 0
        self.backend = str(backend or "")
        self.detail = str(detail or "")


def _mark_progress(
    progress: dict[str, Any] | None,
    reasoning_chars: int,
    has_content: bool,
) -> None:
    """把已经吃进来的进度回传给调用方（供总时长超时归因用；不影响解析结果）。"""
    if progress is None:
        return
    progress["reasoning_chars"] = int(reasoning_chars)
    progress["has_content"] = bool(has_content)


def _reasoning_break_error(chars: int) -> EmptyModelStreamError:
    return EmptyModelStreamError(
        f"思考异常冗长（已累计 {int(chars)} 字推理）且未产出任何正文，已主动中断",
        reasoning_chars=int(chars),
    )


def _break_on_runaway_reasoning(
    reasoning_chars: int,
    limit: float,
    has_content: bool,
    status: StatusCallback | None,
) -> None:
    """推理量超过预算且正文仍为空 ⇒ 主动中断（异常交给 runtime 去降档重试）。

    为什么要熔断：``ONLINE_MODEL_TIMEOUT_SECONDS`` 是 urllib **每次读操作的空闲超时**，
    推理 delta 持续到达就会不停重置它，于是一段 43 万字符的思考循环可以无限流下去，
    直到上游掐断（实测 mimo-v2.5 / deepseek-v4.1-flash 的高档思考）。DeepSeek / Moonshot
    的思考 token 还按输出计费——不是「等它想完就好」，而是**在烧钱**。

    **正文一旦出现即解除熔断**：思考 + 正文正常输出是合法形态，绝不误伤。
    ``limit <= 0`` 表示关闭本层（行为与改动前完全一致）。
    """
    if limit <= 0 or has_content or reasoning_chars <= limit:
        return
    if status:
        status({
            "type": "status",
            "message": (
                f"思考异常冗长（已累计 {int(reasoning_chars)} 字推理）且正文为空，"
                "已主动中断并准备降档重试"
            ),
        })
    raise _reasoning_break_error(reasoning_chars)


def _stream_error(source: str, error: Any) -> BaseException:
    """把流里内嵌的错误对象/文本转成合适的异常（溢出走专用类型，其余保留原文）。

    ``detail`` 保留错误的**完整原样**（dict 就序列化）：本地后端常把真值放在 message
    的兄弟字段里（llama.cpp 的 ``n_ctx``），只留 message 就再也解析不出真实窗口。
    """
    if isinstance(error, dict):
        detail = str(
            error.get("message") or error.get("detail") or error.get("error") or error
        )
        try:
            raw = json.dumps(error, ensure_ascii=False)
        except (TypeError, ValueError):
            raw = str(error)
    else:
        detail = str(error or "").strip()
        raw = detail
    detail = detail or "未知错误"
    if is_context_overflow(detail) or is_context_overflow(raw):
        return ContextOverflowError(detail, detail=raw, backend=source)
    return RuntimeError(f"{source} 流式错误：{detail}")


# Patterns used to keep agent tool-call protocols out of the user-facing
# streaming answer. The classifier below decides, before forwarding any
# fragment, whether the leading model output is ordinary prose or an agent
# action (JSON / XML tool protocol) that must only reach the Agent Loop.
_TOOL_OPEN_TAG = re.compile(r"^<(tool_calls|invoke|tool)\b", re.IGNORECASE)
_TOOL_NAMED_ATTR = re.compile(r"\b(?:name|type)\s*=")
# Some compatible endpoints prepend a sentence before emitting their tool
# protocol. Keep a short unflushed tail so a marker split across SSE chunks is
# detected before it can reach the visible answer.
_TOOL_PROTOCOL_ANYWHERE = re.compile(r"<(?:tool_calls|invoke|tool)\b", re.IGNORECASE)
# Kimi K3 (and other Harmony-style compatible endpoints) may serialize tool
# calls as reserved tokens in ordinary output text instead of returning native
# function_call items, for example ``<|open|>tools<|sep|>...``.  Treat this as
# an agent protocol at the stream boundary so it never leaks into the answer.
_HARMONY_TOOL_ANYWHERE = re.compile(r"<\|open\|>(?:tools|call)\b", re.IGNORECASE)
_JSON_TOOL_ANYWHERE = re.compile(
    r'\{(?=[\s\S]{0,96}"(?:type|tool)"\s*:)',
    re.IGNORECASE,
)
# Upper bound (chars) for buffering an ambiguous leading fragment before we
# give up and treat it as plain text, so a malformed stream can never stall.
_AGENT_BUFFER_LIMIT = 1024


class _InlineReasoningParser:
    """Split local-model <think> streams without leaking them into the answer."""

    _OPEN = ("<think>", "<thinking>", "<reasoning>")
    _CLOSE = ("</think>", "</thinking>", "</reasoning>")

    def __init__(self) -> None:
        self.buffer = ""
        self.inside = False

    def feed(self, text: str, final: bool = False) -> tuple[str, str]:
        self.buffer += str(text or "")
        visible: list[str] = []
        reasoning: list[str] = []
        while self.buffer:
            markers = self._CLOSE if self.inside else self._OPEN
            positions = [(self.buffer.lower().find(marker), marker) for marker in markers]
            positions = [(index, marker) for index, marker in positions if index >= 0]
            if positions:
                index, marker = min(positions, key=lambda item: item[0])
                chunk = self.buffer[:index]
                (reasoning if self.inside else visible).append(chunk)
                self.buffer = self.buffer[index + len(marker):]
                self.inside = not self.inside
                continue
            if final:
                (reasoning if self.inside else visible).append(self.buffer)
                self.buffer = ""
                break
            # Retain only a suffix that can actually become a marker in the
            # next SSE chunk.  A fixed tail made every short answer arrive in
            # bursts even when it contained no reasoning tag at all.
            lower = self.buffer.lower()
            keep = 0
            for marker in markers:
                limit = min(len(marker) - 1, len(lower))
                for size in range(1, limit + 1):
                    if marker.startswith(lower[-size:]):
                        keep = max(keep, size)
            if keep:
                if keep == len(self.buffer):
                    # The entire buffer may be the beginning of a marker
                    # (for example ``<thi``). Keep it for the next SSE chunk
                    # and stop this pass instead of looping over unchanged
                    # data forever.
                    break
                chunk, self.buffer = self.buffer[:-keep], self.buffer[-keep:]
            else:
                chunk, self.buffer = self.buffer, ""
            (reasoning if self.inside else visible).append(chunk)
        return "".join(visible), "".join(reasoning)


class _ReasoningStreamer:
    """Stream reasoning deltas live when a provider exposes them incrementally.

    ``feed`` emits ``reasoning_start`` once, then a ``reasoning_delta`` per
    incoming piece. ``finish`` closes with ``reasoning_end`` when streaming was
    possible; otherwise (a model that only returns a lump of thinking at the end,
    or none at all) it falls back to a one-shot ``_emit_buffered_reasoning`` so
    the reasoning is still shown, just not incrementally.
    """

    def __init__(self, status: StatusCallback | None, parts: list[str]):
        self.status = status
        self.parts = parts
        self.started = False

    def feed(self, reasoning: str) -> None:
        if not reasoning:
            return
        self.parts.append(reasoning)
        if self.status is not None:
            if not self.started:
                self.status({"type": "reasoning_start"})
                self.started = True
            self.status({"type": "reasoning_delta", "content": reasoning})

    def finish(self) -> None:
        if self.started:
            if self.status is not None:
                self.status({"type": "reasoning_end"})
        else:
            # 兜底：模型未实时暴露思考（增量解析没有触发），改为结尾一次性输出。
            StreamMixins._emit_buffered_reasoning(self.status, "".join(self.parts))


def _possible_fence_suffix_length(buffer: str) -> int:
    """缓冲区末尾是不是「半个围栏标记行」——是则留待下一块（围栏标记可能被 SSE 拆两包）。

    只在**行首**（缩进 ≤3 个空格/制表符）算数：行内代码的 ``x = ` `` 不需要保留。
    完整围栏行（≥3）由 :func:`naiba.core.text_fences.fence_scan` 直接处理，这里只补
    那 1–2 个字符的窗口。

    缩进口径必须与 :func:`naiba.core.text_fences.fence_run` **逐字一致**（``lstrip(" \\t")``
    + ``MAX_FENCE_INDENT``）：``text_fences`` 允许围栏行用最多 3 个空格**或制表符**缩进，
    这里若只剥空格，``"\\t``"`` 这种带 Tab 缩进的半个闭合围栏行就不会被保留——
    半截被当正文发出去、``_advance`` 又按整行扫 ⇒ 闭合认不出、``_in_fence`` 永久停在
    围栏内，之后的真实协议会被当成围栏里的代码正文整套放行（协议明文进 UI）。
    """
    text = str(buffer or "")
    if not text:
        return 0
    line = text[text.rfind("\n") + 1:]
    if not line:
        return 0
    # CRLF can be split between ``\r`` and ``\n``.  Treat a terminal CR as
    # line-ending whitespace while still retaining the full partial line.
    probe_line = line[:-1] if line.endswith("\r") else line
    indent = len(probe_line) - len(probe_line.lstrip(" \t"))
    if indent > text_fences.MAX_FENCE_INDENT:
        return 0
    body = probe_line[indent:]
    if not body:
        # 末行缩进后为空（整行只有 1–3 个空格）：没有围栏字符可判，直接放行。
        # 缺这一条就会在下一行 body[0] 越界 ⇒ IndexError 打死整条流。纯空格 delta
        # 真实可达（缩进、两空格硬换行被 SSE 拆包都是 1–3 个空格），且 IndexError
        # 不在 runtime 的重试 except 列表里、Agent 只捕 RuntimeError ⇒ 整轮回答作废。
        return 0
    if body[0] not in text_fences.FENCE_CHARACTERS:
        return 0
    if any(char != body[0] for char in body):
        return 0
    if len(body) >= text_fences.MIN_FENCE_LENGTH:
        return 0
    return len(line)


class _ProtocolStreamGuard:
    """工具协议守卫（SSE / Ollama / LM Studio 三条流式路径共用）：**围栏感知**。

    与旧的无状态 ``_forward_guarded_text`` 只有一处实质差别，但那处是决定性的——
    **协议标记只在 Markdown 围栏之外才算协议**。旧写法在整块缓冲区里任意位置扫标记，
    于是正文里 ```` ```json ```` 的示例（``{`` 后 96 字符内出现 ``"type"``/``"tool"``）
    会被当成协议：吐出标记之前的正文、判定「这是工具轮」，**该次响应剩余正文一条 delta
    都不再发** ⇒ 界面正好停在围栏行。

    围栏状态必须跨 chunk 保持（```` ```` 与闭合行可能被 SSE 拆成两包），所以守卫是有状态的。

    刻意保持不变的行为（有测试钉死，勿动）：

    * ``_classify_agent_output`` 只看缓冲区开头判定「正文还是协议」；
    * 判定为协议后**该次响应剩余正文不再外发**（协议只作为 action 进 Agent Loop）；
    * **未闭合围栏到流尾 = 全部按正文放行**——被切断的回答里，围栏后面的内容不是协议。
    """

    def __init__(self) -> None:
        self.pending = ""
        self.detected = False
        self._in_fence = False
        self._fence_char = ""
        self._fence_length = 0

    def feed(self, text: str, status: StatusCallback | None) -> None:
        """吃进一段正文，按「能安全外发多少」推 delta；已判定协议后一律不外发。"""
        if self.detected:
            return
        piece = str(text or "")
        if not piece:
            return
        self.pending += piece
        self._flush(status, final=False)

    def finish(self, status: StatusCallback | None) -> None:
        """流收尾：把最后一块正文放出去（未闭合围栏此时一律按正文处理）。"""
        if self.detected:
            self.pending = ""
            return
        self._flush(status, final=True)

    # ---- 内部 ----
    def _mask(self, text: str) -> tuple[str, text_fences.FenceState]:
        """掩码 + **本块结束时**的围栏状态（两个返回值都要用，别丢掉状态）。"""
        return text_fences.fence_scan(
            text,
            in_fence=self._in_fence,
            fence_char=self._fence_char,
            fence_length=self._fence_length,
        )

    @staticmethod
    def _emit(status: StatusCallback | None, text: str) -> None:
        if text and status:
            status({"type": "delta", "content": text})

    def _flush(self, status: StatusCallback | None, *, final: bool) -> None:
        pending = self.pending
        if not pending:
            return
        masked, end_state = self._mask(pending)
        # 缓冲区开头正处于围栏内 ⇒ 这段是代码正文，不参与「开头是正文还是协议」判定
        # （掩码后是空格开头，_classify_agent_output 会判成 pending 并把代码块堵到流尾）。
        # 这里问的是「缓冲区开头长什么样」，所以用进入本块时的状态 self._in_fence。
        if not self._in_fence and StreamMixins._classify_agent_output(pending) == "tool":
            self.detected = True
            self.pending = ""
            return
        offset = StreamMixins._tool_protocol_offset(masked)
        if offset is not None:
            visible = pending[:offset]
            if visible:
                self._emit(status, visible)
                self._advance(visible)
            self.detected = True
            self.pending = ""
            return
        if final:
            self._emit(status, pending)
            self.pending = ""
            return
        # 「半个围栏标记」必须**无条件**留住：闭合围栏同样可能被 SSE 拆成两包
        # （"``" + "`"）。漏留会把半截当正文发出去，_advance 又按整行扫 ⇒
        # fence_close 认不出来、_in_fence 永久停在围栏内，之后真正的协议会被
        # 当成围栏里的代码正文整套放行（协议明文进 UI）。
        keep = _possible_fence_suffix_length(pending)
        if not end_state[0]:
            # 协议标记的半截只在**本块结束时已回到围栏外**才需要挽留。判据必须用本块
            # 结束状态而不是 self._in_fence：本块内闭合围栏后掩码已经不在围栏里，
            # 用进入时的状态会让「闭合围栏 + 协议前缀同块」整段外发——协议的前几个
            # 字符一旦流出，下一块里就没有 `{` / `<to` 可锚，协议其余部分全部按正文外发。
            keep = max(keep, StreamMixins._possible_protocol_suffix_length(pending))
        if keep >= len(pending):
            return
        visible = pending[:-keep] if keep else pending
        if visible:
            # 状态推进不能挂在 status 上：status=None 是真实路径（视觉识别、子代理），
            # 那时同样要把已放行的前缀从缓冲里去掉，围栏状态必须跟着推进，否则
            # 同一个输入有没有 UI 回调会走出两种状态机。
            self._emit(status, visible)
            self._advance(visible)
        self.pending = pending[-keep:] if keep else ""

    def _advance(self, forwarded: str) -> None:
        """把围栏状态推进到「已外发部分」的末尾；保留段下一轮从这个状态续扫。"""
        _masked, state = text_fences.fence_scan(
            forwarded,
            in_fence=self._in_fence,
            fence_char=self._fence_char,
            fence_length=self._fence_length,
        )
        self._in_fence, self._fence_char, self._fence_length = state


class StreamMixins:
    @staticmethod
    def _read_ollama_stream(
        response: Any,
        status: StatusCallback | None,
        reasoning_break_chars: float = 0.0,
        progress: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        chunks: list[dict[str, Any]] = []
        full_content_parts: list[str] = []
        guard = _ProtocolStreamGuard()
        reasoning_parts: list[str] = []
        reasoning_streamer = _ReasoningStreamer(status, reasoning_parts)
        reasoning_chars = 0
        inline_parser = _InlineReasoningParser()
        native_tool_calls: dict[int, dict[str, str]] = {}
        for raw_line in response:
            try:
                chunk = json.loads(raw_line.decode("utf-8", errors="replace"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(chunk, dict):
                continue
            # Ollama 把后端错误（含「超出上下文长度」）作为一行 `{"error": "..."}` 直接
            # 塞在流里。此前这个字段整块被吞掉，用户在界面上只能看到「流式响应中没有
            # 文本内容」——真实原因（窗口不够 / 模型未加载 / 参数非法）完全不可见。
            error = chunk.get("error")
            if error not in (None, "", False, {}, []):
                raise _stream_error("Ollama", error)
            chunks.append(chunk)
            message = chunk.get("message") or {}
            for index, raw_call in enumerate(message.get("tool_calls") or [] if isinstance(message, dict) else []):
                if not isinstance(raw_call, dict):
                    continue
                function = raw_call.get("function") or {}
                raw_arguments = function.get("arguments", {})
                arguments = raw_arguments if isinstance(raw_arguments, str) else json.dumps(raw_arguments, ensure_ascii=False)
                native_tool_calls[index] = {
                    "id": str(raw_call.get("id") or ""),
                    "name": str(function.get("name") or ""),
                    "arguments": arguments,
                }
            text, reasoning = StreamMixins._ollama_stream_delta(chunk)
            text, inline_reasoning = inline_parser.feed(text)
            reasoning = reasoning + inline_reasoning
            if reasoning:
                reasoning_chars += len(reasoning)
                reasoning_streamer.feed(reasoning)
            if not text:
                _mark_progress(progress, reasoning_chars, bool(full_content_parts) or bool(native_tool_calls))
                _break_on_runaway_reasoning(
                    reasoning_chars, reasoning_break_chars,
                    bool(full_content_parts) or bool(native_tool_calls), status,
                )
                continue
            full_content_parts.append(text)
            _mark_progress(progress, reasoning_chars, True)
            guard.feed(text, status)
        final_text, final_reasoning = inline_parser.feed("", final=True)
        if final_reasoning:
            reasoning_streamer.feed(final_reasoning)
        if final_text:
            full_content_parts.append(final_text)
            guard.feed(final_text, status)
        guard.finish(status)
        reasoning_streamer.finish()
        return {
            "content": (
                ProtocolMixins._build_action_from_native_tool_calls(native_tool_calls)
                if native_tool_calls
                else StreamMixins._clean_content("".join(full_content_parts))
            ),
            "reasoning": "".join(reasoning_parts),
            "usage": ProtocolMixins._online_usage("ollama", chunks),
            "finish_reason": ProtocolMixins._online_finish_reason("ollama", chunks),
        }


    @staticmethod
    def _ollama_stream_delta(chunk: dict[str, Any]) -> tuple[str, str]:
        message = chunk.get("message") or {}
        if not isinstance(message, dict):
            return "", ""
        return str(message.get("content") or ""), str(message.get("thinking") or "")


    @staticmethod
    def _read_sse_response(
        response: Any,
        request_format: str,
        status: StatusCallback | None,
        reasoning_break_chars: float = 0.0,
        progress: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Collect SSE chunks while forwarding only ordinary prose to the chat client.

        Agent tool protocols (JSON actions, ``<tool_calls>`` / ``<invoke>``,
        DeepSeek ``<tool name="...">`` and native OpenAI ``tool_calls``) are
        buffered but never sent as ``delta`` events—they only reach the Agent
        Loop as the parsed action returned by ``complete``.

        ``reasoning_break_chars``：思考预算熔断（见 ``_break_on_runaway_reasoning``）；
        纯解析层不读 options，由调用处透传。
        """
        chunks: list[dict[str, Any]] = []
        full_content_parts: list[str] = []
        guard = _ProtocolStreamGuard()
        reasoning_parts: list[str] = []
        reasoning_ids: list[str] = []
        reasoning_chars = 0
        reasoning_streamer = _ReasoningStreamer(status, reasoning_parts)
        native_tool_calls: dict[int, dict[str, str]] = {}
        inline_parser = _InlineReasoningParser()
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            # 有些本地后端（llama.cpp / Unsloth 的部分版本、部分兼容网关）不返回 4xx，
            # 而是把错误作为 SSE 事件嵌在流里。正常事件（含 codex_responses 的
            # output_item.* / response.* 与 OpenAI 的 choices delta）都不带顶层 `error`
            # 键，故用真值判定即可，不会误伤正常流。此前这类错误事件被静默跳过，最终
            # 只会以「流式响应中没有文本内容」收场。
            error = chunk.get("error")
            if error not in (None, "", False, {}, []):
                raise _stream_error(
                    _STREAM_SOURCE_LABELS.get(request_format, "模型服务"), error
                )
            chunks.append(chunk)
            event_type = str(chunk.get("type") or "")
            # 捕获 reasoning item 的服务端唯一 id：output_item.added 事件携带 item
            # 对象；delta 事件以 item_id 兜底（OpenAI 规范字段）。回传时需要（官方
            # schema reasoning item 的 id 为必填；缺失在多轮长链实测 400）。
            if request_format == "codex_responses" and event_type == "response.output_item.added":
                item = chunk.get("item") or {}
                if isinstance(item, dict) and item.get("type") == "reasoning":
                    iid = str(item.get("id") or "")
                    if iid and iid not in reasoning_ids:
                        reasoning_ids.append(iid)
            elif request_format == "codex_responses" and "reasoning" in event_type and event_type.endswith("delta"):
                iid = str(chunk.get("item_id") or "")
                if iid and iid not in reasoning_ids:
                    reasoning_ids.append(iid)
            text, reasoning, tool_calls = ProtocolMixins._stream_delta_full(request_format, chunk)
            text, inline_reasoning = inline_parser.feed(text)
            reasoning = reasoning + inline_reasoning
            if reasoning:
                reasoning_chars += len(reasoning)
                reasoning_streamer.feed(reasoning)
            if tool_calls:
                # Native OpenAI tool calls must not appear as answer text.
                guard.finish(status)
                guard.detected = True
                for call in tool_calls:
                    slot = native_tool_calls.setdefault(
                        call.get("index", 0), {"id": "", "name": "", "arguments": ""}
                    )
                    if call.get("id"):
                        slot["id"] = call["id"]
                    if call.get("name"):
                        slot["name"] += call["name"]
                    if call.get("arguments") is not None:
                        slot["arguments"] += call["arguments"]
                continue
            if not text:
                has_content = bool(full_content_parts) or guard.detected or bool(native_tool_calls)
                _mark_progress(progress, reasoning_chars, has_content)
                # 只有「正文仍为空」的思考增量才可能触发熔断；原生工具调用已到位时不算空转。
                _break_on_runaway_reasoning(
                    reasoning_chars, reasoning_break_chars, has_content, status,
                )
                continue
            full_content_parts.append(text)
            _mark_progress(progress, reasoning_chars, True)
            guard.feed(text, status)
        final_text, final_reasoning = inline_parser.feed("", final=True)
        if final_reasoning:
            reasoning_streamer.feed(final_reasoning)
        if final_text:
            full_content_parts.append(final_text)
            guard.feed(final_text, status)
        # codex_responses 中继可能只回聚合事件（response.completed /
        # response.output_item.done）而不逐段发 output_text.delta。增量正文为空时
        # 从这里回填正文/思考/reasoning_id/tool action，避免被误判为空流。
        aggregated_action = ""
        if (
            request_format == "codex_responses"
            and not native_tool_calls
            and not guard.detected
        ):
            agg_text, agg_reasoning, agg_id, aggregated_action = (
                ProtocolMixins._codex_responses_aggregated(chunks)
            )
            if agg_id and agg_id not in reasoning_ids:
                reasoning_ids.append(agg_id)
            if agg_reasoning and not reasoning_parts:
                reasoning_streamer.feed(agg_reasoning)
            if not aggregated_action and agg_text and not "".join(full_content_parts).strip():
                full_content_parts.append(agg_text)
                guard.feed(agg_text, status)
        guard.finish(status)
        reasoning_streamer.finish()
        usage = ProtocolMixins._online_usage(request_format, chunks)
        if native_tool_calls:
            # Convert to the internal action structure the Agent Loop consumes.
            content = ProtocolMixins._build_action_from_native_tool_calls(native_tool_calls)
        elif aggregated_action:
            # 仅聚合事件的 function_call：镜像非流式 _online_response 的 action 优先。
            content = aggregated_action
        else:
            content = StreamMixins._clean_content("".join(full_content_parts))
        return {
            "content": content,
            "reasoning": "".join(reasoning_parts),
            "reasoning_id": reasoning_ids[-1] if reasoning_ids else "",
            "usage": usage,
            "finish_reason": ProtocolMixins._online_finish_reason(request_format, chunks),
        }


    @staticmethod
    def _read_lm_studio_stream(
        response: Any,
        status: StatusCallback | None,
        reasoning_break_chars: float = 0.0,
        progress: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """按 LM Studio 原生 chat SSE 事件解析（``message.delta`` / ``reasoning.delta`` / ``error`` / ``chat.end``）。

        与 OpenAI 兼容流不同，LM Studio 的 ``type`` 事件直接携带 ``content`` 增量；结构化错误事件
        为 ``{"type":"error","error":...}``，结束事件为 ``{"type":"chat.end"}``。
        """
        chunks: list[dict[str, Any]] = []
        full_content_parts: list[str] = []
        guard = _ProtocolStreamGuard()
        reasoning_parts: list[str] = []
        reasoning_streamer = _ReasoningStreamer(status, reasoning_parts)
        reasoning_chars = 0
        inline_parser = _InlineReasoningParser()
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            chunks.append(chunk)
            event_type = str(chunk.get("type") or "")
            if event_type == "error":
                error = chunk.get("error") or "LM Studio 返回未知错误"
                if isinstance(error, dict):
                    error = str(error.get("message") or error.get("details") or error)
                # 统一走 `_stream_error`：溢出（LM Studio 的「greater than the context
                # length (n_keep: …, n_ctx: …)」）转成专用错误，其余保留原文。
                raise _stream_error("LM Studio", error)
            if event_type in ("chat.end", "message.end", "reasoning.end", "message.start", "reasoning.start"):
                continue
            if event_type in ("reasoning.delta", "reasoning.full"):
                reasoning = ProtocolMixins._text_value(chunk.get("content"))
                if reasoning:
                    reasoning_chars += len(reasoning)
                    reasoning_streamer.feed(reasoning)
                    has_content = bool(full_content_parts) or guard.detected
                    _mark_progress(progress, reasoning_chars, has_content)
                    _break_on_runaway_reasoning(
                        reasoning_chars, reasoning_break_chars, has_content, status,
                    )
                continue
            if event_type in ("message.delta", "message.full"):
                text = ProtocolMixins._text_value(chunk.get("content"))
            else:
                # 未知事件但可能带有正文/文本字段（兼容字段命名）。
                text = ProtocolMixins._text_value(chunk.get("content") or chunk.get("text"))
            text, inline_reasoning = inline_parser.feed(text)
            if inline_reasoning:
                reasoning_chars += len(inline_reasoning)
                reasoning_streamer.feed(inline_reasoning)
            if not text:
                has_content = bool(full_content_parts) or guard.detected
                _mark_progress(progress, reasoning_chars, has_content)
                _break_on_runaway_reasoning(
                    reasoning_chars, reasoning_break_chars, has_content, status,
                )
                continue
            full_content_parts.append(text)
            _mark_progress(progress, reasoning_chars, True)
            guard.feed(text, status)
        final_text, final_reasoning = inline_parser.feed("", final=True)
        if final_reasoning:
            reasoning_streamer.feed(final_reasoning)
        if final_text:
            full_content_parts.append(final_text)
            guard.feed(final_text, status)
        guard.finish(status)
        reasoning_streamer.finish()
        return {
            "content": StreamMixins._clean_content("".join(full_content_parts)),
            "reasoning": "".join(reasoning_parts),
            "usage": ProtocolMixins._online_usage("lm_studio", chunks) if chunks else {},
            "finish_reason": ProtocolMixins._online_finish_reason("lm_studio", chunks),
        }


    @staticmethod
    def _classify_agent_output(buffer: str) -> str:
        """Classify the leading model output before forwarding it to the chat UI.

        Returns ``"tool"`` for an agent tool-call protocol (kept out of the
        visible answer), ``"text"`` for ordinary prose (safe to stream), or
        ``"pending"`` when the buffer is too short to decide confidently.
        """
        probe = buffer.lstrip()
        if not probe:
            return "pending"
        first = probe[0]
        if first in "{[":
            # JSON object / array agent action is never part of the answer, but
            # only when it actually carries the action-style "type"/"tool" key.
            # A bare "{" or "[" that is simply the tail of streamed prose/code
            # (e.g. Java braces, "[Shot 1]" labels) must be forwarded as text,
            # otherwise multi-code-block answers stall until the stream ends.
            if re.search(r'"\s*(?:type|tool)"\s*:', probe[:200]):
                return "tool"
            return "text"
        if first == "<":
            match = re.match(r"^<([A-Za-z][\w-]*)", probe)
            if not match:
                return "text" if (">" in probe[:64] or len(probe) > 64) else "pending"
            tag = match.group(1).lower()
            if tag in {"tool_calls", "invoke"}:
                return "tool"
            if tag == "tool":
                # DeepSeek named-tool dialect: <tool name="..."> (optionally
                # wrapped in <tool type="tool">). Require a name/type attribute.
                if _TOOL_NAMED_ATTR.search(probe[:200]):
                    return "tool"
                if ">" in probe[:200]:
                    return "text"
                return "pending" if len(probe) <= _AGENT_BUFFER_LIMIT else "text"
            # Another tag (markdown/HTML in prose, <think>, ...): decide once
            # the opening tag closes; otherwise keep buffering briefly.
            if ">" in probe[:200]:
                return "text"
            return "pending" if len(probe) <= _AGENT_BUFFER_LIMIT else "text"
        return "text"


    @staticmethod
    def _tool_protocol_offset(buffer: str) -> int | None:
        """Return the earliest XML/JSON agent protocol marker in ``buffer``."""
        offsets = []
        xml_match = _TOOL_PROTOCOL_ANYWHERE.search(buffer)
        if xml_match:
            offsets.append(xml_match.start())
        harmony_match = _HARMONY_TOOL_ANYWHERE.search(buffer)
        if harmony_match:
            offsets.append(harmony_match.start())
        json_match = _JSON_TOOL_ANYWHERE.search(buffer)
        if json_match:
            offsets.append(json_match.start())
        return min(offsets) if offsets else None


    @staticmethod
    def _possible_protocol_suffix_length(buffer: str) -> int:
        """Return only the ambiguous suffix that must wait for the next chunk.

        Ordinary answer text should be forwarded immediately.  We retain a
        short partial XML marker (for example ``<tool_ca``) or a partial JSON
        first field (for example ``{\"ty``), rather than delaying every stream
        by a fixed number of characters.
        """
        lower = buffer.lower()
        keep = 0
        for token in (
            "<tool_calls", "<invoke", "<tool",
            "<|open|>tools", "<|open|>call",
        ):
            limit = min(len(token) - 1, len(lower))
            for size in range(1, limit + 1):
                if token.startswith(lower[-size:]):
                    keep = max(keep, size)

        brace = buffer.rfind("{")
        if brace >= 0:
            fragment = buffer[brace:]
            rest = fragment[1:].lstrip()
            possible = not rest
            if rest.startswith('"'):
                field = rest[1:]
                if '"' in field:
                    name, tail = field.split('"', 1)
                    possible = name.lower() in {"type", "tool"} and not tail.strip()
                else:
                    possible = any(name.startswith(field.lower()) for name in ("type", "tool"))
            if possible:
                keep = max(keep, len(fragment))
        return keep


    @staticmethod
    def _emit_buffered_reasoning(status: StatusCallback | None, reasoning: str) -> None:
        """Publish reasoning only after the response is known to be user-facing."""
        if not status or not reasoning.strip():
            return
        status({"type": "reasoning_start"})
        status({"type": "reasoning_delta", "content": reasoning})
        status({"type": "reasoning_end"})


    @staticmethod
    def _forward_guarded_text(
        pending: str,
        status: StatusCallback | None,
        final: bool = False,
        guard: "_FenceGuard | None" = None,
    ) -> tuple[str, bool]:
        """Forward safe prose while retaining enough tail to catch tool XML/JSON.

        Returns the unflushed tail and whether a tool protocol was detected.
        Once detected, callers suppress the remainder of that model response.

        围栏感知（计划 2026-10-01 §3.4）：``guard`` 跨 chunk 跟踪围栏开合，
        **围栏内不扫描协议标记**——代码块里的示例 JSON/XML/Harmony 会被当作正文
        正常下发，围栏前后的正文也都能继续产生 delta。未闭合围栏到流尾按正文放行。
        """
        if guard is None:
            guard = _FenceGuard()
        regions, fence_keep = guard.regions(pending, final)
        masked = _mask_outside(buffer=pending, regions=regions)
        classification = StreamMixins._classify_agent_output(masked)
        if classification == "tool":
            return "", True
        offset = StreamMixins._tool_protocol_offset(masked)
        if offset is not None:
            visible = pending[:offset]
            if visible and status:
                status({"type": "delta", "content": visible})
            return "", True
        keep = 0 if final else max(fence_keep, StreamMixins._possible_protocol_suffix_length(masked))
        if keep >= len(pending):
            return pending, False
        visible = pending[: len(pending) - keep] if keep else pending
        if visible and status:
            status({"type": "delta", "content": visible})
        return pending[len(pending) - keep:] if keep else "", False


    @staticmethod
    def _clean_content(content: str) -> str:
        text = content.strip()
        if "</think>" in text:
            text = text.rsplit("</think>", 1)[-1]
        text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
        return text.strip()
