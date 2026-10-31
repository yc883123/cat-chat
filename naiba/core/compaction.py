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
- **失败 fail-open**：请求失败、输出不合格（过短 / 拒答 / 没变短）都不写标记，
  本轮按原历史继续；失败绝不改写、裁剪原历史。
- **恢复有界且只认真实缩减**：摘要请求报「输入超窗口」时，允许把被总结区间向前收窄
  （被收窄掉的部分作为原样消息继续重放，不丢内容）后重试；其他失败一律不重试——
  同一输入、同一提示重发只会再花一次钱。

纯逻辑与编排都在这里；``run/chat.py`` 负责在 ``build_model_history`` 之后调用。
"""

from __future__ import annotations

import re
import time
from typing import Any

from naiba.core.exceptions import TaskCancelled
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
# 拒答识别只看**开头**：交接材料不可能以道歉或「我无法总结」开场；正文里出现
# 「无法登录」「无法处理」这类业务陈述不算拒答（按全文匹配会误杀正常摘要）。
_REFUSAL_APOLOGY_RE = re.compile(r"^\s*(?:好的|好的[，,])?\s*(?:抱歉|对不起|很抱歉|不好意思)")
_REFUSAL_CANNOT_RE = re.compile(
    r"^\s*(?:我|本模型|AI|作为AI)?\s*(?:无法|不能|没法|难以)[^。\n]{0,12}"
    r"(?:总结|摘要|概括|交接|输出|生成)"
)
_REFUSAL_ROLE_RE = re.compile(r"作为(?:一个)?(?:AI|人工智能|语言模型|助手)")

# 摘要请求最多发几次：1 次正常尝试 + 最多 2 次「区间实际收窄后」的恢复。
# 只有「明确报输入超窗口、且区间真的变小了」才允许进入下一次；其余失败一次终止。
MAX_SUMMARY_ATTEMPTS = 3

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


def latest_real_input_tokens(rows: list[dict[str, Any]]) -> int:
    """最近一次**真实**模型请求的输入 token（读落库 usage，与前端圆环同一份数据）。

    触发判定过去只靠字符估算，而估算不含系统提示与工具 schema——真实占用经常先越线
    （圆环已经 80%、触发估算还停在 57%），轮首整理因此系统性晚触发。这里取上一轮最后
    一次请求的真实 ``input_tokens`` 参与判定，与估算取大。

    只认「晚于最近一次整理标记、且在最新会话分割线之后」的 usage：标记/分割线**之前**
    的 usage 是整理前（或已划出上下文）的口径、只偏大——信了它会在整理后的下一轮立刻
    再触发一次整理。没扫到可信值返回 0（调用方退回纯估算）。
    """
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        metadata = row.get("metadata") or {}
        if metadata.get(MetadataKeys.COMPACTION) or metadata.get(MetadataKeys.SESSION_START):
            return 0
        if str(row.get("role") or "") == "session":  # 遗留形态：独立的分割线行
            return 0
        if str(row.get("role") or "") != "assistant":
            continue
        usage = metadata.get(MetadataKeys.USAGE)
        if not isinstance(usage, dict):
            continue
        try:
            value = int(usage.get("input_tokens") or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


# ---- 总结请求 ----

# 交接材料指令：只规定**要交代哪些方面**，不规定固定标题——按关键词命中数卡标题
# 会把措辞不同但内容完整的合格摘要拒之门外（实测多次 summary_missing_sections /
# summary_not_structured 误拒），可用性交给 validate_summary 的长度与拒答闸门。
SUMMARIZE_INSTRUCTION = """你现在负责给这段对话做一次交接：把上面的内容压缩成一份给「接手者」看的交接材料，读它的人要能不看原对话就直接继续工作。

材料需要交代清楚下面这些方面（有内容的写，没有的略过；标题措辞和组织方式自定，不必照抄任何固定格式，但要让人一眼能找到对应内容）：

- 当前的任务目标是什么，中途变过没有；
- 用户定下的要求、限制和偏好（关键处直接引用原话）；
- 已经核实过的事实、数据、环境信息和文件位置；
- 已经定下的决定及理由，包括被否决的方案，避免接手者重复讨论；
- 哪些已经做完并且确认有效；哪些做了一半、失败过、还欠着；
- 重要的命令输出、报错原文、文件路径、函数名、配置项；
- 还没解决的问题、存疑的地方，以及接下来该做的第一件事。

规则：
- 用 Markdown 组织（标题或编号列表都行），不要输出成一大段散文。
- 路径、标识符、数值、报错字符串必须原样保留，不要改写或省略。
- 只输出这份交接材料本身：不要调用任何工具，不要写开场白或结束语。
- 不要提及「总结」「压缩」「摘要」这些动作，也不要说上下文被截断。
- 上面对话开头若已有一份 <compacted-summary> 旧摘要，它是之前交接的结果：保留其中仍然成立的
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


def clean_summary(text: Any) -> str:
    """剥掉模型爱包的代码围栏与首尾空白（不做内容改写，摘要原样定稿）。"""
    summary = str(text or "").strip()
    if summary.startswith("```"):
        stripped = _FENCE_RE.sub("", summary).strip()
        if stripped:
            summary = stripped
    return summary.strip()


def validate_summary(summary: str, *, max_chars: int = MAX_SUMMARY_CHARS) -> tuple[bool, str]:
    """可用性校验：不合格就不整理（fail-open，宁可这轮不整理也不写坏标记）。

    返回 ``(ok, reason)``。只判**能不能用**，不强迫模型逐字输出某套标题——按固定
    分区关键词命中数卡摘要，会反复误拒措辞不同但内容完整的输出（冻结版实测
    ``summary_missing_sections`` / ``summary_not_structured`` 连续失败）。判据：
    - 长度在 [MIN_SUMMARY_CHARS, max_chars] 内；
    - 不是拒答或敷衍（交接材料不会以道歉、「我无法总结」开场）。
    「摘要确实比输入短」由 compact_conversation 的 freed_est 闸门单独把关。
    """
    if len(summary) < MIN_SUMMARY_CHARS:
        return False, "summary_too_short"
    if len(summary) > max_chars:
        return False, "summary_too_long"
    head = summary[:200]
    if _REFUSAL_APOLOGY_RE.match(head) or _REFUSAL_CANNOT_RE.match(head):
        return False, "summary_refusal"
    if _REFUSAL_ROLE_RE.search(head):
        return False, "summary_refusal"
    return True, ""


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """估算一次总结请求的输入规模（诊断记录用，与 estimate_rows_tokens 同一启发式）。"""
    total = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        total += estimate_text_tokens(message.get("content"))
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and isinstance(call.get("function"), dict):
                total += estimate_text_tokens(call["function"].get("arguments"))
    return total


def _last_output_tokens(app: Any) -> int:
    """最近一次模型请求的输出 token（旁路诊断，读不到就记 0，绝不影响主流程）。"""
    usage = getattr(getattr(app, "models", None), "last_usage", None)
    try:
        return int((usage or {}).get("output_tokens") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def _is_cancellation(exc: BaseException, cancel_event: Any) -> bool:
    if isinstance(exc, TaskCancelled):
        return True
    if cancel_event is not None and cancel_event.is_set():
        return True
    return str(exc) == "任务已取消"


def _failure_category(exc: BaseException) -> str:
    """失败的细分类别（只进诊断记录，不改写对外的 summary_failed:<类型> 原因码）。"""
    from naiba.llm.stream import ContextOverflowError, is_context_overflow

    if isinstance(exc, ContextOverflowError) or is_context_overflow(str(exc)):
        return "context_overflow"
    return type(exc).__name__


def shrink_boundary(rows: list[dict[str, Any]], end: int) -> int:
    """把被整理区间的终点向前挪到「中点附近某个完整轮次」的末尾，返回新终点下标。

    总结请求报输入超窗口时的恢复手段：新区间终点**之前的**消息交给模型总结，
    之后的（含被收窄掉的部分）原样重放，不丢任何内容。切点必须卡在某条 user 消息
    的前一行（与 ``retain_boundary`` 同一条不变量），否则重放会出现
    「tool 结果找不到前面的 tool_calls」这类协议断裂。挪不动返回 -1。
    """
    user_indices = [
        index for index, row in enumerate(rows[: end + 1])
        if isinstance(row, dict) and str(row.get("role") or "") == "user"
    ]
    candidates = [index - 1 for index in user_indices if index > 0]
    target = end - max(MIN_REGION_MESSAGES, (end + 1) // 2)
    for candidate in sorted(candidates, key=lambda index: abs(index - target)):
        if candidate < end and candidate + 1 >= MIN_REGION_MESSAGES:
            return candidate
    return -1


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

    返回 ``{compacted, reason, region_messages, freed_est, summary, attempts}``。
    只有「请求成功 + 校验通过 + 确实变短」才写标记；其余情况一律不动对话。

    恢复规则（严格有界，见 MAX_SUMMARY_ATTEMPTS）：
    - 只有「明确报输入超窗口」的失败才允许重试，且每次重试前必须先把被总结区间
      真实收窄（shrink_boundary），输入没变就不重发——同一输入重试只会再花一次钱；
    - 校验失败、其他运行时失败、写标记失败都一次终止，不重试；
    - 每次尝试的输入/输出规模与失败类别记进 ``attempts``（旁路诊断，不进模型请求）。
    """
    boundary = retain_boundary(rows, int(settings.get("retain_turns") or 3))
    if boundary < 0:
        return {"compacted": False, "reason": "region_too_small"}
    region_rows = rows[: boundary + 1]
    if len(region_rows) < MIN_REGION_MESSAGES:
        return {"compacted": False, "reason": "region_too_small"}
    if not force and estimate_rows_tokens(region_rows) < MIN_REGION_TOKENS:
        return {"compacted": False, "reason": "region_too_small"}
    # 输出预算**独立**于正常回复：摘要请求不挂工具、不流式，max_tokens 只来自
    # auto_compaction 设置（summary_max_tokens），不继承本轮对话的输出上限。
    options: dict[str, Any] = {
        "stream": False,
        "max_tokens": int(settings.get("summary_max_tokens") or 2048),
    }
    if cancel_event is not None:
        options["cancel_event"] = cancel_event

    def cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    attempts: list[dict[str, Any]] = []
    # current_rows 恒为 rows 的前缀：收窄只挪终点，标记永远落在 current_rows[-1] 上，
    # 被收窄掉的部分留在重放区，原文一条不丢。
    current_rows = region_rows
    while True:
        if cancelled():
            raise TaskCancelled("任务已取消")
        current_tokens = estimate_rows_tokens(current_rows)
        region_history = build_region_history(
            app, current_rows, profile=profile, pdf_tools=pdf_tools, video_tools=video_tools,
        )
        if not region_history:
            return {"compacted": False, "reason": "region_empty", "attempts": attempts}
        messages = build_summary_messages(region_history)
        input_tokens = estimate_messages_tokens(messages)
        try:
            raw = app.models.complete(profile, messages, options)
        except Exception as exc:  # noqa: BLE001 - 总结失败不阻断本轮对话
            if _is_cancellation(exc, cancel_event):
                raise TaskCancelled("任务已取消") from exc
            category = _failure_category(exc)
            attempts.append({
                "region_messages": len(current_rows),
                "input_tokens": input_tokens,
                "output_tokens": _last_output_tokens(app),
                "reason": category,
            })
            if category == "context_overflow" and len(attempts) < MAX_SUMMARY_ATTEMPTS:
                new_end = shrink_boundary(rows, len(current_rows) - 1)
                if new_end >= 0:
                    current_rows = rows[: new_end + 1]
                    continue  # 输入确实变小了，允许这一次有界恢复
            return {
                "compacted": False,
                "reason": f"summary_failed:{type(exc).__name__}",
                "attempts": attempts,
            }
        summary = clean_summary(raw)
        ok, reason = validate_summary(summary, max_chars=MAX_SUMMARY_CHARS)
        if not ok:
            attempts.append({
                "region_messages": len(current_rows),
                "input_tokens": input_tokens,
                "output_tokens": _last_output_tokens(app),
                "reason": reason,
            })
            # 校验失败不重试：同一输入、同一提示重发不会得到不同结果。
            return {"compacted": False, "reason": reason, "attempts": attempts}
        summary_tokens = estimate_text_tokens(summary)
        freed_est = current_tokens - summary_tokens
        if freed_est <= 0:
            # 摘要没比原文短：写了也没释放空间，还白白让前缀失效。
            attempts.append({
                "region_messages": len(current_rows),
                "input_tokens": input_tokens,
                "output_tokens": _last_output_tokens(app),
                "reason": "freed_too_small",
            })
            return {"compacted": False, "reason": "freed_too_small", "attempts": attempts}
        marker_row_id = str(current_rows[-1].get("id") or "")
        if not marker_row_id:
            return {"compacted": False, "reason": "missing_marker_row", "attempts": attempts}
        payload = marker_payload(
            summary, source=source, model_key=model_key, model_name=model_name,
            region_messages=len(current_rows), region_tokens=current_tokens,
            freed_est=freed_est, limit=limit, used=used,
        )
        try:
            if not app.storage.merge_message_metadata(
                conversation_id, marker_row_id, {MetadataKeys.COMPACTION: payload},
            ):
                return {
                    "compacted": False, "reason": "marker_write_failed", "attempts": attempts,
                }
        except Exception:  # noqa: BLE001 - 存储异常按整理失败处理，不动对话
            return {"compacted": False, "reason": "marker_write_failed", "attempts": attempts}
        attempts.append({
            "region_messages": len(current_rows),
            "input_tokens": input_tokens,
            "output_tokens": _last_output_tokens(app),
            "reason": "accepted",
        })
        return {
            "compacted": True,
            "region_messages": len(current_rows),
            "freed_est": freed_est,
            "summary": summary,
            "marker_message_id": marker_row_id,
            "attempts": attempts,
        }
