"""打标侧重预设（六档）。

每一档都只产出三样东西：``custom_prompt``（追加给模型的侧重说明）、
``filter_keywords``（命中即丢弃的标签词）、``filter_strength``（丢弃力度）。
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

PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "face_lora",
        "label": "人像脸型",
        "description": "适合训练人物 LoRA：脸型、五官、发型、表情、妆容与服装细节。",
        "custom_prompt": (
            "重点标出与人物身份稳定相关的特征：脸型轮廓、眼睛形状、眉毛、鼻子、嘴唇、"
            "发型与发色、表情、妆容、服装款式与配饰。不要描述背景里无关的路人和杂物。"
        ),
        "filter_keywords": _COMMON_NOISE + ("beautiful", "attractive", "person", "people", "girl", "boy"),
        "filter_strength": "medium",
    },
    {
        "id": "body_lora",
        "label": "人体姿态",
        "description": "体型、姿态、动作、镜头角度与服装结构，适合动作/姿态类训练。",
        "custom_prompt": (
            "重点标出体型与比例、站姿/坐姿/动作、手臂与腿部朝向、镜头角度（正面/侧面/背面/"
            "俯视/仰视）、服装结构与褶皱。不要只给「a woman」这类空泛标签。"
        ),
        "filter_keywords": _COMMON_NOISE + ("beautiful", "attractive", "person", "people"),
        "filter_strength": "medium",
    },
    {
        "id": "style_lora",
        "label": "画风风格",
        "description": "媒介、笔触、配色、光影与构图语言，适合画风/风格迁移。",
        "custom_prompt": (
            "重点标出画面语言：媒介（油画/水彩/赛璐璐/3D 渲染/摄影）、笔触与线条、"
            "主色与配色关系、光线方向与质感、构图方式。不要输出具体人物身份和故事。"
        ),
        "filter_keywords": _COMMON_NOISE + ("beautiful", "nice", "good"),
        "filter_strength": "high",
    },
    {
        "id": "scene_lora",
        "label": "场景环境",
        "description": "地点、时间、天气、空间层次与氛围，适合场景/背景模型。",
        "custom_prompt": (
            "重点标出场景要素：地点类型（室内/街道/森林/海面…）、时间与天气、"
            "前中后景的空间层次、光源类型、整体氛围。忽略画面中人物的身份细节。"
        ),
        "filter_keywords": _COMMON_NOISE + ("beautiful", "scenery", "landscape", "background"),
        "filter_strength": "medium",
    },
    {
        "id": "object_lora",
        "label": "物体道具",
        "description": "主体物件、材质、结构与表面细节，适合产品/道具类模型。",
        "custom_prompt": (
            "重点标出画面主体物件：品类、材质（金属/木/布/玻璃…）、颜色、结构部件、"
            "表面纹理与磨损痕迹。不要泛泛地写「a object」或整段故事描述。"
        ),
        "filter_keywords": _COMMON_NOISE + ("object", "thing", "item"),
        "filter_strength": "medium",
    },
    {
        "id": "concept_lora",
        "label": "概念动作",
        "description": "动作、情绪、叙事元素与超现实概念，适合概念/氛围类训练。",
        "custom_prompt": (
            "重点标出动作与情绪：人物在做什么、动作方向、情绪状态、画面里的关键叙事元素、"
            "超现实或象征性的设定。不要输出无信息的美术套话。"
        ),
        "filter_keywords": _COMMON_NOISE + ("beautiful", "emotional"),
        "filter_strength": "low",
    },
)

PRESET_IDS = tuple(item["id"] for item in PRESETS)
DEFAULT_PRESET = "face_lora"
VALID_STRENGTHS = ("low", "medium", "high")


def preset_list() -> list[dict[str, Any]]:
    """给前端的清单（浅拷贝，避免调用方改到模块常量）。"""
    return [
        {
            "id": item["id"],
            "label": item["label"],
            "description": item["description"],
            "custom_prompt": item["custom_prompt"],
            "filter_strength": item["filter_strength"],
        }
        for item in PRESETS
    ]


def get_preset(preset_id: str) -> dict[str, Any]:
    target = str(preset_id or DEFAULT_PRESET).strip() or DEFAULT_PRESET
    for item in PRESETS:
        if item["id"] == target:
            return item
    raise ValueError(f"未知的打标侧重：{target}")


def _normalize_tag(tag: str) -> str:
    return re.sub(r"\s+", " ", str(tag or "").strip().lower()).strip(" ,，、;；")


def filter_tags(tags: list[str], *, preset: dict[str, Any] | None = None,
                strength: str = "", extra_keywords: list[str] | None = None) -> list[str]:
    """按预设的过滤词与力度清理标签（去重、去空、去无信息词，保持原顺序）。"""
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
