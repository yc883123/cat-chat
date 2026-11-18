"""安全的 KVMem 本机控制台适配层。

KVMem 的 worker 生命周期只能由 KVMem controller 管理。Cat Chat 只调用
controller API 与后台拉起 controller 本身，不扫描端口、不猜 PID、不终止
外部进程。

协议事实（与 0.19.1 安装包 scripts/local-console.py 对齐）：

- ``GET /api/launcher/info`` 返回 ``token``、``console_protocol``、
  ``console_root``、``backend_root``、``backends``、``draft`` 和内嵌的
  ``status``（``state``/``active_config``/``api_key_configured`` 等）。
- 运行配置在 ``status.active_config``（``{schema_version, parameters}``），
  ``ApiKey`` 已被控制程序脱敏；停止后清空。
- ``POST /api/launcher/start`` 接受完整的 ``{schema_version, parameters}``，
  缺省项由控制程序按本版本默认值合成；模型检查、端口占用会以 400 返回。
- ``POST /api/launcher/stop`` 同步等待模型进程退出（至多 10 秒）后返回。
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from naiba import net as net_io

DEFAULT_MANAGER_URL = "http://127.0.0.1:18199"
DEFAULT_INFERENCE_URL = "http://127.0.0.1:18200/v1"
REQUEST_TIMEOUT = 3.0
# 启动请求含模型检查，可能明显超过普通请求；停止请求内含至多 10 秒的进程等待。
START_TIMEOUT = 20.0
STOP_TIMEOUT = 15.0
LAUNCH_WAIT_SECONDS = 12.0
LAUNCH_POLL_SECONDS = 0.35
FALLBACK_PARAMETER_KEYS = ("Backend", "Model", "Worker", "Gpu")
LAST_START_FILENAME = "last-start.json"
CONSOLE_PYTHON = Path("runtime/python/python.exe")
CONSOLE_SCRIPT = Path("scripts/local-console.py")
CONSOLE_MANIFEST = Path("BUILD-INFO.json")
# 状态轮询和启动请求来自不同 HTTP 线程；原子替换不能代替读改写锁。
_LAST_START_LOCK = threading.RLock()


class KVMemError(RuntimeError):
    """用户可见的、已脱敏的 KVMem 错误。"""


class KVMemUnreachable(KVMemError):
    """控制地址明确不可达（连接拒绝/超时），可以尝试后台拉起。"""


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
    """只缓存公开启动字段（旧格式兼容）；完整读改写串行，写失败不影响启动。"""
    picked = _pick_parameters(parameters)
    if not manager_url or not picked.get("Model"):
        return
    target = Path(path)
    with _LAST_START_LOCK:
        try:
            data = _read_last_start_raw(target)
            if data.get(str(manager_url)) == picked:
                return
            data[str(manager_url)] = picked
            _write_last_start_raw(target, data)
        except OSError:
            pass


def last_run_key(provider_id: str, manager_url: str) -> str:
    """完整运行配置按供应商和控制地址双键隔离，避免共用地址或不同后端串用。"""
    return f"{str(provider_id or '').strip()}|{validate_manager_url(manager_url)}"


def _read_last_start_raw(target: Path) -> dict[str, Any]:
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_last_start_raw(target: Path, data: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix="last-start-", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            json.dump(data, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def _clean_launch_parameters(parameters: Any) -> dict[str, Any] | None:
    """可落盘的完整参数：结构合法、Model 非空、不含 ApiKey。"""
    if not isinstance(parameters, dict):
        return None
    clean = {key: value for key, value in parameters.items() if key != "ApiKey"}
    if not str(clean.get("Model") or "").strip():
        return None
    return clean


def read_last_run(path: Any, provider_id: str, manager_url: str) -> dict[str, Any] | None:
    """读取自动记录的最近运行配置。

    返回 ``{"config": {...}, "source": "saved"|"legacy"}``；没有完整记录时回退
    旧四项缓存（``legacy``，缺少的历史完整参数由调用方如实处理，不假称恢复）。
    """
    with _LAST_START_LOCK:
        data = _read_last_start_raw(Path(path))
    entry = data.get(last_run_key(provider_id, manager_url))
    if isinstance(entry, dict) and entry.get("schema_version") == 1:
        clean = _clean_launch_parameters(entry.get("parameters"))
        if clean is not None:
            return {"config": {"schema_version": 1, "parameters": clean}, "source": "saved"}
    legacy = _pick_parameters(data.get(str(manager_url)))
    if legacy.get("Model"):
        return {"config": {"schema_version": 1, "parameters": dict(legacy)}, "source": "legacy"}
    return None


def write_last_run(path: Any, provider_id: str, manager_url: str, parameters: Any) -> bool:
    """完整参数落盘（不含 ApiKey）；写失败返回 False，由调用方明确提示。"""
    clean = _clean_launch_parameters(parameters)
    if not clean or not manager_url:
        return False
    target = Path(path)
    with _LAST_START_LOCK:
        try:
            data = _read_last_start_raw(target)
            data[last_run_key(provider_id, manager_url)] = {"schema_version": 1, "parameters": clean}
            _write_last_start_raw(target, data)
            return True
        except OSError:
            return False


class ConsoleLauncher:
    """后台拉起 KVMem 控制程序。

    只记录本程序启动的进程句柄；不扫描 PID、不终止外部进程。被拉起的控制
    程序在 Cat Chat 退出后继续存在（它不占显存，模型启停仍走控制程序 API）。
    """

    def __init__(self) -> None:
        self._handles: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def owned_running(self, manager_url: str) -> bool:
        with self._lock:
            handle = self._handles.get(str(manager_url))
            process = handle.get("process") if isinstance(handle, dict) else None
            return bool(process is not None and process.poll() is None)

    def _validate_install(self, install_dir: str) -> tuple[Path, Path, Path, str]:
        install = Path(install_dir).expanduser()
        if not install.is_dir():
            raise KVMemError(f"KVMem 安装目录不存在：{_text(install, 200)}")
        python_exe = install / CONSOLE_PYTHON
        script = install / CONSOLE_SCRIPT
        manifest = install / CONSOLE_MANIFEST
        if not python_exe.is_file():
            raise KVMemError("安装目录缺少自带 Python（runtime/python/python.exe），请确认填写的是 KVMem 解压根目录")
        if not script.is_file():
            raise KVMemError("安装目录缺少控制程序（scripts/local-console.py），请确认填写的是 KVMem 解压根目录")
        try:
            manifest_data = json.loads(manifest.read_text(encoding="utf-8-sig"))
            version = _text(_nested(manifest_data, "version"), 40)
        except (OSError, ValueError):
            raise KVMemError("安装目录缺少安装元数据（BUILD-INFO.json），请确认填写的是 KVMem 解压根目录") from None
        return install, python_exe, script, version

    def launch(self, install_dir: str, manager_url: str, *, state_dir: Path, log_path: Path) -> subprocess.Popen:
        """以参数数组拉起控制程序；不打开浏览器、不做交互端口选择。"""
        install, python_exe, script, version = self._validate_install(install_dir)
        validate_manager_url(manager_url)
        port = _port_from_url(manager_url) or 18199
        state_dir = Path(state_dir)
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)
        argv = [
            str(python_exe), "-X", "utf8", "-I", str(script),
            "--host", "127.0.0.1", "--port", str(port),
            "--backend-root", str(install),
            "--state-dir", str(state_dir),
            "--prefer-bundled-worker", "--language", "zh-CN",
        ]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        with self._lock:
            try:
                log = log_path.open("ab")
            except OSError as exc:
                raise KVMemError(f"无法写控制程序日志（{log_path.parent}）：{exc}") from None
            try:
                process = subprocess.Popen(  # noqa: S603 - 固定安装包入口，参数数组不经过 shell
                    argv, cwd=str(install), stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT,
                    creationflags=flags, close_fds=True,
                )
            except OSError as exc:
                log.close()
                raise KVMemError(f"无法启动 KVMem 控制程序：{exc}") from None
            log.close()  # 子进程已在 spawn 时继承句柄，父进程这份立即释放。
            self._handles[validate_manager_url(manager_url)] = {
                "process": process, "log_path": log_path, "install": str(install),
                "version": version, "port": port,
            }
        return process

    def handle(self, manager_url: str) -> dict[str, Any] | None:
        with self._lock:
            handle = self._handles.get(validate_manager_url(manager_url))
            return dict(handle) if handle else None


def wait_for_console(client: "KVMemClient", process: subprocess.Popen | None = None,
                     *, timeout: float = LAUNCH_WAIT_SECONDS,
                     log_path: Path | None = None) -> dict[str, Any]:
    """有界等待控制程序可访问；启动退出与超时分别报错。"""
    deadline = time.monotonic() + max(1.0, float(timeout))
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            hint = f"日志：{log_path}" if log_path else ""
            raise KVMemError(f"KVMem 控制程序启动后立即退出（退出码 {process.returncode}）。{hint}".strip())
        probe = client.probe()
        if probe.get("kind") == "ok":
            return probe["info"]
        if probe.get("kind") == "foreign":
            raise KVMemError(_text(probe.get("error") or "该端口被其他程序占用", 300))
        time.sleep(LAUNCH_POLL_SECONDS)
    raise KVMemError("KVMem 控制程序启动超时：进程未在预期时间内提供服务，可在设置里检查安装目录后重试")


class KVMemClient:
    def __init__(self, manager_url: str, inference_url: str, *, timeout: float = REQUEST_TIMEOUT):
        self.manager_url = validate_manager_url(manager_url)
        self.inference_url = str(inference_url or DEFAULT_INFERENCE_URL).strip().rstrip("/")
        parsed = urllib.parse.urlsplit(self.inference_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("KVMem 推理地址必须是 http/https URL")
        self.timeout = max(0.5, min(float(timeout), 10.0))
        self.last_active_parameters: dict[str, Any] = {}
        self.console_info: dict[str, Any] = {}

    def _request(self, path: str, *, method: str = "GET", token: str = "", timeout: float | None = None,
                 body: dict[str, Any] | None = None) -> dict[str, Any]:
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
            with net_io.open(request, timeout=self.timeout if timeout is None else max(0.5, min(timeout, 120.0))) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read(4096).decode("utf-8", errors="replace"))
            except (ValueError, OSError):
                detail = {}
            message = str(detail.get("error") or "").strip() if isinstance(detail, dict) else ""
            raise KVMemError(_text(message, 400) or "KVMem 控制台返回了错误") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise KVMemUnreachable("KVMem 控制台不可达") from None
        except ValueError:
            raise KVMemError("KVMem 控制台返回了无效响应") from None
        if not isinstance(payload, dict):
            raise KVMemError("KVMem 控制台响应格式不正确")
        return payload

    def probe(self) -> dict[str, Any]:
        """区分「没有服务」「不是兼容控制程序」「可用控制程序」；不抛异常。"""
        try:
            info = self._request("/api/launcher/info")
        except KVMemUnreachable:
            return {"kind": "unreachable", "info": None, "error": "KVMem 控制程序未运行"}
        except KVMemError as exc:
            return {"kind": "foreign", "info": None, "error": _text(str(exc), 200)}
        if not self._protocol_ok(info):
            return {"kind": "foreign", "info": None,
                    "error": "该端口上不是兼容版本的 KVMem 控制程序，不能接管或停止它"}
        return {"kind": "ok", "info": info, "error": ""}

    @staticmethod
    def _token(info: dict[str, Any]) -> str:
        candidate = _nested(info, "token")
        if not candidate and isinstance(info.get("auth"), dict):
            candidate = _nested(info["auth"], "token")
        return str(candidate or "").strip()

    @staticmethod
    def _protocol_ok(info: dict[str, Any]) -> bool:
        # 0.19.x 安装包总是携带 console_protocol；缺失或非 1 都视为不兼容。
        protocol = _nested(info, "console_protocol")
        if protocol != 1:
            return False
        if not str(_nested(info, "backend_root") or "").strip():
            return False
        backends = info.get("backends")
        return isinstance(backends, list) and bool(backends)

    def info(self) -> dict[str, Any]:
        info = self._request("/api/launcher/info")
        if not self._protocol_ok(info):
            raise KVMemError("KVMem 控制程序协议版本不兼容")
        self.console_info = info
        return info

    def status(self) -> dict[str, Any]:
        return self._status_payload(self.info())

    def _active_entry(self, info: dict[str, Any]) -> dict[str, Any]:
        entry = info.get("status") if isinstance(info.get("status"), dict) else {}
        active = entry.get("active_config") if isinstance(entry.get("active_config"), dict) else None
        return active or {}

    def _status_payload(self, info: dict[str, Any]) -> dict[str, Any]:
        entry = info.get("status") if isinstance(info.get("status"), dict) else {}
        state = str(_nested(entry, "state", "status", "phase") or "unknown").strip().lower()
        mapping = {"idle": "stopped", "stopped": "stopped", "down": "stopped",
                   "starting": "starting", "loading": "starting", "ready": "ready",
                   "running": "running", "healthy": "ready", "failed": "failed", "error": "failed",
                   "exited": "stopped"}
        active_params = self._active_entry(info).get("parameters")
        active_params = active_params if isinstance(active_params, dict) else {}
        draft = info.get("draft") if isinstance(info.get("draft"), dict) else {}
        draft_params = draft.get("parameters") if isinstance(draft.get("parameters"), dict) else {}
        backend = str(active_params.get("Backend") or draft_params.get("Backend") or "").strip()
        model = str(active_params.get("Model") or draft_params.get("Model") or "").strip()
        actual_port = active_params.get("Port") or draft_params.get("Port") or _nested(entry, "port", "inference_port")
        if not actual_port:
            actual_port = _port_from_url(self.inference_url)
        self.last_active_parameters = dict(active_params) if active_params else {}
        payload = {
            "ok": True,
            # exited = 模型进程自行退出（崩溃），与用户主动停止后的 stopped 不同。
            "status": mapping.get(state, state if state in {"unknown", "starting", "ready", "running", "failed", "stopped"} else "unknown"),
            "raw_status": state,
            "backend": _text(backend, 100),
            "model": _text(model, 300),
            "actual_port": int(actual_port) if str(actual_port).isdigit() else None,
            "inference_url": self.inference_url,
            "manager_url": self.manager_url,
            "api_key_configured": bool(_nested(entry, "api_key_configured")),
            "pid": _nested(entry, "pid"),
        }
        exit_code = _nested(entry, "exit_code")
        if payload["status"] == "stopped" and state == "exited":
            payload["status"] = "failed"
        if payload["status"] == "failed" and exit_code is not None:
            payload["error"] = f"KVMem 托管模型进程已退出（退出码 {exit_code}）"
        expected_port = _port_from_url(self.inference_url)
        if payload["actual_port"] and expected_port and payload["actual_port"] != expected_port:
            payload["status"] = "port_mismatch"
            payload["error"] = "KVMem 实际推理端口与 Cat Chat 配置不一致"
        return payload

    def start(self, config: dict[str, Any]) -> dict[str, Any]:
        """提交完整启动配置；模型仍由 KVMem 控制程序托管。"""
        if not isinstance(config, dict) or config.get("schema_version") != 1 or not isinstance(config.get("parameters"), dict):
            raise KVMemError("启动配置结构不正确，请在 Cat Chat 设置中重新保存 KVMem 配置")
        info = self.info()
        token = self._token(info)
        if not token:
            raise KVMemError("KVMem 控制程序未提供临时控制令牌")
        result = self._request("/api/launcher/start", method="POST", token=token,
                               timeout=START_TIMEOUT, body=config)
        # start 返回的就是控制程序当前 status（state 在顶层）。
        state = str(_nested(result, "state") or "starting").strip().lower()
        return {"ok": True, "accepted": True, "state": state,
                "message": _text(_nested(result, "message", "status"), 240) or "已请求 KVMem 启动"}

    def stop(self) -> dict[str, Any]:
        info = self.info()
        token = self._token(info)
        if not token:
            raise KVMemError("KVMem 控制程序未提供临时控制令牌")
        result = self._request("/api/launcher/stop", method="POST", token=token,
                               timeout=STOP_TIMEOUT, body={})
        state = str(_nested(result, "state") or "").strip().lower() if isinstance(result, dict) else ""
        return {"ok": True, "accepted": True, "state": state, "raw": result,
                "message": _text(_nested(result, "message", "status"), 240) or "已请求 KVMem 停止"}

    def capture(self) -> dict[str, Any]:
        """读取当前运行配置或后台草稿；不启动模型。GPU 清单一并带出供设置选择。"""
        info = self.info()
        entry = info.get("status") if isinstance(info.get("status"), dict) else {}
        active = entry.get("active_config") if isinstance(entry.get("active_config"), dict) else None
        active_params = active.get("parameters") if isinstance(active, dict) else None
        gpus = info.get("gpus") if isinstance(info.get("gpus"), list) else []
        gpus = [{"uuid": str(item.get("uuid") or ""), "name": str(item.get("name") or ""),
                 "memory_mib": str(item.get("memory_mib") or "")}
                for item in gpus if isinstance(item, dict) and str(item.get("uuid") or "").strip()]
        if isinstance(active_params, dict) and str(active_params.get("Model") or "").strip():
            return {"source": "active", "parameters": dict(active_params), "gpus": gpus}
        draft = info.get("draft") if isinstance(info.get("draft"), dict) else {}
        draft_params = draft.get("parameters") if isinstance(draft.get("parameters"), dict) else {}
        if isinstance(draft_params, dict) and draft_params:
            return {"source": "draft", "parameters": dict(draft_params), "gpus": gpus}
        raise KVMemError("KVMem 控制程序没有可读取的运行配置或默认参数")

    def readiness(self, target_model: str, *, auth_token: str = "") -> dict[str, Any]:
        status = self.status()
        if status.get("status") not in {"ready", "running"}:
            return status
        health_url = f"{self.inference_url.rsplit('/v1', 1)[0]}/health" if self.inference_url.endswith("/v1") else f"{self.inference_url}/health"
        models_url = f"{self.inference_url}/models"
        headers = {"Accept": "application/json"}
        # 模型带 ApiKey 时推理口会拒绝匿名请求；用供应商已有鉴权探测，避免误判失败。
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        try:
            for url in (health_url, models_url):
                request = urllib.request.Request(url, headers=headers, method="GET")
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
