"""本地推理后端的上下文窗口探测（llama.cpp / unsloth、Ollama、LM Studio）。

为什么需要这个模块
------------------
`/v1/models` 这类目录接口基本不返回上下文窗口字段，`config._infer_context_window()`
因此返回 0。窗口未知时旧的预算逻辑按**在线**默认值 `DEFAULT_CONTEXT_WINDOW`(256k)
兜底 —— 对真实 n_ctx 常在 8k~32k 的本地模型等于不设上限：闸门放行 → 请求发给本地
后端 → prefill 做不完 → 界面永久停在「等待本地模型资源」，重启后端乃至重启电脑都
无效（病根在每轮构造的请求体里，不在进程里）。

这里改为直接向本地后端要真值，探测不到才退回保守兜底
（`skills.context.LOCAL_DEFAULT_CONTEXT_WINDOW`）：

- ``llama_cpp`` / ``unsloth``：``GET /props`` → ``default_generation_settings.n_ctx``
- ``ollama``：``POST /api/show`` ``{"name": <model>}`` → ``parameters`` 里的 ``num_ctx``
- ``lm_studio``：``GET /api/v0/models`` → 目标模型的 ``max_context_length``

三条口径都取**运行期真实上下文**，不取模型的训练长度（``n_ctx_train`` /
``*.context_length``）：后者往往十几万，会把「不设上限」换个数字重新引进来。

约定：任何失败（端点不存在 / 超时 / 结构不符 / 主机不可达）一律返回 0 且不上抛；
结果按 ``(base_url, model)`` 进程内记忆化 —— 一次运行只探测一次，不进入每条消息
的热路径。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from naiba import net as net_io

# 探测是「顺手问一句」，不能因为本地服务没起/卡住而拖慢一轮对话。
PROBE_TIMEOUT_SECONDS = 2.5

_cache: dict[str, int] = {}
_cache_lock = threading.Lock()


def _cache_key(profile: dict[str, Any]) -> str:
    return "|".join(
        (
            str(profile.get("base_url") or "").strip().rstrip("/"),
            str(profile.get("model") or "").strip(),
            str(profile.get("request_format") or profile.get("local_backend") or "").strip().lower(),
        )
    )


def _clear_cache() -> None:
    """测试辅助：清空记忆化结果。"""
    with _cache_lock:
        _cache.clear()


def _endpoint(base_url: str, path: str) -> str:
    """与 ``llm.protocols.ProtocolMixins._local_endpoint`` 同口径：base_url 可能已带 /v1。"""
    parsed = urllib.parse.urlsplit(str(base_url or "").strip())
    base_path = parsed.path.rstrip("/")
    if base_path.endswith("/v1"):
        base_path = base_path[:-3]
    target_path = f"{base_path}{path}" if base_path else path
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, target_path, parsed.query, parsed.fragment)
    )


def _request_json(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    api_key: str = "",
) -> Any:
    headers = {"Accept": "application/json", "User-Agent": "naiba-chat/local-probe"}
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with net_io.open(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _llama_cpp_window(profile: dict[str, Any]) -> int:
    """llama.cpp / unsloth：/props 的 default_generation_settings.n_ctx。"""
    payload = _request_json(
        _endpoint(str(profile.get("base_url") or ""), "/props"),
        api_key=str(profile.get("api_key") or "").strip(),
    )
    if not isinstance(payload, dict):
        return 0
    settings = payload.get("default_generation_settings")
    if isinstance(settings, dict):
        found = _positive_int(settings.get("n_ctx"))
        if found:
            return found
    return _positive_int(payload.get("n_ctx"))


def _ollama_window(profile: dict[str, Any]) -> int:
    """Ollama：/api/show 的 parameters 里显式设置的 num_ctx。

    不取 model_info 的 ``*.context_length``（那是模型训练长度，常十几万），
    只认用户/客户端真正指定过的 num_ctx；没指定就交给保守兜底。
    """
    model = str(profile.get("model") or "").strip()
    if not model:
        return 0
    payload = _request_json(
        _endpoint(str(profile.get("base_url") or ""), "/api/show"),
        payload={"name": model},
        api_key=str(profile.get("api_key") or "").strip(),
    )
    if not isinstance(payload, dict):
        return 0
    parameters = payload.get("parameters")
    if isinstance(parameters, dict):
        found = _positive_int(parameters.get("num_ctx"))
        if found:
            return found
    if isinstance(parameters, str):
        for line in parameters.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].strip().lower() == "num_ctx":
                found = _positive_int(parts[1])
                if found:
                    return found
    return 0


def _lm_studio_window(profile: dict[str, Any]) -> int:
    """LM Studio：/api/v0/models 里目标模型的 max_context_length。"""
    model = str(profile.get("model") or "").strip()
    if not model:
        return 0
    payload = _request_json(
        _endpoint(str(profile.get("base_url") or ""), "/api/v0/models"),
        api_key=str(profile.get("api_key") or "").strip(),
    )
    if not isinstance(payload, dict):
        return 0
    entries = payload.get("data")
    if not isinstance(entries, list):
        return 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("id") or "").strip() == model:
            return _positive_int(entry.get("max_context_length"))
    return 0


def probe_local_context_window(profile: dict[str, Any]) -> int:
    """探测本地后端的真实上下文长度；未知或失败返回 0（调用方退回保守兜底）。"""
    if not isinstance(profile, dict):
        return 0
    if str(profile.get("kind") or "").strip().lower() != "local":
        return 0
    if not str(profile.get("base_url") or "").strip():
        return 0
    key = _cache_key(profile)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    backend = str(
        profile.get("request_format") or profile.get("local_backend") or ""
    ).strip().lower()
    if backend in {"llama_cpp", "unsloth", "openai_chat", ""}:
        probe = _llama_cpp_window
    elif backend == "ollama":
        probe = _ollama_window
    elif backend == "lm_studio":
        probe = _lm_studio_window
    else:
        probe = _llama_cpp_window
    try:
        window = _positive_int(probe(profile))
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        NotImplementedError,
    ):
        window = 0
    # 只缓存成功结果：本地服务尚未启动时探到 0，起来之后这一次会话仍应能重新探到真值。
    if window > 0:
        with _cache_lock:
            _cache[key] = window
    return window
