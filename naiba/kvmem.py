"""安全的 KVMem 本机控制台适配层。

KVMem 的 worker 生命周期只能由 KVMem controller 管理。Cat Chat 只调用
controller API，不扫描端口、不猜 PID、不终止外部进程。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from naiba import net as net_io

DEFAULT_MANAGER_URL = "http://127.0.0.1:18199"
DEFAULT_INFERENCE_URL = "http://127.0.0.1:18200/v1"
REQUEST_TIMEOUT = 3.0
FALLBACK_PARAMETER_KEYS = ("Backend", "Model", "Worker", "Gpu")
LAST_START_FILENAME = "last-start.json"
# 状态轮询和启动请求来自不同 HTTP 线程；原子替换不能代替读改写锁。
_LAST_START_LOCK = threading.RLock()


class KVMemError(RuntimeError):
    """用户可见的、已脱敏的 KVMem 错误。"""


def validate_manager_url(raw: str) -> str:
    value = str(raw or DEFAULT_MANAGER_URL).strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("KVMem 控制台地址必须是 http/https URL")
    host = parsed.hostname.lower().rstrip(".")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("KVMem 控制台只允许本机回环地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("KVMem 控制台地址不能包含凭据、查询参数或片段")
    if parsed.path not in {"", "/"}:
        raise ValueError("KVMem 控制台地址不能包含路径")
    return value


def _text(value: Any, limit: int = 240) -> str:
    result = str(value or "").strip()
    return result[:limit]


def _port_from_url(url: str) -> int | None:
    try:
        port = urllib.parse.urlsplit(url).port
    except ValueError:
        return None
    return int(port) if port else None


def _nested(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return None


def last_start_path(data_dir: Any) -> Path:
    return Path(data_dir) / "kvmem" / LAST_START_FILENAME


def _pick_parameters(parameters: Any) -> dict[str, str]:
    if not isinstance(parameters, dict):
        return {}
    return {key: str(parameters[key]).strip() for key in FALLBACK_PARAMETER_KEYS
            if parameters.get(key) is not None and str(parameters[key]).strip()}


def read_last_start(path: Any) -> dict[str, dict[str, str]]:
    """兼容旧的按控制台索引；损坏缓存不能阻止控制台操作。"""
    with _LAST_START_LOCK:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(url): picked for url, parameters in data.items()
                if (picked := _pick_parameters(parameters)).get("Model")}


def write_last_start(path: Any, manager_url: str, parameters: Any) -> None:
    """只缓存公开启动字段；完整读改写串行，写失败不影响启动。"""
    picked = _pick_parameters(parameters)
    if not manager_url or not picked.get("Model"):
        return
    target = Path(path)
    temporary: str | None = None
    with _LAST_START_LOCK:
        try:
            data = read_last_start(target)
            if data.get(str(manager_url)) == picked:
                return
            data[str(manager_url)] = picked
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                             prefix="last-start-", suffix=".tmp", delete=False) as handle:
                temporary = handle.name
                json.dump(data, handle, ensure_ascii=False, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            temporary = None
        except OSError:
            pass
        finally:
            if temporary:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass


class KVMemClient:
    def __init__(self, manager_url: str, inference_url: str, *, timeout: float = REQUEST_TIMEOUT):
        self.manager_url = validate_manager_url(manager_url)
        self.inference_url = str(inference_url or DEFAULT_INFERENCE_URL).strip().rstrip("/")
        parsed = urllib.parse.urlsplit(self.inference_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("KVMem 推理地址必须是 http/https URL")
        self.timeout = max(0.5, min(float(timeout), 10.0))
        self.last_draft_parameters: dict[str, str] = {}

    def _request(self, path: str, *, method: str = "GET", token: str = "", body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.manager_url}{path}"
        headers = {
            "Accept": "application/json",
            "User-Agent": "CatChat/KVMem",
            "Host": urllib.parse.urlsplit(self.manager_url).netloc,
            "Origin": self.manager_url,
        }
        if token:
            headers["X-KVMem-Token"] = token
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with net_io.open(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read(4096).decode("utf-8", errors="replace"))
            except (ValueError, OSError):
                detail = {}
            message = str(detail.get("error") or "").strip() if isinstance(detail, dict) else ""
            raise KVMemError(_text(message, 400) or "KVMem 控制台返回了错误") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise KVMemError("KVMem 控制台不可达或返回了无效响应") from None
        if not isinstance(payload, dict):
            raise KVMemError("KVMem 控制台响应格式不正确")
        return payload

    @staticmethod
    def _token(info: dict[str, Any]) -> str:
        candidate = _nested(info, "token", "launcher_token", "csrf_token")
        if not candidate and isinstance(info.get("auth"), dict):
            candidate = _nested(info["auth"], "token", "launcher_token", "csrf_token")
        return str(candidate or "").strip()

    @staticmethod
    def _protocol_ok(info: dict[str, Any]) -> bool:
        marker = _nested(info, "service", "name", "product", "controller")
        if marker is not None and "kvmem" not in str(marker).lower():
            return False
        version = _nested(info, "protocol_version", "api_version")
        if version is None:
            return True
        try:
            return int(str(version).split(".", 1)[0]) >= 1
        except (TypeError, ValueError):
            return False

    def info(self) -> dict[str, Any]:
        info = self._request("/api/launcher/info")
        if not self._protocol_ok(info):
            raise KVMemError("KVMem 控制台协议版本不兼容")
        draft = info.get("draft")
        self.last_draft_parameters = _pick_parameters(draft.get("parameters") if isinstance(draft, dict) else None)
        return info

    def status(self) -> dict[str, Any]:
        info = self.info()
        status = self._request("/api/launcher/status")
        return self._status_payload(info, status)

    def _status_payload(self, info: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
        state = str(_nested(status, "status", "state", "phase") or "unknown").strip().lower()
        mapping = {
            "idle": "stopped", "stopped": "stopped", "down": "stopped", "exited": "stopped",
            "starting": "starting", "loading": "starting", "ready": "ready",
            "running": "running", "healthy": "ready", "failed": "failed", "error": "failed",
        }
        draft = _nested(info, "draft", "saved_draft")
        draft = draft if isinstance(draft, dict) else {}
        backend = _nested(draft, "backend", "engine", "runtime")
        model = _nested(draft, "model", "model_path", "model_file", "model_name")
        engine = _nested(draft, "engine_params", "engine", "params")
        actual_port = _nested(status, "port", "inference_port", "server_port") or _nested(draft, "port", "inference_port", "server_port")
        if not actual_port:
            actual_port = _port_from_url(self.inference_url)
        payload = {
            "ok": True,
            "status": mapping.get(state, state if state in {"unknown", "starting", "ready", "running", "failed", "stopped"} else "unknown"),
            "raw_status": state,
            "backend": _text(backend, 100),
            "model": _text(model, 300),
            "engine_summary": _text(json.dumps(engine, ensure_ascii=False, separators=(",", ":")) if isinstance(engine, (dict, list)) else engine, 600),
            "actual_port": int(actual_port) if str(actual_port).isdigit() else None,
            "inference_url": self.inference_url,
            "manager_url": self.manager_url,
        }
        expected_port = _port_from_url(self.inference_url)
        if payload["actual_port"] and expected_port and payload["actual_port"] != expected_port:
            payload["status"] = "port_mismatch"
            payload["error"] = "KVMem 实际推理端口与 Cat Chat 配置不一致"
        return payload

    def start(self, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
        info = self.info()
        token = self._token(info)
        if not token:
            raise KVMemError("KVMem 控制台未提供临时控制令牌")
        # 控制台要求完整 {'schema_version': 1, 'parameters': {...}} 结构，原样回传它自己的 draft。
        draft = info.get("draft") if isinstance(info.get("draft"), dict) else None
        params = draft.get("parameters") if draft else None
        if not isinstance(params, dict):
            raise KVMemError("KVMem 控制台没有可用的启动配置")
        self.fill_missing_parameters(params, fallback)
        if not str(params.get("Model") or "").strip():
            raise KVMemError("KVMem 尚未选择模型，请先在控制台选择模型后再启动；成功启动后 Cat Chat 会记住它")
        result = self._request("/api/launcher/start", method="POST", token=token, body=draft)
        self.last_draft_parameters = _pick_parameters(params)
        return {"ok": True, "accepted": True,
                "message": _text(_nested(result, "message", "status"), 240) or "已请求 KVMem 启动"}

    @staticmethod
    def fill_missing_parameters(params: dict[str, Any], fallback: dict[str, Any] | None) -> None:
        cached = _pick_parameters(fallback)
        backend = str(params.get("Backend") or "").strip()
        cached_backend = cached.get("Backend", "")
        # 不把另一后端的模型/worker 填进当前控制台的新选择；未知归属也不猜。
        if backend and backend.casefold() != cached_backend.casefold():
            return
        if not backend and not cached_backend:
            return
        current_model = str(params.get("Model") or "").strip()
        if current_model and current_model != cached.get("Model"):
            return
        for key, value in cached.items():
            if params.get(key) is None or not str(params[key]).strip():
                params[key] = value

    def stop(self) -> dict[str, Any]:
        info = self.info()
        token = self._token(info)
        if not token:
            raise KVMemError("KVMem 控制台未提供临时控制令牌")
        result = self._request("/api/launcher/stop", method="POST", token=token, body={})
        return {"ok": True, "accepted": True, "message": _text(_nested(result, "message", "status"), 240) or "已请求 KVMem 停止"}

    def readiness(self, target_model: str) -> dict[str, Any]:
        status = self.status()
        if status.get("status") not in {"ready", "running"}:
            return status
        health_url = f"{self.inference_url.rsplit('/v1', 1)[0]}/health" if self.inference_url.endswith("/v1") else f"{self.inference_url}/health"
        models_url = f"{self.inference_url}/models"
        try:
            for url in (health_url, models_url):
                request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
                with net_io.open(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
                if url == models_url and target_model:
                    entries = payload.get("data", []) if isinstance(payload, dict) else []
                    ids = {str(item.get("id") or "") for item in entries if isinstance(item, dict)}
                    if ids and target_model not in ids:
                        return {**status, "status": "model_missing", "error": "KVMem 未提供当前配置的模型"}
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError):
            return {**status, "status": "failed", "error": "KVMem 推理服务未通过就绪检查"}
        return status
