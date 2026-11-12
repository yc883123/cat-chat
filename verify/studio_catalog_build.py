"""Cat Chat 工作台（Studio）预设资产转换器。

把工作台旧四套资产（提示词主库 87 = 启元资源 63 + 工作区增补 24、剧本 53、游戏原画 24、
风格库 397）及仓库漫剧原文 21 份转成统一契约的只读预设 JSON，落盘 `public/studio/`：
`catalog_index.json`（索引）+ `bodies_*.json`（按需加载正文）。固定输入重复执行输出逐字节一致。

用法（解释器纪律见《项目维护说明（修改代码前必读）.md》）：
    .venv/Scripts/python.exe verify\\studio_catalog_build.py --sources-dir <目录> [--out-dir public\\studio]
    .venv/Scripts/python.exe verify\\studio_catalog_build.py --append-manju [--out-dir public\\studio]

漫剧始终读取仓库 skills/ai-manju-skills；--manju-dir 可指定包根目录做离线校验。
增补只写索引、script正文与JSON报告；Markdown报告仅完整构建显式 --report-md 时生成。

sources 目录约定文件名（`--sources-dir` 由运行者传语义路径，本脚本不内嵌真实路径）：
- qiyuan_prompt_library_index.json / qiyuan_prompt_library_bodies.json   提示词主库 63
- chat_extra_index.json / chat_extra_bodies.json                        提示词增补 24
- qiyuan_novel_templates.json                                           剧本 53
- qiyuan_game_prompt_templates.json                                     游戏原画 24
- qiyuan_gp_style_lib.json / qiyuan_lib_style_lib.json / qiyuan_vs_style_lib.json   风格库 397
- qiyuan_novel_sensitive.json / qiyuan_game_prompt_templates.json 的敏感扫描见标记表

红线：不搬运启元 pyc/代码，仅使用允许纳入的文本/JSON 数据；NSFW 不入库；清理不静默
（拒收/ anomalies 逐条进 docs/plans/2026-10-22-工作台Studio-转换报告.md 与 conversion_report.json）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path

# 模块/类型枚举与《_studio_plan_v2.md》§3 数据契约一致；越界即拒收。
MODULES_ALLOWED = ("vs", "h3", "vi", "ec", "global", "script", "game", "style")
TYPES_ALLOWED = ("template", "rule", "lexicon", "example")
MODULE_ORDER = ("vs", "h3", "vi", "ec", "global", "script", "game", "style")

SNIPPET_N = 700
BUILD_STAMP = "2026-10-22"
CATALOG_VERSION = "2.0.0"
BASE_EXPECTED_TOTAL = 561
EXPECTED_TOTAL = BASE_EXPECTED_TOTAL + 21
MANJU_SOURCE = "ai-manju-skills"
MANJU_REPOSITORY = "https://github.com/zhanghao123vip/ai-manju-skills"
MANJU_COMMIT = "77fb32a53a00ca7f455cba6a74ec1b05e1a63fb4"

# 手工修正的提示词在转换器中维护，完整重建和增量刷新共用，避免旧来源把修复覆盖。
H3_REFERENCE_COMMIT = "199b7d0e1fd95e7b3eafa8c15eca692534142427"
H3_REFERENCE_ROOT = f"https://github.com/MiniMax-AI/MiniMax-H3/blob/{H3_REFERENCE_COMMIT}/skills/h3-prompt-writing/references"
H3_BASE_PROMPT = """你是 MiniMax H3 基础视频提示词编写助手。根据用户的文字意图、实际提供的关键帧和目标时长，生成可直接使用的英文提示词。本模板依据 MiniMax 官方 base-en 指南整理，适用于 T2VA、I2VA、FL2VA、L2VA。

这是批量转换调用：不要向用户提问、不要要求确认、不要只输出说明，直接按已给素材和剧本写最终提示词。

先看工作流提供的素材槽位证据再选模式：没有图片、只有文字用 T2VA；槽位明确是首帧用 I2VA；明确首尾两帧用 FL2VA；明确仅尾帧用 L2VA。普通参考图不是首帧，也不得当成首帧。槽位证据表明图片是普通参考图（ref_images / reference / subject / 参考）时，直接改用 Ref2VA 参考写法，用 <Subject 1> 引用图片，不要提问。关键帧和普通参考混在一起时，以关键帧模式为准，普通参考图在正文里用 <Subject N> 补充。没有收到图片时按 T2VA 写，不索要图片，也不声称已经看图。

输出格式：只输出最终提示词，不输出规则说明、Markdown 围栏或中文翻译。基础模式输出所选模式需要的对齐首行和下列三个字段，字段之间空一行，顺序固定为：
integrated_multimodal_description: ...
overall_soundscape: ...
non_diegetic_music: ...

普通参考图改用的 Ref2VA 写法输出下列六段，顺序固定为：
subject_definitions: ...
summary: ...
retention_analysis: ...
detailed_description: ...
overall_soundscape: ...
non_diegetic_music: ...

时长采用用户要求；未说明时按剧本动作需要给出 4–15 秒内的合理时长，不询问。镜头切点必须在总时长以内。

模式与关键帧：
- T2VA：从文字建立整个视听过程，直接以 integrated_multimodal_description 开始，不添加图片对齐首行。
- I2VA：<Picture 1> 是 0.00 秒的真实首帧，属于 [Shot 1]。首行严格使用：
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.
  正文先建立该图可见主体、衣着、构图、环境和光线，再描述从首帧连续发生的运动。
- FL2VA：Picture 1 为首帧，Picture 2 为尾帧。首行使用以下格式，把 N 换成实际最后镜头序号，把 S.SS 换成两位小数总时长：
How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the S.SS-second mark of the target video.
  通常用一个连续镜头写清主体、物体、相机和构图如何从首帧逐步到达尾帧；用户要求分镜时才分镜。
- L2VA：<Picture 1> 是最终镜头的尾帧，不是默认首帧。首行使用以下格式，并替换 N 和 S.SS：
How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark of the target video.
  从与尾帧及用户意图相容的较早状态开始，描述动作和相机如何逐步收敛到尾帧。

共同写法：
1. integrated_multimodal_description 按播放顺序写可观察的画面、动作、相机和同步声音。[Shot 1] 不带时间戳；后续镜头用 [Shot N] At MM:SS.mmm, ...，切点严格递增。第一镜建立风格和初始构图，关键帧任务以图片可见事实为准，不凭空编造图片外的信息。
2. 运镜写成句子，必要时说明幅度和速度。Zoom 是变焦，Push/Pull 是前后移动，Pan 是水平转动，Truck 是水平平移，Tilt 是上下转动，Pedestal 是相机整体升降；不要混用这些运动。
3. 发声角色使用跨镜头稳定的 (S1)、(S2)。对白和歌词放在 <d>[Language] 原文</d>，保留原语言和原句；画内文字使用英文双引号保留原文。画外音注明 says in an off-screen voiceover，并在其后说明画内人物嘴唇保持闭合；跨切点对白用 <scenetrans> 标记连续，片尾截断用 <cutoff>。
4. overall_soundscape 用一个英文段落概述环境声、动作声和非语言人声，不重复对白、演唱或画内音乐；只有用户要求全片完全静音时填 N/A。
5. non_diegetic_music 描述观众听见而角色听不见的配乐，写乐器、节奏、速度和动态变化；没有配乐填 N/A。角色听见的电台、乐器演奏和音乐放入画面时间线。

输出前自查：第一句必须是提示词正文（对齐首行或 integrated_multimodal_description），不得以提问、索要图片、请求确认或规则说明开头；信息不全就按现有剧本和素材取最合理的写法补齐，不中断输出。
"""
H3_REFERENCE_PROMPT = """你是 MiniMax H3 参考生成与编辑提示词编写助手。依据用户实际提供的图片、视频、音频及创作目标，整理英文 Ref2VA 提示词。本模板依据 MiniMax 官方 ref-en 指南整理，覆盖参考生成、关键帧补全、视频编辑、视频续写、音频复用与音频参考。

这是批量转换调用：不要向用户提问、不要要求确认、不要只输出说明，直接按已收到的素材和剧本写最终提示词。

先按工作流槽位证据确认各素材的用途、目标时长、保留项与修改项；用途不明时按普通参考处理，不猜成首帧或尾帧。当前界面只发图片时，就只基于图片和剧本工作：缺少视频或音频不算缺项，不要索要，也不声称看过或听过未发送的素材。文件名、路径或用户一句“参考这个视频”不能替代素材内容。目标时长未说明时按剧本需要给出 4–15 秒内的合理时长，不询问。

只输出以下六个英文段落，顺序与字段名固定，不加 Markdown 围栏：
subject_definitions: ...
summary: ...
retention_analysis: ...
detailed_description: ...
overall_soundscape: ...
non_diegetic_music: ...

subject_definitions：逐项定义后文确实会用到的素材内容与引用标签。<Subject N> 表示从素材中提取的角色、物体、场景、服装、风格或动作；<Picture N> 只用于具体帧、关键帧、首尾帧或分镜构图锚点；<Video N> 用于待编辑/续写的原片或整段视频的时间结构；<Audio N> 用于实际复用或参考的音频信号。只定义角色外观的图片应放在对应 Subject 的来源说明里，不额外创建独立 Picture 条目。同一素材的画面与声音分别编号，不因视频带声就自动建立 Audio。标签在所有字段保持同义，发声人物另外使用稳定的 (S1)、(S2)，不要把人物编号和说话者编号混同。

summary：以方括号任务类型开头，用一个短英文段落说明目标和引用关系。按实际用途选择并用 + 合并 keyframe completion、reference generation、video editing、video continuation、audio reuse、audio reference。修改静态图或首尾帧补全不属于 video editing；直接复用同一音频信号才是 audio reuse，只参考音色、节奏或音乐风格属于 audio reference。

retention_analysis：逐项写引用出现在哪些镜头、哪些特征保留或改变。视觉内容的关系标记使用 fully_preserved、partially_preserved、attribute_transfer、weak_reference；音频使用 fully_copy、partially_copy、reference、weak_reference。关系以 subject_definitions 定义的用途为准；新增背景或动作不自动代表角色参考失真。

detailed_description：先用一两句英文建立整体风格，再从 [Shot 1] 开始按播放顺序写分镜。第一镜不加时间戳，后续镜头用 [Shot N] At MM:SS.mmm, ...，切点递增且不超过总时长。逐镜交代构图、可见主体特征和位置、环境光线、动作变化、相机运动、当前声音，以及引用素材何时生效。重要 Subject 首次出现时写清可见特征，后续沿用标签；关键帧说明从哪张图开始、对应哪张图或结束在哪张图。编辑/续写任务自然引用 Video 的原始状态或结构，Audio 在实际生效的镜头或阶段说明复制还是参考。一般生成任务采用约 350–500 个英文词；编辑任务按源片复杂程度调整，对白优先满足实际可播放时长。

对白和歌词使用 <d>[Language] 原文</d>，保持原语言；听不清写 [unclear]，不能猜词。画面里的文字用英文双引号保留原文。发声角色使用跨镜头稳定的 (S1)、(S2)，对应引用人物时写 <Subject N> (Sx)。画外音注明 says in an off-screen voiceover 并说明画内嘴唇保持闭合；跨切点连续语音用 <scenetrans>，片尾截断用 <cutoff>。直接复用的配乐中含人声不等于新增一个发声角色。

overall_soundscape：用一个英文段落写环境声、动作声、非语言人声及实际音频引用关系，不重复对白或配乐段；只有全片完全静音填 N/A。
non_diegetic_music：说明画外配乐的乐器、速度、节奏和动态，并标明相关 Audio 是复制还是参考；没有画外配乐填 N/A。画内可听的音乐放在 detailed_description。

输出前自查：第一句必须是 subject_definitions:，不得以提问、索要素材、请求确认或规则说明开头；信息不全就按现有图片和剧本取最合理的写法补齐，不中断输出。
"""
IMAGE_REVERSE_PROMPT = """请根据实际收到的参考图片反推可复现其可见画面的图像提示词。
先观察图片，再组织文字。保留真实可见的主体、外观、服饰、动作、空间位置、前景背景、构图、色彩、材质、光线和画面风格。不要把本模板复述成结果，不因用户只写了一个概念就凭空增加人物、物件、镜头、特效、材质或光影；看不清的细节不要猜测。
用户补充文字只用于指定关注点和输出语言，不能当成图片中已经存在的事实。没有实际图片时明确要求上传图片，不声称已经完成反推。
输出一段可直接使用的连贯图像提示词，默认中文；用户明确要求英文时输出英文。只描述图片支持的内容，不追加解释、模板规则、无依据的画质口号或对水印的描述。
"""


def prompt_updates() -> list[dict]:
    """应用自己的改写规则；官方链接仅记录格式依据，不复制整份第三方指南。"""
    return [
        {"id": "h3-007-h3-i2va", "title": "MiniMax H3 基础视频 · 文生与首尾帧", "module": "h3",
         "body": H3_BASE_PROMPT, "summary": "依据官方指南整理的基础模式模板：T2VA / I2VA / FL2VA / L2VA；普通参考图改走 Ref2VA。未包含完整官方参考与示例，可选完整 Skill。",
         "tags": ["MiniMax", "H3", "T2VA", "I2VA", "FL2VA", "L2VA"], "source": f"{H3_REFERENCE_ROOT}/base-en.txt"},
        {"id": "h3-014-h3-ref2va", "title": "MiniMax H3 参考生成与视频编辑", "module": "h3",
         "body": H3_REFERENCE_PROMPT, "summary": "依据官方指南整理的 Ref2VA 六字段模板，覆盖参考生成、编辑/续写与音频引用；未包含完整官方参考与示例，可选完整 Skill。",
         "tags": ["MiniMax", "H3", "Ref2VA", "参考生成", "视频编辑"], "source": f"{H3_REFERENCE_ROOT}/ref-en.txt"},
        {"id": "vi-004-cmd-reverse", "title": "图片反推 · 可见画面提示词", "module": "vi",
         "body": IMAGE_REVERSE_PROMPT, "summary": "依据实际图片提取主体、构图、色彩和光影，不把文字概念扩写伪装成看图。",
         "tags": ["反推", "图片", "提示词"], "source": "Cat Chat 图片反推规则"},
        {"id": "vi-111-i2t-enhance", "title": "图生图反推 · 画面细节提取", "module": "vi",
         "body": IMAGE_REVERSE_PROMPT, "summary": "提取实际参考图片可见细节，默认输出中文提示词，不补造画面内容。",
         "tags": ["反推", "图片", "图生图"], "source": "Cat Chat 图片反推规则"},
    ]

# NSFW / 不适宜内容硬标记（入库前逐条扫描正文，命中即拒收并进报告；大小写不敏感）。
SENSITIVE_MARKERS = (
    "露点", "全裸", "做爱", "性爱", "淫水", "淫虐", "群交", "肛交", "口交", "乳交", "迷奸", "强奸",
    "嫖娼", "卖淫", "炮友",
    "nsfw", "porn", "erotic", "nude", "naked", "cumshot", "blowjob", "hardcore sex",
)

# 人工复核例外清单：id -> [(命中标记, 保留理由)]。命中清单内标记不算 NSFW，其余标记照常拒收。
# 理由随转换报告落档（清理不静默）。
SENSITIVE_EXEMPT: dict[str, dict[str, str]] = {
    "h3-001-manga4grid": {
        "群交": "「人群交谈」的子串误命中，上下文是派对会场四宫格示例，无性内容",
    },
    "vs-101-k2-engine": {
        "露点": "K2 SFW 版：正文自述「本文件为 SFW 版，不收录露骨内容」，NSFW 仅作为分级判定说明与外置扩展库登记",
        "nsfw": "K2 SFW 版：NSFW 仅是 SFW/NSFW 判定分叉的说明词，正文不含露骨内容",
    },
}


# 正文'模版变量'占位符：{{key}} 字面文本替换（不执行表达式/脚本）。
VAR_PATTERN = re.compile(r"\{\{([^{}]+)\}\}")

# 产物分片：模块 -> 正文分片文件标签（按需懒加载的最小单位）。
BODIES_TAG_BY_MODULE = {
    "vs": "prompt", "h3": "prompt", "vi": "prompt", "ec": "prompt", "global": "prompt",
    "script": "script", "game": "game", "style": "style",
}


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"源文件不可读或非法 JSON：{path.name}：{exc}")


def source_sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def match_sensitive(text: str, item_id: str = "") -> tuple[list[str], dict[str, str]]:
    """返回 (待拒收标记, 保留的描述性提及{id: 理由})。"""
    low = text.lower()
    exempt = SENSITIVE_EXEMPT.get(item_id, {})
    reject: list[str] = []
    handled: dict[str, str] = {}
    for marker in SENSITIVE_MARKERS:
        if marker in low:
            if marker in exempt:
                handled[marker] = exempt[marker]
            else:
                reject.append(marker)
    return reject, handled


def vars_from_text(text: str) -> list[dict]:
    """把 {{key}} 占位符整理成变量表（key/label/default）。"""
    keys: list[str] = []
    for token in VAR_PATTERN.findall(text):
        key = token.strip()
        if key and key not in keys:
            keys.append(key)
    return [{"key": key, "label": key, "default": ""} for key in keys]


def new_item(
    *,
    item_id: str,
    title: str,
    module: str,
    item_type: str,
    summary: str,
    tags: list[str],
    body: str,
    model: list[str] | None = None,
    source: str,
    cleaned: list[str] | None = None,
    origin: str = "builtin",
    genre: str = "",
) -> dict:
    return {
        "id": item_id,
        "title": title,
        "module": module,
        "type": item_type,
        "summary": summary,
        "tags": tags,
        "chars": len(body),
        "model": [str(m) for m in (model or []) if str(m).strip()],
        "vars": vars_from_text(body),
        "has_vars": False,  # 由 convert_field_check 统一回填
        "source": source,
        "source_sha1": source_sha1(body),
        "cleaned": list(cleaned or []),
        "origin": origin,
        "order": 0,
        "genre": genre,
        "snippet": body[:SNIPPET_N],
    }


# ---- 家族 1：提示词主库 87（启元 63 + 增补 24，两份索引本身已是契约形态） ----

def prompt_family(source_dir: Path):
    items: list[dict] = []
    bodies: dict[str, str] = {}
    rejects: list[dict] = []
    descriptive: list[dict] = []
    stats: "OrderedDict[str, dict]" = OrderedDict()
    order = 1
    for pack, source_name, expect_n in (
        ("qiyuan_prompt_library", "qiyuan_assets.prompt_library", 63),
        ("chat_extra", "chat_studio_extra", 24),
    ):
        index = read_json(source_dir / f"{pack}_index.json")
        body_map = read_json(source_dir / f"{pack}_bodies.json")
        rows = list(index.get("items") or [])
        dupes: list[str] = []
        empty: list[str] = []
        sensitive: list[str] = []
        output = 0
        seen: set[str] = set()
        for raw in rows:
            iid = str(raw.get("id") or "").strip()
            if not iid:
                rejects.append({"id": "", "source": source_name, "reason": ["空ID"], "sensitive": []})
                continue
            if iid in seen:
                dupes.append(iid)
                rejects.append({"id": iid, "source": source_name, "reason": ["重复ID"], "sensitive": []})
                continue
            seen.add(iid)
            body = str(body_map.get(iid) or "")
            if not body.strip():
                empty.append(iid)
                rejects.append({"id": iid, "source": source_name, "reason": ["空正文"], "sensitive": []})
                continue
            hits, handled = match_sensitive(body, iid)
            if handled:
                descriptive.append({"id": iid, "source": source_name, "markers": sorted(handled)})
            if hits:
                sensitive.append(iid)
                rejects.append({"id": iid, "source": source_name, "reason": [], "sensitive": hits})
                continue
            item = new_item(
                item_id=iid,
                title=str(raw.get("title") or "").strip(),
                module=str(raw.get("module") or "").strip(),
                item_type=str(raw.get("type") or "").strip(),
                summary=str(raw.get("summary") or "").strip(),
                tags=[str(t) for t in (raw.get("tags") or []) if str(t).strip()],
                body=body,
                model=[str(m) for m in (raw.get("model") or []) if str(m).strip()],
                source=str(raw.get("source") or "").strip() or source_name,
                cleaned=[str(c) for c in (raw.get("cleaned") or []) if str(c).strip()],
                origin=str(raw.get("origin") or "") or "builtin",
                genre=str(raw.get("genre") or "").strip(),
            )
            item["vars"] = raw.get("vars") or []
            item["has_vars"] = bool(item["vars"])
            item["order"] = order
            order += 1
            items.append(item)
            bodies[iid] = body
            output += 1
        stats[source_name] = {
            "input": len(rows),
            "output": output,
            "dupes": len(dupes),
            "rejected": len(dupes) + len(empty) + len(sensitive),
            "expected": expect_n,
            "empty_ids": empty[:12],
            "sensitive_ids": sensitive[:12],
            "dupes_ids": dupes[:12],
        }
    return items, bodies, rejects, stats, descriptive


# ---- 家族 2：剧本 53 ----

def script_family(source_dir: Path):
    descriptive: list[dict] = []
    data = read_json(source_dir / "qiyuan_novel_templates.json")
    channels = {str(c.get("key")): c for c in (data.get("channels") or []) if c.get("key")}
    source = "qiyuan_assets.novel_templates"
    rows = list(data.get("templates") or [])
    items, bodies, rejects = [], {}, []
    dupes, seen = [], set()
    order = 1
    for raw in rows:
        orig_id = str(raw.get("id") or "").strip()
        nid = f"script-{orig_id}" if orig_id else ""
        if not nid:
            rejects.append({"id": "", "source": source, "reason": ["空ID"], "sensitive": []})
            continue
        if nid in seen:
            dupes.append(nid)
            rejects.append({"id": nid, "source": source, "reason": ["重复ID"], "sensitive": []})
            continue
        seen.add(nid)
        body = str(raw.get("prompt") or "")
        if not body.strip():
            rejects.append({"id": nid, "source": source, "reason": ["空正文"], "sensitive": []})
            continue
        hits, handled = match_sensitive(body, nid)
        descriptive.extend({"id": nid, "source": source, "markers": sorted(handled)} for _ in [0] if handled)
        if hits:
            rejects.append({"id": nid, "source": source, "reason": [], "sensitive": hits})
            continue
        ch = channels.get(str(raw.get("gender") or "")) or {}
        genre_parts = [part for part in (str(raw.get("sub") or ""), str(ch.get("name") or "")) if part]
        item = new_item(
            item_id=nid,
            title=str(raw.get("name") or "").strip(),
            module="script",
            item_type="template",
            summary=str(raw.get("desc") or "").strip()[:200],
            tags=[str(t) for t in (raw.get("tags") or []) if str(t).strip()],
            body=body,
            source=source,
            genre=" / ".join(genre_parts),
        )
        item["order"] = order
        order += 1
        items.append(item)
        bodies[nid] = body
    stats = {
        "input": len(rows),
        "output": len(items),
        "dupes": len(dupes),
        "rejected": len(rejects),
        "dupes_ids": dupes[:12],
    }
    return items, bodies, rejects, stats, descriptive


# ---- 漫剧：只读仓库中锁定版本的原文，不导入/执行上游代码 ----

# 固定清单与摘要独立于 source.json；manifest 自身也必须通过校验，不能自证来源。
MANJU_PRESETS = (
    ("xuanhuan", "玄幻修仙", "幻想", "3520deac5c951adfb80c750d6475e8f9e85c865f31b732ce2f1af3f5758e7a0a"),
    ("xihuan", "西幻魔法", "幻想", "4649b3d538b0e75d4aeb3b9d7169df09b4c2a153509c9e6ff8d408cfa085735d"),
    ("xingji", "星际机甲", "幻想", "91f04f9920e3c21e937948f3d7af36ac5e8f5dfec843a2c03e8cc6f25ede5a19"),
    ("xitong", "系统无限流", "幻想", "6477ba30dfab52a52e3984f30bb61936ab818a248ec53bbae525425d8b5894f5"),
    ("moshi", "末日废土", "幻想", "16aaee3c1905742a6ae2bdac0a4659afa7bc200da19c399170a77afbe1f99375"),
    ("chuanyue", "穿越重生", "古代", "2d13a8c63318b1d4c78c9e10ce4fe3999473207eb4c37637a76d784cd5d83c58"),
    ("gongdou", "宫斗", "古代", "2923dc316eaf812aad455bd3eb1c5aa7891d9a9924562f1ddbb2834157ee01eb"),
    ("zhaidou", "宅斗", "古代", "de0c994f035464bc4ac993959a7db428506f64c18fb67dbd65a2fa41fb296e94"),
    ("quanmou", "权谋朝堂", "古代", "ca11a1a7497386af1fc0363f1ee89dcc985ca6eb60c19a9df15e02309b3c37dc"),
    ("wuxia", "武侠江湖", "古代", "2c0f1a17e9acb9d1a2170633df8c4dd3b1245a9b8ee3432f11760d5df7e00483"),
    ("zhongtian", "种田经商", "古代", "28ac7be35e45065ef8edb5c02bbfd41cc9379987f3643b3a89b298d112e790b5"),
    ("dushi", "都市逆袭", "现代", "278a9c0fcdf2f558e15f2667ce6a8d0ed6aa7514edc5a1fcde8eecf7dd349590"),
    ("tianchong", "总裁甜宠", "现代", "fc0c082da4f7eb6b0dc6c94391e95bbc82d5f98b4f85da47793c77470e6c7aae"),
    ("jiating", "家庭伦理", "现代", "ab2858ea28da674d37a03c87c852f7bb2dc7831ac766a5d69b1789fce20b465f"),
    ("chongsheng", "重生复仇", "现代", "00921ddde81efce17494b853d44f9dede9bc10fdecc7d127ea08e26867f1dc67"),
    ("shangzhan", "职场商战", "现代", "24e67707641071cc1df129c0798063771100e7a9d41c9041554bd7bb0f2a1aae"),
    ("niandai", "年代文", "现代", "0a158cc0f33a2f07a59063b607c1346cadc7b294b20181aff3cfc2aae36bb697"),
    ("yule", "娱乐圈", "现代", "509c878cd05b3ad7be440b92c8a3f19ed04c507161f03f065d83d07d0384f373"),
    ("xiaoyuan", "大学校园青春", "现代", "a0fd16086d64b16ed4c43298c91dd46abfb5b6a77cb1642620604ecd4dd3cba2"),
    ("xuanyi", "悬疑刑侦", "类型片", "080282384fe82d49d97e86a8025269d3c72a9a3a416cbbd7f3d36ebc4d3cd031"),
    ("minsu", "民俗怪谈", "类型片", "5c38535b512c6c273323d8fb7451f2045180f3825854717a6577af7ecc704c68"),
)
MANJU_USAGE = (
    "## 使用说明（聊天版）\n\n"
    "根据我的输入直接生成首批内容，无需先等待就绪确认；未指定的项目采用下文默认值。"
    "已有材料时，我可以指定从某个阶段、某集或某批开始，请以我在对话中提供的材料为依据，"
    "不依赖工具、文件读取或写入。每批开头标明阶段、集数和批次，结束提示本批已完成、下一批内容及继续指令。"
    "我说“继续”时，先结合本段对话衔接当前进度；若交接对话缺少必要材料，"
    "只追问缺少的交接内容，不臆造已完成的结果。仅生成制作文档与提示词，"
    "不声称已经自动出图或生成视频；图片上传和视频制作由我另行完成。\n\n"
)


def _manju_require(ok: bool, context: str) -> None:
    # 不用 assert：即使 python -O 也必须失败闭合。
    if not ok:
        raise SystemExit(f"漫剧转换校验失败：{context}")


def _manju_convert(text: str, slug: str) -> tuple[str, dict[str, int]]:
    """对已逐份核对的实际结构作计数替换；保留全部其它字句与内容尺度。"""
    counts: dict[str, int] = {}

    def sub(name: str, pattern: str, replacement, expected: int, flags: int = 0) -> None:
        nonlocal text
        text, n = re.subn(pattern, replacement, text, flags=flags)
        _manju_require(n == expected, f"{slug}/{name} 匹配 {n}，预期 {expected}")
        counts[name] = n

    sub("frontmatter", r'\A---\nname: ' + re.escape(slug) + r'-manju\ndescription: [^\n]+\n---\n\n', "", 1)
    sub("paste_ready", r'^> 如果你是通过粘贴看到这份文档的：[^\n]+然后等用户输入。\n\n', "", 1, re.M)
    sub("file_tool_paragraph", re.escape(
        "**能写文件的工具**（如 WorkBuddy、Claude Code）：不用每批停下，一个阶段连续写完，写不完再分批。"
        "文件按下面的结构存放。粘贴到聊天窗口使用时，忽略这一条。\n\n"), "", 1)
    sub("directory_block", re.escape(
        "```\n片名/\n  01-企划案.md\n  02-资产库.md\n  03-分集剧本.md\n"
        "  04-导演手册/第01集.md\n  05-发布包/第01集.md\n```\n\n"), "", 1)
    sub("manual_file_clause", re.escape("；能写文件的工具写资产库的文件名和编号即可"), "", 1)
    # 星际机甲在资产卡中额外出现一次 Mixed 2，不能套用其它20份的匹配数。
    sub("reference_tokens", r'\{\{Mixed ([1-3])\}\}', lambda m: f"［参考图 {m[1]}］", 4 if slug == "xingji" else 3)
    sub("upload_order", r'(?<!\w)Mixed ([1-3])(?=＝)', lambda m: f"［参考图 {m[1]}］", 3)
    _manju_require(not re.search(r'Mixed|\{\{|能写文件|WorkBuddy|Claude Code|只回复一句|01-企划案\.md', text),
                   f"{slug} 残留工具/就绪指令/占位符")
    headings = re.findall(r'^## [二三四五六]、阶段([1-5])：[^\n]+', text, re.M)
    _manju_require(headings == list("12345"), f"{slug} 五阶段结构异常")
    _manju_require(text.count("```") == 10, f"{slug} 非目录格式示例围栏异常")
    title, separator, rest = text.partition("\n\n")
    _manju_require(bool(separator) and title.startswith("# "), f"{slug} 主标题异常")
    return title + "\n\n" + MANJU_USAGE + rest, counts


def manju_family(source_dir: Path | None = None):
    """返回 (items, bodies, rejects, stats, descriptive)；source_dir 是漫剧包根目录。"""
    root = Path(source_dir) if source_dir is not None else Path(__file__).resolve().parents[1] / "skills" / MANJU_SOURCE
    manifest = read_json(root / "source.json")
    _manju_require(isinstance(manifest, dict), "source.json 必须是对象")
    _manju_require(manifest.get("repository") == MANJU_REPOSITORY and manifest.get("commit") == MANJU_COMMIT,
                   "source.json repository/commit 不符合锁定版本")
    _manju_require(manifest.get("license") == "MIT" and manifest.get("copyright") == "Copyright (c) 2026 zhanghao123vip",
                   "source.json 许可/署名不符")
    try:
        license_bytes = (root / "LICENSE").read_bytes()
    except OSError as exc:
        raise SystemExit(f"漫剧 LICENSE 不可读：{exc}")
    _manju_require(hashlib.sha256(license_bytes).hexdigest() == "d1fe23143f5d054dc0d7141b01ae8deae82e44bfe7e501c3fd1c484f7500d3c9",
                   "LICENSE 与锁定 MIT 原件不符")
    rows = manifest.get("files")
    _manju_require(isinstance(rows, list) and len(rows) == len(MANJU_PRESETS), "manifest 文件数不符")
    _manju_require(all(isinstance(row, dict) for row in rows), "manifest 文件记录必须是对象")
    mapping = {row.get("path"): row for row in rows}
    _manju_require(set(mapping) == {f"{slug}-manju/SKILL.md" for slug, *_ in MANJU_PRESETS}, "manifest 文件清单不符")
    items, bodies, audits = [], {}, []
    for order, (slug, title, group, digest) in enumerate(MANJU_PRESETS, 1):
        relative = f"{slug}-manju/SKILL.md"
        local_path = f"references/{slug}-manju.md"
        row = mapping[relative]
        _manju_require(row.get("local_path") == local_path, f"{relative} 本地参考路径不符")
        try:
            raw = (root / local_path).read_bytes()
            original = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SystemExit(f"漫剧原件不可读：{relative}：{exc}")
        _manju_require(hashlib.sha256(raw).hexdigest() == digest, f"{relative} 原件 SHA256 不符")
        _manju_require(row.get("sha256") == digest and row.get("bytes") == len(raw)
                       and row.get("title") == title and row.get("group") == group, f"{relative} manifest 元数据不符")
        body, counts = _manju_convert(original, slug)
        iid = f"script-manju-{slug}"
        # 固定版本逐份复核为0命中；未来任何新增命中都须复核，不能静默丢一份。
        hits, handled = match_sensitive(body, iid)
        _manju_require(not hits and not handled, f"{iid} 新敏感标记需人工复核：{hits or handled}")
        source = f"{MANJU_REPOSITORY}/blob/{MANJU_COMMIT}/skills/{relative}"
        item = new_item(
            item_id=iid, title=f"漫剧·{title}", module="script", item_type="template",
            summary=f"{title}漫剧全流程：企划案、全季资产库、分集剧本、导演手册与发布包；按批次在对话中继续。",
            tags=["漫剧", "全流程", group, title], body=body, source=source, genre=title,
            cleaned=list(counts) + ["chat_usage"],
        )
        item["has_vars"] = bool(item["vars"])
        item["order"] = order
        items.append(item)
        bodies[iid] = body
        audits.append({"id": iid, "file": f"skills/{relative}", "sha256": digest,
                       "bytes": len(raw), "rules": counts, "output_sha1": item["source_sha1"]})
    stats = {"input": len(MANJU_PRESETS), "output": len(items), "expected": len(MANJU_PRESETS),
             "dupes": 0, "rejected": 0, "repository": MANJU_REPOSITORY, "commit": MANJU_COMMIT,
             "license": "MIT", "groups": dict(Counter(group for _, _, group, _ in MANJU_PRESETS)),
             "audit": audits}
    return items, bodies, [], stats, []


# ---- 家族 3：游戏原画 24 ----

def game_family(source_dir: Path):
    descriptive: list[dict] = []
    data = read_json(source_dir / "qiyuan_game_prompt_templates.json")
    cat_names = {str(c.get("id")): str(c.get("name") or "") for c in (data.get("categories") or []) if c.get("id")}
    source = "qiyuan_assets.game_prompt_templates"
    rows = list(data.get("items") or [])
    items, bodies, rejects = [], {}, []
    dupes, seen = [], set()
    game_placeholders: list[str] = []
    order = 1
    for raw in sorted(rows, key=lambda r: int(r.get("order") or 0)):
        orig_id = str(raw.get("id") or "").strip()
        gid = f"game-{orig_id}" if orig_id else ""
        if not gid:
            rejects.append({"id": "", "source": source, "reason": ["空ID"], "sensitive": []})
            continue
        if gid in seen:
            dupes.append(gid)
            rejects.append({"id": gid, "source": source, "reason": ["重复ID"], "sensitive": []})
            continue
        seen.add(gid)
        system_text = str(raw.get("system_prompt") or "").strip()
        template_text = str(raw.get("prompt_template") or "").strip()
        parts = []
        if system_text:
            parts.append(f"## 角色设定\n{system_text}")
        if template_text:
            parts.append(f"## 任务模板\n{template_text}")
        body = "\n\n".join(parts)
        placeholders = vars_from_text(template_text)
        game_placeholders.extend(v["key"] for v in placeholders)
        if not body.strip():
            rejects.append({"id": gid, "source": source, "reason": ["空正文"], "sensitive": []})
            continue
        hits, handled = match_sensitive(body, gid)
        if handled:
            descriptive.append({"id": gid, "source": source, "markers": sorted(handled)})
        if hits:
            rejects.append({"id": gid, "source": source, "reason": [], "sensitive": hits})
            continue
        sub_purpose = str(raw.get("sub_purpose") or "").strip()
        item = new_item(
            item_id=gid,
            title=str(raw.get("name") or "").strip(),
            module="game",
            item_type="template",
            summary=str(raw.get("description") or "").strip()[:240],
            tags=[str(t) for t in (raw.get("tags") or []) if str(t).strip()],
            body=body,
            source=source,
            genre=cat_names.get(str(raw.get("category") or ""), "") or sub_purpose,
        )
        item["vars"] = placeholders
        item["has_vars"] = bool(placeholders)
        item["order"] = order
        order += 1
        items.append(item)
        bodies[gid] = body
    stats = {
        "input": len(rows),
        "output": len(items),
        "dupes": len(dupes),
        "rejected": len(rejects),
        "dupes_ids": dupes[:12],
    }
    return items, bodies, rejects, stats, game_placeholders, descriptive


# ---- 家族 4：风格库 397 ----

STYLE_LIBS = (
    ("qiyuan_gp_style_lib.json", "gp"),
    ("qiyuan_lib_style_lib.json", "lb"),
    ("qiyuan_vs_style_lib.json", "vs"),
)


def style_family(source_dir: Path):
    source = "qiyuan_assets.style_libs"
    descriptive: list[dict] = []
    items, bodies, rejects = [], {}, []
    dupes, seen, empty = [], set(), []
    order = 1
    total_input = 0
    per_lib: dict[str, dict] = {}
    for fname, tag in STYLE_LIBS:
        rows = list(read_json(source_dir / fname))
        total_input += len(rows)
        per_lib[tag] = {"input": len(rows), "output": 0}
        for idx, raw in enumerate(rows, 1):
            sid = f"style-{tag}-{idx:03d}"
            if sid in seen:
                dupes.append(sid)
                rejects.append({"id": sid, "source": source, "reason": ["重复ID"], "sensitive": []})
                continue
            seen.add(sid)
            body = str(raw.get("prompt") or "")
            title = str(raw.get("cn") or "").strip()
            en = str(raw.get("en") or "").strip()
            cat = str(raw.get("cat") or "").strip()
            if not body.strip():
                empty.append(sid)
                rejects.append({"id": sid, "source": source, "reason": ["空正文"], "sensitive": []})
                per_lib[tag]["output"] = per_lib[tag]["output"]  # 保持计数不动
                continue
            hits, handled = match_sensitive(body or "", sid)
            if handled:
                descriptive.append({"id": sid, "source": source, "markers": sorted(handled)})
            if hits:
                rejects.append({"id": sid, "source": source, "reason": [], "sensitive": hits})
                continue
            item = new_item(
                item_id=sid,
                title=title,
                module="style",
                item_type="lexicon",
                summary=en,
                tags=[t for t in (cat, en) if t],
                body=body,
                source=source,
                genre=cat,
            )
            item["order"] = order
            order += 1
            items.append(item)
            bodies[sid] = body
            per_lib[tag]["output"] += 1
    stats = {
        "input": total_input,
        "output": len(items),
        "dupes": len(dupes),
        "rejected": len(rejects),
        "per_lib": per_lib,
        "empty_ids": empty[:12],
    }
    return items, bodies, rejects, stats, descriptive


# ---- 校验与落盘 ----

def cross_check(items: list[dict], bodies: dict[str, str]) -> dict[str, list[str]]:
    """契约一致性（ID 唯一 / 正文齐全 / 片段与字符数一致 / 枚举 / has_vars 一致）。"""
    problems: dict[str, list[str]] = {}
    dupes = [iid for iid, n in Counter(it["id"] for it in items).items() if n > 1]
    if dupes:
        problems["dupe_ids"] = sorted(dupes)
    missing = [iid for iid in all_iter(items) if not bodies.get(iid, "").strip()]
    if missing:
        problems["missing_bodies"] = sorted(missing)
    orphan = set(bodies) - set(all_iter(items))
    if orphan:
        problems["unindexed_bodies"] = sorted(orphan)
    bad_module = [it["id"] for it in items if it["module"] not in MODULES_ALLOWED]
    bad_type = [it["id"] for it in items if it["type"] not in TYPES_ALLOWED]
    if bad_module:
        problems["bad_module"] = sorted(bad_module)
    if bad_type:
        problems["bad_type"] = sorted(bad_type)
    snippet_bad = [it["id"] for it in items if bodies.get(it["id"]) and bodies[it["id"]][:SNIPPET_N] != it["snippet"]]
    if snippet_bad:
        problems["snippet_mismatch"] = sorted(snippet_bad)
    chars_bad = [it["id"] for it in items if len(bodies.get(it["id"], "")) != it["chars"]]
    if chars_bad:
        problems["chars_mismatch"] = sorted(chars_bad)
    vars_bad = [it["id"] for it in items if it["has_vars"] != bool(it["vars"])]
    if vars_bad:
        problems["vars_mismatch"] = sorted(vars_bad)
    empty_title = [it["id"] for it in items if not it["title"].strip()]
    if empty_title:
        problems["empty_title"] = sorted(empty_title)
    return problems


def all_iter(items: list[dict]):
    for it in items:
        yield it["id"]


def build_index(items: list[dict]) -> dict:
    modules = Counter(it["module"] for it in items)
    types = Counter(it["type"] for it in items)
    return {
        "ok": True,
        "version": CATALOG_VERSION,
        "built": BUILD_STAMP,
        "count": len(items),
        "chars": sum(int(it["chars"]) for it in items),
        "snippet_n": SNIPPET_N,
        "modules": {m: modules.get(m, 0) for m in MODULE_ORDER if m in modules},
        "types": {t: types.get(t, 0) for t in TYPES_ALLOWED if t in types},
        "items": items,
    }


def write_json(path: Path, payload: object) -> int:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8", newline="")
    return path.stat().st_size


def apply_catalog_cleanup(items: list[dict], bodies: dict[str, str]) -> None:
    """只整理已核实的内置条目；全量构建和增量刷新使用相同规则。"""
    by_id = {item["id"]: item for item in items}
    cinematic_id = "h3-004-cinematic-i2v"
    old = "自动调用行业顶尖创意数据库进行最优补全"
    new = "依据用户已给信息及通用创作知识补全，不声称调用未提供的数据库"
    if cinematic_id in bodies:
        body = bodies[cinematic_id]
        if old not in body and new not in body:
            raise ValueError(f"{cinematic_id} 的已核实句子已变化，请重新检查")
        bodies[cinematic_id] = body.replace(old, new)
    daily_id = "vs-037-daily0621"
    if daily_id in bodies:
        body = bodies[daily_id]
        if "点击下方链接" in body:
            body, count = re.subn(r"^[^\n]*点击下方链接[^\n]*注册[^\n]*\n.*?(?=^画面风格：)",
                                  "", body, flags=re.MULTILINE | re.DOTALL)
            if count != 1:
                raise ValueError(f"{daily_id} 的推广块边界已变化，请重新检查")
        bodies[daily_id] = body
    updates = {
        cinematic_id: ("unavailable_database_reference", None),
        daily_id: ("promotion_download_residue", "图像提示词示例合集，保留画面描述与风格示范，已清理推广、下载说明和空链接。"),
        "vs-101-k2-engine": ("dependency_scope_summary", "K2 人像提示词 SFW 规则与词库；用于生成文本提示词。正文提到的外置 NSFW 扩展库未包含。"),
        "h3-005-ltx-relay": ("dependency_scope_summary", "生成 LTX2.3 双框架 PromptRelay 格式文本；后续运行视频需另配 ComfyUI-PromptRelay 节点，本模板不执行节点。"),
    }
    for item_id, (reason, summary) in updates.items():
        item = by_id.get(item_id)
        if item is None:
            continue
        body = bodies[item_id]
        item["chars"] = len(body)
        item["snippet"] = body[:SNIPPET_N]
        item["source_sha1"] = source_sha1(body)
        item["cleaned"] = list(dict.fromkeys([*(item.get("cleaned") or []), reason]))
        if summary:
            item["summary"] = summary


def catalog_cleanup_report(items: list[dict]) -> dict:
    ids = {"h3-004-cinematic-i2v", "vs-037-daily0621", "vs-101-k2-engine", "h3-005-ltx-relay"}
    return {
        "note": "只整理已核实的两条正文及依赖范围摘要，保留示例分类与其他正文；不改用户自定义库。",
        "items": [{key: item[key] for key in ("id", "source", "source_sha1", "cleaned", "chars")}
                  for item in items if item["id"] in ids],
    }


def apply_prompt_updates(items: list[dict], bodies: dict[str, str]) -> int:
    """原位更新目标条目，保留旧 ID、顺序和所有无关条目；返回新增条数。"""
    existing = {item["id"]: item for item in items}
    added = 0
    for update in prompt_updates():
        previous = existing.get(update["id"])
        replacement = new_item(
            item_id=update["id"], title=update["title"], module=update["module"],
            item_type="rule" if update["module"] == "h3" else "template",
            summary=update["summary"], tags=update["tags"], body=update["body"],
            model=["H3"] if update["module"] == "h3" else [], source=update["source"],
        )
        replacement["has_vars"] = bool(replacement["vars"])
        replacement["order"] = previous["order"] if previous else max(
            (int(item.get("order", 0)) for item in items if item["module"] == update["module"]), default=0
        ) + 1
        if previous is not None:
            previous.update(replacement)
        else:
            items.append(replacement)
            added += 1
        bodies[update["id"]] = update["body"]
    apply_catalog_cleanup(items, bodies)
    return added


def refresh_prompts(out_dir: Path) -> dict:
    """不依赖旧外部素材目录，幂等刷新仓库内置提示词及其索引/报告。"""
    index = read_json(out_dir / "catalog_index.json")
    prompt_bodies = read_json(out_dir / "bodies_prompt.json")
    report = read_json(out_dir / "conversion_report.json")
    items = index["items"]
    added = apply_prompt_updates(items, prompt_bodies)
    bodies = dict(prompt_bodies)
    for tag in ("script", "game", "style"):
        shard = read_json(out_dir / f"bodies_{tag}.json")
        if set(bodies) & set(shard):
            raise ValueError(f"正文分片有重复 ID：{tag}")
        bodies.update(shard)
    for tag in ("prompt", "script", "game", "style"):
        shard = prompt_bodies if tag == "prompt" else read_json(out_dir / f"bodies_{tag}.json")
        expected = {item["id"] for item in items if BODIES_TAG_BY_MODULE[item["module"]] == tag}
        if set(shard) != expected:
            raise ValueError(f"{tag} 正文与索引 ID 集合不相等")
    problems = cross_check(items, bodies)
    if problems:
        raise SystemExit(f"提示词修正后契约不符：{problems}")
    calculated = build_index(items)
    for key in ("ok", "count", "chars", "modules", "types", "items"):
        index[key] = calculated[key]
    counts = report.setdefault("counts", {})
    for key in ("input", "expected_plan"):
        counts[key] = int(counts.get(key, len(items) - added)) + added
    counts["output_index"] = len(items)
    report["catalog_cleanup"] = catalog_cleanup_report(items)
    report["catalog_integrity"] = {"index_body_ids_equal": True, "shards": 4, "count": len(items)}
    report["prompt_updates"] = {
        "ids": [update["id"] for update in prompt_updates()],
        "reference_commit": H3_REFERENCE_COMMIT,
        "note": "保留已有 ID；基础模式和参考编辑独立；图片反推只提取实际可见事实。",
    }
    products = report.setdefault("products", {})
    for name, payload, count in (
        ("catalog_index.json", index, len(items)), ("bodies_prompt.json", prompt_bodies, len(prompt_bodies))
    ):
        products[name] = {**products.get(name, {}), "items": count,
                          "bytes": len(json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"))}
    write_json(out_dir / "catalog_index.json", index)
    write_json(out_dir / "bodies_prompt.json", prompt_bodies)
    write_json(out_dir / "conversion_report.json", report)
    return index


def append_manju(out_dir: Path, source_dir: Path | None = None) -> dict:
    """幂等更新已提交目录；仅写索引、script正文与JSON报告，所有校验先于写入。"""
    manju_items, manju_bodies, rejects, stats, descriptive = manju_family(source_dir)
    index = read_json(out_dir / "catalog_index.json")
    script_bodies = read_json(out_dir / "bodies_script.json")
    report = read_json(out_dir / "conversion_report.json")
    _manju_require(isinstance(index, dict) and isinstance(script_bodies, dict) and isinstance(report, dict),
                   "已有索引/正文/报告必须是对象")
    family_ids = set(manju_bodies)
    old_items = index.get("items")
    _manju_require(isinstance(old_items, list), "已有 items 不是列表")
    _manju_require(not any(it["id"].startswith("script-manju-") and it["id"] not in family_ids for it in old_items),
                   "未知 script-manju ID，需人工复核")
    kept = [it for it in old_items if it["id"] not in family_ids]
    next_order = max((int(it.get("order", 0)) for it in kept if it["module"] == "script"), default=0)
    for offset, item in enumerate(manju_items, 1):
        item["order"] = next_order + offset
    items = kept + manju_items
    script_bodies = {**script_bodies, **manju_bodies}
    bodies = dict(script_bodies)
    for tag in sorted(set(BODIES_TAG_BY_MODULE[it["module"]] for it in items) - {"script"}):
        shard = read_json(out_dir / f"bodies_{tag}.json")
        _manju_require(isinstance(shard, dict), f"bodies_{tag}.json 不是对象")
        bodies.update(shard)
    problems = cross_check(items, bodies)
    _manju_require(not problems, f"增补后契约不符：{problems}")
    # 保留旧索引额外字段、版本/构建日期，以及所有非漫剧条目的字段/顺序。
    calculated = build_index(items)
    for key in ("ok", "count", "chars", "modules", "types", "items"):
        index[key] = calculated[key]
    index.setdefault("sources", {})[MANJU_SOURCE] = stats
    previous = report.setdefault("sources", {}).get(MANJU_SOURCE, {})
    report["sources"][MANJU_SOURCE] = stats
    counts = report.setdefault("counts", {})
    counts["expected_plan"] = int(counts.get("expected_plan", BASE_EXPECTED_TOTAL)) - int(previous.get("expected", 0)) + stats["expected"]
    counts["input"] = int(counts.get("input", len(kept))) - int(previous.get("input", 0)) + stats["input"]
    counts["output_index"] = len(items)
    report["manju_conversion"] = {
        "commit": MANJU_COMMIT, "input": stats["input"], "output": stats["output"],
        "note": "逐份保留五阶段、默认表、题材节奏、企划13项、格式示例与内容尺度；只作聊天适配，不自动生成媒体。",
        "content_review": "21份正文敏感硬标记均0命中；保留原文内容尺度。部分题材存在少年或年龄隐去措辞，不等同明确成年与平台合规保证，需使用者复核。",
        "content_risks": [
            "玄幻原文对受刑/战斗中的未成年人仅规定不写年龄数字、回忆只称少年，不能视为明确成年保证；武侠也保留少年时期回忆，实际创作仍需审查。",
            "保留原文复仇、家暴后果、自杀案件、下毒暗示等虚构剧情及其限制；敏感词0命中不等于生成内容必然安全。",
            "原文的合规建议及民俗怪谈人为解释规则不是平台审核通过保证；本预设仅生成文字制作材料，不自动出图或生成视频。",
        ],
        "audit": stats["audit"],
    }
    products = report.setdefault("products", {})
    # 与 write_json 完全同序列化口径，报告产品字节数不含自身，不形成递归。
    products["catalog_index.json"] = {**products.get("catalog_index.json", {}), "items": len(items),
                                      "bytes": len(json.dumps(index, ensure_ascii=False, indent=1).encode("utf-8"))}
    products["bodies_script.json"] = {**products.get("bodies_script.json", {}), "items": len(script_bodies),
                                      "bytes": len(json.dumps(script_bodies, ensure_ascii=False, indent=1).encode("utf-8"))}
    # 其它来源的 rejects/descriptive_mentions/anomalies 与产品记录原样保留。
    write_json(out_dir / "catalog_index.json", index)
    write_json(out_dir / "bodies_script.json", script_bodies)
    write_json(out_dir / "conversion_report.json", report)
    return index


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="工作台预设资产转换器")
    parser.add_argument("--sources-dir", help="完整构建必需；旧四套JSON的源目录")
    parser.add_argument("--append-manju", action="store_true", help="幂等增补已存在catalog，只写漫剧相关三份JSON")
    parser.add_argument("--refresh-prompts", action="store_true", help="幂等刷新内置规则及已核实模板清理，不需旧来源目录")
    parser.add_argument("--manju-dir", help="漫剧包根目录，默认仓库 skills/ai-manju-skills")
    parser.add_argument("--out-dir", default="public/studio")
    parser.add_argument("--report-md", help="可选：完整构建额外写Markdown报告；增补模式不写文档")
    args = parser.parse_args()

    here = Path(__file__).resolve().parents[1]
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = here / out_dir
    if args.refresh_prompts:
        index = refresh_prompts(out_dir)
        print(f"提示词修正完成；catalog {index['count']} 条 -> {out_dir}")
        return
    manju_dir = Path(args.manju_dir) if args.manju_dir else None
    if manju_dir is not None and not manju_dir.is_absolute():
        manju_dir = here / manju_dir
    if args.append_manju:
        index = append_manju(out_dir, manju_dir)
        print(f"manju 21 条；catalog {index['count']} 条；script {index['modules']['script']} 条 -> {out_dir}")
        return
    if not args.sources_dir:
        parser.error("完整构建需要 --sources-dir；增补已有索引使用 --append-manju")
    source_dir = Path(args.sources_dir)
    if not source_dir.is_absolute():
        source_dir = here / source_dir
    manju_items, manju_bodies, manju_rejects, manju_stats, manju_desc = manju_family(manju_dir)

    prompt_items, prompt_bodies, prompt_rejects, prompt_stats, prompt_desc = prompt_family(source_dir)
    script_items, script_bodies, script_rejects, script_stats, script_desc = script_family(source_dir)
    game_items, game_bodies, game_rejects, game_stats, game_placeholders, game_desc = game_family(source_dir)
    style_items, style_bodies, style_rejects, style_stats, style_desc = style_family(source_dir)

    for item in manju_items:
        item["order"] += max((it["order"] for it in script_items), default=0)
    items = prompt_items + script_items + manju_items + game_items + style_items
    bodies = {**prompt_bodies, **script_bodies, **manju_bodies, **game_bodies, **style_bodies}
    added_prompts = apply_prompt_updates(items, bodies)
    rejects = prompt_rejects + script_rejects + manju_rejects + game_rejects + style_rejects

    problems = cross_check(items, bodies)
    if problems:
        raise SystemExit(f"契约校验失败：{json.dumps(problems, ensure_ascii=False)}")

    index = build_index(items)
    descriptive_mentions = prompt_desc + script_desc + manju_desc + game_desc + style_desc
    index["sources"] = {
        "qiyuan_assets.prompt_library": omit_key(prompt_stats["qiyuan_assets.prompt_library"], "source"),
        "chat_studio_extra": omit_key(prompt_stats["chat_studio_extra"], "source"),
        "qiyuan_assets.novel_templates": script_stats,
        "qiyuan_assets.game_prompt_templates": game_stats,
        "qiyuan_assets.style_libs": style_stats,
        MANJU_SOURCE: manju_stats,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    files = OrderedDict()
    files["catalog_index.json"] = {"items": len(items), "bytes": write_json(out_dir / "catalog_index.json", index)}
    for tag in ("prompt", "script", "game", "style"):
        part = {it["id"]: bodies[it["id"]] for it in items if BODIES_TAG_BY_MODULE[it["module"]] == tag}
        name = f"bodies_{tag}.json"
        files[name] = {"items": len(part), "bytes": write_json(out_dir / name, part)}

    input_total = sum(
        f"{pack}_index.json" and read_json(source_dir / f"{pack}_index.json").get("count") or (len(read_json(source_dir / f"{pack}_index.json").get("items") or []))
        for pack in ("qiyuan_prompt_library", "chat_extra")
    )
    input_total += script_stats["input"] + manju_stats["input"] + game_stats["input"] + style_stats["input"] + added_prompts
    sensitive_rejects = [r for r in rejects if r.get("sensitive")]
    empty_rejects = [r for r in rejects if "空正文" in (r.get("reason") or [])]
    dupe_rejects = [r for r in rejects if "重复ID" in (r.get("reason") or [])]

    report = OrderedDict([
        ("generated_at", BUILD_STAMP),
        ("counts", {
            "expected_plan": EXPECTED_TOTAL + added_prompts,
            "input": input_total,
            "output_index": len(items),
            "rejected": len(rejects),
            "sensitive": len(sensitive_rejects),
            "empty": len(empty_rejects),
            "dupes": len(dupe_rejects),
        }),
        ("sources", index["sources"]),
        ("descriptive_mentions", [
            {"id": row["id"], "source": row["source"], "markers": row["markers"]}
            for row in descriptive_mentions
        ]),
        ("products", {name: meta for name, meta in files.items()}),
        ("anomalies", {
            "note": "主库 4 条（vs-008-qwen50 / vs-027-xh2 / vs-029-guofeng-coll1 / vs-037-daily0621）"
                    "原型截段 chars 与启元清洗后正文不一致，以启元清洗后正文与 chars 为准（cleaned 清单在条目里）",
            "game_vars_placeholder_keys": sorted(set(game_placeholders))[:24],
        }),
        ("rejects", [
            {
                "id": r.get("id") or "",
                "source": r.get("source") or "",
                "reason": r.get("reason") or [],
                "sensitive": r.get("sensitive") or [],
            }
            for r in rejects
        ]),
    ])
    report_path = out_dir / "conversion_report.json"
    if report_path.exists():
        previous_report = read_json(report_path)
        # 完整重建刷新权威统计，但不抹掉用户或增补流程记录的其它信息。
        for key in ("counts", "sources", "products", "anomalies"):
            report[key] = {**previous_report.get(key, {}), **report[key]}
        report = {**previous_report, **report}
    write_json(report_path, report)
    index = refresh_prompts(out_dir)
    if not args.report_md:
        print(f"index {index['count']} 条 -> {out_dir / 'catalog_index.json'}；JSON报告 -> {report_path}")
        return

    lines = [
        "# Cat Chat 工作台（Studio）预设资产转换报告",
        "",
        f"> 本文件由 `verify\\\\studio_catalog_build.py` 自动生成（锚定 {BUILD_STAMP}）；数字一律以命令执行结果为准。",
        "",
        "## 汇总",
        "",
        f"- 计划预期 {EXPECTED_TOTAL}；转换输入 {input_total}；索引条目 {len(items)}；"
        f"拒收 {len(rejects)}（敏感 {len(sensitive_rejects)}、空正文 {len(empty_rejects)}、重复 {len(dupe_rejects)}）",
        f"- 最终可检索数（catalog count）：{index['count']}",
        "",
        "## 各来源",
        "",
    ]
    for name, stats in index["sources"].items():
        lines.append(
            f"- {name}：输入 {stats.get('input', 0)}，输出 {stats.get('output', 0)}，"
            f"拒收 {stats.get('rejected', 0)}，重复 {stats.get('dupes', 0)}"
            + (f"，敏感样例 {', '.join(stats['sensitive_ids'])}" if stats.get("sensitive_ids") else "")
            + (f"，空正文样例 {', '.join(stats['empty_ids'])}" if stats.get("empty_ids") else "")
        )
    lines += ["", "## 产物", ""]
    lines += [f"- `{name}`：{meta['items']} 条 / {meta['bytes']} 字节" for name, meta in files.items()]
    lines += [
        "",
        "## 异常与清理说明",
        "",
        f"- {report['anomalies']['note']}",
        *(
            f"- 描述性提及（人工复核保留）：`{row['id']}` {row['source']} 命中 {'、'.join(row['markers'])}"
            for row in descriptive_mentions
        ),
        "- chars/snippet/has_vars/枚举/ID 唯一性等契约一致性由转换器 cross_check 把关，任一不连贯直接失败。",
        "- 模块->正文分片：vs/h3/vi/ec/global->bodies_prompt.json；script->bodies_script.json；"
        "game->bodies_game.json；style->bodies_style.json（按需懒加载）。",
    ]
    md_path = Path(args.report_md)
    if not md_path.is_absolute():
        md_path = here / md_path
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")

    print(f"index {index['count']} 条 -> {out_dir / 'catalog_index.json'}")
    for name, meta in files.items():
        print(f"  {name}: {meta['items']} 条 / {meta['bytes']} 字节")
    print(f"rejected {len(rejects)}（sensitive={len(sensitive_rejects)}, empty={len(empty_rejects)}, dupes={len(dupe_rejects)}）")
    print(f"report -> {md_path}")


def omit_key(d: dict, key: str) -> dict:
    return {k: v for k, v in d.items() if k != key}


if __name__ == "__main__":
    main()
