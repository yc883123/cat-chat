"""自动压缩上下文（docs/plans/2026-10-26-自动压缩上下文计划.md）。

把较早历史替换为一份**模型生成的交接摘要**，让长会话在接近模型窗口时仍能继续。

设计要点：
- **不引入摘要模型**：总结请求用**当前对话这一轮实际选中的 profile**（同一个 provider /
  同一个 model / 同一份配置），只多发一次请求，不请求另一个模型、不需要任何额外配置。
- **隐藏请求**：不走 agent 循环、不挂工具（``options`` 里没有 ``tools`` 键）、不流式，
  只把「将被压缩的历史 + 已有的旧摘要」交给模型，要求输出结构化交接信息。
- **定稿即冻结**：摘要只在触发整理时生成一次并写进标记；之后每轮从标记里原样重放，
  不在每轮重写。系统提示 / 工具定义 / 摘要消息模板都保持稳定。
- **旧摘要不丢**：再次整理时，被整理区间的模型视图天然以旧摘要开场（旧标记就在区间内，
  由 ``build_model_history`` 还原），所以交给模型的是「旧摘要 + 新进入区间的历史」，
  不是只总结原始消息。
- **保留完整轮次**：保留最近 N 个完整轮次（一条 user 消息及其后的全部内容）原文，
  不按消息行数硬切。
- **失败 fail-open**：请求失败、输出不合格（缺分区 / 过短 / 没变短）都不写标记，
  本轮按原历史继续。

纯逻辑与编排都在这里；``run/chat.py`` 负责在 ``build_model_history`` 之后调用。
"""

from __future__ import annotations

import re
import time
from typing import Any

from naiba.core.history import build_model_history, local_brain
from naiba.core.messages import MetadataKeys

MODE_AUTO = "auto"
MODE_REMIND = "remind"
MODE_OFF = "off"
VALID_MODES = (MODE_AUTO, MODE_REMIND, MODE_OFF)

# 全局设置（config.data["auto_compaction"]）默认值。
# threshold 取 70（比既有「提醒阈值」80 更早）：整理要额外发一次总结请求、之后还要正常
# 回复，触发点必须提前，否则总结请求自己就可能撞上窗口。
COMPACT_DEFAULTS: dict[str, Any] = {
    "mode": MODE_AUTO,
    "threshold_percent": 70,
    "retain_turns": 3,
    "summary_max_tokens": 2048,
}
SETTING_KEYS = frozenset(COMPACT_DEFAULTS)
# 旧版（本地确定性抽取）用过这个键：允许读取但不再生效，避免老配置报「不支持的字段」。
LEGACY_SETTING_KEYS = frozenset({"digest_max_chars", "retain_messages"})

# 摘要正文的长度闸门：太短说明没按要求输出，太长说明它在抄原文（白花一次请求）。
MIN_SUMMARY_CHARS = 120
MAX_SUMMARY_CHARS = 20000
# 交接摘要必须覆盖的分区关键词：至少命中这么多个才算合格（容忍措辞差异，不卡死标题）。
REQUIRED_SECTION_KEYWORDS: tuple[str, ...] = (
    "目标", "约束", "事实", "决定", "进度", "待办", "路径", "不确定",
)
MIN_SECTION_HITS = 4

# 区间下限：太小不值得花一次请求，也不该重置前缀缓存。
MIN_REGION_MESSAGES = 2
MIN_REGION_TOKENS = 1200
# 给「总结请求本身 + 正常回复」预留的 token（触发点据此提前）。
SUMMARY_OUTPUT_SLACK = 1024
# 估算时给「钩子位置尚未组装完的系统提示 + 技能注入」留的余量（token）。
SYSTEM_ESTIMATE_SLACK = 4096


def resolve_settings(raw: Any) -> dict[str, Any]:
    """合并默认值并校验；非法值按默认值处理（设置页有白名单校验，这里兜底）。"""
    merged = dict(COMPACT_DEFAULTS)
    if isinstance(raw, dict):
        merged.update(raw)
    mode = str(merged.get("mode") or COMPACT_DEFAULTS["mode"]).strip().lower()
    if mode not in VALID_MODES:
        mode = str(COMPACT_DEFAULTS["mode"])
    merged["mode"] = mode
    for key, low, high in (("threshold_percent", 30, 95), ("retain_turns", 1, 20),
                           ("summary_max_tokens", 256, 16384)):
        try:
            merged[key] = max(low, min(high, int(merged[key])))
        except (TypeError, ValueError):
            merged[key] = COMPACT_DEFAULTS[key]
    return merged


def effective_mode(settings: dict[str, Any], conversation: Any) -> str:
    """会话级覆盖（P2 字段，先留口径）：空 = 跟全局。"""
    override = ""
    if isinstance(conversation, dict):
        override = str(conversation.get("compaction_mode") or "").strip().lower()
    if override in VALID_MODES:
        return override
    return str(settings.get("mode") or MODE_AUTO)


# ---- 区间切分（按完整轮次）----

def retain_boundary(rows: list[Any], retain_turns: int) -> int:
    """被整理区间最后一行的下标：保留最近 ``retain_turns`` 个**完整轮次**。

    一轮 = 一条 user 消息及其后的全部内容（assistant 回复、工具调用、插话等），
    到下一条 user 消息为止。按轮次切而不是按消息行数切，避免把一轮里的
    提问和答复劈成两半（那会让摘要丢结论、或让保留区缺前提）。
    区间为空（历史轮次不足）返回 -1。
    """
    turns = max(1, int(retain_turns or 1))
    user_indices = [
        index for index, row in enumerate(rows)
        if isinstance(row, dict) and str(row.get("role") or "") == "user"
    ]
    if len(user_indices) <= turns:
        return -1
    # 保留区从「倒数第 turns 条 user 消息」开始，所以区间停在这条 user 的**前一行**。
    return user_indices[-turns] - 1


def should_compact(
    settings: dict[str, Any], *, used: int, limit: int, reserve: int = 0,
) -> bool:
    """估算用量是否越线（limit<=0 视为未知窗口，不触发）。

    触发点 = min(阈值百分比, 窗口 - 预留)，其中预留要覆盖「总结请求的输出 + 本轮正常回复」，
    这样整理完成后本轮还有空间跑完，不会整理完立刻又撞窗口。
    """
    if limit <= 0:
        return False
    percent = max(30, min(95, int(settings["threshold_percent"])))
    by_percent = int(limit) * percent // 100
    headroom = int(settings.get("summary_max_tokens") or 0) + max(0, int(reserve)) + SUMMARY_OUTPUT_SLACK
    ceiling = max(1, int(limit) - headroom)
    return int(used) >= min(by_percent, ceiling)


# ---- 估算（不编码图片、不组请求，只读消息原文与附件元数据）----

def estimate_text_tokens(text: Any) -> int:
    """与 ``SkillAgent._estimate_content_tokens`` 同一条启发式（ASCII 4 字符≈1 token，
    其余 1 字符≈1 token）。只用于「值不值得整理」的判断，不参与任何请求字节。"""
    value = str(text or "")
    if not value:
        return 0
    ascii_chars = sum(1 for char in value if ord(char) < 128)
    return max(1, (ascii_chars + 3) // 4 + (len(value) - ascii_chars))


# 图片在估算里的固定成本，与 SkillAgent 同值（一张图按 1024 token 计）。
IMAGE_ESTIMATE_TOKENS = 1024


def estimate_rows_tokens(rows: list[dict[str, Any]]) -> int:
    """估算一批消息行重放时的 token 量（正文 + 工具结果 + 图片固定成本）。

    **不做图片编码**：整理判断只需量级，编码历史图会白白读盘/压缩一遍。
    """
    total = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        metadata = row.get("metadata") or {}
        total += estimate_text_tokens(row.get("content"))
        for run in metadata.get(MetadataKeys.TOOL_RUNS) or []:
            if isinstance(run, dict):
                total += estimate_text_tokens(run.get("result"))
                total += estimate_text_tokens(run.get("tool"))
        attachments = metadata.get(MetadataKeys.ATTACHMENTS) or []
        if isinstance(attachments, list):
            total += IMAGE_ESTIMATE_TOKENS * sum(
                1 for item in attachments
                if isinstance(item, dict) and _looks_like_image(str(item.get("path") or item.get("name") or ""))
            )
    return total


def _looks_like_image(name: str) -> bool:
    return name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"))


# ---- 总结请求 ----

# 结构化交接分区：总结请求的固定指令。分区名会同时用于格式校验（关键词命中）。
SUMMARIZE_INSTRUCTION = """你现在负责给这段对话做一次交接总结：把上面的内容压缩成一份结构化交接材料，让另一个模型能无缝接续工作。

严格按下面九个分区输出（用 Markdown 二级标题，分区名原样照抄，顺序不变；某个分区没有内容就写“(无)”，绝不能省略分区）：

## 当前目标
- 用户现在到底要完成什么；目标变过的话按时间顺序说明怎么变的。

## 用户约束
- 用户明确提出的限制、偏好、禁止事项、必须遵守的口径（原文关键处直接引用）。

## 已确认事实
- 已经核实过的结论、数据、环境信息（区分「已验证」与「推测」）。

## 已作决定
- 已经拍板的选择及其理由；被否决的方案也要写，避免后面重复讨论。

## 完成进度
- 哪些已经做完并且确认有效。

## 待办事项
- 还没做、做了一半、或需要回头补的事项。

## 关键工具结果与文件路径
- 重要的命令输出结论、文件绝对路径、函数名、配置项、报错原文。
- 路径、标识符、数值、报错字符串必须原样保留，不要改写或省略。

## 尚不确定的事项
- 存疑的结论、待用户确认的问题、可能出错的地方。

## 下一步
- 紧接着该做的第一件事（没有就写“(无)”）。

规则：
- 只输出这份交接材料本身：不要调用任何工具，不要写开场白或结束语。
- 不要提及「总结」「压缩」「摘要」这些动作，也不要说上下文被截断。
- 上面对话开头若已有一份 <compacted-summary> 旧摘要，它是之前整理的结果：保留其中仍然成立的
  信息，把这次新出现的内容合并进去，输出一份完整的合并版，不要只写新增部分，也不要照抄旧摘要。
- 用简洁中文；技术名词、路径、命令、代码片段保持原样。"""


def build_summary_messages(region_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """总结请求的消息：被整理区间的模型视图（含旧摘要）+ 固定指令。

    图片一律降级成文字标记：总结只需要事实与路径，不必让模型重看历史图（省一次
    视觉编码，也避免要求模型具备视觉能力）。
    """
    messages: list[dict[str, Any]] = []
    for item in region_history:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        if role not in {"user", "assistant", "tool"}:
            continue
        content = item.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image":
                    parts.append("[图片]")
                elif part.get("text"):
                    parts.append(str(part["text"]))
            content = "\n".join(part for part in parts if part)
        if not str(content or "").strip() and not item.get("tool_calls"):
            continue
        message = {"role": role, "content": str(content or "")}
        # 工具调用/结果的结构必须带上，否则模型看不出调过什么。
        for key in ("tool_calls", "tool_call_id", "name"):
            if item.get(key):
                message[key] = item[key]
        messages.append(message)
    messages.append({"role": "user", "content": SUMMARIZE_INSTRUCTION})
    return messages


_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n|\n```$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*$", re.M)


def clean_summary(text: Any) -> str:
    """剥掉模型爱包的代码围栏与首尾空白（不做内容改写，摘要原样定稿）。"""
    summary = str(text or "").strip()
    if summary.startswith("```"):
        stripped = _FENCE_RE.sub("", summary).strip()
        if stripped:
            summary = stripped
    return summary.strip()


def validate_summary(summary: str, *, max_chars: int = MAX_SUMMARY_CHARS) -> tuple[bool, str]:
    """格式与长度校验：不合格就不整理（fail-open，宁可这轮不整理也不写坏标记）。

    返回 ``(ok, reason)``。判据：
    - 长度在 [MIN_SUMMARY_CHARS, max_chars] 内；
    - 分区关键词至少命中 MIN_SECTION_HITS 个（容忍模型改写标题措辞）；
    - 至少有两个 Markdown 标题（确认是结构化输出而不是一段散文）。
    """
    if len(summary) < MIN_SUMMARY_CHARS:
        return False, "summary_too_short"
    if len(summary) > max_chars:
        return False, "summary_too_long"
    hits = sum(1 for keyword in REQUIRED_SECTION_KEYWORDS if keyword in summary)
    if hits < MIN_SECTION_HITS:
        return False, f"summary_missing_sections({hits})"
    if len(_HEADING_RE.findall(summary)) < 2:
        return False, "summary_not_structured"
    return True, ""


def build_region_history(
    app: Any, region_rows: list[dict[str, Any]], *, profile: dict[str, Any] | None = None,
    pdf_tools: bool = True, video_tools: bool = True,
) -> list[dict[str, Any]]:
    """把被整理区间的**消息行**还原成模型视图（含旧摘要、含工具调用记录）。

    这是「旧摘要不丢」的关键：旧标记就在区间内，``build_model_history`` 会把它还原成
    摘要消息放在最前面，于是交给模型的自然是「旧摘要 + 新进入区间的历史」。
    """
    options: dict[str, Any] = {}
    config = getattr(app, "config", None)
    for name in ("reasoning_replay_options", "image_encode_cache_options"):
        getter = getattr(config, name, None)
        if callable(getter):
            try:
                options.update(getter() or {})
            except Exception:  # noqa: BLE001 - 配置读取失败退回默认值
                pass
    return build_model_history(
        region_rows, None,
        pdf_tools=bool(pdf_tools), video_tools=bool(video_tools),
        local_image_brain=local_brain(profile or {}),
        **options,
    )


def marker_payload(
    summary: str,
    *,
    source: str,
    model_key: str,
    model_name: str,
    region_messages: int,
    region_tokens: int,
    freed_est: int,
    limit: int,
    used: int,
) -> dict[str, Any]:
    """整理标记内容（存在消息 metadata 的 compaction 键下）。"""
    return {
        "at": int(time.time() * 1000),
        "source": str(source or "auto")[:20],
        "summary": summary,
        # 生成方式：模型隐藏总结（用当前会话同一模型）。
        "generator": "model-summary",
        "model_key": str(model_key or ""),
        "model_name": str(model_name or ""),
        "region_messages": int(region_messages or 0),
        "region_tokens": int(region_tokens or 0),
        "freed_est": int(freed_est or 0),
        "limit": int(limit or 0),
        "used": int(used or 0),
    }


def compact_conversation(
    app: Any,
    *,
    conversation_id: str,
    rows: list[dict[str, Any]],
    profile: dict[str, Any],
    settings: dict[str, Any],
    source: str = "auto",
    model_key: str = "",
    model_name: str = "",
    pdf_tools: bool = True,
    video_tools: bool = True,
    limit: int = 0,
    used: int = 0,
    cancel_event: Any = None,
    force: bool = False,
) -> dict[str, Any]:
    """整理一次：隐藏总结请求 → 校验 → 写标记。

    返回 ``{compacted, reason, region_messages, freed_est, summary}``。
    只有「请求成功 + 校验通过 + 确实变短」才写标记；其余情况一律不动对话。
    """
    boundary = retain_boundary(rows, int(settings.get("retain_turns") or 3))
    if boundary < 0:
        return {"compacted": False, "reason": "region_too_small"}
    region_rows = rows[: boundary + 1]
    if len(region_rows) < MIN_REGION_MESSAGES:
        return {"compacted": False, "reason": "region_too_small"}
    region_tokens = estimate_rows_tokens(region_rows)
    if not force and region_tokens < MIN_REGION_TOKENS:
        return {"compacted": False, "reason": "region_too_small"}
    region_history = build_region_history(
        app, region_rows, profile=profile, pdf_tools=pdf_tools, video_tools=video_tools,
    )
    if not region_history:
        return {"compacted": False, "reason": "region_empty"}
    messages = build_summary_messages(region_history)
    options: dict[str, Any] = {
        "stream": False,
        "max_tokens": int(settings.get("summary_max_tokens") or 2048),
    }
    if cancel_event is not None:
        options["cancel_event"] = cancel_event
    try:
        raw = app.models.complete(profile, messages, options)
    except Exception as exc:  # noqa: BLE001 - 总结失败不阻断本轮对话
        return {"compacted": False, "reason": f"summary_failed:{type(exc).__name__}"}
    summary = clean_summary(raw)
    ok, reason = validate_summary(summary, max_chars=MAX_SUMMARY_CHARS)
    if not ok:
        return {"compacted": False, "reason": reason}
    summary_tokens = estimate_text_tokens(summary)
    freed_est = region_tokens - summary_tokens
    if freed_est <= 0:
        # 摘要没比原文短：写了也没释放空间，还白白让前缀失效。
        return {"compacted": False, "reason": "freed_too_small"}
    marker_row_id = str(region_rows[-1].get("id") or "")
    if not marker_row_id:
        return {"compacted": False, "reason": "missing_marker_row"}
    payload = marker_payload(
        summary, source=source, model_key=model_key, model_name=model_name,
        region_messages=len(region_rows), region_tokens=region_tokens,
        freed_est=freed_est, limit=limit, used=used,
    )
    try:
        if not app.storage.merge_message_metadata(
            conversation_id, marker_row_id, {MetadataKeys.COMPACTION: payload},
        ):
            return {"compacted": False, "reason": "marker_write_failed"}
    except Exception:  # noqa: BLE001 - 存储异常按整理失败处理，不动对话
        return {"compacted": False, "reason": "marker_write_failed"}
    return {
        "compacted": True,
        "region_messages": len(region_rows),
        "freed_est": freed_est,
        "summary": summary,
        "marker_message_id": marker_row_id,
    }
