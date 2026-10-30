# -*- coding: utf-8 -*-
"""织书台提示词层：设定 / 大纲 / 正文提示词、上下文分槽预算与严格 JSON 校验。

与拆书匠（naiba/novel/prompts.py）同层，但服务的是**原创**：输入是设定版本与
章节索引，不是导入原文。写法整理自启元原创模式（取证报告 §3），三条硬约束：

1. **权威提示词只在服务端**：前端不拼接提示词，任务快照固化全部输入，重试不换料。
2. **严格 JSON**：只接受纯 JSON 或单个代码围栏；字段类型、必填项、数量、章号
   唯一连续逐项校验——不用宽松正则从解释文字里猜，不补空角色 / 空大纲。
3. **上下文分槽**：世界观、角色、前文衔接各槽独立限长；不默认塞入全书正文。
   预检与正式提交共用同一组 build 函数，右栏看到的来源就是实际发送的来源。
"""

from __future__ import annotations

import json
import re
from typing import Any

from naiba.writer.store import WriterStoreError

# ---- 步骤定义 ----

# 六步自动链：题材 → 书名 → 简介 → 世界观 → 角色 → 大纲（与启元 NV.start() 同序；
# 大纲是第 6 步，落大纲版本并同步章节索引）。
SETUP_STEPS: tuple[str, ...] = ("genre", "title", "blurb", "world", "chars", "outline")
ALL_STEPS: tuple[str, ...] = (*SETUP_STEPS, "segs", "draft")

STEP_LABELS = {
    "genre": "题材推断",
    "title": "书名候选",
    "blurb": "作品简介",
    "world": "世界观设定",
    "chars": "角色设定",
    "outline": "章节大纲",
    "segs": "段落要点",
    "draft": "正文",
}

CHARS_MIN, CHARS_MAX = 1, 10
OUTLINE_MIN, OUTLINE_MAX = 5, 30
SEGS_MIN, SEGS_MAX = 2, 4

# ---- 写作风格约束（整理自启元 NV.stylePrompt，逐字保留） ----

STYLE_PRESETS = {
    "web": ("网文爽文（默认）", ""),
    "jinyong": ("历史正剧 · 金庸式",
                "【历史正剧 · 金庸式（最高优先约束，与其它要求冲突时以本条为准）】"
                "1. 史实为骨、虚构为肉，重大历史事件与结局不可更改；"
                "2. 虚构主角以小人物视角置身真实历史事件之中；"
                "3. 官职、军制、称谓、礼仪、兵器、物价、饮食须符合对应时代考据；"
                "4. 绝不出现修炼、法术、内力飞升等超自然设定；"
                "5. 每章开头标注对应的历史年份（年号）。"),
    "strict": ("严肃历史（严格考据）",
               "【严肃历史（最高优先约束）】"
               "1. 所有事件、人物、时间线必须符合正史记载；"
               "2. 虚构仅限小人物视角与私人细节；"
               "3. 官制、礼制、称谓、地理、物价按时代考据。"),
    "light": ("轻松幽默",
              "【轻松幽默】语言轻快、对白诙谐、梗密度高，但人物动机与情节逻辑必须自洽。"),
    "sus": ("悬疑烧脑",
            "【悬疑烧脑】每 2—3 章埋一个伏笔，章末必留钩子，信息释放有节奏，"
            "关键真相延迟到后段揭晓。"),
}


def style_prompt(style: str) -> str:
    """风格约束文本：未知取值按默认「网文爽文」处理（不加额外约束）。"""
    return STYLE_PRESETS.get(str(style or "").strip(), STYLE_PRESETS["web"])[1]


def style_label(style: str) -> str:
    return STYLE_PRESETS.get(str(style or "").strip(), STYLE_PRESETS["web"])[0]


# ---- 上下文槽位限长（正文写作的实际输入分槽） ----

WORLDVIEW_SLOT_CHARS = 800
CHARS_SLOT_CHARS = 500
CHARS_SLOT_COUNT = 6
PREV_TAIL_CHARS = 400
SUMMARY_HEAD_CHARS = 100
SUMMARY_TAIL_CHARS = 200
CONTINUE_TAIL_CHARS = 200
SEG_POINT_CHARS = 200
IDEA_SLOT_CHARS = 1200

# 估算系数：中文按 1 字 ≈ 0.6 token 粗估（只用于预算提示，不冒充精确计量）。
TOKENS_PER_CHAR = 0.6
# 容量未知时，估算输入超过这个量就要用户确认后再提交（与拆书匠同口径）。
LARGE_INPUT_CONFIRM_TOKENS = 60000


class PromptError(ValueError):
    """提示词组装或结果解析错误（用户可读，API 边界转 4xx；流水线据此停链）。"""


# ---- 设定内容 → 提示词槽位 ----

def _genre_line(setup: dict[str, Any]) -> str:
    genre = setup.get("genre") if isinstance(setup.get("genre"), dict) else {}
    bits = [str(genre.get("cat") or ""), str(genre.get("sub_cat") or ""), str(genre.get("tone") or "")]
    bits = [bit for bit in bits if bit]
    tags = [str(t) for t in (setup.get("genre_tags") or []) if str(t).strip()]
    line = " · ".join(bits) if bits else "（题材未定）"
    if tags:
        line += "｜关键词：" + "、".join(tags)
    return line


def _characters_text(setup: dict[str, Any], *, limit: int = CHARS_SLOT_CHARS,
                     count: int = CHARS_SLOT_COUNT) -> str:
    rows = [row for row in (setup.get("characters") or []) if isinstance(row, dict)
            and str(row.get("name") or "").strip()]
    lines = []
    for row in rows[:count]:
        role = str(row.get("role") or "").strip()
        identity = str(row.get("identity") or "").strip()
        goal = str(row.get("goal") or "").strip()
        line = f"{row['name']}" + (f"（{role}）" if role else "")
        detail = "；".join(bit for bit in (identity, goal) if bit)
        if detail:
            line += f"：{detail}"
        lines.append(line)
    text = "\n".join(lines) if lines else "（角色未定）"
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def _worldview_text(setup: dict[str, Any], *, limit: int = WORLDVIEW_SLOT_CHARS) -> str:
    text = str(setup.get("worldview") or "").strip() or "（世界观未定）"
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def _constraints_block(setup: dict[str, Any], *, include_style: bool = True) -> str:
    blocks: list[str] = []
    if include_style:
        style_text = style_prompt(str(setup.get("style") or ""))
        if style_text:
            blocks.append(style_text)
    constraints = setup.get("constraints") if isinstance(setup.get("constraints"), dict) else {}
    vibe = str(constraints.get("vibe") or "").strip()
    avoid = str(constraints.get("avoid") or "").strip()
    if vibe:
        blocks.append(f"【参考气质】{vibe}")
    if avoid:
        blocks.append(f"【回避项】{avoid}")
    return ("\n\n".join(blocks) + "\n") if blocks else ""


def idea_text(setup: dict[str, Any], *, limit: int = IDEA_SLOT_CHARS) -> str:
    text = str(setup.get("idea") or "").strip() or "（未填创意）"
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


# ---- 系统提示词（整理自启元 NV.SYS，逐字保留写法） ----

STEP_SYSTEM = {
    "genre": (
        "你是网文题材分析师。根据创作提示词，判定这部小说的题材画像，输出 JSON：\n"
        '{"channel":"male"或"female","cat":"大类（如 玄幻/都市/历史/权谋/军事）",'
        '"subCat":"子流派","tone":"基调（如 冷峻写实/热血爽文/慢热权谋）",'
        '"hook":"核心卖点一句话","tags":["关键词1","关键词2","关键词3"]}\n'
        "只输出 JSON，不要解释。"
    ),
    "title": (
        "你是网文书名策划师。基于创作提示词，输出 JSON 数组：[\"书名1\",\"书名2\",...]\n"
        "共 5—10 个候选，两种风格混合：短名立意（2—4字，如《赤心巡天》式）与"
        "卖点前置（带核心梗，如《重生：高三大器晚成》式）。"
    ),
    "blurb": (
        "你是网文简介写手。基于创作提示词，写 3 版作品简介，每版 150—200 字，"
        "用「处境—目标—悬念」三句式，突出世界观骨架、升级路线与差异化设定，"
        "不剧透后期设定。输出 JSON 数组：[\"简介1\",\"简介2\",\"简介3\"]"
    ),
    "world": (
        "你是网文世界观架构师。基于创作提示词，输出一部小说的世界观设定（Markdown，600—1200字）：\n"
        "一、世界骨架（天地/技术结构、时代风貌）；二、力量体系四件套（等级阶梯6阶以内、"
        "资源产出消耗、瓶颈关卡、不可豁免的代价）；三、势力与格局（至少两组可持续冲突源）；"
        "四、主线抓手（主角目标与阻碍）。\n直接输出设定正文。"
    ),
    "chars": (
        "你是网文角色设计师。基于创作提示词与世界观，输出 JSON 数组：\n"
        '[{"name":"姓名","role":"主角/配角/反派","identity":"身份一句话",'
        '"goal":"核心诉求一句话","traits":"性格标签","speech":"说话习惯",'
        '"arc":"人物弧光一句话","background":"背景与秘密"}]\n'
        "角色之间要有利益冲突或情感张力，反派动机必须合理。所有值纯中文。"
    ),
    "outline": (
        "你是网文大纲策划师。基于创作提示词与世界观，输出 JSON 对象：\n"
        '{"chapters":[{"no":1,"title":"第一章 标题","beat":"本章爽点/兑付点","hook":"章末钩子"}]}\n'
        "title 必须以「第X章」开头；爽点密度每章至少一个；"
        "章末钩子八式轮换（悬念/期待/危机/反转/情感/代价/信息/挑衅），同式不得连续使用。"
    ),
    "segs": (
        "你是网文章节策划。把一章的梗概拆成 2—4 个段落要点，输出 JSON 数组："
        "[\"段落1要点\",\"段落2要点\",...]\n"
        "每个要点一句话，按「开局钩子→推进→加压→兑付」顺序排列，覆盖全章梗概。"
    ),
    "draft": (
        "你是网文作家。基于给定设定与段落要点写一段正文（1200—1800字）：\n"
        "直接输出正文，不要解释写作思路；对白像真人说话，带潜台词；前三章新设定名词不超过3个。\n"
        "【节奏】开局即钩子，段内完成一次小兑付；结尾停在情节推进处，留给下一段续接。\n"
        "题材与尺度尊重创作提示词的约定，不做自我审查。"
    ),
}


def step_system(step: str) -> str:
    if step not in STEP_SYSTEM:
        raise PromptError(f"未知步骤：{step}")
    return STEP_SYSTEM[step]


# ---- 设定各步的用户提示词（预检与正式提交共用） ----

def build_step_user(step: str, setup: dict[str, Any], *, char_count: int = 4,
                    chapter_count: int = 10, extra_note: str = "") -> str:
    """六步设定的用户消息。`setup` 是选定设定版本的内容。"""
    if step not in SETUP_STEPS:
        raise PromptError(f"未知设定步骤：{step}")
    idea = idea_text(setup)
    style_block = _constraints_block(setup)
    parts = [f"【创作提示词】\n{idea}"]
    if step in ("world", "chars", "outline"):
        if str(setup.get("worldview") or "").strip():
            parts.append(f"【世界观摘要】\n{_worldview_text(setup, limit=1200)}")
    if step in ("chars", "outline"):
        chars_text = _characters_text(setup, limit=800, count=CHARS_SLOT_COUNT)
        has_chars = any(isinstance(r, dict) and str(r.get("name") or "").strip()
                        for r in (setup.get("characters") or []))
        if step == "outline" or has_chars:
            parts.append(f"【角色】\n{chars_text}")
    if step == "outline":
        title = str(setup.get("title") or "").strip()
        if title:
            parts.append(f"【书名】{title}")
        blurb = str(setup.get("blurb") or "").strip()
        if blurb:
            parts.append(f"【简介】\n{blurb[:600]}")
    if extra_note.strip():
        parts.append(f"【补充要求】\n{extra_note.strip()[:500]}")
    if style_block:
        parts.append(style_block.rstrip())
    if step == "genre":
        parts.append("请输出题材画像（JSON）。")
    elif step == "title":
        parts.append("请输出书名候选（JSON 数组）。")
    elif step == "blurb":
        parts.append("请输出 3 版简介（JSON 数组）。")
    elif step == "world":
        parts.append("请输出世界观设定。")
    elif step == "chars":
        parts.append(f"请输出 {char_count} 个角色（JSON 数组）。")
    elif step == "outline":
        parts.append(f"请输出 {chapter_count} 章大纲（JSON 对象）。")
    return "\n\n".join(parts)


# ---- 正文写作的上下文 ----

def chapter_head_summary(content: str, *, head: int = SUMMARY_HEAD_CHARS,
                         tail: int = SUMMARY_TAIL_CHARS) -> str:
    """章节首尾衔接片段：这是正文首尾原文截取，**不伪装成模型总结**。"""
    text = str(content or "").strip()
    if not text:
        return ""
    if len(text) <= head + tail + 20:
        return text
    return text[:head] + "\n……\n" + text[-tail:]


def build_draft_context(setup: dict[str, Any], chapter: dict[str, Any], *,
                        mode: str, seg_points: list[str] | None = None,
                        seg_index: int = 0, prev_tail: str = "",
                        draft_tail: str = "", extra_note: str = "") -> dict[str, Any]:
    """组装正文用户消息与槽位清单（预检与正式提交、右栏来源展示共用）。

    `mode`：whole 整章 / segment 分段 / continue 续写。
    返回 `{content, slots}`；slots 描述每个槽是否被带入及省略原因。
    """
    slots: list[dict[str, Any]] = []
    beat = str(chapter.get("beat") or "").strip()
    hook = str(chapter.get("hook") or "").strip()
    outline_text = beat or str(chapter.get("outline_beat") or "").strip()
    worldview = _worldview_text(setup)
    chars = _characters_text(setup)

    parts: list[str] = [f"【创作提示词】\n{idea_text(setup)}"]
    slots.append({"key": "idea", "label": "创作提示词", "included": True,
                  "chars": len(parts[0])})
    if str(setup.get("worldview") or "").strip():
        parts.append(f"【世界观摘要】\n{worldview}")
        slots.append({"key": "worldview", "label": "世界观摘要", "included": True,
                      "chars": len(worldview)})
    else:
        slots.append({"key": "worldview", "label": "世界观摘要", "included": False,
                      "reason": "设定里还没有世界观"})
    has_chars = [r for r in (setup.get("characters") or []) if isinstance(r, dict)]
    if has_chars:
        parts.append(f"【出场角色】\n{chars}")
        slots.append({"key": "characters", "label": "出场角色", "included": True,
                      "chars": len(chars)})
    else:
        slots.append({"key": "characters", "label": "出场角色", "included": False,
                      "reason": "设定里还没有角色"})
    if outline_text:
        chapter_line = f"【本章梗概】第{chapter.get('no')}章 {chapter.get('title')}"
        if outline_text:
            chapter_line += f" —— {outline_text}"
        if hook:
            chapter_line += f"\n【章末钩子】{hook}"
        parts.append(chapter_line)
        slots.append({"key": "chapter_brief", "label": "本章梗概", "included": True,
                      "chars": len(chapter_line)})
    if prev_tail.strip():
        parts.append(f"【上一章结尾】\n{prev_tail.strip()}")
        slots.append({"key": "prev_tail", "label": "上一章结尾", "included": True,
                      "chars": len(prev_tail.strip())})
    if mode == "segment":
        points = [str(p).strip()[:SEG_POINT_CHARS] for p in (seg_points or []) if str(p).strip()]
        if points:
            lines = []
            for index, point in enumerate(points):
                marker = "▶ " if index == seg_index else "· "
                lines.append(f"{marker}{point}")
            parts.append("【段落要点】\n" + "\n".join(lines))
            slots.append({"key": "seg_points", "label": "段落要点", "included": True,
                          "chars": len("\n".join(lines))})
    if mode == "continue" and draft_tail.strip():
        parts.append(f"【当前稿结尾】\n{draft_tail.strip()}")
        slots.append({"key": "draft_tail", "label": "当前稿结尾", "included": True,
                      "chars": len(draft_tail.strip())})
    if extra_note.strip():
        note = extra_note.strip()[:500]
        parts.append(f"【补充要求】\n{note}")
        slots.append({"key": "extra_note", "label": "补充要求", "included": True,
                      "chars": len(note)})
    style_block = _constraints_block(setup)
    if style_block:
        parts.append(style_block.rstrip())
        slots.append({"key": "style", "label": "风格与约束", "included": True,
                      "chars": len(style_block)})
    if mode == "whole":
        parts.append(f"请写出第 {chapter.get('no')} 章完整正文（2000—4000字）。")
    elif mode == "continue":
        parts.append(f"请从当前稿结尾处继续写第 {chapter.get('no')} 章正文，紧接上文、不重复已写内容。")
    else:
        parts.append(f"现在写第 {chapter.get('no')} 章第 {seg_index + 1} 段（只输出本段正文，1200—1800字）。")
    return {"content": "\n\n".join(parts), "slots": slots}


def budget_report(*, content: str, slots: list[dict[str, Any]],
                  context_window: int, extra_items: dict[str, int] | None = None) -> dict[str, Any]:
    """输入预算：included / omitted / estimated_tokens / context_known / fits。

    已知容量不足在排队前阻止；容量未知且输入很大时要求用户确认（不伪造模型上限）。
    """
    omitted = [
        {"key": slot.get("key"), "label": slot.get("label"),
         "reason": str(slot.get("reason") or "")}
        for slot in slots if not slot.get("included")
    ]
    chars = len(content)
    if extra_items:
        chars += sum(int(value) for value in extra_items.values())
    estimated_tokens = int(chars * TOKENS_PER_CHAR) + 1
    known = bool(context_window and context_window > 0)
    fits = True
    suggestions: list[str] = []
    if known:
        # 生成还需要输出空间：按输入 ≤ 窗口的 60% 估算（经验闸门，偏保守）。
        limit = int(context_window * 0.6)
        if estimated_tokens > limit:
            fits = False
            suggestions.append(
                f"本次输入约 {estimated_tokens} tokens，超过模型窗口（{context_window}）的保守可用额（{limit}）；"
                "请减少设定或前文引用后再生成"
            )
    return {
        "included": [slot for slot in slots if slot.get("included")],
        "omitted": omitted,
        "estimated_tokens": estimated_tokens,
        "context_known": known,
        "fits": fits,
        "suggestions": suggestions,
        "chars": chars,
    }


# ---- 严格 JSON 解析 ----

_FENCE_PATTERN = re.compile(
    r"^\s*```[a-zA-Z0-9_-]*\s*\n(.*?)\n?```\s*$", re.DOTALL,
)


def extract_json_payload(text: str) -> Any:
    """取出模型输出里的 JSON：纯 JSON 或单个代码围栏；其余一律报错。

    不用宽松正则从解释文字中猜 JSON（实施计划 §6.1）。
    """
    raw = str(text or "").strip()
    if not raw:
        raise PromptError("模型没有返回内容")
    candidate = raw
    fence = _FENCE_PATTERN.match(raw)
    if fence:
        candidate = fence.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise PromptError(
            "模型输出不是合法 JSON（只接受纯 JSON 或单个代码围栏）；可重试或换模型"
        ) from exc


def parse_genre(text: str) -> dict[str, Any]:
    payload = extract_json_payload(text)
    if not isinstance(payload, dict):
        raise PromptError("题材结果必须是 JSON 对象")
    cat = str(payload.get("cat") or "").strip()
    if not cat:
        raise PromptError("题材结果缺少 cat（大类）字段")
    channel = str(payload.get("channel") or "").strip().lower()
    if channel not in ("male", "female"):
        channel = ""
    tags_raw = payload.get("tags")
    tags: list[str] = []
    if isinstance(tags_raw, list):
        for item in tags_raw:
            value = str(item or "").strip()
            if value and value not in tags:
                tags.append(value)
            if len(tags) >= 5:
                break
    return {
        "channel": channel,
        "cat": cat[:40],
        "sub_cat": str(payload.get("subCat") or "").strip()[:40],
        "tone": str(payload.get("tone") or "").strip()[:40],
        "hook": str(payload.get("hook") or "").strip()[:200],
        "tags": tags,
    }


def _parse_str_list(text: str, *, min_count: int, max_count: int, what: str) -> list[str]:
    payload = extract_json_payload(text)
    if not isinstance(payload, list):
        raise PromptError(f"{what}必须是 JSON 数组")
    rows: list[str] = []
    for item in payload:
        value = str(item or "").strip()
        if value:
            rows.append(value)
    if len(rows) < min_count:
        raise PromptError(f"{what}只有 {len(rows)} 条（至少需要 {min_count} 条）；如实失败，不补空条目")
    return rows[:max_count]


def parse_title_candidates(text: str) -> list[str]:
    return [row[:120] for row in _parse_str_list(text, min_count=1, max_count=10, what="书名候选")]


def parse_blurb_candidates(text: str) -> list[str]:
    return [row[:2000] for row in _parse_str_list(text, min_count=1, max_count=6, what="简介候选")]


def parse_world(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        raise PromptError("世界观结果为空")
    fence = _FENCE_PATTERN.match(raw)
    if fence:
        raw = fence.group(1).strip()
    if len(raw) > 30000:
        raw = raw[:30000]
    return raw


def parse_characters(text: str, *, wanted: int) -> list[dict[str, str]]:
    payload = extract_json_payload(text)
    if isinstance(payload, dict) and isinstance(payload.get("characters"), list):
        payload = payload["characters"]
    if not isinstance(payload, list):
        raise PromptError("角色结果必须是 JSON 数组")
    rows: list[dict[str, str]] = []
    for item in payload:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        rows.append({
            "name": name[:60],
            "role": str(item.get("role") or "").strip()[:40],
            "identity": str(item.get("identity") or "").strip()[:200],
            "goal": str(item.get("goal") or "").strip()[:200],
            "traits": str(item.get("traits") or "").strip()[:200],
            "speech": str(item.get("speech") or "").strip()[:200],
            "arc": str(item.get("arc") or "").strip()[:200],
            "background": str(item.get("background") or "").strip()[:1000],
        })
    if len(rows) < wanted:
        raise PromptError(
            f"角色结果只有 {len(rows)} 个（要求 {wanted} 个）；如实失败，不补空角色"
        )
    return rows[:wanted]


def parse_outline(text: str, *, wanted: int) -> list[dict[str, Any]]:
    payload = extract_json_payload(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("chapters"), list):
        raise PromptError("大纲结果必须是含 chapters 数组的 JSON 对象")
    rows: list[dict[str, Any]] = []
    for item in payload["chapters"]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        rows.append({
            "no": item.get("no", len(rows) + 1),
            "title": title[:200],
            "beat": str(item.get("beat") or "").strip()[:2000],
            "hook": str(item.get("hook") or "").strip()[:500],
        })
    if len(rows) < wanted:
        raise PromptError(
            f"大纲只有 {len(rows)} 章（要求 {wanted} 章）；如实失败，不补空章节"
        )
    nos: list[Any] = [row["no"] for row in rows[:wanted]]
    cleaned: list[dict[str, Any]] = []
    for index, row in enumerate(rows[:wanted], start=1):
        no_raw = row["no"]
        if isinstance(no_raw, bool) or not isinstance(no_raw, int) or no_raw < 1:
            no_raw = index
        cleaned.append({**row, "no": no_raw})
    seen: set[int] = set()
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(cleaned, start=1):
        no = row["no"] if row["no"] not in seen else index
        seen.add(no)
        normalized.append({**row, "no": no})
    if [row["no"] for row in normalized] != list(range(1, len(normalized) + 1)):
        # 章号缺失 / 重复：按顺序重排，但要求条目数本身已够（前面已校验）。
        normalized = [{**row, "no": pos} for pos, row in enumerate(normalized, start=1)]
    return normalized


def parse_segs(text: str) -> list[str]:
    rows = _parse_str_list(text, min_count=SEGS_MIN, max_count=SEGS_MAX, what="段落要点")
    return [row[:SEG_POINT_CHARS] for row in rows]


def parse_draft(text: str, *, finish_reason: str = "") -> str:
    body = str(text or "").strip()
    if not body:
        raise PromptError("正文结果为空")
    if finish_reason == "length":
        raise PromptError(
            "正文因达到输出长度上限被截断（finish_reason=length）；请续写或重试，不把残稿当完成稿"
        )
    return body
