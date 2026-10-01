# -*- coding: utf-8 -*-
"""终态瘦身守门：`slim_terminal_run` 的事件/快照收缩必须在**终态事件之后**、且幂等。

为什么钉死：`done`/`cancelled` 事件带着完整消息对象（含 `metadata.trace`），`create_chat_run`
会把整段会话固化进 `snapshot.conversation_messages`。这两份在 run 终态后都没有读取方
（前端是唯一读者，收到终态即停止轮询），但会一直堆在库里——实测存量 35.0MB / 228 行事件
全部仍带 message 对象。守门要保证：① 瘦身真的发生；② 瘦身**只**发生在终态、且不碰
interrupted（恢复期还要读快照）；③ 幂等、可重复调用；④ 失败不吞成假成功。
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

from naiba.storage.store import ChatStorage  # noqa: E402

TRACE = [{"role": "user", "content": "问"}, {"role": "assistant", "content": "答"}]


class SlimTerminalRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"
        self.storage = ChatStorage(self.db_path)
        self.conversation = self.storage.create_conversation()
        # 先有一条历史消息：`create_chat_run` 会把**当时整段会话**固化进 snapshot
        # （这正是每轮 2× 历史写入的来源），所以必须先落消息再建 run。
        self.storage.add_message(str(self.conversation["id"]), "user", "历史提问")
        agent = {"id": "general", "name": "通用 Agent"}
        run, _handle = self.storage.create_chat_run(
            str(self.conversation["id"]), "问题", [], agent,
            {"model_key": "online:demo"}, "craft",
        )
        self.run_id = str(run["id"])
        # 终态事件带完整消息对象（含 trace）——真实形态
        self.message = self.storage.add_message(
            str(self.conversation["id"]), "assistant", "答复", {"trace": TRACE}
        )
        # `create_chat_run` 已不再固化整段会话（新契约），所以这里**显式构造存量形态**：
        # 本用例守护的是"终态把存量副本收缩掉"这条保护（历史上它挡过 81MB 的 O(N²) 累积），
        # 不构造就会变成空跑。状态也直接改库：`update_background_task(finished)` 自己会
        # 顺手收缩，那样就测不到 `slim_terminal_run` 了。
        with closing(sqlite3.connect(self.db_path)) as db:
            snapshot = json.loads(db.execute(
                "SELECT snapshot FROM background_tasks WHERE id = ?", (self.run_id,)
            ).fetchone()[0] or "{}")
            snapshot["conversation_messages"] = [
                {"id": "m0", "role": "user", "content": "历史提问", "metadata": {}},
            ]
            db.execute(
                "UPDATE background_tasks SET snapshot = ?, status = 'completed', finished_at = ? "
                "WHERE id = ?",
                (json.dumps(snapshot, ensure_ascii=False), int(time.time() * 1000), self.run_id),
            )
            db.commit()
        self.assertIn(
            "conversation_messages", self._snapshot(),
            "前提：已构造出带整段会话的存量快照（正是本用例要收缩的对象）",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ---- helpers ----

    def _append_done(self) -> None:
        self.storage.append_run_event(self.run_id, {
            "type": "done",
            "message": {"id": str(self.message["id"]), "role": "assistant", "content": "答复",
                        "metadata": {"trace": TRACE}},
        })

    def _payloads(self) -> list[dict]:
        with closing(sqlite3.connect(self.db_path)) as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT payload FROM run_events WHERE run_id = ? ORDER BY sequence", (self.run_id,)
                )
            ]

    def _snapshot(self) -> dict:
        with closing(sqlite3.connect(self.db_path)) as db:
            row = db.execute(
                "SELECT snapshot FROM background_tasks WHERE id = ?", (self.run_id,)
            ).fetchone()
        return json.loads(row[0] or "{}")

    # ---- 1. 事件瘦身 ----

    def test_done_event_keeps_message_until_slim_called(self) -> None:
        """瘦身之前事件必须**保持原样**——前端要靠它即时渲染，早瘦身就是丢内容。"""
        self._append_done()
        payloads = self._payloads()
        self.assertIn("message", payloads[-1], "终态事件在瘦身之前必须带完整消息")
        self.assertTrue(self._snapshot().get("conversation_messages"), "快照同理")

    def test_slim_removes_message_and_snapshot_history(self) -> None:
        self._append_done()
        result = self.storage.slim_terminal_run(self.run_id)
        self.assertEqual(result["events"], 1)
        self.assertEqual(result["snapshots"], 1)
        payloads = self._payloads()
        self.assertNotIn("message", payloads[-1], "done 事件不得再带完整消息对象")
        self.assertEqual(payloads[-1]["type"], "done", "事件类型保留（前端仍按类型收尾）")
        self.assertNotIn("conversation_messages", self._snapshot(), "快照不得再固化整段会话")

    def test_cancelled_event_aborted_message_also_slimmed(self) -> None:
        self.storage.append_run_event(self.run_id, {
            "type": "cancelled",
            "aborted_message": {"id": "m2", "role": "assistant", "content": "（已中止）",
                                "metadata": {"aborted": True}},
        })
        self.storage.slim_terminal_run(self.run_id)
        payload = self._payloads()[-1]
        self.assertNotIn("aborted_message", payload, "已中止消息同理不得留在事件里")
        self.assertEqual(payload["type"], "cancelled")

    def test_cancelled_event_string_message_is_also_removed(self) -> None:
        """`message` 是**字符串**形态（"任务已取消"）时同样按事件载荷瘦身规则去掉。

        与迁移 `_slim_terminal_event_payloads` 逐字同口径：它 pop 的就是 `message` 这个键，
        不看类型——"任务已取消"这类文案由 `type` 字段承担，前端不靠它渲染。
        """
        self.storage.append_run_event(self.run_id, {
            "type": "cancelled", "message": "任务已取消",
        })
        self.storage.slim_terminal_run(self.run_id)
        payload = self._payloads()[-1]
        self.assertNotIn("message", payload)
        self.assertEqual(payload["type"], "cancelled", "类型必须保留（前端按它收尾）")

    # ---- 2. 不碰 interrupted（恢复期要读快照） ----

    def test_interrupted_snapshot_is_not_slimmed(self) -> None:
        self.storage.update_background_task(self.run_id, status="interrupted", finished=True)
        self.storage.slim_terminal_run(self.run_id)
        self.assertTrue(
            self._snapshot().get("conversation_messages"),
            "interrupted 运行还要靠快照恢复，不得收缩",
        )

    def test_new_run_snapshot_has_no_conversation_copy(self) -> None:
        """新契约：`create_chat_run` 不再固化整段会话（每轮省掉 2× 历史的写入）。"""
        agent = {"id": "general", "name": "通用 Agent"}
        fresh, _history = self.storage.create_chat_run(
            str(self.conversation["id"]), "另一轮提问", [], agent,
            {"model_key": "online:demo"}, "craft",
        )
        snapshot = self.storage.get_run_snapshot(str(fresh["id"])) or {}
        self.assertNotIn("conversation_messages", snapshot)
        expected = len(self.storage.get_conversation(str(self.conversation["id"]))["messages"])
        self.assertEqual(snapshot.get("history_size"), expected, "只留体量标记")

    # ---- 3. 幂等 ----

    def test_slim_is_idempotent(self) -> None:
        self._append_done()
        first = self.storage.slim_terminal_run(self.run_id)
        second = self.storage.slim_terminal_run(self.run_id)
        self.assertEqual(first["events"], 1)
        self.assertEqual(second["events"], 0, "第二次不得再改任何行")
        self.assertEqual(second["snapshots"], 0)
        self.assertEqual(self._payloads()[-1]["type"], "done")

    def test_slim_only_touches_its_own_run(self) -> None:
        """只能动本 run：另一个未结束的 run 的事件/快照必须原样保留。"""
        agent = {"id": "general", "name": "通用 Agent"}
        other, _h = self.storage.create_chat_run(
            str(self.conversation["id"]), "另一个问题", [], agent,
            {"model_key": "online:demo"}, "craft",
        )
        other_id = str(other["id"])
        self.storage.append_run_event(other_id, {
            "type": "done", "message": {"id": "x", "role": "assistant", "content": "y",
                                        "metadata": {"trace": TRACE}},
        })
        # 给"别的 run"也构造存量快照：这样"只动自己的 run"这条才真的可判。
        with closing(sqlite3.connect(self.db_path)) as db:
            snapshot = json.loads(db.execute(
                "SELECT snapshot FROM background_tasks WHERE id = ?", (other_id,)
            ).fetchone()[0] or "{}")
            snapshot["conversation_messages"] = [{"id": "z", "role": "user", "content": "别的"}]
            db.execute(
                "UPDATE background_tasks SET snapshot = ? WHERE id = ?",
                (json.dumps(snapshot, ensure_ascii=False), other_id),
            )
            db.commit()
        self._append_done()
        self.storage.slim_terminal_run(self.run_id)
        with closing(sqlite3.connect(self.db_path)) as db:
            other_payload = json.loads(db.execute(
                "SELECT payload FROM run_events WHERE run_id = ?", (other_id,)
            ).fetchone()[0])
            other_snapshot = json.loads(db.execute(
                "SELECT snapshot FROM background_tasks WHERE id = ?", (other_id,)
            ).fetchone()[0])
        self.assertIn("message", other_payload, "别的 run 的事件不得被顺手瘦身")
        self.assertIn("conversation_messages", other_snapshot, "别的 run 的快照同理")

    # ---- 4. 与 trace 抽表的配合 ----

    def test_slim_does_not_touch_message_rows(self) -> None:
        """瘦身只动事件与快照：消息本体（含 hydrate 的 trace）必须完好。"""
        self._append_done()
        self.storage.slim_terminal_run(self.run_id)
        loaded = self.storage.get_conversation(str(self.conversation["id"]))
        target = [m for m in loaded["messages"] if str(m["id"]) == str(self.message["id"])][0]
        self.assertEqual(target["metadata"]["trace"], TRACE, "消息里的 trace 不得被动到")


if __name__ == "__main__":
    unittest.main()
