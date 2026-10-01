# -*- coding: utf-8 -*-
"""文本围栏（Markdown code fence）识别：`` ``` `` 与 `` ~~~ ``。

单一事实来源，供两条链路共用（避免「实时看到一部分、最终保存另一部分」）：

1. **终态解析**（``skills/agent.py``）：整条响应本身就是一个围栏块时，围栏内容作为协议候选
   （来源标记 ``fenced``）；**围栏内的内容永远不作为「裸协议」候选**——示例代码块里的
   ``{"type":"tool",...}`` 不会被当成真实动作。
2. **流式护栏**（``llm/stream.py``）：跟踪围栏开合状态，围栏内不扫描协议标记，未闭合围栏
   到流尾按正文放行。

行级规则（与 CommonMark 一致到够用为止）：

- 开启行：行首最多 3 个空格/制表符 + 连续 ≥3 个 ``` 或 ~~~；反引号围栏的 info string
  不得包含反引号。
- 闭合行：同字符、长度 ≥ 开启长度、其后只有空白。
- 未闭合的围栏一直延伸到文本末尾（`closed=False`）。

所有偏移都是**原文字符偏移**：`fence_mask` 逐字符替换、长度不变，因此可以「先掩码再扫描，
再按原偏移取回原文」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FENCE_BACKTICK = "`"
FENCE_TILDE = "~"

_OPEN_RE = re.compile(r"^(?P<indent>[ \t]{0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_FENCE_NOISE_RE = re.compile(r"^[ \t]*(?:`{3,}|~{3,})[ \t]*$")


@dataclass(frozen=True)
class FenceSpan:
    """一段围栏在原文中的位置（偏移均为原文字符下标）。"""

    start: int          # 开启行起点
    content_start: int  # 开启行之后（含换行）
    content_end: int    # 闭合行起点（未闭合时等于 len(text)）
    end: int            # 闭合行之后（未闭合时等于 len(text)）
    char: str
    length: int
    closed: bool


def parse_fence_open(line: str) -> tuple[str, int, str] | None:
    """一行（**不含换行**）若是围栏开启行，返回 ``(char, length, info)``。"""
    match = _OPEN_RE.match(line)
    if not match:
        return None
    fence = match.group("fence")
    char = fence[0]
    info = match.group("info")
    if char == FENCE_BACKTICK and FENCE_BACKTICK in info:
        return None
    return char, len(fence), info


def is_fence_close_line(line: str, char: str, length: int) -> bool:
    """一行（**不含换行**）是否闭合 ``char``/``length`` 开启的围栏。"""
    match = _OPEN_RE.match(line)
    if not match:
        return False
    fence = match.group("fence")
    if fence[0] != char or len(fence) < length:
        return False
    return match.group("info").strip() == ""


def is_fence_noise(text: str) -> bool:
    """``text`` 是否只是空白与孤立的围栏行（没有实质正文）。"""
    for line in text.splitlines():
        if not line.strip():
            continue
        if not _FENCE_NOISE_RE.match(line):
            return False
    return True


def iter_fences(text: str) -> list[FenceSpan]:
    """按行扫描出 ``text`` 中全部围栏段（未闭合的段延伸到文本末尾）。"""
    spans: list[FenceSpan] = []
    if not text:
        return spans
    lines = text.splitlines(keepends=True)
    offset = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        opened = parse_fence_open(line.rstrip("\r\n"))
        if opened is None:
            offset += len(line)
            index += 1
            continue
        char, length, _info = opened
        start = offset
        content_start = offset + len(line)
        cursor = content_start
        closed = False
        content_end = len(text)
        end = len(text)
        probe = index + 1
        while probe < len(lines):
            candidate = lines[probe]
            if is_fence_close_line(candidate.rstrip("\r\n"), char, length):
                content_end = cursor
                end = cursor + len(candidate)
                closed = True
                break
            cursor += len(candidate)
            probe += 1
        spans.append(
            FenceSpan(
                start=start,
                content_start=content_start,
                content_end=content_end,
                end=end,
                char=char,
                length=length,
                closed=closed,
            )
        )
        if not closed:
            break
        offset = end
        index = probe + 1
    return spans


def fence_mask(text: str) -> str:
    """把全部围栏段（含 `` ``` `` 标记行本身）替换成**等长空格**。

    换行保留 ⇒ 行结构不变、偏移逐字符对齐，调用方可以「在掩码文本上定位协议、
    用原偏移切回原文」。围栏内的示例协议因此对裸协议扫描**不可见**。
    """
    if not text:
        return text
    chars = list(text)
    for span in iter_fences(text):
        for position in range(span.start, min(span.end, len(chars))):
            if chars[position] not in "\r\n":
                chars[position] = " "
    return "".join(chars)


def unwrap_whole_response_fence(raw: str) -> tuple[str, str] | None:
    """整条响应就是一个完整围栏块时，返回 ``(inner, char)``；否则 ``None``。

    判定：去掉首尾空白后，**第一行**是围栏开启行、**最后一行**是同一围栏的闭合行。
    带自然语言前言（「以下是示例：```json … ```」）或围栏后面还有正文的形态一律不匹配
    —— 这是本计划接受的已知限制（见计划 §3.6 限制 1）。
    """
    text = (raw or "").strip()
    if not text:
        return None
    lines = text.split("\n")
    if len(lines) < 2:
        return None
    opened = parse_fence_open(lines[0].rstrip("\r"))
    if opened is None:
        return None
    char, length, _info = opened
    if not is_fence_close_line(lines[-1].rstrip("\r"), char, length):
        return None
    return "\n".join(lines[1:-1]), char


def partial_fence_opener_length(buffer: str) -> int:
    """流式场景：``buffer`` 末尾**未完结的一行**若可能是围栏开启行，返回需保留的长度。

    - 只含 1~2 个围栏字符（还可能长成 3 个）：必须等待；
    - 已有 ≥3 个围栏字符（已确定是开启行，info string 可能还在增长）：必须等待行结束，
      否则会把形如 `` ```json `` 的开启行提前当正文发出去；
    - 行内已出现其它字符且不足 3 个围栏字符（如 ``说明``）：不是围栏，返回 0。
    """
    if not buffer:
        return 0
    line_start = buffer.rfind("\n") + 1
    line = buffer[line_start:]
    if not line or line.endswith("\n"):
        return 0
    stripped = line.lstrip(" \t")
    indent = len(line) - len(stripped)
    if indent > 3 or not stripped:
        return 0
    char = stripped[0]
    if char not in (FENCE_BACKTICK, FENCE_TILDE):
        return 0
    run = 0
    for piece in stripped:
        if piece != char:
            break
        run += 1
    rest = stripped[run:]
    if char == FENCE_BACKTICK and FENCE_BACKTICK in rest:
        return 0
    if run >= 3:
        return len(line)
    return len(line) if not rest else 0
