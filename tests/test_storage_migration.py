# -*- coding: utf-8 -*-
"""护栏：存量历史数据压缩迁移 v14。

背景：run_events 90 万行中 87 万行是逐 token 推理 delta；done/cancelled 载荷
携带完整消息；终态 snapshot 固化完整会话消息（O(N²) 累积）。v14 迁移全部幂等：
- reasoning_delta 按「2048 字符 / 1s」窗口合流为整段 reasoning；
- done/cancelled 去掉 message / aborted_message；
- 终态 snapshot 去掉 conversation_messages（interrupted 保留）；
- 迁移前自动备份整库到 data/backups。
"""

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.storage import store as storage_module  # noqa: E402
from naiba.storage.store import CURRENT_SCHEMA_VERSION, ChatStorage  # noqa: E402


def _insert_delta_events(db, run_id: str, count: int, chars: int):
    now = int(time.time() * 1000)
    rows = []
    for i in range(count):
        rows.append((
            run_id, i + 1, "reasoning_delta",
            json.dumps({"type": "reasoning_delta", "content": "字" * chars}, ensure_ascii=False),
            now + i,
        ))
    db.executemany(
        "INSERT INTO run_events(run_id, sequence, event_type, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    db.commit()


def _reasoning_text_total(db, run_id: str) -> int:
    total = 0
    for (payload,) in db.execute(
        "SELECT payload FROM run_events WHERE event_type='reasoning' AND run_id=?", (run_id,)
    ):
        total += len(json.loads(payload).get("content") or "")
    return total


class MigrationV14Tests(unittest.TestCase):
    def _make_v13_like_db(self, tmp: Path) -> ChatStorage:
        """用当前 schema 建库后把 user_version 降回 13，模拟待迁移的旧库。"""
        storage = ChatStorage(tmp / "chat.db")
        storage.set_user_version(13)
        return storage

    def test_fresh_db_reaches_current_version_without_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            self.assertEqual(storage.get_user_version(), CURRENT_SCHEMA_VERSION)
            self.assertFalse((Path(tmp) / "backups").exists(), "新库不应产生备份")

    def test_v13_data_migrated_and_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = self._make_v13_like_db(Path(tmp))
            convo = storage.create_conversation()
            agent = {"id": "general", "name": "通用 Agent"}
            done_run = storage.create_chat_run(
                convo["id"], "消息1", [], agent, {"model_key": "online:demo"}, "craft"
            )
            # 终态化（ACTIVE 集合释放，才能创建下一个 run）
            storage.update_background_task(done_run["id"], status="completed", finished=True)
            cancelled_run = storage.create_chat_run(
                convo["id"], "消息2", [], agent, {"model_key": "online:demo"}, "craft"
            )
            storage.update_background_task(cancelled_run["id"], status="failed", finished=True)
            interrupted_run = storage.create_chat_run(
                convo["id"], "消息3", [], agent, {"model_key": "online:demo"}, "craft"
            )
            # interrupted 始终保留（恢复重建需要）
            storage.update_background_task(interrupted_run["id"], status="interrupted", finished=True)

            # 造大量推理 delta + done/cancelled 全量消息事件。
            # 注意：`create_chat_run` 已不再把整段会话固化进快照（新契约），而 v14 迁移的
            # "终态收缩 conversation_messages"是**存量数据**保护——所以这里必须显式把存量
            # 形态写回去，否则第 3 段断言会退化成"测了个本来就没有的键"。
            with closing(sqlite3.connect(Path(tmp) / "chat.db")) as db:
                for target in (done_run["id"], interrupted_run["id"]):
                    snapshot = json.loads(db.execute(
                        "SELECT snapshot FROM background_tasks WHERE id=?", (target,)
                    ).fetchone()[0] or "{}")
                    snapshot["conversation_messages"] = [
                        {"id": "m1", "role": "user", "content": "存量历史", "metadata": {}},
                    ]
                    db.execute(
                        "UPDATE background_tasks SET snapshot=? WHERE id=?",
                        (json.dumps(snapshot, ensure_ascii=False), target),
                    )
                db.commit()
            with closing(sqlite3.connect(Path(tmp) / "chat.db")) as db:
                _insert_delta_events(db, interrupted_run["id"], 5000, 20)  # 100000 字符
                for run_id in (done_run["id"], cancelled_run["id"]):
                    payload = json.dumps(
                        {"type": "done" if run_id == done_run["id"] else "cancelled",
                         "message": {"content": "x" * 5000, "metadata": {"trace": ["y" * 2000]}},
                         "aborted_message": {"content": "z" * 1000}},
                        ensure_ascii=False,
                    )
                    db.execute(
                        "INSERT INTO run_events(run_id, sequence, event_type, payload, created_at) "
                        "VALUES (?, 1, ?, ?, 1)", (run_id, "done" if run_id == done_run["id"] else "cancelled", payload),
                    )
                    db.execute(
                        "INSERT INTO run_events(run_id, sequence, event_type, payload, created_at) "
                        "VALUES (?, 2, 'run_completed', '{\"type\":\"run_completed\",\"run_id\":\"x\"}', 1)",
                        (run_id,),
                    )
                db.commit()

            # 重新打开：触发 v14 迁移（其后 v15 收藏列为纯增量，不影响压缩结果）
            reopened = ChatStorage(Path(tmp) / "chat.db")
            self.assertEqual(reopened.get_user_version(), CURRENT_SCHEMA_VERSION)
            with closing(sqlite3.connect(Path(tmp) / "chat.db")) as db:
                # 1) 推理合流
                deltas = db.execute(
                    "SELECT COUNT(*) FROM run_events WHERE event_type='reasoning_delta'"
                ).fetchone()[0]
                self.assertEqual(deltas, 0, "delta 事件应全部合流")
                merged = db.execute(
                    "SELECT COUNT(*) FROM run_events WHERE event_type='reasoning'"
                ).fetchone()[0]
                self.assertGreaterEqual(merged, 48)  # 100000/2048 ≈ 49 段
                self.assertEqual(
                    _reasoning_text_total(db, interrupted_run["id"]), 100000,
                    "合流不应丢文本",
                )
                # 2) done/cancelled 瘦身
                for event_type in ("done", "cancelled"):
                    payload = db.execute(
                        "SELECT payload FROM run_events WHERE event_type=? AND sequence=1",
                        (event_type,),
                    ).fetchone()[0]
                    obj = json.loads(payload)
                    self.assertNotIn("message", obj)
                    self.assertNotIn("aborted_message", obj)
                # 3) snapshot 收缩
                snap = db.execute(
                    "SELECT snapshot FROM background_tasks WHERE id=?", (done_run["id"],)
                ).fetchone()[0]
                self.assertNotIn("conversation_messages", json.loads(snap))
                snap = db.execute(
                    "SELECT snapshot FROM background_tasks WHERE id=?", (interrupted_run["id"],)
                ).fetchone()[0]
                self.assertIn("conversation_messages", json.loads(snap))
                ok = db.execute("PRAGMA integrity_check").fetchone()[0]
                self.assertEqual(ok, "ok")
            # 备份存在
            backup_dir = Path(tmp) / "backups"
            self.assertTrue(backup_dir.exists(), "迁移前应自动备份")
            self.assertTrue(list(backup_dir.glob("chat.db*")), "备份文件应为 chat.db (+wal/shm)")

    def test_migration_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = self._make_v13_like_db(Path(tmp))
            convo = storage.create_conversation()
            agent = {"id": "general", "name": "通用 Agent"}
            run = storage.create_chat_run(
                convo["id"], "消息", [], agent, {"model_key": "online:demo"}, "craft"
            )
            storage.update_background_task(run["id"], status="completed", finished=True)
            with closing(sqlite3.connect(Path(tmp) / "chat.db")) as db:
                _insert_delta_events(db, run["id"], 300, 10)
            storage.apply_pending_migrations()  # 手动重跑：无 delta 行、无重复段
            with closing(sqlite3.connect(Path(tmp) / "chat.db")) as db:
                deltas = db.execute(
                    "SELECT COUNT(*) FROM run_events WHERE event_type='reasoning_delta'"
                ).fetchone()[0]
                self.assertEqual(deltas, 0)
                # 合流后文本总量不变
                self.assertEqual(_reasoning_text_total(db, run["id"]), 3000)

    def test_coalesce_function_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            convo = storage.create_conversation()
            agent = {"id": "general", "name": "通用 Agent"}
            run = storage.create_chat_run(
                convo["id"], "消息", [], agent, {"model_key": "online:demo"}, "craft"
            )
            with closing(sqlite3.connect(Path(tmp) / "chat.db")) as db:
                _insert_delta_events(db, run["id"], 10, 5)  # 50 字符 < 2048 -> 1 段
                processed = storage_module._coalesce_reasoning_deltas(db)
                self.assertEqual(processed, 10)
                again = storage_module._coalesce_reasoning_deltas(db)
                self.assertEqual(again, 0, "幂等：第二次无 delta 行")
                row = db.execute(
                    "SELECT payload, sequence FROM run_events "
                    "WHERE event_type='reasoning' AND run_id=?",
                    (run["id"],),
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(json.loads(row[0])["content"], "字" * 50)
                self.assertEqual(row[1], 1, "合流行使用段首 sequence")

    def test_vacuum_failure_does_not_break_migration(self):
        """窗口版 stderr=None 时 VACUUM 失败不得让迁移抛异常（发布前防御）。"""

        class FakeDb:
            """只读包装真实连接：VACUUM 一律失败，其余转发（sqlite3.Connection.execute 只读不可 patch）。"""

            def __init__(self, real):
                self._real = real

            def execute(self, sql, *args, **kwargs):
                if "VACUUM" in str(sql):
                    raise sqlite3.OperationalError("disk full (simulated)")
                return self._real.execute(sql, *args, **kwargs)

            def commit(self):
                self._real.commit()

        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            convo = storage.create_conversation()
            agent = {"id": "general", "name": "通用 Agent"}
            run = storage.create_chat_run(
                convo["id"], "消息", [], agent, {"model_key": "online:demo"}, "craft"
            )
            storage.update_background_task(run["id"], status="completed", finished=True)
            with closing(sqlite3.connect(Path(tmp) / "chat.db")) as db:
                _insert_delta_events(db, run["id"], 100, 5)
                storage_module._migrate_to_v14(FakeDb(db))  # 不应抛异常
                left = db.execute(
                    "SELECT COUNT(*) FROM run_events WHERE event_type='reasoning_delta'"
                ).fetchone()[0]
                self.assertEqual(left, 0, "内容迁移应在 VACUUM 失败时仍完成")
            # 警告已落到数据目录文件
            warning_log = Path(tmp) / "data-migration-warning.log"
            self.assertTrue(warning_log.exists(), "VACUUM 失败应写入警告文件")
            self.assertIn("VACUUM 未执行", warning_log.read_text(encoding="utf-8"))


class FirstTurnSlimMigrationTests(unittest.TestCase):
    """v22：清掉 first_turn 里只写不读的 `full_messages`（会话列 + run 快照两处）。"""

    def _seed(self, tmp: Path) -> ChatStorage:
        storage = ChatStorage(tmp / "chat.db")
        convo = storage.create_conversation()
        agent = {"id": "general", "name": "通用 Agent"}
        run = storage.create_chat_run(
            convo["id"], "消息", [], agent, {"model_key": "online:demo"}, "craft"
        )
        payload = {
            "system": "系统提示词",
            "tools": [{"name": "read_file"}],
            "full_messages": [{"role": "system", "content": "x" * 4096}],
        }
        storage.set_conversation_first_turn(convo["id"], payload)
        storage.update_run_snapshot(run["id"], {"first_turn": dict(payload)})
        # 回到 v21 再迁移：模拟存量库
        storage.set_user_version(21)
        return storage

    def test_full_messages_removed_from_both_places(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = self._seed(root)
            storage.apply_pending_migrations()
            with closing(sqlite3.connect(root / "chat.db")) as db:
                raw = db.execute(
                    "SELECT first_turn FROM conversations"
                ).fetchone()[0]
                payload = json.loads(raw)
                self.assertNotIn("full_messages", payload, "会话列必须清掉 full_messages")
                self.assertEqual(payload["system"], "系统提示词", "其余键原样保留")
                snapshot = db.execute(
                    "SELECT snapshot FROM background_tasks"
                ).fetchone()[0]
                self.assertNotIn(
                    "full_messages", json.loads(snapshot)["first_turn"],
                    "run 快照（老会话兜底读源）同样要清",
                )

    def test_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = self._seed(root)
            storage.apply_pending_migrations()
            storage.apply_pending_migrations()  # 二次重跑：不得报错、不得改坏
            with closing(sqlite3.connect(root / "chat.db")) as db:
                payload = json.loads(db.execute("SELECT first_turn FROM conversations").fetchone()[0])
                self.assertNotIn("full_messages", payload)
                self.assertEqual(payload["tools"], [{"name": "read_file"}])

    def test_broken_json_row_is_skipped_not_fatal(self) -> None:
        """坏 JSON 行不得让整个迁移失败（否则用户卡在启动迁移上）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = self._seed(root)
            storage.set_user_version(21)
            with closing(sqlite3.connect(root / "chat.db")) as db:
                db.execute(
                    "UPDATE conversations SET first_turn = ?", ("{不是 JSON",)
                )
                db.commit()
            storage.apply_pending_migrations()  # 不应抛异常
            self.assertEqual(storage.get_user_version(), CURRENT_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
