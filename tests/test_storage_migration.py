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


class TerminalEventSlimMigrationTests(unittest.TestCase):
    """v24：补齐终态事件重复副本的收缩口径。

    v14 只收了 done/cancelled 的 `message`/`aborted_message`，两条口子一直漏着：
    ① `error.partial_message` 压根不在口径内（失败事件永久带着整条 partial 消息）；
    ② v14 之后、运行期瘦身之前落库的 done 事件仍是带整份消息的形态（实测 201 行 / 36MB）。
    这些副本在终态后都没有读取方——前端收尾时重载会话拿完整消息。
    """

    def _seed(self, tmp: Path) -> tuple[ChatStorage, str, str]:
        storage = ChatStorage(tmp / "chat.db")
        convo = storage.create_conversation()
        agent = {"id": "general", "name": "通用 Agent"}
        run = storage.create_chat_run(
            convo["id"], "消息", [], agent, {"model_key": "online:demo"}, "craft"
        )
        storage.update_background_task(run["id"], status="completed", finished=True)
        # 直接落"存量形态"事件：新契约的发射点已经不带这些键了。
        storage.append_run_event(run["id"], {
            "type": "done",
            "message": {
                "id": "m1", "role": "assistant", "content": "答复",
                "metadata": {"trace": [{"role": "user", "content": "x" * 256}]},
            },
        })
        storage.append_run_event(run["id"], {
            "type": "cancelled", "message": "任务已取消",
            "aborted_message": {"id": "m2", "role": "assistant", "content": "（已中止）"},
        })
        storage.append_run_event(run["id"], {
            "type": "error", "message": "HTTP 400",
            "partial_message": {"id": "m3", "role": "assistant", "content": "半截"},
        })
        storage.set_user_version(23)  # 回到 v23 再迁移：模拟存量库
        return storage, str(run["id"]), str(convo["id"])

    def _payloads(self, root: Path, run_id: str) -> dict[str, dict]:
        with closing(sqlite3.connect(root / "chat.db")) as db:
            return {
                str(row[0]): json.loads(row[1] or "{}")
                for row in db.execute(
                    "SELECT event_type, payload FROM run_events WHERE run_id = ?", (run_id,)
                )
            }

    def test_all_terminal_message_copies_are_slimmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage, run_id, conversation_id = self._seed(root)
            storage.apply_pending_migrations()
            payloads = self._payloads(root, run_id)
            self.assertNotIn("message", payloads["done"], "done 的完整消息副本必须清掉")
            self.assertNotIn("aborted_message", payloads["cancelled"])
            self.assertNotIn("partial_message", payloads["error"], "error 那份是 v14 漏掉的")
            self.assertNotIn("message", payloads["cancelled"], "cancelled 文案由 type 承担")
            self.assertEqual(payloads["error"]["message"], "HTTP 400",
                             "error 的 message 是错误原因短文案：必须保留（重放也要能显示）")
            self.assertEqual(payloads["done"]["type"], "done", "事件类型保留（前端按它收尾）")
            # 只动事件副本：消息本体（含 hydrate 回来的 trace）必须完好。
            messages = storage.get_conversation(conversation_id)["messages"]
            self.assertTrue(messages, "迁移只动事件载荷，不碰 messages 表")
            self.assertEqual(storage.get_user_version(), CURRENT_SCHEMA_VERSION)

    def test_v24_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage, run_id, _ = self._seed(root)
            storage.apply_pending_migrations()
            storage.apply_pending_migrations()  # 二次重跑：不得报错、不得改坏
            payloads = self._payloads(root, run_id)
            self.assertEqual(payloads["done"].get("message"), None)
            self.assertEqual(payloads["error"].get("partial_message"), None)
            self.assertEqual(payloads["error"]["message"], "HTTP 400")


class ShowMediaToolMigrationTests(unittest.TestCase):
    """v27：给内置 Agent 的**既有主会话**固化工具集追加一件 `show_media`。

    背景（计划 2026-10-20 §5.2）：会话的 `enabled_tool_ids` 在首条消息时固化、之后不可改。
    新出厂的「指名出卡」若不迁移，老用户升级后在这条会话里永远用不上。

    边界刻意收得很紧（本组要钉的东西）：
    - 只动 `conv_kind='main'` 且 `agent_id` 属于 6 个内置 Agent 的会话；
    - `enabled_tool_ids` 必须能解析成**非空**数组（空/损坏原样跳过）；
    - **只追加 show_media 一件，绝不补后台工具**（旧会话能力不变）；
    - 保留其余工具与顺序；重复迁移稳定。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.storage = ChatStorage(self.root / "chat.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _tools(self, conversation_id: str) -> list[str]:
        with closing(sqlite3.connect(self.root / "chat.db")) as db:
            raw = db.execute(
                "SELECT enabled_tool_ids FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()[0]
        return json.loads(raw or "[]")

    def test_version_and_id_constant(self) -> None:
        self.assertEqual(CURRENT_SCHEMA_VERSION, 28)
        from naiba.config import built_in_agent_ids
        from naiba.storage.store import _BUILTIN_AGENT_IDS

        self.assertEqual(set(_BUILTIN_AGENT_IDS), built_in_agent_ids(),
                         "存储层冻结的内置 id 必须与配置层同源（改名漏改即静默不迁移）")

    def test_built_in_main_conversations_get_show_media(self) -> None:
        from naiba.storage.store import _BUILTIN_AGENT_IDS

        base = ["read_file", "pwsh"]
        expected = {}
        for agent_id in _BUILTIN_AGENT_IDS:
            conv = self.storage.create_conversation(agent_id=agent_id)
            self.storage.set_enabled_tool_ids(conv["id"], list(base))
            expected[conv["id"]] = agent_id
        self.storage.set_user_version(26)
        self.storage.apply_pending_migrations()
        for conversation_id, agent_id in expected.items():
            with self.subTest(agent=agent_id):
                self.assertEqual(self._tools(conversation_id), [*base, "show_media"],
                                 "只追加一件，其余工具与顺序不变")

    def test_aux_plan_and_custom_agents_are_untouched(self) -> None:
        base = ["read_file"]
        aux = self.storage.create_conversation(conv_kind="aux", agent_id="master")
        custom = self.storage.create_conversation(agent_id="agent_0123456789ab")
        empty = self.storage.create_conversation(agent_id="coder")
        for conversation_id in (aux["id"], custom["id"]):
            self.storage.set_enabled_tool_ids(conversation_id, list(base))
        self.storage.set_user_version(26)
        self.storage.apply_pending_migrations()
        self.assertEqual(self._tools(aux["id"]), base, "辅助对话的工具集恒被只读拓扑收口，不得扩权")
        self.assertEqual(self._tools(custom["id"]), base, "自定义 Agent 是用户资产，升级不得扩权")
        self.assertEqual(self._tools(empty["id"]), [], "空固化集语义是「未固化」，不得凭空写入")

    def test_broken_json_row_is_skipped_not_fatal(self) -> None:
        conv = self.storage.create_conversation(agent_id="master")
        with closing(sqlite3.connect(self.root / "chat.db")) as db:
            db.execute(
                "UPDATE conversations SET enabled_tool_ids = ? WHERE id = ?",
                ("{不是 JSON", conv["id"]),
            )
            db.commit()
        self.storage.set_user_version(26)
        self.storage.apply_pending_migrations()  # 不应抛异常
        self.assertEqual(self.storage.get_user_version(), CURRENT_SCHEMA_VERSION)
        with closing(sqlite3.connect(self.root / "chat.db")) as db:
            raw = db.execute(
                "SELECT enabled_tool_ids FROM conversations WHERE id = ?", (conv["id"],)
            ).fetchone()[0]
        self.assertEqual(raw, "{不是 JSON", "损坏 JSON 原样保留，不猜也不覆盖")

    def test_migration_never_adds_background_tools(self) -> None:
        """旧会话**只多 show_media**：后台工具是对已固化快照的追溯扩权，刻意不做。"""
        conv = self.storage.create_conversation(agent_id="master")
        self.storage.set_enabled_tool_ids(conv["id"], ["read_file"])
        self.storage.set_user_version(26)
        self.storage.apply_pending_migrations()
        tools = set(self._tools(conv["id"]))
        self.assertIn("show_media", tools)
        self.assertEqual(
            tools - {"read_file", "show_media"}, set(),
            "迁移不得顺带补后台工具（旧会话能力保持不变）",
        )

    def test_v27_is_idempotent(self) -> None:
        from naiba.storage.store import _migrate_to_v27

        conv = self.storage.create_conversation(agent_id="master")
        self.storage.set_enabled_tool_ids(conv["id"], ["read_file"])
        with closing(sqlite3.connect(self.root / "chat.db")) as db:
            _migrate_to_v27(db)
            _migrate_to_v27(db)  # 二次重跑：不得重复追加
        self.assertEqual(self._tools(conv["id"]), ["read_file", "show_media"])


class DetachedJobMigrationTests(unittest.TestCase):
    """v28：background_tasks 加 ``detached`` / ``origin_run_id``（分离式 Job 的显式标记）。

    为什么需要这两列（计划 2026-10-22 §4.3）：现有「顶层 Run」的判据是 parent_job_id 为空，
    一条空父 ID 的活动记录会占住会话互斥位、进入 list_primary、被聊天收尾等待。逐帧抽取
    Job 必须与主聊天并行，所以需要一枚独立的显式标记，不能复用"空父 ID"表达。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _columns(self, db_path: Path) -> dict[str, tuple[str, str]]:
        with closing(sqlite3.connect(db_path)) as db:
            rows = db.execute("PRAGMA table_info(background_tasks)").fetchall()
        return {str(row[1]): (str(row[2]), str(row[4])) for row in rows}

    def test_new_database_has_columns_with_defaults(self) -> None:
        db_path = self.root / "chat.db"
        self.storage = ChatStorage(db_path)
        self.assertEqual(self.storage.get_user_version(), CURRENT_SCHEMA_VERSION)
        columns = self._columns(db_path)
        self.assertIn("detached", columns)
        self.assertIn("origin_run_id", columns)
        self.assertIn("0", columns["detached"][1], "detached 必须默认 0（存量行为不变）")
        self.assertIn("''", columns["origin_run_id"][1], "origin_run_id 必须默认空串")

    def test_upgrade_from_v27_adds_columns_and_keeps_rows(self) -> None:
        from naiba.storage.store import ChatStorage as Store

        db_path = self.root / "chat.db"
        storage = Store(db_path)
        conv = storage.create_conversation(agent_id="master")
        job = storage.create_run(
            conv["id"], "旧任务", {"id": "master", "name": "Job", "system_prompt": "", "skill_ids": []},
            {}, kind="shell",
        )
        # 模拟 v27 库：把 user_version 回退，迁移会重新走 v28（ALTER 前先查列，幂等）。
        with closing(sqlite3.connect(db_path)) as db:
            db.execute("ALTER TABLE background_tasks DROP COLUMN detached")
            db.execute("ALTER TABLE background_tasks DROP COLUMN origin_run_id")
            db.commit()
        storage.set_user_version(27)
        storage.apply_pending_migrations()
        self.assertEqual(storage.get_user_version(), CURRENT_SCHEMA_VERSION)
        columns = self._columns(db_path)
        self.assertIn("detached", columns)
        self.assertIn("origin_run_id", columns)
        row = storage.get_background_task(str(job["id"]))
        self.assertEqual(row["detached"], False, "存量行读出来必须是 False（不是 None/0）")
        self.assertEqual(row["origin_run_id"], "")

    def test_detached_run_does_not_occupy_active_slot(self) -> None:
        db_path = self.root / "chat.db"
        storage = ChatStorage(db_path)
        conv = storage.create_conversation(agent_id="master")
        agent = {"id": "master", "name": "Job", "system_prompt": "", "skill_ids": []}
        blocking = storage.create_run(conv["id"], "普通 Run", agent, {}, kind="chat")
        # detached 与主聊天并行：不占顶层互斥位，active_run 也看不到它。
        detached = storage.create_run(
            conv["id"], "逐帧", agent, {}, kind="video_frames", detached=True, origin_run_id=str(blocking["id"]),
        )
        self.assertEqual(str(storage.active_run(conv["id"])["id"]), str(blocking["id"]),
                         "active_run 仍指向普通 Run（detached 不参与互斥）")
        row = storage.get_background_task(str(detached["id"]))
        self.assertTrue(row["detached"])
        self.assertEqual(row["origin_run_id"], str(blocking["id"]))


if __name__ == "__main__":
    unittest.main()
