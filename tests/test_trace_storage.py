# -*- coding: utf-8 -*-
"""trace 内容寻址存储守门（v23）：同一份 trace blob 全库只存一份，读回契约不变。

冻结的不变量：
1. **写入**：`add_message` 的 trace 不留在 `messages.metadata` 里，而是进
   `message_traces(trace_hash, data)`；消息行只留 `trace_hash`。
2. **去重**：内容相同的 trace 无论来自几条消息，表里只有一行。
3. **读回零改动**：`get_conversation` / `create_chat_run` 快照 / `build_model_history`
   拿到的仍是 `metadata["trace"]` 原文（逐字节等价）。
4. **路径引用扇描**：trace 里的宿主缓存路径必须能被 `referenced_cache_paths` 与
   `upload_path_referenced` 看见——否则缓存清理会删掉仍被对话引用的图片。
5. **迁移幂等**：v23 重复执行不改坏数据。
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.storage.store import CURRENT_SCHEMA_VERSION, ChatStorage  # noqa: E402

TRACE_A = [
    {"role": "system", "content": "系统提示"},
    {"role": "user", "content": "第一问"},
    {"role": "assistant", "content": "第一答"},
]
TRACE_B = [
    {"role": "system", "content": "系统提示"},
    {"role": "user", "content": "第二问（不同内容）"},
]


class TraceStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"
        self.storage = ChatStorage(self.db_path)
        self.conversation = self.storage.create_conversation()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _add(self, trace, content: str = "回答"):
        return self.storage.add_message(
            str(self.conversation["id"]), "assistant", content, {"trace": trace, "run_id": "r1"}
        )

    def _raw_metadata(self, message_id: str) -> str:
        with closing(sqlite3.connect(self.db_path)) as db:
            return db.execute(
                "SELECT metadata FROM messages WHERE id = ?", (message_id,)
            ).fetchone()[0]

    def _trace_rows(self) -> list[tuple[str, str]]:
        with closing(sqlite3.connect(self.db_path)) as db:
            return db.execute("SELECT trace_hash, data FROM message_traces").fetchall()

    # ---- 1/2. 写入形态与去重 ----

    def test_trace_not_stored_inline_but_hash_is(self) -> None:
        message = self._add(TRACE_A)
        raw = self._raw_metadata(str(message["id"]))
        payload = json.loads(raw)
        self.assertNotIn("trace", payload, "trace 不得再留在 metadata 里")
        self.assertEqual(payload["run_id"], "r1", "其余键原样保留")
        with closing(sqlite3.connect(self.db_path)) as db:
            trace_hash = db.execute(
                "SELECT trace_hash FROM messages WHERE id = ?", (str(message["id"]),)
            ).fetchone()[0]
        self.assertTrue(trace_hash, "消息行必须记下 trace_hash")
        rows = self._trace_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], trace_hash)
        self.assertEqual(json.loads(rows[0][1]), TRACE_A, "表里存的必须是 trace 原文")

    def test_identical_traces_share_one_row(self) -> None:
        first = self._add(TRACE_A, "答一")
        second = self._add(TRACE_A, "答二")
        self.assertEqual(len(self._trace_rows()), 1, "同内容 trace 只应有一行")
        with closing(sqlite3.connect(self.db_path)) as db:
            hashes = [
                row[0]
                for row in db.execute("SELECT trace_hash FROM messages ORDER BY created_at, rowid")
            ]
        self.assertEqual(hashes[0], hashes[1], "两条消息指向同一 trace_hash")

    def test_different_traces_get_different_rows(self) -> None:
        self._add(TRACE_A, "答一")
        self._add(TRACE_B, "答二")
        self.assertEqual(len(self._trace_rows()), 2)

    def test_empty_trace_writes_no_row_and_no_key(self) -> None:
        message = self.storage.add_message(
            str(self.conversation["id"]), "assistant", "无 trace", {"trace": []}
        )
        self.assertEqual(self._trace_rows(), [], "空 trace 不建表行")
        self.assertNotIn("trace", json.loads(self._raw_metadata(str(message["id"]))))

    def test_image_paths_do_not_enter_the_trace_table_as_metadata(self) -> None:
        """大 payload（图片 data URI）也只存一份，且不会回到 metadata 列。"""
        heavy = [{"role": "user", "content": [{"type": "image", "data": "x" * 4096}]}]
        first = self._add(heavy, "答一")
        second = self._add(heavy, "答二")
        self.assertEqual(len(self._trace_rows()), 1)
        self.assertLess(
            len(self._raw_metadata(str(first["id"]))),
            len(self._raw_metadata(str(second["id"]))) + 4096,
            "metadata 列里不得再出现那份 4KB 负载",
        )

    # ---- 3. 读回契约不变 ----

    def test_get_conversation_hydrates_trace(self) -> None:
        message = self._add(TRACE_A)
        loaded = self.storage.get_conversation(str(self.conversation["id"]))
        target = [m for m in loaded["messages"] if str(m["id"]) == str(message["id"])][0]
        self.assertEqual(target["metadata"]["trace"], TRACE_A, "读回必须与写入逐字一致")
        self.assertNotIn("trace_hash", target, "trace_hash 是实现细节，不透给消费方")

    def test_build_model_history_replays_original_trace(self) -> None:
        from naiba.core.history import build_model_history

        self._add(TRACE_A)
        loaded = self.storage.get_conversation(str(self.conversation["id"]))
        history = build_model_history(loaded["messages"])
        roles = [item.get("role") for item in history]
        self.assertIn("assistant", roles, "抽表后 trace 重放路径必须照旧可用")
        self.assertTrue(
            any("第一答" in json.dumps(item, ensure_ascii=False) for item in history),
            "trace 内容必须真的进了模型历史",
        )

    def test_run_snapshot_history_keeps_trace(self) -> None:
        """快照不再固化整段会话，但**冻结历史**里的 trace 必须照旧 hydrate 回来。

        新契约：`create_chat_run` 只在快照里留 `history_size`，冻结历史由
        `input_message_id` 游标在运行期解析。这里直接验证那条路径拿到的消息带 trace。
        """
        self._add(TRACE_A)
        agent = {"id": "general", "name": "通用 Agent"}
        run = self.storage.create_chat_run(
            str(self.conversation["id"]), "第一问", [], agent,
            {"model_key": "online:demo"}, "craft",
        )
        snapshot = self.storage.get_run_snapshot(str(run["id"])) or {}
        self.assertNotIn("conversation_messages", snapshot, "新契约：快照不固化整段会话")
        self.assertGreaterEqual(int(snapshot.get("history_size") or 0), 1)

        from naiba.run.chat import _frozen_history_for_run

        frozen = _frozen_history_for_run(run, self.storage.get_conversation(
            str(self.conversation["id"])
        ))
        assistant = [m for m in frozen if m.get("metadata", {}).get("trace")]
        self.assertTrue(assistant, "冻结历史里的消息必须带 hydrate 回来的 trace")
        self.assertEqual(assistant[-1]["metadata"]["trace"], TRACE_A)

    def test_frozen_history_prefers_legacy_snapshot_copy(self) -> None:
        """存量 run（快照里还有副本）必须优先用那份副本，保证升级后行为不变。"""
        from naiba.run.chat import _frozen_history_for_run

        legacy = [{"id": "old1", "role": "user", "content": "旧副本", "metadata": {}}]
        frozen = _frozen_history_for_run(
            {"conversation_messages": legacy, "input_message_id": "whatever"}, {}
        )
        self.assertEqual(frozen, legacy)

    def test_frozen_history_falls_back_to_full_conversation(self) -> None:
        """边界 id 找不到（消息被删/截断）时退回全量，绝不静默丢上下文。"""
        from naiba.run.chat import _frozen_history_for_run

        messages = [{"id": "a", "role": "user", "content": "问", "metadata": {}}]
        frozen = _frozen_history_for_run(
            {"input_message_id": "不存在的 id"}, {"messages": messages}
        )
        self.assertEqual(frozen, messages)

    # ---- 4. 路径引用扇描 ----

    def test_referenced_cache_paths_sees_paths_inside_trace(self) -> None:
        cache_file = Path(self.tmp.name) / "data" / "generated" / "cached.png"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(b"png")
        # 与真实 trace 同形：路径是**结构化字段**，在库里以单层转义出现。
        trace = [{"role": "tool", "name": "write_file", "path": str(cache_file)}]
        message = self._add(trace, "产物到了")
        roots = [cache_file.parent]
        found = {key.lower() for key in self.storage.referenced_cache_paths(roots)}
        self.assertIn(
            str(cache_file).lower(), found,
            "trace 里的缓存路径必须出现在引用集合里（否则缓存会被误删）",
        )
        self.assertTrue(
            self.storage.upload_path_referenced(cache_file),
            "逐文件复核同样必须看得见 trace 里的路径",
        )
        # 前提确认：路径确实在独立表里，而不是被塞回 metadata
        self.assertNotIn("cached.png", self._raw_metadata(str(message["id"])))
        self.assertIn("cached.png", self._trace_rows()[0][1])

    # ---- 5. 迁移 ----

    def test_migration_dedupes_legacy_inline_traces(self) -> None:
        """存量库（trace 内联）迁移后：表里按内容去重、metadata 不再内联。"""
        with closing(sqlite3.connect(self.db_path)) as db:
            now = 1
            for index in range(4):
                payload = json.dumps({"trace": TRACE_A if index < 3 else TRACE_B},
                                     ensure_ascii=False)
                db.execute(
                    "INSERT INTO messages(id, conversation_id, role, content, metadata, created_at) "
                    "VALUES (?, ?, 'assistant', ?, ?, ?)",
                    (f"legacy{index}", str(self.conversation["id"]), f"答{index}", payload, now + index),
                )
            db.execute("UPDATE messages SET trace_hash = ''")
            db.commit()
        self.storage.set_user_version(22)
        self.storage.apply_pending_migrations()
        with closing(sqlite3.connect(self.db_path)) as db:
            rows = db.execute("SELECT data FROM message_traces").fetchall()
            self.assertEqual(len(rows), 2, "4 条消息（3 同 + 1 异）应只留 2 份唯一 blob")
            hashes = [
                row[0] for row in db.execute("SELECT trace_hash FROM messages ORDER BY rowid")
            ]
            self.assertTrue(all(hashes), "每行都必须有 trace_hash")
            self.assertEqual(len(set(hashes)), 2, "同内容消息必须指向同一 hash")
            for (raw,) in db.execute("SELECT metadata FROM messages"):
                self.assertNotIn("trace", json.loads(raw), "metadata 里不得再留 trace")

    def test_migration_is_idempotent(self) -> None:
        self._add(TRACE_A)
        self.storage.set_user_version(22)
        self.storage.apply_pending_migrations()
        rows_first = sorted(self._trace_rows())
        self.storage.apply_pending_migrations()
        self.assertEqual(sorted(self._trace_rows()), rows_first, "二次迁移不得增删内容")
        loaded = self.storage.get_conversation(str(self.conversation["id"]))
        assistant = [m for m in loaded["messages"] if m.get("role") == "assistant"][-1]
        self.assertEqual(assistant["metadata"]["trace"], TRACE_A)

    def test_broken_trace_row_does_not_break_reads(self) -> None:
        """trace 表行缺失/损坏时读回退化为"无 trace"，不得抛异常。"""
        message = self._add(TRACE_A)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE messages SET trace_hash = 'missing-hash' WHERE id = ?",
                       (str(message["id"]),))
            db.commit()
        loaded = self.storage.get_conversation(str(self.conversation["id"]))
        target = [m for m in loaded["messages"] if str(m["id"]) == str(message["id"])][0]
        self.assertNotIn("trace", target["metadata"])

    # ---- 6. 整块替换怪癖：不得把 trace 塞回来 ----

    def test_update_message_metadata_lifts_trace_back_into_table(self) -> None:
        """整块替换（update_message_metadata）收到含 trace 的 metadata 时摘表，不得内联。"""
        message = self._add(TRACE_A)
        loaded = self.storage.get_conversation(str(self.conversation["id"]))
        target = [m for m in loaded["messages"] if str(m["id"]) == str(message["id"])][0]
        self.storage.update_message_metadata(
            str(self.conversation["id"]), str(message["id"]), target["metadata"]
        )
        self.assertNotIn("trace", json.loads(self._raw_metadata(str(message["id"]))))
        self.assertEqual(len(self._trace_rows()), 1, "仍只有一份 blob")
        with closing(sqlite3.connect(self.db_path)) as db:
            trace_hash = db.execute(
                "SELECT trace_hash FROM messages WHERE id = ?", (str(message["id"]),)
            ).fetchone()[0]
        self.assertTrue(trace_hash, "trace_hash 不得被整块替换抹掉")

    def test_update_message_metadata_without_trace_keeps_existing_hash(self) -> None:
        message = self._add(TRACE_A)
        self.storage.update_message_metadata(
            str(self.conversation["id"]), str(message["id"]), {"run_id": "r2"}
        )
        with closing(sqlite3.connect(self.db_path)) as db:
            trace_hash = db.execute(
                "SELECT trace_hash FROM messages WHERE id = ?", (str(message["id"]),)
            ).fetchone()[0]
        self.assertTrue(trace_hash, "不带 trace 的整块替换必须保留既有 trace_hash")

    def test_schema_version_matches_migration_table(self) -> None:
        self.assertEqual(self.storage.get_user_version(), CURRENT_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
