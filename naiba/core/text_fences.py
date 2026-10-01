# -*- coding: utf-8 -*-
"""文本围栏（Markdown code fence）识别：`` ``` `` 与 `` ~~~ ``。**全项目唯一口径**。

单一事实来源，供两条链路共用（避免「实时看到一部分、最终保存另一部分」）：

1. **终态解析**（``skills/agent.py``）：整条响应本身就是一个围栏块时，围栏内容作为协议候选
   （来源标记 ``fenced``）；**围栏内的内容永远不作为「裸协议」候选**——示例代码块里的
   ``{"type":"tool",...}`` 不会被当成真实动作。
2. **流式护栏**（``llm/stream.py``）：跟踪围栏开合状态（跨 chunk 续扫），围栏内不扫描协议
   标记，未闭合围栏到流尾按正文放行。

行级规则（与 CommonMark 一致到够用为止）：

- 开启行：行首最多 3 个空格/制表符 + 连续 ≥3 个 ``` 或 ~~~；反引号围栏的 info string
  不得包含反引号。
- 闭合行：同字符、长度 ≥ 开启长度、其后只有空白。
- 未闭合的围栏一直延伸到文本末尾（`closed=False`）。

所有偏移都是**原文字符偏移**：掩码逐字符替换、长度不变，因此可以「先掩码再扫描，
再按原偏移取回原文」。

命名分两族，语义同源、用法不同（并存是为了两侧调用方零改动）：

- 终态族：``fence_mask`` / ``iter_fences`` / ``parse_fence_open`` / ``is_fence_close_line`` /
  ``is_fence_noise`` / ``partial_fence_opener_length`` / ``unwrap_whole_response_fence``；
- 状态族：``fence_scan``（跨 chunk）/ ``mask_fenced_code`` / ``fence_run`` / ``fence_open`` /
  ``fence_close`` / ``is_fence_line`` / ``only_fence_tail`` / ``in_final_block``。

``unwrap_whole_response_fence`` 只有**一个**返回契约：``(inner, char)`` 或 ``None``。
带自然语言前言、或围栏后面还有正文的形态一律不匹配（已知限制，见计划 §3.6 限制 1）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FENCE_BACKTICK = "`"
FENCE_TILDE = "~"
FENCE_CHARACTERS = (FENCE_BACKTICK, FENCE_TILDE)
MIN_FENCE_LENGTH = 3
MAX_FENCE_INDENT = 3

_OPEN_RE = re.compile(r"^(?P<indent>[ \t]{0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
# 只有围栏标记、没有 info string 的行（尾锚定判据的一部分）
_FENCE_NOISE_RE = re.compile(r"^[ \t]*(?:`{3,}|~{3,})[ \t]*$")
# 空行（只含空白的行）：段落边界，`in_final_block` 用它判断标记是否落在最后一段。
_BLANK_LINE = re.compile(r"\n[ \t]*\n")
# 协议自身的**收尾标签**：动作后面只跟这些不算"还接正文"。DeepSeek 兼容端点会把
# 命名工具包在外层 ``<tool type="tool">`` 里、再用 ``</invoke>`` 收尾，正则取到内层
# ``<tool name=…>…</tool>`` 之后剩下的就是这些标签——不放行会把已上线方言误判成
# "协议后面接正文"，整条方言退化成 parse_error（工具不再执行）。
_CLOSING_TAG = re.compile(r"</[A-Za-z][\w:-]*\s*>")


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


def fence_run(line: str) -> tuple[str, int, str] | None:
    """行首（缩进 ≤3 个空格/制表符）的 `` ``` `` / ``~~~`` 标记 → ``(字符, 长度, 行尾剩余)``。

    不是围栏行返回 ``None``。行内代码（``x = `y``` ` ``）不匹配——标记必须在行首。
    """
    text = str(line or "")
    indent = len(text) - len(text.lstrip(" \t"))
    if indent > MAX_FENCE_INDENT:
        return None
    body = text[indent:]
    if not body or body[0] not in FENCE_CHARACTERS:
        return None
    char = body[0]
    length = 0
    while length < len(body) and body[length] == char:
        length += 1
    if length < MIN_FENCE_LENGTH:
        return None
    return char, length, body[length:]


def parse_fence_open(line: str) -> tuple[str, int, str] | None:
    """一行（**不含换行**）若是围栏开启行，返回 ``(char, length, info)``。

    反引号围栏的信息串里不得再出现反引号（CommonMark 口径），否则
    ```` ```x = `y` ``` ```` 这类行会被误判成代码块起点、把它后面的正文整段吞掉。
    """
    run = fence_run(line)
    if run is None:
        return None
    char, length, info = run
    if char == FENCE_BACKTICK and FENCE_BACKTICK in info:
        return None
    return char, length, info


def fence_open(line: str) -> tuple[str, int] | None:
    """该行是否为**开启**围栏：返回 ``(字符, 长度)``；否则 ``None``。"""
    opened = parse_fence_open(line)
    if opened is None:
        return None
    return opened[0], opened[1]


def is_fence_close_line(line: str, char: str, length: int) -> bool:
    """一行（**不含换行**）是否闭合 ``char``/``length`` 开启的围栏。"""
    run = fence_run(line)
    if run is None:
        return False
    found_char, found_length, rest = run
    return found_char == char and found_length >= length and not rest.strip()


def fence_close(line: str, char: str, length: int) -> bool:
    """该行是否为匹配闭合围栏：同字符、长度 ≥ 开启长度、行尾只允许空白。"""
    return is_fence_close_line(line, char, length)


def is_fence_line(line: str) -> bool:
    """该行是否是**任意**围栏标记行（开或闭都算）——用于识别尾部残留的闭合行。"""
    return fence_run(line) is not None


def is_fence_noise(text: str) -> bool:
    """``text`` 是否只是空白与孤立的围栏行（没有实质正文）。"""
    for line in str(text or "").splitlines():
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


def _blank(chars: list[str], start: int, end: int) -> None:
    """把 ``[start, end)`` 的非换行字符换成空格（换行/回车必须留着，否则行结构变了）。"""
    for position in range(start, min(end, len(chars))):
        if chars[position] not in "\r\n":
            chars[position] = " "


def fence_mask(text: str) -> str:
    """把全部围栏段（含 `` ``` `` 标记行本身）替换成**等长空格**。

    换行保留 ⇒ 行结构不变、偏移逐字符对齐，调用方可以「在掩码文本上定位协议、
    用原偏移切回原文」。围栏内的示例协议因此对裸协议扫描**不可见**。
    """
    source = str(text or "")
    if not source:
        return source
    chars = list(source)
    for span in iter_fences(source):
        _blank(chars, span.start, span.end)
    return "".join(chars)


FenceState = tuple[bool, str, int]


def fence_scan(
    text: str,
    *,
    in_fence: bool = False,
    fence_char: str = "",
    fence_length: int = 0,
) -> tuple[str, FenceState]:
    """带状态的掩码：返回 ``(掩码文本, 扫描结束时的围栏状态)``，跨 chunk 续扫用同一份状态。

    流式层的围栏标记可能被 SSE 拆成两包（前一块以 ``` 开头、后一块才是闭合行），
    所以「这段文本处于围栏内吗」必须由调用方持有状态；终态层一次拿到整文，
    用默认状态即可。未闭合的围栏一路掩到文本结尾（模型被切断时没有闭合行，
    不能反过来把后续内容当协议吞掉）。
    """
    source = str(text or "")
    if not source:
        return source, (bool(in_fence), str(fence_char or ""), int(fence_length or 0))
    out = list(source)
    total = len(source)
    state: FenceState = (bool(in_fence), str(fence_char or ""), int(fence_length or 0))
    index = 0
    while index < total:
        line_start = index
        newline = source.find("\n", line_start)
        line_end = total if newline < 0 else newline
        line = source[line_start:line_end]
        if state[0]:
            _blank(out, line_start, line_end)
            if fence_close(line, state[1], state[2]):
                state = (False, "", 0)
        else:
            opened = fence_open(line)
            if opened:
                _blank(out, line_start, line_end)
                state = (True, opened[0], opened[1])
        index = line_end + 1
    return "".join(out), state


def mask_fenced_code(text: str) -> str:
    """把每个围栏区间（含围栏行本身）替换为等长空格；换行原样保留 ⇒ 偏移逐字符保持。"""
    return fence_scan(text)[0]


def unwrap_whole_response_fence(raw: str) -> tuple[str, str] | None:
    """整条响应就是一个完整围栏块时，返回 ``(inner, char)``；否则 ``None``。

    判定：去掉首尾空白后，**第一行**是围栏开启行、**最后一行**是同一围栏的闭合行。
    带自然语言前言（「以下是示例：```json … ```」）或围栏后面还有正文的形态一律不匹配。
    部分兼容端点会把协议整块包进 ```json / ```xml，这是**有意保留**的既有支持；
    它与「正文中间夹一个代码块」是两件事——前者整条回答都是协议，后者是讲解用的示例。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    lines = text.split("\n")
    if len(lines) < 2:
        return None
    opened = parse_fence_open(lines[0].rstrip("\r"))
    if opened is None:
        return None
    char, length, _info = opened
    last_index = len(lines) - 1
    while last_index > 0 and not lines[last_index].strip():
        last_index -= 1
    if last_index <= 0 or not is_fence_close_line(lines[last_index].rstrip("\r"), char, length):
        return None
    return "\n".join(lines[1:last_index]), char


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
    if indent > MAX_FENCE_INDENT or not stripped:
        return 0
    char = stripped[0]
    if char not in FENCE_CHARACTERS:
        return 0
    run = 0
    for piece in stripped:
        if piece != char:
            break
        run += 1
    rest = stripped[run:]
    if char == FENCE_BACKTICK and FENCE_BACKTICK in rest:
        return 0
    if run >= MIN_FENCE_LENGTH:
        return len(line)
    return len(line) if not rest else 0


def only_fence_tail(text: str, index: int, *, closers: bool = False) -> bool:
    """``index`` 之后是否只剩空白、围栏标记行（可选：协议自身的收尾标签）。

    「协议必须顶到回答末尾」是终态层的判据：工具动作后面还接正文的输出**不再执行该动作**，
    整体按正文展示——与旧行为（静默执行动作、静默丢掉正文）相反，宁可不动作也不吞内容。

    ``closers=True`` 额外放行 ``</tool>`` / ``</invoke>`` 这类收尾标签：命名工具方言
    （``<tool type="tool">`` 里再包一层 ``<tool name=…>``）取到内层块后，尾部落的正是外层
    收尾标签，按"正文"处理会把整条已上线方言打成 parse_error。

    ⚠️ 调用方必须传**原文**：掩码文本里一个孤立的 ```` ``` ```` 会开启假围栏、把它后面的
    正文一并掩成空格，尾锚定就会误判通过（动作被执行、尾部正文却消失）。
    """
    source = str(text or "")
    rest = source[max(0, int(index)):]
    if not rest.strip():
        return True
    if closers:
        rest = _CLOSING_TAG.sub(" ", rest)
    return all(not line.strip() or is_fence_line(line) for line in rest.split("\n"))


def in_final_block(text: str, index: int) -> bool:
    """``index`` 之后没有空行 ⇒ 标记落在回答的最后一段里。

    只用于**协议被切断**（解析不出来）时判定「这确实是被截断的动作」：截断总是停在
    最后一段的中途。中间位置的裸 JSON 不算协议，按正文放行。
    """
    source = str(text or "")
    return _BLANK_LINE.search(source[max(0, int(index)):]) is None
