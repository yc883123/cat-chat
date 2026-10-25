"""打标生成模式（九档）：选什么，描述就忽略什么。

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
    "hips", "muscular", "slim", "slender", "curvy",
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
# 「忽略动作」只收动作本身，不收情绪/叙事——情绪不是动作，描述里保留它不影响学动作。
_ACTION_WORDS = (
    "action", "motion", "movement", "gesture", "dynamic",
    "running", "walking", "jumping", "dancing", "fighting", "flying", "falling",
    "sitting", "standing", "lying", "kneeling", "crouching", "leaning",
    "reaching", "holding", "waving", "hugging", "kissing", "turning",
    "动作", "姿态", "姿势", "手势", "运动", "动态", "奔跑", "跑步", "行走", "跳跃",
    "舞蹈", "跳舞", "打斗", "战斗", "拥抱", "亲吻", "挥手", "伸手", "举起", "转身",
    "回眸", "坐姿", "站姿", "躺姿", "蹲姿", "俯身", "倾斜",
)
# 人物档在面部 + 人体之外，再收发型与年龄：这三类合起来才是「这个人的长相」。
_HAIR_AGE_WORDS = (
    "hair", "hairstyle", "haircut", "bangs", "ponytail", "braid", "beard", "mustache",
    "age", "young", "teenager", "adult", "elderly",
    "头发", "发型", "发色", "长发", "短发", "卷发", "直发", "马尾", "刘海", "辫子",
    "胡须", "胡子", "年龄", "年轻", "年长", "少年", "少女",
)
# 服装档：品类、面料、图案结构、配饰。不收颜色——颜色在人物/场景里也用得上。
_OUTFIT_WORDS = (
    "clothing", "clothes", "outfit", "garment", "attire", "costume", "uniform",
    "dress", "shirt", "blouse", "sweater", "hoodie", "jacket", "coat", "raincoat",
    "skirt", "pants", "trousers", "jeans", "shorts", "suit", "vest", "robe",
    "kimono", "cheongsam", "qipao", "lingerie", "underwear", "stockings", "socks",
    "shoes", "boots", "heels", "sandals", "gloves", "scarf", "belt", "necktie",
    "ribbon", "fabric", "textile", "silk", "cotton", "leather", "denim", "velvet",
    "knit", "lace", "embroidery", "buttons", "zipper", "collar", "sleeve", "hemline",
    "pleat", "accessory", "accessories", "jewelry", "necklace", "earrings",
    "bracelet", "handbag", "backpack", "hat", "glasses",
    "服装", "衣服", "服饰", "衣着", "穿搭", "上衣", "外套", "大衣", "风衣", "衬衫",
    "毛衣", "卫衣", "裙子", "短裙", "长裙", "连衣裙", "裤", "牛仔裤", "短裤", "西装",
    "制服", "礼服", "旗袍", "汉服", "和服", "内衣", "丝袜", "袜子", "鞋子", "靴子",
    "高跟鞋", "手套", "围巾", "腰带", "领带", "丝带", "蕾丝", "面料", "布料", "丝绸",
    "棉", "皮革", "牛仔", "天鹅绒", "针织", "刺绣", "纽扣", "拉链", "领口", "袖口",
    "袖子", "裙摆", "褶皱", "配饰", "首饰", "项链", "耳环", "手链", "戒指", "手提包",
    "背包", "帽子", "眼镜",
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
        "id": "character_lora",
        "label": "训练人物 LoRA · 忽略人物",
        "description": "描述里略过长相、发型、体型与年龄，人物本身留给模型学习；指代人物时只写「一个人」。",
        "ignored": "人物（长相、五官、发型、体型、年龄）",
        "ignore_prompt": (
            "忽略人物本身：不要描述长相、五官、肤色、发型发色、体型身材与年龄，"
            "也不要评价美丑。需要指代人物时只写「一个人」「人物」这类泛称，把笔墨放在服装、"
            "动作、场景、光线和其他物体上。"
        ),
        "filter_keywords": _COMMON_NOISE + _FACE_WORDS + _BODY_WORDS + _HAIR_AGE_WORDS,
        "filter_strength": "medium",
    },
    {
        "id": "face_lora",
        "label": "训练面部 LoRA · 忽略面部",
        "description": "描述里略过脸型、五官、妆容与表情，面部留给模型学习。",
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
        "id": "outfit_lora",
        "label": "训练服装 LoRA · 忽略服装",
        "description": "描述里略过服装的品类、面料、图案与配饰，服装留给模型学习。",
        "ignored": "服装（品类、面料、图案、配饰）",
        "ignore_prompt": (
            "忽略服装：不要描述衣服的品类（裙子、衬衫、外套、制服等）、面料材质、图案花纹、"
            "版型剪裁与配饰（帽子、眼镜、首饰、包）。需要指代穿着时只写「一身衣服」"
            "「穿着服装」，把笔墨放在人物特征、动作、场景与光线上。"
        ),
        "filter_keywords": _COMMON_NOISE + _OUTFIT_WORDS,
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
        "id": "action_lora",
        "label": "训练动作 LoRA · 忽略动作",
        "description": "描述里略过人物在做什么、动作方向与姿态变化，动作留给模型学习。",
        "ignored": "动作（动作过程、手势、姿态变化）",
        "ignore_prompt": (
            "忽略动作：不要描述人物在做什么、动作方向与过程、手势、身体运动轨迹与姿态变化"
            "（例如「奔跑」「挥手」「拥抱」「转身」），也不要解释画面讲的故事。只描述人物外观、"
            "服装、物体、场景与画面风格；情绪和氛围可以正常写。"
        ),
        "filter_keywords": _COMMON_NOISE + _ACTION_WORDS,
        "filter_strength": "medium",
    },
)

PRESET_IDS = tuple(item["id"] for item in PRESETS)
DEFAULT_PRESET = "general"
VALID_STRENGTHS = ("low", "medium", "high")

# 旧 id 只读兼容：历史任务、续跑配置与旧标注里还存着 ``concept_lora``（已改名 action_lora）。
# 只在读取时映射，不写回库；导出仍能看到当时真实用的档位。
LEGACY_PRESET_ALIASES: dict[str, str] = {"concept_lora": "action_lora"}


def canonical_preset_id(preset_id: Any) -> str:
    """把历史 id 映射到现行 id；空值返回空串（默认档由调用方决定）。"""
    target = str(preset_id or "").strip()
    return LEGACY_PRESET_ALIASES.get(target, target)


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
    target = canonical_preset_id(preset_id) or DEFAULT_PRESET
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
