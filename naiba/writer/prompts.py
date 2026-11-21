# -*- coding: utf-8 -*-
"""织书台提示词层：设定 / 大纲 / 正文提示词、上下文分槽预算与严格 JSON 校验。

与拆书匠（naiba/novel/prompts.py）同层，但服务的是**原创**：输入是设定版本与
章节索引，不是导入原文。写法整理自启元原创模式（取证报告 §3），三条硬约束：

1. **权威提示词只在服务端**：前端不拼接提示词，任务快照固化全部输入，重试不换料。
2. **严格结构**：只接受纯 JSON 或单个代码围栏；按白名单校验字段、类型与数量，
   章号按目录顺序分配。不从解释文字里猜结构，不补空角色 / 空大纲。
3. **上下文分槽**：世界观、角色、前文衔接各槽独立限长；不默认塞入全书正文。
   预检与正式提交共用同一组 build 函数，右栏看到的来源就是实际发送的来源。
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from typing import Any

from naiba.writer.store import (
    CHAPTER_CONTENT_MAX_CHARS, CHAPTER_STATE_KEYS, WriterStoreError, normalize_chapter_state,
)

# ---- 步骤定义 ----

# 设定自动链：题材 → 书名 → 简介 → 世界观 → 角色（第二次反馈：设定只出设定，
# 大纲与章节不在设定链里——「大纲章数」曾让章节目录在设定阶段就凭空多出 30 章）。
# 老的「大纲」步保留：只服务于老 kind `outline`（逐章大纲）的兼容路径。
SETUP_STEPS: tuple[str, ...] = ("genre", "title", "blurb", "world", "chars")
# 3.9.13 起「大纲」不再等于章节表：大纲是一整篇文字（可分部），章节由独立的 split 步拆出来。
TEXT_STEPS: tuple[str, ...] = ("outline_text", "split", "outline_review")
# state：章末状态提取（跨章连贯性修复计划 §三.B）——正文写完落盘后跑的派生步，
# 失败不丢正文，也不阻断链上后续章节。
ALL_STEPS: tuple[str, ...] = (*SETUP_STEPS, *TEXT_STEPS, "segs", "draft", "state")

STEP_LABELS = {
    "genre": "题材推断",
    "title": "书名候选",
    "blurb": "作品简介",
    "world": "世界观设定",
    "chars": "角色设定",
    "outline": "章节大纲",
    "outline_text": "全篇大纲",
    "outline_review": "多部复核梳理",
    "split": "拆章",
    "segs": "段落要点",
    "draft": "正文",
    "state": "章末状态",
}

CHARS_MIN, CHARS_MAX = 1, 10
SIDE_COUNT_MAX = 99
# 旧的「大纲=章节表」上限，只服务于老 kind（outline）的兼容路径。
OUTLINE_MIN, OUTLINE_MAX = 5, 30
SEGS_MIN, SEGS_MAX = 2, 4
# 分部大纲：部数 1–20，每部正文上限 20 万字符（"几万字"够用，再大必然超窗口）。
PARTS_MIN, PARTS_MAX = 1, 20
PART_TEXT_MAX = 200_000
# 拆章：一部最多 9999 章；单条流水线最多 999 章（第二次反馈：200 不够用）。
SPLIT_MIN, SPLIT_MAX = 1, 9999
SPLIT_BATCH_MAX = 999
# 拆章自动分批（第三次反馈⑦）：一次让模型拆 180 章，它会先在思考里把全书推演一遍，
# 正文一个字没出就撞「思考异常冗长」熔断。切成小批连拆，每批落一次盘、带上前文衔接。
SPLIT_STEP_CHAPTERS = 20
# 接续拆分时带上的「已拆章节」衔接上下文：最多最近 8 章、总量上限（防撑窗口）。
SPLIT_PREV_MAX_CHAPTERS = 8
SPLIT_PREV_MAX_CHARS = 2400
# 单章字数限制的合法取值（0 = 不限）。8000/9000 档是第三次反馈补的：
# 阅读平台按章订阅，常见单章 4000–9000 字。
CHAPTER_CHARS_CHOICES = (0, 2000, 3000, 4000, 6000, 8000, 9000)
# 章节结尾策略。
ENDING_CHOICES = ("suspense", "natural", "hook")
ENDING_LABELS = {"suspense": "留悬念", "natural": "自然收", "hook": "留钩子+预告"}

# ---- 写作风格约束（整理自启元 NV.stylePrompt，逐字保留） ----

STYLE_PRESETS = {
    "web": ("自由写作", ""),
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
    # ---- 3.9.13：按反馈补到 30 种（统一模板 + 每风格 2–3 句差异） ----
    "realism": ("现实主义", "客观冷静地写日常，细节要真，社会现状与人物处境比情节反转更重要；不美化也不煽情。"),
    "romance": ("浪漫主义", "情感与想象优先，允许理想化与英雄气；自然景物可以承载情绪，但别写成散文。"),
    "magic_real": ("魔幻现实主义", "把神奇事件当日常写：超自然现象不做解释、不惊讶，人物照常过日子。"),
    "stream": ("意识流", "贴着人物内心走，允许跳跃与重复，时间可不线性；但每段仍要有一个可抓的动作或物件。"),
    "black_humor": ("黑色幽默", "用荒诞与反讽讲严肃甚至悲惨的事，语调冷静；不解释笑点，让读者自己品。"),
    "gothic": ("哥特式", "阴郁、神秘、衰败氛围；古堡旧宅、雾、旧物、传闻；恐怖来自不安而非血腥。"),
    "hardboiled": ("硬汉派（冷硬派）", "语言简练冷峻，短句多；主角多为边缘人，靠行动推进；暴力点到为止，情绪不外露。"),
    "minimal": ("极简主义", "文字精炼，省略背景与解释；靠对话和动作暗示，留白多，忌形容词堆砌。"),
    "baroque": ("巴洛克式", "辞藻华丽、句式繁复、感官描写密集；但每段仍要推进情节，不能只有装饰。"),
    "existential": ("存在主义", "写人的孤独、自由与选择，带思辨；允许人物自省，但不写成论文。"),
    "satire": ("讽刺主义", "用夸张与反语揭短，语调可诙谐可尖锐；讽刺对象要具体，不空喊。"),
    "naturalism": ("自然主义", "比现实主义更冷：环境与出身决定命运，底层生活不粉饰；不安排廉价的转机。"),
    "epic": ("史诗风格", "格局大、时间跨度长，涉族群与历史命运；叙事庄重，忌轻佻调侃。"),
    "pastoral": ("田园牧歌", "乡村的宁静与秩序，四季与劳作入景；冲突来自人心与外部世界的侵入，节奏舒缓。"),
    "thriller": ("悬疑惊悚", "每段留一个问题，节奏快；信息分批给，忌一次解释完；恐惧来自未知而非血腥。"),
    "cyberpunk": ("赛博朋克", "高科技低生活，霓虹与潮湿的街；技术异化人，身份可被改写；术语密度适中。"),
    "steampunk": ("蒸汽朋克", "维多利亚美学 + 蒸汽机械；齿轮、黄铜、飞艇；技术设定自洽，别混入电力魔法。"),
    "wuxia": ("武侠风格", "江湖义气与门派规矩；打斗写招式与分寸，不写能量条；语言半文半白，意境留白。"),
    "xianxia": ("仙侠风格", "道门与修真体系，境界与代价要对得上；语言飘逸，忌现代口语与网络梗。"),
    "lightnovel": ("轻小说风格", "语言轻快，内心独白多，角色个性鲜明；段落短、对白多，严肃处也要收着写。"),
    "epistolary": ("书信体 / 日记体", "用信件、日记、记录推进叙事；主观视角，允许不可靠叙述，时间可跳跃。"),
    "metafiction": ("元小说", "作者身份可进入文本，讨论创作本身；但故事仍要能读下去，不能只剩炫技。"),
    "postmodern": ("后现代主义", "碎片化、拼贴、戏仿；消解单一意义，允许断裂结构，但要给读者可跟随的线索。"),
    "new_sensation": ("新感觉派", "重主观感受与新奇比喻，抓瞬间印象与城市节奏；语言敏感，忌陈词。"),
    "local": ("乡土文学", "扎根特定地域，方言与风俗克制使用；人情与土地的重量大于情节爽感。"),
    "hard_sf": ("科幻硬核", "严格遵循已知科学，技术细节要能自圆其说；推演优先于奇观，术语首次出现要解释。"),
    "space_opera": ("太空歌剧", "宇宙尺度 + 冒险浪漫，科学不必精确；重戏剧冲突与阵营对立，场面要大。"),
    "aesthetic": ("唯美主义", "追求形式与感官之美，语言精致；情节可以为意境让路，但不写成空转抒情。"),
    "absurd": ("荒诞派", "人与世界的疏离，情节不合逻辑，对话答非所问；荒诞之下要有真实的痛感。"),
    "webnovel": ("网络爽文", "节奏极快，三章内给第一个爽点；主角成长与逆袭清晰，章末必留钩子；语言直白。"),
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
# 正文上下文的人物槽：不再「最多 6 人 + 总长 500」一刀切（跨章连贯性修复计划 §二.3）。
# 改为按预算挑选相关人物：主角必带、本章梗概/前文点名的配角必带，其余配角按预算补足；
# 预算不够时压缩次要描述，不静默丢掉相关角色。总输入仍有 budget_report 把关。
DRAFT_CHARS_SLOT_CHARS = 2400
# 人物条目的字段预算：身份/背景/关系是高优字段，预算紧张时先丢性格/说话/目标，
# 不从条目中段乱砍；字段超长时保留开头（关键事实通常在句首）。
_CHAR_FIELDS_FULL = (
    # (字段, 备选旧字段, 标签, 上限)
    ("identity", "", "身份", 100),
    ("origin", "", "出身", 60),
    ("intro", "background", "背景", 160),
    ("relations", "", "关系", 160),
    ("personality", "traits", "性格", 80),
    ("voice", "speech", "说话", 60),
    ("goal", "", "目标", 80),
)
_CHAR_FIELDS_MIN = (
    ("identity", "", "身份", 60),
    ("intro", "background", "背景", 80),
    ("relations", "", "关系", 100),
)
PREV_TAIL_CHARS = 400
SUMMARY_HEAD_CHARS = 100
SUMMARY_TAIL_CHARS = 200
CONTINUE_TAIL_CHARS = 200
SEG_POINT_CHARS = 200
IDEA_SLOT_CHARS = 1200
# 前情状态槽（章末状态渲染）预算：窗口内每章一段，超预算丢最旧的章。
CHAPTER_STATES_SLOT_CHARS = 2400
CHAPTER_STATE_TEXT_CHARS = 800
# 状态提取的正文输入预算：超出按「头 + 尾」截取（章末状态对结尾更敏感）。
STATE_CONTENT_MAX_CHARS = 16000
STATE_CONTENT_HEAD_CHARS = 12000
STATE_CONTENT_TAIL_CHARS = 4000

# 估算系数：中文按 1 字 ≈ 0.6 token 粗估（只用于预算提示，不冒充精确计量）。
TOKENS_PER_CHAR = 0.6
# 容量未知时，估算输入超过这个量就要用户确认后再提交（与拆书匠同口径）。
LARGE_INPUT_CONFIRM_TOKENS = 60000


class PromptError(ValueError):
    """提示词组装或结果解析错误（用户可读，API 边界转 4xx）。"""


# 原文按字符保留，至少能容下正文/分部大纲的 20 万字与代码围栏。
CONTENT_ISSUE_RAW_MAX = 256_000


class ContentIssue(PromptError):
    """待处理结果：原文用于查看，partial 用于编辑，只有 accepted 可直接采用。"""

    def __init__(self, message: str, *, category: str = "invalid_content",
                 raw: str = "", partial: Any = None, accepted: Any = None,
                 missing: list[str] | None = None, counts: dict[str, int] | None = None) -> None:
        super().__init__(message or "模型结果需要处理")
        self.category = category
        self.raw = raw[:CONTENT_ISSUE_RAW_MAX]
        self.raw_truncated = len(raw) > CONTENT_ISSUE_RAW_MAX
        self.partial_truncated = isinstance(partial, str) and len(partial) > CONTENT_ISSUE_RAW_MAX
        self.partial = partial[:CONTENT_ISSUE_RAW_MAX] if self.partial_truncated else partial
        self.accepted = accepted
        self.missing = missing or []
        self.counts = counts or {}
        self.available_actions = ["retry", "supplement"]
        if accepted is not None:
            self.available_actions.append("accept")
        self.available_actions.append("skip")


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


def _character_line(row: dict[str, Any], *,
                    fields: tuple[tuple[str, str, str, int], ...] = _CHAR_FIELDS_FULL) -> str:
    """单个人物条目：字段顺序固定（身份→出身→背景→关系→性格→说话→目标），
    单字段独立限长、超长保留开头；预算紧张时换更小的字段集，而不是中段乱砍。"""
    name = str(row.get("name") or "").strip()
    role_raw = str(row.get("role") or "").strip()
    role_text = {"lead": "主角", "side": "配角"}.get(role_raw, role_raw)
    line = name + (f"（{role_text}）" if role_text else "")
    bits: list[str] = []
    for key, alt, label, cap in fields:
        value = str(row.get(key) or "").strip()
        if not value and alt:
            value = str(row.get(alt) or "").strip()
        if not value:
            continue
        if len(value) > cap:
            value = value[:cap] + "…"
        bits.append(f"{label}：{value}" if label else value)
    if bits:
        line += "：" + "；".join(bits)
    return line


def _character_aliases(row: dict[str, Any]) -> list[str]:
    """可用于正文点名匹配的名字集合：本名 + 别名（别名按分隔符拆开）。"""
    names = [str(row.get("name") or "").strip()]
    alias_raw = str(row.get("alias") or "").strip()
    if alias_raw:
        names.extend(bit.strip() for bit in re.split(r"[、,，/;；|]+", alias_raw) if bit.strip())
    return [name for name in names if name]


def select_context_characters(rows: list[Any], *,
                              relevance_text: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """聚焦人物选择（跨章连贯性修复计划 §三.A）：主角优先，配角结合
    「章节梗概 / 上一章出场人物 / 显式提及」挑选。

    返回 (must_include, others)：must_include 是全部主角 + 被点名的配角
    （预算再紧也不能丢）；others 是其余配角（按预算补足，可被排除）。
    """
    valid = [row for row in rows if isinstance(row, dict) and str(row.get("name") or "").strip()]
    needle = str(relevance_text or "")
    leads = [row for row in valid if str(row.get("role") or "") == "lead"]
    sides = [row for row in valid if str(row.get("role") or "") != "lead"]
    mentioned = [row for row in sides
                 if any(name in needle for name in _character_aliases(row))]
    others = [row for row in sides if row not in mentioned]
    return leads + mentioned, others


def _fit_characters_text(must_include: list[dict[str, Any]], others: list[dict[str, Any]],
                         *, limit: int) -> tuple[str, list[str]]:
    """按总预算渲染人物条目：必带人物压缩描述也要带上；其余配角放不下就排除并如实报告。"""
    lines: list[str] = []
    excluded: list[str] = []
    used = 0

    def _append(text: str) -> bool:
        nonlocal used
        cost = len(text) + (1 if lines else 0)
        if used + cost > limit:
            return False
        lines.append(text)
        used += cost
        return True

    for row in must_include:
        text = _character_line(row)
        if not _append(text):
            # 相关人物不许丢：换最小字段集，仍超预算也强制带上（宁可略超不缺人）。
            text = _character_line(row, fields=_CHAR_FIELDS_MIN)
            cost = len(text) + (1 if lines else 0)
            lines.append(text)
            used += cost
    for row in others:
        text = _character_line(row)
        if _append(text):
            continue
        if _append(_character_line(row, fields=_CHAR_FIELDS_MIN)):
            continue
        excluded.append(str(row.get("name") or "").strip())
    return "\n".join(lines), excluded


def _characters_text(setup: dict[str, Any], *, limit: int = CHARS_SLOT_CHARS,
                     count: int | None = CHARS_SLOT_COUNT) -> str:
    """设定链（chars/outline/大纲）用的角色摘要：主角先带，配角按数组顺序补足。

    同样走预算装填（放不下的整条排除，不中段乱砍），字段用最小集保持紧凑。
    """
    rows = [row for row in (setup.get("characters") or []) if isinstance(row, dict)
            and str(row.get("name") or "").strip()]
    rows.sort(key=lambda row: 0 if str(row.get("role") or "") == "lead" else 1)
    if count is not None:
        rows = rows[:count]
    lines: list[str] = []
    used = 0
    for row in rows:
        text = _character_line(row, fields=_CHAR_FIELDS_MIN)
        cost = len(text) + (1 if lines else 0)
        if used + cost > limit and lines:
            continue
        lines.append(text)
        used += cost
    return "\n".join(lines) if lines else "（角色未定）"


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
    refs = [row for row in (setup.get("references") or []) if isinstance(row, dict)
            and str(row.get("title") or "").strip()]
    if refs:
        lines = []
        for row in refs:
            dims = "、".join(str(dim) for dim in (row.get("dims") or []) if str(dim).strip())
            note = str(row.get("note") or "").strip()
            same = "（本作品同人）" if row.get("same_fanfic") else ""
            bit = f"《{row['title']}》{same}"
            if dims:
                bit += f"：只参考{dims}"
            if note:
                bit += f"；{note}"
            lines.append(bit)
        blocks.append("【参考作品】（只学结构与手法，不抄情节与文字）\n" + "\n".join(lines))
    return ("\n\n".join(blocks) + "\n") if blocks else ""


def idea_text(setup: dict[str, Any], *, limit: int = IDEA_SLOT_CHARS) -> str:
    text = str(setup.get("idea") or "").strip() or "（未填创意）"
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


# ---- 系统提示词：只保留任务协议，不规定默认文风 ----

OUTPUT_MODE_LABELS = {"json": "结构化字段（JSON）", "freeform": "自由文本"}
JSON_STEPS = frozenset({"genre", "title", "blurb", "chars", "outline", "split", "segs", "state"})


def output_mode(value: str) -> str:
    return "freeform" if str(value or "").strip().lower() == "freeform" else "json"


STEP_SYSTEM = {
    "genre": (
        "根据创作提示词整理题材画像。只输出一个 JSON 对象，例如：\n"
        '{"channel":"male","cat":"大类","subCat":"子流派",'
        '"tone":"基调","hook":"核心卖点一句话","tags":["关键词"]}\n'
        "cat（题材大类）必须是非空字符串，不要用 subCat（子流派）替代。"
        "channel 如填写，只用 male 或 female。不要附加解释。"
    ),
    "title": (
        "根据创作提示词整理书名候选，输出 JSON 数组：[\"书名1\",\"书名2\",...]。"
    ),
    "blurb": (
        "根据创作提示词写作品简介，输出 JSON 数组：[\"简介1\",\"简介2\",...]。"
    ),
    "world": (
        "根据创作提示词与现有设定写世界观设定（Markdown 或自然文本）。\n"
        "只输出设定正文，不解释。"
    ),
    "chars": (
        "根据创作提示词与世界观整理角色，输出 JSON 数组；每项包含 name、role、identity、goal、traits、speech、arc、background、relations。\n"
        "按用户要求给足数量，角色必须有独立动机和关系。"
    ),
    "outline": (
        "根据创作提示词与世界观整理章节大纲，输出 JSON 对象："
        '{"chapters":[{"no":1,"title":"标题","beat":"本章内容","hook":"结尾安排"}]}。'
    ),
    "outline_text": (
        "根据创作提示词、世界观与角色，写这一部的整篇大纲（不要分章、不要 JSON）：\n"
        "用主线、本部目标、阶段推进、关键转折、结尾落点组织，直接输出大纲正文。"
    ),
    "outline_review": (
        "对照整本书上下文修订当前部的大纲，只输出修订后的大纲正文，不要 JSON、不要复核说明。"
    ),
    "split": (
        "把给定的大纲正文拆成章节目录，按顺序输出 JSON 对象，字段为 no、title、beat、hook。"
    ),
    "segs": (
        "把一章梗概拆成 2—4 个段落要点，输出 JSON 数组；每个要点一句话。"
    ),
    "draft": (
        "基于给定设定与段落要点直接输出正文，不解释写作思路，遵守作品系统提示词的文风。"
    ),
    "state": (
        "通读给定章节正文，提取本章结束时的事实状态。只输出一个 JSON 对象："
        '{"characters":[{"name":"角色姓名","presence":"出场情况","location":"位置","condition":"状态"}],'
        '"relations":[{"between":["角色甲","角色乙"],"change":"关系变化"}],'
        '"knowledge":[{"name":"角色姓名","known":["已知事实"],"unknown":["未知事实"]}],'
        '"events":["本章事件"],"open_threads":["未解线索"],"next_hooks":["下章钩子"]}。'
        "没有可记录事实的字段给空数组。只记录正文明确写出的事实，不补造内容。"
    ),
}


def step_system(step: str, global_system_prompt: str = "") -> str:
    if step not in STEP_SYSTEM:
        raise PromptError(f"未知步骤：{step}")
    # 作品系统提示词是唯一的 system 级写作规则；阶段协议进入 user 输入，避免
    # 内置写法与用户作品方向争抢同一优先级。
    return str(global_system_prompt or "")


def stage_protocol(step: str, mode: str = "json") -> str:
    current = output_mode(mode)
    if current == "freeform":
        return (f"【本阶段内置要求｜只读】\n当前阶段：{STEP_LABELS.get(step, step)}。\n"
                "请直接输出自然文本，不要输出 JSON；结果会保存为原文，题材、简介、分类、角色、章节等结构化字段不会自动填入。\n"
                "写作方向、题材和文风以作品系统提示词为准。")
    if step not in JSON_STEPS:
        return (f"【本阶段内置要求｜只读】\n当前阶段：{STEP_LABELS.get(step, step)}。\n"
                "请直接输出自然文本，不要套 JSON。写作方向、题材和文风以作品系统提示词为准。")
    return (f"【本阶段内置要求｜只读】\n当前阶段：{STEP_LABELS.get(step, step)}。\n"
            "请严格按本阶段要求只输出 JSON，不附加说明；程序会校验字段、类型、数量和必要结构。\n"
            "JSON 只是内部交接格式，写作方向、题材和文风以作品系统提示词为准。")


# ---- 设定各步的用户提示词（预检与正式提交共用） ----

def build_step_user(step: str, setup: dict[str, Any], *, char_count: int = 4,
                    side_count: int = 0, chapter_count: int = 10,
                    extra_note: str = "", output_mode_value: str = "json") -> str:
    """设定各步的用户消息。`setup` 是选定设定版本的内容。

    `char_count` / `side_count` 只在角色步生效：主角与配角数量都要给足
    （第二次反馈：只报主角数会让 20 个配角一个都不出）。

    `outline` 步不在设定链里了，但老的 `outline` 流水线 kind 还会走这里，保留。
    """
    if step not in (*SETUP_STEPS, "outline"):
        raise PromptError(f"未知设定步骤：{step}")
    idea = idea_text(setup)
    style_block = _constraints_block(setup)
    parts = [stage_protocol(step, output_mode_value), f"【创作提示词】\n{idea}"]
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
    if output_mode(output_mode_value) == "freeform":
        parts.append("请自由输出本阶段内容；不要尝试替我填写结构化字段。")
    elif step == "genre":
        parts.append(STEP_SYSTEM["genre"])
    elif step == "title":
        parts.append("请输出书名候选（JSON 数组）。")
    elif step == "blurb":
        parts.append("请输出 3 版简介（JSON 数组）。")
    elif step == "world":
        parts.append("请输出世界观设定。")
    elif step == "chars":
        lead = max(1, int(char_count or 1))
        side = max(0, int(side_count or 0))
        if side:
            parts.append(f"请输出 {lead} 个主角和 {side} 个配角（共 {lead + side} 个，JSON 数组）。")
        else:
            parts.append(f"请输出 {lead} 个角色（JSON 数组）。")
    elif step == "outline":
        parts.append(f"请输出 {chapter_count} 章大纲（JSON 对象）。")
    return "\n\n".join(parts)


# ---- 分部大纲与拆章（3.9.13） ----


def build_outline_text_user(setup: dict[str, Any], *, part_no: int = 1,
                            part_title: str = "", target_chars: int = 8000,
                            extra_note: str = "",
                            prev_parts: list[dict[str, Any]] | None = None,
                            output_mode_value: str = "json") -> str:
    """整篇大纲的用户消息：**不要 JSON**，只要一段能读的大纲文字（按部）。

    `prev_parts` 是已生成好的前几部大纲（一次生成全部时保证七部互相衔接，
    不会各写各的）——每部只带开头一段，防止撑爆窗口。
    """
    parts = [stage_protocol("outline_text", output_mode_value),
             f"【创作提示词】\n{idea_text(setup)}"]
    if str(setup.get("worldview") or "").strip():
        parts.append(f"【世界观摘要】\n{_worldview_text(setup, limit=1200)}")
    chars_text = _characters_text(setup, limit=800, count=CHARS_SLOT_COUNT)
    has_chars = any(isinstance(r, dict) and str(r.get("name") or "").strip()
                    for r in (setup.get("characters") or []))
    if has_chars:
        parts.append(f"【角色】\n{chars_text}")
    title = str(setup.get("title") or "").strip()
    if title:
        parts.append(f"【书名】{title}")
    blurb = str(setup.get("blurb") or "").strip()
    if blurb:
        parts.append(f"【简介】\n{blurb[:600]}")
    if prev_parts:
        blocks = []
        for row in prev_parts:
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            label = f"第 {row.get('no')} 部" + (f" · {row.get('title')}" if str(row.get("title") or "").strip() else "")
            blocks.append(f"《{label}》（节选）\n{text[:1200]}")
        if blocks:
            parts.append("【已定稿的前几部大纲】（本部必须与它们衔接：接住前一部留下的线，不重复、不矛盾）\n"
                         + "\n\n".join(blocks))
    label = f"第 {part_no} 部" + (f" · {part_title}" if part_title else "")
    parts.append(f"【本部】{label}；目标篇幅约 {int(target_chars or 8000)} 字")
    if extra_note.strip():
        parts.append(f"【补充要求】\n{extra_note.strip()[:500]}")
    style_block = _constraints_block(setup)
    if style_block:
        parts.append(style_block.rstrip())
    parts.append("请直接输出这一部的大纲正文（不要 JSON、不要逐章罗列）。")
    return "\n\n".join(parts)


def build_outline_review_user(setup: dict[str, Any], *, part_no: int, part_title: str,
                              part_text: str, prev_parts: list[dict[str, Any]] | None = None,
                              next_parts: list[dict[str, Any]] | None = None,
                              extra_note: str = "", output_mode_value: str = "json") -> str:
    """多部复核梳理的用户消息：本部全文 + 其余各部节选，让模型对照全书修订本部。

    与 build_outline_text_user 的区别：本部的大纲文字是**输入**而不是让模型现写；
    前后各部都要带（复核要往后看，只带前几部会漏掉「后面的部已经用过这个反转」）。
    """
    parts = [stage_protocol("outline_review", output_mode_value),
             f"【创作提示词】\n{idea_text(setup)}"]
    if str(setup.get("worldview") or "").strip():
        parts.append(f"【世界观摘要】\n{_worldview_text(setup, limit=1200)}")
    chars_text = _characters_text(setup, limit=800, count=CHARS_SLOT_COUNT)
    has_chars = any(isinstance(r, dict) and str(r.get("name") or "").strip()
                    for r in (setup.get("characters") or []))
    if has_chars:
        parts.append(f"【角色】\n{chars_text}")
    title = str(setup.get("title") or "").strip()
    if title:
        parts.append(f"【书名】{title}")
    blurb = str(setup.get("blurb") or "").strip()
    if blurb:
        parts.append(f"【简介】\n{blurb[:600]}")
    others: list[str] = []
    for row in (prev_parts or []):
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        label = f"第 {row.get('no')} 部" + (f" · {row.get('title')}" if str(row.get("title") or "").strip() else "")
        others.append(f"《{label}》（在它之前，节选）\n{text[:1200]}")
    for row in (next_parts or []):
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        label = f"第 {row.get('no')} 部" + (f" · {row.get('title')}" if str(row.get("title") or "").strip() else "")
        others.append(f"《{label}》（在它之后，节选）\n{text[:600]}")
    if others:
        parts.append("【整本书的其余各部】（本部要和它们互相衔接：不重复、不矛盾、接得上）\n"
                     + "\n\n".join(others))
    label = f"第 {part_no} 部" + (f" · {part_title}" if part_title else "")
    parts.append(f"【本部（要复核梳理的就是这一部）】{label}\n{str(part_text or '').strip()}")
    if extra_note.strip():
        parts.append(f"【补充要求】\n{extra_note.strip()[:500]}")
    style_block = _constraints_block(setup)
    if style_block:
        parts.append(style_block.rstrip())
    parts.append("请直接输出修订后的本部大纲正文（不要 JSON、不要逐章罗列、不要复核说明）。")
    return "\n\n".join(parts)


def parse_outline_text(text: str, *, limit: int = PART_TEXT_MAX) -> str:
    """整篇大纲：只去围栏，不解析结构；空结果和超长结果交给用户处理。"""
    return _parse_text(text, what="大纲", limit=limit, strip_fence=True)


def build_split_user(setup: dict[str, Any], *, part_text: str, want_count: int,
                     from_no: int = 1, part_label: str = "",
                     extra_note: str = "",
                     prev_chapters: list[dict[str, Any]] | None = None,
                     output_mode_value: str = "json") -> str:
    """拆章的用户消息：给定这一部的大纲正文，按顺序拆出 want_count 章。

    `prev_chapters` 是已拆好的章节（接续拆分时必有）：不带这个，第 31 章起
    拆的章会当故事从头讲——这正是「前 30 章和 31 章不衔接」的根因之一。
    """
    parts = [f"【大纲正文】{('· ' + part_label) if part_label else ''}\n{part_text}"]
    if str(setup.get("worldview") or "").strip():
        parts.append(f"【世界观摘要】\n{_worldview_text(setup, limit=600)}")
    if prev_chapters:
        lines = []
        for row in prev_chapters:
            line = f"第{row.get('no')}章 {row.get('title') or ''}"
            beat = str(row.get("beat") or "").strip()
            hook = str(row.get("hook") or "").strip()
            if beat:
                line += f"：{beat}"
            if hook:
                line += f"（章末：{hook}）"
            lines.append(line)
        prev_text = "\n".join(lines)
        if len(prev_text) > SPLIT_PREV_MAX_CHARS:
            prev_text = "……\n" + prev_text[-SPLIT_PREV_MAX_CHARS:]
        parts.append(f"【已拆好的前文】（新章必须从这里继续：接住上一章的钩子，不重讲、不跳跃）\n{prev_text}")
    parts.append(f"【本次任务】从第 {int(from_no)} 章开始，拆 {int(want_count)} 章")
    parts.append(stage_protocol("split", output_mode_value))
    if extra_note.strip():
        parts.append(f"【补充要求】\n{extra_note.strip()[:300]}")
    if output_mode(output_mode_value) == "freeform":
        parts.append("请直接自由写出本批章节安排，不要 JSON；每章单独起行、以「第N章 标题」开头，"
                     "生成后需要确认登记才会进章节目录。")
    else:
        parts.append('请输出 JSON：{"chapters":[{"no":%d,"title":"第%d章 …","beat":"…","hook":"…"}]}'
                     % (int(from_no), int(from_no)))
    return "\n\n".join(parts)


def parse_split(text: str, *, from_no: int = 1, want_count: int = 1,
                require_count: bool = False) -> list[dict[str, Any]]:
    """拆章：按 from_no 连续编号；默认允许干净但数量不足的批次。"""
    return _parse_chapter_rows(text, wanted=want_count, from_no=from_no,
                               what="拆章", require_count=require_count)


# 自由文本拆章的候选预填只认「第N章」开头的行：其余写法不猜，留给用户手补。
_SPLIT_HEADING_RE = re.compile(r"^第[0-9零一二三四五六七八九十百千两]+章")
SPLIT_FREEFORM_PREFILL_MAX = 60


def prefill_split_candidates(text: str) -> list[dict[str, str]]:
    """从自由文本里按「第N章」行预填候选章节：标题取行内文字，简述/钩子一律留空。

    候选只是编辑器预填，落库必须经用户确认；不识别任何行时返回空表。
    """
    rows: list[dict[str, str]] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        match = _SPLIT_HEADING_RE.match(stripped)
        if not match:
            continue
        # 整行只有「第三章」时标题兜底用整行，避免好行被丢掉。
        title = stripped[match.end():].strip() or stripped
        rows.append({"title": title[:200], "beat": "", "hook": ""})
        if len(rows) >= SPLIT_FREEFORM_PREFILL_MAX:
            break
    return rows


def confirm_freeform_split(text: str, *, from_no: int, want_count: int) -> ContentIssue:
    """自由文本拆章不直接收尾：原文转待处理，等用户整理成章节行并登记。

    与拆章抢救同一原则：预填的候选行没有「接受」动作，必须走「补充并继续」确认。
    """
    raw = str(text or "")
    candidates = prefill_split_candidates(raw)
    target = max(1, int(want_count or 1))
    if candidates:
        message = f"已按「第N章」行预填 {len(candidates)} 章候选；请核对标题、补齐缺的章后再登记"
        missing = [f"已预填 {len(candidates)} 章，本批目标 {target} 章；缺的章手动补上"]
    else:
        message = "自由文本拆章结果待确认：请按原文整理出章节标题并登记，或跳过本批"
        missing = ["没有识别到「第N章」开头的行；请对照原文手动整理章节"]
    return ContentIssue(message, category="freeform_confirm", raw=raw,
                        partial=candidates or None, missing=missing,
                        counts={"actual": len(candidates), "target": target})


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


# ---- 章末状态（跨章连贯性修复计划 §三.B） ----

def state_content_excerpt(content: str) -> str:
    """状态提取的正文输入：超长按「头 + 尾」截取并明确标注省略。"""
    text = str(content or "").strip()
    if len(text) <= STATE_CONTENT_MAX_CHARS:
        return text
    return (text[:STATE_CONTENT_HEAD_CHARS]
            + "\n……（中间部分省略）……\n"
            + text[-STATE_CONTENT_TAIL_CHARS:])


def build_state_user(*, chapter_no: int, content: str, names: list[str] | None = None,
                     part_note: str = "", output_mode_value: str = "json") -> str:
    """章末状态提取的用户消息：本章全文（超长截取）+ 角色名单（帮助统一人名）。"""
    body = state_content_excerpt(content)
    parts = [f"【章节】第 {int(chapter_no or 0)} 章"
             + (f"（{part_note}）" if part_note else ""), f"【正文】\n{body}"]
    parts.insert(0, stage_protocol("state", output_mode_value))
    clean_names = [str(n or "").strip() for n in (names or []) if str(n or "").strip()]
    if clean_names:
        parts.append("【本作角色名单】（人名以这份名单为准；正文里新出现的人物如实记录）\n"
                     + "、".join(clean_names[:60]))
    if output_mode(output_mode_value) == "freeform":
        parts.append("请直接用自然文本写出本章结束时的状态，只记录正文明确写出的事实。")
    else:
        parts.append(STEP_SYSTEM["state"])
    return "\n\n".join(parts)


def parse_chapter_state(text: str) -> dict[str, Any]:
    """至少要有一条落盘后仍有效的事实；正常结果保留原来的对象形状。"""
    raw = str(text or "")
    missing_fact = ["至少补一条章末事实"]
    payload = _extract_step_json(raw, what="章末状态", missing=missing_fact,
                                 empty_partial={"events": []}, target=1)
    if not isinstance(payload, dict):
        raise ContentIssue("章末状态必须是 JSON 对象", raw=raw, category="wrong_shape",
                           partial=payload, missing=missing_fact, counts={"target": 1})

    # 存储层为旧数据允许 str() 转换；这里先剔除错误类型，不能把对象当成事实。
    facts: dict[str, list[Any]] = {}
    malformed = False
    for key in CHAPTER_STATE_KEYS:
        if key not in payload:
            continue
        entries = payload[key]
        if not isinstance(entries, list):
            malformed = True
            continue
        facts[key] = [item for item in entries if _valid_state_entry(key, item)]
        malformed |= len(facts[key]) != len(entries)
    try:
        normalized = normalize_chapter_state(facts)
    except WriterStoreError as exc:
        raise ContentIssue("章末状态没有任何可用事实，请补充、重试或跳过",
                           raw=raw, category="empty", partial=payload, missing=missing_fact,
                           counts={"actual": 0, "target": 1}) from exc
    if malformed:
        raise ContentIssue("章末状态有格式不正确的事实，请编辑后继续",
                           raw=raw, category="incomplete", partial=payload, accepted=normalized,
                           missing=["修正章末事实的字段类型"],
                           counts={"actual": sum(map(len, normalized.values())), "target": 1})
    return payload


def _valid_state_entry(key: str, item: Any) -> bool:
    if key in ("events", "open_threads", "next_hooks"):
        return isinstance(item, str)
    if not isinstance(item, dict):
        return False
    text_fields, array_fields = {
        "characters": (("name", "presence", "location", "condition"), ()),
        "relations": (("change",), ("between",)),
        "knowledge": (("name",), ("known", "unknown")),
    }[key]
    return _text_fields_valid(item, text_fields) and all(
        field not in item or (isinstance(item[field], list)
                              and all(isinstance(value, str) for value in item[field]))
        for field in array_fields
    )


def chapter_states_text(rows: list[dict[str, Any]], *,
                        per_chapter_limit: int = CHAPTER_STATE_TEXT_CHARS) -> str:
    """前序章末状态 → 提示词段落：每章一段、逐章限长，整体预算由调用方裁剪。"""
    lines: list[str] = []
    for row in rows:
        no = row.get("no")
        state = row.get("state") if isinstance(row.get("state"), dict) else {}
        bits: list[str] = []
        for character in state.get("characters") or []:
            if not isinstance(character, dict):
                continue
            bit = f"{character.get('name')}（{character.get('presence') or '出场'}"
            location = str(character.get("location") or "").strip()
            condition = str(character.get("condition") or "").strip()
            if location:
                bit += f"，在{location}"
            if condition:
                bit += f"，{condition}"
            bits.append(bit + "）")
        for relation in state.get("relations") or []:
            if isinstance(relation, dict):
                between = "、".join(str(b) for b in (relation.get("between") or []))
                bits.append(f"关系：{between}——{relation.get('change')}")
        for item in state.get("knowledge") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            for known in item.get("known") or []:
                bits.append(f"{name}已知：{known}")
            for unknown in item.get("unknown") or []:
                bits.append(f"{name}不知道：{unknown}")
        for event in state.get("events") or []:
            bits.append(f"事件：{event}")
        for thread in state.get("open_threads") or []:
            bits.append(f"未决：{thread}")
        for hook in state.get("next_hooks") or []:
            bits.append(f"下章：{hook}")
        text = f"第{no}章末：" + "；".join(bit for bit in bits if bit)
        if len(text) > per_chapter_limit:
            text = text[:per_chapter_limit] + "…"
        lines.append(text)
    return "\n".join(lines)


def fit_chapter_states(rows: list[dict[str, Any]], *,
                       limit: int = CHAPTER_STATES_SLOT_CHARS) -> list[dict[str, Any]]:
    """按总预算裁剪前序状态：超预算丢最旧的章（最近的续写最要紧）。"""
    kept: list[dict[str, Any]] = []
    used = 0
    for row in reversed(rows):
        text = chapter_states_text([row])
        cost = len(text) + (1 if kept else 0)
        if used + cost > limit and kept:
            break
        kept.append(row)
        used += cost
    kept.reverse()
    return kept


def build_draft_context(setup: dict[str, Any], chapter: dict[str, Any], *,
                        mode: str, seg_points: list[str] | None = None,
                        seg_index: int = 0, prev_tail: str = "",
                        draft_tail: str = "", extra_note: str = "",
                        previous_chapter: dict[str, Any] | None = None,
                        chapter_states: list[dict[str, Any]] | None = None,
                        output_mode_value: str = "json", step: str = "draft") -> dict[str, Any]:
    """组装正文或段落要点的用户消息与槽位清单（预检与正式提交共用）。

    `step`：draft 写正文 / segs 先列要点。`mode`：whole 整章 / segment 分段 / continue 续写。
    `chapter_states`：前序章末状态（`store.chapter_context_states` 的输出，
    只含有效记录）；原文摘录仍保留作措辞衔接，长程事实由状态承担。
    返回 `{content, slots}`；slots 描述每个槽是否被带入及省略原因。
    """
    slots: list[dict[str, Any]] = []
    beat = str(chapter.get("beat") or "").strip()
    hook = str(chapter.get("hook") or "").strip()
    outline_text = beat or str(chapter.get("outline_beat") or "").strip()
    worldview = _worldview_text(setup)
    previous = previous_chapter or {}
    excerpt = str(previous.get("excerpt") or prev_tail).strip()
    if mode == "segment":
        seg_point_texts = [str(p).strip()[:SEG_POINT_CHARS]
                           for p in (seg_points or []) if str(p).strip()]
    else:
        seg_point_texts = []
    # 聚焦人物的点名依据：本章梗概、章末钩子、上一章原文节选、段落要点与补充要求。
    relevance_text = "\n".join(
        bit for bit in (outline_text, hook, excerpt, str(extra_note or ""), *seg_point_texts) if bit
    )
    has_chars = [r for r in (setup.get("characters") or []) if isinstance(r, dict)]
    chars_text = ""
    chars_excluded: list[str] = []
    if has_chars:
        must_include, others = select_context_characters(
            list(setup.get("characters") or []), relevance_text=relevance_text)
        chars_text, chars_excluded = _fit_characters_text(
            must_include, others, limit=DRAFT_CHARS_SLOT_CHARS)
        if not chars_text:
            chars_text = "（角色未定）"

    parts: list[str] = [stage_protocol(step, output_mode_value), f"【创作提示词】\n{idea_text(setup)}"]
    slots.append({"key": "idea", "label": "创作提示词", "included": True,
                  "chars": len(parts[0])})
    summary = setup.get("continuation_summary")
    if isinstance(summary, dict):
        labels = (("characters_state", "人物现状"), ("relations_state", "关系现状"),
                  ("plot_state", "剧情现状"), ("next_outline", "后续大纲"))
        summary_text = "\n".join(f"{label}：{summary.get(key, '')}" for key, label in labels)
        parts.append("【人工确认的续写现状与后续大纲】\n" + summary_text
                     + "\n以上为导入时人工确认的接续基线。已生成的新章事实优先于更早的状态，"
                       "不得把人物、关系或剧情无故重置回原作早期，也不得把未确认推测写成既定事实。")
        slots.append({"key": "continuation_summary", "label": "人工确认的续写现状", "included": True,
                      "chars": len(summary_text)})
    if str(setup.get("worldview") or "").strip():
        parts.append(f"【世界观摘要】\n{worldview}")
        slots.append({"key": "worldview", "label": "世界观摘要", "included": True,
                      "chars": len(worldview)})
    else:
        slots.append({"key": "worldview", "label": "世界观摘要", "included": False,
                      "reason": "设定里还没有世界观"})
    if has_chars:
        parts.append(f"【出场角色】\n{chars_text}")
        slot: dict[str, Any] = {"key": "characters", "label": "出场角色", "included": True,
                                "chars": len(chars_text)}
        if chars_excluded:
            slot["excluded_names"] = chars_excluded
            slot["excluded_reason"] = "人物预算不足或未在本章点名，未进上下文"
        slots.append(slot)
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
    previous = previous_chapter or {}
    state_rows = fit_chapter_states(list(chapter_states or []))
    if state_rows:
        states_text = chapter_states_text(state_rows)
        parts.append("【前情状态（截至上一章结束的已确认事实）】\n" + states_text
                     + "\n【续写要求】后续章节必须延续以上事实：人物位置、身体与身份状态、"
                       "关系变化不得无故回退；标注「不知道」的事项不得写成角色已知道；"
                       "未决线索不得无故消失。与角色卡冲突时以已确认设定与正文事实为准。")
        slots.append({"key": "chapter_states", "label": "前情状态", "included": True,
                      "chars": len(states_text),
                      "chapters": [int(row.get("no") or 0) for row in state_rows]})
    else:
        slots.append({"key": "chapter_states", "label": "前情状态", "included": False,
                      "reason": "还没有有效的章末状态记录（首章或尚未提取）"})
    if excerpt:
        parts.append("【上一章原文节选（首尾截取，非总结）】\n" + excerpt
                     + "\n请承接上一章结束时的人物位置、时间、情绪与未决事件，不要重演已发生情节。")
        slots.append({"key": "prev_tail", "label": "上一章原文节选", "included": True,
                      "chars": len(excerpt), **previous})
    else:
        reasons = {
            "first_chapter": "首章没有上一章", "missing_chapter": "上一章不存在",
            "no_current_version": "上一章尚未选择当前正文版本",
            "empty_content": "上一章当前正文为空", "unavailable": "上一章当前正文不可读取",
        }
        slots.append({"key": "prev_tail", "label": "上一章原文节选", "included": False,
                      **previous, "reason": reasons.get(str(previous.get("status") or ""),
                                                       "未提供上一章当前正文")})
    if mode == "segment":
        points = seg_point_texts
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
    if step == "segs":
        if output_mode(output_mode_value) == "freeform":
            parts.append("请按剧情顺序列出本章段落要点，每条一句；不要写正文或输出 JSON。")
        else:
            parts.append(STEP_SYSTEM["segs"] + '每项用字符串，例如：["第一段要点", "第二段要点"]。不要写正文。')
    elif mode == "whole":
        parts.append(f"请写出第 {chapter.get('no')} 章完整正文（2000—4000字）。")
    elif mode == "continue":
        parts.append(f"请从当前稿结尾处继续写第 {chapter.get('no')} 章正文，紧接上文、不重复已写内容。")
    else:
        parts.append(f"现在写第 {chapter.get('no')} 章第 {seg_index + 1} 段（只输出本段正文，1200—1800字）。")
    return {"content": "\n\n".join(parts), "slots": slots}


def budget_report(*, content: str, slots: list[dict[str, Any]],
                  context_window: int, extra_items: dict[str, int] | None = None,
                  system: str = "") -> dict[str, Any]:
    """输入预算：included / omitted / estimated_tokens / context_known / fits。

    已知容量不足在排队前阻止；容量未知且输入很大时要求用户确认（不伪造模型上限）。
    """
    omitted = [
        {"key": slot.get("key"), "label": slot.get("label"),
         "reason": str(slot.get("reason") or "")}
        for slot in slots if not slot.get("included")
    ]
    chars = len(content) + len(system)
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
        "system_chars": len(system),
    }


# ---- 严格 JSON 与文本解析 ----

_FENCE_PATTERN = re.compile(
    r"^\s*```[a-zA-Z0-9_-]*\s*\n(.*?)\n?```\s*$", re.DOTALL,
)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("JSON 键重复")
        obj[key] = value
    return obj


def _finite_json_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON 数值不是有限值")
    return number


def _ensure_json_unicode(value: Any) -> None:
    """避免转义的孤立代理字符通过解析后才在 UTF-8 落盘时出错。"""
    if isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, list):
        for item in value:
            _ensure_json_unicode(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            key.encode("utf-8")
            _ensure_json_unicode(item)


def extract_json_payload(text: str) -> Any:
    """只接受纯 JSON 或一个代码围栏，不从解释文字或多个围栏中猜。"""
    raw = str(text or "")
    candidate = raw.strip()
    fence = _FENCE_PATTERN.match(candidate)
    if fence:
        candidate = fence.group(1).strip()
    if not candidate:
        raise ContentIssue("模型没有返回内容", raw=raw, category="empty")
    try:
        payload = json.loads(candidate, object_pairs_hook=_unique_json_object,
                             parse_float=_finite_json_number, parse_constant=_finite_json_number)
        _ensure_json_unicode(payload)
        return payload
    except (ValueError, RecursionError) as exc:
        raise ContentIssue("模型输出不是合法 JSON（只接受纯 JSON 或单个代码围栏）；请编辑或重试",
                           raw=raw, category="unrecognized_format") from exc


# ---- 非法 JSON 的有界抢救（拆章/逐章大纲）：只修语法，不猜内容 ----
#
# 拆章是全流水线单次输出最大的 JSON（一批 20 章、每章带长梗概），最容易因
# 输出被截断、尾逗号、字符串里夹裸换行或表格外带解释文字而「整批作废」。
# 这里的修复只处理能唯一确定的语法问题：修好走原有校验，修不好才交人工。

_FENCE_ANY_PATTERN = re.compile(r"```[a-zA-Z0-9_-]*[ \t]*\r?\n(.*?)(?:```|\Z)", re.DOTALL)
_JSON_CLOSER = {"{": "}", "[": "]"}
_JSON_CONTROL_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}
_SALVAGE_ROW_MAX = 60


def _load_strict_json(text: str) -> Any:
    """与 extract_json_payload 同一套钩子的 json.loads；失败抛 ValueError。"""
    payload = json.loads(text, object_pairs_hook=_unique_json_object,
                         parse_float=_finite_json_number, parse_constant=_finite_json_number)
    _ensure_json_unicode(payload)
    return payload


def _json_candidates(raw: str) -> list[str]:
    """严格候选失败后的抢救候选：全文修复、围栏内容（含写到一半没闭合的围栏）、
    去掉前缀解释文字的括号区；按顺序尝试，不从多处来源里挑。"""
    candidates: list[str] = []
    stripped = raw.strip()
    if stripped:
        candidates.append(stripped)
    for match in _FENCE_ANY_PATTERN.finditer(raw):
        body = match.group(1).strip()
        if body:
            candidates.append(body)
    if stripped:
        for opener in ("{", "["):
            index = stripped.find(opener)
            if index > 0:
                candidates.append(stripped[index:])
    return candidates


def _repair_json_text(text: str) -> str | None:
    """单遍扫描修复：字符串内裸控制符转义、闭括号前尾逗号去除、截断收口补齐括号。

    顶层结构完整就按完整返回；停在半截就回到最后一个闭合处收口。括号错配
    这类没法唯一确定的损坏返回 None，不硬猜。
    """
    out: list[str] = []
    stack: list[str] = []
    checkpoints: list[tuple[int, list[str]]] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            elif char < " ":
                escape = _JSON_CONTROL_ESCAPES.get(char)
                out.append(escape if escape is not None else f"\\u{ord(char):04x}")
                continue
            out.append(char)
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack or _JSON_CLOSER[stack[-1]] != char:
                return None
            while out and out[-1] in " \t\r\n":
                out.pop()
            if out and out[-1] == ",":
                out.pop()
            stack.pop()
            out.append(char)
            if not stack:
                return "".join(out)  # 顶层结构闭合即止，不收后面的解释文字
            checkpoints.append((len(out), list(stack)))
            continue
        out.append(char)
    if not in_string and not stack:
        return "".join(out)
    if not checkpoints:
        return None
    cut, open_stack = checkpoints[-1]
    return "".join(out[:cut]) + "".join(_JSON_CLOSER[bracket] for bracket in reversed(open_stack))


def _rescued_json_payload(raw: str) -> Any | None:
    """严格解析失败后的抢救入口：逐候选先直接读、再修复读；全失败返回 None。"""
    for candidate in _json_candidates(raw):
        for text in (candidate, _repair_json_text(candidate)):
            if not text:
                continue
            try:
                return _load_strict_json(text)
            except (ValueError, RecursionError):
                continue
    return None


def _salvage_chapter_rows(raw: str) -> list[dict[str, Any]]:
    """整段修不好时，按括号配平打捞完整的章对象；只保留带标题/梗概/钩子的行。"""
    rows: list[dict[str, Any]] = []
    starts: list[int] = []
    in_string = False
    escaped = False
    for index, char in enumerate(raw):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            starts.append(index)
        elif char == "}" and starts:
            start = starts.pop()
            piece = raw[start:index + 1]
            try:
                payload = _load_strict_json(piece)
            except (ValueError, RecursionError):
                repaired = _repair_json_text(piece)
                if repaired is None:
                    continue
                try:
                    payload = _load_strict_json(repaired)
                except (ValueError, RecursionError):
                    continue
            if isinstance(payload, dict) and any(
                isinstance(payload.get(key), str) and payload[key].strip()
                for key in ("title", "beat", "hook")
            ):
                rows.append(payload)
                if len(rows) >= _SALVAGE_ROW_MAX:
                    break
    return rows


def _extract_step_json(raw: str, *, what: str, missing: list[str],
                       empty_partial: Any, target: int) -> Any:
    """补上阶段信息；只有确认空输出才给可编辑的空模板和实际数量 0。"""
    try:
        return extract_json_payload(raw)
    except ContentIssue as exc:
        if exc.category == "empty":
            raise ContentIssue(f"模型没有返回{what}", raw=raw, category="empty",
                               partial=empty_partial, missing=missing,
                               counts={"actual": 0, "target": target}) from exc
        raise ContentIssue(f"{what}不是合法 JSON；请编辑为有效 JSON 或重试", raw=raw,
                           category=exc.category, missing=missing, counts={"target": target}) from exc


def _parse_text(text: str, *, what: str, limit: int, strip_fence: bool = False,
                truncated: bool = False) -> str:
    raw = str(text or "")
    body = raw.strip()
    if strip_fence and (fence := _FENCE_PATTERN.match(body)):
        body = fence.group(1).strip()
    if not body:
        raise ContentIssue(f"{what}结果为空", raw=raw, category="empty", partial="",
                           missing=[f"填写{what}"], counts={"actual": 0})
    if len(body) > limit:
        detail = "已被截断，且超过" if truncated else "超过"
        missing = [f"删减到 {limit} 字符以内"]
        if truncated:
            missing.append("补完正文结尾")
        raise ContentIssue(f"{what}{detail} {limit} 字符上限，请编辑后继续",
                           raw=raw, category="too_long", partial=body, missing=missing,
                           counts={"actual": len(body), "target": limit})
    if truncated:
        raise ContentIssue("正文因达到输出长度上限被截断；请编辑残稿、续写或重试",
                           raw=raw, category="truncated", partial=body,
                           missing=["补完正文结尾"], counts={"chars": len(body)})
    return body


def parse_freeform(text: str, *, limit: int = PART_TEXT_MAX) -> str:
    return _parse_text(text, what="自由文本", limit=limit)


def _json_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _text_fields_valid(obj: dict[str, Any], fields: Iterable[str]) -> bool:
    return all(field not in obj or isinstance(obj[field], str) for field in fields)


def parse_genre(text: str) -> dict[str, Any]:
    raw = str(text or "")
    payload = _extract_step_json(raw, what="题材结果", missing=["cat"],
                                 empty_partial={"cat": ""}, target=1)
    if not isinstance(payload, dict):
        raise ContentIssue("题材结果必须是 JSON 对象", raw=raw, category="wrong_shape",
                           partial=payload, missing=["cat"], counts={"target": 1})
    # 第一个明确的大类字段优先；格式错误不会用后面的别名掩盖。
    cat_value = next((payload[key] for key in ("cat", "category", "大类") if key in payload), "")
    cat = _json_text(cat_value)
    if not cat:
        raise ContentIssue("题材缺少有效的大类：请填写 cat（大类）",
                           raw=raw, category="missing_field", partial={"cat": "", **payload},
                           missing=["cat"], counts={"actual": 0, "target": 1})
    sub_key = "subCat" if "subCat" in payload else "sub_cat"
    invalid = [field for field in ("channel", sub_key, "tone", "hook")
               if not _text_fields_valid(payload, (field,))]
    tags_raw = payload.get("tags", [])
    if not isinstance(tags_raw, list) or not all(isinstance(item, str) for item in tags_raw):
        invalid.append("tags")
    if invalid:
        raise ContentIssue("题材有格式不正确的字段，请编辑后继续", raw=raw,
                           category="wrong_shape", partial=payload,
                           missing=[f"修正 {field} 的类型" for field in invalid])
    channel = _json_text(payload.get("channel")).lower()
    tags = list(dict.fromkeys(value for item in tags_raw if (value := item.strip())))[:5]
    return {
        "channel": channel if channel in ("male", "female") else "",
        "cat": cat[:40],
        "sub_cat": _json_text(payload.get(sub_key))[:40],
        "tone": _json_text(payload.get("tone"))[:40],
        "hook": _json_text(payload.get("hook"))[:200],
        "tags": tags,
    }


def _payload_list(raw: str, *, keys: tuple[str, ...], what: str, target: int,
                  rescue: bool = False) -> list[Any]:
    """只认白名单包装；前面的键明确存在时，不回退到后面的键。

    rescue=True 时严格解析失败先做有界语法抢救（只修格式不猜内容），
    抢救成功的结果走同一套白名单解包与校验。
    """
    missing = [f"填写 {keys[0]} 数组"]
    try:
        payload = _extract_step_json(raw, what=what, missing=missing, empty_partial=[], target=target)
    except ContentIssue as exc:
        if not rescue or exc.category != "unrecognized_format":
            raise
        payload = _rescued_json_payload(raw)
        if payload is None:
            raise
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        key = next((key for key in keys if key in payload), None)
        if key is not None and isinstance(payload[key], list):
            return payload[key]
    raise ContentIssue(f"{what}必须是 JSON 数组或使用 {' / '.join(keys)} 包装的数组",
                       raw=raw, category="wrong_shape", partial=payload, missing=missing,
                       counts={"target": target})


def _finish_rows(raw: str, partial: list[Any], rows: list[Any], *, target: int,
                 min_count: int, max_count: int, what: str, unit: str, row_label: str,
                 invalid_rows: int, missing_field: tuple[str, str, int] | None = None) -> list[Any]:
    actual = len(rows)
    accepted = rows[:max_count]
    missing: list[str] = []
    counts = {"actual": actual, "target": target}
    if actual < min_count:
        missing.append(f"还缺 {min_count - actual} {unit}{row_label}")
    if missing_field is not None:
        key, label, count = missing_field
        if count:
            missing.append(f"{count} {unit}缺少{label}")
            counts[f"missing_{key}"] = count
    if invalid_rows:
        missing.append(f"{invalid_rows} {unit}格式不正确，请修正或删除")
        counts["invalid_rows"] = invalid_rows
    if missing:
        category = ("empty" if not partial else "insufficient" if actual < min_count else "incomplete")
        reason = "只有" if actual < min_count else "有"
        raise ContentIssue(f"{what}{reason} {actual} {unit}有效内容；{'、'.join(missing)}",
                           raw=raw, category=category, partial=partial,
                           accepted=accepted or None, missing=missing, counts=counts)
    return accepted


def _parse_str_list(text: str, *, min_count: int, max_count: int, max_chars: int, what: str,
                    wrapper_keys: tuple[str, ...]) -> list[str]:
    raw = str(text or "")
    partial = _payload_list(raw, keys=wrapper_keys, what=what, target=min_count)
    rows = [value[:max_chars] for item in partial if (value := _json_text(item))]
    return _finish_rows(raw, partial, rows, target=min_count, min_count=min_count,
                        max_count=max_count, what=what, unit="条", row_label="内容",
                        invalid_rows=len(partial) - len(rows))


def parse_title_candidates(text: str) -> list[str]:
    return _parse_str_list(text, min_count=1, max_count=10, max_chars=120,
                           what="书名候选", wrapper_keys=("titles", "candidates"))


def parse_blurb_candidates(text: str) -> list[str]:
    return _parse_str_list(text, min_count=1, max_count=6, max_chars=2000,
                           what="简介候选", wrapper_keys=("blurbs", "descriptions", "candidates"))


def parse_world(text: str) -> str:
    return _parse_text(text, what="世界观", limit=30000, strip_fence=True)


_CHARACTER_FIELDS = {
    "role": 40, "identity": 200, "goal": 200, "traits": 200, "speech": 200,
    "arc": 200, "background": 1000, "relations": 1000,
}
_CHAPTER_FIELDS = {"beat": 2000, "hook": 500}


def _parse_named_rows(partial: list[Any], *, required: str, required_max: int,
                      fields: dict[str, int], allow_names: bool = False
                      ) -> tuple[list[dict[str, str]], int, int]:
    """保留编辑用原始行不动，单独生成可用行和真实缺口。"""
    rows: list[dict[str, str]] = []
    missing_required = invalid_rows = 0
    for item in partial:
        if allow_names and isinstance(item, str):
            item = {required: item}
        if not isinstance(item, dict):
            invalid_rows += 1
            continue
        value = item.get(required)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing_required += 1
            continue
        if not isinstance(value, str) or not _text_fields_valid(item, fields):
            invalid_rows += 1
            continue
        rows.append({required: value.strip()[:required_max], **{
            key: _json_text(item.get(key))[:limit] for key, limit in fields.items()
        }})
    return rows, missing_required, invalid_rows


def parse_characters(text: str, *, wanted: int, require_count: bool = True) -> list[dict[str, str]]:
    """返回最多 wanted 张角色卡；人工编辑可放宽数量，但不能提交无效行。"""
    raw = str(text or "")
    partial = _payload_list(raw, keys=("characters", "角色"), what="角色结果", target=wanted)
    rows, missing_name, invalid_rows = _parse_named_rows(
        partial, required="name", required_max=60, fields=_CHARACTER_FIELDS, allow_names=True,
    )
    return _finish_rows(raw, partial, rows, target=wanted, min_count=wanted if require_count else 1,
                        max_count=wanted, what="角色结果", unit="个", row_label="有名字的角色",
                        invalid_rows=invalid_rows, missing_field=("name", "姓名（name）", missing_name))


def _parse_chapter_rows(text: str, *, wanted: int, from_no: int, what: str,
                        require_count: bool) -> list[dict[str, Any]]:
    raw = str(text or "")
    try:
        partial = _payload_list(raw, keys=("chapters", "outline"), what=what, target=wanted,
                                rescue=True)
    except ContentIssue as exc:
        # unrecognized=语法全坏；wrong_shape 且没有任何包装键=模型可能按行连发
        # 了章对象（修复扫描只会带回第一个）。两种都值得按括号配平打捞一次；
        # 包装键存在但形状不对的维持原样，交给编辑器展示。
        if exc.category == "unrecognized_format" or (
            exc.category == "wrong_shape" and isinstance(exc.partial, dict)
            and not any(key in exc.partial for key in ("chapters", "outline"))
        ):
            salvaged = _salvage_chapter_rows(raw)
            if salvaged:
                raise ContentIssue(
                    f"{what}不是合法 JSON；已从原文找回 {len(salvaged)} 章放入下方编辑器，请检查补齐后再继续",
                    raw=raw, category="unrecognized_format", partial=salvaged,
                    missing=["检查找回的章，缺标题的补上标题",
                             *( [f"还缺 {wanted - len(salvaged)} 章有效内容"]
                                if len(salvaged) < wanted else [] )],
                    counts={"actual": len(salvaged), "target": wanted}) from exc
        raise
    rows, missing_title, invalid_rows = _parse_named_rows(
        partial, required="title", required_max=200, fields=_CHAPTER_FIELDS,
    )
    # 章号是按任务起点分配的目录位置；不让异常/重复的模型章号改动已有目录。
    numbered = [{"no": int(from_no) + index, **row} for index, row in enumerate(rows)]
    return _finish_rows(raw, partial, numbered, target=wanted,
                        min_count=wanted if require_count else 1, max_count=wanted,
                        what=what, unit="章", row_label="有标题的内容", invalid_rows=invalid_rows,
                        missing_field=("title", "标题（title）", missing_title))


def parse_outline(text: str, *, wanted: int, require_count: bool = True) -> list[dict[str, Any]]:
    """旧版逐章大纲：默认要求数量达标，章号从 1 连续编号。"""
    return _parse_chapter_rows(text, wanted=wanted, from_no=1,
                               what="章节大纲", require_count=require_count)


def parse_segs(text: str, *, min_count: int = SEGS_MIN) -> list[str]:
    if not 1 <= min_count <= SEGS_MAX:
        raise PromptError(f"段落要点的最少数量必须在 1–{SEGS_MAX} 之间")
    return _parse_str_list(text, min_count=min_count, max_count=SEGS_MAX, max_chars=SEG_POINT_CHARS,
                           what="段落要点", wrapper_keys=("segments", "points"))


def parse_draft(text: str, *, finish_reason: str = "") -> str:
    truncated = str(finish_reason or "").strip().lower() in ("length", "max_tokens", "max_output_tokens")
    return _parse_text(text, what="正文", limit=CHAPTER_CONTENT_MAX_CHARS, truncated=truncated)
