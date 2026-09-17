# -*- coding: utf-8 -*-
"""Agent 说明弹层 + 工作区提示的回归验证（自编排：隔离源码实例 + Node 浏览器检查）。

为什么不是「静态 public/ 服务」：断言确实不需要后端，但**截图需要**。
静态服务下 `/api/starter-prompts` 404，开始页只剩 index.html 里写死的那两张 Skill 卡，
配置驱动的那几张（`BUILTIN_STARTER_PRESETS` → 6 张）全部消失——截图看起来像"卡片少了一大半"，
而断言照样全绿（2026-09-17 真的这么错过一次，见维护说明 §九.92）。
所以这里用 `verify/_serve_tmp.py`：真后端、真接口、独立 data_dir / config / 端口，
既不碰用户数据，页面又是真实形态；顺带把"开始页卡片数"也变成断言。

用法：.venv\\Scripts\\python.exe verify\\agent_help_popover.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("NAIBA_AGENT_HELP_PORT", "8801"))
BASE = f"http://127.0.0.1:{PORT}"
TMP_ROOT = ROOT / "verify" / "_tmp_agent_help"


def wait_until(predicate, timeout: float = 30.0, interval: float = 0.3) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def expected_starter_count() -> int:
    """内置预设张数：前端「开始页」应当渲染出的配置驱动卡片数。"""
    sys.path.insert(0, str(ROOT))
    from naiba.config import BUILTIN_STARTER_PRESETS

    return len(BUILTIN_STARTER_PRESETS)


def main() -> int:
    # 每轮从全新根开始：否则上一轮写进 config 的 `starter_presets_dismissed` 会让卡片数对不上。
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)

    log = (ROOT / "verify" / "_agent_help_server.log").open("w", encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "PYTHONUTF8": "1",
        "NAIBA_TMP_ROOT": str(TMP_ROOT),
        "NAIBA_TMP_PORT": str(PORT),
    })
    server = subprocess.Popen(  # noqa: S603 - 固定 argv
        [sys.executable, str(ROOT / "verify" / "_serve_tmp.py")],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        ready = wait_until(
            lambda: urllib.request.urlopen(f"{BASE}/api/health", timeout=2).status == 200
        )
        if not ready:
            print(f"隔离实例未就绪（日志见 {log.name}）")
            return 1

        starters = expected_starter_count()
        print(f"隔离实例就绪 {BASE}（data_dir={TMP_ROOT / 'data'}）")
        print(f"开始页卡片基线：配置驱动 {starters} 张 + index.html 写死 2 张 + 「自定义指令」1 张")

        smoke_env = dict(os.environ)
        smoke_env.update({
            "NAIBA_SMOKE_BASE": BASE,
            "NAIBA_EXPECT_STARTERS": str(starters),
            "NODE_PATH": str(ROOT / "node_modules"),
        })
        node = "node"  # 约定同 verify/tasks_panel_smoke.py：node 走 PATH
        try:
            proc = subprocess.run(  # noqa: S603 - 固定 argv
                [node, str(ROOT / "verify" / "agent_help_popover.cjs")],
                cwd=str(ROOT), env=smoke_env, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            print("找不到 node：请确认它在 PATH 里（本机 node 装在与 Python 无关的目录，需先 export）")
            return 1
        print(proc.stdout or "", end="")
        if proc.stderr.strip():
            print("--- node stderr ---")
            print(proc.stderr)
        return proc.returncode
    finally:
        server.terminate()
        try:
            server.wait(timeout=8)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
        # 见维护说明 §九.4：临时目录落在 verify/ 下，收尾交给 cleanup_verify.py 也行，
        # 但这轮实例只服务本脚本，留一个干净现场更好排查。
        shutil.rmtree(TMP_ROOT, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
