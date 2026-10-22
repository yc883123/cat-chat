"""模型 profile 解析共享层（run/chat 与 Studio runner 共用，单一来源）。

会话里的 `model_key` + `model_name` 需要解析成完整模型配置：`config.profile()` 给出基础
配置，会话/工作台选定的 `model_name` 覆盖 `profile["model"]`；本地（kind=local）后端缺失
上下文窗口时做一次探测（`naiba/llm/local_probe`）。探测结果按 (base_url, model) 进程内
记忆化，显式配置的窗口永远优先。

此前这段逻辑只挂在 `naiba/run/chat.py::_profile_with_model_override`（普通对话域）；
工作台（Studio）按计划只依赖 shared 的 `naiba/config.py` / `naiba/llm/*`，因此把同一
实现移入 llm 域，`_profile_with_model_override` 保留同名转发（原测试/调用点零改动）。
"""

from __future__ import annotations

from typing import Any

from naiba.llm.local_probe import probe_local_context_window


def resolve_profile_with_override(config: Any, model_key: str, model_name: str) -> dict[str, Any]:
    """Resolve an API profile using the concrete model selected in the caller (chat/studio).

    错误文案沿用普通对话原句（可能出现在对话提交路径里，改动会牵动既有测试与回放）。
    """
    profile = dict(config.profile(model_key))
    clean_name = str(model_name or "").strip()
    if not clean_name:
        raise ValueError("请先在会话中选择模型；请先在设置中检查模型")
    profile["model"] = clean_name
    # 本地后端的窗口探测：/v1/models 不返回窗口字段，config._infer_context_window() 因此
    # 恒为 0。不补这一刀，上下文闸门与前端上下文环（含「上下文提醒阈值」）都会按在线的
    # 256k 兜底，等于对本地模型不设上限——超长会话的请求会被放行，本地后端做不完 prefill，
    # 界面永久停在「等待本地模型资源」（模块头见 naiba/llm/local_probe.py）。
    # 探测结果按 (base_url, model) 进程内记忆化；显式配置的 context_window 永远优先。
    if str(profile.get("kind") or "").strip().lower() == "local":
        # 「已确定」= 用户/供应商配置里显式写的窗口（来源 local_config / provider_config）；
        # 这种情况永远不探测也不回写。其余情况（unknown / local_probe）都重新探一次：
        # 本地后端可能是这次才起来的，或用户换了 n_ctx——探测本身按 (base_url, model)
        # 进程内记忆化，重复调用不会增加网络开销。
        source = str(profile.get("context_window_source") or "")
        if source not in {"local_config", "provider_config"}:
            probed = probe_local_context_window(profile)
            if probed > 0:
                profile["context_window"] = probed
                profile["context_size"] = probed
                profile["context_window_source"] = "local_probe"
                # 回写 provider 配置。只改本轮 profile 副本的话，探测值仅在「会话路径」
                # 生效：设置页显示的上下文长度、以及其它所有走 _infer_context_window 的
                # 读取点仍然拿到 0 → 又退回在线 256k 兜底，等于对本地模型不设上限。
                remember = getattr(config, "remember_local_context_window", None)
                if callable(remember):
                    remember(model_key, probed)
    return profile
