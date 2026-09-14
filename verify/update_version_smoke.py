# -*- coding: utf-8 -*-
"""冒烟：软件更新「目标版本」下拉（源码 server，端口 8801）。

编排（全自动，跑完还原现场）：
1. config.json 的 data_dir 临时指向 verify/update_version_smoke_tmp_<pid>/，避开本机
   正在运行实例的实例锁（否则 server 会以 exit=2 拒绝启动）；带 pid 是为了不和残留
   server 抢同一把锁——抢锁失败时新 server 会退出，而健康检查打到残留进程上，
   等于用旧代码验证新改动（假绿）；
2. 起 8801 源码 server（版本数据全部由 .cjs 侧路由拦截注入，不动用户真实数据）；
3. 跑 update_version_smoke.cjs（起 Edge 无头）；
4. 收尾：kill 整棵 server 进程树、还原 config.json、删临时目录与日志。

用法：.venv\\Scripts\\python.exe verify\\update_version_smoke.py
若提示端口 8801 被占用（上次异常中断留下的 server），先清理再跑：
  Get-NetTCPConnection -LocalPort 8801 -State Listen | ForEach-Object { taskkill /PID $_.OwningProcess /T /F }
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# 仓库约定的输出编码：PowerShell 默认 GB2312，否则收尾信息等中文会显示为乱码。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
PORT = 8801
BASE = f"http://127.0.0.1:{PORT}"
DATA_DIR = ROOT / "verify" / f"update_version_smoke_tmp_{os.getpid()}"


def wait_health(server: subprocess.Popen, log_path: Path, deadline: float = 60.0) -> bool:
    """等 /api/health 就绪；同时盯着自己起的进程有没有提前退出。

    只看 HTTP 健康会漏掉致命场景：端口被别人（残留 server）占着时，本进程绑定失败
    立刻退出，而健康检查仍能拿到 200，于是整轮冒烟都在对着旧进程说话。
    """
    end = time.time() + deadline
    while time.time() < end:
        if server.poll() is not None:
            print(f"server 提前退出（exit={server.returncode}），日志尾部：")
            print(read_log_tail(log_path))
            return False
        try:
            with urllib.request.urlopen(f"{BASE}/api/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - 启动期连接失败是预期分支
            time.sleep(0.5)
    print(f"server 未就绪，日志尾部：\n{read_log_tail(log_path)}")
    return False


def read_log_tail(log_path: Path, lines: int = 15) -> str:
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(日志不可读)"
    return "\n".join(content[-lines:]) or "(日志为空)"


def stop_process_tree(server: subprocess.Popen) -> None:
    """终止 server 及其子进程。

    `Popen.terminate()` 只杀直接子进程：Windows 上 .venv 的 python.exe 会派生子进程
    承载真正的服务，于是「已退出」的假象过后，端口仍被孙进程占着，下一轮直接失败。
    """
    if server.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(  # noqa: S603,S607 - 固定 argv，taskkill 为系统内置
            ["taskkill", "/PID", str(server.pid), "/T", "/F"],
            capture_output=True, check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(server.pid), 15)
        except OSError:
            server.terminate()
    try:
        server.wait(timeout=15)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=10)


def port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", PORT)) == 0


def main() -> int:
    # 前置校验：端口被占用说明有残留 server，此时「健康检查通过」会是对着旧进程说话，
    # 等于用旧代码验证新改动——宁可明确失败，也不要静默假绿。
    if port_in_use():
        print(f"端口 {PORT} 已被占用，请先结束残留的 server 再跑（避免验证到旧代码）")
        return 1
    config_path = ROOT / "config.json"
    original_config = config_path.read_bytes()
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    config = json.loads(original_config.decode("utf-8"))
    config.update({"host": "127.0.0.1", "port": PORT, "data_dir": str(DATA_DIR)})
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    code = 1
    server: subprocess.Popen | None = None
    # 日志放系统临时目录：既避免往工作区里塞临时文件（本机 safe-delete 垫片还会在
    # 删除时插一脚），也保证失败时仍能回看 server 输出。
    log_path = Path(tempfile.gettempdir()) / f"naiba_update_version_smoke_{os.getpid()}.log"
    log = log_path.open("w", encoding="utf-8")
    try:
        env = dict(os.environ)
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        server = subprocess.Popen(  # noqa: S603 - 固定 argv
            [sys.executable, "server.py", "--host", "127.0.0.1", "--port", str(PORT)],
            cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
            # POSIX 上单独建进程组，收尾时能整组 kill（Windows 走 taskkill /T）。
            start_new_session=os.name != "nt",
        )
        if not wait_health(server, log_path):
            return 1
        node_env = dict(os.environ)
        node_env["NODE_PATH"] = str(Path.home() / "node_modules")
        node_env["NAIBA_SMOKE_BASE"] = BASE
        node = subprocess.run(  # noqa: S603 - 固定 argv
            ["node", str(ROOT / "verify" / "update_version_smoke.cjs")],
            cwd=str(ROOT), env=node_env, check=False,
        )
        code = node.returncode
    finally:
        if server is not None:
            stop_process_tree(server)
        log.close()
        # 清理一律尽力而为：本机 IDE 的 safe-delete 垫片会拦截 unlink/rmtree，
        # 清理失败不能污染冒烟结论（曾把 exit code 变成 1 并掩盖真实断言结果）。
        for cleanup in (
            lambda: config_path.write_bytes(original_config),
            lambda: log_path.unlink(missing_ok=True),
            lambda: shutil.rmtree(DATA_DIR, ignore_errors=True),
        ):
            try:
                cleanup()
            except OSError as error:
                print(f"清理警告（已忽略）：{error}")
        print(f"已还原 config.json 并清理 {DATA_DIR.name}"
              f"；server 已退出（exit={server.returncode if server else 'n/a'}）")
    return code


if __name__ == "__main__":
    sys.exit(main())
