"""API 用量 Σ 与费用计算（纯常量 + 纯函数，无 IO，符合 core 分层）。

两条口径，**统计与圆环 deliberately 分家**：

- 上下文圆环继续用消息 ``metadata.usage`` 的「最后一次请求」口径
  （``SkillAgent._summarize_usage``，契约不动）——它回答的是「这一轮上下文有多大」；
- 用量统计（``usage_records`` 台账 + ``/api/usage/stats``）用本模块的
  ``tokens_from_summary``——它回答的是「这一轮一共烧了多少 token」，按
  ``requests_detail`` 逐次求和（Σ 全部模型请求）。长 agent 轮次里每次请求都会
  重新发送全部历史，逐次求和才是真实计费量；Σcached/Σinput 的命中率与
  「最后一次请求口径」不可比，所以这里不产出命中率（展示层自行按需计算）。

费用恒按**当前单价**计算、不落价格快照：用户事后补单价，历史用量也能追溯出
费用；改价后历史金额随新价重算（个人工具语义，维护说明 §五.6 已登记）。
"""

from __future__ import annotations

from typing import Any

# 单价语义：每百万 tokens 的价格。三个档位键与 provider.pricing 字段一一对应。
PRICING_KEYS = ("input_per_million", "cached_input_per_million", "output_per_million")
# 未显式设置币种时的默认符号（与前端默认值一致）。
DEFAULT_CURRENCY = "¥"

_TOKENS_SUM_KEYS = (
    "input_tokens",
    "cached_tokens",
    "output_tokens",
    "total_tokens",
    "requests",
)


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def tokens_from_summary(summary: Any) -> dict[str, int]:
    """从一轮的 usage 汇总算「Σ 全部请求」口径的 token 数。

    优先用 ``requests_detail`` 逐次求和（每次请求一条记录）；旧数据没有明细时
    退化为把顶层值当单次请求——顶层是**最后一次请求**口径（见模块 docstring），
    只作兼容降级，不等于真实累计。``requests`` 优先取顶层（它本来就是计数）
    明细存在时取明细条数。
    """
    result = {key: 0 for key in _TOKENS_SUM_KEYS}
    result["uncached_tokens"] = 0
    if not isinstance(summary, dict):
        return result
    details = summary.get("requests_detail")
    if isinstance(details, list) and details:
        for key in _TOKENS_SUM_KEYS:
            if key == "requests":
                result[key] = len(details)
                continue
            result[key] = sum(
                _non_negative_int(item.get(key)) for item in details if isinstance(item, dict)
            )
    else:
        for key in _TOKENS_SUM_KEYS:
            result[key] = _non_negative_int(summary.get(key))
    result["uncached_tokens"] = max(0, result["input_tokens"] - result["cached_tokens"])
    return result


def _pricing_value(pricing: dict[str, Any], key: str) -> float:
    try:
        value = float(pricing.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def cost_for(tokens: dict[str, Any], pricing: Any) -> dict[str, Any] | None:
    """按三档单价算费用；``pricing`` 缺失/三档全空返回 None（未定价，不计入费用）。

    计费口径：``未命中输入/1e6×输入单价 + 缓存命中/1e6×命中单价 + 输出/1e6×输出单价``。
    输入 ``tokens`` 至少要有 ``input_tokens`` / ``cached_tokens`` / ``output_tokens``
    （``tokens_from_summary`` 的输出即满足）。金额保留 6 位小数，避免百万级 token
    的小单价被 2 位小数抹平；展示层自行决定四舍五入位数。
    """
    if not isinstance(pricing, dict):
        return None
    prices = {key: _pricing_value(pricing, key) for key in PRICING_KEYS}
    if not any(prices.values()):
        return None
    input_tokens = _non_negative_int(tokens.get("input_tokens"))
    cached_tokens = min(_non_negative_int(tokens.get("cached_tokens")), input_tokens)
    uncached_tokens = max(0, input_tokens - cached_tokens)
    output_tokens = _non_negative_int(tokens.get("output_tokens"))
    amount = (
        uncached_tokens / 1_000_000 * prices["input_per_million"]
        + cached_tokens / 1_000_000 * prices["cached_input_per_million"]
        + output_tokens / 1_000_000 * prices["output_per_million"]
    )
    currency = str(pricing.get("currency") or DEFAULT_CURRENCY).strip() or DEFAULT_CURRENCY
    return {"amount": round(amount, 6), "currency": currency}
