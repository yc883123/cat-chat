# -*- coding: utf-8 -*-
"""冻结版实跑自检（打包后必跑）：确认本次修复相关的代码/常量真的进了 exe。

用法：`dist\\naiba-chat.exe --run-skill-script <本文件绝对路径>`（走 launcher 的隐藏入口，
不起 GUI/HTTP/实例锁）。**只能断言运行期可达的东西**：冻结版把源码打进 PYZ、
`_MEIPASS` 下没有可读的 `.py`，所以这里一律用 import + 常量 + inspect，
不读源码文件（源码级断言在 `tests/test_interrupt_recovery.py` 里）。
"""
from __future__ import annotations

import inspect
import sys
import tempfile
from pathlib import Path

# 源码模式直接跑本文件时，仓库根不在 sys.path 上（冻结版走打包内的 PYZ，不需要）。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{'' if ok or not detail else '  -> ' + detail}")
    if not ok:
        failures.append(label)


print(f"frozen={getattr(sys, 'frozen', False)} python={sys.version.split()[0]}")

from naiba.run.chat import ConversationRunMixin  # noqa: E402
from naiba.storage.store import INTERRUPTED_TASK_REASON, ChatStorage  # noqa: E402

check("ConversationRunMixin.recover_interrupted_runs 存在",
      callable(getattr(ConversationRunMixin, "recover_interrupted_runs", None)))
check("中断原因常量就位", INTERRUPTED_TASK_REASON == "服务重启，运行已中断", INTERRUPTED_TASK_REASON)
placeholder = inspect.signature(ConversationRunMixin._persist_failed_message).parameters.get("placeholder")
check("_persist_failed_message 支持覆盖占位正文", placeholder is not None
      and placeholder.default == "（本次回答未完成）")

with tempfile.TemporaryDirectory() as tmp:
    storage = ChatStorage(Path(tmp) / "chat.db")
    check("ChatStorage 启动即交出 interrupted_tasks", hasattr(storage, "interrupted_tasks")
          and storage.interrupted_tasks == [])

from naiba.updater import APPLY_UPDATE_SCRIPT  # noqa: E402

start_lines = [line for line in APPLY_UPDATE_SCRIPT.splitlines() if "Start-Process" in line]
check("重启脚本两条 Start-Process 都不带 -WindowStyle Hidden",
      len(start_lines) == 2 and all("-WindowStyle Hidden" not in line for line in start_lines),
      str(start_lines))

from launcher import Launcher  # noqa: E402

check("Launcher 有窗口就绪兜底 _on_window_loaded",
      callable(getattr(Launcher, "_on_window_loaded", None)))

print("FAILED: " + " | ".join(failures) if failures else "ALL PASS")
raise SystemExit(1 if failures else 0)
