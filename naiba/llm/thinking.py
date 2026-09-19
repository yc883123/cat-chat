"""思考强度与输出上限的声明式预设层（把 if/else 与子串嗅探变成数据）。

背景
----
改造前「思考强度怎么落到 wire 字段」由 ``protocols._reasoning_params`` 的 6 条 if 分支 +
``_is_kimi_k3_profile`` 的模型名子串嗅探决定：中继改个名字就失效，网关用自有词汇
（``ultra`` / ``xhigh``）无从配置。更糟的是那条 if 链里藏着一个真 bug——``openai_chat``
分支只给 K3 开了特判，K2.x 落到最后一行「原样发 ``reasoning_effort``」，而同文件注释
自己写着「K2.x 收到 ``reasoning_effort`` 会 400」⇒ 对 Kimi K2 会话，把思考强度从默认
``auto`` 主动调到任意一档，首请求必然先 400 一次（能否自愈取决于 400 文案命中既有的
剥字段 fallback）。本模块把承载方式换成数据，那条 bug 随之消失。

三个面
------
- ``format``：这个协议族的思考字段在 wire 上**怎么拼**（挂 provider，因为同一网关方言固定）。
- ``efforts``：**界面档 → 真正发给 API 的值**，``None`` = 该档什么都不发。网关用自有
  词汇也能配，不必改代码发版。
- ``max_output_ceiling``：该端点**最多接受多长**输出（A0 拿它 clamp）。与模型卡已有的
  ``max_output_tokens``（=「用户想要多长」）**不是一回事**，别混。

三条纪律
--------
1. **声明 ≠ 选择**：预设只声明「这个端点允许选哪些档、线上怎么拼」，不替你选默认档。
2. **未声明的档位在发请求前就失败**（不是发出去等 400）。
3. **不跨适配器家族搬字段**。

命中规则与资格约束
------------------
**卡片手填 ``thinking`` > 模型名匹配的预设 > ``request_format`` 默认预设。**
模型名预设仅当其命中条件里的 ``request_format`` **同时满足**时才有资格——例如
``kimi-k3`` 模型走 ``codex_responses`` 中继时**不得**命中 ``kimi_k3``，须落到
``openai_codex``（保住「codex 不做 K3 特判、由中继按 OpenAI 方言翻译」的既有契约）。

与 A0 的关系
------------
预设表里的 ``max_output_ceiling`` 是 A0 取 ``ceiling`` 的**唯一来源**；而「auto 档能不能
填默认输出上限」需要**模型级证据**（本表的 ``match`` 含 ``deepseek`` / ``model_contains``
即为模型级），协议族预设命中**不构成**依据——否则 gpt-4o 这类不思考的模型每请求都会
「填 32768 → 400 → 剥字段重试」，而自愈只在单次请求内生效，是永久的白打一次 400 往返。
"""
from __future__ import annotations

from typing import Any

# 思考字段的 wire 方言（决定怎么拼，不决定值）。
THINKING_FORMAT_OPENAI = "openai"            # {"reasoning_effort": <值>}
THINKING_FORMAT_RESPONSES = "responses"      # {"reasoning": {"effort": <值>}}
THINKING_FORMAT_OLLAMA = "ollama"            # {"think": <值>}
THINKING_FORMAT_LM_STUDIO = "lm_studio"      # {"reasoning": <值>}
THINKING_FORMAT_NONE = "none"                # 不发任何字段
THINKING_FORMATS = frozenset({
    THINKING_FORMAT_OPENAI,
    THINKING_FORMAT_RESPONSES,
    THINKING_FORMAT_OLLAMA,
    THINKING_FORMAT_LM_STUDIO,
    THINKING_FORMAT_NONE,
})

# 界面四档（``auto`` 不是档位：它表示「不发字段、由模型自决」）。
THINKING_EFFORT_KEYS: tuple[str, ...] = ("off", "low", "medium", "high")

# 所有可能出现在请求体里的思考字段名（降档时统一先剥再补）。
THINKING_WIRE_KEYS: tuple[str, ...] = ("reasoning_effort", "reasoning", "think", "thinking")

# llama.cpp / Unsloth 暴露的是 OpenAI Chat API，只是本地推理后端：wire 层统一成 openai_chat。
_OPENAI_CHAT_ALIASES = frozenset({"llama_cpp", "unsloth"})

_ALL_NULL_EFFORTS = {key: None for key in THINKING_EFFORT_KEYS}


class ThinkingConfigError(ValueError):
    """卡片 ``thinking`` 字段或档位声明不合法（发请求前就失败，不等上游 400）。"""


def normalize_request_format(value: Any) -> str:
    """把 provider 的 ``request_format`` 归一化成 wire 协议名。"""
    raw = str(value or "openai_chat").strip().lower()
    return "openai_chat" if raw in _OPENAI_CHAT_ALIASES else raw


def normalize_effort(value: Any) -> str:
    return str(value or "auto").strip().lower()


def is_deepseek_profile(profile: dict[str, Any]) -> bool:
    """是否 DeepSeek 方言画像（模型名或官方域名）。

    DeepSeek 的 OpenAI 兼容端点**不收** ``reasoning_effort``（它从模型本身选择是否思考），
    且要求每一条重放的 assistant 消息带 ``reasoning_content``。
    """
    base_url = str(profile.get("base_url") or "").lower()
    model = str(profile.get("model") or "").lower()
    return "deepseek" in model or "deepseek.com" in base_url


def model_matches(patterns: Any, model: Any) -> bool:
    """模型名（小写子串）是否命中给定模式集合。"""
    value = str(model or "").lower()
    if not value:
        return False
    return any(str(pattern).lower() in value for pattern in (patterns or ()))


# ---------------------------------------------------------------------------
# 内置预设表（跟随代码既有事实，不凭空猜；「待实测」的项一律填 0 = 不注入）
# ---------------------------------------------------------------------------
THINKING_PRESETS: tuple[dict[str, Any], ...] = (
    {
        # DeepSeek Responses API 的 effort 取值：none/low/high/max。
        "id": "deepseek_codex",
        "format": THINKING_FORMAT_RESPONSES,
        "efforts": {"off": "none", "low": "low", "medium": "high", "high": "max"},
        "max_output_ceiling": 32768,
        "match": {"request_format": "codex_responses", "deepseek": True},
    },
    {
        # DeepSeek 从模型本身决定思考；OpenAI 兼容端点拒收 reasoning_effort。
        # 全 null = 「无字段可发」的声明；调用点仍保留一条显式抑制兜底（照 A0 方案）。
        # ceiling 沿用既有 clamp（>393216 会被 DeepSeek 拒）。
        "id": "deepseek",
        "format": THINKING_FORMAT_NONE,
        "efforts": dict(_ALL_NULL_EFFORTS),
        "max_output_ceiling": 393216,
        "match": {"request_format": "openai_chat", "deepseek": True},
    },
    {
        # Kimi K3 官方方言：reasoning_effort 只认 low/high/max（默认 max），且思考永远开启、
        # 无法关闭——off 落到最低档 low 是最贴近的语义。
        "id": "kimi_k3",
        "format": THINKING_FORMAT_OPENAI,
        "efforts": {"off": "low", "low": "low", "medium": "high", "high": "max"},
        # 官方规格 max_completion_tokens 上限 1048576（1M 上下文模型）；
        # teynex 中继实测 1M 接受、10M 撞计费墙（按 max_tokens 预扣费）、int32 上限 500
        # 「max_tokens is invalid」。上限定协议口径 1048576，计费预扣属中继行为、非协议拒绝。
        "max_output_ceiling": 1048576,
        "match": {"request_format": "openai_chat", "model_contains": ("kimi-k3",)},
    },
    {
        # K2.x 用 thinking 参数、收到 reasoning_effort 会 400：并发任何字段（根因 6 的真 bug）。
        "id": "kimi_k2",
        "format": THINKING_FORMAT_NONE,
        "efforts": dict(_ALL_NULL_EFFORTS),
        "max_output_ceiling": 0,
        "match": {"request_format": "openai_chat", "model_contains": ("kimi-k2",)},
    },
    {
        # Ollama 支持布尔值以及 low/medium/high；保留用户选择的强度。
        "id": "ollama",
        "format": THINKING_FORMAT_OLLAMA,
        "efforts": {"off": False, "low": "low", "medium": "medium", "high": "high"},
        # 本地后端的上限由后端自己决定，不注入（0 = 不注入）。
        "max_output_ceiling": 0,
        "match": {"request_format": "ollama"},
    },
    {
        # LM Studio 原生 API 的 reasoning 支持 off/low/medium/high/on。
        "id": "lm_studio",
        "format": THINKING_FORMAT_LM_STUDIO,
        "efforts": {"off": "off", "low": "low", "medium": "medium", "high": "high"},
        "max_output_ceiling": 0,
        "match": {"request_format": "lm_studio"},
    },
    {
        # OpenAI Codex Responses：低/中/高三档。
        # （Kimi K3 无 Responses API；经中继走此格式时按 OpenAI 方言透传，由中继自行翻译，
        #  不做 K3 特判——资格约束保证 kimi-k3 + codex_responses 落到这一行。）
        "id": "openai_codex",
        "format": THINKING_FORMAT_RESPONSES,
        "efforts": {"off": None, "low": "low", "medium": "medium", "high": "high"},
        "max_output_ceiling": 32768,
        "match": {"request_format": "codex_responses"},
    },
    {
        # OpenAI 仅支持 low/medium/high；off 视为不启用（不发送字段）。
        # 位置放在模型级预设之后：否则 kimi_k3 / kimi_k2 / deepseek 会被它先命中。
        "id": "openai",
        "format": THINKING_FORMAT_OPENAI,
        "efforts": {"off": None, "low": "low", "medium": "medium", "high": "high"},
        "max_output_ceiling": 32768,
        "match": {"request_format": "openai_chat"},
    },
    {
        # Anthropic 的 max_tokens 是协议必填（4096 是兼容兜底），首期不发思考强度字段。
        "id": "claude",
        "format": THINKING_FORMAT_NONE,
        "efforts": dict(_ALL_NULL_EFFORTS),
        "max_output_ceiling": 8192,
        "match": {"request_format": "claude"},
    },
    {
        "id": "gemini",
        "format": THINKING_FORMAT_NONE,
        "efforts": dict(_ALL_NULL_EFFORTS),
        "max_output_ceiling": 8192,
        "match": {"request_format": "gemini"},
    },
)

_UNKNOWN_PRESET: dict[str, Any] = {
    "id": "unknown",
    "format": THINKING_FORMAT_NONE,
    "efforts": dict(_ALL_NULL_EFFORTS),
    "max_output_ceiling": 0,
    "source": "none",
    "match": {},
}


def _match_kind(match: dict[str, Any]) -> str:
    """命中条件是「模型级证据」还是「协议族预设」。"""
    if "deepseek" in match or "model_contains" in match:
        return "model"
    return "format"


def _matches(
    match: dict[str, Any],
    request_format: str,
    model: str,
    deepseek: bool,
) -> bool:
    wanted = str(match.get("request_format") or "")
    if wanted and wanted != request_format:
        return False
    if match.get("deepseek") and not deepseek:
        return False
    patterns = match.get("model_contains")
    if patterns and not model_matches(patterns, model):
        return False
    return True


def find_preset(
    request_format: str,
    *,
    model: Any = "",
    deepseek: bool = False,
    kimi_k3: bool = False,
    kimi_k2: bool = False,
) -> dict[str, Any]:
    """按「模型级优先、协议族兜底」命中预设（不含卡片覆盖）。

    ``kimi_k3`` / ``kimi_k2`` 是给旧调用点（``protocols._reasoning_params`` 的历史签名）
    用的显式模型级信号；新代码直接传 ``model`` 即可。
    """
    fmt = normalize_request_format(request_format)
    value = str(model or "").lower()
    if kimi_k3 and "kimi-k3" not in value:
        value = f"{value} kimi-k3".strip()
    if kimi_k2 and "kimi-k2" not in value:
        value = f"{value} kimi-k2".strip()
    for preset in THINKING_PRESETS:
        match = preset.get("match") or {}
        if _matches(match, fmt, value, deepseek):
            resolved = dict(preset)
            resolved["source"] = _match_kind(match)
            return resolved
    return dict(_UNKNOWN_PRESET)


def preset_model_patterns(preset_id: str) -> tuple[str, ...]:
    """某个预设的模型名模式（模型级证据的唯一来源，供协议层转发判定）。"""
    for preset in THINKING_PRESETS:
        if str(preset.get("id") or "") == preset_id:
            return tuple((preset.get("match") or {}).get("model_contains") or ())
    return ()


def _validate_efforts(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ThinkingConfigError("thinking.efforts 必须是「界面档 → 线上值」的对象")
    unknown = [key for key in raw if key not in THINKING_EFFORT_KEYS]
    if unknown:
        raise ThinkingConfigError(
            f"thinking.efforts 含未知档位 {'/'.join(sorted(unknown))}；"
            f"可用档位：{'/'.join(THINKING_EFFORT_KEYS)}"
        )
    efforts: dict[str, Any] = {}
    for key, value in raw.items():
        if value is not None and not isinstance(value, (str, bool, int)):
            raise ThinkingConfigError(f"thinking.efforts.{key} 必须是字符串或 null")
        efforts[key] = value
    return efforts


def _apply_card(preset: dict[str, Any], card: Any) -> dict[str, Any]:
    """把卡片 ``thinking`` 字段覆盖到预设上（三项全可选，不填就走内置预设）。"""
    if card is None or card == "":
        return preset
    if not isinstance(card, dict):
        raise ThinkingConfigError("provider 卡片的 thinking 必须是对象")
    resolved = dict(preset)
    declared_format = card.get("format")
    if declared_format not in (None, ""):
        fmt = str(declared_format).strip().lower()
        if fmt not in THINKING_FORMATS:
            raise ThinkingConfigError(
                f"thinking.format「{declared_format}」不是已知方言；"
                f"可用：{'/'.join(sorted(THINKING_FORMATS))}"
            )
        if card.get("efforts") in (None, ""):
            # 声明了方言却没给词表 ⇒ 直接判非法（这一项是「声明了就必须自洽」的硬要求）。
            raise ThinkingConfigError("thinking 声明了 format 就必须同时给出 efforts")
        resolved["format"] = fmt
    if card.get("efforts") not in (None, ""):
        resolved["efforts"] = _validate_efforts(card.get("efforts"))
    ceiling = card.get("max_output_ceiling")
    if ceiling not in (None, ""):
        try:
            value = int(ceiling)
        except (TypeError, ValueError):
            raise ThinkingConfigError("thinking.max_output_ceiling 必须是整数") from None
        if value < 0:
            raise ThinkingConfigError("thinking.max_output_ceiling 不能为负数（0 = 不注入）")
        resolved["max_output_ceiling"] = value
    resolved["source"] = "card"
    return resolved


def resolve_thinking(
    profile: dict[str, Any] | None,
    request_format: str | None = None,
) -> dict[str, Any]:
    """解析 provider 画像的思考预设（卡片手填 > 模型名 > request_format 默认）。"""
    data = profile if isinstance(profile, dict) else {}
    fmt = normalize_request_format(
        request_format if request_format is not None else data.get("request_format")
    )
    base = find_preset(
        fmt,
        model=data.get("model"),
        deepseek=is_deepseek_profile(data),
    )
    return _apply_card(base, data.get("thinking"))


def thinking_payload(preset: dict[str, Any], effort: Any, *, strict: bool = True) -> dict[str, Any]:
    """把界面档映射成 wire 补丁（``auto`` / 未声明档位返回空对象）。

    ``strict=True`` 时，**界面上有这一档、预设却没声明**直接抛错（发请求前失败）；
    旧入口 ``protocols._reasoning_params`` 用 ``strict=False`` 保持既有容错语义。
    """
    key = normalize_effort(effort)
    if key not in THINKING_EFFORT_KEYS:
        # auto：不发任何强度字段，交由模型/网关自决。
        return {}
    efforts = preset.get("efforts") or {}
    if key not in efforts:
        if strict:
            raise ThinkingConfigError(
                f"该 API 的思考预设（{preset.get('id') or 'unknown'}）未声明「{key}」档，"
                "请改选其它档位或在卡片 thinking.efforts 里补上"
            )
        return {}
    value = efforts[key]
    if value is None:
        return {}
    fmt = str(preset.get("format") or THINKING_FORMAT_NONE)
    if fmt == THINKING_FORMAT_OPENAI:
        return {"reasoning_effort": value}
    if fmt == THINKING_FORMAT_RESPONSES:
        return {"reasoning": {"effort": value}}
    if fmt == THINKING_FORMAT_OLLAMA:
        # bool 是有意义的值（off = False），不能按「假值即不发」处理。
        return {"think": value}
    if fmt == THINKING_FORMAT_LM_STUDIO:
        return {"reasoning": value}
    return {}


def strip_thinking_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """剥掉请求体里已有的思考字段（降档重建负载时先剥再补，避免两种方言残留）。"""
    cleaned = dict(payload)
    for key in THINKING_WIRE_KEYS:
        cleaned.pop(key, None)
    return cleaned


def lower_effort(preset: dict[str, Any], effort: Any) -> str:
    """**按词表声明顺序**下探一档；不可降（off/auto/单档/未知）时返回空串。

    「应用四档减一」的写死映射对只声明 low/high/max 的词表会降到不存在的档
    （例如 K3 的线上档位里没有 medium）。按 ``efforts`` 的**声明顺序**退就没有这个问题，
    同时保住既有语义：退到 ``off``（= 不发字段）不算「降档」，返回空串。
    """
    key = normalize_effort(effort)
    efforts = preset.get("efforts") or {}
    declared = [item for item in THINKING_EFFORT_KEYS if item in efforts]
    if key not in declared:
        return ""
    index = declared.index(key)
    if index == 0:
        return ""
    previous = declared[index - 1]
    if previous == "off" or efforts.get(previous) is None:
        return ""
    return previous


def max_output_ceiling(preset: dict[str, Any]) -> int:
    try:
        return max(0, int(preset.get("max_output_ceiling") or 0))
    except (TypeError, ValueError):
        return 0


def thinking_active(
    profile: dict[str, Any] | None,
    effort: Any,
    *,
    preset: dict[str, Any] | None = None,
) -> bool:
    """现在这一次请求**是否确认会思考**（A0 只在这时填默认输出上限）。

    判据必须是**模型级**，不能是协议族级：
    - ``low`` / ``medium`` / ``high``：用户已明示要思考 ⇒ 填（端点不支持由既有 fallback 处理）；
    - ``off``：永不填；
    - ``auto``：**仅当模型级证据成立**才填——卡片显式配置了 ``thinking``，或命中的预设本身
      是模型级（``kimi-k3`` / DeepSeek 画像等已知思考模型）。协议族预设（``openai`` /
      ``openai_codex`` 默认命中）**不构成**依据：否则 gpt-4o / claude 系这类不思考的模型
      每请求都会完整重演「填 32768 → 400 → 剥字段 → 重试」，白打一次 400 往返、永久如此。
      模型级证据缺失 ⇒ 不填（宁可不兜底，也不换来每请求 400）。
    """
    data = profile if isinstance(profile, dict) else {}
    key = normalize_effort(effort)
    if key in {"low", "medium", "high"}:
        return True
    if key != "auto":
        return False
    if isinstance(data.get("thinking"), dict) and data.get("thinking"):
        return True
    resolved = preset if preset is not None else resolve_thinking(data)
    return str(resolved.get("source") or "") == "model"
