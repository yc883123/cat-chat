# -*- coding: utf-8 -*-
"""小说域提示词与长文预算。

阈值**全部集中在本文件**，不散落在服务层与前端（《阶段二》§五）。取值照启元实证：

| 做法 | 取证 | 本侧取值 |
|---|---|---|
| 章节级调用，全书永不进 prompt | `nv_main.js:1548-1568` | 一次只带勾选章节 |
| 参考素材硬上限 | `:1551` `slice(0,3000)` | `BODY_SLOT_CHARS`（默认 3000，用户可调） |
| 分槽限额 | `:2412-2417`（≤4000 token） | 要素 800 / 大纲 1200 |
| 滚动摘要链 | `:2569-2589`（近 3 章首 120 + 尾 220） | `SUMMARY_PREV_CHAPTERS = 3` |
| 截断绝不静默 | `:3458/:3476/:3510` | 超限说明超了多少、建议减到几章 |

两档可调参数由用户在阶段面板上用滑条设定（计划 §八）：

- `BODY_SLOT_CHARS`：每章正文上限，3000–15000，步长 500。
- `MAX_CHAPTERS_PER_CALL`：一次最多带几章，1–20，步长 1。

它们**只影响引用范围**，不会把字数当成模型的输出长度。提交前用同一套组装函数
做预算预检（`build_stage_content` + `estimate_input_tokens`），预检与正式提交
共用规则，避免「预览说 6000 实际按 3000 截断」。

摘要链是**零额外推理**的：不额外调模型，直接取每章正文的首尾片段。每章产出的
摘要按「章节 + 稿件」记入 `summaries["<章号>@<稿件ID>"]`，后续调用带前 3 章。
"""

from __future__ import annotations

import re
from typing import Any

# ---- 槽位上限（字符） ----
# 正文槽：参考素材的**默认**上限（用户可在 3000–15000 之间调整）。
BODY_SLOT_CHARS = 3000
BODY_SLOT_MIN = 3000
BODY_SLOT_MAX = 15000
BODY_SLOT_STEP = 500
# 要素槽 / 大纲槽：分槽限额，防止要素把大纲的位置挤掉。
ELEMENTS_SLOT_CHARS = 800
OUTLINE_SLOT_CHARS = 1200
# 摘要链：带前几章、每章摘要取首尾多少字。
SUMMARY_PREV_CHAPTERS = 3
SUMMARY_HEAD_CHARS = 120
SUMMARY_TAIL_CHARS = 220
# 用户补充要求的上限（不让人把整本书塞进「补充说明」）。
EXTRA_NOTE_CHARS = 800
# 一次最多带几章正文：默认 6，用户可在 1–20 之间调整；超了明示让用户减章。
MAX_CHAPTERS_PER_CALL = 6
MAX_CHAPTERS_MIN = 1
MAX_CHAPTERS_MAX = 20
# 输出预留（token）：给模型回复留的空间，预算预检按它扣减可用上下文。
OUTPUT_RESERVE_TOKENS = 4096

# ---- 阶段四 ----
# 4A 分块续写：单章改编的正文超上限时按块顺序生成，一个任务内多轮调用。
# 块数上限是硬闸门：超出只取前 N 块，剩余字数必须明说，绝不静默。
ADAPT_CHUNK_LIMIT = 20
# 上一轮已生成的改编稿带进下一轮的结尾字数（保持前后块衔接）。
CONTINUITY_TAIL_CHARS = 500
# 4B-1 图谱反哺：已保存要素的实体进系统提示词（不是用户消息，不污染正文语气）。
KG_ENTITY_LIMIT = 20
KG_LINE_CHARS = 80
KG_SLOT_CHARS = 1200
KG_HEADER = "【全书设定参考（来自已保存要素，仅供保持人物与设定一致，不是本次素材）】"
# 4C 分镜生图：单场剧本正文进提示词的裁剪上限。
STORYBOARD_SCENE_CHARS = 4000
# 4C 分镜生图：用户自定义的系统提示词（模板）上限——模板可能很长，但也不能无界。
STORYBOARD_SYSTEM_CHARS = 8000

# 阶段名与中文标题（前端阶段导航、任务标题共用）。
# 注意 `STAGES` 只收「按章节素材生成」的阶段：build_stage_content 的分槽/摘要链
# 只对它们有意义；单章改编与分镜生图走各自的组装函数。
STAGE_LABELS = {
    "elements": "要素",
    "outline": "改编大纲",
    "script": "剧本",
}
STAGES = tuple(STAGE_LABELS)
# 单章 AI 改编是小说域新增的阶段，只作用于一份稿件（不参与多章素材选择）。
ADAPT_STAGE = "adapt"
STORYBOARD_STAGE = "storyboard"
ALL_STAGES = STAGES + (ADAPT_STAGE, STORYBOARD_STAGE)
STORYBOARD_LABEL = "分镜生图"


class PromptError(ValueError):
    """提示词组装失败（用户可读）。"""


def _clip(text: str, limit: int) -> tuple[str, int]:
    """按字符截断，返回 ``(片段, 被截掉的字符数)``。"""
    raw = str(text or "")
    if limit <= 0 or len(raw) <= limit:
        return raw, 0
    return raw[:limit], len(raw) - limit


def clamp_body_limit(value: Any) -> int:
    """每章正文上限：非整数/越界一律按边界收（滑条值来自用户，必须收敛）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return BODY_SLOT_CHARS
    return max(BODY_SLOT_MIN, min(BODY_SLOT_MAX, number))


def clamp_chapter_limit(value: Any) -> int:
    """一次最多带几章：同上收敛到 1–20。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return MAX_CHAPTERS_PER_CALL
    return max(MAX_CHAPTERS_MIN, min(MAX_CHAPTERS_MAX, number))


def summarize_chapter(content: str) -> str:
    """一章的摘要（零推理）：取首尾片段，中间用省略号。

    长章开头交代场景、结尾推进情节，中间大段描写对「跨章一致性」贡献最小，
    所以取首 120 + 尾 220。短章整段保留。
    """
    text = str(content or "").strip()
    if not text:
        return ""
    limit = SUMMARY_HEAD_CHARS + SUMMARY_TAIL_CHARS
    if len(text) <= limit:
        return text
    return f"{text[:SUMMARY_HEAD_CHARS]}…{text[-SUMMARY_TAIL_CHARS:]}"


def summary_key(chapter_no: int, manuscript_id: str = "") -> str:
    """摘要的键：按「章节 + 稿件」定位，换稿不会串用另一份稿件的前文。"""
    number = int(chapter_no or 0)
    mid = str(manuscript_id or "").strip()
    return f"{number}@{mid}" if mid else str(number)


def build_summary_chain(summaries: dict[str, Any], chapter_ids: list[int],
                        manuscript_ids: dict[int, str] | None = None) -> str:
    """把「前 N 章摘要」拼成提示词片段。

    `summaries` 是作品里的 `{"<章号>@<稿件ID>": 摘要}`；只带**当前批次之前**的最近
    N 章，批次内的章节不算「前文」（它们的正文已经在正文槽里）。

    查找顺序：本批选定稿件对应的摘要 → 不区分稿件的旧键（老数据）→ 跳过。
    这样用改编稿生成时不会误用旧原文的摘要（计划 §3.4）。
    """
    if not summaries or not chapter_ids:
        return ""
    wanted = manuscript_ids if isinstance(manuscript_ids, dict) else {}
    first = min(int(n) for n in chapter_ids)
    prior: list[int] = []
    for raw in summaries:
        number_part = str(raw).split("@", 1)[0]
        try:
            number = int(number_part)
        except (TypeError, ValueError):
            continue
        if number < first and number not in prior:
            prior.append(number)
    if not prior:
        return ""
    prior.sort()
    picked = prior[-SUMMARY_PREV_CHAPTERS:]
    lines = []
    for number in picked:
        text = ""
        mid = str(wanted.get(number) or wanted.get(str(number)) or "")
        if mid:
            text = str(summaries.get(summary_key(number, mid)) or "").strip()
        if not text:
            text = str(summaries.get(str(number)) or "").strip()
        if text:
            lines.append(f"第 {number} 章摘要：{text}")
    return "\n".join(lines)


def chapter_block(row: dict[str, Any], body_limit: int) -> tuple[str, dict[str, Any], str]:
    """一章的正文槽：返回 ``(片段, 统计, 截断说明)``。

    截断绝不静默：说明里带「超了多少字」，供右栏和任务告警原样展示。
    """
    number = int(row.get("i") or 0)
    title = str(row.get("title") or f"第 {number} 章")
    content = str(row.get("content") or "")
    body, dropped = _clip(content, int(body_limit))
    note = ""
    if dropped:
        note = (f"第 {number} 章《{title}》正文超出单章上限 {int(body_limit)} 字，"
                f"已省略后 {dropped} 字")
    stats = {
        "i": number,
        "title": title,
        "manuscript_id": str(row.get("manuscript_id") or ""),
        "manuscript_label": str(row.get("manuscript_label") or ""),
        "manuscript_kind": str(row.get("manuscript_kind") or ""),
        "total_chars": len(content),
        "used_chars": len(body),
        "dropped_chars": dropped,
    }
    return f"【第 {number} 章】{title}\n{body}", stats, note


def _chapter_blocks(chapters: list[dict[str, Any]], body_limit: int) -> tuple[str, list[str], list[dict[str, Any]]]:
    """拼正文槽：每章按 `body_limit` 截断，返回 ``(正文片段, 截断说明, 逐章统计)``。"""
    blocks: list[str] = []
    notes: list[str] = []
    stats: list[dict[str, Any]] = []
    for row in chapters:
        block, stat, note = chapter_block(row, body_limit)
        if note:
            notes.append(note)
        stats.append(stat)
        blocks.append(block)
    return "\n\n".join(blocks), notes, stats


def check_chapter_budget(chapter_ids: list[int], max_chapters: int = MAX_CHAPTERS_PER_CALL) -> None:
    """一次带几章的硬闸门：超了直接拒绝并给出可操作建议，不静默减章。"""
    count = len(chapter_ids)
    limit = clamp_chapter_limit(max_chapters)
    if count == 0:
        raise PromptError("至少选择一章作为素材")
    if count > limit:
        raise PromptError(
            f"一次最多带 {limit} 章（这次选了 {count} 章，多了 {count - limit} 章）；"
            f"请分批生成，或调高「一次最多选择章节」"
        )


_ELEMENTS_INSTRUCTION = """你是影视改编的策划。请只依据下面给出的原作章节，整理改编要用的要素。

要求：
1. 分成三类：人物、场景、道具。每类用「· 」开头的列表项。
2. 人物写：姓名/称呼、身份、性格关键词、在本批素材里的关键行为。不要编造素材里没有的设定。
3. 场景写：地点名、出现的章、这个地点承担的戏剧功能。
4. 道具写：名称、出现的章、它推动情节的作用。
5. 素材里没提到的类别写「（本批素材未出现）」，不要用常识补。
6. 只输出要素清单，不要写改编建议、不要复述原文。"""

_OUTLINE_INSTRUCTION = """你是影视改编的编剧。请只依据下面给出的原作章节，整理一份三幕改编大纲。

要求：
1. 严格分成三幕：第一幕 建置、第二幕 对抗、第三幕 结局。每幕用「## 第一幕 建置」这样的标题。
2. 每一幕下列 3–7 条要点，每条写清「谁 在什么处境 做了什么 造成什么变化」。
3. 只覆盖本批素材实际写到的内容；素材不足以支撑某一幕时，在该幕下写「（本批素材未覆盖，需补后续章节）」。
4. 不要编造素材里没有的人物、地点、事件。
5. 不要写分镜、不要写台词。"""

_SCRIPT_INSTRUCTION = """你是专业编剧。把下面给出的原作章节改编为标准电影剧本（screenplay）。

格式要求（必须严格遵守，否则无法拆成场次）：
1. 每一场用一行「场景行」开头，格式为：`INT. 地点 - 时间` 或 `EXT. 地点 - 时间`。
   时间只写：日 / 夜 / 黄昏 / 清晨。
2. 场景行下面写动作描述：只写镜头里看得见、听得见的东西，不写心理活动。
3. 人物名单独一行（全大写或用中文原名，全书统一），下一行写台词。
4. 场与场之间空一行。不要写「第一场」这类编号，也不要写分镜、镜头号、时长。
5. 人物称谓必须与给定的人物设定一致，不得改名、不得新增设定里没有的主要角色。
6. 只改编给定素材里实际发生的事；素材没交代的因果不要自己补。

输出只有剧本正文，不要写说明、不要复述原文。"""

_ADAPT_INSTRUCTION = """你是影视改编的编剧。请根据下面这份单章稿件，以及用户提出的改编要求，产出一份新的改编稿。

要求：
1. 只依据给出的这份稿件和用户要求改写，不要引入素材里没有的人物、设定或事件。
2. 保留原文的情节走向与人物关系；用户明确要求改动的地方按用户要求改。
3. 输出完整的章节正文（不是要点、不是说明、不是评论），保留分段。
4. 不要写「以下是改编稿」这类前言，不要复述用户的要求。

用户要求：
{user_request}"""

_ADAPT_CHUNK_NOTE = """这一份稿件太长，已经按段落切成 {total} 块，按顺序逐块改写。
本次只改写给出的这一块原稿：输出这一块对应的改编正文，不要提前写后面块的内容，
不要复述前一块已生成的部分；块与块之间要像同一篇稿子一样衔接。"""

_INSTRUCTIONS = {
    "elements": _ELEMENTS_INSTRUCTION,
    "outline": _OUTLINE_INSTRUCTION,
    "script": _SCRIPT_INSTRUCTION,
}

# 场景行：`INT. 老宅书房 - 夜` / `EXT. 长桥 - 黄昏`（中英文标点都认）。
SCENE_LINE_RE = re.compile(
    r"^[\s　]*((?:INT|EXT|I/E)[.．]?\s*[^\n]{1,120}?)[\s　]*[-–—]\s*([^\n]{1,20})[\s　]*$",
    re.IGNORECASE,
)


def parse_scenes(text: str, *, from_chapter: int = 0) -> tuple[list[dict[str, Any]], bool]:
    """把剧本正文拆成场次列表。返回 ``(scenes, parsed)``。

    `parsed=False` 表示**没有识别到场次结构**：调用方必须整段原样保存并如实告诉
    用户「未能识别场次结构，已按整段保存」，绝不臆造切分（《阶段三》§五 P1）。
    """
    raw = str(text or "")
    if not raw.strip():
        return [], False
    lines = raw.splitlines()
    scenes: list[dict[str, Any]] = []
    current_head = ""
    buffer: list[str] = []

    def flush() -> None:
        nonlocal current_head, buffer
        body = "\n".join(buffer).strip()
        if current_head or body:
            scenes.append({
                "scene": current_head,
                "content": body,
                "from_chapter": int(from_chapter or 0),
            })
        buffer = []

    for line in lines:
        match = SCENE_LINE_RE.match(line)
        if match:
            flush()
            current_head = f"{match.group(1).strip()} - {match.group(2).strip()}"
            continue
        buffer.append(line)
    flush()
    # 至少要有一场带场景行的，才算「识别到场次结构」。
    if not any(row["scene"] for row in scenes):
        return [], False
    return scenes, True


def build_stage_content(
    *,
    stage: str,
    book_title: str,
    chapters: list[dict[str, Any]],
    chapter_ids: list[int],
    summaries: dict[str, Any] | None = None,
    extra_note: str = "",
    outline: str = "",
    characters: list[str] | None = None,
    body_limit: int = BODY_SLOT_CHARS,
    max_chapters: int = MAX_CHAPTERS_PER_CALL,
    manuscript_ids: dict[int, str] | None = None,
) -> dict[str, Any]:
    """组装一次阶段生成的 content。

    返回 ``{content, warnings, chapters}``：`warnings` 是要如实展示给用户的截断/
    超限说明，前端必须显示，不能吞掉；`chapters` 是逐章实际引用统计（右栏与任务
    快照都用它，避免界面自己再算一遍）。

    剧本阶段（`stage="script"`）**必带大纲槽与人物槽**：写正文时不带设定与人物，
    同一个角色会换个名字（《阶段三》§三、§五 P1）。要素/大纲阶段不带这两槽——
    它们本来就是产出这两样的，再塞回去只会互相污染。
    """
    if stage not in _INSTRUCTIONS:
        raise PromptError(f"未知阶段：{stage}")
    limit = clamp_body_limit(body_limit)
    check_chapter_budget(chapter_ids, max_chapters)

    body, notes, stats = _chapter_blocks(chapters, limit)
    chain = build_summary_chain(summaries or {}, chapter_ids, manuscript_ids)

    parts: list[str] = [f"作品：《{book_title}》", _INSTRUCTIONS[stage]]
    if stage == "script":
        outline_slot, outline_dropped = _clip(str(outline or "").strip(), OUTLINE_SLOT_CHARS)
        if outline_slot:
            parts.append(f"【改编大纲（必须遵守，人物与场次顺序以此为准）】\n{outline_slot}")
        if outline_dropped:
            notes.append(f"改编大纲超出 {OUTLINE_SLOT_CHARS} 字上限，已省略后 {outline_dropped} 字")
        roles = [str(row).strip() for row in (characters or []) if str(row).strip()]
        if roles:
            joined = "\n".join(f"· {row}" for row in roles)
            roles_slot, roles_dropped = _clip(joined, ELEMENTS_SLOT_CHARS)
            parts.append(f"【人物设定（称谓必须与此一致，不得改名）】\n{roles_slot}")
            if roles_dropped:
                notes.append(f"人物设定超出 {ELEMENTS_SLOT_CHARS} 字上限，已省略后 {roles_dropped} 字")
    if chain:
        parts.append(f"【前文摘要（供维持跨章一致性，不是本批素材）】\n{chain}")
    parts.append(f"【本批原作素材】\n{body}")

    note, note_dropped = _clip(extra_note.strip(), EXTRA_NOTE_CHARS)
    if note:
        parts.append(f"【补充要求】\n{note}")
    if note_dropped:
        notes.append(f"补充要求超出 {EXTRA_NOTE_CHARS} 字上限，已省略后 {note_dropped} 字")

    return {"content": "\n\n".join(parts), "warnings": notes, "chapters": stats}


def build_adapt_content(*, book_title: str, chapter_no: int, chapter_title: str,
                        manuscript_label: str, content: str,
                        user_request: str, body_limit: int = BODY_SLOT_CHARS,
                        max_chapters: int = MAX_CHAPTERS_PER_CALL) -> dict[str, Any]:
    """组装一次单章 AI 改编的 content（固定单章来源，不转成整本改编）。"""
    limit = clamp_body_limit(body_limit)
    check_chapter_budget([int(chapter_no)], min(clamp_chapter_limit(max_chapters), 1) or 1)
    body, dropped = _clip(str(content or ""), limit)
    notes: list[str] = []
    if dropped:
        notes.append(
            f"第 {chapter_no} 章《{chapter_title}》正文超出单章上限 {limit} 字，已省略后 {dropped} 字"
        )
    instruction = _ADAPT_INSTRUCTION.replace(
        "{user_request}", user_request.strip() or "（用户没有写额外要求，请只做文字润色与通顺化处理）"
    )
    parts = [
        f"作品：《{book_title}》",
        instruction,
        f"【本次稿件】第 {chapter_no} 章 {chapter_title} · {manuscript_label}\n{body}",
    ]
    return {
        "content": "\n\n".join(parts),
        "warnings": notes,
        "chapters": [{
            "i": int(chapter_no), "title": str(chapter_title),
            "total_chars": len(str(content or "")), "used_chars": len(body),
            "dropped_chars": dropped,
        }],
    }


# ---- 4A 分块续写 ----

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；!?;\n])")
# 全角数字 → 半角，识别「【第 ３ 场】」这类写法。
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def _hard_split(text: str, limit: int) -> list[str]:
    """单段超限时按句子硬切（句子仍超长再按字符硬切），保证每块 ≤ limit。"""
    parts: list[str] = []
    buf = ""
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if not sentence:
            continue
        if len(buf) + len(sentence) <= limit:
            buf += sentence
            continue
        if buf:
            parts.append(buf)
            buf = ""
        while len(sentence) > limit:
            parts.append(sentence[:limit])
            sentence = sentence[limit:]
        buf = sentence
    if buf:
        parts.append(buf)
    return parts


def split_body_chunks(content: str, limit: int,
                      max_chunks: int = ADAPT_CHUNK_LIMIT) -> tuple[list[str], int]:
    """把超上限的单章正文按段落贪心打包成多块；返回 ``(块列表, 未纳入的字符数)``。

    未纳入 > 0 表示块数顶到了 `max_chunks`：调用方必须明说「最后 X 字未纳入」，
    绝不静默丢内容。limit 内的内容原样成一块，不做无谓切分。
    """
    limit = int(limit)
    text = str(content or "")
    if limit <= 0 or len(text) <= limit:
        return ([text] if text else []), 0
    chunks: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(_hard_split(line, limit))
            continue
        if buf and len(buf) + len(line) > limit:
            chunks.append(buf)
            buf = ""
        buf += line
    if buf:
        chunks.append(buf)
    chunks = [chunk for chunk in chunks if chunk.strip()]
    if len(chunks) <= max_chunks:
        return chunks, 0
    kept = chunks[:max_chunks]
    dropped = sum(len(chunk) for chunk in chunks[max_chunks:])
    return kept, dropped


def build_adapt_chunks(*, book_title: str, chapter_no: int, chapter_title: str,
                       manuscript_label: str, content: str,
                       user_request: str, body_limit: int = BODY_SLOT_CHARS) -> dict[str, Any]:
    """单章改编的分块组装：正文超上限时按块顺序生成，逐块拼接，绝不静默截断。

    返回 ``{chunked, content, warnings, chapters, overhead_chars}``。`chunked` 是
    runner 的多轮契约（见 StudioRunner）；`content` 是静态部分的拼合（任务快照与
    预算预检用）；`overhead_chars` 是各轮衔接上下文的额外字符，预算按它补足，
    避免估算比实际发送量偏小。
    """
    limit = clamp_body_limit(body_limit)
    check_chapter_budget([int(chapter_no)], 1)
    blocks, dropped = split_body_chunks(content, limit)
    total = len(blocks)
    instruction = _ADAPT_INSTRUCTION.replace(
        "{user_request}", user_request.strip() or "（用户没有写额外要求，请只做文字润色与通顺化处理）"
    )
    header_parts = [
        f"作品：《{book_title}》",
        instruction,
        _ADAPT_CHUNK_NOTE.replace("{total}", str(total)),
        f"【本次稿件】第 {chapter_no} 章 {chapter_title} · {manuscript_label}",
    ]
    prefix = "\n\n".join(header_parts)
    notes = [
        f"第 {chapter_no} 章《{chapter_title}》正文超出单章上限 {limit} 字，已按 {total} 块顺序生成"
    ] if total > 1 else [f"第 {chapter_no} 章《{chapter_title}》正文按单块顺序生成"]
    if dropped:
        notes.append(f"正文过长，最后 {dropped} 字未纳入本次生成（块数上限 {ADAPT_CHUNK_LIMIT} 块）")
    used_chars = len(str(content or "")) - dropped
    return {
        "chunked": {
            "blocks": blocks,
            "prefix": prefix,
            "continuity_chars": CONTINUITY_TAIL_CHARS,
            "continuity_label": "【已生成改编稿的结尾（新内容必须紧接它继续，不要重复）】",
        },
        "content": prefix + "\n\n" + "\n\n".join(blocks),
        "warnings": notes,
        "chapters": [{
            "i": int(chapter_no), "title": str(chapter_title),
            "total_chars": len(str(content or "")),
            "used_chars": used_chars,
            "dropped_chars": dropped,
        }],
        "overhead_chars": max(0, total - 1) * (CONTINUITY_TAIL_CHARS + 40),
    }


# ---- 4B-1 图谱反哺 ----

_KG_BUCKETS = (("characters", "人物"), ("scenes", "场景"), ("props", "道具"))


def kg_entities(elements: Any) -> list[str]:
    """从要素三类取最多 `KG_ENTITY_LIMIT` 条实体（人物优先，其次场景、道具）。

    每条压成一行并裁到 `KG_LINE_CHARS`——反哺是「保持一致」的参考，不是素材。
    """
    if not isinstance(elements, dict):
        return []
    rows: list[str] = []
    for key, label in _KG_BUCKETS:
        items = elements.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            text = re.sub(r"\s+", " ", str(item or "")).strip()
            if not text:
                continue
            if len(text) > KG_LINE_CHARS:
                text = text[:KG_LINE_CHARS] + "…"
            rows.append(f"· {label}：{text}")
            if len(rows) >= KG_ENTITY_LIMIT:
                return rows
    return rows


def build_kg_context(elements: Any) -> str:
    """图谱反哺的系统提示词；没有可用要素时返回空串（调用方不注入）。"""
    rows = kg_entities(elements)
    if not rows:
        return ""
    body = "\n".join(rows)
    if len(body) > KG_SLOT_CHARS:
        body = body[:KG_SLOT_CHARS] + "…"
    return f"{KG_HEADER}\n{body}"


# ---- 4C 分镜生图 ----

_STORYBOARD_INSTRUCTION = """你是分镜师。请把下面给出的每一场剧本，逐场转成一条「文生图提示词」。

要求：
1. 每一场输出一块，块的第一行写「【第 N 场】场景行」：N 用剧本里给出的场次号，场景行原样照抄。
2. 提示词正文只写画面本身：地点与景物、出场人物的外观与动作、景别与构图、光线与氛围，60–150 字。
3. 不要写对话内容，不要写台词字幕，不要虚构剧本里没有的人物长相。
4. 人物名字与剧本保持一致；场次顺序与剧本一致，一场都不跳过。
5. 只输出分块提示词，不要写说明、不要复述剧本。"""

# 「【第 3 场】INT. 老宅书房 - 夜」这类分块头。
STORYBOARD_HEAD_RE = re.compile(r"^[\s　]*【\s*第\s*([0-9]+)\s*场\s*】\s*(.*)$")

# 输出格式要求始终随正文发送：按场拆分依赖「【第 N 场】」块头，写法（模板）
# 换成什么都不能破坏这个格式。
_STORYBOARD_FORMAT_NOTE = (
    "输出格式（必须严格遵守，否则无法按场拆分）："
    "每一场输出一块，块的第一行写「【第 N 场】场景行」，N 用上面给出的场次号、"
    "场景行原样照抄；块内是这一场的提示词正文；场与场之间空一行。"
    "只输出分块提示词，不要写说明。"
)


def parse_storyboard(text: str) -> tuple[list[dict[str, Any]], bool]:
    """把分镜提示词拆成逐场条目，返回 ``(rows, parsed)``。

    `parsed=False` 表示没有识别到「【第 N 场】」结构：调用方整段原样显示并如实
    说明，绝不臆造切分（与 `parse_scenes` 同口径）。
    """
    raw = str(text or "")
    if not raw.strip():
        return [], False
    rows: list[dict[str, Any]] = []
    current_no = 0
    current_head = ""
    buffer: list[str] = []

    def flush() -> None:
        nonlocal current_no, current_head
        body = "\n".join(buffer).strip()
        if current_no or current_head or body:
            rows.append({
                "scene_no": int(current_no or 0),
                "scene_head": current_head,
                "prompt": body,
            })
        buffer.clear()

    for line in raw.splitlines():
        match = STORYBOARD_HEAD_RE.match(line.translate(_FULLWIDTH_DIGITS))
        if match:
            flush()
            current_no = int(match.group(1))
            current_head = match.group(2).strip()
            continue
        buffer.append(line)
    flush()
    if not any(row["scene_no"] for row in rows):
        return [], False
    return rows, True


def build_storyboard_content(*, book_title: str, scenes: list[dict[str, Any]],
                             scene_ids: list[int] | None = None,
                             extra_note: str = "",
                             system_prompt: str = "") -> dict[str, Any]:
    """组装分镜生成的 content：逐场给正文，模型逐场回出图提示词。

    `scene_ids` 缺省 = 全部场次（取已保存剧本的最新场次）；给了不存在的场次号
    直接报错，不静默跳过。

    写法（系统提示词）与正文分离：`system_prompt` 非空时作为**用户自定义写法**
    （通常来自提词库模板或手动编辑）进系统消息；为空时用内置分镜写法。两者都
    放系统消息，正文里只保留按场拆分所必需的输出格式要求——模板怎么换，
    「【第 N 场】」的解析契约都不受影响。
    """
    rows = [row for row in (scenes or []) if isinstance(row, dict)]
    if not rows:
        raise PromptError("这个作品还没有剧本场次；请先在剧本阶段生成并保存场次")
    known = {int(row.get("i") or 0) for row in rows}
    wanted = [int(n) for n in (scene_ids or [])] if scene_ids else sorted(known)
    if not wanted:
        raise PromptError("至少选择一场来生成分镜提示词")
    for no in wanted:
        if no not in known:
            raise PromptError(f"场次不存在：{no}")
    warnings: list[str] = []
    user_system = str(system_prompt or "").strip()
    if len(user_system) > STORYBOARD_SYSTEM_CHARS:
        warnings.append(
            f"自定义写法超出 {STORYBOARD_SYSTEM_CHARS} 字上限，已省略后 "
            f"{len(user_system) - STORYBOARD_SYSTEM_CHARS} 字"
        )
        user_system = user_system[:STORYBOARD_SYSTEM_CHARS]
    system = user_system if user_system else _STORYBOARD_INSTRUCTION
    parts = [f"作品：《{book_title}》", _STORYBOARD_FORMAT_NOTE]
    stats: list[dict[str, Any]] = []
    blocks: list[str] = []
    for no in wanted:
        row = next(item for item in rows if int(item.get("i") or 0) == no)
        head = str(row.get("scene") or "（没有场景行）")
        body, dropped = _clip(str(row.get("content") or ""), STORYBOARD_SCENE_CHARS)
        if dropped:
            warnings.append(f"第 {no} 场正文超出单场 {STORYBOARD_SCENE_CHARS} 字上限，已省略后 {dropped} 字")
        blocks.append(f"【第 {no} 场】{head}\n{body}")
        stats.append({
            "scene_no": no, "head": head,
            "total_chars": len(str(row.get("content") or "")),
            "used_chars": len(body), "dropped_chars": dropped,
        })
    parts.append("【剧本场次】\n" + "\n\n".join(blocks))
    note, note_dropped = _clip(str(extra_note or "").strip(), EXTRA_NOTE_CHARS)
    if note:
        parts.append(f"【补充要求】\n{note}")
    if note_dropped:
        warnings.append(f"补充要求超出 {EXTRA_NOTE_CHARS} 字上限，已省略后 {note_dropped} 字")
    return {
        "content": "\n\n".join(parts),
        "system": system,
        "system_custom": bool(user_system),
        "warnings": warnings,
        "scenes": stats,
        "scene_ids": wanted,
    }


def estimate_input_tokens(text: str) -> int:
    """估算一次输入的 token 量。

    复用 `naiba/core/compaction.py::estimate_text_tokens` 的同一条启发式
    （ASCII 4 字符≈1 token，其余 1 字符≈1 token），并**明确标为估算**：
    字符数不等于真实 token，界面必须写「估算」。
    """
    from naiba.core.compaction import estimate_text_tokens

    return int(estimate_text_tokens(text))


def budget_report(*, content: str, chapters: list[dict[str, Any]], warnings: list[str],
                  body_limit: int, max_chapters: int,
                  context_window: int = 0,
                  output_reserve: int = OUTPUT_RESERVE_TOKENS,
                  system: str = "", overhead_chars: int = 0,
                  kg_count: int = 0) -> dict[str, Any]:
    """提交前预检：把这次输入与模型容量对照，给出可执行建议。

    `context_window` 未知（0）时如实标为未知，**不伪造模型上限**；此时大输入
    需要用户确认后才提交（前端据此弹确认，服务端不拦）。

    `system` 是随任务注入的系统提示词（图谱反哺），`overhead_chars` 是分块生成
    各轮衔接上下文的额外字符——两者都是真实会发送的输入，估算必须计入。
    """
    system_text = str(system or "")
    overhead = max(0, int(overhead_chars or 0))
    tokens = estimate_input_tokens(content) + (
        estimate_input_tokens(system_text) if system_text else 0
    ) + overhead
    chars = len(content) + len(system_text) + overhead
    total_chars = sum(int(row.get("total_chars") or 0) for row in chapters)
    used_chars = sum(int(row.get("used_chars") or 0) for row in chapters)
    dropped_chars = sum(int(row.get("dropped_chars") or 0) for row in chapters)
    known = int(context_window or 0) > 0
    available = max(0, int(context_window) - int(output_reserve)) if known else 0
    fits = (tokens <= available) if known else None
    suggestions: list[str] = []
    if known and not fits:
        over = tokens - available
        suggestions.append(f"估算输入比模型可用空间多约 {over} tokens")
        if dropped_chars == 0 and used_chars > 0:
            # 没触发截断还超：建议减章或调低每章字数上限。
            suggestions.append("可以减少勾选章节，或调低每章字数上限")
        else:
            suggestions.append("当前已按每章上限截断，仍超出模型容量，请减少章节或换容量更大的模型")
    if dropped_chars > 0:
        suggestions.append(
            f"本次有 {dropped_chars} 字因每章上限被省略；要完整引用请调高每章字数上限"
        )
    return {
        "chars": chars,
        "total_source_chars": total_chars,
        "used_chars": used_chars,
        "dropped_chars": dropped_chars,
        "estimated_tokens": tokens,
        "estimate_note": "字数是字符数；token 为估算值，不等于实际用量",
        "context_window": int(context_window or 0),
        "context_known": known,
        "output_reserve": int(output_reserve),
        "available_tokens": available,
        "fits": fits,
        "suggestions": suggestions,
        "warnings": list(warnings),
        "body_limit": clamp_body_limit(body_limit),
        "max_chapters": clamp_chapter_limit(max_chapters),
        "chapter_count": len(chapters),
        "kg_entities": max(0, int(kg_count or 0)),
        "system_chars": len(system_text),
        "overhead_chars": overhead,
        "chapters": [
            {
                "i": int(row.get("i") or 0),
                "title": str(row.get("title") or ""),
                "manuscript_id": str(row.get("manuscript_id") or ""),
                "manuscript_label": str(row.get("manuscript_label") or ""),
                "manuscript_kind": str(row.get("manuscript_kind") or ""),
                "total_chars": int(row.get("total_chars") or 0),
                "used_chars": int(row.get("used_chars") or 0),
                "dropped_chars": int(row.get("dropped_chars") or 0),
            }
            for row in chapters
        ],
    }


def task_title(book_title: str, stage: str) -> str:
    """任务标题：`《书名》· 阶段名`（与总计划 §5.3 的字段契约一致）。"""
    if stage == ADAPT_STAGE:
        return f"《{book_title}》· 单章改编"
    if stage == STORYBOARD_STAGE:
        return f"《{book_title}》· {STORYBOARD_LABEL}"
    return f"《{book_title}》· {STAGE_LABELS.get(stage, stage)}"
