# -*- coding: utf-8 -*-
"""shell Job 的墙钟上限，以及「取消 / 超时必须在秒级生效」。

守门要点：

1. **上限要与「同一件事」的其他入口对齐**：pwsh / run_skill_script 是 7200
   （``tools/providers/core.py``），收集端 ``job_wait`` 默认也肯等 7200——原先这里
   钳在 900，等于「收集端愿意等 2 小时、被收集的任务 15 分钟就死」，系统提示里
   「耗时任务用 run_in_background」在一个说不出口的数字上静默失效。
2. **取消与超时必须秒级生效**：``_run_shell`` 原先在循环里直接 ``proc.stdout.readline()``，
   子进程长时间无输出时会**永久阻塞**，循环体里的 ``cancel.is_set()`` / ``elapsed >= timeout``
   形同虚设。``tests/test_tool_progress_stream.py`` 第 3 条已把这种写法写成硬标准
   （"不许再退回 readline 直读"），当时只落到了 pwsh/run_skill_script 路径。
   下面两条用例用「静默 6 秒」的命令钉死：**退回阻塞直读即红**。
3. 改造不得改变输出语义：逐行 ``job_log``、空行不进日志、终态 ``exit_code`` / ``output`` 不变。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.jobs import (  # noqa: E402
    JOB_SHELL_DEFAULT_TIMEOUT_SECONDS,
    JOB_SHELL_MAX_TIMEOUT_SECONDS,
    JobRegistry,
    JobSpec,
    _job_shell_timeout,
)
from naiba.storage.store import ChatStorage  # noqa: E402

SILENT_SECONDS = 6          # 静默时长：比断言窗口长，退回阻塞直读就必然超窗
ASSERT_WINDOW_SECONDS = 4.0


class _ConfigStub:
    def __init__(self, data_dir: Path) -> None:
        self.data = {"workspace_dir": str(data_dir)}
        self._data_dir = data_dir

    def resolve_data_dir(self) -> Path:
        return self._data_dir


class _BusStub:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self._conditions: dict[str, threading.Condition] = {}

    def emit(self, job_id: str, payload: dict, raise_on_error: bool = True) -> None:
        self.events.append((job_id, payload))

    def ensure(self, job_id: str) -> threading.Condition:
        return self._conditions.setdefault(job_id, threading.Condition())


class _AppStub:
    """JobRegistry 只需要 storage / config / event_bus。"""

    def __init__(self, storage: ChatStorage, config: _ConfigStub, bus: _BusStub) -> None:
        self.storage = storage
        self.config = config
        self.event_bus = bus


@unittest.skipUnless(os.name == "nt", "shell Job 走 powershell.exe")
class ShellJobTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba_job_shell_"))
        self.data_dir = self.tmp / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.storage = ChatStorage(self.data_dir / "chat.db")
        self.bus = _BusStub()
        self.registry = JobRegistry(_AppStub(self.storage, _ConfigStub(self.data_dir), self.bus))
        self.conversation_id = str(self.storage.create_conversation("shell Job")["id"])

    def tearDown(self) -> None:
        self.registry.shutdown(timeout=2.0)
        self.tmp.cleanup() if hasattr(self.tmp, "cleanup") else None

    def _start(self, command: str, timeout: int) -> str:
        return self.registry.start(JobSpec(
            kind="shell",
            conversation_id=self.conversation_id,
            params={"command": command, "timeout": timeout},
        ))

    def _wait_terminal(self, job_id: str, seconds: float) -> dict:
        deadline = time.monotonic() + seconds
        job: dict = {}
        while time.monotonic() < deadline:
            job = self.registry.get(job_id) or {}
            if str(job.get("status") or "") in {"completed", "failed", "cancelled", "interrupted"}:
                return job
            time.sleep(0.1)
        return job

    # ---- 1. 上限与钳位 ----
    def test_timeout_clamp_matches_the_other_entries(self) -> None:
        self.assertEqual(JOB_SHELL_MAX_TIMEOUT_SECONDS, 7200, "与 pwsh / run_skill_script / job_wait 对齐")
        self.assertEqual(_job_shell_timeout({}), JOB_SHELL_DEFAULT_TIMEOUT_SECONDS)
        self.assertEqual(_job_shell_timeout({"timeout": 7201}), 7200, "越界钳到上限")
        self.assertEqual(_job_shell_timeout({"timeout": 0}), 1, "下限 1 秒")
        self.assertEqual(_job_shell_timeout({"timeout": "300"}), 300, "字符串数字可接受")
        self.assertEqual(
            _job_shell_timeout({"timeout": "abc"}), JOB_SHELL_DEFAULT_TIMEOUT_SECONDS,
            "非法值退回默认而不是把整条任务打成异常（原先那行在 try 之外）",
        )

    # ---- 2. 静默命令：取消 / 超时秒级生效（判决性）----
    def test_silent_command_cancels_promptly(self) -> None:
        job_id = self._start(f"Start-Sleep -Seconds {SILENT_SECONDS}", timeout=120)
        time.sleep(0.6)
        self.registry.cancel(job_id)
        job = self._wait_terminal(job_id, ASSERT_WINDOW_SECONDS)
        self.assertEqual(
            job.get("status"), "cancelled",
            "静默命令必须能被秒级取消（退回 readline 直读就会红：它会一直跑到命令自己结束）",
        )

    def test_silent_command_hits_timeout_promptly(self) -> None:
        job_id = self._start(f"Start-Sleep -Seconds {SILENT_SECONDS}", timeout=1)
        job = self._wait_terminal(job_id, ASSERT_WINDOW_SECONDS)
        self.assertEqual(job.get("status"), "failed", "超时必须自己收尾，不能等命令跑完")
        self.assertIn("超时", str(job.get("error") or ""), "超时要带原因，便于任务面板说明")

    # ---- 3. 输出语义不变 ----
    def test_output_lines_and_exit_code_are_preserved(self) -> None:
        job_id = self._start('1..3 | ForEach-Object { "line$_" }', timeout=60)
        job = self._wait_terminal(job_id, 30.0)
        self.assertEqual(job.get("status"), "completed", str(job.get("error") or ""))
        result = job.get("result") or {}
        self.assertEqual(result.get("exit_code"), 0)
        self.assertEqual(str(result.get("output") or "").splitlines(), ["line1", "line2", "line3"])

    def test_blank_lines_are_not_logged(self) -> None:
        job_id = self._start("Write-Output 'a'\nWrite-Output ''\nWrite-Output 'b'", timeout=60)
        job = self._wait_terminal(job_id, 30.0)
        self.assertEqual(job.get("status"), "completed", str(job.get("error") or ""))
        self.assertEqual(str((job.get("result") or {}).get("output") or "").splitlines(), ["a", "b"])
        logged = [
            str((payload or {}).get("line") or "")
            for _job, payload in self.bus.events
            if str((payload or {}).get("type") or "") == "job_log"
        ]
        self.assertEqual(logged, ["a", "b"], "空行不进 job_log（改造前 if line: 的口径）")


if __name__ == "__main__":
    unittest.main()
