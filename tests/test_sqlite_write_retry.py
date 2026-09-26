# -*- coding: utf-8 -*-
"""守门：storage 写路径必须扛住瞬时 SQLite 故障（WAL 下多连接竞态）。

背景（2.9.2-beta 定位的真实缺陷）：WAL 下最后一个连接关闭时会 checkpoint 并删除
``-shm``/``-wal``，此刻另一个刚打开的连接写库报
``attempt to write a readonly database``（``SQLITE_READONLY_CANTINIT``）。它不是
「库真的只读」，重连即成功；但 ``jobs._emit`` 的容错会把它吞成**静默丢一行事件**
——实测任务面板偶发少一行过程日志（22 次循环复现 7 次异常、1 次断言失败）。

2026-09-25 补齐（拍教程截图时暴露的**覆盖面**缺陷）：上一轮修复只把
``append_run_event`` / ``update_job`` 两处包上重试，其余 30+ 条写路径全裸奔。
实测 ``update_run_snapshot`` 撞上同一个瞬时故障，**整个回合被打死**，用户看到的是
「请求失败：attempt to write a readonly database」。结论是：写路径的重试覆盖必须是
「全部」，不能是「记得的那几处」——故本文件既守行为，也守**结构**（见第 6 节）。

守门口径（全部确定性，不依赖 flaky 复现）：
1. 瞬时故障重试后必须成功，且**不得写出重复事件 / 不得断号**；
2. 非瞬时错误（``no such table``）与业务错误（``LookupError``）必须原样抛出；
3. 重试次数用尽必须抛出，不得静默返回「假成功」；
4. 判据只认已知瞬时形态，别的错误一个都不许被当成可重试；
5. 每条写路径（自开连接 + 写 SQL）都必须包重试，例外必须显式登记且给出理由。
"""

import ast
import re
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

STORE_PATH = Path(__file__).resolve().parents[1] / "naiba" / "storage" / "store.py"

# 允许「写库但不重试」的方法：必须是**有理由**的显式例外。
# 新增写路径时，守门用例会红；要么补 @_retry_transient_write，要么在这里登记理由。
#
# 不在扫描面内的写库 helper（无需登记，也**不该**登记）：
# `_slim_run_snapshot(db, task_id)` 复用调用方已经打开的事务，自己不 `_connect`
# ⇒ 由唯一调用方 `update_background_task`（已包重试）兜住。扫描器要求「自开连接 +
# 写 SQL」两者同时成立，正是为了把它这类 helper 排除在外。
NON_RETRY_WRITE_PATHS: dict[str, str] = {
    "_initialize": (
        "构造期 schema 引导：方法体里的 `executescript` 逐句自动提交，整个方法**不是**"
        "单一事务，不满足装饰器前提（整方法重试可能重复写入「服务重启中断」事件）。"
        "它失败即「库根本建不起来」，必须原样暴露给启动方，不该被重试掩盖。"
    ),
}

# 写 SQL 的开头关键字（含 PRAGMA 赋值形态；只读的 `PRAGMA user_version` 不带 = 不会命中）。
# `\b` 不能省：没有它 'created_at' / 'updated_at' 这种**列名字面量**会被当成 CREATE/UPDATE，
# 把一堆只读查询误判成写路径（实测误报 7 条）。
_WRITE_SQL = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|VACUUM)\b|^\s*PRAGMA\s+\w+\s*=",
    re.IGNORECASE,
)
# 自身不写 SQL、但被调用即代表写库的模块级 helper。
_WRITE_HELPERS = {"_coalesce_reasoning_deltas"}


def _literal_texts(node: ast.AST) -> list[str]:
    """方法体里所有字符串字面量（含 f-string 的字面量片段）。"""
    texts: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            texts.append(sub.value)
        elif isinstance(sub, ast.JoinedStr):
            texts.append(
                "".join(
                    value.value
                    for value in sub.values
                    if isinstance(value, ast.Constant) and isinstance(value.value, str)
                )
            )
    return texts


def _opens_own_connection(node: ast.AST) -> bool:
    return any(
        isinstance(sub, ast.Attribute) and sub.attr == "_connect" for sub in ast.walk(node)
    )


def _performs_write(node: ast.AST) -> bool:
    if any(_WRITE_SQL.match(text) for text in _literal_texts(node)):
        return True
    return any(
        isinstance(sub, ast.Name) and sub.id in _WRITE_HELPERS for sub in ast.walk(node)
    )


def _is_retry_wrapped(node: ast.AST) -> bool:
    """两种合格写法：装饰器 `@_retry_transient_write`，或方法体内 `self._write_with_retry(...)`。"""
    for decorator in getattr(node, "decorator_list", []):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name) and target.id == "_retry_transient_write":
            return True
        if isinstance(target, ast.Attribute) and target.attr == "_retry_transient_write":
            return True
    return any(
        isinstance(sub, ast.Attribute) and sub.attr == "_write_with_retry"
        for sub in ast.walk(node)
    )


def _scan_write_paths() -> dict[str, bool]:
    """扫描 ChatStorage：{方法名: 是否已包重试}，只含「自开连接 + 写库」的方法。"""
    tree = ast.parse(STORE_PATH.read_text(encoding="utf-8"))
    storage = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ChatStorage"
    )
    found: dict[str, bool] = {}
    for item in storage.body:
        if not isinstance(item, ast.FunctionDef):
            continue
        if not (_opens_own_connection(item) and _performs_write(item)):
            continue
        found[item.name] = _is_retry_wrapped(item)
    return found


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

    # ---- 6. 覆盖面：每条写路径都必须包重试（上一轮修复只包了 2 处的回归护栏） ----

    def test_every_write_path_is_retry_wrapped_or_explicitly_justified(self) -> None:
        """结构守门：新增写路径必须自带重试，否则这里直接红。

        这条用例的价值在于**不依赖任何人记得**：只要 store.py 里多出一条
        「自开连接 + 写库」的方法而没包重试，扫描器就会发现它。
        """
        paths = _scan_write_paths()
        self.assertGreaterEqual(
            len(paths), 40,
            f"只扫到 {len(paths)} 条写路径，扫描器可能已失效（关键字/结构变了）：{sorted(paths)}",
        )
        offenders = sorted(
            name
            for name, wrapped in paths.items()
            if not wrapped and name not in NON_RETRY_WRITE_PATHS
        )
        self.assertEqual(
            offenders, [],
            "这些写路径没包瞬时故障重试——补 @_retry_transient_write，"
            f"或在 NON_RETRY_WRITE_PATHS 里登记理由：{offenders}",
        )

    def test_non_retry_write_paths_are_justified_and_minimal(self) -> None:
        """例外表必须「少、且每条都有实质理由」：防止有人图省事往这里堆方法。"""
        self.assertEqual(
            sorted(NON_RETRY_WRITE_PATHS), ["_initialize"],
            "例外表只能放真正不满足装饰器前提的方法；能包重试的一律包",
        )
        paths = _scan_write_paths()
        for name, reason in NON_RETRY_WRITE_PATHS.items():
            with self.subTest(name=name):
                self.assertIn(name, paths, f"{name} 已不再是写路径，请从例外表删除")
                self.assertGreater(len(reason.strip()), 40, f"{name} 的理由太短，等于没写")

    def test_scanner_actually_detects_known_write_paths(self) -> None:
        """扫描器自检：把已知必须命中的方法逐个点名，防止「扫描器静默返回空集」。"""
        paths = _scan_write_paths()
        for name in (
            "append_run_event",
            "update_job",
            "create_run",
            "create_chat_run",
            "update_run_snapshot",
            "add_message",
            "create_conversation",
            "delete_conversation",
            "truncate_from_message",
            "update_background_task",
            "create_plan",
            "update_plan",
            "apply_pending_migrations",
            "compress_run_events",
            "_initialize",
        ):
            with self.subTest(name=name):
                self.assertIn(name, paths, f"{name} 应被识别为写路径")
                if name not in NON_RETRY_WRITE_PATHS:
                    self.assertTrue(paths[name], f"{name} 应已包重试")

    # ---- 7. 新补齐的写路径：行为回归（重试到成功 + 副作用只发生一次） ----

    def test_create_conversation_retries_transient_readonly(self) -> None:
        fake = self._flaky(2)
        conversation = self.storage.create_conversation(title="重试会话")
        calls = fake.calls
        self.assertEqual(calls, 4, "两次瞬时故障 + 一次成功（写 + 回读）")
        self.assertEqual(conversation["title"], "重试会话")
        titled = [c for c in self.storage.list_conversations() if c["title"] == "重试会话"]
        self.assertEqual(len(titled), 1, "重试不得插出第二条会话")

    def test_add_message_retries_transient_readonly(self) -> None:
        fake = self._flaky(2)
        message = self.storage.add_message(self.conversation["id"], "user", "只写一次")
        calls = fake.calls
        self.assertEqual(calls, 3, "两次瞬时故障 + 一次成功写入")
        self.assertTrue(message.get("id"))
        self.assertEqual(
            self.storage.message_count(self.conversation["id"]), 1,
            "重试不得写出重复消息",
        )

    def test_update_run_snapshot_retries_transient_readonly(self) -> None:
        """真实缺陷回归：这条写路径的瞬时故障曾把整个回合打成「请求失败：attempt to write a readonly database」。"""
        fake = self._flaky(2)
        merged = self.storage.update_run_snapshot(self.run_id, {"first_turn": {"question": "你好"}})
        calls = fake.calls
        self.assertEqual(calls, 4, "两次瞬时故障 + 一次成功（读-合并 + 写）")
        self.assertEqual(merged.get("first_turn"), {"question": "你好"})
        self.assertEqual(
            (self.storage.get_run_snapshot(self.run_id) or {}).get("first_turn"),
            {"question": "你好"},
            "重试后快照必须真的落库",
        )

    def test_create_run_retries_transient_readonly(self) -> None:
        conversation = self.storage.create_conversation(title="重试 run")
        fake = self._flaky(2)
        run = self.storage.create_run(
            conversation["id"], "你好", {"id": "master", "name": "全能 Agent"}, {}, kind="chat"
        )
        calls = fake.calls
        self.assertEqual(calls, 4, "两次瞬时故障 + 一次成功（写 + 回读）")
        self.assertTrue(run.get("id"))
        self.assertEqual(
            (self.storage.get_background_task(run["id"]) or {}).get("status"), "queued"
        )

    def test_update_plan_retries_transient_readonly(self) -> None:
        plan = self.storage.create_plan(self.conversation["id"], "做一张封面")
        fake = self._flaky(2)
        updated = self.storage.update_plan(plan["id"], title="封面计划", status="ready")
        calls = fake.calls
        self.assertEqual(calls, 4, "两次瞬时故障 + 一次成功（写 + 回读）")
        self.assertEqual(updated.get("title"), "封面计划")
        self.assertEqual((self.storage.get_plan(plan["id"]) or {}).get("status"), "ready")

    def test_clear_terminal_background_tasks_retries_transient_readonly(self) -> None:
        self.storage.update_job(self.run_id, status="cancelled", finished=True)
        fake = self._flaky(2)
        deleted = self.storage.clear_terminal_background_tasks()
        calls = fake.calls
        self.assertEqual(calls, 3, "两次瞬时故障 + 一次成功清理")
        self.assertEqual(deleted, 1)
        self.assertEqual(self.storage.list_background_tasks(), [])

    def test_compress_run_events_retries_transient_readonly(self) -> None:
        fake = self._flaky(2)
        self.assertEqual(self.storage.compress_run_events(self.run_id), 0)
        self.assertEqual(fake.calls, 3, "两次瞬时故障 + 一次成功合流")

    def test_business_error_in_newly_wrapped_path_is_not_retried(self) -> None:
        """新包的写路径同样只重试瞬时故障：业务错误一次都不许重试。"""
        fake = self._flaky(0)
        with self.assertRaises(LookupError):
            self.storage.delete_message(self.conversation["id"], "not-a-message-id")
        self.assertEqual(fake.calls, 1)


if __name__ == "__main__":
    unittest.main()
