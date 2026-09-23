# -*- coding: utf-8 -*-
"""工具集清单冒烟的自编排入口（随仓库发布的可复用资产，见 §九.133）。

为什么要自编排而不是直接对着在跑实例跑：这个冒烟会**写数据**（建一个工具集 + 一个 Agent，
用来模拟"已配好的 Agent"），所以必须落在隔离数据目录里。这里复用 `_serve_tmp.py`
（自带独立 config / data_dir / workspace / 端口），每轮从空目录起步。

隔离实例的 config 里 `mcp_servers` 为空 ⇒ 目录里没有 mcp__ 工具，脚本自己按这条假设挑工具。

用法：.venv\\Scripts\\python.exe verify\\tool_peek_smoke.py
     （失败时保留 verify/_tmp_tool_peek/ 供排查，成功则删掉；日志永远留在 verify/tool_peek_smoke_server.log）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("NAIBA_TMP_PORT", "8805"))
BASE = f"http://127.0.0.1:{PORT}"
ISOLATED_ROOT = ROOT / "verify" / "_tmp_tool_peek"


def wait_health(deadline: float = 90.0) -> bool:
    """等隔离实例起来：/api/health 就绪即可（脚本第一步就是打 /api/tool_catalog）。"""
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
    # 每轮从空目录起步：上一轮建的「探针工具集」/Agent 残留会让计数断言失真。
    if ISOLATED_ROOT.exists():
        shutil.rmtree(ISOLATED_ROOT, ignore_errors=True)

    env = dict(os.environ)
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "NAIBA_TMP_PORT": str(PORT),
        "NAIBA_TMP_ROOT": str(ISOLATED_ROOT),
    })
    log_path = ROOT / "verify" / "tool_peek_smoke_server.log"
    log = log_path.open("w", encoding="utf-8")
    server = subprocess.Popen(  # noqa: S603 - 固定 argv
        [sys.executable, str(ROOT / "verify" / "_serve_tmp.py")],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
    )
    code = 1
    try:
        if not wait_health():
            print(f"隔离实例未就绪（日志见 {log_path.name}）")
            return 1
        node_env = dict(os.environ)
        node_env["NODE_PATH"] = str(ROOT / "node_modules")
        node_env["NAIBA_TMP_BASE"] = BASE
        node = subprocess.run(  # noqa: S603 - 固定 argv
            ["node", str(ROOT / "verify" / "tool_peek_smoke.cjs")],
            cwd=str(ROOT), env=node_env, check=False,
        )
        code = node.returncode
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=10)
        log.close()
        if code == 0:
            shutil.rmtree(ISOLATED_ROOT, ignore_errors=True)
            print("已删除隔离数据目录（本轮全绿）")
        else:
            print(f"保留隔离数据目录供排查：{ISOLATED_ROOT}")
        print(f"隔离实例已退出（exit={server.returncode}；terminate 收尾的正常退出码，不是失败判据）")
    # 打印最后一行结论，方便在 CI / 手工一眼看清
    if code != 0:
        print("FAILED：详见上面的逐条 PASS/FAIL")
    return code


if __name__ == "__main__":
    sys.exit(main())
