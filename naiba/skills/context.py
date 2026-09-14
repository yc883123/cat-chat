"""上下文窗口常量（原 skill_runtime 模块级；窗口预算方法暂随 SkillAgent 保留于 agent.py）。"""

from __future__ import annotations

from typing import Any

# Conservative context ceiling (tokens) used when a provider exposes no window
# (e.g. DeepSeek's /v1/models returns no context-length field, so auto-detection
# yields 0). Rather than silently truncating history — which both drops context
# and re-breaks DeepSeek's token-prefix cache every turn — a conversation is
# blocked with a user-visible notice once it reaches this bound.
DEFAULT_CONTEXT_WINDOW = 256000

# 本地推理后端（llama_cpp / unsloth / ollama / lm_studio）在探测失败时的保守兜底窗口。
#
# 为什么必须与在线默认值分开：本地后端起服务时的 n_ctx 常在 8k~32k，而它们的
# /v1/models 基本不返回窗口字段（config._infer_context_window 恒返回 0）。旧行为让本地
# 模型套用上面那个 256k 的在线默认值，闸门（SkillAgent._context_fits）等于几乎永不触发：
# 超长会话每轮把全部历史 + 图片 + reasoning + 工具 schema 原样重发，请求量可超出真实窗口
# 一个数量级，本地后端收下后做不完 prefill —— 界面永久停在「等待本地模型资源」，
# 重启后端乃至重启电脑都无效（病根在每轮构造的请求体里，不在进程里）。
# 同理，上下文圆环与「运行设置 → 上下文提醒阈值」的分母过去也是这个 256k，
# 本地用户永远涨不到提醒线，等于整套超限提醒对本地模型失效。
#
# 取值口径：这是「探测失败时」的兜底，不是用来猜模型能力的。优先顺序为
# ① 设置里显式填写的 context_window → ② llm.local_probe 探测到的真实 n_ctx → ③ 本常量。
# 用户填了真实值就以显式值为准。
LOCAL_DEFAULT_CONTEXT_WINDOW = 32768


def fallback_context_window(profile: dict[str, Any]) -> int:
    """窗口未知时的兜底上限：本地与在线用不同口径（本地更保守）。

    纯函数、不查网络 —— 预算判定与前端上下文环必须同源同值。
    """
    kind = ""
    if isinstance(profile, dict):
        kind = str(profile.get("kind") or "").strip().lower()
    return LOCAL_DEFAULT_CONTEXT_WINDOW if kind == "local" else DEFAULT_CONTEXT_WINDOW



