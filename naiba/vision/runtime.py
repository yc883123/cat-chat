"""视觉运行时：给纯文本 naiba-chat 装上「眼睛」。

设计（对应 dsh-vision-router 的「眼睛/大脑」架构，视觉调用统一由模型驱动）：
- 视觉模型只当「眼睛」，DeepSeek 等文本模型仍是「大脑」。
- 模型主动调用 vision_analyze 工具（与其它工具一致，结果以工具块呈现）：
  图片不随请求发送给纯文本模型，仅以路径占位引用，何时看图、问什么都由模型决定。
- 多模态聊天模型仍直接收到原图（不做占位改写），无需调用视觉工具。
- 内置免费 OVH 匿名视觉链兜底（免 Key，限流约 2 次/分钟/IP/模型）；用户自配
  视觉供应商优先调用。

全部基于 Pillow（naiba-chat 已有依赖）与标准库，无新增打包负担。
"""
from __future__ import annotations

from naiba.core.contracts import AppContext

import hashlib
import io
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from naiba import net as net_io
from naiba.llm.runtime import ModelRuntime
from naiba.storage.media import _process_uploaded_image
from naiba.vision.images import (
    _image_size,
    _make_probe_jpeg_b64,
    _read_rgb,
    encode_image_file,
)

logger = logging.getLogger("naiba.vision_runtime")

# dsh-vision-router 内置的免费匿名视觉链（OpenAI 兼容，免注册免 Key）。
OVH_VISION_BASE = "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1"
DEFAULT_VISION_CHAIN = [
    "Qwen3.5-397B-A17B",
    "Qwen2.5-VL-72B-Instruct",
    "Qwen3.6-27B",
    "Mistral-Small-3.2-24B-Instruct-2506",
    "Qwen3.5-9B",
]

# The valid 32x32 RGB JPEG probe is created below with Pillow.  Some
# llama-server builds reject a 1x1 transparent PNG before model inference.

PROBE_JPEG_B64 = _make_probe_jpeg_b64()

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# 大脑模型名里出现这些关键词时，视为「自身支持看图」，跳过占位改写。
VISION_BRAIN_HINTS = (
    "vl", "vision", "4o", "gpt-4", "gpt-5", "gemini", "claude", "glm",
    "llava", "minicpm-v", "internvl", "qwen2.5-vl", "qwen-vl", "pixtral",
    "omni", "step-1v", "qwen3.5", "mistral-small-3.2",
)

DEFAULT_TIMEOUT_SECONDS = 180


class VisionBudget:
    """Run-scoped visual request cache and counter with a per-call timeout."""

    def __init__(self, timeout_seconds: float):
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.calls = 0
        self.cache: dict[str, str] = {}
        self._lock = threading.RLock()

    def cached(self, key: str) -> str | None:
        with self._lock:
            value = self.cache.get(key)
        return value

    def begin(self, key: str) -> float:
        del key
        with self._lock:
            self.calls += 1
        return self.timeout_seconds

    def store(self, key: str, value: str) -> None:
        with self._lock:
            self.cache[key] = value


def _default_vision_config() -> dict[str, Any]:
    return {
        "provider_model_key": "",
        "fallback_models": [],
        "brain_supports_image": False,
        "timeout_ms": DEFAULT_TIMEOUT_SECONDS * 1000,
        "cache": True,
        "cache_ttl_seconds": 3600,
        "cache_max_entries": 200,
        "max_images": 4,
    }


class VisionRouter:
    """视觉后端调用 + 会话化视觉工具（vision_analyze / vision_image_ops）。"""

    def __init__(self, app: AppContext):
        self.app = app
        # 独立 ModelRuntime，避免污染大脑（app.models）的 last_usage / last_reasoning 线程状态。
        self._runtime = ModelRuntime()
        self._local = threading.local()
        self._cache: dict[str, tuple[float, str]] = {}
        self._cache_lock = threading.RLock()
        self._capability_cache: dict[str, dict[str, Any]] = {}
        self._capability_lock = threading.RLock()

    @property
    def last_trace(self) -> dict[str, Any]:
        value = getattr(self._local, "last_trace", {})
        return dict(value) if isinstance(value, dict) else {}

    @last_trace.setter
    def last_trace(self, value: dict[str, Any]) -> None:
        self._local.last_trace = dict(value or {})

    # ---- 配置 ----
    def config(self) -> dict[str, Any]:
        data = self.app.config.data.get("vision") or {}
        if not isinstance(data, dict):
            return _default_vision_config()
        merged = _default_vision_config()
        for key in merged:
            if key in data:
                merged[key] = data[key]
        return merged

    # ---- 视觉后端链 ----
    def vision_backends(self, provider_model_key: str | None = None) -> list[dict[str, Any]]:
        """返回视觉后端 profile 列表；显式选择时不静默切换到匿名链。"""
        cfg = self.config()
        backends: list[dict[str, Any]] = []

        selected_key = cfg.get("provider_model_key") if provider_model_key is None else provider_model_key
        user_key = str(selected_key or "").strip()
        if user_key:
            try:
                profile = self.app.config.profile(user_key)
                profile = {**profile, "name": str(profile.get("name") or "用户视觉供应商")}
                backends.append(profile)
            except (ValueError, KeyError):
                logger.warning("vision: 配置的视觉供应商不可用：%s", user_key)

        for fallback in cfg.get("fallback_models") or []:
            if not isinstance(fallback, dict):
                continue
            base = str(fallback.get("base_url") or "").strip().rstrip("/")
            model = str(fallback.get("model") or "").strip()
            if not base or not model:
                continue
            backends.append(
                {
                    "kind": "online",
                    "request_format": "openai_chat",
                    "base_url": base,
                    "model": model,
                    "api_key": str(fallback.get("api_key") or "").strip(),
                    "name": str(fallback.get("name") or model),
                }
            )

        # 只有留空使用默认视觉链时才加入匿名 OVH；显式选择失败必须显示真实失败原因。
        if not user_key:
            for model in DEFAULT_VISION_CHAIN:
                backends.append(
                    {
                        "kind": "online",
                        "request_format": "openai_chat",
                        "base_url": OVH_VISION_BASE,
                        "model": model,
                        "api_key": "",
                        "name": f"OVH 免费视觉 · {model}",
                    }
                )
        return backends

    @staticmethod
    def _brain_supports_vision(profile: dict[str, Any]) -> bool:
        model = str(profile.get("model") or "").lower()
        request_format = str(profile.get("request_format") or "").lower()
        if request_format in {"gemini", "claude"}:
            return True
        return any(hint in model for hint in VISION_BRAIN_HINTS)

    @classmethod
    def brain_supports_images(cls, profile: dict[str, Any]) -> bool:
        """Resolve image capability with an explicit boolean taking precedence."""
        explicit = profile.get("supports_images")
        if isinstance(explicit, bool):
            return explicit
        return cls._brain_supports_vision(profile)

    @staticmethod
    def _capability_base_url(profile: dict[str, Any]) -> str:
        base_url = str(profile.get("base_url") or "").rstrip("/")
        return base_url[:-3] if base_url.endswith("/v1") else base_url

    @staticmethod
    def _request_json(
        endpoint: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        api_key: str = "",
        timeout: float = 2,
    ) -> Any:
        headers = {"Accept": "application/json", "User-Agent": "naiba-chat"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(endpoint, data=data, headers=headers, method=method)
        with net_io.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))

    @staticmethod
    def _lm_studio_model_matches(item: dict[str, Any], configured_model: str) -> bool:
        """Match both LM Studio catalog models and their loaded instance identifiers."""
        wanted = str(configured_model or "").strip().replace("\\", "/").casefold()
        if not wanted:
            return False
        identities: list[str] = []

        def collect(value: Any) -> None:
            if not isinstance(value, dict):
                return
            for key in ("id", "key", "model", "name", "display_name", "model_key", "instance_id"):
                if value.get(key) not in (None, ""):
                    identities.append(str(value[key]))

        collect(item)
        loaded = item.get("loaded_instances")
        if isinstance(loaded, list):
            for instance in loaded:
                collect(instance)
        elif isinstance(loaded, dict):
            collect(loaded)
        normalized = [value.strip().replace("\\", "/").casefold() for value in identities]
        if wanted in normalized:
            return True
        wanted_name = wanted.rsplit("/", 1)[-1]
        return any(value.rsplit("/", 1)[-1] == wanted_name for value in normalized)

    def _runtime_image_capability(self, profile: dict[str, Any]) -> tuple[bool | None, str]:
        request_format = str(profile.get("request_format") or "").lower()
        base_url = self._capability_base_url(profile)
        api_key = str(profile.get("api_key") or "").strip()
        model = str(profile.get("model") or "").strip()
        if not base_url:
            return None, ""
        try:
            if request_format == "llama_cpp":
                props = self._request_json(f"{base_url}/props", api_key=api_key)
                modalities = props.get("modalities") if isinstance(props, dict) else None
                vision = modalities.get("vision") if isinstance(modalities, dict) else None
                return (vision, "llama_props") if isinstance(vision, bool) else (None, "")
            if request_format == "ollama":
                details = self._request_json(
                    f"{base_url}/api/show",
                    method="POST",
                    payload={"model": model},
                    api_key=api_key,
                )
                if isinstance(details, dict) and isinstance(details.get("capabilities"), list):
                    capabilities = {str(value).strip().lower() for value in details["capabilities"]}
                    return "vision" in capabilities, "ollama_show"
                return None, ""
            if request_format == "lm_studio":
                catalog = self._request_json(f"{base_url}/api/v1/models", api_key=api_key)
                items = []
                if isinstance(catalog, dict):
                    items = catalog.get("data") or catalog.get("models") or []
                for item in items if isinstance(items, list) else []:
                    if not isinstance(item, dict) or not self._lm_studio_model_matches(item, model):
                        continue
                    capabilities = item.get("capabilities")
                    vision = capabilities.get("vision") if isinstance(capabilities, dict) else None
                    return (vision, "lm_studio_models") if isinstance(vision, bool) else (None, "")
        except (OSError, ValueError, json.JSONDecodeError):
            return None, ""
        return None, ""

    def _probe_image_capability(self, profile: dict[str, Any]) -> bool | None:
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "只回复 OK"},
                {"type": "image", "media_type": "image/jpeg", "data": PROBE_JPEG_B64},
            ],
        }]
        options = {
            "temperature": 0,
            "max_tokens": 16,
            "stream": False,
            "connection_test": True,
            "request_timeout_seconds": 15,
            "request_attempts": 1,
            "reasoning_enabled": False,
            # 本地锁占用者标签：本地视觉能力探测也可能占住全进程唯一的本地锁，
            # 标上角色后，同一时刻在等锁的主对话能说出「谁在用」。
            "lock_label": "视觉能力探测",
        }
        try:
            self._runtime.complete({**profile, "reasoning_effort": "off"}, messages, options, None)
            return True
        except Exception as exc:  # noqa: BLE001 - distinguish capability rejection from availability
            reason = str(exc).lower()
            unsupported_markers = (
                "image input is not supported", "does not support images", "unsupported image",
                "provide the mmproj", "failed to load image", "vision is not supported",
                "image input unsupported", "images are not supported", "不支持图片输入",
                "不支持图像输入", "不支持视觉", "未加载 mmproj",
            )
            if any(marker in reason for marker in unsupported_markers):
                return False
            return None

    def brain_image_capability(
        self,
        profile: dict[str, Any],
        probe_if_unknown: bool = False,
    ) -> dict[str, Any]:
        """Resolve image support and retain whether the result was actually confirmed."""
        explicit = profile.get("supports_images_explicit")
        if isinstance(explicit, bool):
            return {"supported": explicit, "confirmed": True, "source": "explicit"}
        cache_key = "|".join((
            str(profile.get("request_format") or "").lower(),
            self._capability_base_url(profile),
            str(profile.get("model") or ""),
        ))
        now = time.time()
        with self._capability_lock:
            cached = dict(self._capability_cache.get(cache_key) or {})
        if cached and now - float(cached.get("timestamp") or 0) < float(cached.get("ttl") or 0):
            if cached.get("confirmed") or not probe_if_unknown:
                return {key: cached[key] for key in ("supported", "confirmed", "source")}

        detected, source = self._runtime_image_capability(profile)
        if isinstance(detected, bool):
            result = {
                "supported": detected, "confirmed": True, "source": source,
                "timestamp": now, "ttl": 30,
            }
        else:
            probed = self._probe_image_capability(profile) if probe_if_unknown else None
            if isinstance(probed, bool):
                result = {
                    "supported": probed, "confirmed": True, "source": "image_probe",
                    "timestamp": now, "ttl": 300,
                }
            else:
                result = {
                    "supported": self.brain_supports_images(profile),
                    "confirmed": False,
                    "source": "model_name",
                    "timestamp": now,
                    "ttl": 30,
                }
        with self._capability_lock:
            self._capability_cache[cache_key] = result
        return {key: result[key] for key in ("supported", "confirmed", "source")}

    def resolve_brain_supports_images(
        self,
        profile: dict[str, Any],
        probe_if_unknown: bool = False,
    ) -> bool:
        """Resolve image support in explicit/API/probe/name order."""
        return bool(self.brain_image_capability(profile, probe_if_unknown)["supported"])

    @staticmethod
    def strip_images_for_text_model(
        history: list[dict[str, Any]], reason: str = "视觉路由不可用"
    ) -> tuple[list[dict[str, Any]], int]:
        """Fail closed by removing every image part before a text-only request."""
        cleaned: list[dict[str, Any]] = []
        removed = 0
        safe_reason = " ".join(str(reason or "视觉路由不可用").split())[:500]
        for item in history:
            content = item.get("content")
            if not isinstance(content, list):
                cleaned.append(item)
                continue
            text_parts = [
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            image_parts = [
                part
                for part in content
                if isinstance(part, dict) and part.get("type") == "image"
            ]
            if not image_parts:
                cleaned.append(item)
                continue
            removed += len(image_parts)
            names = [str(part.get("path") or part.get("name") or "") for part in image_parts]
            marker = (
                f"[已移除 {len(image_parts)} 张图片，未发送给纯文本模型]\n"
                f"图片引用：{json.dumps(names, ensure_ascii=False)}\n"
                f"原因：{safe_reason}"
            )
            text = "\n".join(part for part in text_parts if part).strip()
            merged = (text + "\n\n" + marker).strip() if text else marker
            cleaned.append({**item, "content": [{"type": "text", "text": merged}]})
        return cleaned, removed

    # ---- 调用视觉模型 ----
    def _call_backend(
        self,
        profile: dict[str, Any],
        image_parts: list[dict[str, Any]],
        question: str,
        max_tokens: int = 2048,
        timeout_seconds: float | None = None,
        attempts: int | None = None,
        connection_test: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> str:
        content: list[dict[str, Any]] = [{"type": "text", "text": question}]
        for part in image_parts:
            content.append(
                {"type": "image", "media_type": part.get("media_type"), "data": part.get("data")}
            )
        messages = [{"role": "user", "content": content}]
        options = {
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "stream": False,
            # 本地锁占用者标签：视觉识别不走对话的 status 通道（下面是 status=None），
            # 占住本地锁时界面完全看不到它。标上角色，等锁的一方就能指名道姓。
            "lock_label": "视觉识别",
        }
        if timeout_seconds is None:
            try:
                timeout_seconds = int(self.config().get("timeout_ms", 180000)) / 1000
            except (TypeError, ValueError):
                timeout_seconds = DEFAULT_TIMEOUT_SECONDS
        options["request_timeout_seconds"] = max(1, float(timeout_seconds))
        if attempts is not None:
            options["request_attempts"] = max(1, int(attempts))
        if connection_test:
            options["connection_test"] = True
        if cancel_event is not None:
            options["cancel_event"] = cancel_event
        # Image reverse-inference is a perception pass. Do not inherit the
        # brain/provider's reasoning setting: thinking tokens add latency and
        # can leave some vision endpoints with no usable final description.
        vision_profile = {**profile, "reasoning_effort": "off"}
        # Do not unload a local vision backend after a request. Unloading here
        # is invisible to the user and can interrupt the next local turn or
        # force an expensive reload. Explicit unload remains available in the
        # model controls.
        result = self._runtime.complete(vision_profile, messages, options, None)
        diagnostics = self._runtime.last_diagnostics
        trace = self.last_trace
        trace["requests"] = int(trace.get("requests") or 0) + max(1, int(diagnostics.get("attempts") or 1))
        trace["usage"] = self._runtime.last_usage
        trace["diagnostics"] = diagnostics
        trace["model"] = str(vision_profile.get("model") or "")
        trace["provider"] = str(vision_profile.get("name") or "")
        self.last_trace = trace
        return result

    def describe_parts(
        self,
        image_parts: list[dict[str, Any]],
        question: str = "",
        json_mode: bool = False,
        cancel_event: threading.Event | None = None,
        vision_budget: VisionBudget | None = None,
    ) -> str:
        """对内存中的 image 部件做看图问答，带链式 failover。"""
        if not getattr(self._local, "last_trace", None):
            self.last_trace = {"requests": 0, "cache_hit": False}
        if not image_parts:
            raise ValueError("vision: 没有可用的图片")
        prompt = question.strip() or "请描述这张图片的内容。"
        if json_mode:
            prompt += (
                "\n\n请严格只输出一个 JSON 对象，字段：summary（中文简述）、"
                "layout（主要布局区域列表）、entities（实体清单）、text（图片中原文逐字转写，没有则空字符串）。"
                "不要输出 JSON 以外的任何文字或 Markdown 代码块。"
            )
        return self._describe_with_chain(image_parts, prompt, json_mode, cancel_event, vision_budget)

    def describe_files(
        self,
        paths: list[str],
        question: str = "",
        json_mode: bool = False,
        cancel_event: threading.Event | None = None,
        vision_budget: VisionBudget | None = None,
    ) -> str:
        parts = []
        for path in paths:
            part = encode_image_file(str(path))
            if part:
                parts.append(part)
        if not parts:
            raise ValueError("vision: 无法读取任何图片文件")
        return self.describe_parts(parts, question, json_mode, cancel_event, vision_budget)

    def _describe_with_chain(
        self,
        image_parts: list[dict[str, Any]],
        prompt: str,
        json_mode: bool,
        cancel_event: threading.Event | None = None,
        vision_budget: VisionBudget | None = None,
    ) -> str:
        cache_key = ""
        cfg = self.config()
        if cfg.get("cache"):
            digest = hashlib.sha256()
            for part in image_parts:
                digest.update((part.get("data") or "").encode("ascii", errors="ignore"))
            digest.update(prompt.encode("utf-8"))
            digest.update(b"json" if json_mode else b"text")
            cache_key = digest.hexdigest()
            cached = self._cache_get(cache_key)
            if cached is not None:
                trace = self.last_trace
                trace["cache_hit"] = True
                self.last_trace = trace
                return cached

        errors: list[str] = []
        for profile in self.vision_backends():
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("任务已取消")
            try:
                content = self._budgeted_backend_call(
                    profile,
                    image_parts,
                    prompt,
                    cancel_event=cancel_event,
                    budget=vision_budget,
                    operation=f"vision_analyze:{json_mode}",
                )
                if content and content.strip():
                    self._cache_put(cache_key, content, cfg)
                    return content
                errors.append(f"{profile.get('name')}：空响应")
            except Exception as exc:  # noqa: BLE001 - 逐供应商降级
                errors.append(f"{profile.get('name')}：{exc}")
        raise RuntimeError("视觉后端全部失败：" + "；".join(errors[-6:]))

    # ---- 缓存 ----
    def _cache_get(self, key: str) -> str | None:
        if not key:
            return None
        with self._cache_lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            ts, value = entry
            if time.time() - ts > int(self.config().get("cache_ttl_seconds", 3600)):
                self._cache.pop(key, None)
                return None
            return value

    def _cache_put(self, key: str, value: str, cfg: dict[str, Any]) -> None:
        if not key or not cfg.get("cache"):
            return
        with self._cache_lock:
            if len(self._cache) >= int(cfg.get("cache_max_entries", 200)):
                # 简单淘汰：删除最旧的 10%。
                stale = sorted(self._cache.items(), key=lambda item: item[1][0])
                for old_key, _ in stale[: max(1, len(stale) // 10)]:
                    self._cache.pop(old_key, None)
            self._cache[key] = (time.time(), value)

    def prepare_history(
        self,
        history: list[dict[str, Any]],
        brain_profile: dict[str, Any],
        cancel_event: threading.Event | None = None,
        vision_budget: VisionBudget | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """把历史里的 image 部件改写成安全文本占位，由模型按需调用 vision_analyze 看图。

        视觉/多模态聊天模型（包括 llama.cpp + mmproj）始终保留原图直接看图（无需工具）；
        纯文本聊天模型不接收图片，改写为明确文本占位（文件路径引用 + 工具提示），
        何时看图、看什么由模型主动调用 vision_analyze 工具决定（与其它工具一致）。

        本地（kind=local）多模态大脑额外走「每请求图片总量上限」，见
        ``_cap_local_history_images``；在线模型不受影响。

        ``cancel_event`` / ``vision_budget`` 为历史兼容参数（自动路由已移除，不再使用）。

        返回 (new_history, note)。note 非空表示本轮发生了安全清洗。
        """
        self.last_trace = {"requests": 0, "cache_hit": False}
        cfg = self.config()
        # 多模态聊天模型始终直接收到原图；占位改写只服务纯文本聊天模型。
        brain_supports = self.brain_supports_images(brain_profile)
        if brain_supports:
            # 本地多模态大脑同样直发原图，但必须有「每请求图片总量上限」：
            # core.history 的 MODEL_IMAGE_HISTORY_LIMIT=3 是**每条 user 消息**的封顶
            # （计数在 per-message 循环里重置），全对话没有任何总量约束，而多模态分支
            # 过去直接 return 原样放行。16 小时会话可累积上百张 ~1MB 图并在每轮全量
            # 重发 ⇒ 请求体上百 MB、本地视觉塔 prefill 做不完 ⇒ 界面永久停在
            # 「等待本地模型资源」。在线模型不动（保住 1.6.0 的前缀缓存契约）。
            if str(brain_profile.get("kind") or "").strip().lower() != "local":
                return history, ""
            return self._cap_local_history_images(history)

        try:
            max_images = max(1, int(cfg.get("max_images", 4)))
        except (TypeError, ValueError):
            max_images = 4
        new_history: list[dict[str, Any]] = []
        removed_images = 0
        for item in history:
            content = item.get("content")
            if not isinstance(content, list):
                new_history.append(item)
                continue
            image_parts = [p for p in content if isinstance(p, dict) and p.get("type") == "image"]
            text_parts = [p for p in content if isinstance(p, dict) and p.get("type") == "text"]
            if not image_parts:
                new_history.append(item)
                continue
            text = "\n".join(str(p.get("text") or "") for p in text_parts).strip()
            selected = image_parts[:max_images]
            paths = [str(p.get("path") or p.get("name") or "") for p in selected]
            removed_images += len(selected)
            marker = (
                f"[本轮附带了 {len(selected)} 张图片]\n"
                f"图片文件路径：{json.dumps(paths, ensure_ascii=False)}\n"
                "（纯文本模型无法直接接收图片内容，图片未随请求发送，仅作为文件路径引用；"
                "如需看图请调用 vision_analyze 工具并传入图片路径。）"
            )
            merged_text = (text + "\n\n" + marker).strip() if text else marker
            new_history.append({**item, "content": [{"type": "text", "text": merged_text}]})
        note = f"已移除 {removed_images} 张图片（纯文本模型，图片仅以路径引用）" if removed_images else ""
        return new_history, note

    # 本地多模态模型单次请求的图片上限：张数 + base64 载荷字节（≈ 原始字节 × 4/3）。
    # 目的不是「提升效果」，而是把本地视觉塔的 prefill 成本压回常量级：本地后端做不完
    # prefill 时通常不报错、只是不吐首字节，界面便表现为永久卡死（见模块内注释）。
    LOCAL_REQUEST_IMAGE_LIMIT = 12
    LOCAL_REQUEST_IMAGE_BYTES_LIMIT = 8 * 1024 * 1024

    @classmethod
    def _cap_local_history_images(
        cls, history: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], str]:
        """只保留**最近** N 张真图（且不超字节上限），更早的图片改写成路径占位。

        对同一输入结果恒定（保留的是「按位置最后 N 张」），所以同一轮内多次调用一致。
        被省略的图片仍按文件名列出，模型可随时用 vision_analyze 按路径重新装入，信息不丢。
        """
        positions: list[tuple[int, int]] = []
        total_bytes = 0
        for message_index, item in enumerate(history):
            content = item.get("content") if isinstance(item, dict) else None
            if not isinstance(content, list):
                continue
            for part_index, part in enumerate(content):
                if isinstance(part, dict) and part.get("type") == "image":
                    positions.append((message_index, part_index))
                    total_bytes += cls._image_part_bytes(part)
        if (
            len(positions) <= cls.LOCAL_REQUEST_IMAGE_LIMIT
            and total_bytes <= cls.LOCAL_REQUEST_IMAGE_BYTES_LIMIT
        ):
            return history, ""

        kept: set[tuple[int, int]] = set()
        kept_bytes = 0
        for message_index, part_index in reversed(positions):
            if len(kept) >= cls.LOCAL_REQUEST_IMAGE_LIMIT:
                break
            part = history[message_index]["content"][part_index]
            part_bytes = cls._image_part_bytes(part)
            # 至少保留一张：单张就超过字节上限时，不因预算把图片全删。
            if kept and kept_bytes + part_bytes > cls.LOCAL_REQUEST_IMAGE_BYTES_LIMIT:
                break
            kept.add((message_index, part_index))
            kept_bytes += part_bytes

        capped: list[dict[str, Any]] = []
        for message_index, item in enumerate(history):
            content = item.get("content") if isinstance(item, dict) else None
            has_dropped = isinstance(content, list) and any(
                isinstance(part, dict)
                and part.get("type") == "image"
                and (message_index, part_index) not in kept
                for part_index, part in enumerate(content)
            )
            if not has_dropped:
                capped.append(item)
                continue
            new_content: list[dict[str, Any]] = []
            dropped: list[str] = []
            for part_index, part in enumerate(content):
                if not isinstance(part, dict):
                    continue
                if part.get("type") != "image":
                    new_content.append(part)
                elif (message_index, part_index) in kept:
                    new_content.append(part)
                else:
                    dropped.append(str(part.get("name") or part.get("path") or "（未命名图片）"))
            new_content.append(
                {
                    "type": "text",
                    "text": (
                        f"[已省略 {len(dropped)} 张较早的图片：本地模型单次请求的图片上限]\n"
                        f"图片文件名：{json.dumps(dropped, ensure_ascii=False)}\n"
                        "（如需查看请调用 vision_analyze 工具并传入图片路径。）"
                    ),
                }
            )
            capped.append({**item, "content": new_content})
        omitted = len(positions) - len(kept)
        note = (
            f"本地模型单次请求图片上限：已省略 {omitted} 张较早的图片，"
            f"保留最近 {len(kept)} 张（被省略的仍可用 vision_analyze 按路径查看）"
        )
        return capped, note

    @staticmethod
    def _image_part_bytes(part: dict[str, Any]) -> int:
        """图片部件的近似原始字节数（``data`` 是 base64，1 字节 ≈ 4/3 个字符）。"""
        return len(str(part.get("data") or "")) * 3 // 4

    # ---- 工具处理函数（签名与 ToolRegistry 系统处理器一致）----
    def tool_handlers(self) -> dict[str, Any]:
        return {
            "vision_analyze": self._tool_visual_entry,
            "vision_image_ops": self._tool_image_ops,
        }

    def _tool_visual_entry(self, args: dict[str, Any], _skills: Any, ctx: Any) -> tuple[bool, str]:
        """vision_analyze 统一入口：按会话模型能力分流（模型全程无感）。

        - 会话无视觉能力（文本大脑）：委托视觉模型后端分析（describe 式，问题直达）；
        - 会话有视觉能力（多模态大脑）：直接把图片装入对话（装载形态）。
        分流依据为 run_context.model_has_vision（会话固化产物，非模型自省）。
        """
        if isinstance(ctx, dict) and ctx.get("model_has_vision"):
            return self._tool_read_folder(args, _skills, ctx)
        return self._tool_describe(args, _skills, ctx)

    def _tool_image_ops(self, args: dict[str, Any], skills: Any, ctx: Any) -> tuple[bool, str]:
        """vision_image_ops 统一入口：PIL 本地图像计算（colors/crop/pixel_diff 三合一）。"""
        op = str((args or {}).get("op") or "").strip()
        if op == "colors":
            return self._tool_colors(args, skills, ctx)
        if op == "crop":
            return self._tool_crop(args, skills, ctx)
        if op == "pixel_diff":
            return self._tool_pixel_diff(args, skills, ctx)
        return False, "vision_image_ops: op 需为 colors/crop/pixel_diff 之一"

    def session_tool_defs(self, model_has_vision: bool) -> dict[str, Any] | None:
        """会话化 def 覆盖（RunContext.tool_defs 生产方）：vision_analyze 按会话能力换形态。

        模型看到的工具名永远是 vision_analyze；schema 形态按会话模型能力变化：
        - model_has_vision=True（多模态）：装载形态（按装载变体的参数/描述）；
        - model_has_vision=False（文本）：分析形态（registry 基 def 原样）。
        """
        from naiba.tools.registry import vision_analyze_load_variant

        app_registry = getattr(self.app, "tool_registry", None)
        base = app_registry.get("vision_analyze") if app_registry is not None else None
        if base is None:
            return None
        if model_has_vision:
            return {"vision_analyze": vision_analyze_load_variant(base)}
        return None

    def _tool_read_folder(self, args: dict[str, Any], _skills: Any, _ctx: Any) -> tuple[bool, str]:
        """一次性从文件夹/路径读取多张图片，缓存到宿主 uploads 并生成缩略图。

        返回 JSON：{note, images:[{name,path,thumb_path,width,height}]}。供多模态模型
        作为 image content 直观读取（由 agent loop 负责把主图注入下一条消息）。
        """
        try:
            # 安全起见复用 server 的缓存逻辑，避免循环导入。
            paths: list[str] = []
            for raw in args.get("paths") or []:
                value = str(raw or "").strip()
                if value:
                    paths.append(value)
            folder = str(args.get("folder") or "").strip()
            if folder:
                paths.append(folder)
            try:
                max_images = max(1, int(args.get("max_images") or 4))
            except (TypeError, ValueError):
                max_images = 4
            return self._cache_folder_images(paths, max_images)
        except Exception as exc:  # noqa: BLE001
            return False, f"vision_analyze 失败：{exc}"

    def _cache_folder_images(self, paths: list[str], max_images: int, skip_uploads: bool = True) -> tuple[bool, str]:
        """扫描路径/文件夹里的图片，经 _process_uploaded_image 缓存到 uploads，返回带缩略图的列表。"""
        candidates_all: list[Path] = []
        seen: set[str] = set()
        for raw in paths:
            if not raw:
                continue
            p = Path(str(raw)).expanduser()
            if p.is_dir():
                for file in sorted(p.iterdir()):
                    if file.is_file() and file.suffix.lower() in IMAGE_SUFFIXES:
                        key = str(file.resolve()).lower()
                        if key not in seen:
                            seen.add(key)
                            candidates_all.append(file)
            elif p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES:
                key = str(p.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    candidates_all.append(p)
        total_candidates = len(candidates_all)
        candidates = candidates_all[:max_images]
        if not candidates:
            return False, "vision_analyze：未找到图片文件"

        target_dir = (Path(self.app.config.resolve_data_dir()) / "uploads").resolve() \
            if getattr(self.app, "config", None) else (Path.cwd() / "uploads").resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            imaging = dict(self.app.config.data.get("imaging") or {})
        except (AttributeError, TypeError, ValueError):
            imaging = {}

        import secrets as _secrets
        import time as _time
        images: list[dict[str, Any]] = []
        for file in candidates:
            try:
                data = file.read_bytes()
                fname = f"naiba_chat_{int(_time.time())}_{_secrets.token_hex(3)}_{re.sub(r'[^A-Za-z0-9._\\-\\u4e00-\\u9fff]', '_', file.name)}"
                main_bytes, thumb_name, thumb_bytes = _process_uploaded_image(data, fname, imaging)
                main_path = target_dir / fname
                main_path.write_bytes(main_bytes)
                thumb_path = ""
                if thumb_name and thumb_bytes:
                    tf = target_dir / thumb_name
                    tf.write_bytes(thumb_bytes)
                    thumb_path = str(tf)
                with io.BytesIO(main_bytes) as _b:
                    try:
                        from PIL import Image as _Image
                        _img = _Image.open(_b)
                        width, height = _img.size
                    except Exception:  # noqa: BLE001
                        width, height = 0, 0
                images.append({"name": file.name, "path": str(main_path), "thumb_path": thumb_path, "width": width, "height": height})
            except OSError:
                continue
        if not images:
            return False, "vision_analyze：图片读取/缓存失败"
        note = f"已读取 {len(images)} 张图片"
        if total_candidates > len(images):
            note += (
                f"，共发现 {total_candidates} 张（单次最多读取 {len(images)} 张），"
                f"还有 {total_candidates - len(images)} 张未读取，可继续调用本次工具"
            )
        return True, json.dumps({"note": note, "images": images}, ensure_ascii=False)

    def _resolve_paths(self, args: dict[str, Any]) -> list[str]:
        paths: list[str] = []
        for raw in args.get("paths") or args.get("attachmentIds") or []:
            value = str(raw or "").strip()
            if value:
                paths.append(value)
        single = str(args.get("image") or args.get("path") or "").strip()
        if single:
            paths.append(single)
        # Models often emit only the uploaded basename even though the
        # history contains an absolute path. Resolve that basename against
        # the current data/uploads directory (and the app directory) so a
        # harmless tool retry does not consume the whole vision budget.
        resolved: list[str] = []
        data_roots: list[Path] = []
        try:
            data_dir = self.app.config.resolve_data_dir()
            if data_dir:
                data_roots.append(Path(data_dir).expanduser().resolve())
        except (AttributeError, OSError, TypeError, ValueError):
            pass
        for value in paths:
            candidate = Path(value).expanduser()
            if candidate.is_file():
                resolved.append(str(candidate.resolve()))
                continue
            basename = candidate.name
            if basename:
                alternatives = [
                    *(root / "uploads" / basename for root in data_roots),
                    *(root / basename for root in data_roots),
                ]
                try:
                    alternatives.append(Path.cwd() / basename)
                except OSError:
                    pass
                match = next((item for item in alternatives if item.is_file()), None)
                if match is not None:
                    resolved.append(str(match.resolve()))
                    continue
            resolved.append(value)
        return resolved

    def _tool_describe(self, args: dict[str, Any], _skills: Any, _ctx: Any) -> tuple[bool, str]:
        try:
            cancel_event = _ctx.get("cancel_event") if isinstance(_ctx, dict) else None
            paths = self._resolve_paths(args)
            if not paths:
                return False, "vision_analyze：请提供 paths 或 image 参数（图片文件路径）"
            try:
                limit = max(1, min(int(args.get("max_images") or 4), 4))
            except (TypeError, ValueError):
                limit = 4
            shown = paths[:limit]
            total = len(paths)
            question = str(args.get("question") or "")
            json_mode = bool(args.get("json"))
            result = self.describe_files(shown, question, json_mode, cancel_event, self._context_budget(_ctx))
            if total > len(shown):
                result += (
                    f"\n（本次共 {total} 张，已分析前 {len(shown)} 张；"
                    f"其余 {total - len(shown)} 张可继续使用本工具，传剩余路径即可。）"
                )
            return True, result
        except Exception as exc:  # noqa: BLE001
            return False, f"vision_analyze 失败：{exc}"

    @staticmethod
    def _extract_json(text: str) -> Any:
        cleaned = (text or "").strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            for index, char in enumerate(cleaned):
                if char in "{[":
                    try:
                        value, _ = decoder.raw_decode(cleaned[index:])
                        return value
                    except json.JSONDecodeError:
                        continue
            return None

    def _tool_ground_retired_note(self) -> None:
        """（legacy 预留）结构化定位/检测管线已随「单入口 vision_analyze」废弃：

        旧 _tool_ground / _tool_detect / _tool_ocr 与 _tool_describe 共享同一视觉后端，
        差异仅为 prompt 模板与输出解析（坐标框/标注图）。单入口由提问直达视觉后端，
        用户意图由问题本身表达；_extract_json / _annotate_box 保留备用，
        若未来需要恢复结构化坐标输出可从本处继续使用。
        """

    def _tool_crop(self, args: dict[str, Any], _skills: Any, _ctx: Any) -> tuple[bool, str]:
        try:
            paths = self._resolve_paths(args)
            if not paths:
                return False, "vision_image_ops(crop)：请提供 image 参数"
            region = str(args.get("region") or "").strip()
            parts = [int(x) for x in region.replace(",", " ").split() if x.strip().lstrip("-").isdigit() or x.strip().isdigit()]
            if len(parts) != 4:
                return False, "vision_image_ops(crop)：region 需为 x1,y1,x2,y2 四个整数"
            x1, y1, x2, y2 = parts
            size = _image_size(paths[0])
            if not size:
                return False, "vision_image_ops(crop)：无法读取图片尺寸"
            width, height = size
            x1, x2 = sorted((max(0, min(x1, width)), max(0, min(x2, width))))
            y1, y2 = sorted((max(0, min(y1, height)), max(0, min(y2, height))))
            if x2 - x1 < 1 or y2 - y1 < 1:
                return False, "vision_image_ops(crop)：裁剪区域为空"
            from PIL import Image

            with Image.open(paths[0]) as image:
                image.load()
                cropped = image.crop((x1, y1, x2, y2)).convert("RGB")
            # 默认保存到当前会话工作区根目录（用户可直接看到产物；模型也可直接引用）。
            out = self._artifact_path("crop", ".png", base_dir=self._run_workspace_dir(_ctx))
            cropped.save(out, format="PNG")
            return True, json.dumps(
                {"path": str(out), "size": [cropped.size[0], cropped.size[1]], "box": [x1, y1, x2, y2]},
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"vision_image_ops(crop) 失败：{exc}"

    def _tool_colors(self, args: dict[str, Any], _skills: Any, _ctx: Any) -> tuple[bool, str]:
        try:
            paths = self._resolve_paths(args)
            if not paths:
                return False, "vision_image_ops(colors)：请提供 image 参数"
            top = min(max(int(args.get("top", 6)), 1), 20)
            image = _read_rgb(paths[0])
            small = image.copy()
            small.thumbnail((200, 200))
            quantized = small.quantize(colors=top, method=2)  # MEDIANCUT
            palette = quantized.getpalette() or []
            counts = quantized.getcolors() or []
            total = sum(count for count, _ in counts)
            rows = []
            for count, index in sorted(counts, reverse=True):
                base = index * 3
                if base + 2 >= len(palette):
                    continue
                r, g, b = palette[base], palette[base + 1], palette[base + 2]
                rows.append(
                    {"hex": f"#{r:02x}{g:02x}{b:02x}", "share": round(count / total, 4) if total else 0.0}
                )
            return True, json.dumps({"colors": rows}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return False, f"vision_image_ops(colors) 失败：{exc}"

    def _tool_pixel_diff(self, args: dict[str, Any], _skills: Any, _ctx: Any) -> tuple[bool, str]:
        try:
            original = str(args.get("original") or "").strip()
            rebuilt = str(args.get("rebuilt") or "").strip()
            if not original or not rebuilt:
                return False, "vision_image_ops(pixel_diff)：请提供 original 与 rebuilt 参数"
            threshold = min(max(int(args.get("threshold", 16)), 0), 255)
            a = _read_rgb(original)
            b = _read_rgb(rebuilt)
            width = min(a.size[0], b.size[0])
            height = min(a.size[1], b.size[1])
            a = a.resize((width, height))
            b = b.resize((width, height))
            pa = a.load()
            pb = b.load()
            from PIL import Image, ImageDraw

            heat = Image.new("RGB", (width, height), (255, 255, 255))
            ph = heat.load()
            grid = 8
            grid_diff: dict[tuple[int, int], int] = {}
            total_pixels = width * height
            diff_count = 0
            for y in range(height):
                for x in range(width):
                    ra, ga, ba = pa[x, y][:3]
                    rb, gb, bb = pb[x, y][:3]
                    differs = (
                        abs(ra - rb) > threshold
                        or abs(ga - gb) > threshold
                        or abs(ba - bb) > threshold
                    )
                    if differs:
                        diff_count += 1
                        ph[x, y] = (230, 60, 60)
                        gx, gy = x * grid // width, y * grid // height
                        grid_diff[(gx, gy)] = grid_diff.get((gx, gy), 0) + 1
                    else:
                        ph[x, y] = pa[x, y][:3]
            worst = sorted(grid_diff.items(), key=lambda item: -item[1])[:5]
            out = self._artifact_path("diff", ".png", base_dir=self._run_workspace_dir(_ctx))
            heat.save(out, format="PNG")
            return True, json.dumps(
                {
                    "ratio": round(diff_count / total_pixels, 6) if total_pixels else 0.0,
                    "differing_pixels": diff_count,
                    "total_pixels": total_pixels,
                    "threshold": threshold,
                    "heatmap": str(out),
                    "worst_regions": [
                        {"grid": [gx, gy], "pixels": count} for (gx, gy), count in worst
                    ],
                },
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"vision_image_ops(pixel_diff) 失败：{exc}"

    # ---- 内部工具 ----
    @staticmethod
    def _context_cancel_event(ctx: Any) -> threading.Event | None:
        return ctx.get("cancel_event") if isinstance(ctx, dict) else None

    @staticmethod
    def _context_budget(ctx: Any) -> VisionBudget | None:
        budget = ctx.get("vision_budget") if isinstance(ctx, dict) else None
        return budget if isinstance(budget, VisionBudget) else None

    @staticmethod
    def _budget_key(
        parts: list[dict[str, Any]], prompt: str, operation: str, max_tokens: int
    ) -> str:
        digest = hashlib.sha256()
        digest.update(operation.encode("utf-8"))
        digest.update(b"\0")
        digest.update(prompt.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(max_tokens).encode("ascii"))
        for part in parts:
            digest.update(str(part.get("data") or "").encode("ascii", errors="ignore"))
        return digest.hexdigest()

    def _budgeted_backend_call(
        self, profile: dict[str, Any], parts: list[dict[str, Any]], prompt: str,
        max_tokens: int = 2048, cancel_event: threading.Event | None = None,
        budget: VisionBudget | None = None, operation: str = "vision_analyze",
    ) -> str:
        key = self._budget_key(parts, prompt, operation, max_tokens)
        if budget:
            cached = budget.cached(key)
            if cached is not None:
                return cached
            timeout = budget.begin(key)
        else:
            timeout = None
        result = self._call_backend(
            profile, parts, prompt, max_tokens, timeout_seconds=timeout, cancel_event=cancel_event
        )
        if budget and result:
            budget.store(key, result)
        return result

    def _call_with_first_backend(
        self,
        parts: list[dict[str, Any]],
        prompt: str,
        max_tokens: int,
        cancel_event: threading.Event | None = None,
        vision_budget: VisionBudget | None = None,
        operation: str = "vision_analyze",
    ) -> str:
        errors: list[str] = []
        for profile in self.vision_backends():
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("任务已取消")
            try:
                content = self._budgeted_backend_call(
                    profile, parts, prompt, max_tokens, cancel_event, vision_budget, operation
                )
                if content and content.strip():
                    return content
                errors.append(f"{profile.get('name')}：空响应")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{profile.get('name')}：{exc}")
        raise RuntimeError("视觉后端全部失败：" + "；".join(errors[-6:]))

    def _artifact_dir(self) -> Path:
        workspace = self.app.config.resolve_workspace_dir()
        folder = workspace / ".naiba-chat" / "vision"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _run_workspace_dir(self, ctx: Any) -> Path:
        """当前运行会话的工作区（run_context.workspace_dir）；缺失时兜底默认工作区。

        crop/pixel_diff 产物默认落这里：产物是用户要的交付物，直接放在工作区根目录，
        用户与模型都能直接引用；不再藏进 .naiba-chat/vision 隐藏目录。
        """
        try:
            raw = str((ctx or {}).get("workspace_dir") or "").strip()
            if raw:
                return Path(raw).expanduser().resolve()
        except (TypeError, ValueError, OSError):
            pass
        return self._artifact_dir().parent

    def _artifact_path(self, prefix: str, suffix: str, base_dir: Path | None = None) -> Path:
        stamp = f"{int(time.time() * 1000)}_{secrets_token(4)}"
        directory = base_dir if base_dir is not None else self._artifact_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            directory = self._artifact_dir()
        return directory / f"{prefix}_{stamp}{suffix}"

    def _annotate_box(self, path: str, x1: int, y1: int, x2: int, y2: int) -> str | None:
        """在图上画出目标框并写回工作区 .naiba-chat/vision/，返回标注图路径。"""
        try:
            from PIL import Image, ImageDraw

            with Image.open(path) as image:
                image.load()
                annotated = image.convert("RGB")
            draw = ImageDraw.Draw(annotated)
            draw.rectangle([x1, y1, x2, y2], outline=(230, 60, 60), width=max(2, max(annotated.size) // 400))
            out = self._artifact_path("annotated", ".png")
            annotated.save(out, format="PNG")
            return str(out)
        except Exception:
            return None

    @staticmethod
    def _probe_error(
        probe: str, profile: dict[str, Any], error: Exception
    ) -> tuple[str, str]:
        reason = str(error)
        lowered = reason.lower()
        is_llama_cpp = (
            str(profile.get("kind") or "").lower() == "local"
            and (
                str(profile.get("local_backend") or "").lower() in {"llama_cpp", "unsloth"}
                or str(profile.get("request_format") or "").lower() in {"llama_cpp", "unsloth"}
            )
        )
        if any(token in lowered for token in (
            "connection refused", "unable to connect", "url error", "network is unreachable",
            "connection reset", "connection aborted", "timed out",
        )):
            return "connection", "\u65e0\u6cd5\u8fde\u63a5\u5230\u670d\u52a1\u3002\u8bf7\u68c0\u67e5 Base URL\u3001\u670d\u52a1\u72b6\u6001\u548c\u7f51\u7edc\u3002"
        if probe == "text":
            return "text_inference", "\u6587\u672c\u63a8\u7406\u5931\u8d25\uff0c/models \u53ef\u8bbf\u95ee\u4e0d\u4ee3\u8868\u6a21\u578b\u53ef\u4ee5\u63a8\u7406\u3002"
        if "failed to load image or audio file" in lowered:
            hint = "\u56fe\u7247\u5728\u670d\u52a1\u7aef\u52a0\u8f7d\u5931\u8d25\u3002"
            if is_llama_cpp:
                hint += " \u8bf7\u786e\u8ba4 GGUF \u662f\u89c6\u89c9\u6a21\u578b\uff0c\u5df2\u52a0\u8f7d\u5339\u914d\u7684 mmproj\uff0c\u5e76\u68c0\u67e5 llama-server \u65e5\u5fd7\u3002"
            return "image_load", hint
        if any(token in lowered for token in (
            "mmproj", "vision model", "does not support image", "doesn't support image",
            "multimodal", "image input",
        )):
            hint = "\u5f53\u524d\u6a21\u578b\u53ef\u80fd\u4e0d\u652f\u6301\u89c6\u89c9\u8f93\u5165\u3002"
            if is_llama_cpp:
                hint += " \u8bf7\u786e\u8ba4\u4f7f\u7528\u89c6\u89c9 GGUF \u5e76\u52a0\u8f7d\u5339\u914d\u7684 mmproj\u3002"
            return "vision_capability", hint
        return "unknown", "\u89c6\u89c9\u63a8\u7406\u5931\u8d25\u3002\u8bf7\u4fdd\u7559\u4e0b\u65b9\u7684\u670d\u52a1\u7aef\u8fd4\u56de\u4fe1\u606f\u4ee5\u4fbf\u6392\u67e5\u3002"

    def _probe(
        self,
        provider_model_key: str | None,
        probe: str,
        image_parts: list[dict[str, Any]],
        prompt: str,
    ) -> dict[str, Any]:
        backends = self.vision_backends(provider_model_key)
        if not backends:
            return {"ok": False, "probe": probe, "error_kind": "unknown", "reason": "No vision backend is configured."}
        try:
            configured_timeout = int(self.config().get("timeout_ms", 30000)) / 1000
        except (TypeError, ValueError):
            configured_timeout = 30
        probe_timeout = max(3, min(configured_timeout, 30))
        errors: list[str] = []
        last_kind = "unknown"
        last_hint = ""
        for profile in backends:
            name = str(profile.get("name") or profile.get("model") or "Unknown backend")
            endpoint = str(profile.get("base_url") or "").rstrip("/")
            started = time.monotonic()
            try:
                content = self._call_backend(
                    profile, image_parts, prompt, max_tokens=16,
                    timeout_seconds=probe_timeout, attempts=1, connection_test=True,
                )
                latency = int((time.monotonic() - started) * 1000)
                if content and content.strip():
                    return {
                        "ok": True, "probe": probe, "latency_ms": latency,
                        "backend": name, "model": profile.get("model"), "endpoint": endpoint,
                    }
                errors.append(f"{name}: empty response")
                last_kind = "text_inference" if probe == "text" else "vision_capability"
                last_hint = "\u6a21\u578b\u672a\u8fd4\u56de\u6709\u6548\u7684\u63a8\u7406\u7ed3\u679c\u3002"
            except Exception as exc:  # noqa: BLE001 - try the configured failover chain
                last_kind, last_hint = self._probe_error(probe, profile, exc)
                errors.append(f"{name}: {exc}")
        last = backends[-1]
        return {
            "ok": False, "probe": probe, "error_kind": last_kind, "hint": last_hint,
            "reason": "; ".join(errors[-6:]),
            "backend": str(last.get("name") or last.get("model") or "Unknown backend"),
            "endpoint": str(last.get("base_url") or "").rstrip("/"),
        }

    def probe_text(self, provider_model_key: str | None = None) -> dict[str, Any]:
        """Probe text inference independently from image-input capability."""
        return self._probe(provider_model_key, "text", [], "\u53ea\u56de\u590d OK")

    def probe(self, provider_model_key: str | None = None) -> dict[str, Any]:
        """Probe actual image-input capability with a valid RGB JPEG."""
        image_part = {"type": "image", "media_type": "image/jpeg", "data": PROBE_JPEG_B64}
        return self._probe(
            provider_model_key, "vision", [image_part],
            "\u8fd9\u662f\u4e00\u5f20\u6d4b\u8bd5\u56fe\u3002\u8bf7\u53ea\u56de\u590d OK\u3002",
        )

    def _legacy_probe(self, provider_model_key: str | None = None) -> dict[str, Any]:
        """发送真实的最小图片请求探测视觉后端可用性（不再仅用 /models 可达性冒充能力）。

        按 vision_backends() 顺序 failover；显式选择时不会静默落到匿名 OVH。
        成功返回首个可用后端的名称与延迟；全部失败返回聚合原因与最后尝试的后端名称。
        """
        backends = self.vision_backends(provider_model_key)
        if not backends:
            return {"ok": False, "reason": "没有可用的视觉后端"}
        image_part = {"type": "image", "media_type": "image/jpeg", "data": PROBE_JPEG_B64}
        prompt = "这是一张 1x1 测试图。请只回复两个字：OK。"
        try:
            configured_timeout = int(self.config().get("timeout_ms", 30000)) / 1000
        except (TypeError, ValueError):
            configured_timeout = 30
        probe_timeout = max(3, min(configured_timeout, 30))
        errors: list[str] = []
        for profile in backends:
            name = str(profile.get("name") or profile.get("model") or "未知后端")
            endpoint = str(profile.get("base_url") or "").rstrip("/")
            started = time.monotonic()
            try:
                content = self._call_backend(
                    profile,
                    [image_part],
                    prompt,
                    max_tokens=16,
                    timeout_seconds=probe_timeout,
                    attempts=1,
                    connection_test=True,
                )
                latency = int((time.monotonic() - started) * 1000)
                if content and content.strip():
                    return {
                        "ok": True,
                        "latency_ms": latency,
                        "backend": name,
                        "model": profile.get("model"),
                        "endpoint": endpoint,
                    }
                errors.append(f"{name}：空响应")
            except Exception as exc:  # noqa: BLE001 - 逐后端降级
                errors.append(f"{name}：{exc}")
        last = backends[-1]
        return {
            "ok": False,
            "reason": "；".join(errors[-6:]),
            "backend": str(last.get("name") or last.get("model") or "未知后端"),
            "endpoint": str(last.get("base_url") or "").rstrip("/"),
        }


def secrets_token(length: int) -> str:
    import secrets as _secrets

    return _secrets.token_hex(max(1, length // 2 + 1))[:length]
