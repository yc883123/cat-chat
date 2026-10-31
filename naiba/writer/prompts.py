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

# 设定自动链：题材 → 书名 → 简介 → 世界观 → 角色（第二次反馈：设定只出设定，
# 大纲与章节不在设定链里——「大纲章数」曾让章节目录在设定阶段就凭空多出 30 章）。
# 老的「大纲」步保留：只服务于老 kind `outline`（逐章大纲）的兼容路径。
SETUP_STEPS: tuple[str, ...] = ("genre", "title", "blurb", "world", "chars")
# 3.9.13 起「大纲」不再等于章节表：大纲是一整篇文字（可分部），章节由独立的 split 步拆出来。
TEXT_STEPS: tuple[str, ...] = ("outline_text", "split")
ALL_STEPS: tuple[str, ...] = (*SETUP_STEPS, *TEXT_STEPS, "segs", "draft")

STEP_LABELS = {
    "genre": "题材推断",
    "title": "书名候选",
    "blurb": "作品简介",
    "world": "世界观设定",
    "chars": "角色设定",
    "outline": "章节大纲",
    "outline_text": "全篇大纲",
    "split": "拆章",
    "segs": "段落要点",
    "draft": "正文",
}

CHARS_MIN, CHARS_MAX = 1, 10
SIDE_COUNT_MAX = 99
# 旧的「大纲=章节表」上限，只服务于老 kind（outline）的兼容路径。
OUTLINE_MIN, OUTLINE_MAX = 5, 30
SEGS_MIN, SEGS_MAX = 2, 4
# 分部大纲：部数 1–20，每部正文上限 20 万字符（"几万字"够用，再大必然超窗口）。
PARTS_MIN, PARTS_MAX = 1, 20
PART_TEXT_MAX = 200_000
# 拆章：一部最多 9999 章；单批最多 999 章（第二次反馈：200 不够用）。
SPLIT_MIN, SPLIT_MAX = 1, 9999
SPLIT_BATCH_MAX = 999
# 接续拆分时带上的「已拆章节」衔接上下文：最多最近 8 章、总量上限（防撑窗口）。
SPLIT_PREV_MAX_CHAPTERS = 8
SPLIT_PREV_MAX_CHARS = 2400
# 单章字数限制的合法取值（0 = 不限）。
CHAPTER_CHARS_CHOICES = (0, 2000, 3000, 4000, 6000)
# 章节结尾策略。
ENDING_CHOICES = ("suspense", "natural", "hook")
ENDING_LABELS = {"suspense": "留悬念", "natural": "自然收", "hook": "留钩子+预告"}

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
    # 主角先带（占比有限），配角按数组顺序补足。
    rows.sort(key=lambda row: 0 if str(row.get("role") or "") == "lead" else 1)
    lines = []
    for row in rows[:count]:
        role = str(row.get("role") or "").strip()
        role_text = {"lead": "主角", "side": "配角"}.get(role, role)
        identity = str(row.get("identity") or "").strip()
        goal = str(row.get("goal") or "").strip()
        age = str(row.get("age") or "").strip()
        origin = str(row.get("origin") or "").strip()
        personality = str(row.get("personality") or row.get("traits") or "").strip()
        relations = str(row.get("relations") or "").strip()
        voice = str(row.get("voice") or row.get("speech") or "").strip()
        line = f"{row['name']}" + (f"（{role_text}）" if role_text else "")
        detail = "；".join(bit for bit in (
            identity, f"{age} 岁" if age else "", origin, personality,
            f"关系：{relations}" if relations else "", f"说话：{voice}" if voice else "", goal,
        ) if bit)
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
        "按用户消息要求的主角与配角数量给足，一个都不能少；配角也要有独立的动机与"
        "生活，不做工具人。角色之间要有利益冲突或情感张力，反派动机必须合理。所有值纯中文。"
    ),
    "outline": (
        "你是网文大纲策划师。基于创作提示词与世界观，输出 JSON 对象：\n"
        '{"chapters":[{"no":1,"title":"第一章 标题","beat":"本章爽点/兑付点","hook":"章末钩子"}]}\n'
        "title 必须以「第X章」开头；爽点密度每章至少一个；"
        "章末钩子八式轮换（悬念/期待/危机/反转/情感/代价/信息/挑衅），同式不得连续使用。"
    ),
    "outline_text": (
        "你是网文大纲策划师。按创作提示词、世界观与角色，写这一「部」的整篇大纲（不要分章、不要 JSON）：\n"
        "用「主线 / 本部目标 / 阶段推进（一、二、三…）/ 关键转折 / 结尾落点」组织，允许长句叙事；\n"
        "目标是让人读完就知道故事怎么走，不写逐章条目，也不要写正文对白。\n"
        "直接输出大纲正文。"
    ),
    "split": (
        "你是网文章节拆分师。把给定的大纲正文拆成章节目录，输出 JSON 对象：\n"
        '{"chapters":[{"no":1,"title":"第1章 标题","beat":"本章发生什么（80—150字）","hook":"章末钩子"}]}\n'
        "严格按大纲顺序推进，不跳过、不重复、不编造大纲之外的重大情节；章标题不超过 20 字。"
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
                    side_count: int = 0, chapter_count: int = 10,
                    extra_note: str = "") -> str:
    """设定各步的用户消息。`setup` 是选定设定版本的内容。

    `char_count` / `side_count` 只在角色步生效：主角与配角数量都要给足
    （第二次反馈：只报主角数会让 20 个配角一个都不出）。

    `outline` 步不在设定链里了，但老的 `outline` 流水线 kind 还会走这里，保留。
    """
    if step not in (*SETUP_STEPS, "outline"):
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
                            prev_parts: list[dict[str, Any]] | None = None) -> str:
    """整篇大纲的用户消息：**不要 JSON**，只要一段能读的大纲文字（按部）。

    `prev_parts` 是已生成好的前几部大纲（一次生成全部时保证七部互相衔接，
    不会各写各的）——每部只带开头一段，防止撑爆窗口。
    """
    parts = [f"【创作提示词】\n{idea_text(setup)}"]
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


def parse_outline_text(text: str, *, limit: int = PART_TEXT_MAX) -> str:
    """整篇大纲：只去围栏、不解析结构；空结果如实失败。"""
    raw = str(text or "").strip()
    if not raw:
        raise PromptError("模型没有返回大纲内容")
    fence = _FENCE_PATTERN.match(raw)
    body = fence.group(1).strip() if fence else raw
    if not body:
        raise PromptError("模型返回的大纲为空")
    if len(body) > limit:
        raise PromptError(f"大纲超过 {limit} 字符上限；请拆成多部或精简后重试")
    return body


def build_split_user(setup: dict[str, Any], *, part_text: str, want_count: int,
                     from_no: int = 1, part_label: str = "",
                     extra_note: str = "",
                     prev_chapters: list[dict[str, Any]] | None = None) -> str:
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
    if extra_note.strip():
        parts.append(f"【补充要求】\n{extra_note.strip()[:300]}")
    parts.append('请输出 JSON：{"chapters":[{"no":%d,"title":"第%d章 …","beat":"…","hook":"…"}]}'
                 % (int(from_no), int(from_no)))
    return "\n\n".join(parts)


def parse_split(text: str, *, from_no: int = 1, want_count: int = 1) -> list[dict[str, Any]]:
    """拆章结果：允许少于 want_count（一批拆不完可以续），但一条都没有就失败。"""
    payload = extract_json_payload(text)
    rows_raw = payload.get("chapters") if isinstance(payload, dict) else payload
    if not isinstance(rows_raw, list):
        raise PromptError("拆章结果必须是含 chapters 数组的 JSON 对象")
    rows: list[dict[str, Any]] = []
    for item in rows_raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        rows.append({
            "title": title[:200],
            "beat": str(item.get("beat") or "").strip()[:2000],
            "hook": str(item.get("hook") or "").strip()[:500],
        })
    if not rows:
        raise PromptError("拆章结果没有任何章节；请重试或换模型")
    if len(rows) > want_count:
        rows = rows[:want_count]
    return [{"no": int(from_no) + index, **row} for index, row in enumerate(rows)]


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
