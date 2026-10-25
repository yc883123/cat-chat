"""拆书匠的纯算法层：把一整本 TXT 小说按章节切开。

设计边界（docs/plans/2026-10-25-小说拆章模块移植计划.md §5.1 + 评审 §2）：
- **本模块只处理字符串，不做任何 IO**：读文件、编码嗅探、缓存都在服务/工具层，
  这样算法可以被单测直接喂字符串验证，也不需要关心权限策略。
- 不 import 任何 Cat Chat 业务模块（只用标准库 `re`）。
- 纯函数、无全局可变状态（缓存由调用方持有）。

四级降级（照竞品实战口径，命中即停）：
1. 按标题切分：识别到 ≥2 个章节标题 → 主流路径
2. 符号分节：某类符号分隔行出现 ≥5 次 → 取其**后一行**当标题
3. 单标题兜底：只识别到 1 个标题 → 仍按它切，好过乱切
4. 等分兜底：一个标题都没有 → 按字数硬切，尽量在换行处收尾

三个容易忽略的处理：
- 剥头部：网文开头是版权声明/简介，用正文标记分隔线定位，**只在全文前 20% 内找**
- 卷不单独成章：卷标题并进所属章（`第一卷 X · 第一章 Y`），否则会冒出一堆空章
- 超长章再切：单章 > max_len 再切小块，标题加 ` (1/2)` 后缀（为喂模型服务）
"""

from __future__ import annotations

import re

# 单章上限（字符）：超过就再切小块，为的是能塞进模型上下文。
DEFAULT_MAX_LEN = 6000
# 单文件上限（字节）：超了直接拒（与竞品一致，60 MiB）。
MAX_FILE_BYTES = 60 * 1024 * 1024
# 标题最多保留多少字。
MAX_TITLE_CHARS = 60
# 候选标题行最长多少字（超长行当正文，不当标题）。
MAX_HEADING_LINE_CHARS = 40
# 卷标题并进章标题时的分隔符。
VOL_JOIN = " · "
# 等分兜底：在目标点前后这个比例窗口内找最近的换行。
_LEN_SPLIT_WINDOW = 0.6

# 数字字符集：阿拉伯数字 + 全角数字 + 汉字数字（含「两」，用于「第两百章」这类写法）。
_NUM = r"[0-9０-９零一二三四五六七八九十百千万两]"
# 标题后的副标题允许长度（超出说明这是正文句子，不是标题）。
_SUBTITLE = r"[^\n]{0,60}"

# 章标题：带前括号变体 / 第 N 章 / 第 N 卷（篇部集季）/ 卷 N / 序章类词 / 英文 Chapter / "12. 标题"。
CHAPTER_RE = re.compile(
    r"^[\s　]*[（(【\[]?\s*(?:"
    rf"第\s*{_NUM}{{1,12}}\s*[章回节節]"
    rf"|第\s*{_NUM}{{1,12}}\s*[卷篇部集季]"
    rf"|卷\s*{_NUM}{{1,12}}"
    r"|(?:楔子|序章|序言|序|引子|尾声|尾聲|后记|後記|终章|終章|番外|前言|附录|附錄)"
    r"|[Cc][Hh][Aa][Pp][Tt][Ee][Rr]\s*[0-9IVXLCivxlc]{1,8}"
    rf"|{_NUM}{{1,12}}\s*[.、,，]\s*\S"
    r")\s*" + _SUBTITLE + r"$",
    re.MULTILINE,
)
# 卷/篇/部/集/季：不单独成章，拼进所属章标题。
VOL_RE = re.compile(rf"^[\s　]*第\s*{_NUM}{{1,12}}\s*[卷篇部集季]|^[\s　]*卷\s*{_NUM}{{1,12}}", re.MULTILINE)
# 符号分隔行（◎ *** === 等）：用于「符号分节」与「正文标记」判定。
SYMBOL_RE = re.compile(r"^[\W_]{1,10}$", re.UNICODE)
# 「正文」标记行：====正文==== / ---- 开始 ---- 这类。
BODY_MARK_RE = re.compile(
    r"^[=＝\-—*＊~～]{2,}\s*(正文|全文|内容|开始|序章)?\s*[=＝\-—*＊~～]{2,}$",
    re.MULTILINE,
)
# 剥头部时只在前 20% 找正文标记：正文中间也可能有分隔线，越往后越可能是正文。
_BODY_MARK_SCAN_RATIO = 0.2
# 符号分节：同一符号行至少出现这么多次才认为它在分节。
_MIN_SYMBOL_SPLIT = 5


class NovelSplitError(ValueError):
    """拆章输入非法（不是崩溃，是用户可读的拒绝理由）。"""


def _norm_title(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _looks_like_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > MAX_HEADING_LINE_CHARS:
        return False
    return bool(CHAPTER_RE.match(stripped))


# 主章节标记：取「第N章 / 第N回 / 第N节」里出现最多的那个字。
# 一本书通常只用一种；混进来的其它标记多半是正文（见 `_pick_heading_marker`）。
_MAIN_MARKER_RE = re.compile(r"^[\s　]*[（(【\[]?\s*第\s*[0-9０-９零一二三四五六七八九十百千万两]{1,12}\s*([章回节節])")
_NUMERIC_LIST_RE = re.compile(r"^[\s　]*[0-9０-９]{1,12}\s*[.、,，]\s*\S")
# 主导标记占比达到这个数，才算「这本书用的是它」。
_MAIN_MARKER_RATIO = 0.8
# 主标记至少出现这么多次，样本才够判断（太短的书不做这种降级）。
_MAIN_MARKER_MIN_HITS = 8


def _pick_heading_marker(text: str) -> str:
    """判断这本书的**主章节标记**（章/回/节）。

    真书里常见两类误判，都会把正文当章节切碎：

    - 副本规则那种编号列表（``1.需要穿着正装…``）被「12. 标题」这条规则命中；
    - 正文句子 ``第9节 是秦西西的英语课。`` 被「第N节」这条规则命中。

    一本书只会用其中一种写法当章节标记。所以统计「第N章/回/节」里出现最多的那个，
    占比 ≥80% 且样本够多时，用它当主标记：**其它写法一律当正文**，不再当章节标题。
    判定不了（样本太少 / 几种写法势均力敌）就返回空串，退回原有宽松行为。
    """
    counts: dict[str, int] = {}
    for line in str(text or "").splitlines():
        match = _MAIN_MARKER_RE.match(line.strip())
        if match:
            counts[match.group(1)] = counts.get(match.group(1), 0) + 1
    if not counts:
        return ""
    marker, hits = max(counts.items(), key=lambda item: item[1])
    total = sum(counts.values())
    if hits < _MAIN_MARKER_MIN_HITS or hits / total < _MAIN_MARKER_RATIO:
        return ""
    return marker


def _looks_like_heading_with_marker(line: str, marker: str) -> bool:
    """带主标记约束的标题判定（`marker` 为空时等于 `_looks_like_heading`）。

    有主标记时：`第N<主标记>` 照常认；其它章节标记与「N. 标题」式编号一律不认
    ——它们在这本书里是正文。
    """
    if not _looks_like_heading(line):
        return False
    if not marker:
        return True
    stripped = line.strip()
    match = _MAIN_MARKER_RE.match(stripped)
    if match:
        return match.group(1) == marker
    # 「N. 标题」式：主标记是「第N章」这类写法时，编号列表属于正文。
    return not _NUMERIC_LIST_RE.match(stripped)


def _clip_title(text: str) -> str:
    title = _norm_title(text)
    return title[:MAX_TITLE_CHARS] if len(title) > MAX_TITLE_CHARS else title


def strip_head(text: str) -> str:
    """剥掉正文之前的版权声明/简介。

    用 ``BODY_MARK_RE`` 找正文分隔标记，**只在全文前 20% 范围内找**——正文中间
    也可能出现 ``====`` 这类分隔线，越往后越可能是正文内容，误判会把开头整段吃掉。
    找不到标记就原样返回（宁可多留，不要误删）。
    """
    if not text:
        return text
    window = max(1, int(len(text) * _BODY_MARK_SCAN_RATIO))
    head = text[:window]
    match = BODY_MARK_RE.search(head)
    if not match:
        return text
    # 从标记行**之后**开始算正文。
    line_end = text.find("\n", match.end())
    if line_end < 0:
        return text
    return text[line_end + 1:]


def _split_by_heads(text: str, marker: str = "") -> list[tuple[str, str]]:
    """按章节标题切分；卷标题并进所属章标题，不产生空章。"""
    lines = text.splitlines(keepends=True)
    chapters: list[tuple[str, str]] = []
    pending_vol = ""
    title = ""
    buffer: list[str] = []
    # 标题行前 ≤30 字的铺垫行归给上一章（网文常见的「本章完」「求票」这类短行）。
    _PRE_TITLE_SLACK = 30

    def flush() -> None:
        nonlocal title, buffer, pending_vol
        body = "".join(buffer).strip()
        if not body and not title:
            buffer = []
            return
        head = VOL_JOIN.join(part for part in (pending_vol, title) if part)
        if not head:
            head = "正文"
        chapters.append((_clip_title(head), body))
        buffer = []

    for line in lines:
        stripped = line.strip()
        if stripped and len(stripped) <= MAX_HEADING_LINE_CHARS and VOL_RE.match(stripped):
            # 卷标题：先收掉当前章，再把它挂到下一个章标题上。
            flush()
            pending_vol = _norm_title(stripped)
            title = ""
            continue
        if _looks_like_heading_with_marker(line, marker):
            flush()
            title = _norm_title(stripped)
            continue
        buffer.append(line)
    flush()
    # 开头没有标题的那段：并进第一章（而不是单独成章），空的直接丢掉。
    # 剥头之后残留的空「正文」段就是这里消掉的。
    if len(chapters) > 1 and chapters[0][0] == "正文":
        lead_body = chapters[0][1].strip()
        chapters.pop(0)
        if lead_body:
            first_title, first_body = chapters[0]
            chapters[0] = (first_title, (lead_body + "\n" + first_body).strip())
    del _PRE_TITLE_SLACK  # 说明性常量，实际归并靠上面的 flush 顺序
    # 只留**有正文**的章节：只有标题没有正文的（例如全书末尾的「（番外完）」、
    # 「全文完」这类收尾标记行）不是章节。留着会得到一个 0 字章节，导出一片空白，
    # 还会占掉「一次带 N 章」的额度。
    return [(t, b) for t, b in chapters if b.strip()]


# 符号分节的首字符集：古籍/论坛贴常用这些起行分节。
_SEP_LEAD_CHARS = "◎◇◆★☆※§¶❖●○■□▲△▼▽→"


def _sep_lead(line: str) -> str:
    """这一行是不是「符号分节行」：返回其前导符号（不是则空串）。

    两种形态都算：整行只有符号（``***`` / ``===``），或符号后接标题
    （``◎ 第一节 起始``）——后者是古籍/论坛贴最常见的写法。
    """
    stripped = line.strip()
    if not stripped:
        return ""
    if SYMBOL_RE.match(stripped):
        return stripped[0]
    if stripped[0] in _SEP_LEAD_CHARS:
        return stripped[0]
    return ""


def _split_by_sep(text: str) -> list[tuple[str, str]]:
    """符号分节：某类符号分隔行出现 ≥5 次时，取其**后一行**当标题。"""
    lines = text.splitlines()
    counts: dict[str, int] = {}
    for line in lines:
        lead = _sep_lead(line)
        if lead:
            counts[lead] = counts.get(lead, 0) + 1
    if not counts:
        return []
    symbol, hits = max(counts.items(), key=lambda item: item[1])
    if hits < _MIN_SYMBOL_SPLIT:
        return []
    chapters: list[tuple[str, str]] = []
    title = ""
    buffer: list[str] = []
    pending_title = False
    index = 0
    for line in lines:
        stripped = line.strip()
        if _sep_lead(line) == symbol:
            if title or "".join(buffer).strip():
                chapters.append((_clip_title(title or f"第 {index + 1} 节"), "\n".join(buffer).strip()))
                index += 1
            buffer = []
            title = ""
            # 「◎ 第一节 起始」这种同行带标题的写法：符号后面的内容就是标题。
            # 连续符号要整段剥掉（`***` 剥完是空，不能把 `**` 当标题）。
            inline = _norm_title(re.sub(rf"^[{re.escape(_SEP_LEAD_CHARS)}\W_]+", "", stripped).strip())
            if inline and len(inline) <= 24:
                title = inline
                pending_title = False
            else:
                pending_title = True
            continue
        if pending_title:
            candidate = _norm_title(stripped)
            # 分隔线后一行当标题；太长/为空就退回「第 N 节」。
            if candidate and len(candidate) <= 24:
                title = candidate
            pending_title = False
            continue
        buffer.append(line)
    if title or "".join(buffer).strip():
        chapters.append((_clip_title(title or f"第 {index + 1} 节"), "\n".join(buffer).strip()))
    return [(t, b) for t, b in chapters if b.strip() or t]


def _split_by_len(text: str, max_len: int) -> list[tuple[str, str]]:
    """等分兜底：按字数硬切，尽量在换行处收尾。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [("全文", text)]
    chunks: list[tuple[str, str]] = []
    start = 0
    total = len(text)
    while start < total:
        end = min(total, start + max_len)
        if end < total:
            # 在目标点前 max_len*window 范围内找最近的换行，避免把句子切成两半。
            window_start = max(start + 1, int(end - max_len * (1 - _LEN_SPLIT_WINDOW)))
            cut = text.rfind("\n", window_start, end)
            if cut > start:
                end = cut + 1
        chunks.append((f"第 {len(chunks) + 1} 段", text[start:end].strip()))
        start = end
    return [(t, b) for t, b in chunks if b]


def _cap_chunks(chapters: list[tuple[str, str]], max_len: int) -> list[tuple[str, str]]:
    """超长章再切小块，标题加 `` (1/2)`` 后缀（为喂模型服务，不是给人看的）。"""
    if max_len <= 0:
        return chapters
    out: list[tuple[str, str]] = []
    for title, body in chapters:
        if len(body) <= max_len:
            out.append((title, body))
            continue
        pieces = _split_by_len(body, max_len)
        total = len(pieces)
        for index, (_ignored, piece) in enumerate(pieces, start=1):
            out.append((f"{title} ({index}/{total})", piece))
    return out


def split_chapters(text: str, max_len: int = DEFAULT_MAX_LEN) -> list[dict[str, object]]:
    """把整本书切成章节。返回 ``[{title, body}]``（正文为原文，不做任何改写）。

    四级降级，命中即停。空输入返回空列表（调用方按「没有可拆内容」处理）。
    """
    raw = str(text or "")
    if not raw.strip():
        return []
    body = strip_head(raw)
    if not body.strip():
        body = raw

    # 主章节标记（章/回/节）：这本书用哪个当章节标记，其它写法就当正文。
    # 判定不了时为空串，下面的判定完全退回原来的宽松行为。
    marker = _pick_heading_marker(body)
    heads = [line for line in body.splitlines() if _looks_like_heading_with_marker(line, marker)]
    if len(heads) >= 2:
        chapters = _split_by_heads(body, marker)
    else:
        chapters = _split_by_sep(body)
        if not chapters:
            if len(heads) == 1:
                chapters = _split_by_heads(body, marker)
            else:
                chapters = _split_by_len(body, max_len)
    if not chapters:
        chapters = _split_by_len(body, max_len)
    chapters = _cap_chunks(chapters, max_len)
    return [{"title": title, "body": text_body} for title, text_body in chapters]


def outline(text: str, max_len: int = DEFAULT_MAX_LEN) -> list[dict[str, object]]:
    """只返回索引（``{i, title, len}``）——**不含正文**。

    两段式 API 的关键：长会话里回传整本书正文会把上下文撑爆，
    索引长度只随章节数线性增长。
    """
    rows = split_chapters(text, max_len)
    return [
        {"i": index, "title": str(row["title"]), "len": len(str(row["body"]))}
        for index, row in enumerate(rows)
    ]


def detect_level(text: str, max_len: int = DEFAULT_MAX_LEN) -> str:
    """复算这次拆分实际走的降级级别：``heading`` / ``sep`` / ``single`` / ``len``。

    ``split_chapters`` 返回的章节不带级别标记，而脚本与工作台都必须**明示**降级
    ——按符号分节或按字数等分时，用户要知道这不是常规章节识别。判定用的是与
    ``split_chapters`` 同一批谓词、同一顺序，放在本模块供两边共用：各写一套就会漂，
    而「脚本拆的结果 = 工作台拆的结果」是硬要求。
    """
    body = strip_head(str(text or ""))
    if not body.strip():
        body = str(text or "")
    marker = _pick_heading_marker(body)
    heads = [line for line in body.splitlines() if _looks_like_heading_with_marker(line, marker)]
    if len(heads) >= 2:
        return "heading"
    if _split_by_sep(body):
        return "sep"
    if len(heads) == 1:
        return "single"
    return "len"
