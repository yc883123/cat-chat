# -*- coding: utf-8 -*-
"""守门：插话（interjection）——运行中的第二输入通道。

复活这套能力的四条不可退让的契约（2026-09-19 定稿，见 计划-复活插话功能.md）：

1. **状态机唯一**：pending（待引导）→ guided（已引导，等 agent 取走）→ consumed；
   取消时一律 stopped。`list_run_interjections` 只放行 guided 未消费的那些。
2. **未被消费前不进模型上下文**：插话落库是普通 role=user 行，`build_model_history`
   必须按 metadata 过滤——否则用户"排队但没发"的指令会被送进下一轮请求（stopped 的
   更是直接违反 README「取消后绝不自动发送」的承诺）。
3. **只追加，不改写历史**：agent 消费点把插话 append 到 messages 末尾，相对上一次请求
   仍是 append-only（DeepSeek 前缀缓存契约）；终态答复与插话同步到达时先落答复、再把
   插话排回末尾（不能让 messages 出现连续两条 user）。
4. **不做自动 follow-up**：run 结束时队列里的残留只提示、不自动开新 run，否则终态带
   选择题时刚弹出的选择面板会被新 run 立刻锁死。

全量验证口径见 `项目维护说明（修改代码前必读）.md` §六。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core.history import build_model_history  # noqa: E402
from naiba.core.messages import MetadataKeys  # noqa: E402
from naiba.run.manager import ConversationRunManager  # noqa: E402
from naiba.skills.agent import SkillAgent  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402

AGENT = {"id": "", "name": "Chat", "system_prompt": "", "skill_ids": []}


def _interjection_message(message_id: str, content: str, **flags) -> dict:
    """一条插话在库里的形态（role=user + metadata 标记）。"""
    return {
        "id": message_id,
        "role": "user",
        "content": content,
        "metadata": {
            MetadataKeys.INTERJECTION: True,
            MetadataKeys.RUN_ID: "run-1",
            **flags,
        },
        "created_at": 1_700_000_000_000,
    }


class InterjectionStorageTests(unittest.TestCase):
    """存储层状态机：入库 → 引导 → 消费，各自只走一条路。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.conversation = self.storage.create_conversation()
        self.conversation_id = str(self.conversation["id"])
        run = self.storage.create_run(self.conversation_id, "回答", AGENT, {}, kind="chat")
        self.run_id = str(run["id"])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _end_run(self) -> None:
        self.storage.update_background_task(self.run_id, status="completed", finished=True)

    def _new_run(self) -> str:
        run = self.storage.create_run(self.conversation_id, "回答", AGENT, {}, kind="chat")
        return str(run["id"])

    def _metadata(self, message_id: str) -> dict:
        """直读落库 metadata：公开读接口不回传元数据，而本文件断言的全是标记位。"""
        with self.storage._connect() as db:  # noqa: SLF001 - 见上
            row = db.execute(
                "SELECT metadata FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        return json.loads(row["metadata"] or "{}")

    def test_pending_is_not_pullable_until_guided(self) -> None:
        """未引导的插话不进 agent 的可拉取队列（用户没点「引导」就不该被取走）。"""
        saved = self.storage.add_run_interjection(self.conversation_id, self.run_id, "先别截图")
        self.assertTrue(saved["metadata"][MetadataKeys.INTERJECTION])
        self.assertFalse(saved["metadata"][MetadataKeys.INTERJECTION_GUIDED])
        self.assertEqual(self.storage.list_run_interjections(self.run_id), [])

        guided = self.storage.guide_run_interjection(self.conversation_id, self.run_id, saved["id"])
        self.assertTrue(guided["metadata"][MetadataKeys.INTERJECTION_GUIDED])
        pullable = self.storage.list_run_interjections(self.run_id)
        self.assertEqual([item["id"] for item in pullable], [saved["id"]])
        self.assertEqual(pullable[0]["content"], "先别截图")

    def test_consumed_leaves_the_queue_but_stays_in_history(self) -> None:
        saved = self.storage.add_run_interjection(self.conversation_id, self.run_id, "改用 4K")
        self.storage.guide_run_interjection(self.conversation_id, self.run_id, saved["id"])
        self.storage.mark_run_interjections_consumed(self.run_id, [saved["id"]])
        self.assertEqual(self.storage.list_run_interjections(self.run_id), [])
        self.assertTrue(self._metadata(saved["id"])[MetadataKeys.INTERJECTION_CONSUMED])

    def test_consumed_flag_is_ignored_for_other_run(self) -> None:
        """标记消费必须校验 run_id：别的 Run 的 id 传进来不得污染本 Run 的插话。"""
        saved = self.storage.add_run_interjection(self.conversation_id, self.run_id, "A")
        self.storage.guide_run_interjection(self.conversation_id, self.run_id, saved["id"])
        self.storage.mark_run_interjections_consumed("other-run", [saved["id"]])
        self.assertEqual(len(self.storage.list_run_interjections(self.run_id)), 1)

    def test_add_and_guide_reject_finished_run(self) -> None:
        """Run 一结束就关门：迟到的插话不收（否则它会永远等一个不再跑的循环）。"""
        self._end_run()
        with self.assertRaises(LookupError):
            self.storage.add_run_interjection(self.conversation_id, self.run_id, "晚了")

        live_run = self._new_run()
        saved = self.storage.add_run_interjection(self.conversation_id, live_run, "先来")
        self.storage.update_background_task(live_run, status="completed", finished=True)
        with self.assertRaises(LookupError):
            self.storage.guide_run_interjection(self.conversation_id, live_run, saved["id"])

    def test_edit_is_in_place_and_guarded(self) -> None:
        saved = self.storage.add_run_interjection(self.conversation_id, self.run_id, "原文")
        edited = self.storage.edit_run_interjection(
            self.conversation_id, self.run_id, saved["id"], "改后的内容"
        )
        self.assertEqual(edited["id"], saved["id"], "编辑必须就地改内容、保持消息 id 稳定")
        self.assertEqual(edited["content"], "改后的内容")
        with self.assertRaises(ValueError):
            self.storage.edit_run_interjection(self.conversation_id, self.run_id, saved["id"], "   ")

        # 已引导的插话不能再改：它已进入 agent 的可拉取队列，就地改写会让"模型看到的"
        # 与"用户界面上显示的"不一致。
        self.storage.guide_run_interjection(self.conversation_id, self.run_id, saved["id"])
        with self.assertRaises(LookupError):
            self.storage.edit_run_interjection(self.conversation_id, self.run_id, saved["id"], "偷偷改")

    def test_delete_only_before_guided(self) -> None:
        saved = self.storage.add_run_interjection(self.conversation_id, self.run_id, "待删")
        self.assertTrue(self.storage.delete_run_interjection(self.conversation_id, self.run_id, saved["id"]))
        self.assertFalse(self.storage.delete_run_interjection(self.conversation_id, self.run_id, saved["id"]))

        keep = self.storage.add_run_interjection(self.conversation_id, self.run_id, "已引导")
        self.storage.guide_run_interjection(self.conversation_id, self.run_id, keep["id"])
        self.assertFalse(
            self.storage.delete_run_interjection(self.conversation_id, self.run_id, keep["id"]),
            "Run 还活着时已引导的插话不能被删除——它已经排在 agent 的取用队列里",
        )

    def test_stopped_row_is_deletable_even_after_guided(self) -> None:
        """Run 结束后队列冻结，残留的已引导行必须能删——否则用户排的字既发不出也删不掉。"""
        saved = self.storage.add_run_interjection(self.conversation_id, self.run_id, "冻结后清掉")
        self.storage.guide_run_interjection(self.conversation_id, self.run_id, saved["id"])
        self.storage.stop_pending_interjections(self.run_id)
        self.assertTrue(
            self.storage.delete_run_interjection(self.conversation_id, self.run_id, saved["id"]),
            "已冻结（Run 已结束）的行必须可删——这是「取回输入框」出口的前提",
        )

    def test_consumed_row_is_never_deletable(self) -> None:
        """已消费的插话是本轮真实上下文的一部分，删掉会让库内历史与模型看到的不一致。"""
        saved = self.storage.add_run_interjection(self.conversation_id, self.run_id, "已进上下文")
        self.storage.guide_run_interjection(self.conversation_id, self.run_id, saved["id"])
        self.storage.mark_run_interjections_consumed(self.run_id, [saved["id"]])
        self.storage.stop_pending_interjections(self.run_id)
        self.assertFalse(
            self.storage.delete_run_interjection(self.conversation_id, self.run_id, saved["id"])
        )

    def test_stop_pending_freezes_queue_without_sending(self) -> None:
        """取消运行 → 队列就地冻结：guided 的也一并停掉，且一条都不会被派发。"""
        first = self.storage.add_run_interjection(self.conversation_id, self.run_id, "一")
        second = self.storage.add_run_interjection(self.conversation_id, self.run_id, "二")
        self.storage.guide_run_interjection(self.conversation_id, self.run_id, first["id"])

        self.assertEqual(self.storage.stop_pending_interjections(self.run_id), 2)
        self.assertEqual(self.storage.list_run_interjections(self.run_id), [])
        for item in (first, second):
            metadata = self._metadata(item["id"])
            self.assertTrue(metadata[MetadataKeys.INTERJECTION_STOPPED])
            self.assertFalse(metadata.get(MetadataKeys.INTERJECTION_CONSUMED))
        # 幂等：再停一次不重复计数（消费过的也不该被改写）
        self.assertEqual(self.storage.stop_pending_interjections(self.run_id), 0)


class InterjectionHistoryTests(unittest.TestCase):
    """没被消费的插话不得进模型上下文；消费过的必须原样在上下文里（否则前缀缓存错位）。"""

    def _history(self) -> list[dict]:
        conversation = [
            {"role": "user", "content": "第一问", "metadata": {}},
            {"role": "assistant", "content": "第一答", "metadata": {}},
            _interjection_message("i-pending", "排队但没引导"),
            _interjection_message("i-guided", "已引导但模型还没取走",
                                  **{MetadataKeys.INTERJECTION_GUIDED: True}),
            _interjection_message("i-stopped", "取消时被冻结",
                                  **{MetadataKeys.INTERJECTION_STOPPED: True}),
            _interjection_message("i-consumed", "已被模型取走",
                                  **{MetadataKeys.INTERJECTION_CONSUMED: True}),
            {"role": "user", "content": "第二问", "metadata": {}},
        ]
        return build_model_history(conversation)

    def test_only_consumed_interjection_reaches_context(self) -> None:
        history = self._history()
        contents = [str(item.get("content") or "") for item in history]
        self.assertEqual(contents, ["第一问", "第一答", "已被模型取走", "第二问"])

    def test_stopped_interjection_never_leaks(self) -> None:
        """取消后冻结的插话绝不能出现在后续任何一轮请求里（README 的承诺）。"""
        for item in self._history():
            self.assertNotIn("取消时被冻结", str(item.get("content") or ""))


# ---------------------------------------------------------------------------
# Agent 消费点：只追加、不改写；终态答复与插话同步到达时先落答复再重排
# ---------------------------------------------------------------------------


class _Catalog:
    def scan(self) -> list:
        return []

    def read_skill_content(self, path: str) -> str:  # pragma: no cover
        return ""


class _EchoRegistry:
    """永远成功、结果逐次不同（否则先撞「无进展」熔断）。"""

    def __init__(self) -> None:
        self.calls = 0

    def schemas(self) -> list:
        return []

    def side_effect(self, name: str) -> bool:
        return False

    def media_declaration(self, name: str) -> dict:
        return {"extract": "none", "policy": "never"}

    def execute(self, tool: str, arguments: dict, active: list, run_context: object):
        self.calls += 1
        return True, f"工具结果 #{self.calls}"


class _Queue:
    """可控的插话队列桩：`arm_at` 决定第几次拉取开始返回内容。"""

    def __init__(self, *, arm_at: int, item: dict) -> None:
        self.arm_at = arm_at
        self.item = item
        self.pulls = 0
        self.items: list[dict] = []
        self.consumed: list[str] = []

    def pull(self) -> list[dict]:
        self.pulls += 1
        if self.pulls >= self.arm_at and not self.consumed:
            self.items = [self.item]
        return list(self.items)

    def mark(self, ids: list[str]) -> None:
        self.consumed.extend(ids)
        self.items = []


def _tool_call_reply(_profile, _messages, _options, _event) -> str:
    return '{"type":"tool","tool":"echo","arguments":{"n":1}}'


class InterjectionConsumeTests(unittest.TestCase):
    def _drive(self, complete, queue: _Queue) -> tuple:
        events: list[dict] = []
        requests: list[list[dict]] = []

        def wrapped(profile, messages, options, event):
            requests.append([dict(item) for item in messages])
            return complete(profile, messages, options, event)

        worker = SkillAgent(_Catalog(), None, wrapped, None)
        run_context = {
            "pull_interjections": queue.pull,
            "mark_interjections_consumed": queue.mark,
            "workspace_dir": str(ROOT),
        }
        result = worker.run(
            "跑起来",
            [],
            {"kind": "online", "model": "m", "context_window": 100_000},
            {"stream": False, "max_tokens": 512},
            {"mode": "auto", "skill_ids": []},
            [],
            "",
            ["echo"],
            events.append,
            None,
            10,
            tool_registry=_EchoRegistry(),
            run_context=run_context,
        )
        return str(result[0]), events, requests

    def test_interjection_is_appended_never_rewrites_prefix(self) -> None:
        """消费点只往 messages 末尾追加：上一次请求必须仍是本次请求的逐字前缀。"""
        queue = _Queue(arm_at=2, item={"id": "i1", "content": "改用竖版", "metadata": {}})
        calls = {"n": 0}

        def complete(profile, messages, options, event):
            calls["n"] += 1
            if calls["n"] == 1:
                return _tool_call_reply(profile, messages, options, event)
            return "收尾答复，已经完整结束。"

        response, events, requests = self._drive(complete, queue)
        self.assertEqual(response, "收尾答复，已经完整结束。")
        self.assertGreaterEqual(
            len(requests), 2, "插话被取走后必须再发一次请求（模型得看到这条指令）"
        )

        before, after = requests[0], requests[1]
        prefix = after[: len(before)]
        self.assertEqual(
            json.dumps(prefix, ensure_ascii=False, sort_keys=True),
            json.dumps(before, ensure_ascii=False, sort_keys=True),
            "插话必须只追加；改写/重排已有消息会破坏 DeepSeek 前缀缓存",
        )
        self.assertIn("改用竖版", str(after[-1].get("content") or ""))

        self.assertEqual(queue.consumed, ["i1"], "取走后必须标记已消费（否则会重复追加）")
        consumed_events = [item for item in events if item.get("type") == "interjection_consumed"]
        self.assertEqual([item.get("message_id") for item in consumed_events], ["i1"])
        self.assertEqual(set(consumed_events[0]), {"type", "message_id", "message"})

    def test_final_answer_and_interjection_in_one_step_reorders(self) -> None:
        """模型认为这轮结束了、插话同时到达：先落答复再把插话排回末尾（不许连续两条 user）。"""
        queue = _Queue(arm_at=3, item={"id": "i2", "content": "先停手，改做封面", "metadata": {}})
        calls = {"n": 0}

        def complete(profile, messages, options, event):
            calls["n"] += 1
            if calls["n"] == 1:
                return _tool_call_reply(profile, messages, options, event)
            if calls["n"] == 2:
                return "第一版答复：我打算收尾了。"
            return "收到插话后的答复，已经按照新指令重做。"

        response, _events, requests = self._drive(complete, queue)
        self.assertEqual(response, "收到插话后的答复，已经按照新指令重做。")
        self.assertEqual(queue.consumed, ["i2"])

        tail = requests[2][-2:]
        self.assertEqual(tail[0].get("role"), "assistant", "答复必须先于插话落位")
        self.assertEqual(tail[0].get("content"), "第一版答复：我打算收尾了。")
        self.assertEqual(tail[1].get("role"), "user")
        self.assertIn("先停手，改做封面", str(tail[1].get("content") or ""))
        # 全程不得出现连续两条同角色消息（模型侧非法且破坏 append-only 语义）
        roles = [str(item.get("role") or "") for item in requests[-1]]
        self.assertFalse(
            any(roles[i] == roles[i + 1] == "user" for i in range(len(roles) - 1)),
            "messages 里出现了连续两条 user",
        )

    def test_no_pull_callback_means_no_interjections(self) -> None:
        """子 Agent / 计划等形态不注入回调：消费点必须安静跳过（不能报错）。"""
        calls = {"n": 0}

        def complete(profile, messages, options, event):
            calls["n"] += 1
            return "直接答复，已经完整。"

        events: list[dict] = []
        worker = SkillAgent(_Catalog(), None, complete, None)
        response, _runs, _reasonings, _usage = worker.run(
            "跑起来",
            [],
            {"kind": "online", "model": "m", "context_window": 100_000},
            {"stream": False, "max_tokens": 512},
            {"mode": "auto", "skill_ids": []},
            [],
            "",
            ["echo"],
            events.append,
            None,
            5,
        )
        self.assertEqual(response, "直接答复，已经完整。")
        self.assertEqual([item for item in events if item.get("type") == "interjection_consumed"], [])


# ---------------------------------------------------------------------------
# Run 骨架：引导时撤销旧确认、取消时冻结队列
# ---------------------------------------------------------------------------


class _StorageStub:
    def __init__(self, run: dict | None) -> None:
        self.run = run
        self.guided: list[tuple] = []
        self.stopped: list[str] = []
        self.deleted: list[tuple] = []
        self.deleted_ok = True
        self.updated: list[tuple] = []

    def get_background_task(self, run_id: str) -> dict | None:
        return self.run

    def active_run(self, conversation_id: str) -> dict | None:
        return self.run

    def list_background_tasks(self, conversation_id="", active_only=False, limit=50, exclude_kinds=None):
        return []

    def update_background_task(self, task_id: str, **kwargs):
        self.updated.append((task_id, kwargs))
        if self.run:
            self.run = {**self.run, "status": kwargs.get("status") or self.run.get("status")}
        return self.run

    def guide_run_interjection(self, conversation_id: str, run_id: str, message_id: str) -> dict:
        self.guided.append((conversation_id, run_id, message_id))
        return {"id": message_id, "content": "改用 4K"}

    def stop_pending_interjections(self, run_id: str) -> int:
        self.stopped.append(run_id)
        return 1

    def delete_run_interjection(self, conversation_id: str, run_id: str, message_id: str) -> bool:
        self.deleted.append((conversation_id, run_id, message_id))
        return self.deleted_ok


class _AppStub:
    def __init__(self, storage) -> None:
        self.storage = storage


class _ExecutorStub:
    def __init__(self, pending: list[str]) -> None:
        self.pending_confirmation = {confirm_id: {} for confirm_id in pending}
        self.rejected: list[str] = []

    def reject_execute(self, confirm_id: str):
        self.rejected.append(confirm_id)
        self.pending_confirmation.pop(confirm_id, None)
        return True, "已拒绝"


class _FakeBus:
    """`_finish` 要用 bus 唤醒等待中的流线程 / 丢弃 run（等价事件总线的最小替身）。"""

    def __init__(self) -> None:
        self.conditions: dict[str, threading.Condition] = {}

    def ensure(self, run_id: str) -> threading.Condition:
        return self.conditions.setdefault(run_id, threading.Condition())

    def drop(self, run_id: str) -> None:
        self.conditions.pop(run_id, None)


def _manager(run: dict | None, storage: _StorageStub | None = None) -> ConversationRunManager:
    manager = ConversationRunManager.__new__(ConversationRunManager)
    manager.app = _AppStub(storage or _StorageStub(run))
    manager.bus = _FakeBus()
    manager._lock = threading.RLock()
    manager._submit_lock = threading.RLock()
    manager._events = {}
    manager._threads = {}
    manager._executors = {}
    manager._sinks = {}
    manager._sinks_lock = threading.Lock()
    manager._schedule_forced_cancel = lambda run_id: None
    return manager


class RunSkeletonInterjectionTests(unittest.TestCase):
    def test_guide_rejects_pending_tool_confirmations(self) -> None:
        """引导必须撤掉待确认的工具卡：否则 agent 停在那一步等用户点，插话永远轮不到。"""
        storage = _StorageStub({"id": "run-1", "status": "running", "conversation_id": "c1"})
        manager = _manager(None, storage)
        executor = _ExecutorStub(["confirm-a", "confirm-b"])
        manager._executors["run-1"] = executor
        events: list[dict] = []
        manager.emit = lambda run_id, payload: events.append(payload)

        manager.guide_interjection(
            {"conversation_id": "c1", "run_id": "run-1", "message_id": "m1"}
        )
        self.assertEqual(sorted(executor.rejected), ["confirm-a", "confirm-b"])
        self.assertEqual(executor.pending_confirmation, {})
        self.assertEqual(storage.guided, [("c1", "run-1", "m1")])
        self.assertEqual([item["type"] for item in events], ["user_guidance"])
        self.assertEqual(events[0]["message_id"], "m1")

    def test_guide_validation(self) -> None:
        manager = _manager({"id": "run-1", "status": "running", "conversation_id": "c1"})
        with self.assertRaises(ValueError):
            manager.guide_interjection({"conversation_id": "c1", "run_id": "run-1"})
        with self.assertRaises(ValueError):
            manager.interject({"conversation_id": "c1", "run_id": "run-1", "message": " "})
        with self.assertRaises(ValueError):
            manager.interject({
                "conversation_id": "c1", "run_id": "run-1", "message": "x", "attachments": "no",
            })

    def test_delete_works_after_the_run_ended(self) -> None:
        """删除**不得**要求「有活动 Run」：本版无自动 follow-up，run 结束后残留只能靠用户清理
        （面板「取回输入框」= 写回输入框 + 调删除）。在这里要求活动 Run 会把唯一出口堵死。"""
        storage = _StorageStub(None)   # 没有活动 Run
        manager = _manager(None, storage)
        self.assertEqual(
            manager.delete_interjection(
                {"conversation_id": "c1", "run_id": "run-1", "message_id": "m1"}
            ),
            {"ok": True, "message_id": "m1"},
        )
        storage.deleted_ok = False
        with self.assertRaises(LookupError):
            manager.delete_interjection(
                {"conversation_id": "c1", "run_id": "run-1", "message_id": "m2"}
            )

    def test_finish_freezes_the_queue(self) -> None:
        """任何终态（含正常完成）都要冻结队列——否则 run 结束后用户点「引导」只会拿到 404。"""
        storage = _StorageStub({"id": "run-1", "status": "running", "conversation_id": "c1"})
        manager = _manager(None, storage)
        manager._finish("run-1")
        self.assertEqual(storage.stopped, ["run-1"])

    def test_cancel_freezes_the_queue(self) -> None:
        """取消路径必须冻结队列——否则 run 线程还卡在模型流上时，用户点「引导」还会补进去。"""
        storage = _StorageStub({"id": "run-1", "status": "running", "conversation_id": "c1"})
        manager = _manager(None, storage)
        manager.cancel("run-1")
        self.assertEqual(storage.stopped, ["run-1"])


if __name__ == "__main__":
    unittest.main()
