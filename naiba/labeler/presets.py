"""打标生成模式（七档）：选什么，描述就忽略什么。

LoRA 训练的核心口径：**描述里不包含的内容，才是模型要学习的内容**。
所以每一档产出的不是「要重点写什么」，而是「要刻意不写什么」：

- ``ignore_prompt``：追加给模型的忽略说明（前端历史字段名 ``custom_prompt`` 继续
  返回同一份文本，避免任务配置与指纹改名）；
- ``filter_keywords``：模型没听话、仍然输出了该领域词时，落库前直接丢弃；
- ``filter_strength``：丢弃力度。

不含任何密钥、不承担引擎路由；``preset`` 只进 ``config_fingerprint``。
用户自己填的「补充要求」永远追加在预设之后，预设不覆盖用户输入。
"""

from __future__ import annotations

import re
from typing import Any

# 通用垃圾标签：模型很爱复读的无信息词（与具体画面无关）。
_COMMON_NOISE = (
    "high quality", "highly detailed", "best quality", "masterpiece", "ultra detailed",
    "4k", "8k", "hd", "uhd", "detailed", "realistic", "photo", "photograph", "image",
    "picture", "artwork", "art", "illustration", "render", "rendering", "ai generated",
    "watermark", "signature", "text", "logo", "blurry", "jpeg artifacts",
    "高清", "超清", "杰作", "高质量", "细节丰富", "插画", "照片", "图片", "水印", "文字",
)

# 各档要忽略的领域词：中英都收，命中即丢（子串匹配，见 filter_tags）。
_FACE_WORDS = (
    "face", "facial", "eyes", "eye", "eyebrows", "eyelashes", "nose", "lips", "mouth",
    "teeth", "smile", "smiling", "makeup", "make-up", "skin", "cheeks", "chin", "jaw",
    "forehead", "portrait", "expression",
    "脸", "面部", "五官", "眼睛", "眼神", "眼妆", "眉毛", "鼻子", "嘴唇", "嘴角", "笑容",
    "微笑", "妆容", "皮肤", "脸颊", "下巴", "额头", "肖像", "表情",
)
_BODY_WORDS = (
    "body", "figure", "pose", "posture", "gesture", "standing", "sitting", "lying",
    "walking", "running", "arms", "legs", "hands", "fingers", "shoulders", "waist",
    "hips", "muscular", "slim", "curvy",
    "体型", "身材", "体态", "姿态", "姿势", "动作", "站姿", "坐姿", "躺姿", "手臂", "双腿",
    "双手", "手指", "肩膀", "腰", "臀", "比例",
)
_STYLE_WORDS = (
    "oil painting", "watercolor", "ink painting", "anime style", "cartoon", "comic style",
    "3d render", "cgi", "cinematic", "film grain", "bokeh", "depth of field", "lighting",
    "backlight", "sunlight", "shadow", "color palette", "palette", "color grading",
    "tonal", "brushstroke", "brush strokes", "line art", "sketch", "impressionism",
    "surrealism", "style",
    "油画", "水彩", "水墨", "动漫风", "卡通", "漫画风", "3d渲染", "电影感", "胶片感",
    "景深", "虚化", "光影", "光线", "逆光", "阴影", "色调", "配色", "笔触", "线条",
    "画风", "风格", "后期", "质感",
)
_SCENE_WORDS = (
    "background", "scene", "environment", "indoor", "outdoor", "sky", "clouds", "forest",
    "street", "city", "room", "wall", "floor", "nature", "landscape", "seascape",
    "weather", "sunset", "sunrise", "night", "daytime", "interior", "exterior",
    "背景", "场景", "环境", "室内", "室外", "天空", "云", "森林", "街道", "城市", "房间",
    "墙面", "地面", "自然", "风景", "海景", "天气", "日落", "日出", "夜晚", "白天",
    "空间", "氛围",
)
_OBJECT_WORDS = (
    "object", "item", "product", "prop", "thing",
    "物体", "物品", "道具", "产品", "东西",
)
_CONCEPT_WORDS = (
    "action", "pose", "gesture", "emotion", "mood", "expression", "storytelling",
    "narrative", "symbolism", "concept",
    "动作", "姿态", "情绪", "心情", "故事", "叙事", "象征", "概念", "氛围",
)

PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "general",
        "label": "通用描述",
        "description": "完整描述画面里有什么，不刻意忽略任何内容；先看模型发挥时用它。",
        "ignored": "",
        "ignore_prompt": "",
        "filter_keywords": _COMMON_NOISE,
        "filter_strength": "low",
    },
    {
        "id": "face_lora",
        "label": "训练面部 LoRA · 忽略面部",
        "description": "描述里略过脸型、五官、妆容与表情，面部留给模型当作要学的概念。",
        "ignored": "面部（五官、妆容、表情）",
        "ignore_prompt": (
            "忽略面部：不要描述脸型、五官、眼睛、眉毛、鼻子、嘴唇、牙齿、肤色、妆容和表情。"
            "必要时只写「一张脸」或「面部被头发部分遮挡」，把笔墨放在服装、姿态、场景、"
            "光线和其他物体上。"
        ),
        "filter_keywords": _COMMON_NOISE + _FACE_WORDS,
        "filter_strength": "medium",
    },
    {
        "id": "body_lora",
        "label": "训练体型 LoRA · 忽略人体",
        "description": "描述里略过体型、身材比例、姿态与动作，身体留给模型学习。",
        "ignored": "人体（体型、姿态、动作）",
        "ignore_prompt": (
            "忽略人体：不要描述体型、身高、身材比例、姿态、动作与四肢朝向。"
            "把笔墨放在面部特征、发型、服装细节、场景与光线上。"
        ),
        "filter_keywords": _COMMON_NOISE + _BODY_WORDS,
        "filter_strength": "medium",
    },
    {
        "id": "style_lora",
        "label": "训练画风 LoRA · 忽略画风",
        "description": "描述里略过媒介、笔触、配色与光影，画风留给模型学习。",
        "ignored": "画风（媒介、笔触、色调、光影）",
        "ignore_prompt": (
            "忽略画风：不要描述媒介（油画/水彩/动漫/3D 渲染/摄影）、笔触、线条、配色、"
            "色调、光线与后期质感，也不要评价「好看」「精美」。只描述画面里有什么："
            "人物、服装、物体、场景、动作与情绪。"
        ),
        "filter_keywords": _COMMON_NOISE + _STYLE_WORDS,
        "filter_strength": "high",
    },
    {
        "id": "scene_lora",
        "label": "训练场景 LoRA · 忽略场景",
        "description": "描述里略过地点、天气、背景与空间层次，场景留给模型学习。",
        "ignored": "场景（地点、天气、背景环境）",
        "ignore_prompt": (
            "忽略场景：不要描述地点、时间、天气、空间层次、背景环境与整体氛围。"
            "只描述主体人物、服装、发型、物体与动作。"
        ),
        "filter_keywords": _COMMON_NOISE + _SCENE_WORDS,
        "filter_strength": "medium",
    },
    {
        "id": "object_lora",
        "label": "训练物体 LoRA · 忽略主体物件",
        "description": "描述里略过主要物件的品类、材质、颜色与结构；要训练哪个物件，可在补充要求里点名。",
        "ignored": "主体物件（品类、材质、颜色、结构）",
        "ignore_prompt": (
            "忽略主体物件：不要描述画面里最主要那个物件的品类、材质、颜色、结构、纹理"
            "和磨损。如果用户在补充要求里点名了要训练的物件，只忽略那一个；"
            "其他人物、服装、场景可以正常描述。"
        ),
        "filter_keywords": _COMMON_NOISE + _OBJECT_WORDS,
        "filter_strength": "medium",
    },
    {
        "id": "concept_lora",
        "label": "训练概念 LoRA · 忽略动作概念",
        "description": "描述里略过动作、情绪与叙事含义，概念留给模型学习。",
        "ignored": "动作与概念（动作、情绪、叙事）",
        "ignore_prompt": (
            "忽略动作与概念：不要描述人物在做什么、动作方向、情绪状态，也不要解释叙事"
            "或象征含义。只描述画面里稳定可见的元素：人物外观、发型、服装、物体与场景。"
        ),
        "filter_keywords": _COMMON_NOISE + _CONCEPT_WORDS,
        "filter_strength": "medium",
    },
)

PRESET_IDS = tuple(item["id"] for item in PRESETS)
DEFAULT_PRESET = "general"
VALID_STRENGTHS = ("low", "medium", "high")


def preset_list() -> list[dict[str, Any]]:
    """给前端的清单（浅拷贝，避免调用方改到模块常量）。

    ``custom_prompt`` 是历史字段名，值等于 ``ignore_prompt``；前端拿它拼任务配置。
    """
    return [
        {
            "id": item["id"],
            "label": item["label"],
            "description": item["description"],
            "ignored": item["ignored"],
            "custom_prompt": item["ignore_prompt"],
            "filter_strength": item["filter_strength"],
        }
        for item in PRESETS
    ]


def get_preset(preset_id: str) -> dict[str, Any]:
    target = str(preset_id or DEFAULT_PRESET).strip() or DEFAULT_PRESET
    for item in PRESETS:
        if item["id"] == target:
            return item
    raise ValueError(f"未知的生成模式：{target}")


def _normalize_tag(tag: str) -> str:
    return re.sub(r"\s+", " ", str(tag or "").strip().lower()).strip(" ,，、;；")


def normalize_tag(tag: str) -> str:
    """公开给 runner 的归一化口径（与过滤时完全一致）。"""
    return _normalize_tag(tag)


def filter_tags(tags: list[str], *, preset: dict[str, Any] | None = None,
                strength: str = "", extra_keywords: list[str] | None = None) -> list[str]:
    """按生成模式的忽略词与力度清理标签（去重、去空、去无信息词，保持原顺序）。"""
    preset = preset or {}
    level = str(strength or preset.get("filter_strength") or "medium")
    if level not in VALID_STRENGTHS:
        level = "medium"
    keywords = {_normalize_tag(word) for word in (preset.get("filter_keywords") or ())}
    keywords.update(_normalize_tag(word) for word in (extra_keywords or ()))
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags or []:
        tag = _normalize_tag(raw)
        if not tag or tag in seen:
            continue
        matched = tag in keywords or any(word and word in tag for word in keywords)
        # low：只丢完全命中的；medium：命中即丢；high：再加上短词兜底（<=2 字符的泛词）。
        if matched and level != "low":
            continue
        if level == "high" and len(tag) <= 2:
            continue
        seen.add(tag)
        out.append(tag)
    return out
