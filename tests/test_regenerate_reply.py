# -*- coding: utf-8 -*-
"""护栏：AI 回复「重新生成」 + 用户消息「编辑」回归。

语义：第 N 轮 `U1 → A1 … UN → AN`，点 `AN` 的「重新生成」时——

   截断点 = UN（那条用户消息本身，不是 AI 回复）
   删除 UN 及其之后 → 原样重发 UN' → 新 AN' 落回同一位置
   请求前缀 = system + U1, A1, …, U(N-1), A(N-1), UN'   （不含原 AN）

截断点选 UN 而不是 AN，是为了反复点击不累积重复提问；且 U1..A(N-1) 字节级不动，
前缀缓存照常命中（这是重发最省钱的理由）。

「重新生成」= 「编辑」的不改字版本，两者截断位置完全相同，因此共用 resendFromUserMessage。
后端零改动，复用 POST /api/messages/edit。
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.storage.store import ChatStorage  # noqa: E402


def _read(*parts: str) -> str:
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def _fn_body(src: str, marker: str) -> str:
    """取某个顶层函数/导出函数的正文（到列 0 的 `}` 为止）。"""
    start = src.index(marker)
    return src[start:src.index("\n}\n", start)]


class RegenerateFrontendSourceTests(unittest.TestCase):
    """前端源码守门：入口存在、顺序正确、共用函数被复用。"""

    def setUp(self):
        self.messages_js = _read("public", "js", "04-messages.js")
        self.bind_js = _read("public", "js", "15-bind-events.js")
        self.css = _read("public", "styles.css")

    def test_user_actions_expose_edit_and_branch(self):
        self.assertIn('data-edit-message', self.messages_js, "用户消息操作区必须回归「编辑」")
        self.assertIn('data-branch-message', self.messages_js, "「分支」保留")
        self.assertIn('<button data-edit-message', self.messages_js)
        self.assertIn('<button data-branch-message', self.messages_js)
        # 顺序：编辑在前（更常用），分支在后
        edit_at = self.messages_js.index('<button data-edit-message')
        branch_at = self.messages_js.index('<button data-branch-message')
        self.assertLess(edit_at, branch_at, "用户操作区顺序应为 编辑 → 分支")

    def test_assistant_actions_order_copy_regenerate_session(self):
        self.assertIn('data-regenerate-message=', self.messages_js, "AI 回复要有「重新生成」")
        self.assertIn(
            '<button data-copy-message>复制</button>${regenerateButton}${sessionButton}',
            self.messages_js,
            "AI 操作区顺序必须是 复制 → 重新生成 → 新会话",
        )
        self.assertIn("(!temporary && message.id)", self.messages_js,
                      "「重新生成」显示条件与「新会话」同口径：已落库的完整回复才有")

    def test_regenerate_and_edit_share_one_resend_helper(self):
        self.assertIn("export async function resendFromUserMessage(", self.messages_js,
                      "截断→回填→发送 必须抽成共用函数")
        self.assertIn("export async function regenerateMessage(", self.messages_js)
        # confirmEditMessage 也必须走同一个函数，避免两条重发路径漂移
        edit_body_start = self.messages_js.index("export async function confirmEditMessage(")
        edit_body = self.messages_js[edit_body_start:edit_body_start + 600]
        self.assertIn("resendFromUserMessage(", edit_body,
                      "「编辑」与「重新生成」必须共用同一个重发函数")

    def test_regenerate_reads_display_content_not_content(self):
        """原文必须取 metadata.display_content（含 /ref），否则引用会被静默丢掉。"""
        start = self.messages_js.index("export async function regenerateMessage(")
        body = self.messages_js[start:start + 2500]
        self.assertIn("metadata.display_content", body)
        self.assertIn("role === 'user'", body, "往前找最近一条用户消息推导截断点")
        self.assertIn("state.messages", body, "从 state.messages 推导（懒加载下 DOM 未必有那条提问）")

    def test_guards_against_running_conversation(self):
        start = self.messages_js.index("export async function regenerateMessage(")
        body = self.messages_js[start:start + 700]
        self.assertIn("state.chatRunId || state.abortController", body,
                      "回答进行中不允许重新生成")

    def test_click_delegation_wired(self):
        self.assertIn("regenerateMessage", self.bind_js, "15-bind-events.js 必须 import 并委托")
        self.assertIn("closest('[data-regenerate-message]')", self.bind_js)
        self.assertIn("closest('[data-edit-message]')", self.bind_js, "「编辑」委托原本就在，不能丢")

    def test_css_hover_and_running_hide(self):
        for rule in (".message-actions [data-edit-message]:hover",
                     ".message-actions [data-regenerate-message]:hover",
                     "#messages.conversation-running .message-actions [data-edit-message]",
                     "#messages.conversation-running .message-actions [data-regenerate-message]"):
            with self.subTest(rule=rule):
                self.assertIn(rule, self.css)

    def test_edit_backend_still_present(self):
        """防止有人把「没触发者」的编辑后端当死代码顺手删掉。"""
        app = _read("naiba", "app.py")
        http = _read("naiba", "http.py")
        self.assertIn("def _edit_message(", app)
        self.assertIn('"/api/messages/edit"', http)

    def test_attachment_only_round_can_be_resent(self):
        """纯附件轮次（无文字）也要能编辑/重新生成。

        放行口径必须与 sendChatMessage / submit_chat 一致：**文字与可用附件至少有一个**。
        否则用户只发了图片的那一轮，点「重新生成」会被前置校验直接拒掉。
        """
        start = self.messages_js.index("export async function resendFromUserMessage(")
        body = self.messages_js[start:start + 1000]
        self.assertIn("!content && !knownAttachments.length", body,
                      "只在「既无文字又无附件」时才拒绝")
        self.assertIn("attachments = []", self.messages_js, "重发入口要能收到该轮的附件清单")
        self.assertIn("attachments: metadata.attachments", self.messages_js,
                      "「重新生成」要把该条用户消息的附件带进重发")

    def test_references_survive_the_composer_handoff(self):
        """引用不能在路上丢：重发是把文本交给底部输入框，必须走同一条输入管线。"""
        start = self.messages_js.index("export async function resendFromUserMessage(")
        body = self.messages_js[start:start + 2200]
        for call in ("resizeTextarea()", "renderInputMirror()", "updateSkillPopup()"):
            with self.subTest(call=call):
                self.assertIn(call, body, "程序化改输入框后必须刷新输入管线（含发送按钮）")
        self.assertIn("notifyComposerChanged", body)

    def test_edit_restores_attachments_with_same_fields_as_branch(self):
        """重发恢复附件时字段口径必须与「分支」一致（name/path/size/thumb_path）。

        少带 `thumb_path` 时渲染层会退化成推导 `<path>_thumb.webp`：缩略图与原件不同目录
        （例如拖拽进来的文件、旧数据目录迁移过的记录）就 404 破图。两条路径不能各写一套。
        """
        resend_body = _fn_body(self.messages_js, "export async function resendFromUserMessage(")
        self.assertIn("result.attachments", resend_body, "附件由 /api/messages/edit 回传，不靠前端猜")
        self.assertIn("state.pendingFiles", resend_body)
        branch_start = self.messages_js.index("export async function branchMessage(")
        branch_body = self.messages_js[branch_start:branch_start + 2500]
        self.assertIn("thumb_path", branch_body, "「分支」是参照实现，必须带 thumb_path")
        self.assertIn("thumb_path", resend_body,
                      "「编辑/重新生成」的附件字段必须与「分支」同口径（含 thumb_path）")

    def test_edit_box_shows_original_attachments(self):
        """编辑富消息时，完整 composer 携带原附件进入消息气泡。"""
        body = _fn_body(self.messages_js, "export function startEditMessage(")
        self.assertIn("composer-wrap", self.messages_js, "编辑必须移动完整 composer-wrap")
        self.assertIn(".composer-wrap", self.messages_js, "编辑必须复用完整 composer 表单")
        self.assertIn("pendingFiles", self.messages_js, "编辑附件必须进入统一 pendingFiles 列表")
        self.assertIn("row.__messageMetadata?.attachments", body,
                      "附件来源是同一条消息的 metadata，不是 DOM 反推")
        self.assertNotIn("uploadedFileMarkup(attachments)", self.messages_js,
                         "编辑附件不能只读展示，必须复用可删除 pendingFiles")


class EditComposerBridgeSourceTests(unittest.TestCase):
    """编辑态移动完整 composer：底部隐藏、气泡内复用同一输入管线。"""

    def setUp(self):
        self.messages_js = _read("public", "js", "04-messages.js")
        self.media_js = _read("public", "js", "03-media.js")
        self.bind_js = _read("public", "js", "15-bind-events.js")
        self.chat_js = _read("public", "js", "12-chat-input.js")
        self.core_js = _read("public", "js", "01-core.js")
        self.css = _read("public", "styles.css")

    def test_editing_state_registered_on_global_state(self):
        """态挂 `state` 而不是 04-messages 的模块变量：03-media/15 只读消费，避免 03↔04 循环 import。"""
        for key in ("editingMessageId:",):
            with self.subTest(key=key):
                self.assertIn(key, self.core_js)

    def test_apply_editing_state_owns_the_transition(self):
        body = _fn_body(self.messages_js, "function applyEditingState(row)")
        self.assertIn("state.editingMessageId", body)
        self.assertIn("is-editing-message", self.messages_js, "编辑态需切 body class")
        self.assertIn("composer-wrap", self.messages_js, "状态切换要控制完整 composer-wrap 位置")
        self.assertIn("hidden", self.messages_js, "底部 composer 需要隐藏占位")

    def test_send_button_becomes_resend_while_editing(self):
        body = _fn_body(self.media_js, "export function updateSendButtonState()")
        self.assertIn("state.editingMessageId", body, "编辑态下发送按钮要有独立分支")
        self.assertIn("重新发送", body)
        self.assertIn("state.pendingFiles", body, "编辑附件应复用 pendingFiles 可用性")

    def test_composer_is_hidden_at_bottom_while_editing(self):
        self.assertIn("composer-wrap", self.messages_js)
        self.assertIn("hidden", self.messages_js)
        self.assertIn("is-editing-message", self.css)

    def test_bottom_submit_and_enter_confirm_the_edit(self):
        self.assertIn("confirmActiveEdit", self.bind_js)
        self.assertGreaterEqual(self.bind_js.count("confirmActiveEdit()"), 2,
                                "表单提交与回车两条路径都要改走确认编辑")
        body = _fn_body(self.messages_js, "export function confirmActiveEdit()")
        self.assertIn("submitEdit(", body)
        self.assertIn("applyEditingState(null)", body, "确认时退出编辑态并恢复底部 composer")

    def test_set_busy_no_longer_writes_input_state(self):
        """输入框状态是单点写入：setBusy 再直接改 disabled/placeholder 就会覆盖编辑态。"""
        body = _fn_body(self.chat_js, "export function setBusy(")
        self.assertNotIn("messageInput.disabled", body)
        self.assertNotIn(".placeholder =", body)

    def test_editing_state_survives_same_conversation_rerender(self):
        """重渲染不再无条件退编辑态。

        轮询 syncCurrentConversation（后台任务写消息、流式落库都会引爆）走的就是
        renderMessages；改前那里无条件 applyEditingState(null)，一次刷新就会静默吃掉编辑现场
        （改到一半的字掉回底部、附件丢失、按钮从「重新发送」变回「发送」）。
        现在的契约：同会话且被编辑消息还在 → 保现场复挂；消息没了/换会话 → 退出且**必还草稿**。
        """
        body = _fn_body(self.messages_js, "export function renderMessages(")
        self.assertIn("prepareEditRerender(", body)
        self.assertIn("resumeEditComposer(", body)
        self.assertNotIn("applyEditingState(null)", body,
                         "不能无条件退编辑态（那会静默吃掉编辑中的文字与附件）")
        prep = _fn_body(self.messages_js, "function prepareEditRerender(")
        self.assertIn("switched", prep, "必须区分「同会话刷新」与「切换会话」")
        self.assertIn("restoreInlineComposer({ resume: true })", prep,
                      "同会话刷新要先归位、保 stash，渲染后再复挂")
        self.assertNotIn("restoreDraft", prep, "保现场分支不得还原/丢弃草稿")
        exit_body = _fn_body(self.messages_js, "function exitEditWithDraft()")
        self.assertIn("restoreDraft: true", exit_body, "强制退出必须把底部草稿还回去")
        self.assertIn("applyEditingState(null)", exit_body)

    def test_mount_edit_composer_is_shared_by_both_paths(self):
        """首次进入编辑与重渲染后复挂必须走同一个挂载函数，避免两条路径逻辑漂移。

        另钉住一个派生坑：编辑期间底部留着一个 `composer-wrap edit-placeholder` 空锚点，
        且它被插在真 composer **前面**，所以复挂时不能再查一次 `.composer-wrap`。
        """
        start = _fn_body(self.messages_js, "export function startEditMessage(")
        self.assertIn("mountEditComposer(row, { wrap, text: currentText, files })", start)
        self.assertIn("composer-wrap:not(.edit-placeholder)", start,
                      "取真 composer 必须排除占位锚点")
        resume = _fn_body(self.messages_js, "function resumeEditComposer(snapshot)")
        self.assertIn("mountEditComposer(row, { wrap: state.editComposerStash.wrap", resume)
        self.assertIn("ensureMessageRendered(", resume, "懒加载窗口里没有该行时先扩窗口")
        self.assertIn("exitEditWithDraft()", resume, "实在挂不上就退出编辑，草稿仍不丢")

    def test_pending_files_move_with_composer_and_remain_editable(self):
        self.assertIn("pendingFiles", self.messages_js)
        self.assertIn("renderPendingFiles", self.messages_js)
        self.assertIn("data-remove-file", self._read_upload())

    def _read_upload(self):
        return _read("public", "js", "10-upload.js")


class RegenerateTruncationTests(unittest.TestCase):
    """截断语义：从 UN 截断后，只剩 U1..A(N-1)；重发 UN' 即得完整的一轮。

    这是「反复点重新生成不累积重复提问」与「前缀缓存命中」两条结论的存储层依据。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.conversation_id = self.storage.create_conversation()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _messages(self):
        return self.storage.get_conversation(self.conversation_id)["messages"]

    def _seed_two_rounds(self):
        u1 = self.storage.add_message(self.conversation_id, "user", "第一问")
        self.storage.add_message(self.conversation_id, "assistant", "第一答")
        u2 = self.storage.add_message(self.conversation_id, "user", "第二问")
        self.storage.add_message(self.conversation_id, "assistant", "第二答")
        return u1, u2

    def test_truncate_from_user_message_keeps_prefix_only(self):
        u1, u2 = self._seed_two_rounds()
        removed = self.storage.truncate_from_message(self.conversation_id, u2["id"])
        self.assertEqual(removed, 2, "UN 与其后的 AN 一起删掉")
        self.assertEqual([m["content"] for m in self._messages()], ["第一问", "第一答"],
                         "U1..A(N-1) 必须逐字节保留（前缀缓存命中的前提）")

    def test_truncate_from_assistant_message_over_deletes(self):
        """反证：截断点必须是 UN 而不是 AN——从 AN 截断会把提问也删掉。"""
        _u1, u2 = self._seed_two_rounds()
        messages = self._messages()
        a2 = messages[-1]
        self.storage.truncate_from_message(self.conversation_id, a2["id"])
        self.assertEqual([m["content"] for m in self._messages()], ["第一问", "第一答", "第二问"],
                         "从 AN 截断会留下一条没有答复的提问 → 正是要避免的「累积重复提问」")

    def test_repeated_regenerate_is_idempotent(self):
        """连点三次：每次都从 UN 截断再补一轮，消息总数不变、提问不重复。"""
        _u1, u2 = self._seed_two_rounds()
        for _ in range(3):
            self.storage.truncate_from_message(self.conversation_id, u2["id"])
            resent = self.storage.add_message(self.conversation_id, "user", "第二问")
            self.storage.add_message(self.conversation_id, "assistant", "第二答（新）")
            u2 = resent
        self.assertEqual([m["content"] for m in self._messages()],
                         ["第一问", "第一答", "第二问", "第二答（新）"],
                         "反复重发只应留下最新一份答复")

    def test_regenerate_preserves_attachments_of_that_turn(self):
        """重发要能拿回那一轮的附件（与「编辑」同路径：/api/messages/edit 回传 attachments）。"""
        self.storage.add_message(self.conversation_id, "user", "第一问")
        self.storage.add_message(self.conversation_id, "assistant", "第一答")
        u2 = self.storage.add_message(
            self.conversation_id, "user", "带图的提问",
            {"attachments": [{"name": "a.png", "path": "uploads/a.png", "size": 12}]},
        )
        self.storage.add_message(self.conversation_id, "assistant", "第二答")
        target = next(m for m in self._messages() if m["id"] == u2["id"])
        self.assertEqual((target.get("metadata") or {}).get("attachments"),
                         [{"name": "a.png", "path": "uploads/a.png", "size": 12}])
        self.storage.truncate_from_message(self.conversation_id, u2["id"])
        self.assertEqual([m["content"] for m in self._messages()], ["第一问", "第一答"])


class RegenerateHistoryPrefixTests(unittest.TestCase):
    """前缀一致性：重发那一轮的请求前缀与首次逐字节相同（除了结论本身）。"""

    def test_resend_prefix_matches_first_turn(self):
        from naiba.core.history import build_model_history

        first = [
            {"role": "user", "content": "第一问", "metadata": {}},
            {"role": "assistant", "content": "第一答", "metadata": {}},
            {"role": "user", "content": "第二问", "metadata": {}},
        ]
        after_regenerate = [
            {"role": "user", "content": "第一问", "metadata": {}},
            {"role": "assistant", "content": "第一答", "metadata": {}},
            {"role": "user", "content": "第二问", "metadata": {}},
        ]
        self.assertEqual(build_model_history(first), build_model_history(after_regenerate),
                         "重发前后前缀必须一致，才能命中提供商的前缀缓存")


if __name__ == "__main__":
    unittest.main()
