"""用正式 EXE 的内置页面运行发布门禁；配置与浏览器资料始终放在独立临时目录。

用法：项目解释器 verify/release_ui_smoke.py --exe dist/naiba-chat.exe
依赖：Node.js、锁定版本的 Playwright、环境指定的浏览器（默认 msedge）。
输出目录只含日志与截图，不能把运行目录的 config.json / chat.db 上传为 artifact。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPTS = ("browser_smoke.cjs", "chat_composer_layout_smoke.cjs")


class OwnedProcesses:
    """仅持有本轮启动的进程；Windows Job 关闭时同时回收它们留下的子进程。"""

    def __init__(self):
        self.processes: list[subprocess.Popen] = []
        self.job = None
        self.kernel = None
        if os.name != "nt":
            return
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("reads", "writes", "other", "read_bytes", "write_bytes", "other_bytes")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel = kernel
        self.job = kernel.CreateJobObjectW(None, None)
        if not self.job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel.CloseHandle(self.job)
            self.job = None
            raise error

    def start(self, command: Sequence[str], *, env: dict[str, str], cwd: Path, output):
        options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} \
            if os.name == "nt" else {"start_new_session": True}
        process = subprocess.Popen(list(command), env=env, cwd=cwd, stdout=output,
                                   stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **options)
        self.processes.append(process)
        if self.job and not self.kernel.AssignProcessToJobObject(self.job, int(process._handle)):
            # 不能建立独立进程树归属时直接判红，不能退化为按镜像名终止用户进程。
            error = ctypes.WinError(ctypes.get_last_error())
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            raise error
        return process

    def close(self):
        if self.job:
            job, self.job = self.job, None
            if not self.kernel.CloseHandle(job):
                raise ctypes.WinError(ctypes.get_last_error())
        elif os.name != "nt":
            for process in self.processes:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for process in self.processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait(timeout=5)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def isolated_environment(runtime: Path, artifacts: Path, port: int, browser: str) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("NAIBA_")}
    for name in ("localappdata", "roaming", "temp", "workspace", "data"):
        (runtime / name).mkdir()
    config = {
        "host": "127.0.0.1", "port": port, "access_token": "",
        "data_dir": str(runtime / "data"), "workspace_dir": str(runtime / "workspace"),
        "providers": [], "default_model_key": "", "mcp_servers": [],
        "onebot": {"enabled": False, "access_token": "", "allowed_user_ids": []},
    }
    (runtime / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    environment.update({
        "LOCALAPPDATA": str(runtime / "localappdata"), "APPDATA": str(runtime / "roaming"),
        "TEMP": str(runtime / "temp"), "TMP": str(runtime / "temp"),
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
        "NAIBA_TUT_ROOT": str(runtime), "NAIBA_TUT_PORT": str(port), "NAIBA_TUT_FIXTURE": "fresh",
        "NAIBA_SMOKE_BASE": f"http://127.0.0.1:{port}", "NAIBA_SMOKE_OUTPUT": str(artifacts),
        "NAIBA_SMOKE_BROWSER_CHANNEL": browser,
    })
    return environment


def wait_for_service(process, base: str, runtime: Path, timeout: float):
    deadline = time.monotonic() + timeout
    # 回环探针不经过用户或 CI 的 HTTP 代理，且不接受端口上的其他实例。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise RuntimeError(f"正式包服务在就绪前退出（exit {code}）")
        try:
            with opener.open(base + "/api/bootstrap", timeout=1) as response:
                payload = json.load(response)
            location = payload.get("data_location") or {}
            expected = (runtime / "data").resolve()
            actual = Path(str(location.get("data_dir") or "")).resolve()
            if location.get("is_frozen") is not True or actual != expected \
                    or Path(str(location.get("config_path") or "")).resolve() != runtime / "config.json":
                raise RuntimeError("HTTP 端口没有提供本轮隔离正式包实例，拒绝继续验收")
            settings = payload.get("settings") or {}
            if payload.get("model_profiles") or payload.get("providers") or payload.get("mcp_servers") \
                    or (settings.get("onebot") or {}).get("enabled"):
                raise RuntimeError("隔离实例包含模型、MCP 或已启用 QQ 渠道，拒绝继续验收")
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
            time.sleep(0.15)
    raise TimeoutError(f"等待正式包 HTTP 就绪超过 {timeout:g} 秒")


def run_gate(exe_command: Sequence[str], node_command: Sequence[str], *, artifacts: Path,
             repo: Path = ROOT, runtime_parent: Path | None = None, browser: str = "msedge",
             startup_timeout: float = 120, script_timeout: float = 180) -> int:
    artifacts = artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    runtime_parent = (runtime_parent or repo / "verify" / "_release_ui_runtime").resolve()
    runtime_parent.mkdir(parents=True, exist_ok=True)
    result = 1
    with (artifacts / "gate.log").open("w", encoding="utf-8") as report:
        def log(message):
            print(message, flush=True)
            print(message, file=report, flush=True)

        try:
            with tempfile.TemporaryDirectory(prefix="run-", dir=runtime_parent) as folder:
                runtime = Path(folder).resolve()
                runtime.relative_to(runtime_parent)  # 清理对象只能是本轮创建的子目录。
                port = free_port()
                env = isolated_environment(runtime, artifacts, port, browser)
                owned = OwnedProcesses()
                try:
                    with (artifacts / "server.log").open("w", encoding="utf-8") as server_log:
                        command = [*exe_command, "--run-skill-script", str(repo / "verify" / "_tutorial_serve.py"), "--no-seed"]
                        server = owned.start(command, env=env, cwd=runtime, output=server_log)
                        log("正在启动隔离正式包服务")
                        wait_for_service(server, env["NAIBA_SMOKE_BASE"], runtime, startup_timeout)
                        log("正式包 HTTP 就绪，配置与数据隔离检查通过")
                        for name in SMOKE_SCRIPTS:
                            if server.poll() is not None:
                                raise RuntimeError("正式包服务在浏览器检查期间退出")
                            with (artifacts / f"{Path(name).stem}.log").open("w", encoding="utf-8") as output:
                                process = owned.start([*node_command, str(repo / "verify" / name)],
                                                      env=env, cwd=repo, output=output)
                                code = process.wait(timeout=script_timeout)
                            if code:
                                raise RuntimeError(f"{name} 未通过（exit {code}），禁止发布")
                            log(f"通过 {name}")
                        if server.poll() is not None:
                            raise RuntimeError("正式包服务在浏览器检查结束前退出")
                        result = 0
                finally:
                    owned.close()
                    log("本轮服务、Node 和浏览器进程树已回收")
        except Exception as error:  # noqa: BLE001 - 命令行门禁统一非零，详细资料保留在专用输出目录
            result = 1
            log(f"正式包 UI 门禁失败：{error}")
        if result == 0:
            log("正式包真实 UI 门禁通过")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True, help="待验收的正式打包 EXE")
    parser.add_argument("--node", default="node", help="Node.js 可执行文件名或路径")
    parser.add_argument("--artifacts", type=Path, default=ROOT / "verify" / "release-ui-artifacts")
    parser.add_argument("--browser-channel", default=os.environ.get("NAIBA_SMOKE_BROWSER_CHANNEL", "msedge"))
    parser.add_argument("--startup-timeout", type=float, default=120)
    parser.add_argument("--script-timeout", type=float, default=180)
    args = parser.parse_args(argv)
    if not args.exe.is_file():
        parser.error("--exe 必须指向已有正式包文件")
    node = shutil.which(args.node)
    if not node:
        parser.error("找不到 Node.js 可执行文件")
    if args.startup_timeout <= 0 or args.script_timeout <= 0:
        parser.error("超时必须大于零")
    return run_gate([str(args.exe.resolve())], [node], artifacts=args.artifacts,
                    browser=args.browser_channel, startup_timeout=args.startup_timeout,
                    script_timeout=args.script_timeout)


if __name__ == "__main__":
    raise SystemExit(main())
