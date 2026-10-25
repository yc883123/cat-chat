# -*- coding: utf-8 -*-
"""小说域提示词与长文预算（阶段二）。

阈值**全部集中在本文件**，不散落在服务层与前端（《阶段二》§五）。取值照启元实证：

| 做法 | 取证 | 本侧取值 |
|---|---|---|
| 章节级调用，全书永不进 prompt | `nv_main.js:1548-1568` | 一次只带勾选章节 |
| 参考素材硬上限 | `:1551` `slice(0,3000)` | `BODY_SLOT_CHARS = 3000` |
| 分槽限额 | `:2412-2417`（≤4000 token） | 要素 800 / 大纲 1200 |
| 滚动摘要链 | `:2569-2589`（近 3 章首 120 + 尾 220） | `SUMMARY_PREV_CHAPTERS = 3` |
| 截断绝不静默 | `:3458/:3476/:3510` | 超限说明超了多少、建议减到几章 |

摘要链是**零额外推理**的：不额外调模型，直接取每章正文的首尾片段。每章产出的
摘要落 `summaries[章号]`，后续调用带前 3 章，用来维持跨章一致性。
"""

from __future__ import annotations

import re
from typing import Any

# ---- 槽位上限（字符） ----
# 正文槽：参考素材硬上限（照竞品 slice(0,3000)）。
BODY_SLOT_CHARS = 3000
# 要素槽 / 大纲槽：分槽限额，防止要素把大纲的位置挤掉。
ELEMENTS_SLOT_CHARS = 800
OUTLINE_SLOT_CHARS = 1200
# 摘要链：带前几章、每章摘要取首尾多少字。
SUMMARY_PREV_CHAPTERS = 3
SUMMARY_HEAD_CHARS = 120
SUMMARY_TAIL_CHARS = 220
# 用户补充要求的上限（不让人把整本书塞进「补充说明」）。
EXTRA_NOTE_CHARS = 800
# 一次最多带几章正文：再多就明示让用户减章，不静默截断。
MAX_CHAPTERS_PER_CALL = 6

# 阶段名与中文标题（前端阶段导航、任务标题共用）。
STAGE_LABELS = {
    "elements": "要素",
    "outline": "改编大纲",
    "script": "剧本",
}
STAGES = tuple(STAGE_LABELS)


class PromptError(ValueError):
    """提示词组装失败（用户可读）。"""


def _clip(text: str, limit: int) -> tuple[str, int]:
    """按字符截断，返回 ``(片段, 被截掉的字符数)``。"""
    raw = str(text or "")
    if limit <= 0 or len(raw) <= limit:
        return raw, 0
    return raw[:limit], len(raw) - limit


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


def build_summary_chain(summaries: dict[str, Any], chapter_ids: list[int]) -> str:
    """把「前 N 章摘要」拼成提示词片段。

    `summaries` 是作品里的 `{章号字符串: 摘要}`；只带**当前批次之前**的最近 N 章，
    批次内的章节不算「前文」（它们的正文已经在正文槽里）。
    """
    if not summaries or not chapter_ids:
        return ""
    first = min(int(n) for n in chapter_ids)
    prior: list[int] = []
    for raw in summaries:
        try:
            number = int(str(raw))
        except (TypeError, ValueError):
            continue
        if number < first:
            prior.append(number)
    if not prior:
        return ""
    prior.sort()
    picked = prior[-SUMMARY_PREV_CHAPTERS:]
    lines = []
    for number in picked:
        text = str(summaries.get(str(number)) or summaries.get(number) or "").strip()
        if text:
            lines.append(f"第 {number} 章摘要：{text}")
    return "\n".join(lines)


def _chapter_blocks(chapters: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """拼正文槽：每章按 `BODY_SLOT_CHARS` 截断，返回 ``(正文片段, 截断说明)``。

    截断绝不静默：每一章的截断都记进说明，附「超了多少字」。
    """
    blocks: list[str] = []
    notes: list[str] = []
    for row in chapters:
        number = int(row.get("i") or 0)
        title = str(row.get("title") or f"第 {number} 章")
        content = str(row.get("content") or "")
        body, dropped = _clip(content, BODY_SLOT_CHARS)
        if dropped:
            notes.append(f"第 {number} 章《{title}》正文超出单章上限 {BODY_SLOT_CHARS} 字，"
                         f"已省略后 {dropped} 字")
        blocks.append(f"【第 {number} 章】{title}\n{body}")
    return "\n\n".join(blocks), notes


def check_chapter_budget(chapter_ids: list[int]) -> None:
    """一次带几章的硬闸门：超了直接拒绝并给出可操作建议，不静默减章。"""
    count = len(chapter_ids)
    if count == 0:
        raise PromptError("至少选择一章作为素材")
    if count > MAX_CHAPTERS_PER_CALL:
        raise PromptError(
            f"一次最多带 {MAX_CHAPTERS_PER_CALL} 章（这次选了 {count} 章，多了 {count - MAX_CHAPTERS_PER_CALL} 章）；"
            f"请分批生成，或先减少勾选章节"
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
) -> dict[str, Any]:
    """组装一次阶段生成的 content。

    返回 ``{content, warnings}``：`warnings` 是要如实展示给用户的截断/超限说明，
    前端必须显示，不能吞掉。

    剧本阶段（`stage="script"`）**必带大纲槽与人物槽**：写正文时不带设定与人物，
    同一个角色会换个名字（《阶段三》§三、§五 P1）。要素/大纲阶段不带这两槽——
    它们本来就是产出这两样的，再塞回去只会互相污染。
    """
    if stage not in _INSTRUCTIONS:
        raise PromptError(f"未知阶段：{stage}")
    check_chapter_budget(chapter_ids)

    body, notes = _chapter_blocks(chapters)
    chain = build_summary_chain(summaries or {}, chapter_ids)

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

    return {"content": "\n\n".join(parts), "warnings": notes}


def task_title(book_title: str, stage: str) -> str:
    """任务标题：`《书名》· 阶段名`（与总计划 §5.3 的字段契约一致）。"""
    return f"《{book_title}》· {STAGE_LABELS.get(stage, stage)}"
