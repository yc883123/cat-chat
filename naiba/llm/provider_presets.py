# -*- coding: utf-8 -*-
"""供应商预设表：主流供应商「只填 API Key」的连接模板（纯数据 + 纯函数，零项目依赖）。

一张预设 = 一个主流模型供应商的连接模板：名称 / API URL / 请求格式 / 推荐模型 /
Key 申请入口全部预填，用户只需粘贴 API Key 即可开始对话；「自定义」预设保留
全字段手填路径（含各种 OpenAI 兼容中转）。前端（设置弹层与首启引导）经
``GET /api/provider-presets`` 取这份表，不在第二处复制名单；``POST /api/providers``
接受可选 ``preset_id``，服务端按本表回填连接字段（用户显式提交的值优先），
未知 ``preset_id`` 抛 ``ValueError``（HTTP 层转 400）。

字段约定：
  - ``kind``：``online`` / ``local``（与 config 词表一致，合法性由测试对表守门）；
  - ``abbr``：卡片图标里的 1~3 个字符（首字母色块，不引品牌 logo 图片，离线可用）；
  - ``request_format``：与设置弹层「请求格式」下拉同一套取值；
  - ``model``：推荐默认模型，只是省一次手填的初值，保存前仍可在弹层里改/拉目录；
  - ``key_required``：本地后端（Ollama 等）为 False，向导据此跳过 Key 输入；
  - ``key_url``：申请 Key 的站点入口（只放域名级首页，不放易失效的深链）；
  - ``hint``：三段式引导——①这是什么站 → ②怎么注册/充值 → ③Key 在哪创建、长什么样。
    hint 里出现的远端 URL 必须在界面上有可点按钮（``key_url`` → 「打开注册页」），
    不让用户手抄网址；本地预设的 localhost 地址只作信息，不给按钮。

顺序即界面顺序：在线（大厂 → 中转 → 自定义）在前、本地在后。
"""

from __future__ import annotations

from typing import Any, Mapping

PROVIDER_PRESETS: tuple[dict[str, Any], ...] = (
    # ---- 在线大厂 ----
    {
        "id": "deepseek",
        "kind": "online",
        "name": "DeepSeek",
        "abbr": "DS",
        "base_url": "https://api.deepseek.com",
        "request_format": "openai_chat",
        "model": "deepseek-chat",
        "key_required": True,
        "key_url": "https://platform.deepseek.com/",
        "hint": (
            "DeepSeek 官方 API（深度求索）。使用方法：① 打开 platform.deepseek.com 注册账号；"
            "② 充值（新用户通常有赠送额度）；③ 在「API keys」页创建 API Key，"
            "复制 sk- 开头的字符串粘到下方 API Key 栏。"
        ),
    },
    {
        "id": "kimi",
        "kind": "online",
        "name": "Kimi（月之暗面）",
        "abbr": "K",
        "base_url": "https://api.moonshot.cn/v1",
        "request_format": "openai_chat",
        "model": "kimi-k2-0905-preview",
        "key_required": True,
        "key_url": "https://platform.moonshot.cn/",
        "hint": (
            "月之暗面官方 API（Kimi）。使用方法：① 打开 platform.moonshot.cn 注册账号；"
            "② 充值；③ 在「API Key 管理」页新建 Key，复制 sk- 开头的字符串粘到下方。"
        ),
    },
    {
        "id": "zhipu",
        "kind": "online",
        "name": "智谱 GLM",
        "abbr": "GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "request_format": "openai_chat",
        "model": "glm-4.6",
        "key_required": True,
        "key_url": "https://open.bigmodel.cn/",
        "hint": (
            "智谱开放平台（GLM 系列）。使用方法：① 打开 open.bigmodel.cn 注册账号；"
            "② 充值或领用免费额度；③ 在控制台「API 密钥」页创建 Key，复制粘到下方。"
        ),
    },
    {
        "id": "qwen",
        "kind": "online",
        "name": "通义千问（阿里云百炼）",
        "abbr": "Q",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "request_format": "openai_chat",
        "model": "qwen-plus",
        "key_required": True,
        "key_url": "https://bailian.console.aliyun.com/",
        "hint": (
            "阿里云百炼平台（通义千问）。使用方法：① 打开 bailian.console.aliyun.com，"
            "用阿里云账号登录并开通百炼；② 领用免费额度或充值；③ 在「API-KEY 管理」"
            "页创建，复制 sk- 开头的 Key 粘到下方。"
        ),
    },
    {
        "id": "openai",
        "kind": "online",
        "name": "OpenAI",
        "abbr": "OA",
        "base_url": "https://api.openai.com/v1",
        "request_format": "openai_chat",
        "model": "gpt-5-mini",
        "key_required": True,
        "key_url": "https://platform.openai.com/",
        "hint": (
            "OpenAI 官方 API。使用方法：① 打开 platform.openai.com 注册账号；"
            "② 在 Billing 里充值（按量计费）；③ 在「API keys」页创建，"
            "复制 sk- 开头的 Key 粘到下方。注意：需要可访问 OpenAI 的网络环境。"
        ),
    },
    {
        "id": "claude",
        "kind": "online",
        "name": "Claude（Anthropic）",
        "abbr": "C",
        "base_url": "https://api.anthropic.com",
        "request_format": "claude",
        "model": "claude-sonnet-4-5",
        "key_required": True,
        "key_url": "https://console.anthropic.com/",
        "hint": (
            "Anthropic 官方 API（Claude）。使用方法：① 打开 console.anthropic.com 注册；"
            "② 在 Billing 里充值；③ 在「API Keys」页创建，复制 sk-ant- 开头的 Key 粘到下方。"
            "注意：需要可访问 Anthropic 的网络环境。"
        ),
    },
    {
        "id": "gemini",
        "kind": "online",
        "name": "Gemini（Google）",
        "abbr": "G",
        "base_url": "https://generativelanguage.googleapis.com",
        "request_format": "gemini",
        "model": "gemini-2.5-flash",
        "key_required": True,
        "key_url": "https://aistudio.google.com/",
        "hint": (
            "Google AI Studio（Gemini）。使用方法：① 打开 aistudio.google.com 用 Google 账号登录；"
            "② 免费额度即可开始试用（付费需在 Cloud 项目开通结算）；③ 点「Get API key」创建，"
            "复制 Key 粘到下方。注意：需要可访问 Google 的网络环境。"
        ),
    },
    # ---- API 中转（聚合站，一份余额调多家模型） ----
    {
        "id": "te",
        "kind": "online",
        "name": "TE 中转",
        "abbr": "TE",
        "base_url": "https://teynex.com",
        "request_format": "openai_chat",
        "model": "kimi-k3",
        "key_required": True,
        "key_url": "https://teynex.com/",
        "hint": (
            "TE 中转是一个 API 聚合站：注册一个账号、充一份余额，就能用一个 Key 调用 "
            "GPT、Claude、Kimi 等多家模型。使用方法：① 打开 teynex.com 注册账号；"
            "② 在「钱包/充值」充入少量余额（按量计费，几分钱就能试）；"
            "③ 到控制台「令牌」页点「添加令牌」，复制 sk- 开头的字符串粘到下方 API Key 栏。"
            "注意：该站按 max_tokens 预扣费，余额不足时请求会被拒绝（报 403）——"
            "这不是配置错了，去充值就好。用 GPT-5/Codex 系模型请把请求格式改为 "
            "codex_responses、URL 末尾补 /v1。"
        ),
    },
    {
        "id": "bailan",
        "kind": "online",
        "name": "摆烂中转",
        "abbr": "摆",
        "base_url": "https://api.bailan.store",
        "request_format": "openai_chat",
        "model": "grok-4.6",
        "key_required": True,
        "key_url": "https://bailan.store/",
        "hint": (
            "摆烂中转是一个 API 聚合站：注册一个账号，就能用一个 Key 调用 Grok、GPT 等"
            "多家模型。使用方法：① 打开 bailan.store 注册账号；② 按站点规则领取/充值额度；"
            "③ 到控制台「令牌」页创建令牌，复制 sk- 开头的字符串粘到下方 API Key 栏。"
        ),
    },
    # ---- 全手填 ----
    {
        "id": "custom",
        "kind": "online",
        "name": "自定义（OpenAI 兼容或其他）",
        "abbr": "…",
        "base_url": "",
        "request_format": "openai_chat",
        "model": "",
        "key_required": True,
        "key_url": "",
        "hint": (
            "自定义端点：适用于各种 OpenAI 兼容中转、公司内网或私有服务——"
            "需要自己填好 API URL、请求格式和模型名。不知道填什么时，"
            "可以先在上方选一个现成预设。"
        ),
    },
    # ---- 本地后端（免 Key） ----
    {
        "id": "ollama",
        "kind": "local",
        "name": "本机 Ollama",
        "abbr": "OI",
        "base_url": "http://127.0.0.1:11434/v1",
        "request_format": "ollama",
        "model": "",
        "key_required": False,
        "key_url": "",
        "hint": (
            "本机 Ollama（免 Key）：① 先在本机安装并启动 Ollama；"
            "② 确认服务地址是 http://127.0.0.1:11434/v1（默认值，一般不用改）；"
            "③ API Key 留空即可，保存后点「检查模型」拉取本机已下载的模型。"
        ),
    },
    {
        "id": "lm_studio",
        "kind": "local",
        "name": "本机 LM Studio",
        "abbr": "LM",
        "base_url": "http://127.0.0.1:1234/v1",
        "request_format": "lm_studio",
        "model": "",
        "key_required": False,
        "key_url": "",
        "hint": (
            "本机 LM Studio（免 Key）：① 打开 LM Studio 的 Developer → Local Server 页面"
            "并启动服务、加载模型；② 确认地址是 http://127.0.0.1:1234/v1；"
            "③ API Key 留空，保存后点「检查模型」。"
        ),
    },
    {
        "id": "llama_cpp",
        "kind": "local",
        "name": "本机 llama.cpp",
        "abbr": "LC",
        "base_url": "http://127.0.0.1:8080/v1",
        "request_format": "llama_cpp",
        "model": "",
        "key_required": False,
        "key_url": "",
        "hint": (
            "本机 llama.cpp（免 Key）：① 用 llama-server 启动服务；"
            "② 确认地址是 http://127.0.0.1:8080/v1；"
            "③ API Key 留空，保存后点「检查模型」。"
        ),
    },
    {
        "id": "unsloth",
        "kind": "local",
        "name": "本机 Unsloth",
        "abbr": "UN",
        "base_url": "http://127.0.0.1:8000",
        "request_format": "unsloth",
        "model": "",
        "key_required": False,
        "key_url": "",
        "hint": (
            "本机 Unsloth（Key 可先留空）：① 启动 Unsloth 桌面版或 unsloth studio；"
            "② 地址通常是 http://127.0.0.1:8000 或 http://127.0.0.1:8888；"
            "③ 保存后点「检查模型」（需要 Key 时在 Unsloth Settings → API 里创建）。"
        ),
    },
)

_PRESET_BY_ID = {item["id"]: item for item in PROVIDER_PRESETS}

# 预设只负责回填连接字段；api_key 等敏感/个性化字段永远以用户提交为准。
_PRESET_FILL_KEYS = ("kind", "name", "base_url", "request_format", "model", "local_backend")


def provider_preset(preset_id: str) -> dict[str, Any] | None:
    """按 id 取预设（找不到返回 None）。"""
    return _PRESET_BY_ID.get(str(preset_id or "").strip())


def provider_presets_payload() -> list[dict[str, Any]]:
    """导出给前端的只读副本（在线在前、本地在后，顺序即界面顺序）。"""
    return [dict(item) for item in PROVIDER_PRESETS]


def provider_preset_key_url(preset_id: str) -> str:
    """取预设的 Key 申请入口（「打开注册页」按钮唯一可用的地址来源）。

    只从预设表白名单读取、不接受调用方传入任意 URL，因此不存在被当作
    「打开任意网址」通道的风险。无入口（自定义 / 本地预设）时返回空串。
    """
    preset = provider_preset(preset_id)
    return str(preset.get("key_url") or "") if preset else ""


def apply_preset_values(values: Mapping[str, Any]) -> dict[str, Any]:
    """把 ``preset_id`` 展开成完整连接字段：预设值作默认，用户显式提交的非空值优先。

    未携带 ``preset_id`` 时原样返回（全手动路径不变）；携带时返回新字典并保留
    ``preset_id`` 本身（供存储层随卡片记住来源，前端反显与卡片角标用）。
    未知 ``preset_id`` 抛 ``ValueError``。
    """
    merged = dict(values)
    preset_id = str(merged.pop("preset_id", "") or "").strip()
    if not preset_id:
        return merged
    preset = provider_preset(preset_id)
    if preset is None:
        raise ValueError(f"未知的供应商预设：{preset_id}")
    merged["preset_id"] = preset_id
    for key in _PRESET_FILL_KEYS:
        submitted = str(merged.get(key) or "").strip()
        if submitted:
            continue
        if key == "local_backend":
            # 本地预设的 local_backend 恒等于其 request_format；在线预设不携带该键。
            fallback = preset["request_format"] if preset["kind"] == "local" else ""
        else:
            fallback = str(preset.get(key) or "")
        if fallback:
            merged[key] = fallback
        else:
            merged.pop(key, None)
    return merged
