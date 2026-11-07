"""安全的 KVMem 本机控制台适配层。

KVMem 的 worker 生命周期只能由 KVMem controller 管理。Cat Chat 只调用
controller API，不扫描端口、不猜 PID、不终止外部进程。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from naiba import net as net_io

DEFAULT_MANAGER_URL = "http://127.0.0.1:18199"
DEFAULT_INFERENCE_URL = "http://127.0.0.1:18200/v1"
REQUEST_TIMEOUT = 3.0


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


class KVMemClient:
    def __init__(self, manager_url: str, inference_url: str, *, timeout: float = REQUEST_TIMEOUT):
        self.manager_url = validate_manager_url(manager_url)
        self.inference_url = str(inference_url or DEFAULT_INFERENCE_URL).strip().rstrip("/")
        parsed = urllib.parse.urlsplit(self.inference_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("KVMem 推理地址必须是 http/https URL")
        self.timeout = max(0.5, min(float(timeout), 10.0))

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

    def start(self) -> dict[str, Any]:
        info = self.info()
        token = self._token(info)
        if not token:
            raise KVMemError("KVMem 控制台未提供临时控制令牌")
        # 控制台要求完整 {'schema_version': 1, 'parameters': {...}} 结构，原样回传它自己的 draft。
        draft = info.get("draft") if isinstance(info.get("draft"), dict) else None
        params = draft.get("parameters") if draft else None
        if not isinstance(params, dict):
            raise KVMemError("KVMem 控制台没有可用的启动配置")
        if not str(params.get("Model") or "").strip():
            raise KVMemError("KVMem 尚未选择模型，请先在控制台选择模型后再启动")
        result = self._request("/api/launcher/start", method="POST", token=token, body=draft)
        return {"ok": True, "accepted": True,
                "message": _text(_nested(result, "message", "status"), 240) or "已请求 KVMem 启动"}

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
