# -*- coding: utf-8 -*-
"""守门：storage 写路径必须扛住瞬时 SQLite 故障（WAL 下多连接竞态）。

背景（2.9.2-beta 定位的真实缺陷）：WAL 下最后一个连接关闭时会 checkpoint 并删除
``-shm``/``-wal``，此刻另一个刚打开的连接写库报
``attempt to write a readonly database``（``SQLITE_READONLY_CANTINIT``）。它不是
「库真的只读」，重连即成功；但 ``jobs._emit`` 的容错会把它吞成**静默丢一行事件**
——实测任务面板偶发少一行过程日志（22 次循环复现 7 次异常、1 次断言失败）。

守门口径（全部确定性，不依赖 flaky 复现）：
1. 瞬时故障重试后必须成功，且**不得写出重复事件 / 不得断号**；
2. 非瞬时错误（``no such table``）与业务错误（``LookupError``）必须原样抛出；
3. 重试次数用尽必须抛出，不得静默返回「假成功」；
4. 判据只认已知瞬时形态，别的错误一个都不许被当成可重试。
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.storage.store import (  # noqa: E402
    ChatStorage,
    _SQLITE_WRITE_ATTEMPTS,
    _is_transient_sqlite_error,
)

READONLY = "attempt to write a readonly database"


class _FlakyConnect:
    """把 ``_connect`` 换成「前 N 次抛故障，之后走真实实现」，并记录调用次数。"""

    def __init__(self, real, failures: int, message: str = READONLY) -> None:
        self._real = real
        self.failures = failures
        self.message = message
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise sqlite3.OperationalError(self.message)
        return self._real()


class SqliteWriteRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.conversation = self.storage.create_conversation()
        run = self.storage.create_run(
            self.conversation["id"], "job", {"id": "master", "name": "全能 Agent"}, {}, kind="subagent"
        )
        self.run_id = str(run["id"])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _flaky(self, failures: int, message: str = READONLY) -> _FlakyConnect:
        fake = _FlakyConnect(self.storage._connect, failures, message)
        patcher = mock.patch.object(self.storage, "_connect", new=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    # ---- 1. 瞬时故障必须重试到成功，且副作用只发生一次 ----

    def test_append_run_event_retries_transient_readonly(self) -> None:
        fake = self._flaky(2)
        event = self.storage.append_run_event(self.run_id, {"type": "job_log", "line": "第一步"})
        self.assertEqual(fake.calls, 3, "两次瞬时故障 + 一次成功")
        self.assertEqual(event["sequence"], 1)
        events = self.storage.list_run_events(self.run_id)
        self.assertEqual([item.get("line") for item in events], ["第一步"], "重试不得写出重复事件")

    def test_update_job_retries_transient_readonly(self) -> None:
        fake = self._flaky(1)
        job = self.storage.update_job(self.run_id, status="running", current_step="已启动")
        # 一次瞬时故障 + 一次成功写入 + 写后回读该行（get_background_task 也走 _connect）。
        self.assertEqual(fake.calls, 3)
        self.assertIsNotNone(job)
        self.assertEqual(job.get("status"), "running")
        self.assertEqual(job.get("current_step"), "已启动")

    def test_sequences_stay_gapless_under_retry(self) -> None:
        """重试后序号仍必须连续（sequence 在事务内重算，不重号、不断号）。"""
        fake = self._flaky(2)
        self.storage.append_run_event(self.run_id, {"type": "job_log", "line": "第一行"})
        self.storage.append_run_event(self.run_id, {"type": "job_log", "line": "第二行"})
        self.assertEqual(fake.calls, 4, "第一条重试两次后成功，第二条一次成功")
        events = self.storage.list_run_events(self.run_id)
        self.assertEqual([item["sequence"] for item in events], [1, 2])
        self.assertEqual([item.get("line") for item in events], ["第一行", "第二行"])

    # ---- 2. 非瞬时错误与业务错误必须原样抛出，且不得重试 ----

    def test_non_transient_error_is_not_retried(self) -> None:
        fake = self._flaky(99, message="no such table: run_events")
        with self.assertRaises(sqlite3.OperationalError):
            self.storage.append_run_event(self.run_id, {"type": "job_log", "line": "x"})
        self.assertEqual(fake.calls, 1, "非瞬时错误一次都不许重试")

    def test_business_error_is_not_retried(self) -> None:
        fake = self._flaky(0)
        with self.assertRaises(LookupError):
            self.storage.append_run_event("not-a-run-id", {"type": "job_log", "line": "x"})
        self.assertEqual(fake.calls, 1)

    # ---- 3. 重试耗尽必须抛出，不得静默假成功 ----

    def test_retry_exhausted_raises(self) -> None:
        fake = self._flaky(99)
        with self.assertRaises(sqlite3.OperationalError) as ctx:
            self.storage.update_job(self.run_id, status="running")
        self.assertIn("readonly", str(ctx.exception).lower())
        self.assertEqual(fake.calls, _SQLITE_WRITE_ATTEMPTS, "重试次数必须与常量一致")

    # ---- 4. 判据本身：只认已知瞬时形态 ----

    def test_transient_predicate_matches_only_known_markers(self) -> None:
        for message in (
            "attempt to write a readonly database",
            "database is locked",
            "database table is locked",
            "unable to open database file",
        ):
            with self.subTest(accepted=message):
                self.assertTrue(_is_transient_sqlite_error(sqlite3.OperationalError(message)))
        for exc in (
            sqlite3.OperationalError("no such table: run_events"),
            sqlite3.OperationalError("near \"SELEC\": syntax error"),
            sqlite3.IntegrityError("UNIQUE constraint failed"),
            LookupError("运行不存在"),
            ValueError("x"),
        ):
            with self.subTest(rejected=repr(exc)):
                self.assertFalse(_is_transient_sqlite_error(exc))

    # ---- 5. 回归护栏：重试包装不得改掉既有返回语义 ----

    def test_update_job_missing_row_still_returns_none(self) -> None:
        self.assertIsNone(self.storage.update_job("not-a-run-id", status="running"))

    def test_update_job_no_fields_returns_current_row(self) -> None:
        self.assertEqual(
            (self.storage.update_job(self.run_id) or {}).get("id"), self.run_id,
            "无字段更新应原样返回当前行（不因重试包装变成 None）",
        )


if __name__ == "__main__":
    unittest.main()
