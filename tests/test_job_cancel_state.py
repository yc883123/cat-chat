# -*- coding: utf-8 -*-
"""守门：后台任务的「停止」必须真的落得下去、也收得了尾。

背景：``background_tasks`` 是 run 与 job 共用的一张表。任务面板改为只列**后台作业**之后，
面板上的「停止」按钮直接打在单个作业上（``POST /api/jobs/{id}/cancel``，worker 真的会收到
取消信号），因此这条链路上的四个前提都必须钉死：

1. ``JobRegistry.cancel`` 必须同时写 ``cancel_requested``：只写 ``stopping`` 的话，
   ``_set_status`` 里那段「停止请求不被 worker 覆盖」的防护是空转的——worker 的下一次状态
   更新会把 ``stopping`` 打回 ``running``，用户看到的就是「点了停止它还在跑」；
2. worker 卡在网络等待或退避里不退出时，``stopping`` 会永久占住活动名额
   （``stopping`` 属于 ``ACTIVE_TASK_STATUSES``），所以必须有超时兜底把它强制收尾；
3. 兜底只针对**登记过取消事件**的作业：被误传到 Job 接口的顶层 Run 不能被强行标成已取消；
4. ``ConversationRunManager.cancel`` 不得改写已终态的父 Run：后台任务的常态是「回答早已
   发出、只剩子 Job 在跑」，把父行从「已完成」改写成「已取消」等于篡改历史记录。

全量验证口径见 `项目维护说明（修改代码前必读）.md` §六。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba import jobs as jobs_module  # noqa: E402
from naiba.jobs import JobRegistry  # noqa: E402
from naiba.run.manager import ConversationRunManager  # noqa: E402
from naiba.storage.store import ACTIVE_TASK_STATUSES, ChatStorage  # noqa: E402

AGENT = {"id": "", "name": "Job", "system_prompt": "", "skill_ids": []}


class JobCancelStateTests(unittest.TestCase):
    """取消持久化 / 状态冻结 / 停止超时兜底。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.conversation = self.storage.create_conversation()
        self.app = SimpleNamespace(storage=self.storage)
        self.registry = JobRegistry(self.app)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _job(self, kind: str = "shell", status: str = "running", parent: str = "parent-1") -> str:
        """建一条后台作业行。parent 非空 → 不占会话的顶层运行位（与真实 Job 一致）。"""
        run = self.storage.create_run(
            self.conversation["id"], "job", AGENT, {}, kind=kind, parent_job_id=parent
        )
        self.storage.update_background_task(run["id"], status=status)
        return str(run["id"])

    def _register_worker(self, job_id: str) -> threading.Event:
        """模拟 ``start()`` 登记过的 worker（cancel event 与线程句柄都在）。"""
        event = threading.Event()
        self.registry._cancel[job_id] = event
        self.registry._threads[job_id] = threading.Thread(target=lambda: None)
        return event

    def _wait_for_watchdog(self, job_id: str, timeout: float = 10.0) -> None:
        """等到兜底线程**彻底**收尾：状态已落库、取消事件与线程句柄都已摘掉。

        只等「状态变了」是不够的——线程随后还要 ``_forget_job_thread`` 并释放 SQLite 连接，
        不等它就会在 tearDown 里撞上 ``PermissionError: [WinError 32]``（CI runner 上必挂），
        断言本身也会因为读到「状态已改、句柄未摘」的中间态而随机红（本地线程调度快，
        恰好盖住了这个竞态，2026-09-15 流水线实测）。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and job_id in self.registry._cancel:
            time.sleep(0.02)
        self.assertNotIn(job_id, self.registry._cancel, "兜底线程没在超时内收尾")
        self.assertNotIn(job_id, self.registry._threads, "收尾后必须摘掉线程句柄")

    def test_cancel_persists_cancel_requested(self) -> None:
        job_id = self._job()
        snapshot = self.registry.cancel(job_id, owner=self.conversation["id"], reason="测试停止")
        self.assertEqual(snapshot["status"], "stopping")
        row = self.storage.get_background_task(job_id)
        self.assertEqual(row["status"], "stopping")
        self.assertTrue(
            row["cancel_requested"],
            "cancel 必须写 cancel_requested，否则 _set_status 的冻结是空转的",
        )

    def test_worker_status_update_cannot_override_stop_request(self) -> None:
        job_id = self._job()
        self.registry.cancel(job_id, owner=self.conversation["id"])
        # worker 后续的状态更新（内部即 _set_status）不得把 stopping 打回 running
        self.registry._set_status(job_id, "running", current_step="还在跑")
        row = self.storage.get_background_task(job_id)
        self.assertEqual(row["status"], "stopping", "停止请求不能被 worker 的状态更新覆盖")
        self.assertEqual(row["current_step"], "正在停止")

    def test_finish_after_stop_request_reports_cancelled(self) -> None:
        job_id = self._job()
        self.registry.cancel(job_id, owner=self.conversation["id"])
        self.registry._finish(job_id, "completed", result={"ok": True})
        row = self.storage.get_background_task(job_id)
        self.assertEqual(row["status"], "cancelled", "停止请求之后的收尾必须落在 cancelled")

    def test_stop_timeout_watchdog_forces_terminal(self) -> None:
        job_id = self._job()
        self._register_worker(job_id)
        with mock.patch.object(jobs_module, "STOP_WATCHDOG_SECONDS", 0.05):
            self.registry.cancel(job_id, owner=self.conversation["id"])
            # 先把线程等干净再断言：看门狗线程的收尾顺序是「落库 → 发事件 → 摘句柄」，
            # 抢在中间断言会读到半成品，tearDown 还会因为连接没释放而删不掉临时目录。
            self._wait_for_watchdog(job_id)
        row = self.storage.get_background_task(job_id)
        self.assertEqual(
            row["status"], "cancelled",
            "停止超时必须强制收尾——否则它永久占着活动名额，整条会话被判成「回复进行中」",
        )
        self.assertIn("停止超时", row["error"])
        self.assertTrue(row["finished_at"], "强制收尾也要写 finished_at")

    def test_unregistered_job_is_not_force_cancelled(self) -> None:
        """没登记过取消事件的（例如被误传到 Job 接口的顶层 Run）连兜底线程都不该起。

        直接断言「没挂兜底」，而不是 sleep 一会儿看它没被改——前者是确定性的证据，
        后者只能证明「这段时间恰好没出事」。
        """
        job_id = self._job()
        with mock.patch.object(self.registry, "_schedule_stop_watchdog") as scheduled:
            self.registry.cancel(job_id, owner=self.conversation["id"])
        self.assertEqual(scheduled.call_count, 0, "没有 worker 就不该挂兜底线程")
        self.assertEqual(self.storage.get_background_task(job_id)["status"], "stopping")

    def test_cancel_cascades_to_children(self) -> None:
        parent = self._job(kind="shell")
        self._register_worker(parent)
        child = self._job(kind="comfyui", parent=parent)
        # 父任务会挂兜底线程：把超时推到很远，这里断言的是「取消已级联到子任务」，
        # 不是「兜底已经把父任务收尾了」（默认 90 秒会在本用例结束后唤醒，届时临时库已删）。
        with mock.patch.object(jobs_module, "STOP_WATCHDOG_SECONDS", 3600.0):
            self.registry.cancel(parent, owner=self.conversation["id"], reason="父任务取消")
        self.assertEqual(self.storage.get_background_task(parent)["status"], "stopping")
        self.assertEqual(self.storage.get_background_task(child)["status"], "stopping")


class ParentRunTerminalPreservationTests(unittest.TestCase):
    """已终态的父 Run 只是子任务的归属锚点，不能被取消流程改写。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.conversation = self.storage.create_conversation()
        self.app = SimpleNamespace(storage=self.storage)
        self.app.jobs = JobRegistry(self.app)
        self.manager = ConversationRunManager(self.app)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_completed_parent_is_kept_and_children_are_stopped(self) -> None:
        parent = self.storage.create_run(self.conversation["id"], "回答", AGENT, {}, kind="chat")
        self.storage.update_background_task(parent["id"], status="completed", finished=True)
        child = self.storage.create_run(
            self.conversation["id"], "内层", AGENT, {}, kind="comfyui", parent_job_id=str(parent["id"])
        )
        self.storage.update_background_task(child["id"], status="running")

        updated = self.manager.cancel(str(parent["id"]))

        parent_row = self.storage.get_background_task(str(parent["id"]))
        self.assertEqual(
            parent_row["status"], "completed",
            "已终态的父 Run 不得被改写成已取消（后台任务的常态就是父已结束、子仍在跑）",
        )
        self.assertFalse(parent_row["cancel_requested"])
        child_row = self.storage.get_background_task(str(child["id"]))
        self.assertEqual(child_row["status"], "stopping", "级联必须停掉还在跑的子系统")
        self.assertTrue(child_row["cancel_requested"])
        self.assertEqual(updated["status"], "completed")

    def test_active_parent_still_goes_to_cancelling(self) -> None:
        """回归：真正的「回答进行中」仍要走 cancelling + 3 秒看门狗那条老路。"""
        parent = self.storage.create_run(self.conversation["id"], "回答", AGENT, {}, kind="chat")
        self.storage.update_background_task(parent["id"], status="running")
        updated = self.manager.cancel(str(parent["id"]))
        self.assertEqual(updated["status"], "cancelling")
        self.assertTrue(self.storage.get_background_task(str(parent["id"]))["cancel_requested"])


class StoreStatusVocabularySourceTests(unittest.TestCase):
    """「未结束」状态集合只能有一份定义：三处自写状态列表就是僵尸状态的来源。"""

    def test_hardcoded_active_status_lists_are_gone(self) -> None:
        source = (ROOT / "naiba" / "storage" / "store.py").read_text(encoding="utf-8")
        self.assertNotIn(
            "'queued', 'running', 'waiting', 'cancelling'", source,
            "状态列表必须引用 ACTIVE_TASK_STATUSES 常量，不得再手写（漏 stopping 即僵尸）",
        )
        self.assertGreaterEqual(
            source.count("_status_in_clause(ACTIVE_TASK_STATUSES)"), 4,
            "启动清理、活动列表、顶层占用判定的每一处都要走同一份定义",
        )

    def test_stopping_is_covered_by_active_set(self) -> None:
        self.assertIn("stopping", ACTIVE_TASK_STATUSES)


if __name__ == "__main__":
    unittest.main()
