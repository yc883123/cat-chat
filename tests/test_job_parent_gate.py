# -*- coding: utf-8 -*-
"""守门：已取消的父任务不得再派生新 Job；子 Agent 收尾必须收网逃逸子任务。

背景（2026-09-15 真实事故，会话 9b77c3eb）：chat 主回答 16:25:07 被取消后，
16:26 / 16:29 仍有两个子 Agent 以它为父被创建；其中一个子 Agent 被取消后，
16:32 又派生 ComfyUI Job 并跑完 6/6。两个洞：

1. ``JobRegistry.start()`` 原本不看父状态——取消级联（``_cancel_children``）只抓
   「取消那一刻」已存在的子任务，worker 只要还活着，之后创建的新 Job 完全逃逸；
2. 子 Agent worker 退出时没人复查它名下是否还有活跃子任务。

闸门口径必须精确：**只拦「已取消/正在取消」，不拦「已终态」**——后台任务的常态
就是「回答已发出（父 Run 终态）、子 Job 仍在跑」，``resume()/retry()`` 也要在
终态父下重建 Job；拦了终态等于把正常恢复路径全部掐死。

全量验证口径见 `项目维护说明（修改代码前必读）.md` §六。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core.exceptions import TaskCancelled  # noqa: E402
from naiba.jobs import JobRegistry, JobSpec  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402

AGENT = {"id": "", "name": "Job", "system_prompt": "", "skill_ids": []}


class ParentCancelGateTests(unittest.TestCase):
    """start() 的父任务取消闸门：只拦取消，不拦终态。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.conversation = self.storage.create_conversation()
        self.app = SimpleNamespace(storage=self.storage)
        self.registry = JobRegistry(self.app)
        # noop worker：start() 真的会起线程，用空实现避免副作用。
        self.registry._workers["noop"] = lambda *_args: None

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _parent(self, status: str, cancel_requested: bool = False, conversation_id: str | None = None) -> str:
        run = self.storage.create_run(conversation_id or self.conversation["id"], "回答", AGENT, {}, kind="chat")
        self.storage.update_background_task(
            run["id"], status=status, cancel_requested=cancel_requested
        )
        return str(run["id"])

    def _spec(self, parent_id: str) -> JobSpec:
        return JobSpec(
            kind="noop",
            conversation_id=self.conversation["id"],
            parent_job_id=parent_id or None,
        )

    def test_start_refuses_when_parent_cancel_requested(self) -> None:
        parent = self._parent("running", cancel_requested=True)
        with self.assertRaises(TaskCancelled):
            self.registry.start(self._spec(parent), owner=self.conversation["id"])

    def test_start_refuses_when_parent_stopping_or_cancelled(self) -> None:
        for status in ("stopping", "cancelling", "cancelled"):
            with self.subTest(status=status):
                # 每个子用例一条新会话：stopping/cancelling 属活跃状态，同一会话
                # 只允许占一个顶层运行位，重复 create_run 会被 ACTIVE_RUN 拦下。
                conversation = self.storage.create_conversation()
                parent = self._parent(status, conversation_id=conversation["id"])
                with self.assertRaises(TaskCancelled):
                    self.registry.start(self._spec(parent), owner=conversation["id"])

    def test_start_allows_completed_parent(self) -> None:
        """终态父是后台 Job 的正常归属锚点（回答已发出、子任务仍在跑），不得拦。"""
        parent = self._parent("completed")
        job_id = self.registry.start(self._spec(parent), owner=self.conversation["id"])
        self.assertTrue(job_id)
        self.assertIsNotNone(self.storage.get_background_task(job_id))

    def test_start_allows_missing_parent(self) -> None:
        """父记录已被清理：无从判定取消，不拦（清理语义见 is_job_cleaned）。"""
        job_id = self.registry.start(self._spec("parent-gone"), owner=self.conversation["id"])
        self.assertTrue(job_id)


class SubagentExitCascadeTests(unittest.TestCase):
    """子 Agent 收尾兜底：被取消/失败退出时，收网它名下仍活跃的子 Job。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.conversation = self.storage.create_conversation()
        self.app = SimpleNamespace(storage=self.storage)
        self.registry = JobRegistry(self.app)
        # worker 正常返回（noop）：子 Agent 本身的执行不是这里的关注点。
        self.registry.agent_runner = lambda _job_id, _spec, _cancel, _sink: None

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _subagent(self) -> str:
        run = self.storage.create_run(
            self.conversation["id"], "子 Agent", AGENT, {}, kind="subagent", parent_job_id="chat-run"
        )
        self.storage.update_background_task(run["id"], status="running")
        return str(run["id"])

    def _child(self, parent_id: str) -> str:
        run = self.storage.create_run(
            self.conversation["id"], "ComfyUI 批量生成", AGENT, {}, kind="comfyui",
            parent_job_id=parent_id,
        )
        self.storage.update_background_task(run["id"], status="running")
        return str(run["id"])

    def _run_subagent(self, job_id: str) -> None:
        spec = JobSpec(kind="subagent", conversation_id=self.conversation["id"], params={})
        self.registry._run_subagent(job_id, spec, threading.Event())

    def test_cancelled_subagent_cancels_late_children(self) -> None:
        """复现事故现场：取消之后才被创建的逃逸子任务，收尾时必须被收网。"""
        sub_id = self._subagent()
        self.registry.cancel(sub_id, owner=self.conversation["id"], reason="用户取消")
        # 逃逸点：子任务在取消「之后」才创建，级联那一刻抓不到它。
        child = self._child(sub_id)
        self._run_subagent(sub_id)
        row = self.storage.get_background_task(child)
        self.assertEqual(row["status"], "stopping", "子 Agent 被取消后，其活跃子任务必须被级联停止")
        self.assertTrue(row["cancel_requested"])

    def test_completed_subagent_leaves_children_running(self) -> None:
        """正常完成的子 Agent 不得误杀它留下的后台任务（完成≠取消）。"""
        sub_id = self._subagent()
        child = self._child(sub_id)
        self._run_subagent(sub_id)
        self.assertEqual(self.storage.get_background_task(sub_id)["status"], "completed")
        self.assertEqual(
            self.storage.get_background_task(child)["status"],
            "running",
            "子 Agent 正常完成时，它派生的后台任务应继续跑（与主回答的后台任务同口径）",
        )


if __name__ == "__main__":
    unittest.main()
