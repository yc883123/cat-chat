"""Markdown 围栏代码块识别（纯函数、零 IO）。

**为什么要有这个模块**：Agent 的工具协议本身就是「正文位置的纯文本」——模型要调工具，
就是直接在回答里输出 ``{"type":"tool",...}`` 或 ``<tool name=…>``，后端没有独立字段可依据，
只能扫文本。而流式层与终态层此前都在**任意位置**扫协议标记，于是正文里的
```` ```json ```` 示例会被当成协议：

* 流式层（``llm/stream.py``）在标记处「吐出前面的正文 + 判定协议」，**该次响应剩余正文
  一条 delta 都不再发** ⇒ 界面正好停在围栏行；
* 终态层（``skills/agent.py``）更进一步：``_extract_json`` 对**任意位置**的 ``{`` 做
  ``raw_decode``，把示例里的 ``{"type":"tool","tool":"pwsh",…}`` 当成真动作**直接执行**。

唯一可靠的区分依据就是 Markdown 围栏：**围栏内一律不算协议**。两侧必须共用这一份口径，
否则会出现「流式把正文放出去了、终态仍判 parse_error」的半修状态。

两条硬约束：

1. **偏移保持**：掩码只把围栏区间换成等长空格（换行仍是换行），因此调用方可以
   「在掩码文本上定位标记 → 到原文同一偏移取内容」；
2. **未闭合围栏按正文放行**：模型输出被切断时围栏常常没有闭合行，这时**不能**把它
   后面的一切当协议吞掉——只有真正闭合的围栏才是代码块。
"""
from __future__ import annotations

import re

FENCE_CHARACTERS = ("`", "~")
MIN_FENCE_LENGTH = 3
MAX_FENCE_INDENT = 3
# 空行（只含空白的行）：段落边界，`in_final_block` 用它判断标记是否落在最后一段。
_BLANK_LINE = re.compile(r"\n[ \t]*\n")


def fence_run(line: str) -> tuple[str, int, str] | None:
    """行首（缩进 ≤3 空格）的 ````` ``` ````` / ``~~~`` 标记 → ``(字符, 长度, 行尾剩余)``。

    不是围栏行返回 ``None``。行内代码（``x = `y``` ` ``）不匹配——标记必须在行首。
    """
    text = str(line or "")
    indent = len(text) - len(text.lstrip(" "))
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


def fence_open(line: str) -> tuple[str, int] | None:
    """该行是否为**开启**围栏：返回 ``(字符, 长度)``；否则 ``None``。

    反引号围栏的信息串里不得再出现反引号（CommonMark 口径），否则
    ```` ```x = `y` ``` ```` 这类行会被误判成代码块起点、把它后面的正文整段吞掉。
    """
    run = fence_run(line)
    if not run:
        return None
    char, length, rest = run
    if char == "`" and "`" in rest:
        return None
    return char, length


def fence_close(line: str, char: str, length: int) -> bool:
    """该行是否为匹配闭合围栏：同字符、长度 ≥ 开启长度、行尾只允许空白。"""
    run = fence_run(line)
    if not run:
        return False
    found_char, found_length, rest = run
    return found_char == char and found_length >= length and not rest.strip()


def is_fence_line(line: str) -> bool:
    """该行是否是**任意**围栏标记行（开或闭都算）——用于识别尾部残留的闭合行。"""
    return fence_run(line) is not None


def mask_fenced_code(text: str) -> str:
    """把每个围栏区间（含围栏行本身）替换为等长空格；换行原样保留 ⇒ 偏移逐字符保持。"""
    return fence_scan(text)[0]


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


def _blank(chars: list[str], start: int, end: int) -> None:
    """把 ``[start, end)`` 的非换行字符换成空格（换行必须留着，否则行结构变了）。"""
    for position in range(start, end):
        if chars[position] != "\n":
            chars[position] = " "


def unwrap_whole_response_fence(text: str) -> str:
    """整条响应就是一块围栏时剥掉围栏，返回围栏内的内容；否则返回空串。

    这是**有意保留**的既有支持：部分兼容端点会把协议整块包进 ```` ```json ```` 或
    ```` ```xml ````。它与「正文中间夹一个代码块」是两件事——前者整条回答都是协议，
    后者是讲解用的示例，绝不能执行。
    """
    source = str(text or "").strip()
    if not source:
        return ""
    lines = source.split("\n")
    if len(lines) < 2:
        return ""
    opened = fence_open(lines[0])
    if not opened:
        return ""
    char, length = opened
    last_index = len(lines) - 1
    while last_index > 0 and not lines[last_index].strip():
        last_index -= 1
    if last_index <= 0 or not fence_close(lines[last_index], char, length):
        return ""
    return "\n".join(lines[1:last_index]).strip()


def only_fence_tail(text: str, index: int) -> bool:
    """``index`` 之后是否只剩空白与围栏标记行（协议可以顶到响应尾）。

    「协议必须顶到回答末尾」是终态层的判据：工具动作后面还接正文的输出**不再执行该动作**，
    整体按正文展示——与旧行为（静默执行动作、静默丢掉正文）相反，宁可不动作也不吞内容。
    """
    source = str(text or "")
    rest = source[max(0, int(index)):]
    if not rest.strip():
        return True
    return all(not line.strip() or is_fence_line(line) for line in rest.split("\n"))


def in_final_block(text: str, index: int) -> bool:
    """``index`` 之后没有空行 ⇒ 标记落在回答的最后一段里。

    只用于**协议被切断**（解析不出来）时判定「这确实是被截断的动作」：截断总是停在
    最后一段的中途。中间位置的裸 JSON 不算协议，按正文放行。
    """
    source = str(text or "")
    return _BLANK_LINE.search(source[max(0, int(index)):]) is None
