# -*- coding: utf-8 -*-
"""护栏：辅助对话（aux chat，计划-辅助对话.md）。

四条守门线：
1. 迁移 v25：conversations 加 conv_kind 列，存量行默认 'main'；
2. 排除规则：aux 不出现在 list_conversations / find_conversations /
   search_history / search_messages / usage_stats；
3. 只读锁死：aux 会话提交 run 后 allowed_tools 只含只读集（带副作用的工具被过滤）；
4. 过期清扫：delete_expired_aux_conversations 只删 aux 且不动 main。
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]


def _strip_js_comments(source: str) -> str:
    """去掉 // 行注释与 /* */ 块注释：断言不该被注释里的反例文字误伤。"""
    import re
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", source)

from naiba.plans import READONLY_TOOLS  # noqa: E402
from naiba.run.session import resolve_allowed_tools  # noqa: E402
from naiba.storage.store import CURRENT_SCHEMA_VERSION, ChatStorage  # noqa: E402


class _StubRegistry:
    """MCP 只读工具注册表的窄桩：readonly_mcp_tools 返回带副作用的 mcp 工具名。"""

    def readonly_mcp_tools(self):
        return ["mcp__comfy__run_workflow"]


class _StubWebSearch:
    def is_available(self):
        return False


class _StubApp:
    def __init__(self):
        self.tool_registry = _StubRegistry()
        self.web_search = _StubWebSearch()
        self.config = type("C", (), {"data": {"agent_tools": [
            "read_file", "write_file", "list_directory", "search_files",
            "http_request", "edit_file", "pwsh",
        ]}})()


class AuxChatStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")

    def tearDown(self):
        self.tmp.cleanup()

    # ---- 迁移 v25 / v26 ----

    def test_fresh_db_reaches_v26_with_conv_kind_and_parent(self):
        self.assertEqual(CURRENT_SCHEMA_VERSION, 26)
        with self.storage._connect() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(conversations)")}
        self.assertIn("conv_kind", columns)
        self.assertIn("parent_id", columns)
        conv = self.storage.create_conversation()
        self.assertEqual(conv["conv_kind"], "main")
        self.assertEqual(conv["parent_id"], "")

    def test_parent_id_round_trips(self):
        """aux 会话的 parent_id 落库并在 list_aux_conversations 回显（前端跟随行靠它）。"""
        main = self.storage.create_conversation(title="正式任务")
        aux = self.storage.create_conversation(title="辅助对话 1", conv_kind="aux", parent_id=main["id"])
        self.assertEqual(aux["parent_id"], main["id"])
        listed = {item["id"]: item for item in self.storage.list_aux_conversations()}
        self.assertEqual(listed[aux["id"]]["parent_id"], main["id"])
        self.assertEqual(
            self.storage.get_conversation(aux["id"], include_messages=False)["parent_id"], main["id"]
        )

    def test_aux_settings_store_own_provider_and_model(self):
        """辅助对话的供应商/模型存在自己的会话行上（与主对话解耦）。"""
        aux = self.storage.create_conversation(title="辅助对话 1", conv_kind="aux")
        updated = self.storage.update_conversation_settings(
            aux["id"], model_key="online:demo", model_name="demo-model"
        )
        self.assertEqual(updated["model_key"], "online:demo")
        self.assertEqual(updated["model_name"], "demo-model")
        listed = {item["id"]: item for item in self.storage.list_aux_conversations()}
        self.assertEqual(listed[aux["id"]]["model_key"], "online:demo")
        self.assertEqual(listed[aux["id"]]["model_name"], "demo-model")

    def test_legacy_db_gets_main_default(self):
        """存量库（v24，无 conv_kind 列）迁移后旧行自动归为 main。"""
        legacy = ChatStorage(Path(self.tmp.name) / "legacy.db")
        with legacy._connect() as db:
            db.execute("PRAGMA user_version = 24")
            db.execute(
                "CREATE TABLE IF NOT EXISTS conversations ("
                "id TEXT PRIMARY KEY, title TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'online', "
                "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
            )
            db.execute(
                "INSERT INTO conversations(id, title, created_at, updated_at) VALUES ('old1', '旧会话', 1, 1)"
            )
        migrated = ChatStorage(Path(self.tmp.name) / "legacy.db")
        with migrated._connect() as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            row = db.execute("SELECT conv_kind FROM conversations WHERE id='old1'").fetchone()
        self.assertEqual(version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(str(row[0]), "main")

    # ---- 排除规则 ----

    def test_aux_hidden_from_listing_and_search(self):
        aux = self.storage.create_conversation(title="辅助对话 1", conv_kind="aux")
        main = self.storage.create_conversation(title="正式任务")
        self.storage.add_message(main["id"], "user", "主对话里的独特词组量子跃迁")
        self.storage.add_message(aux["id"], "user", "辅助对话里的独特词组量子跃迁")

        listed_ids = {item["id"] for item in self.storage.list_conversations()}
        self.assertIn(main["id"], listed_ids)
        self.assertNotIn(aux["id"], listed_ids)

        found_ids = {item["id"] for item in self.storage.find_conversations("")}
        self.assertNotIn(aux["id"], found_ids)

        history = self.storage.search_history("量子跃迁")
        hit_convs = {item["id"] for item in history.get("conversations") or []}
        self.assertNotIn(aux["id"], hit_convs)

        hits = self.storage.search_messages("量子跃迁")
        hit_conv_ids = {item["conversation_id"] for item in hits.get("hits") or []}
        self.assertNotIn(aux["id"], hit_conv_ids)

        aux_listed = [item["id"] for item in self.storage.list_aux_conversations()]
        self.assertEqual(aux_listed, [aux["id"]])
        # get_conversation 直取仍可用（重载恢复标签的通路）。
        self.assertEqual(
            self.storage.get_conversation(aux["id"], include_messages=False)["conv_kind"], "aux"
        )

    def test_usage_stats_excludes_aux(self):
        aux = self.storage.create_conversation(title="辅助对话 1", conv_kind="aux")
        main = self.storage.create_conversation(title="正式任务")
        now = int(time.time() * 1000)
        self.storage.record_usage({
            "run_id": "run-aux", "conversation_id": aux["id"], "kind": "chat",
            "model_key": "online:demo", "model_name": "Demo",
            "requests": 1, "input_tokens": 100, "output_tokens": 50,
            "total_tokens": 150, "created_at": now,
        })
        self.storage.record_usage({
            "run_id": "run-main", "conversation_id": main["id"], "kind": "chat",
            "model_key": "online:demo", "model_name": "Demo",
            "requests": 1, "input_tokens": 1000, "output_tokens": 500,
            "total_tokens": 1500, "created_at": now,
        })
        stats = self.storage.usage_stats(since_ms=0)
        # aux 的 100/50 不计入；main 的 1000/500 计入。
        self.assertEqual(int(stats["totals"]["input_tokens"]), 1000)
        self.assertEqual(int(stats["totals"]["output_tokens"]), 500)

    # ---- 过期清扫 ----

    def test_sweep_expires_only_old_aux(self):
        old_aux = self.storage.create_conversation(title="辅助对话 1", conv_kind="aux")
        fresh_aux = self.storage.create_conversation(title="辅助对话 2", conv_kind="aux")
        main = self.storage.create_conversation(title="正式任务")
        cutoff_beyond_ttl = int(time.time() * 1000) - 8 * 24 * 60 * 60 * 1000
        with self.storage._connect() as db:
            db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (cutoff_beyond_ttl, old_aux["id"]))
        deleted = self.storage.delete_expired_aux_conversations(ttl_days=7)
        self.assertEqual(deleted, 1)
        self.assertIsNone(self.storage.get_conversation(old_aux["id"], include_messages=False))
        self.assertIsNotNone(self.storage.get_conversation(fresh_aux["id"], include_messages=False))
        self.assertIsNotNone(self.storage.get_conversation(main["id"], include_messages=False))


class AuxChatCreateApiTests(unittest.TestCase):
    """创建接口的 parent_id 语义：只接受存在的主对话，aux / 不存在一律忽略。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        from naiba.app import NaibaChatApp
        from naiba.paths import PathContext

        self.app = NaibaChatApp(paths=PathContext.local(root, root / "config.json"))

    def test_parent_id_accepts_main_conversation(self):
        main = self.app.storage.create_conversation(title="正式任务")
        payload, status = self.app.api_create_aux_chat({"parent_id": main["id"]})
        self.assertEqual(int(status), 201)
        self.assertEqual(payload["parent_id"], main["id"])

    def test_parent_id_ignored_for_aux_or_missing(self):
        other_aux = self.app.storage.create_conversation(title="辅助对话 9", conv_kind="aux")
        for bad in (other_aux["id"], "does-not-exist", ""):
            with self.subTest(parent_id=bad):
                payload, status = self.app.api_create_aux_chat({"parent_id": bad})
                self.assertEqual(int(status), 201)
                self.assertEqual(payload["parent_id"], "")


class AuxChatAgentSwitchTests(unittest.TestCase):
    """辅助面板可直接换 Agent：固化工具集后也允许，且按新 Agent 重建只读工具集。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        from naiba.app import NaibaChatApp
        from naiba.paths import PathContext

        self.app = NaibaChatApp(paths=PathContext.local(root, root / "config.json"))

    def test_aux_switch_allowed_after_toolset_frozen(self):
        aux = self.app.storage.create_conversation(
            title="辅助对话 1", conv_kind="aux", agent_id="master"
        )
        # 模拟首轮已固化（含副作用工具），辅助对话仍必须能换 Agent。
        self.app.storage.set_enabled_tool_ids(
            aux["id"], ["read_file", "list_directory", "write_file", "pwsh"]
        )
        payload, status = self.app.api_update_conversation_settings(
            aux["id"], {"agent_id": "tutor"}
        )
        self.assertEqual(int(status), 200, payload)
        self.assertEqual(payload["agent_id"], "tutor")
        # 新 Agent 的固化工具集仍只含只读工具（不因换人设而扩权）。
        tool_ids = set(payload["enabled_tool_ids"])
        self.assertIn("read_file", tool_ids)
        self.assertNotIn("write_file", tool_ids)
        self.assertNotIn("pwsh", tool_ids)
        stored = self.app.storage.get_conversation(aux["id"], include_messages=False)
        self.assertEqual(stored["agent_id"], "tutor")
        self.assertEqual(set(stored["enabled_tool_ids"]), tool_ids)

    def test_main_chat_still_locked_after_toolset_frozen(self):
        main = self.app.storage.create_conversation(title="正式任务", agent_id="master")
        self.app.storage.set_enabled_tool_ids(main["id"], ["read_file"])
        payload, status = self.app.api_update_conversation_settings(
            main["id"], {"agent_id": "tutor"}
        )
        self.assertEqual(int(status), 409, payload)


class AuxChatAgentSelectorTests(unittest.TestCase):
    """面板上的 Agent 下拉：标记、渲染、切换与新建时继承一次。"""

    def test_panel_markup_has_agent_select(self):
        html = (ROOT / "public/index.html").read_text(encoding="utf-8")
        self.assertIn('id="auxAgentSelect"', html)

    def test_frontend_renders_and_persists_agent(self):
        source = (ROOT / "public/js/21-aux-chat.js").read_text(encoding="utf-8")
        code = _strip_js_comments(source)
        self.assertIn("function renderAuxAgentSelect()", code)
        self.assertIn("renderAuxAgentSelect();", code)
        self.assertIn("body: { agent_id: next }", code)
        self.assertIn("agent_id: currentAgentId", code)
        self.assertIn("agentId: String(row.agent_id || '')", code)


class AuxChatReadonlyTests(unittest.TestCase):
    """只读锁死（§3.2）：工具决议收口到只读白名单（后端唯一判定）。"""

    def setUp(self):
        self.app = _StubApp()
        # 带副作用的固化集：模拟用户在正式会话里开满全部工具后转 aux 的最坏情况。
        self.full_baked = [
            "read_file", "write_file", "edit_file", "list_directory", "search_files",
            "http_request", "pwsh", "run_in_background", "job_output", "job_status",
            "job_wait", "subagent", "vision_analyze", "vision_image_ops", "web_search",
            "mcp__comfy__run_workflow",
        ]

    def test_aux_filters_everything_but_readonly(self):
        tools = resolve_allowed_tools(
            self.app, "craft", {"tool_scope": []}, False, "", list(self.full_baked), "aux"
        )
        self.assertIn("read_file", tools)
        self.assertIn("list_directory", tools)
        self.assertIn("search_files", tools)
        self.assertIn("http_request", tools)
        self.assertIn("vision_analyze", tools)
        self.assertIn("mcp__comfy__run_workflow", tools, "只读 MCP 工具应放行")
        for blocked in ("write_file", "edit_file", "pwsh", "run_in_background",
                        "subagent", "vision_image_ops", "comfyui_prepare_workflow"):
            self.assertNotIn(blocked, tools)

    def test_aux_plain_http_request_only(self):
        tools = resolve_allowed_tools(
            self.app, "craft", {"tool_scope": []}, False, "", ["http_request"], "aux"
        )
        self.assertEqual(tools, ["http_request"])

    def test_main_unaffected(self):
        tools = resolve_allowed_tools(
            self.app, "craft", {"tool_scope": []}, False, "", list(self.full_baked), "main"
        )
        self.assertIn("write_file", tools)
        self.assertIn("pwsh", tools)

    def test_readonly_baseline_covers_plan_tools(self):
        # 收口是**交集**语义：aux 只会缩小固化集，绝不注入固化集之外的工具。
        # 只读白名单（READONLY_TOOLS ∪ SYSTEM_TOOLS_READONLY ∪ 只读 MCP）是上限。
        tools = resolve_allowed_tools(self.app, "craft", {}, False, "", ["read_file"], "aux")
        self.assertEqual(tools, ["read_file"])
        self.assertTrue(set(tools) <= set(READONLY_TOOLS) | {"vision_analyze", "web_search"})


class AuxChatPanelGeometryTests(unittest.TestCase):
    """顶栏「辅助」按钮与面板宽度的不变量（3.3.2 挤压 bug + 3.3.3 入口死锁）。

    背景：右侧面板一开，顶栏就挤成一团（用户截图）。三条相关原因都在这里钉住：
    ① 面板开着时顶栏还留一个 ~94px 的「辅助」重复入口；
    ② 21-aux-chat.js 自己复制了一份 filePanelWidthPx()，把 clamp 丢了；
    ③ 3.3.2 顺手加的门槛把**全新用户的唯一入口**关掉了（死锁，见下面那条用例）。
    """

    def _aux(self) -> str:
        return (ROOT / "public/js/21-aux-chat.js").read_text(encoding="utf-8")

    def _panel(self) -> str:
        return (ROOT / "public/js/14-file-panel.js").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def test_reopen_button_hides_while_panel_open(self):
        aux = self._aux()
        self.assertIn(
            "button.hidden = filePanelState.open;",
            aux,
            "面板开着时「辅助」按钮必须隐藏（与文件按钮同口径）",
        )
        # 反例：不得再按「有没有标签」决定显隐 —— 那就是 3.3.0~3.3.2 的死锁
        self.assertNotIn(
            "auxChatState.tabs.length === 0 || filePanelState.open",
            aux,
            "又按 tabs.length 卡入口了：全新用户永远进不去辅助对话",
        )

    def test_entry_is_reachable_with_zero_aux_chats(self):
        """全新用户（0 个辅助对话）必须能从顶栏打开面板，且在空态里能直接新建。

        3.3.0~3.3.2 的死锁：顶栏按钮要求 tabs.length > 0 才显示；而新建辅助对话的
        ＋ 只在面板里；面板又只能靠这个按钮打开 ⇒ 没人能第一次用上这个功能。
        """
        aux = self._aux()
        code = _strip_js_comments(aux)
        button_fn = code[code.index("function updateAuxTabsButton()"):]
        button_fn = button_fn[: button_fn.index("\n}\n")]
        self.assertNotIn("tabs.length === 0", button_fn, "门槛又回来了（死锁）")
        # 只许按「面板是否开着」决定显隐：`button.hidden = ...` 那一行里不得出现 tabs
        hidden_line = next(
            (line for line in button_fn.splitlines() if "button.hidden" in line), "")
        self.assertIn("filePanelState.open", hidden_line)
        self.assertNotIn("tabs", hidden_line, "显隐判断不得依赖有没有标签（死锁）")
        # 空态里必须有可点的新建入口（空态 HTML 已抽到 auxEmptyState()）
        self.assertIn("aux-empty-start", aux, "空态缺少「开一个辅助对话」按钮")
        empty_fn = code[code.index("function auxEmptyState()"):]
        empty_fn = empty_fn[: empty_fn.index("\n}\n")]
        self.assertIn('data-aux-new', empty_fn, "空态的新建按钮没有挂委托标记")
        # 标签栏与空态两个 ＋ 都要接上委托（否则点了没反应）
        self.assertGreaterEqual(code.count("closest('[data-aux-new]')"), 2)

    def test_zero_state_shows_plus_not_bare_zero(self):
        """0 个辅助对话时计数显示「＋」（引导新建），不是看起来像故障的裸 0。"""
        aux = self._aux()
        self.assertIn("auxChatState.tabs.length ? String(auxChatState.tabs.length) : '＋'", aux)

    def test_zero_state_button_is_styled(self):
        """空态按钮与空态文案都要有样式，否则是个裸 button（UA 灰底）。"""
        css = self._css()
        self.assertIn(".aux-empty-start", css)
        self.assertIn(".aux-empty-hint", css)

    def test_panel_state_is_shared_not_duplicated(self):
        aux = self._aux()
        self.assertIn('from "./14-file-panel.js"', aux)
        self.assertNotIn("localStorage.getItem('naibaChatFilePanelW')", aux,
                         "面板宽度只许有一份实现（复制品会丢 clamp）")
        self.assertNotIn("return 380;", aux)

    def test_panel_width_is_clamped_by_ratio(self):
        panel = self._panel()
        width_fn = panel[panel.index("export function filePanelWidthPx()"):]
        width_fn = width_fn[: width_fn.index("\n}\n")]
        self.assertIn("0.28", width_fn, "上限必须按窗口比例收（原来 0.5 能开到 600px）")
        self.assertIn("Math.min(420", width_fn)
        self.assertIn("Math.max(300", width_fn)

    def test_aux_button_hidden_rule_overrides_author_display(self):
        css = self._css()
        self.assertIn(
            ".aux-reopen-button[hidden] { display: none; }",
            css,
            "缺这条时 `.topbar-actions .control-button{display:inline-flex}` 会压过 [hidden]",
        )

    def test_panel_toggle_is_broadcast(self):
        """开合状态要广播出去，两个模块各自的重开按钮才能同步（避免 21↔14 循环 import）。"""
        panel = self._panel()
        self.assertIn("naiba:file-panel-toggle", panel)
        self.assertIn("naiba:file-panel-toggle", self._aux())
        aux = self._aux()
        # open/close 必须走 filePanelState + applyFilePanelOpenClass，不能只 add class
        self.assertIn("filePanelState.open = true;", aux)
        self.assertIn("applyFilePanelOpenClass();", aux)


if __name__ == "__main__":
    unittest.main()
