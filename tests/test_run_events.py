# -*- coding: utf-8 -*-
"""护栏：Run 事件流与取消竞态行为规格。

保护对象：阶段 2 将 async_tasks 拆成 run/{manager,stream,session,chat} 时的行为等价性。
覆盖：emit→状态映射、cancelling 冻结、ACTIVE_RUN 互斥、3 秒强制取消看门狗兜底。
（看门狗/互斥用例为确定性实现：patch time.sleep 使 3 秒延迟归零。）
"""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.http import RequestHandler  # noqa: E402
from naiba.run.manager import ConversationRunManager  # noqa: E402
from naiba.run import manager as run_manager  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402


class RecordingStorage:
    def __init__(self):
        self.events = []
        self.task = {"status": "running", "conversation_id": "c1", "detail": {}}
        self.updates = []

    def append_run_event(self, run_id, payload):
        self.events.append((run_id, payload))
        return {"run_id": run_id, "sequence": len(self.events)}

    def get_background_task(self, run_id):
        return dict(self.task)

    def update_background_task(self, run_id, **kw):
        self.updates.append((run_id, kw))


class StubApp:
    def __init__(self):
        self.storage = RecordingStorage()


class SharedEventConnectionTests(unittest.TestCase):
    """run 事件流复用长连接（storage.open_event_connection + append_run_event(db=...)）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        convo = self.storage.create_conversation()
        agent = {"id": "general", "name": "通用 Agent"}
        self.run = self.storage.create_chat_run(
            str(convo["id"]), "你好", [], agent, {"model_key": "online:demo"}, "craft"
        )
        self.run_id = str(self.run["id"])

    def test_shared_connection_appends_and_stays_consistent(self):
        conn = self.storage.open_event_connection()
        try:
            first = self.storage.append_run_event(
                self.run_id, {"type": "status", "message": "开始"}, db=conn
            )
            second = self.storage.append_run_event(
                self.run_id, {"type": "delta", "content": "字"}, db=conn
            )
        finally:
            conn.close()
        self.assertEqual((first["sequence"], second["sequence"]), (1, 2), "序号连续、不重号")
        events = self.storage.list_run_events(self.run_id)
        self.assertEqual([e["type"] for e in events], ["status", "delta"])

    def test_shared_connection_unknown_run_still_raises(self):
        conn = self.storage.open_event_connection()
        try:
            with self.assertRaises(LookupError):
                self.storage.append_run_event("missing", {"type": "delta", "content": "x"}, db=conn)
            # 失败后回滚，连接必须还能继续用（不能一次失败就报废整条 run 的事件流）。
            event = self.storage.append_run_event(
                self.run_id, {"type": "delta", "content": "仍可用"}, db=conn
            )
            self.assertEqual(event["sequence"], 1)
        finally:
            conn.close()


class RunEventTests(unittest.TestCase):
    def setUp(self):
        self.storage = RecordingStorage()
        self.manager = ConversationRunManager(StubApp())
        self.manager.app.storage = self.storage

    def test_status_event_marks_running(self):
        self.manager.emit("r1", {"type": "status", "message": "开始执行"})
        run_id, kw = self.storage.updates[-1]
        self.assertEqual(run_id, "r1")
        self.assertEqual(kw["status"], "running")
        self.assertEqual(kw["detail"]["message"], "开始执行")

    def test_tool_confirm_marks_waiting(self):
        self.manager.emit(
            "r1",
            {"type": "tool_confirm", "tool_name": "pwsh", "tool_desc": "执行命令",
             "arguments": {"command": "dir"}, "confirm_id": "c9"},
        )
        run_id, kw = self.storage.updates[-1]
        self.assertEqual(kw["status"], "waiting")
        self.assertIn("等待工具确认", kw["detail"]["message"])
        self.assertEqual(kw["detail"]["confirm_id"], "c9")

    def test_tool_result_reads_back_to_running(self):
        self.manager.emit("r1", {"type": "tool_result", "tool": "pwsh"})
        run_id, kw = self.storage.updates[-1]
        self.assertEqual(kw["status"], "running")

    def test_cancelling_freezes_status_updates(self):
        self.storage.task["status"] = "cancelling"
        self.manager.emit("r1", {"type": "status", "message": "不应改写状态"})
        # 终态冻结：status 不落库（update 仍会带 detail 但 status=None）。
        run_id, kw = self.storage.updates[-1]
        self.assertIsNone(kw["status"])

    def test_watchdog_forces_cancelled_when_run_stuck(self):
        # 3 秒兜底看门狗：run 线程未及时收尾时，仍把状态置 cancelled 并发出事件。
        self.storage.task = {"status": "cancelling", "conversation_id": "c1", "detail": {}}
        with mock.patch.object(run_manager.time, "sleep", return_value=None):
            self.manager._schedule_forced_cancel("r1")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            cancelled = [
                kw for _run_id, kw in self.storage.updates
                if kw.get("status") == "cancelled"
            ]
            if cancelled:
                break
            time.sleep(0.02)
        self.assertTrue(cancelled, "看门狗未把卡住的 run 置为 cancelled")
        self.assertTrue(
            any(payload.get("type") == "cancelled" for _rid, payload in self.storage.events),
            "看门狗未发出 cancelled 事件",
        )


class _FakeWFile:
    """`_stream_run` 只用到 write/flush，行内容按 UTF-8 NDJSON 收着供断言。"""

    def __init__(self) -> None:
        self.raw = ""

    def write(self, data: bytes) -> None:
        self.raw += bytes(data).decode("utf-8")

    def flush(self) -> None:
        return None

    @property
    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.raw.splitlines() if line.strip()]


class _FakeRuns:
    """最小 runs 门面：与真实实现同构的关键点是"无事件才等"（见 EventBus.wait_for_events）。"""

    TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})
    TERMINAL_EVENT_TYPES = frozenset({"done", "cancelled", "error"})

    def __init__(self, status: str = "running", events: list[dict] | None = None) -> None:
        self.status = status
        self.events = list(events or [])
        self.waits = 0
        self.on_wait = None
        self._wake = threading.Event()

    def get(self, run_id: str) -> dict:
        return {"id": run_id, "status": self.status}

    def append(self, payload: dict, sequence: int) -> None:
        self.events.append({**payload, "sequence": sequence})
        self._wake.set()

    def wait_for_events(self, run_id: str, after: int = 0, timeout: float = 15.0) -> list[dict]:
        self.waits += 1
        if self.on_wait is not None:
            self.on_wait(self.waits)
        pending = [item for item in self.events if item["sequence"] > after]
        if pending:
            return pending
        # 与 manager.wait_for_events 逐条同构：run 已终态时**不再等待**（正是这个短路让
        # "状态先终态、终态事件后落库"的窗口变成丢收尾事件的窗口）。
        if self.status in self.TERMINAL:
            return []
        self._wake.wait(timeout)
        self._wake.clear()
        return [item for item in self.events if item["sequence"] > after]

    def events_after(self, run_id: str, after: int = 0) -> list[dict]:
        return [item for item in self.events if item["sequence"] > after]

    def terminal_event_sequence(self, run_id: str) -> int:
        sequences = [
            item["sequence"] for item in self.events
            if item["type"] in self.TERMINAL_EVENT_TYPES
        ]
        return max(sequences) if sequences else 0

    def owns_confirmation(self, run_id: str, confirm_id: str) -> bool:
        return True


class _FakeStreamHandler:
    """`RequestHandler._stream_run` 的宿主替身（只提供它真正用到的属性）。"""

    def __init__(self, runs: _FakeRuns) -> None:
        self.app = SimpleNamespace(runs=runs)
        self.wfile = _FakeWFile()
        self.close_connection = False
        self.status = 0
        self.headers: list[tuple[str, str]] = []

    def send_response(self, status) -> None:  # noqa: ANN001 - 与 BaseHTTPRequestHandler 同形
        self.status = status

    def send_header(self, key: str, value: str) -> None:
        self.headers.append((key, value))

    def end_headers(self) -> None:
        return None


class RunEventStreamCloseTests(unittest.TestCase):
    """`_stream_run` 关流判据（2026-10 修）：**终态事件必须送达客户端**才允许关流。

    为什么钉死：run 状态置终态（`update_background_task`）与终态事件落库（`emit`）不是一次
    原子写——收尾路径先置状态再发 done/cancelled/error。旧判据只看"状态已终态 + 没有新事件"
    就 break，窗口命中时客户端整轮收不到终态事件（实测 `verify/run_freeze_smoke.py` 三次里
    中过一次）。判据见 `naiba/http.py::_stream_run`：已送达 / 早已在游标之下 → 关流；
    状态先到而事件还没落库 → 宽限等它。
    """

    def test_terminal_event_is_delivered_when_status_lands_first(self) -> None:
        """状态先变终态、终态事件随后落库：done 仍必须送到客户端（旧判据会在此关流）。"""
        runs = _FakeRuns(status="completed", events=[{"type": "usage", "sequence": 1}])

        def on_wait(waits: int) -> None:
            if waits >= 2:  # 宽限期内终态事件落库（真实窗口只有几毫秒）
                runs.append({"type": "done", "followup_run_id": ""}, 2)

        runs.on_wait = on_wait
        handler = _FakeStreamHandler(runs)
        with mock.patch("naiba.http.TERMINAL_EVENT_GRACE_SECONDS", 0.2):
            RequestHandler._stream_run(handler, "run-1")
        types = [event["type"] for event in handler.wfile.events]
        self.assertIn("done", types, "状态先终态、事件后落库时，done 必须仍然送达客户端")
        self.assertEqual(types[-1], "done", "done 之后不该再写别的事件")

    def test_no_grace_when_terminal_event_already_passed_the_cursor(self) -> None:
        """客户端此前已拿到终态事件（游标在它之后）：立即关流，不再等宽限。"""
        runs = _FakeRuns(status="completed", events=[{"type": "done", "sequence": 2}])
        handler = _FakeStreamHandler(runs)
        started = time.monotonic()
        RequestHandler._stream_run(handler, "run-1", after=2)
        elapsed = time.monotonic() - started
        self.assertEqual(handler.wfile.events, [], "游标之后没有事件可发")
        self.assertLess(elapsed, 1.0, "终态事件早已送达，不得再等宽限（真实宽限 2s）")

    def test_stream_closes_right_after_terminal_event(self) -> None:
        """终态事件写出去就关流：不必再等一轮状态检查（此刻状态甚至还是 running）。"""
        runs = _FakeRuns(status="running", events=[{"type": "done", "sequence": 1}])
        handler = _FakeStreamHandler(runs)
        started = time.monotonic()
        RequestHandler._stream_run(handler, "run-1")
        self.assertEqual([event["type"] for event in handler.wfile.events], ["done"])
        self.assertLess(time.monotonic() - started, 1.0, "终态事件之后不该再空转等待")

    def test_run_without_terminal_event_closes_after_grace(self) -> None:
        """真没有终态事件的 run（崩溃/被中断清理）：宽限到期后照旧关流，不能挂死。"""
        runs = _FakeRuns(status="failed", events=[{"type": "status", "sequence": 1}])
        handler = _FakeStreamHandler(runs)
        with mock.patch("naiba.http.TERMINAL_EVENT_GRACE_SECONDS", 0.2):
            started = time.monotonic()
            RequestHandler._stream_run(handler, "run-1")
            elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.2, "宽限期没到就关流会让'事件还在落库'的轮次丢收尾")
        self.assertLess(elapsed, 3.0, "宽限必须有上限，不能挂死流线程")


class ActiveRunInterlockTests(unittest.TestCase):
    def test_second_chat_run_rejected_with_active_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            conversation = storage.create_conversation()
            snapshot = {"model_key": "online:demo", "provider_id": "demo"}
            agent = {"id": "general", "name": "通用 Agent"}
            storage.create_chat_run(conversation["id"], "第一条", [], agent, snapshot, "craft")
            with self.assertRaisesRegex(RuntimeError, "ACTIVE_RUN"):
                storage.create_chat_run(conversation["id"], "第二条", [], agent, snapshot, "craft")


if __name__ == "__main__":
    unittest.main()
