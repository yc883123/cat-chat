# -*- coding: utf-8 -*-
"""冒烟：插话（interjection）队列 + 流式状态「重连中…」滞留修复。

两个计划一起验（2026-09-19）：
- `计划-复活插话功能.md`：运行中入队 → 编辑 / 引导 / 删除 / 冻结后取回；与选择面板互不锁死。
- `计划-流式状态误报重连中.md`：重连恢复后 `#runtimeStatus` 不得滞留「重连中…」，且不得
  越权清掉「正在思考 · 已等待 X 秒」（那是设计特性）。

编排（全自动，跑完还原现场）：
1. 起一个**真后端**隔离实例（复用 `verify/_serve_tmp.py`：独立 config / data_dir / 端口，
   `public_dir` 指向仓库 `public/`，所以改的 css/js 在浏览器里立刻可见）；
2. 往它的库里播种：一个带活动 Run 的会话 + 一个「终态带选择题 + 队列残留」的会话；
3. 跑 `verify/interjection_smoke.cjs`（Playwright + msedge）；
4. 收尾：kill server、删临时根目录。

为什么必须真后端：模板渲染 / 事件契约 / 队列面板都吃后端数据，静态打开 `public/index.html`
会让「配置驱动的 UI」整块消失而断言照样全绿（§九.92）。

用法：.venv\\Scripts\\python.exe verify\\interjection_smoke.py
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
sys.path.insert(0, str(ROOT))

PORT = int(os.environ.get("NAIBA_SMOKE_PORT", "8796"))
BASE = f"http://127.0.0.1:{PORT}"
TMP_ROOT = ROOT / "verify" / "interjection_smoke_tmp"
DATA_DIR = TMP_ROOT / "data"

ACTIVE_TITLE = "插话冒烟"
CHOICE_TITLE = "插话选择共存冒烟"
AGENT = {"id": "", "name": "Chat", "system_prompt": "", "skill_ids": []}


def wait_health(deadline: float = 90.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{BASE}/api/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - 启动期连接失败是预期分支
            time.sleep(0.5)
    return False


def seed() -> tuple[str, str, str]:
    """播种夹具，返回 (活动 Run 的会话 id, 活动 Run id, 选择题会话 id)。

    注意：必须在服务启动**之后**调用（见 main 的注释）——启动清理会把先播的活动 Run
    判成「服务重启，运行已中断」。
    """
    from naiba.storage.store import ChatStorage

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    storage = ChatStorage(DATA_DIR / "chat.db")

    # ① 有一个**活动 Run** 的会话：插话的入队/引导都要求 Run 活着（storage 侧硬校验）。
    #    真起一个 run 线程太重（要模型），这里只写库行——前端经 resumeRun 认领它，
    #    事件流由冒烟脚本在页面里用合成 SSE 提供（见 .cjs 的注入段）。
    active = storage.create_conversation(ACTIVE_TITLE)
    storage.add_message(active["id"], "user", f"{ACTIVE_TITLE}：先做第一版")
    storage.add_message(active["id"], "assistant", "第一版做完了，等你的新指令。")
    run = storage.create_run(active["id"], f"{ACTIVE_TITLE}：先做第一版", AGENT, {}, kind="chat")

    # ② 「终态带选择题 + 队列残留」的会话：验证插话面板与选择面板共存互不锁死。
    choice = storage.create_conversation(CHOICE_TITLE)
    storage.add_message(choice["id"], "user", f"{CHOICE_TITLE}：给我三套风格选一个")
    storage.add_message(
        choice["id"],
        "assistant",
        "三套风格都准备好了，选一套我就开工。",
        {"choice_groups": [{
            "prompt": "选一套风格",
            "choices": ["清新", "暗黑", "复古"],
            "mode": "single",
        }]},
    )
    # 一条**已冻结**的残留插话：run 结束后没被消费、也没被用户清掉的那种。
    storage.add_message(
        choice["id"],
        "user",
        "这条是上一轮冻结的残留插话",
        {
            "interjection": True,
            "run_id": "finished-run-fixture",
            "interjection_guided": False,
            "interjection_stopped": True,
        },
    )
    return str(active["id"]), str(run["id"]), str(choice["id"])


def main() -> int:
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)

    code = 1
    server: subprocess.Popen | None = None
    log = (ROOT / "verify" / "interjection_smoke_server.log").open("w", encoding="utf-8")
    try:
        env = dict(os.environ)
        env.update({
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "NAIBA_TMP_ROOT": str(TMP_ROOT),
            "NAIBA_TMP_PORT": str(PORT),
        })
        server = subprocess.Popen(  # noqa: S603 - 固定 argv
            [sys.executable, str(ROOT / "verify" / "_serve_tmp.py")],
            cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
        )
        if not wait_health():
            print(f"server 未就绪（日志见 {log.name}）")
            return 1
        # 必须在 server 起来**之后**播种：启动清理会把库里残留的活动 Run 判为
        # 「服务重启，运行已中断」，先播的话活动 Run 一开机就被改成终态，插话入队全 404。
        active_id, run_id, choice_id = seed()
        node_env = dict(os.environ)
        node_env.update({
            "NAIBA_SMOKE_BASE": BASE,
            "NAIBA_SMOKE_ACTIVE_ID": active_id,
            "NAIBA_SMOKE_RUN_ID": run_id,
            "NAIBA_SMOKE_CHOICE_ID": choice_id,
            "NAIBA_SMOKE_ACTIVE_TITLE": ACTIVE_TITLE,
            "NAIBA_SMOKE_CHOICE_TITLE": CHOICE_TITLE,
        })
        node = subprocess.run(  # noqa: S603 - 固定 argv
            ["node", str(ROOT / "verify" / "interjection_smoke.cjs")],
            cwd=str(ROOT), env=node_env, check=False,
        )
        code = node.returncode
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
        log.close()
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        print(f"已清理 {TMP_ROOT.name}；server 已退出"
              f"（exit={server.returncode if server else 'n/a'}）")
    return code


if __name__ == "__main__":
    sys.exit(main())
