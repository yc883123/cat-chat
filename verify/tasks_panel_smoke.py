# -*- coding: utf-8 -*-
"""冒烟编排：任务面板（只列后台作业）的真机验证。

8765 上常驻的通常是**打包版 exe**（读内置资源），拿它验仓库 `public/` 等于验空气。
本脚本自编排：全新隔离根（独立 config / data_dir / 端口）→ 直连库播种「回答记录 + 后台作业」
混排数据 → 起源码实例 → 跑 `verify/tasks_panel_smoke.cjs`（Playwright + 系统 Edge）→ 收尾。

覆盖：
- 接口契约：`/api/tasks?jobs_only=1` 只回后台作业；不带该参数时行为与从前一致（向后兼容）；
- Job 取消：`POST /api/jobs/{id}/cancel` 落 `stopping` + `cancel_requested`；
- 前端：面板只列作业、按回答分组、组标题为「类型 + 时间」、状态含「停止中」、
  停止按钮只对活动作业出现、零 pageerror / 零 console.error、窄屏不横向滚动。

前置：仓库根 `node_modules/` 里有 playwright（`npm install --no-save playwright`），
浏览器用系统 Edge（`channel: 'msedge'`），无需 `npx playwright install`。

用法：`.venv\\Scripts\\python.exe verify\\tasks_panel_smoke.py`
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PORT = int(os.environ.get("NAIBA_SMOKE_PORT", "8811"))
BASE = f"http://127.0.0.1:{PORT}"
TITLE = "任务面板冒烟"
USER_MESSAGE = "这句话不该出现在任务面板里"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return bool(ok)


def _api(path: str, payload: dict | None = None, timeout: float = 20.0) -> dict:
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = "POST"
    request = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_until(predicate, timeout: float = 60.0, interval: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 - 起服务期间连不上是预期分支
            pass
        time.sleep(interval)
    try:
        return bool(predicate())
    except Exception:  # noqa: BLE001
        return False


def isolated_paths(root: Path):
    from naiba.paths import PathContext

    paths = PathContext.local(root, root / "config.json")
    paths.public_dir = ROOT / "public"
    return paths


def insert_task(db_path: Path, task_id: str, conversation_id: str, kind: str, status: str,
                parent: str = "", message: str = "", error: str = "",
                current_step: str = "", attempt: int = 0) -> None:
    """直接落库造数据：面板渲染只需要 background_tasks 行，不必真跑一遍 Job。"""
    now = int(time.time() * 1000)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT OR REPLACE INTO background_tasks("
            "id, conversation_id, kind, status, message, error, current_step, attempt, "
            "created_at, updated_at, parent_job_id, owner_session_id, detail, checkpoint, result"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', '{}')",
            (task_id, conversation_id, kind, status, message, error, current_step, attempt,
             now, now, parent, conversation_id),
        )
        db.commit()


def seed(root: Path) -> str:
    from naiba.storage.store import ChatStorage

    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "chat.db"
    storage = ChatStorage(db_path)
    conversation = storage.create_conversation(TITLE)
    conversation_id = str(conversation["id"])

    # 先只播终态数据：启动清理会把**活动状态**（queued/running/waiting/stopping/
    # cancelling）一律标成 interrupted，活动行必须在服务起来之后再插（见 seed_active）。
    insert_task(db_path, "smoke-reply-1", conversation_id, "chat", "completed", message=USER_MESSAGE)
    insert_task(db_path, "smoke-reply-2", conversation_id, "chat", "completed", message=USER_MESSAGE)
    insert_task(db_path, "smoke-job-failed", conversation_id, "comfyui", "failed",
                parent="smoke-reply-2", message="ComfyUI 批量生成", error="第 1 段提交失败")
    return conversation_id


def seed_active(root: Path, conversation_id: str) -> None:
    """服务起来之后再插活动作业：否则会被启动清理改写成 interrupted。"""
    db_path = root / "data" / "chat.db"
    insert_task(db_path, "smoke-job-running", conversation_id, "comfyui", "running",
                parent="smoke-reply-1", message="ComfyUI 批量生成",
                current_step="已提交 2/3", attempt=3)
    insert_task(db_path, "smoke-job-stopping", conversation_id, "comfyui", "stopping",
                parent="smoke-reply-1", message="ComfyUI 批量生成")


def main() -> int:
    # 每轮用全新隔离根：避免上一次的 config/db 影响断言（也避开 rmtree 被重定向到回收站的问题）
    root = ROOT / "verify" / f"tasks_panel_tmp_{int(time.time())}"
    root.mkdir(parents=True, exist_ok=True)

    server: subprocess.Popen | None = None
    log = (ROOT / "verify" / "tasks_panel_smoke_server.log").open("w", encoding="utf-8")
    try:
        conversation_id = seed(root)
        env = dict(os.environ)
        env.update({
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "NAIBA_TMP_ROOT": str(root),
            "NAIBA_TMP_PORT": str(PORT),
        })
        server = subprocess.Popen(  # noqa: S603 - 固定 argv
            [sys.executable, str(ROOT / "verify" / "_serve_tmp.py")],
            cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if not wait_until(lambda: urllib.request.urlopen(f"{BASE}/api/health", timeout=2).status == 200):
            print(f"server 未就绪（日志见 {log.name}）")
            return 1

        print("① 接口契约：jobs_only 只回后台作业 / 不带参数时向后兼容")
        job_ids = [str(item.get("id") or "") for item in (_api("/api/tasks?jobs_only=1").get("tasks") or [])]
        check("jobs_only=1 不回回答记录", job_ids and all(i.startswith("smoke-job-") for i in job_ids), f"{job_ids}")
        every = [str(item.get("id") or "") for item in (_api("/api/tasks").get("tasks") or [])]
        check("不带参数时回答记录照旧可见（向后兼容）",
              "smoke-reply-1" in every and "smoke-reply-2" in every, f"{every}")

        print("② 服务已就绪后再播活动作业（避开启动清理）")
        seed_active(root, conversation_id)
        active_ids = [str(item.get("id") or "") for item in (_api("/api/tasks?jobs_only=1").get("tasks") or [])]
        check("三条作业齐备且未被启动清理改写",
              set(active_ids) == {"smoke-job-running", "smoke-job-stopping", "smoke-job-failed"},
              f"{active_ids}")

        print("③ 前端（Playwright + 系统 Edge）")
        smoke_env = dict(os.environ)
        smoke_env.update({
            "NAIBA_SMOKE_BASE": BASE,
            "NODE_PATH": str(ROOT / "node_modules"),
        })
        completed = subprocess.run(  # noqa: S603 - 固定 argv
            ["node", str(ROOT / "verify" / "tasks_panel_smoke.cjs")],
            cwd=str(ROOT), env=smoke_env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        print(completed.stdout or "")
        if completed.stderr:
            print(completed.stderr)
        check("浏览器冒烟全部通过", completed.returncode == 0, f"exit={completed.returncode}")

        print("④ Job 取消：必须落 stopping + cancel_requested")
        _api("/api/jobs/smoke-job-running/cancel", {"conversation_id": conversation_id})
        row = next((item for item in _api("/api/tasks?jobs_only=1").get("tasks") or []
                    if str(item.get("id")) == "smoke-job-running"), {})
        check("状态转为 stopping", row.get("status") == "stopping", f"status={row.get('status')}")
        check("cancel_requested 已持久化（worker 的状态更新覆盖不了它）",
              bool(row.get("cancel_requested")), f"cancel_requested={row.get('cancel_requested')}")

        failed = [name for name, ok, _ in RESULTS if not ok]
        print(f"\n结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过"
              + (f"；失败：{failed}" if failed else ""))
        return 1 if failed else 0
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
        log.close()
        shutil.rmtree(root, ignore_errors=True)
        print(f"隔离目录 {root} 已清理（开发 config.json 未修改）")


if __name__ == "__main__":
    sys.exit(main())
