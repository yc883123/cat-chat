# -*- coding: utf-8 -*-
"""护栏：分支时可选「更换 Agent」（`branch_conversation(reset_agent=True)` + 前端选择框）。

保护对象：
- `branch_conversation` 新增的 `reset_agent` 关键字参数**默认 False** ⇒ 存量调用方（含
  所有历史测试）行为零变化，工具集 / 技能策略 / 首轮上下文三列照旧继承；
- `reset_agent=True` 时这三列必须**同时清空**：只清工具集会留下「按旧 Agent 冻结的技能集 +
  旧的首轮折叠卡」，新会话的 system 前缀于是嵌着旧 Agent 的上下文，比不清更糟；
- 无论如何 **`agent_id` 仍继承**：下拉预选同一个 Agent，用户可换可不换——这是「可选」而非
  「强制换」的语义，清掉 agent_id 会让下拉跳回默认 Agent，用户反而要多点一次；
- 分支的既有契约（历史复制、`branched_from_id` / `branch_message_id` 登记、标题 `(N)` 序号、
  源会话不被改动）在 `reset_agent=True` 下也必须完好；
- 静态接线钉到**调用层**（§九.81：「按钮存在」≠「用户能用」）：`http.py` 真把 body 里的
  `reset_agent` 透传下去；`04-messages.js` 的 `branchMessage` 函数体里真出现选择框调用与
  `reset_agent` 字段，且选择发生在 API 调用**之前**（取消不产生新会话）。
"""

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.storage.store import ChatStorage  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _function_body(source: str, header: str) -> str:
    """截出 `header` 开头那个函数的函数体（到下一个顶层 `export `/`function `/`// ----` 为止）。

    静态断言必须钉在**函数体**里：同名字符串在文件别处出现（注释、别处的调用）不算接线。
    """
    start = source.index(header)
    rest = source[start + len(header):]
    stop = len(rest)
    for marker in ("\nexport ", "\nfunction ", "\n// ----"):
        found = rest.find(marker)
        if found != -1:
            stop = min(stop, found)
    return rest[:stop]


class BranchResetAgentStorageTests(unittest.TestCase):
    """`reset_agent=True`：清固化态、留 agent_id、其余分支语义不动。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_branch_agent_")
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.source = self.storage.create_conversation(title="源会话")
        # 造出「已固化」的源会话：工具集 + 技能策略 + 首轮上下文三列都非空。
        self.storage.set_enabled_tool_ids(self.source["id"], ["read_file", "pwsh"])
        self.storage.set_conversation_skill_policy(self.source["id"], {"ids": ["demo-skill"]})
        self.storage.set_conversation_first_turn(
            self.source["id"], {"agent_name": "旧 Agent", "system_prompt": "旧前缀"}
        )
        self.storage.update_conversation_settings(self.source["id"], agent_id="agent-old")
        self.first = self.storage.add_message(self.source["id"], "user", "第一轮问题")
        self.storage.add_message(self.source["id"], "assistant", "第一轮回答")
        self.second = self.storage.add_message(self.source["id"], "user", "第二轮问题")
        self.storage.add_message(self.source["id"], "assistant", "第二轮回答")

    def tearDown(self):
        self.tmp.cleanup()

    def _branch(self, **kwargs):
        result = self.storage.branch_conversation(self.source["id"], self.second["id"], **kwargs)
        self.assertIn("branch_message", result)
        return result["conversation"]

    def _source_live(self):
        return self.storage.get_conversation(self.source["id"], include_messages=False)

    def _first_turn(self, conversation_id):
        """`get_conversation` 的 SELECT 有意不含 `first_turn`（那是 run 侧内部列），
        所以直接读列——本用例保护的正是这一列的继承/清空口径。"""
        with closing(sqlite3.connect(self.storage.db_path)) as db:
            row = db.execute(
                "SELECT first_turn FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return str(row[0] or "")

    # ---- 默认值：存量行为零变化 ----

    def test_default_reset_agent_is_false(self):
        """不传参数 = 保持现状（旧调用方、旧测试的行为契约）。"""
        self.assertEqual(self._branch()["enabled_tool_ids"], ["read_file", "pwsh"])

    def test_keep_inherits_all_three_frozen_columns(self):
        branch = self._branch(reset_agent=False)
        src = self._source_live()
        self.assertEqual(branch["enabled_tool_ids"], ["read_file", "pwsh"], "工具集照旧继承")
        self.assertEqual(branch["skill_policy"], src["skill_policy"], "技能策略照旧继承")
        self.assertEqual(
            self._first_turn(branch["id"]), self._first_turn(self.source["id"]),
            "首轮上下文照旧继承",
        )
        self.assertNotEqual(self._first_turn(self.source["id"]), "", "前置条件：源会话确实有首轮上下文")

    # ---- 更换 Agent：三列一起清 ----

    def test_reset_clears_tool_ids_so_agent_select_unlocks(self):
        branch = self._branch(reset_agent=True)
        self.assertEqual(branch["enabled_tool_ids"], [],
                         "必须是空列表：前端 locked 判据就是「工具集非空」")

    def test_reset_clears_skill_policy(self):
        branch = self._branch(reset_agent=True)
        self.assertEqual(branch["skill_policy"], {}, "技能集是按旧 Agent 冻结的，不能继承")

    def test_reset_clears_first_turn(self):
        branch = self._branch(reset_agent=True)
        self.assertEqual(self._first_turn(branch["id"]), "",
                         "否则顶部折叠卡展示的是旧 Agent 的固化上下文，误导用户")

    def test_reset_keeps_agent_id_so_selection_is_optional(self):
        branch = self._branch(reset_agent=True)
        self.assertEqual(branch["agent_id"], "agent-old",
                         "「可选」而非「强制换」：下拉预选同一个 Agent")

    def test_reset_does_not_touch_source_conversation(self):
        self._branch(reset_agent=True)
        src = self._source_live()
        self.assertEqual(src["enabled_tool_ids"], ["read_file", "pwsh"], "源会话照旧固化")
        self.assertNotEqual(self._first_turn(self.source["id"]), "", "源会话的首轮上下文不能被清")
        self.assertEqual(
            len(self.storage.get_conversation(self.source["id"])["messages"]), 4,
            "分支是非破坏性的：源会话消息一条不少",
        )

    # ---- 分支既有契约在 reset 下同样成立 ----

    def test_reset_still_copies_history_and_records_source(self):
        branch = self._branch(reset_agent=True)
        contents = [m["content"] for m in self.storage.get_conversation(branch["id"])["messages"]]
        self.assertEqual(contents, ["第一轮问题", "第一轮回答"], "只复制分支点之前")
        self.assertEqual(branch["branched_from_id"], self.source["id"])
        self.assertEqual(branch["branch_message_id"], self.second["id"])

    def test_reset_still_uses_incremental_title(self):
        # 源标题已被首条 user 消息顶成「第一轮问题」（add_message 的既有行为），基准取实时值。
        base = self._source_live()["title"]
        first = self._branch(reset_agent=True)
        second = self._branch(reset_agent=True)
        self.assertEqual(first["title"], f"{base} (1)")
        self.assertEqual(second["title"], f"{base} (2)", "序号逻辑不因换 Agent 而变")

    def test_reset_from_first_message_is_also_cleared(self):
        """分支点是首条消息时本就留空；reset 下同样留空（两条通路一致，不互相打架）。"""
        branch = self.storage.branch_conversation(
            self.source["id"], self.first["id"], reset_agent=True
        )["conversation"]
        self.assertEqual(branch["enabled_tool_ids"], [])
        self.assertEqual(branch["skill_policy"], {})
        self.assertEqual(self._first_turn(branch["id"]), "")
        self.assertEqual(branch["agent_id"], "agent-old")


class BranchResetAgentWiringTests(unittest.TestCase):
    """静态接线：路由透传 + 前端选择框真被调用。"""

    def setUp(self):
        self.http = (ROOT / "naiba" / "http.py").read_text(encoding="utf-8")
        self.html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        self.js = (ROOT / "public" / "js" / "04-messages.js").read_text(encoding="utf-8")

    def test_route_passes_reset_agent_through(self):
        self.assertIn("reset_agent=bool(body.get(\"reset_agent\"))", self.http,
                      "路由必须把 body 里的 reset_agent 透传给 storage")
        # 注意 `/branch_chain` 路由在文件里排得**更靠前**，不能拿它当右边界。
        branch_route = self.http[
            self.http.index('path.endswith("/branch")'):
            self.http.index('path.endswith("/session_start")')
        ]
        self.assertIn("reset_agent", branch_route, "透传必须发生在 /branch 分支体内")

    def test_dialog_buttons_exist(self):
        for element_id in ("branchAgentDialog", "branchAgentKeep", "branchAgentReset", "branchAgentCancel"):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', self.html)

    def test_dialog_reuses_existing_style_classes(self):
        """零新增 CSS：复用删除确认框的样式类（否则要动 styles.css）。"""
        dialog = self.html[
            self.html.index('id="branchAgentDialog"') - 200:self.html.index('id="branchAgentCancel"')
        ]
        self.assertIn("rename-conversation-dialog message-delete-dialog", dialog)
        self.assertIn("form-actions message-delete-actions", dialog)
        self.assertIn('class="message-delete-notes"', dialog)

    def test_branch_message_awaits_choice_before_api_call(self):
        body = _function_body(self.js, "export async function branchMessage(row) {")
        self.assertIn("askBranchAgentChoice", body, "必须真调用选择框（不是只在别处定义）")
        self.assertIn("reset_agent", body, "请求体必须带上 reset_agent")
        self.assertLess(body.index("await askBranchAgentChoice"),
                        body.index("/branch`"),
                        "选择必须在 API 调用之前：取消 = 不产生新会话")
        self.assertIn("if (!choice) return;", body, "取消分支要直接返回")

    def test_ask_branch_agent_choice_mirrors_delete_scope_contract(self):
        body = _function_body(self.js, "function askBranchAgentChoice(")
        for key in ("showModal", "removeEventListener", "settled"):
            with self.subTest(key=key):
                self.assertIn(key, body, "与 askDeleteScope 同款 Promise 模式")
        for value in ("'keep'", "'reset'", "''"):
            with self.subTest(value=value):
                self.assertIn(value, body)

    def test_missing_dialog_degrades_to_keep(self):
        """对话框缺失时降级为「保持」：少一个选项可以，分支按钮点不动不行。"""
        body = _function_body(self.js, "function askBranchAgentChoice(")
        self.assertIn("return Promise.resolve('keep')", body)

    def test_reset_toast_mentions_agent_switch(self):
        body = _function_body(self.js, "export async function branchMessage(row) {")
        self.assertIn("发送前可在输入区更换 Agent", body)

    def test_no_comment_only_wiring(self):
        """负向断言前先剔掉整行注释：注释里写了不算接线（§九 既有惯例）。"""
        body = _function_body(self.js, "export async function branchMessage(row) {")
        code = "\n".join(
            line for line in body.splitlines() if not line.strip().startswith("//")
        )
        self.assertIn("reset_agent", code)
        self.assertIn("askBranchAgentChoice", code)


class BranchResetAgentSchemaTests(unittest.TestCase):
    """签名与落库口径：新参数是关键字、有默认值，且不新增列。"""

    def test_signature_has_keyword_default(self):
        import inspect

        signature = inspect.signature(ChatStorage.branch_conversation)
        self.assertEqual(list(signature.parameters), ["self", "source_id", "message_id", "reset_agent"])
        self.assertEqual(signature.parameters["reset_agent"].default, False,
                         "默认必须是 False，否则存量调用方行为被改")
        self.assertEqual(signature.parameters["reset_agent"].kind,
                         inspect.Parameter.POSITIONAL_OR_KEYWORD)

    def test_no_schema_change(self):
        """换 Agent 只是「不写那三列」，不引入新列 ⇒ 不需要迁移。"""
        with tempfile.TemporaryDirectory(prefix="naiba_branch_agent_mig_") as tmp:
            db_path = Path(tmp) / "chat.db"
            storage = ChatStorage(db_path)
            with closing(sqlite3.connect(db_path)) as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(conversations)")}
            self.assertIn("enabled_tool_ids", columns)
            self.assertNotIn("reset_agent", columns, "不得把一次性选择落成会话列")
            self.assertTrue(storage.list_conversations() is not None)

    def test_branch_message_payload_is_untouched_by_reset(self):
        """预填输入框的载荷（display_content / attachments）与 Agent 选择无关。"""
        with tempfile.TemporaryDirectory(prefix="naiba_branch_agent_payload_") as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            source = storage.create_conversation(title="源")
            storage.add_message(source["id"], "user", "前一轮")
            anchor = storage.add_message(
                source["id"], "user", "这轮",
                metadata={"display_content": "这轮（显示）", "attachments": []},
            )
            payload = storage.branch_conversation(
                source["id"], anchor["id"], reset_agent=True
            )["branch_message"]
            self.assertEqual(payload["display_content"], "这轮（显示）")
            self.assertEqual(payload["content"], "这轮")
            self.assertIn("metadata", payload)


if __name__ == "__main__":
    unittest.main()
