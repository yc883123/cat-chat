"""net_io - 统一网络请求入口（代理策略 / 本地直连 / 诊断状态）

所有由应用主动发起的 HTTP/HTTPS 请求都应改走 ``net_io.open()`` 发出，
这样它们能共享同一套代理策略：

* 本地服务（127.0.0.1 / localhost / ::1 / 私有网段 / 链路本地地址）永远直连，
  不受“系统代理”或手动代理影响。Ollama、LM Studio、llama.cpp、ComfyUI 等
  常见本地服务都落在这些网段内。
* 外部请求按「运行设置 → 代理」字段路由：

  - ``enabled=false``            -> 强制直连（忽略系统代理与环境变量）
  - ``enabled=true, url 非空``    -> 使用手动代理地址（仅支持 HTTP/HTTPS）
  - ``enabled=true, url 为空``    -> 按 ``use_system_fallback`` 决定：
                                    true 回退系统代理；false 则直连
  * 旧配置文件未含 ``proxy`` 字段时保持历史兼容行为（等效于走系统代理），
    避免老用户升级后网络行为突变。

``configure()`` 在每次设置保存后调用，会立即重建 opener 缓存，无需重启。

异常语义保持不变：底层 ``urllib`` / ``OSError`` 异常原样上抛（调用方按
HTTPError / URLError / socket 错误处理），便于各模块保持现有重试与错误分类。
"""

from __future__ import annotations

import ipaddress
import threading
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

_LOCAL_HOST_SUFFIX = (".localhost", ".local", ".lan")


def _is_local_host(host: str) -> bool:
    """判断目标 host 是否属于本地直连范围（不回退、不代理）。"""
    value = (host or "").strip().lower().rstrip(".")
    if not value:
        return False
    if value in {"localhost", "127.0.0.1", "::1"}:
        return True
    if value.startswith("127.") or any(value.endswith(suffix) for suffix in _LOCAL_HOST_SUFFIX):
        return True
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        # 非 IP 主机名：按外部域名处理，交给代理策略。
        return False
    return bool(address.is_loopback or address.is_private or address.is_link_local)


def _normalize_url(raw: str) -> str:
    """规范化用户填写的代理地址，仅接受 HTTP/HTTPS。"""
    value = (raw or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"http://{value}"
    parts = urlsplit(value)
    if not parts.scheme or parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"代理地址格式不正确：{raw}（示例：http://127.0.0.1:7890）")
    return value


class NetIO:
    """代理策略状态机与 opener 工厂（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # None 表示从未配置（旧版行为：跟随系统代理）；dict 来自运行设置 proxy 字段。
        self._configured: dict[str, Any] | None = None
        self._manual_opener: urllib.request.OpenerDirector | None = None
        self._direct_opener: urllib.request.OpenerDirector | None = None
        self._system_opener: urllib.request.OpenerDirector | None = None
        self._config_error: str = ""
        # scoped 覆盖项（如「更新代理」）的 opener 缓存：键为 (mode, url)。
        self._scoped_openers: dict[tuple[str, str], urllib.request.OpenerDirector] = {}

    # ---- 配置与状态 ----

    def configure(self, proxy_settings: dict[str, Any] | None) -> None:
        """应用新的代理设置；传入 None 表示保持旧版“跟随系统代理”兼容行为。"""
        with self._lock:
            # Every configuration update invalidates all opener instances. This
            # includes system/direct openers whose handlers may capture stale
            # environment state.
            self._manual_opener = None
            self._direct_opener = None
            self._system_opener = None
            self._scoped_openers = {}
            self._config_error = ""
            if proxy_settings is None:
                self._configured = None
                return
            if not isinstance(proxy_settings, dict):
                proxy_settings = {}
            enabled = bool(proxy_settings.get("enabled", False))
            try:
                url = _normalize_url(str(proxy_settings.get("url") or ""))
            except ValueError as exc:
                url = ""
                self._config_error = str(exc)
            use_system_fallback = bool(proxy_settings.get("use_system_fallback", True))
            self._configured = {
                "enabled": enabled,
                "url": url,
                "use_system_fallback": use_system_fallback,
            }
            self._manual_opener = self._build_manual_opener(url) if url else None

    def proxy_state(self) -> dict[str, Any]:
        """返回当前代理策略状态，供设置页与 API 测试展示“实际生效模式”。"""
        with self._lock:
            configured = self._configured
            config_error = self._config_error
        def finish(state: dict[str, Any]) -> dict[str, Any]:
            if config_error:
                state["error"] = config_error
            return state
        if configured is None:
            return finish({
                "enabled": False,
                "url": "",
                "source": "system",
                "note": "旧配置兼容：未设置代理开关，按系统代理发送外部请求。",
            })
        enabled = bool(configured["enabled"])
        url = configured["url"]
        use_system_fallback = bool(configured["use_system_fallback"])
        if not enabled:
            return finish({
                "enabled": False,
                "url": "",
                "source": "direct",
                "note": "代理已关闭：外部请求强制直连，忽略系统代理。",
            })
        if config_error:
            return finish({
                "enabled": enabled,
                "url": "",
                "source": "direct",
                "note": "代理地址无效：仅支持 HTTP/HTTPS，当前请求强制直连。",
            })
        if url:
            return finish({
                "enabled": True,
                "url": url,
                "source": "manual",
                "note": "外部请求使用手动代理。",
            })
        if use_system_fallback:
            return finish({
                "enabled": True,
                "url": "",
                "source": "system",
                "note": "已开启代理但未填地址：按设置回退到系统代理。",
            })
        return finish({
            "enabled": True,
            "url": "",
            "source": "direct",
            "note": "已开启代理但未填地址且未启用系统回退：当前外部请求直连。",
        })

    # ---- 内部 opener 选择 ----

    @staticmethod
    def _build_manual_opener(url: str) -> urllib.request.OpenerDirector:
        """按手动代理地址构建 opener（唯一构造点，供 configure/重试共用）。"""
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": url, "https": url}))

    def _opener_for(self, host: str, local: bool) -> urllib.request.OpenerDirector:
        with self._lock:
            configured = self._configured
            if configured is not None and not configured["enabled"]:
                # 显式关闭代理：强制直连（即使地址非空也被忽略）。
                if self._direct_opener is None:
                    self._direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                return self._direct_opener
        if local:
            if self._direct_opener is None:
                self._direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            return self._direct_opener
        if configured is None:
            # 历史兼容：从未配置代理开关，跟随系统代理。
            if self._system_opener is None:
                self._system_opener = urllib.request.build_opener(urllib.request.ProxyHandler())
            return self._system_opener
        if configured.get("url"):
            # 缓存缺失时按当前策略重建手动代理 opener。绝不能退回 build_opener()
            # （空 ProxyHandler 一族会静默直连）——那等于把一次抖动变成绕开代理。
            if self._manual_opener is None:
                self._manual_opener = self._build_manual_opener(str(configured["url"]))
            return self._manual_opener
        if configured.get("use_system_fallback"):
            if self._system_opener is None:
                self._system_opener = urllib.request.build_opener(urllib.request.ProxyHandler())
            return self._system_opener
        if self._direct_opener is None:
            self._direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return self._direct_opener

    def _refresh_opener(self, host: str, local: bool) -> urllib.request.OpenerDirector:
        """丢弃当前策略下的 opener 并创建新实例，用于一次受控重试。

        分支必须与 ``_opener_for`` 的最终选择逐一对齐，否则会出现
        “重试却复用同一个失败 opener”的空转：例如「代理已开启 + 未填地址 +
        关闭系统回退」在旧写法下不匹配任何分支，直连 opener 不会被丢弃。
        """
        with self._lock:
            configured = self._configured
            if local or (configured is not None and not configured.get("enabled")):
                self._direct_opener = None
            elif configured is None:
                self._system_opener = None
            elif configured.get("url"):
                self._manual_opener = None
            elif configured.get("use_system_fallback"):
                self._system_opener = None
            else:
                self._direct_opener = None
        return self._opener_for(host, local)

    # ---- scoped 覆盖项（「更新代理」等按功能单独指定的出站策略） ----

    def normalize_override(self, override: dict[str, Any] | None) -> dict[str, str]:
        """归一化 scoped 覆盖项。``inherit``/缺失/非法值一律视为「跟随全局策略」。

        ``manual`` 但地址为空时退回 ``system``（与全局代理「开了但没填地址」的
        回退语义一致），避免出现「显式选了手动代理却静默直连」这种最难查的形态。
        """
        if not isinstance(override, dict):
            return {"mode": "inherit", "url": ""}
        mode = str(override.get("mode") or "inherit").strip().lower()
        if mode not in {"inherit", "system", "direct", "manual"}:
            mode = "inherit"
        try:
            url = _normalize_url(str(override.get("url") or ""))
        except ValueError:
            url = ""
        if mode == "manual" and not url:
            mode = "system"
        return {"mode": mode, "url": url}

    def _scoped_opener(self, mode: str, url: str) -> urllib.request.OpenerDirector:
        """按 scoped 模式取（并缓存）独立 opener；与全局 opener 完全隔离。"""
        key = (mode, url)
        with self._lock:
            cached = self._scoped_openers.get(key)
            if cached is not None:
                return cached
        if mode == "direct":
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        elif mode == "manual":
            opener = self._build_manual_opener(url)
        else:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler())
        with self._lock:
            self._scoped_openers[key] = opener
        return opener

    def describe_override(self, override: dict[str, Any] | None) -> dict[str, str]:
        """把 scoped 覆盖项翻译成给用户看的「本次请求走哪里」。"""
        normalized = self.normalize_override(override)
        mode = normalized["mode"]
        if mode == "manual":
            return {"mode": mode, "note": f"手动代理 {normalized['url']}"}
        if mode == "system":
            return {"mode": mode, "note": "系统代理"}
        if mode == "direct":
            return {"mode": mode, "note": "直连（忽略系统代理）"}
        state = self.proxy_state()
        note = str(state.get("note") or "").strip()
        return {"mode": "inherit", "note": f"跟随全局设置（{note}）" if note else "跟随全局设置"}

    def open_scoped(
        self,
        request: str | urllib.request.Request,
        override: dict[str, Any] | None = None,
        timeout: float | None = None,
    ):
        """按 scoped 覆盖项发起请求；覆盖项为 inherit 时回落全局策略。

        与 ``open()`` 同语义：HTTPError 直接上抛不重试；网络类错误仅对幂等
        请求重建 opener 后重试一次（下载/清单都是 GET）。
        """
        target = request
        if isinstance(request, str):
            target = urllib.request.Request(request)
        normalized = self.normalize_override(override)
        if normalized["mode"] == "inherit":
            return self.open(target, timeout=timeout)
        mode, url = normalized["mode"], normalized["url"]
        opener = self._scoped_opener(mode, url)
        try:
            return opener.open(target, timeout=timeout)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            method_name = getattr(target, "get_method", lambda: "GET")().upper()
            if method_name not in {"GET", "HEAD", "OPTIONS"}:
                raise
            with self._lock:
                self._scoped_openers.pop((mode, url), None)
            return self._scoped_opener(mode, url).open(target, timeout=timeout)

    # ---- 统一入口 ----

    def open(
        self,
        request: str | urllib.request.Request,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        method: str | None = None,
    ):
        """统一发起一次 HTTP/HTTPS 请求，返回 file-like response。

        参数兼容两种用法：
        * ``net_io.open(urllib.request.Request(url, headers=..., method=...), timeout=...)``
        * ``net_io.open(url, timeout=..., headers=..., data=..., method=...)``
        """
        target = request
        if isinstance(request, str):
            req = urllib.request.Request(request, data=data, headers=headers or {}, method=method)
            target = req
        try:
            host = urlsplit(target.full_url).hostname or ""
        except ValueError:
            host = ""
        local = _is_local_host(host)
        opener = self._opener_for(host, local)
        try:
            return opener.open(target, timeout=timeout)
        except urllib.error.HTTPError:
            # 服务端已返回明确 HTTP 状态时不重试，交由上层按状态分类。
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            # 更新清单/下载均为 GET；仅对幂等请求重建当前策略 opener 后
            # 重试一次，避免对 POST 等有副作用请求重复提交。
            method_name = getattr(target, "get_method", lambda: "GET")().upper()
            if method_name not in {"GET", "HEAD", "OPTIONS"}:
                raise
            refreshed = self._refresh_opener(host, local)
            return refreshed.open(target, timeout=timeout)


# 模块级单例：核心进程内所有模块共用同一策略状态。
registry = NetIO()


def configure(proxy_settings: dict[str, Any] | None) -> None:
    registry.configure(proxy_settings)


def proxy_state() -> dict[str, Any]:
    return registry.proxy_state()


def open(
    request: str | urllib.request.Request,
    timeout: float | None = None,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    method: str | None = None,
):
    return registry.open(request, timeout=timeout, headers=headers, data=data, method=method)


def open_scoped(
    request: str | urllib.request.Request,
    override: dict[str, Any] | None = None,
    timeout: float | None = None,
):
    """按 scoped 覆盖项发起请求（见 :meth:`NetIO.open_scoped`）。"""
    return registry.open_scoped(request, override=override, timeout=timeout)


def describe_override(override: dict[str, Any] | None) -> dict[str, str]:
    return registry.describe_override(override)
