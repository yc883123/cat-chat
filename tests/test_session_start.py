# -*- coding: utf-8 -*-
"""护栏：「新会话」分割线（同一会话窗口内裁掉前缀上下文）。

语义：在某条消息的 metadata 上打 `session_start` 标记 = **新会话从这条消息之后开始**——
1. `build_model_history` 遇到该标记即清空此前历史，该消息本身也不进新上下文；
2. 允许多条标记（最新的那条决定当前上下文起点），聊天记录一条不删；
3. 前端在该消息正下方渲染分割线，可逐条撤销。
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core.history import build_model_history  # noqa: E402
from naiba.core.messages import MESSAGE_METADATA_KEYS, MetadataKeys  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402


def _flag(at: int = 1789000000000, **extra) -> dict:
    return {"session_start": {"at": at, "source": "manual", **extra}}


def _assistant(content: str, metadata: dict | None = None) -> dict:
    return {"role": "assistant", "content": content, "metadata": metadata or {}}


class SessionStartContractTests(unittest.TestCase):
    def test_metadata_key_registered(self):
        self.assertEqual(MetadataKeys.SESSION_START, "session_start")
        self.assertIn("session_start", MESSAGE_METADATA_KEYS, "新增 metadata 键必须登记契约表")
        from naiba.core.contracts import MetadataKeys as Reexported

        self.assertEqual(Reexported.SESSION_START, "session_start", "契约 re-export 不能漏")


class SessionStartHistoryTests(unittest.TestCase):
    """重放侧：标记所在消息及其之前的消息都不进模型请求。"""

    def test_history_cut_after_marked_assistant(self):
        history = build_model_history([
            {"role": "user", "content": "旧问题"},
            _assistant("旧回答", _flag()),
            {"role": "user", "content": "新问题"},
            _assistant("新回答"),
        ])
        self.assertEqual(
            [(m["role"], m["content"]) for m in history],
            [("user", "新问题"), ("assistant", "新回答")],
            "标记所在消息与其之前的内容必须被裁掉，标记之后的消息保留",
        )

    def test_last_marker_wins_with_multiple(self):
        history = build_model_history([
            {"role": "user", "content": "第一段"},
            _assistant("第一答", _flag(at=1)),
            {"role": "user", "content": "第二段"},
            _assistant("第二答", _flag(at=2)),
            {"role": "user", "content": "第三段"},
        ])
        self.assertEqual([m["content"] for m in history], ["第三段"], "多条标记取最后一条")

    def test_marker_on_last_message_yields_empty_history(self):
        history = build_model_history([
            {"role": "user", "content": "旧问题"},
            _assistant("旧回答", _flag()),
        ])
        self.assertEqual(history, [], "标记在最后一条 → 当前上下文为空（下一条消息即新会话起点）")

    def test_legacy_marker_row_still_supported(self):
        """早期版本用独立 role=session 标记行；旧数据必须继续被裁剪（向后兼容路径）。"""
        history = build_model_history([
            {"role": "user", "content": "旧问题"},
            {"role": "session", "content": "", "metadata": _flag()},
            {"role": "user", "content": "新问题"},
        ])
        self.assertEqual([m["content"] for m in history], ["新问题"])

    def test_trace_replay_still_works_after_marker(self):
        history = build_model_history([
            {"role": "user", "content": "旧"},
            _assistant("旧答", _flag()),
            {"role": "user", "content": "新"},
            {"role": "assistant", "content": "答复", "metadata": {
                "trace": [
                    {"role": "assistant", "content": "", "tool_calls": [
                        {"id": "c1", "name": "pwsh", "arguments": {"command": "dir"}},
                    ]},
                    {"role": "tool", "tool_call_id": "c1", "name": "pwsh", "content": "ok"},
                    {"role": "assistant", "content": "答复"},
                ],
            }},
        ])
        self.assertEqual(history[0]["content"], "新")
        self.assertEqual(history[1]["tool_calls"][0]["id"], "c1")
        self.assertEqual(history[2]["role"], "tool")
        self.assertEqual(history[3]["content"], "答复")


class SessionStartStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.conversation = self.storage.create_conversation()
        self.conversation_id = self.conversation["id"]
        self.storage.add_message(self.conversation_id, "user", "旧问题")
        self.anchor = self.storage.add_message(self.conversation_id, "assistant", "旧回答",
                                               {"usage": {"total_tokens": 1}})

    def tearDown(self):
        self.tmp.cleanup()

    def _messages(self):
        return self.storage.get_conversation(self.conversation_id)["messages"]

    def test_set_marker_on_message_keeps_everything_else(self):
        before_updated = self.storage.get_conversation(self.conversation_id)["updated_at"]
        result = self.storage.set_session_start(self.conversation_id, self.anchor["id"], note="交接完成")
        self.assertIsNotNone(result)
        messages = self._messages()
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"], "不新增行、不删消息")
        info = messages[1]["metadata"]["session_start"]
        self.assertEqual(info["source"], "manual")
        self.assertEqual(info["note"], "交接完成")
        self.assertTrue(info["at"])
        self.assertEqual(messages[1]["metadata"]["usage"], {"total_tokens": 1}, "其余 metadata 保留")
        self.assertGreaterEqual(self.storage.get_conversation(self.conversation_id)["updated_at"],
                                before_updated, "落标记要推进 updated_at")
        self.assertEqual(build_model_history(messages), [], "标记之后没有消息 → 上下文为空")

    def test_set_marker_unknown_message_returns_none(self):
        self.assertIsNone(self.storage.set_session_start(self.conversation_id, "不存在的消息"))

    def test_multiple_markers_coexist_and_last_wins(self):
        self.storage.add_message(self.conversation_id, "user", "新问题")
        third = self.storage.add_message(self.conversation_id, "assistant", "新回答")
        self.storage.set_session_start(self.conversation_id, self.anchor["id"])
        self.storage.set_session_start(self.conversation_id, third["id"])
        self.storage.add_message(self.conversation_id, "user", "再问")
        messages = self._messages()
        flagged = [m["content"] for m in messages if (m["metadata"] or {}).get("session_start")]
        self.assertEqual(flagged, ["旧回答", "新回答"], "允许保留多条分割线")
        self.assertEqual([m["content"] for m in build_model_history(messages)], ["再问"],
                         "实际生效的是最后一条分割线")

    def test_clear_marker_restores_context(self):
        self.storage.set_session_start(self.conversation_id, self.anchor["id"])
        new_message = self.storage.add_message(self.conversation_id, "user", "新问题")
        self.assertTrue(self.storage.clear_session_start(self.anchor["id"]))
        messages = self._messages()
        self.assertNotIn("session_start", messages[1]["metadata"])
        self.assertEqual(messages[1]["metadata"]["usage"], {"total_tokens": 1}, "其余 metadata 不动")
        self.assertEqual([m["content"] for m in build_model_history(messages)], ["旧问题", "旧回答", "新问题"])
        self.assertFalse(self.storage.clear_session_start(self.anchor["id"]), "重复撤销返回 False")
        self.assertFalse(self.storage.clear_session_start(new_message["id"]), "没标记的消息返回 False")

    def test_marker_is_copied_by_branch(self):
        self.storage.set_session_start(self.conversation_id, self.anchor["id"])
        branch_point = self.storage.add_message(self.conversation_id, "user", "新问题")
        branch = self.storage.branch_conversation(self.conversation_id, branch_point["id"])
        messages = self.storage.get_conversation(branch["conversation"]["id"])["messages"]
        self.assertTrue(any((m["metadata"] or {}).get("session_start") for m in messages),
                        "分割线随消息 metadata 一起复制")
        self.assertEqual(build_model_history(messages), [], "分支同样从分割线之后重算")

    def test_legacy_marker_row_deletion(self):
        row = self.storage.add_message(self.conversation_id, "session", "", _flag())
        self.assertTrue(self.storage.delete_session_start(row["id"]))
        self.assertEqual([m["role"] for m in self._messages()], ["user", "assistant"])
        self.assertFalse(self.storage.delete_session_start(row["id"]))
        self.assertFalse(self.storage.delete_session_start(self.anchor["id"]), "非 session 行不可删")


class SessionStartFrontendTests(unittest.TestCase):
    """前端契约：入口在 AI 回复「复制」右侧、分割线渲染在锚点下方、可多条、可撤销。"""

    def _read(self, name: str) -> str:
        return (ROOT / "public" / "js" / name).read_text(encoding="utf-8")

    def test_button_sits_next_to_copy_in_assistant_actions(self):
        js = self._read("04-messages.js")
        self.assertIn('data-session-start-after=', js)
        self.assertIn('<button data-copy-message>复制</button>${regenerateButton}${deleteButton}${sessionButton}', js,
                      "AI 操作区顺序必须是 复制 → 重新生成 → 删除 → 新会话")
        self.assertIn("(!temporary && message.id)", js, "只有已落库的完整回复才有入口")
        html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('id="newSessionButton"', html, "输入区底部的旧入口已移除")
        css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
        for rule in (".message-row.session-divider", ".session-divider-bar", ".session-divider-label",
                     ".session-divider-cancel", ".session-divider-hint",
                     ".message-actions [data-session-start-after]:hover",
                     "#messages.conversation-running .message-actions [data-session-start-after]"):
            with self.subTest(rule=rule):
                self.assertIn(rule, css)

    def test_divider_renders_below_anchor_and_supports_many(self):
        js = self._read("04-messages.js")
        self.assertIn("export function sessionDividerAfter(message)", js)
        self.assertIn("element.insertAdjacentElement('afterend', divider)", js,
                      "终态渲染路径也要把分割线补在锚点之后")
        render = js[js.index("export function renderMessages(messages)"):]
        render = render[: render.index("updateContextUsage(list)")]
        self.assertIn("messageRangeFragment(list, state.renderStart, list.length)", render,
                      "历史渲染路径要按窗口逐段渲染（懒加载）")
        self.assertIn("sessionDividerAfter(message)", js, "分段渲染里分割线紧跟锚点")
        self.assertIn("此线以上不再进入模型上下文", js, "分割线文案说明裁剪方向")
        self.assertNotIn("MAX_SESSION_DIVIDERS", js, "分割线条数不设上限")

    def test_actions_call_api_and_refresh(self):
        js = self._read("04-messages.js")
        for snippet in (
            "export async function startNewSession(afterMessageId)",
            "export async function cancelSessionStart(messageId)",
            "after_message_id: afterMessageId",
            "/api/session_start/",
            "await syncCurrentConversation()",
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, js)
        start = js[js.index("export async function startNewSession("):]
        start = start[: start.index("\n}")]
        self.assertIn("state.abortController", start, "运行中不允许划分割线（与「分支」同口径）")
        binds = self._read("15-bind-events.js")
        self.assertIn("data-session-start-after", binds)
        self.assertIn("data-cancel-session-start", binds)
        self.assertNotIn("#newSessionButton", binds, "旧入口绑定已移除")

    def test_backend_routes_match_frontend_paths(self):
        http = (ROOT / "naiba" / "http.py").read_text(encoding="utf-8")
        self.assertIn('path.endswith("/session_start")', http)
        self.assertIn('"after_message_id"', http, "POST 必须按锚点消息定位")
        self.assertIn('path.startswith("/api/session_start/")', http)


if __name__ == "__main__":
    unittest.main()
