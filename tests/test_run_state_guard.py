# -*- coding: utf-8 -*-
"""守门：「谁算在运行」的口径统一，以及「停止」这条链路真的可达。

背景（2026-09-14 客户机实测）：一台机器用本地 unsloth 学习 16 小时后，界面永久
停在「等待本地模型资源」。用户反馈三条互相咬死的现象——点「停止」无效、鼠标移到
AI 回复上点「新会话」只弹「请先等待当前回答结束或停止后再划分割线」、重启应用乃至
重启电脑都一样。四处独立缺陷叠成这个死锁：

1. 启动清理的状态集合漏了 ``stopping``：``jobs.cancel()`` 会写它、活动白名单也认它，
   于是「已取消但线程没退出去」的子任务成了重启也清不掉的僵尸；
2. ``/api/runs`` 把后台子 Job 也当成「会话正在回答」⇒ 整条会话被判成运行中，
   「分支 / 重新生成 / 新会话」三个救援入口全被挡住；
3. ``cancelCurrentRun`` 只在「确认停止成功」时才复位界面 ⇒ 停止失败即永久冻住；
4. 流式读取阻塞在 socket ``readline``，循环体不看 ``cancel_event`` ⇒ 本地模型
   prefill 期间「停止」天然无效（只能等满 30 分钟超时）。

本文件是上述四条的回归守门。全量验证口径见 `项目维护说明（修改代码前必读）.md` §六。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.llm import runtime as llm_runtime  # noqa: E402
from naiba.run.manager import (  # noqa: E402
    PRIMARY_RUN_KINDS,
    ConversationRunManager,
)
from naiba.storage import store as storage_module  # noqa: E402
from naiba.storage.store import (  # noqa: E402
    ACTIVE_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    ChatStorage,
)


def _read(name: str) -> str:
    return (ROOT / "public" / "js" / name).read_text(encoding="utf-8")


def _function_body(source: str, signature: str) -> str:
    """取函数体（到下一个顶层 function/export function 之前）。"""
    import re

    start = source.index(signature)
    rest = source[start:]
    match = re.search(r"\n(?:export )?function ", rest[1:])
    return rest if match is None else rest[: match.start() + 1]


class RunStateVocabularyTests(unittest.TestCase):
    """状态词表必须只有一份真相：否则又会出现「某处认它、某处不认」的僵尸状态。"""

    def test_active_statuses_include_stopping(self) -> None:
        self.assertIn(
            "stopping",
            ACTIVE_TASK_STATUSES,
            "jobs.cancel() 会把子任务置为 stopping；漏掉它就没有任何清理会覆盖该状态",
        )

    def test_active_and_terminal_are_disjoint(self) -> None:
        self.assertEqual(set(ACTIVE_TASK_STATUSES) & set(TERMINAL_TASK_STATUSES), set())

    def test_interrupted_counts_as_terminal(self) -> None:
        self.assertIn("interrupted", TERMINAL_TASK_STATUSES, "重启清理产生的状态必须是终态")

    def test_manager_shares_storage_vocabulary(self) -> None:
        self.assertEqual(ConversationRunManager.ACTIVE, set(ACTIVE_TASK_STATUSES))
        self.assertEqual(ConversationRunManager.TERMINAL, set(TERMINAL_TASK_STATUSES))

    def test_job_active_subset_of_store_active(self) -> None:
        """jobs 与 store 两份词表必须互相包含，任何一侧新增状态都要同步。"""
        from naiba.jobs import JOB_ACTIVE

        self.assertTrue(
            JOB_ACTIVE <= set(ACTIVE_TASK_STATUSES),
            f"jobs.JOB_ACTIVE 有 store 未覆盖的状态：{JOB_ACTIVE - set(ACTIVE_TASK_STATUSES)}",
        )


class StartupCleanupTests(unittest.TestCase):
    """重启清理必须覆盖 stopping —— 这是「重启电脑也没用」的直接原因。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "chat.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_child_task(self, storage: ChatStorage, status: str) -> str:
        conversation = storage.create_conversation(title="t")
        conversation_id = str(conversation["id"])
        run, _history = storage.create_chat_run(
            conversation_id,
            "子任务",
            [],
            {"id": "a1", "name": "Agent"},
            {},
            "craft",
            parent_job_id="parent-run-id",
        )
        run_id = str(run["id"])
        storage.update_background_task(run_id, status=status)
        return run_id

    def test_stopping_child_is_cleared_on_restart(self) -> None:
        first = ChatStorage(self.db_path)
        run_id = self._make_child_task(first, "stopping")
        self.assertEqual(first.get_background_task(run_id)["status"], "stopping")

        # 重新构造 = 服务重启（_initialize 里做启动清理）
        second = ChatStorage(self.db_path)
        self.assertEqual(
            second.get_background_task(run_id)["status"],
            "interrupted",
            "卡在 stopping 的子任务必须随重启被终结，否则永远清不掉",
        )
        self.assertFalse(
            second.list_background_tasks("", active_only=True),
            "清理之后不应再有任何「未结束」任务残留",
        )

    def test_running_task_is_still_cleared(self) -> None:
        first = ChatStorage(self.db_path)
        run_id = self._make_child_task(first, "running")
        second = ChatStorage(self.db_path)
        self.assertEqual(second.get_background_task(run_id)["status"], "interrupted")


class _StorageStub:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, bool]] = []

    def list_background_tasks(
        self, conversation_id: str = "", active_only: bool = False, limit: int = 50
    ) -> list[dict]:
        self.calls.append((conversation_id, active_only))
        return list(self.rows)


class _AppStub:
    def __init__(self, rows: list[dict]) -> None:
        self.storage = _StorageStub(rows)


def _manager_with(rows: list[dict]) -> ConversationRunManager:
    """不跑 __init__（避免需要完整 app），只注入 storage。"""
    manager = ConversationRunManager.__new__(ConversationRunManager)
    manager.app = _AppStub(rows)
    return manager


class PrimaryRunFilterTests(unittest.TestCase):
    """`/api/runs` 只回顶层对话 Run：子 Job 不得让整条会话进入「运行中」。"""

    def test_child_job_does_not_count_as_conversation_running(self) -> None:
        rows = [
            {
                "id": "child-1",
                "kind": "chat",
                "status": "stopping",
                "parent_job_id": "run-1",
                "conversation_id": "c1",
            },
            {
                "id": "job-2",
                "kind": "vision",
                "status": "running",
                "parent_job_id": "",
                "conversation_id": "c1",
            },
        ]
        manager = _manager_with(rows)
        self.assertEqual(
            manager.list_primary("c1", active_only=True),
            [],
            "后台子任务不得让会话显示「回复进行中」——否则三个救援入口全被挡住",
        )

    def test_top_level_conversation_run_is_returned(self) -> None:
        rows = [
            {"id": "job-2", "kind": "vision", "status": "running", "parent_job_id": "", "conversation_id": "c1"},
            {"id": "run-1", "kind": "chat", "status": "running", "parent_job_id": "", "conversation_id": "c1"},
        ]
        manager = _manager_with(rows)
        primary = manager.list_primary("c1", active_only=True)
        self.assertEqual([item["id"] for item in primary], ["run-1"])

    def test_plan_execute_counts_as_primary(self) -> None:
        self.assertIn("plan_execute", PRIMARY_RUN_KINDS)
        rows = [{"id": "p1", "kind": "plan_execute", "status": "running", "parent_job_id": "", "conversation_id": "c1"}]
        self.assertEqual([item["id"] for item in _manager_with(rows).list_primary("c1")], ["p1"])

    def test_list_still_exposes_children_for_task_panel(self) -> None:
        rows = [{"id": "child-1", "kind": "chat", "status": "running", "parent_job_id": "run-1", "conversation_id": "c1"}]
        manager = _manager_with(rows)
        self.assertEqual(len(manager.list("c1", True)), 1, "/api/tasks 侧必须仍能看到子任务")


class _BlockingStream:
    """模拟「模型一个字都不吐」的本地流：迭代时阻塞，直到 close() 被调用。"""

    def __init__(self) -> None:
        self.closed = threading.Event()
        self.iterating = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        self.iterating.set()
        if self.closed.wait(5):
            raise ValueError("readline of closed file")
        raise TimeoutError("test stream never closed")

    def close(self) -> None:
        self.closed.set()


class _LineStream:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def __iter__(self):
        return iter(self._lines)


class _BrokenStream:
    def __iter__(self):
        return self

    def __next__(self):
        raise RuntimeError("网络中断")


class StreamCancellationTests(unittest.TestCase):
    """流式读取必须响应取消：否则本地模型 prefill 期间「停止」形同虚设。"""

    def test_yields_all_lines_without_cancel(self) -> None:
        lines = [b"data: one\n", b"data: two\n"]
        self.assertEqual(
            list(llm_runtime.ModelRuntime._iter_stream_lines(_LineStream(lines), None)),
            lines,
        )

    def test_already_cancelled_raises_before_reading(self) -> None:
        cancel_event = threading.Event()
        cancel_event.set()
        with self.assertRaises(RuntimeError) as ctx:
            list(llm_runtime.ModelRuntime._iter_stream_lines(_LineStream([b"x"]), cancel_event))
        self.assertIn("取消", str(ctx.exception))

    def test_blocked_read_is_aborted_promptly(self) -> None:
        """核心用例：读取线程正卡在等字节，取消必须能把它拽出来。"""
        stream = _BlockingStream()
        cancel_event = threading.Event()
        errors: list[str] = []

        def consume() -> None:
            try:
                for _line in llm_runtime.ModelRuntime._iter_stream_lines(stream, cancel_event):
                    pass
            except RuntimeError as exc:
                errors.append(str(exc))

        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        self.assertTrue(stream.iterating.wait(3), "读取线程应已进入阻塞读")
        started = time.perf_counter()
        cancel_event.set()
        worker.join(3)
        elapsed = time.perf_counter() - started
        self.assertFalse(worker.is_alive(), "取消后读取线程必须立刻退出，而不是等满超时")
        self.assertLess(elapsed, 2.5, f"取消耗时应接近即时，实际 {elapsed:.2f}s")
        self.assertTrue(errors, "取消必须抛出可识别的取消错误")
        self.assertIn("取消", errors[0])

    def test_non_cancel_error_propagates_unchanged(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            list(llm_runtime.ModelRuntime._iter_stream_lines(_BrokenStream(), threading.Event()))
        self.assertIn("网络中断", str(ctx.exception))

    def test_stream_paths_use_the_cancelable_iterator(self) -> None:
        """回归守门：三种流式解析都必须包一层可取消迭代器（且带上首字节超时）。"""
        source = (ROOT / "naiba" / "llm" / "runtime.py").read_text(encoding="utf-8")
        self.assertNotIn("_read_sse_response(response,", source)
        self.assertNotIn("_read_lm_studio_stream(response,", source)
        self.assertNotIn("_read_ollama_stream(response,", source)
        self.assertEqual(
            source.count("_iter_stream_lines(response, cancel_event, first_byte_timeout)"),
            3,
            "ollama / lm_studio / sse 三条流式路径都要接上取消 + 首字节超时",
        )
        self.assertEqual(
            source.count("_iter_stream_lines(retry_response, cancel_event, first_byte_timeout)"),
            1,
            "Ollama 的 think=false 重发路径同样要接上",
        )


class FrontendStopChainTests(unittest.TestCase):
    """前端：停止失败也必须复位界面；重连只认顶层对话 Run。"""

    def test_cancel_unlocks_ui_even_when_not_confirmed(self) -> None:
        body = _function_body(_read("12-chat-input.js"), "export async function cancelCurrentRun")
        self.assertNotIn(
            "if (terminalConfirmed &&",
            body,
            "复位界面不得以「服务端已确认停止」为前提：否则停止失败即永久冻住，"
            "输入框与三个救援入口全部不可用",
        )
        self.assertIn("if (state.cancelConversationId === conversationId) {", body)
        self.assertIn("setBusy(false)", body)
        self.assertIn("failureMessage", body, "失败要通过统一变量提示，避免重复 toast")

    def test_run_reconnect_ignores_child_jobs(self) -> None:
        body = _function_body(_read("11-run-stream.js"), "export async function resumeConversationRun")
        self.assertNotIn("(result.runs || [])[0]", body, "不得无条件取第一条（可能正是子任务）")
        self.assertIn("parent_job_id", body)

    def test_poll_recovery_ignores_child_jobs(self) -> None:
        body = _function_body(_read("06-tasks-plans.js"), "export async function maybeRecoverRunFromPoll")
        self.assertNotIn("(result.runs || [])[0]", body)
        self.assertIn("parent_job_id", body)

    def test_api_runs_route_uses_primary_listing(self) -> None:
        source = (ROOT / "naiba" / "http.py").read_text(encoding="utf-8")
        self.assertIn(
            "self.app.runs.list_primary(conversation_id, active_only)",
            source,
            "/api/runs 必须只回顶层对话 Run",
        )


class MaintenanceDocSyncTests(unittest.TestCase):
    """快照式文档必须与代码同步（项目规则：改代码即改写对应章节）。"""

    def test_doc_mentions_the_unified_status_set(self) -> None:
        doc = (ROOT / "项目维护说明（修改代码前必读）.md").read_text(encoding="utf-8")
        self.assertIn("list_primary", doc)
        self.assertIn("stopping", doc)


if __name__ == "__main__":
    unittest.main()
