# -*- coding: utf-8 -*-
"""护栏：删除单条消息 / 整轮 + 撤销（`ChatStorage.delete_message` / `restore_messages`）。

保护对象：
- **single** 只删该 id 那一行；`role='session'` 的遗留标记行**拒绝**（那是「新会话分割线」
  的旧形态，语义属于 `delete_session_start` —— 误删会把上下文起点悄悄往前挪）；
- **turn** 仅对 user 可用：删该 user + 其后**紧随的连续 assistant**（遇下一条 user 即停），
  途中的遗留 session 标记行一并带走（它属于这一轮的痕迹）；对 assistant 用 turn 要拒绝；
- 快照必须**逐字节完整**（id / role / content / metadata_json / created_at），
  撤销后 `build_model_history` 与删除前**逐字节一致** —— 否则「删了又撤销」会让
  模型请求的前缀缓存整体失效（这是本功能最贵的代价）；
- 撤销**幂等**：重复点撤销跳过已存在的 id，不报错、不覆盖；
- 删除与撤销都要在事务内推进 `conversations.updated_at`（前端既有轮询靠它感知）；
- 非法 mode / 不存在的消息 / 不存在的会话各自映射到正确的错误类型（路由层转 400 / 404）。
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.core.history import build_model_history  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class MessageDeleteTestCase(unittest.TestCase):
    """公共脚手架：一个「两轮」会话 + 冻结 updated_at 的助手。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_msgdelete_")
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.conversation = self.storage.create_conversation(title="删除实验")
        self.cid = self.conversation["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _add(self, role, content, metadata=None):
        return self.storage.add_message(self.cid, role, content, metadata)["id"]

    def _two_turns(self):
        """u1 a1 u2 a2，返回 id 元组（顺序即库内顺序）。"""
        return (
            self._add("user", "第一轮问题"),
            self._add("assistant", "第一轮回答", {"reasoning": "想了一下"}),
            self._add("user", "第二轮问题"),
            self._add("assistant", "第二轮回答"),
        )

    def _rows(self):
        return self.storage.get_conversation(self.cid)["messages"]

    def _freeze_updated_at(self, value):
        with closing(sqlite3.connect(self.storage.data_dir / "chat.db")) as db:
            db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (value, self.cid))
            db.commit()

    def _live_updated_at(self):
        return self.storage.get_conversation(self.cid, include_messages=False)["updated_at"]

    def _history(self):
        return build_model_history(self._rows())


class DeleteSingleModeTests(MessageDeleteTestCase):
    def test_single_removes_only_that_row(self):
        u1, a1, u2, a2 = self._two_turns()
        result = self.storage.delete_message(self.cid, a1, "single")
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "single")
        self.assertEqual([m["id"] for m in self._rows()], [u1, u2, a2],
                         "只删指定那一条，邻居原样保留")
        self.assertEqual([s["id"] for s in result["removed"]], [a1])

    def test_single_can_remove_tail(self):
        u1, a1, u2, a2 = self._two_turns()
        self.storage.delete_message(self.cid, a2, "single")
        self.assertEqual([m["id"] for m in self._rows()], [u1, a1, u2])

    def test_single_default_mode(self):
        _, a1, _, _ = self._two_turns()
        result = self.storage.delete_message(self.cid, a1)  # 不传 mode
        self.assertEqual(result["mode"], "single", "默认 single，不能顺手删掉一整轮")

    def test_session_marker_row_is_rejected(self):
        self._add("session", "")
        session_id = self._rows()[0]["id"]
        self.assertEqual(self._rows()[0]["role"], "session")
        with self.assertRaises(ValueError):
            self.storage.delete_message(self.cid, session_id, "single")
        self.assertEqual(len(self._rows()), 1, "拒绝即不落删：标记行必须还在")

    def test_unknown_message_raises_lookup(self):
        self._two_turns()
        with self.assertRaises(LookupError):
            self.storage.delete_message(self.cid, "missing-id", "single")

    def test_invalid_mode_raises_value_error(self):
        _, a1, _, _ = self._two_turns()
        with self.assertRaises(ValueError):
            self.storage.delete_message(self.cid, a1, "turn-all")

    def test_message_from_other_conversation_is_not_found(self):
        other = self.storage.create_conversation(title="别的会话")
        foreign = self.storage.add_message(other["id"], "user", "外来消息")["id"]
        with self.assertRaises(LookupError):
            self.storage.delete_message(self.cid, foreign, "single")
        self.assertEqual(len(self._rows()), 0, "跨会话 id 不能误删自己这边的东西")

    def test_delete_advances_updated_at(self):
        _, a1, _, _ = self._two_turns()
        self._freeze_updated_at(1)  # 冻结成远古值，确保推进可观测
        result = self.storage.delete_message(self.cid, a1, "single")
        self.assertEqual(result["updated_at"], self._live_updated_at())
        self.assertGreater(self._live_updated_at(), 1)


class DeleteTurnModeTests(MessageDeleteTestCase):
    def test_turn_removes_user_and_its_assistant(self):
        u1, a1, u2, a2 = self._two_turns()
        result = self.storage.delete_message(self.cid, u1, "turn")
        self.assertEqual([m["id"] for m in self._rows()], [u2, a2])
        self.assertEqual(result["removed"][0]["id"], u1, "快照按原文顺序，首个是被点的 user")

    def test_turn_stops_at_next_user(self):
        u1, a1, u2, a2 = self._two_turns()
        a3 = self._add("assistant", "追加的连续回复")
        result = self.storage.delete_message(self.cid, u1, "turn")
        # 第一轮 = u1 + a1；遇到 u2 就停，后面那串 assistant 一律不动。
        self.assertEqual([s["id"] for s in result["removed"]], [u1, a1])
        self.assertEqual([m["id"] for m in self._rows()], [u2, a2, a3])

    def test_turn_takes_legacy_session_row_along(self):
        u1 = self._add("user", "第一轮问题")
        self._add("assistant", "第一轮回答")
        session_id = self._add("session", "")
        u2 = self._add("user", "第二轮问题")
        result = self.storage.delete_message(self.cid, u1, "turn")
        self.assertIn(session_id, [s["id"] for s in result["removed"]],
                      "本轮痕迹里的遗留标记行要一起带走")
        self.assertEqual([m["id"] for m in self._rows()], [u2])

    def test_turn_rejected_for_assistant(self):
        _, a1, _, _ = self._two_turns()
        with self.assertRaises(ValueError):
            self.storage.delete_message(self.cid, a1, "turn")
        self.assertEqual(len(self._rows()), 4, "拒绝即不落删")

    def test_turn_on_tail_user_removes_rest(self):
        _, _, u2, a2 = self._two_turns()
        result = self.storage.delete_message(self.cid, u2, "turn")
        self.assertEqual([s["id"] for s in result["removed"]], [u2, a2])
        self.assertEqual(len(self._rows()), 2, "前面的轮次不受影响")

    def test_turn_removes_metadata_bearing_assistant(self):
        u1, a1, _, _ = self._two_turns()
        result = self.storage.delete_message(self.cid, u1, "turn")
        self.assertEqual(len(result["removed"]), 2)
        removed = {s["id"]: s for s in result["removed"]}
        self.assertIn("想了一下", removed[a1]["metadata_json"], "带 reasoning 的回复也要完整入快照")


class RestoreTests(MessageDeleteTestCase):
    def test_snapshot_is_byte_complete(self):
        _, a1, _, _ = self._two_turns()
        before = next(m for m in self._rows() if m["id"] == a1)
        result = self.storage.delete_message(self.cid, a1, "single")
        snapshot = result["removed"][0]
        for key in ("id", "role", "content", "metadata_json", "created_at"):
            with self.subTest(key=key):
                self.assertIn(key, snapshot, "快照缺字段就没法原样插回")
        self.assertEqual(snapshot["id"], a1)
        self.assertEqual(snapshot["role"], before["role"])
        self.assertEqual(snapshot["content"], before["content"])
        self.assertEqual(snapshot["created_at"], before["created_at"])

    def test_restore_restores_order_and_content(self):
        u1, a1, u2, a2 = self._two_turns()
        result = self.storage.delete_message(self.cid, u2, "turn")
        restored = self.storage.restore_messages(self.cid, result["removed"])
        self.assertEqual(restored["count"], 2)
        self.assertEqual(restored["skipped"], [])
        self.assertEqual([m["id"] for m in self._rows()], [u1, a1, u2, a2])

    def test_restore_keeps_metadata_byte_identical(self):
        _, a1, _, _ = self._two_turns()
        self.storage.delete_message(self.cid, a1, "single")
        self.storage.restore_messages(self.cid, [
            s for s in [{"id": a1, "role": "assistant", "content": "第一轮回答",
                         "metadata_json": '{"reasoning": "想了一下"}', "created_at": 123}]
        ])
        row = next(m for m in self._rows() if m["id"] == a1)
        self.assertEqual(row["metadata"], {"reasoning": "想了一下"},
                         "metadata_json 原文本写回，不做二次序列化")

    def test_restore_is_idempotent(self):
        _, a1, _, _ = self._two_turns()
        result = self.storage.delete_message(self.cid, a1, "single")
        first = self.storage.restore_messages(self.cid, result["removed"])
        second = self.storage.restore_messages(self.cid, result["removed"])
        self.assertEqual(first["count"], 1)
        self.assertEqual(second["count"], 0)
        self.assertEqual(second["skipped"], [a1], "重复点撤销不能报错也不能翻倍")
        self.assertEqual(len([m for m in self._rows() if m["id"] == a1]), 1)

    def test_restore_requires_non_empty_snapshots(self):
        with self.assertRaises(ValueError):
            self.storage.restore_messages(self.cid, [])
        with self.assertRaises(ValueError):
            self.storage.restore_messages(self.cid, None)

    def test_restore_unknown_conversation_raises_lookup(self):
        _, a1, _, _ = self._two_turns()
        result = self.storage.delete_message(self.cid, a1, "single")
        with self.assertRaises(LookupError):
            self.storage.restore_messages("no-such-conversation", result["removed"])

    def test_restore_rejects_snapshot_without_valid_role(self):
        _, a1, _, _ = self._two_turns()
        self.storage.delete_message(self.cid, a1, "single")
        with self.assertRaises(ValueError):
            self.storage.restore_messages(self.cid, [{"id": "x", "role": "hacker"}])

    def test_restore_derives_metadata_json_when_absent(self):
        """前端只回传 ``metadata`` 字典时，落盘也要等价（缺 metadata_json 的兜底分支）。"""
        self._add("user", "占位")
        self.storage.restore_messages(self.cid, [
            {"id": "derived-1", "role": "user", "content": "新内容",
             "metadata": {"attachments": [{"name": "a.png"}]}, "created_at": 999},
        ])
        row = next(m for m in self._rows() if m["id"] == "derived-1")
        self.assertEqual(row["metadata"]["attachments"], [{"name": "a.png"}])
        self.assertEqual(row["created_at"], 999)

    def test_restore_advances_updated_at(self):
        _, a1, _, _ = self._two_turns()
        result = self.storage.delete_message(self.cid, a1, "single")
        self._freeze_updated_at(1)
        restored = self.storage.restore_messages(self.cid, result["removed"])
        self.assertGreater(restored["updated_at"], 1)
        self.assertEqual(restored["updated_at"], self._live_updated_at())


class PrefixCacheStabilityTests(MessageDeleteTestCase):
    """本功能最贵的一条：删了又撤销，模型请求必须逐字节不变。"""

    def test_history_is_byte_identical_after_delete_and_restore(self):
        self._add("user", "带图的问题", {"attachments": [{"name": "shot.png", "path": "a.png"}]})
        self._add("assistant", "第一轮回答", {"reasoning": "推理原文", "usage": {"prompt_tokens": 12}})
        self._add("user", "第二轮问题")
        self._add("assistant", "第二轮回答")
        target = self._rows()[1]["id"]

        before = self._history()
        frozen = json.dumps(before, ensure_ascii=False, sort_keys=True)

        result = self.storage.delete_message(self.cid, target, "single")
        self.assertNotEqual(
            json.dumps(self._history(), ensure_ascii=False, sort_keys=True), frozen,
            "删除本身当然会改变上下文（否则这个测试就是空的）",
        )

        self.storage.restore_messages(self.cid, result["removed"])
        after = self._history()
        self.assertEqual(
            json.dumps(after, ensure_ascii=False, sort_keys=True), frozen,
            "撤销后历史必须逐字节一致 —— 否则前缀缓存整体失效",
        )

    def test_turn_delete_and_restore_round_trips_history(self):
        self._two_turns()
        u1 = self._rows()[0]["id"]
        frozen = json.dumps(self._history(), ensure_ascii=False, sort_keys=True)
        result = self.storage.delete_message(self.cid, u1, "turn")
        self.storage.restore_messages(self.cid, result["removed"])
        self.assertEqual(json.dumps(self._history(), ensure_ascii=False, sort_keys=True), frozen)

    def test_session_start_survives_round_trip(self):
        u1, a1, u2, a2 = self._two_turns()
        self.storage.set_session_start(self.cid, a1, note="切一下")
        frozen = json.dumps(self._history(), ensure_ascii=False, sort_keys=True)
        result = self.storage.delete_message(self.cid, a1, "single")
        self.storage.restore_messages(self.cid, result["removed"])
        self.assertEqual(
            json.dumps(self._history(), ensure_ascii=False, sort_keys=True), frozen,
            "分割线标记写在 metadata 里，撤销必须一并还原",
        )
        self.assertIn("session_start", json.dumps(self._rows(), ensure_ascii=False))


class DeleteDialogSourceTests(unittest.TestCase):
    """前端确认框的粒度契约（源码级护栏 —— 这里曾真出过一个 bug）。

    事故：`askDeleteScope` 曾把两个按钮都写成 `hidden = !isUser`，于是**回复一条都删不掉**
    （弹窗只剩「取消」）。契约是：「仅删这一条」对 user / assistant 都合法；
    「连同 AI 回复整轮删除」只对 user 开放（删掉提问比删掉回答危险得多）。
    """

    def setUp(self):
        self.js = (ROOT / "public" / "js" / "04-messages.js").read_text(encoding="utf-8")
        self.html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")

    def _ask_body(self):
        start = self.js.index("function askDeleteScope(")
        return self.js[start:start + 1600]

    def test_single_option_always_available(self):
        body = self._ask_body()
        self.assertIn("singleButton.hidden = false", body,
                      "「仅删这一条」必须对所有角色可见，否则弹窗只剩取消")

    def test_turn_option_is_user_only(self):
        body = self._ask_body()
        self.assertIn("turnButton.hidden = !isUser", body,
                      "「整轮删除」只对 user 开放")

    def test_both_options_exist_in_dialog(self):
        for marker in ('id="messageDeleteSingle"', 'id="messageDeleteTurn"', 'id="messageDeleteCancel"'):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.html)

    def test_assistant_and_user_rows_both_have_delete_button(self):
        self.assertIn('data-delete-message title="删除这条提问', self.js)
        self.assertIn('data-delete-message title="删除这条回复', self.js)
        self.assertIn("复制</button>${regenerateButton}${deleteButton}${sessionButton}", self.js,
                      "AI 操作区顺序：复制 → 重新生成 → 删除 → 新会话")
        # 用户消息操作区是内联拼接：编辑 → 分支 → 删除，删除排最后（破坏性最强）。
        self.assertIn(
            "'<button data-edit-message title=\"编辑这条提问并从这里重新发送（其后的消息会被删除）\">编辑</button>'"
            "\n        + '<button data-branch-message title=\"从这条消息分支到新会话继续\">分支</button>'"
            "\n        + '<button data-delete-message title=\"删除这条提问",
            self.js,
            "用户操作区顺序：编辑 → 分支 → 删除",
        )

    def test_undo_window_is_ten_seconds(self):
        self.assertIn("const UNDO_WINDOW_MS = 10000", self.js)
        self.assertIn("window.setTimeout(() => hideDeleteUndo(), UNDO_WINDOW_MS)", self.js)

    def test_undo_snapshot_is_memory_only(self):
        """快照只存内存：刷新即失效，所以按钮文案必须写明。"""
        self.assertIn("deleteUndoState = null", self.js)
        self.assertIn("刷新页面后不可撤销", self.html)


class DeleteRouteTests(unittest.TestCase):
    """路由接线 + app 层校验映射。"""

    def test_routes_registered(self):
        http = (ROOT / "naiba" / "http.py").read_text(encoding="utf-8")
        self.assertIn('path == "/api/messages/delete"', http)
        self.assertIn('path == "/api/messages/restore"', http)
        self.assertIn("api_delete_message", http)
        self.assertIn("api_restore_messages", http)

    def _app(self, tmp):
        from types import SimpleNamespace

        return SimpleNamespace(storage=ChatStorage(Path(tmp) / "chat.db"))

    def test_delete_missing_fields_is_400(self):
        from naiba.app import NaibaChatApp

        with tempfile.TemporaryDirectory(prefix="naiba_del_route_") as tmp:
            app = self._app(tmp)
            payload, status = NaibaChatApp.api_delete_message(app, {"conversation_id": "c"})
            self.assertEqual(status, 400)
            self.assertIn("error", payload)

    def test_delete_unknown_message_is_404(self):
        from naiba.app import NaibaChatApp

        with tempfile.TemporaryDirectory(prefix="naiba_del_route2_") as tmp:
            app = self._app(tmp)
            conversation = app.storage.create_conversation(title="甲")
            payload, status = NaibaChatApp.api_delete_message(
                app, {"conversation_id": conversation["id"], "message_id": "nope"}
            )
            self.assertEqual(status, 404)
            self.assertIn("error", payload)

    def test_delete_session_row_is_400(self):
        from naiba.app import NaibaChatApp

        with tempfile.TemporaryDirectory(prefix="naiba_del_route3_") as tmp:
            app = self._app(tmp)
            conversation = app.storage.create_conversation(title="甲")
            session_id = app.storage.add_message(
                conversation["id"], "session", ""
            )["id"]
            payload, status = NaibaChatApp.api_delete_message(
                app, {"conversation_id": conversation["id"], "message_id": session_id}
            )
            self.assertEqual(status, 400, "语义错误（ValueError）→ 400，不是 404")

    def test_restore_bad_payload_is_400(self):
        from naiba.app import NaibaChatApp

        with tempfile.TemporaryDirectory(prefix="naiba_del_route4_") as tmp:
            app = self._app(tmp)
            for body in ({"conversation_id": "c"}, {"messages": []}, {"messages": "not-a-list"}):
                with self.subTest(body=body):
                    payload, status = NaibaChatApp.api_restore_messages(app, body)
                    self.assertEqual(status, 400)
                    self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()
