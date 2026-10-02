# -*- coding: utf-8 -*-
"""护栏：推理流/正文流的流式合批（delta 块）与终态单 run 合流。

背景：流式期按「4096 字符 / 0.1s」合批落库——前端渲染本就被 scheduleStreamingMarkdown
节流（40ms 起步、240ms 封顶），逐 chunk 落库只是让每条增量各付一次完整写事务；
run 结束后由收尾（chat.py finally → store.compress_run_events）把该 run 的
delta 事件合并为整段 reasoning（与迁移 v14 同口径），历史库不膨胀。
本组测试守护：
- 流式期：reasoning_delta 与 delta 一样走合批——未达阈值不落库，flush 后合并成一块；
- 顺序：推理块先于正文块（跨通道按首字符到达时间排序）；整段 reasoning 事件透传不受缓冲影响；
- 非增量事件到达前必须先 flush（缓冲不得把事件序打乱）；
- 终态合流：仅 compress 指定 run、文本总量不变、幂等；
- 终态（completed/failed/cancelled）收缩 snapshot 的 conversation_messages，
  interrupted 保留（恢复重建需要）。
"""

import json
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.run.stream import (  # noqa: E402
    _RunEventSink,
)
from naiba.storage.store import ChatStorage  # noqa: E402


class RecordingManager:
    def __init__(self):
        self.events: list[dict] = []

    def emit(self, run_id, payload):
        self.events.append(dict(payload))
        return {"run_id": run_id, "sequence": len(self.events)}


class ReasoningStreamTests(unittest.TestCase):
    def setUp(self):
        self.manager = RecordingManager()
        self.sink = _RunEventSink(self.manager, "r1", threading.Event())

    def test_reasoning_deltas_buffered_and_merged(self):
        # 流式期新契约：reasoning_delta 与正文同节奏合批（4096 字符 / 0.1s）——
        # 逐 chunk 落库只是让每条增量各付一次完整写事务，界面节奏由前端节流器决定。
        for i in range(3):
            self.sink({"type": "reasoning_delta", "content": f"词{i}"})
        self.assertEqual(self.manager.events, [], "未达阈值不落库")
        self.sink.flush()
        self.assertEqual([e["type"] for e in self.manager.events], ["reasoning_delta"])
        self.assertEqual(self.manager.events[0]["content"], "词0词1词2", "合批后文本不丢不乱")

    def test_reasoning_before_delta_ordering(self):
        self.sink({"type": "reasoning_delta", "content": "思考"})
        self.sink({"type": "delta", "content": "正文"})
        self.sink.flush()
        self.assertEqual([e["type"] for e in self.manager.events], ["reasoning_delta", "delta"])

    def test_explicit_reasoning_passes_through(self):
        # 非 delta 的整段 reasoning 事件原样落库，不误合流。
        self.sink({"type": "reasoning", "content": "完整段落"})
        self.assertEqual(len(self.manager.events), 1)
        self.assertEqual(self.manager.events[0]["type"], "reasoning")

    def test_non_delta_event_flushes_buffers_first(self):
        # 缓冲不得把事件序打乱：status 到达前，两路缓冲必须先落库。
        self.sink({"type": "reasoning_delta", "content": "思考"})
        self.sink({"type": "delta", "content": "正文"})
        self.sink({"type": "status", "message": "进行中"})
        self.assertEqual(
            [e["type"] for e in self.manager.events],
            ["reasoning_delta", "delta", "status"],
        )

    def test_reasoning_flushed_with_content(self):
        # 收尾 flush 把缓冲的推理一并刷出（取消/失败路径靠它不丢最后一段思考）。
        self.sink({"type": "reasoning_delta", "content": "未完成思考"})
        self.sink.flush()
        self.assertEqual(len(self.manager.events), 1)
        self.assertEqual(self.manager.events[0]["content"], "未完成思考")


class RunSnapshotSlimTests(unittest.TestCase):
    """快照收缩：**新** run 不再固化整段会话；**旧** run（存量副本）终态仍会被收缩。

    2026-10 起 `create_chat_run` 不再把 `conversation_messages` 写进快照（冻结语义改由
    `input_message_id` 游标表达，见 `run/chat.py::_frozen_history_for_run`），因此这里
    必须**显式构造存量形态**——否则"终态收缩"这条保护会被悄悄测不到（历史上它挡过
    81MB 的 O(N²) 累积）。
    """

    def _make_run(self, storage, conversation_id, agent, *, legacy_history: bool = False):
        run = storage.create_chat_run(
            conversation_id, "测试消息", [], agent, {"model_key": "online:demo"}, "craft"
        )
        if legacy_history:
            # 存量形态：老版本把整段会话固化进了快照。直接改库模拟。
            # `create_chat_run` 已不再返回整段会话（新契约），这里自己读活库。
            snapshot = storage.get_run_snapshot(run["id"]) or {}
            snapshot["conversation_messages"] = storage.get_conversation(conversation_id)["messages"]
            with storage._connect() as db:  # noqa: SLF001 - 构造存量数据
                db.execute(
                    "UPDATE background_tasks SET snapshot = ? WHERE id = ?",
                    (json.dumps(snapshot, ensure_ascii=False), run["id"]),
                )
        return run

    def test_new_run_snapshot_has_no_conversation_copy(self):
        """新契约：快照里没有整段会话副本，只留体量标记与游标。"""
        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            convo = storage.create_conversation()
            agent = {"id": "general", "name": "通用 Agent"}
            run = self._make_run(storage, convo["id"], agent)
            snapshot = storage.get_run_snapshot(run["id"])
            self.assertNotIn(
                "conversation_messages", snapshot,
                "每轮固化整段会话是「运行期几百 MB 写入、库却不大」的最大来源，不得回归",
            )
            self.assertEqual(snapshot.get("history_size"), 1, "只留体量标记")
            self.assertEqual(snapshot.get("model_key"), "online:demo")

    def test_terminal_status_slims_legacy_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            convo = storage.create_conversation()
            agent = {"id": "general", "name": "通用 Agent"}
            run = self._make_run(storage, convo["id"], agent, legacy_history=True)
            before = storage.get_run_snapshot(run["id"])
            self.assertIn("conversation_messages", before, "前提：存量副本已构造出来")
            storage.update_background_task(run["id"], status="completed", finished=True)
            after = storage.get_run_snapshot(run["id"])
            self.assertNotIn("conversation_messages", after)
            self.assertEqual(after.get("model_key"), "online:demo")

    def test_failed_and_cancelled_also_slim(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            convo = storage.create_conversation()
            agent = {"id": "general", "name": "通用 Agent"}
            for status in ("failed", "cancelled"):
                run = self._make_run(storage, convo["id"], agent, legacy_history=True)
                storage.update_background_task(run["id"], status=status, finished=True)
                self.assertNotIn(
                    "conversation_messages", storage.get_run_snapshot(run["id"]),
                    f"{status} 后 snapshot 应收缩",
                )

    def test_interrupted_keeps_legacy_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            convo = storage.create_conversation()
            agent = {"id": "general", "name": "通用 Agent"}
            run = self._make_run(storage, convo["id"], agent, legacy_history=True)
            storage.update_background_task(run["id"], status="interrupted", finished=True)
            self.assertIn(
                "conversation_messages", storage.get_run_snapshot(run["id"]),
                "interrupted 是恢复期读源，存量副本不得被收缩",
            )


class CompressRunEventsTests(unittest.TestCase):
    def _insert_delta_events(self, db, run_id: str, count: int, chars: int, created_base: int = 1000):
        rows = []
        for i in range(count):
            rows.append((
                run_id, i + 1, "reasoning_delta",
                json.dumps({"type": "reasoning_delta", "content": "字" * chars}, ensure_ascii=False),
                created_base + i,
            ))
        db.executemany(
            "INSERT INTO run_events(run_id, sequence, event_type, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        db.commit()

    def test_compress_scoped_to_run_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            convo = storage.create_conversation()
            agent = {"id": "general", "name": "通用 Agent"}
            run_a = storage.create_chat_run(convo["id"], "A", [], agent, {"model_key": "m"}, "craft")
            storage.update_background_task(run_a["id"], status="completed", finished=True)
            run_b = storage.create_chat_run(convo["id"], "B", [], agent, {"model_key": "m"}, "craft")
            storage.update_background_task(run_b["id"], status="interrupted", finished=True)
            with closing(__import__("sqlite3").connect(Path(tmp) / "chat.db")) as db:
                self._insert_delta_events(db, run_a["id"], 300, 10)  # 3000 字符 -> 2 段
                self._insert_delta_events(db, run_b["id"], 100, 10)
                processed = storage.compress_run_events(run_a["id"])
                self.assertEqual(processed, 300)
                # 仅 run_a 被压缩；run_b 保持 delta
                a_delta = db.execute(
                    "SELECT COUNT(*) FROM run_events WHERE event_type='reasoning_delta' AND run_id=?",
                    (run_a["id"],),
                ).fetchone()[0]
                b_delta = db.execute(
                    "SELECT COUNT(*) FROM run_events WHERE event_type='reasoning_delta' AND run_id=?",
                    (run_b["id"],),
                ).fetchone()[0]
                self.assertEqual(a_delta, 0)
                self.assertEqual(b_delta, 100)
                # 文本不丢
                a_text = sum(
                    len(json.loads(r[0]).get("content") or "")
                    for r in db.execute(
                        "SELECT payload FROM run_events WHERE event_type='reasoning' AND run_id=?",
                        (run_a["id"],),
                    )
                )
                self.assertEqual(a_text, 3000)
                # 幂等
                self.assertEqual(storage.compress_run_events(run_a["id"]), 0)


if __name__ == "__main__":
    unittest.main()
