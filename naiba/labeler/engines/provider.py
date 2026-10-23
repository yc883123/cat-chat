"""Provider 打标引擎（``engine=provider``）：组多模态消息，调共享 ``app.models.complete()``。

「provider」= 设置里配置的任意模型供应商：云端 OpenAI 兼容端点，也包括 Ollama /
LM Studio / llama.cpp 这类本地后端（同样走 HTTP 协议）。本引擎不关心供应商跑在哪，
也**不会**在应用内加载任何本地小模型（没有 torch / WD14 / JoyCaption）。

职责边界：
- 只负责「一张图 → 一条模型请求 → 解析成标签/描述」；用量、重试、落库由 runner 统一管。
- 不读配置里的密钥：``profile`` 由 runner 经 ``resolve_profile_with_override`` 解析后传入。
- 解析容忍模型跑偏（代码块、前后废话、纯逗号列表），但**不伪造**标签/置信度。
"""

from __future__ import annotations

import json
import re
from typing import Any

from naiba.vision.images import encode_image_file

ENGINE_ID = "provider"

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.S)
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$", re.S)


_LANGUAGE_RULES = {
    "mixed": "输出语言：标签用小写英文名词短语（多个词用空格），description 用中文。",
    "en": "输出语言：全部用英文。标签用小写英文名词短语（多个词用空格），description 也用英文。",
    "zh": "输出语言：全部用中文。标签用中文名词短语，description 也用中文（品牌名、专有名词可保留原文）。",
}


def _language_rule(language: str) -> str:
    return _LANGUAGE_RULES.get(str(language or ""), _LANGUAGE_RULES["mixed"])


def _mode_rule(output_mode: str, language: str) -> str:
    if output_mode == "keywords":
        return '输出形式：只要标签。description 字段固定输出空字符串 ""。'
    if output_mode == "phrases":
        if str(language) == "en":
            return (
                "输出形式：词组短语。把画面内容写成 6-15 个用英文逗号分隔的英文短语，"
                "短语是名词短语，不要完整句子、不要句号、不要编号。"
                'tags 字段固定输出空数组 []，短语全部写进 description。'
            )
        return (
            "输出形式：词组短语。把画面内容写成 6-15 个用中文逗号分隔的短语，"
            "短语是名词短语，不要完整句子、不要句号、不要编号。"
            'tags 字段固定输出空数组 []，短语全部写进 description。'
        )
    if output_mode == "description":
        return (
            "输出形式：自然语言。tags 字段固定输出空数组 []，"
            "description 写 40-120 字（英文为 40-120 词）的连贯描述。"
        )
    return (
        "输出形式：标签 + 自然语言描述。description 写 40-120 字"
        "（英文为 40-120 词）的连贯描述，覆盖主体、构图、风格与光线。"
    )


def build_prompt(config: dict[str, Any], preset: dict[str, Any]) -> str:
    """把生成模式（要忽略什么）+ 输出形式 + 语言 + 补充要求拼成一条稳定指令。

    口径与 LoRA 训练一致：**描述里不包含的内容，才是模型要学习的内容**；
    所以预设段落写成「必须忽略」，而不是「重点描述」。
    """
    output_mode = str(config.get("output_mode") or "keywords")
    language = str(config.get("language") or "mixed")
    max_tags = int(config.get("max_tags") or 24)
    threshold = float(config.get("confidence_threshold") or 0.0)
    parts = [
        "你是图片标注助手。请观察这张图，只输出一个 JSON 对象，不要输出任何解释或代码块标记。",
        'JSON 结构：{"tags": [{"tag": "标签文本", "confidence": 0.0到1.0之间的小数}], '
        '"description": "描述文本"}',
        f"标签最多 {max_tags} 个；confidence 低于 {threshold:g} 的标签不要输出。"
        "标签不要编号、不要整句。",
        _language_rule(language),
        _mode_rule(output_mode, language),
    ]
    ignore = ""
    if isinstance(preset, dict):
        ignore = str(preset.get("ignore_prompt") or preset.get("custom_prompt") or "").strip()
    if ignore:
        parts.append("生成模式（以下内容必须从标签和描述里省略）：" + ignore)
    extra = str(config.get("custom_prompt") or "").strip()
    if extra:
        parts.append("用户补充要求（优先满足，可点名要忽略的元素）：" + extra)
    return "\n".join(parts)


def build_messages(image_path: str, prompt: str) -> tuple[list[dict[str, Any]], str]:
    """返回 (messages, 错误)。图片无法编码时不发请求，把原因交给 runner 记成永久失败。"""
    part = encode_image_file(str(image_path or ""))
    if part is None:
        return [], "图片无法读取或编码（文件可能已被清理）"
    # 与带图生成保持同一顺序：先图后文字，模型不会把两张图的内容混在一起。
    content = [part, {"type": "text", "text": "\n\n" + prompt}]
    return [{"role": "user", "content": content}], ""


def _as_tags(value: Any) -> tuple[list[str], list[float]]:
    tags: list[str] = []
    confidences: list[float] = []
    rows = value if isinstance(value, list) else []
    for row in rows:
        if isinstance(row, dict):
            tag = str(row.get("tag") or row.get("name") or row.get("label") or "").strip()
            confidence = row.get("confidence")
        else:
            tag = str(row or "").strip()
            confidence = None
        if not tag:
            continue
        tags.append(tag)
        try:
            conf = float(confidence)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            conf = -1.0
        # -1 = 模型没给置信度；保留但用 -1 标记，前端显示时不编造数字。
        confidences.append(round(max(0.0, min(1.0, conf)), 3) if conf >= 0 else -1.0)
    return tags, confidences


def _fallback_parse(text: str) -> tuple[list[str], list[float], str]:
    """没有 JSON 时的兜底：按行/逗号切标签，长句当描述。"""
    cleaned = _FENCE_RE.sub("", str(text or "")).strip()
    if not cleaned:
        return [], [], ""
    tags: list[str] = []
    description_lines: list[str] = []
    for line in re.split(r"[\r\n]+", cleaned):
        line = line.strip().strip("-*•").strip()
        if not line:
            continue
        # 一句英文/中文自然语言（含空格且以句号结尾或很长）更像描述。
        if len(line) > 40 or line.endswith(("。", "！", "？", ".", "!", "?")):
            description_lines.append(line)
            continue
        for piece in re.split(r"[,，、;；]", line):
            piece = piece.strip().strip('"\'')
            if piece:
                tags.append(piece)
    return tags, [-1.0] * len(tags), " ".join(description_lines)


def parse_response(text: str) -> dict[str, Any]:
    """解析模型输出：``{tags, confidences, description, parsed}``。"""
    cleaned = _FENCE_RE.sub("", str(text or "")).strip()
    candidate = ""
    match = _JSON_BLOCK_RE.search(cleaned)
    if match:
        candidate = match.group(0)
    payload: dict[str, Any] | None = None
    if candidate:
        try:
            loaded = json.loads(candidate)
            if isinstance(loaded, dict):
                payload = loaded
        except json.JSONDecodeError:
            payload = None
    if payload is None:
        tags, confidences, description = _fallback_parse(cleaned)
        return {
            "tags": tags, "confidences": confidences,
            "description": description, "parsed": "fallback",
        }
    tags, confidences = _as_tags(payload.get("tags"))
    description = str(payload.get("description") or "").strip()
    if not tags and not description:
        tags, confidences, description = _fallback_parse(cleaned)
        return {
            "tags": tags, "confidences": confidences,
            "description": description, "parsed": "fallback",
        }
    return {"tags": tags, "confidences": confidences, "description": description, "parsed": "json"}


class ProviderLabelerEngine:
    """``engine=provider``：一次一张图，返回解析后的标注数据。"""

    id = ENGINE_ID

    def __init__(self, app: Any):
        self.app = app

    def run(
        self,
        *,
        profile: dict[str, Any],
        image_path: str,
        prompt: str,
        options: dict[str, Any],
        status: Any = None,
    ) -> dict[str, Any]:
        messages, error = build_messages(image_path, prompt)
        if error:
            raise ValueError(error)
        text = str(self.app.models.complete(profile, messages, options, status) or "")
        parsed = parse_response(text)
        parsed["raw"] = text
        return parsed
