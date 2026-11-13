# -*- coding: utf-8 -*-
"""供应商预设 + 首启引导向导的真后端浏览器冒烟（自编排入口）。

为什么要真后端：预设网格、引导条、向导的「可用供应商判据」全靠 `/api/provider-presets`
与 bootstrap 里的 model_profiles 驱动；只开静态 `public/` 会让整块配置驱动的 UI 整体消失，
而断言照样能全绿（§九.92 的坑）。

隔离方式：起 `verify/_serve_tmp.py`（独立 config/data_dir/端口 + 读仓库 public/），
把仓库 `config.json` 完全放在一边——本脚本不碰开发配置与开发库。
播种的 `probe-*` 两张卡都没有 api_key ⇒ 正好是「一个可用供应商都没有」的首启态。

用法：
    .venv\\Scripts\\python.exe verify\\provider_presets_smoke.py
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
with socket.socket() as port_probe:
    port_probe.bind(("127.0.0.1", 0))
    PORT = int(os.environ.get("NAIBA_PRESET_SMOKE_PORT") or port_probe.getsockname()[1])
BASE = f"http://127.0.0.1:{PORT}"


def wait_health(deadline: float = 60.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{BASE}/api/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - 启动期连接失败是预期分支
            time.sleep(0.5)
    return False


def main() -> int:
    tmp_root = Path(tempfile.mkdtemp(prefix="_tmp_preset_smoke_", dir=ROOT / "verify"))
    env = dict(os.environ)
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "NAIBA_TMP_PORT": str(PORT),
        "NAIBA_TMP_ROOT": str(tmp_root),
    })
    log_path = tmp_root / "server.log"
    server: subprocess.Popen | None = None
    code = 1
    with log_path.open("w", encoding="utf-8") as log:
        try:
            server = subprocess.Popen(  # noqa: S603 - 固定 argv
                [sys.executable, str(ROOT / "verify" / "_serve_tmp.py")],
                cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
            )
            if not wait_health():
                print(f"隔离实例未就绪（日志见 {log_path.name}）")
                return 1
            node_env = dict(os.environ)
            node_env.update({
                "NAIBA_SMOKE_BASE": BASE,
                "NAIBA_SMOKE_SHOTS": str(tmp_root),
                "NODE_PATH": os.pathsep.join(filter(None, [
                    str(ROOT / "node_modules"),
                    str(Path.home() / "node_modules"),
                    os.environ.get("NODE_PATH", ""),
                ])),
            })
            node = subprocess.run(  # noqa: S603 - 固定 argv
                ["node", str(ROOT / "verify" / "provider_presets_smoke.cjs")],
                cwd=str(ROOT), env=node_env, check=False,
            )
            code = node.returncode
        finally:
            if server is not None:
                server.terminate()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=10)
            print(f"隔离实例已退出（exit={server.returncode if server else 'n/a'}）；日志、截图和隔离数据保留在 {tmp_root}")
    return code


if __name__ == "__main__":
    sys.exit(main())
