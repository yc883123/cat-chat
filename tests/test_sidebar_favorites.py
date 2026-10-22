# -*- coding: utf-8 -*-
"""侧栏收藏（favorite）与会话级系统提示词退役的守门。

本轮两条互相关联的改动必须在同一处被钉住：

1. ``conversations.favorite`` 是新增的纯增量列（迁移 v15，幂等），只作为侧栏「已收藏」
   分组标记，不参与模型上下文、不影响任何冻结快照；
2. 会话级系统提示词整体退役（UI 入口、注入点、角色卡导入全部迁到 Agent），
   因此后端三处注入点不得再读取 ``conversation.system_prompt``，
   前端也不得再出现旧的「对话设置」对话框元素。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.storage.store import CURRENT_SCHEMA_VERSION, MIGRATIONS, ChatStorage  # noqa: E402


class FavoriteMigrationTests(unittest.TestCase):
    """迁移 v15：列存在、幂等、旧库可升级。"""

    def test_schema_version_registers_v15(self) -> None:
        self.assertGreaterEqual(CURRENT_SCHEMA_VERSION, 15)
        self.assertIn(15, MIGRATIONS)

    def test_migration_adds_column_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="naiba_favorite_mig_") as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            conn = sqlite3.connect(storage.db_path)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
                self.assertIn("favorite", columns, "初始化后必须已有 favorite 列")
                # 重复执行不得抛错（列已存在时跳过），否则升级路径会被二次迁移打断。
                MIGRATIONS[15](conn)
                MIGRATIONS[15](conn)
                again = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
                self.assertIn("favorite", again)
            finally:
                conn.close()

    def test_legacy_database_upgrades(self) -> None:
        """模拟 v14 老库：删列后重跑迁移必须补回且默认 0。"""
        with tempfile.TemporaryDirectory(prefix="naiba_favorite_legacy_") as tmp:
            db_path = Path(tmp) / "chat.db"
            storage = ChatStorage(db_path)
            conv = storage.create_conversation("legacy", "老会话")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("ALTER TABLE conversations DROP COLUMN favorite")
                conn.commit()
                MIGRATIONS[15](conn)
                conn.commit()
                row = conn.execute(
                    "SELECT favorite FROM conversations WHERE id = ?", (str(conv["id"]),)
                ).fetchone()
                self.assertEqual(int(row[0]), 0)
            finally:
                conn.close()


class FavoriteStorageTests(unittest.TestCase):
    """收藏的读写回路与「不影响其他字段」的不变量。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="naiba_favorite_ws_")
        self.addCleanup(self._tmp.cleanup)
        self.storage = ChatStorage(Path(self._tmp.name) / "chat.db")
        self.conv_id = str(self.storage.create_conversation("c1", "收藏测试")["id"])

    def test_defaults_to_not_favorite(self) -> None:
        row = self.storage.get_conversation(self.conv_id, include_messages=False)
        self.assertEqual(int(row["favorite"]), 0)

    def test_toggle_round_trip_through_list_and_get(self) -> None:
        updated = self.storage.set_conversation_favorite(self.conv_id, True)
        self.assertEqual(int(updated["favorite"]), 1)
        listed = next(
            item for item in self.storage.list_conversations() if item["id"] == self.conv_id
        )
        self.assertEqual(int(listed["favorite"]), 1)
        self.assertEqual(
            int(self.storage.get_conversation(self.conv_id, include_messages=False)["favorite"]), 1
        )
        again = self.storage.set_conversation_favorite(self.conv_id, False)
        self.assertEqual(int(again["favorite"]), 0)

    def test_favorite_does_not_bump_updated_at(self) -> None:
        """收藏不得推进 updated_at：否则侧栏按时间排序会把会话顶到工作区最前。"""
        before = self.storage.get_conversation(self.conv_id, include_messages=False)
        self.storage.set_conversation_favorite(self.conv_id, True)
        after = self.storage.get_conversation(self.conv_id, include_messages=False)
        self.assertEqual(before["updated_at"], after["updated_at"], "收藏改动了 updated_at（会重排侧栏）")
        # 对照：改标题这类真实设置必须推进时间（侧栏排序依赖它）。
        self.storage.update_conversation_settings(self.conv_id, title="改个名")
        renamed = self.storage.get_conversation(self.conv_id, include_messages=False)
        self.assertGreaterEqual(int(renamed["updated_at"]), int(after["updated_at"]))

    def test_favorite_keeps_other_fields(self) -> None:
        self.storage.update_conversation_settings(self.conv_id, title="自定义标题", system_prompt="旧提示词")
        before = self.storage.get_conversation(self.conv_id, include_messages=False)
        self.storage.set_conversation_favorite(self.conv_id, True)
        after = self.storage.get_conversation(self.conv_id, include_messages=False)
        for key in (
            "title",
            "title_customized",
            "system_prompt",
            "stream_enabled",
            "agent_id",
            "workspace_group",
            "permission_mode",
            "model_key",
            "updated_at",
        ):
            with self.subTest(field=key):
                self.assertEqual(before[key], after[key])


class FavoriteApiTests(unittest.TestCase):
    """HTTP 层口径：favorite 只接受布尔值，其余字段透传不变。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="naiba_favorite_api_")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        from naiba.app import NaibaChatApp
        from naiba.paths import PathContext

        self.paths = PathContext.local(root, root / "config.json")
        self.app = NaibaChatApp(paths=self.paths)
        created, status = self.app.api_create_conversation({"title": "接口收藏"})
        self.assertEqual(int(status), 201)
        self.conv_id = str(created["id"])

    def test_rejects_non_boolean(self) -> None:
        payload, status = self.app.api_update_conversation_settings(self.conv_id, {"favorite": "yes"})
        self.assertEqual(int(status), 400)
        self.assertIn("favorite", payload.get("error", ""))

    def test_accepts_boolean_and_returns_field(self) -> None:
        before = self.app.storage.get_conversation(self.conv_id, include_messages=False)
        payload, status = self.app.api_update_conversation_settings(self.conv_id, {"favorite": True})
        self.assertEqual(int(status), 200)
        self.assertEqual(int(payload["favorite"]), 1)
        after = self.app.storage.get_conversation(self.conv_id, include_messages=False)
        self.assertEqual(before["updated_at"], after["updated_at"], "接口路径也在推进 updated_at")
        payload, status = self.app.api_update_conversation_settings(self.conv_id, {"favorite": False})
        self.assertEqual(int(status), 200)
        self.assertEqual(int(payload["favorite"]), 0)

    def test_unknown_conversation_returns_404(self) -> None:
        payload, status = self.app.api_update_conversation_settings("nope", {"favorite": True})
        self.assertEqual(int(status), 404)
        self.assertIn("error", payload)


class ConversationPromptRetiredTests(unittest.TestCase):
    """会话级系统提示词必须彻底退役：后端注入点归零、前端旧入口不存在。"""

    BACKEND_FILES = ("naiba/run/chat.py", "naiba/plans.py", "naiba/subagent.py")

    def test_backend_no_conversation_prompt_injection(self) -> None:
        for name in self.BACKEND_FILES:
            with self.subTest(file=name):
                source = (ROOT / name).read_text(encoding="utf-8")
                self.assertNotIn("conversation_system_prompt", source, f"{name} 仍在注入会话级提示词")
                self.assertNotIn(
                    'conversation.get("system_prompt")', source, f"{name} 仍在读取会话级提示词"
                )

    def test_agent_prompt_is_the_only_source(self) -> None:
        chat = (ROOT / "naiba/run/chat.py").read_text(encoding="utf-8")
        self.assertIn('prompt = str(agent.get("system_prompt") or "").strip()', chat)

    def test_frontend_removes_conversation_settings_entry_points(self) -> None:
        index = (ROOT / "public/index.html").read_text(encoding="utf-8")
        for snippet in (
            "conversationSettingsDialog",
            "conversationSystemPrompt",
            "conversationPromptPresetSelect",
            "openCurrentConversationSettings",
            "importCharacterCardBtn",
        ):
            with self.subTest(snippet=snippet):
                self.assertNotIn(snippet, index, "旧的会话级系统提示词入口仍在 index.html")

    def test_agent_form_owns_preset_and_card_import(self) -> None:
        index = (ROOT / "public/index.html").read_text(encoding="utf-8")
        self.assertIn("agentPromptPresetButton", index)
        self.assertIn("saveAgentPromptPreset", index, "「存为快捷提示词」按钮必须在 Agent 表单里")
        self.assertIn("importAgentCharacterCard", index)
        # 导入按钮必须紧跟「系统提示词（预设与规则）」行之后。
        prompt_row = index.index("系统提示词（预设与规则）")
        self.assertLess(prompt_row, index.index("agentPromptPresetButton"))
        conversations = (ROOT / "public/js/08-conversations.js").read_text(encoding="utf-8")
        self.assertIn("export function mergeAgentPromptText(", conversations)
        self.assertIn("export async function importAgentCharacterCard(", conversations)

    def test_conversation_prompts_settings_page_removed(self) -> None:
        """快捷提示词设置页整体下线：入口、面板、表单与旧绑定都不许残留。"""
        index = (ROOT / "public/index.html").read_text(encoding="utf-8")
        for snippet in ("conversation-prompts", "conversationPromptPresetList",
                        "conversationPromptPresetForm", "addConversationPromptPreset"):
            with self.subTest(snippet=snippet):
                self.assertNotIn(snippet, index, "快捷提示词设置页残留")
        bind = (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")
        self.assertNotIn("conversation-prompt", bind)
        conversations = (ROOT / "public/js/08-conversations.js").read_text(encoding="utf-8")
        for snippet in ("renderConversationPromptPresets", "openConversationPromptPresetForm",
                        "saveConversationPromptPreset", "importConversationPromptPresetCard"):
            with self.subTest(snippet=snippet):
                self.assertNotIn(snippet, conversations, "已下线的设置页函数还在")

    def test_sidebar_item_uses_star_and_more_menu(self) -> None:
        source = (ROOT / "public/js/08-conversations.js").read_text(encoding="utf-8")
        self.assertIn('class="conversation-star', source)
        self.assertIn('data-action="toggle-favorite"', source)
        self.assertIn('data-action="open-conversation-menu"', source)
        self.assertNotIn("conversation-settings", source, "齿轮入口应已退役")
        self.assertNotIn("delete-conversation", source, "删除已并入「⋯」菜单")

    def test_favorites_group_is_rendered_last(self) -> None:
        source = (ROOT / "public/js/08-conversations.js").read_text(encoding="utf-8")
        self.assertIn("SIDE_FAVORITES_GROUP", source)
        self.assertIn("label: '已收藏'", source)
        # 收藏分组必须在工作区分组循环之后追加（侧栏最下方）。
        loop_end = source.index("const favoriteList = sortConv(")
        self.assertLess(source.index("for (const wsName of orderedNames)"), loop_end)

    def test_favorite_has_dedicated_storage_path(self) -> None:
        """收藏只能走 set_conversation_favorite（不推进 updated_at），别塞回通用设置更新。"""
        source = (ROOT / "naiba/storage/store.py").read_text(encoding="utf-8")
        self.assertIn("def set_conversation_favorite(", source)
        start = source.index("def update_conversation_settings(")
        end = source.index("def set_enabled_tool_ids(", start)
        block = source[start:end]
        self.assertNotIn("favorite: bool | None", block, "通用设置更新又加了 favorite 参数")
        self.assertNotIn('values["favorite"]', block, "通用设置更新又写 favorite（会推进 updated_at）")
        app = (ROOT / "naiba/app.py").read_text(encoding="utf-8")
        self.assertIn("set_conversation_favorite(conversation_id, favorite)", app)

    def test_frontend_toggle_uses_settings_endpoint_without_reorder(self) -> None:
        source = (ROOT / "public/js/08-conversations.js").read_text(encoding="utf-8")
        # 收藏只提交 favorite 一个字段：不带任何会推进 updated_at 的字段（否则侧栏重排）。
        self.assertIn("body: { favorite: next }", source)
        # 工作区分组的排序口径只有 updated_at / 标题，不含 favorite。
        sort_block = source[source.index("const sortConv = (list) => {"):]
        sort_block = sort_block[: sort_block.index("};")]
        self.assertNotIn("favorite", sort_block)

    def test_top_new_chat_button_removed(self) -> None:
        """顶栏「新会话」按钮已删：每个工作区分组自带「＋ 新会话」，空列表另有兜底入口。"""
        index = (ROOT / "public/index.html").read_text(encoding="utf-8")
        self.assertNotIn("newChatButton", index, "顶栏「新会话」按钮又回来了（与工作区内新建重复）")
        source = (ROOT / "public/js/08-conversations.js").read_text(encoding="utf-8")
        self.assertIn('data-action="new-chat"', source, "空列表缺少兜底新建入口")

    def test_active_scroll_uses_minimal_scroll(self) -> None:
        """点击会话不得把列表强制滚到顶：定位用最小滚动（已可见则不动）。"""
        source = (ROOT / "public/js/08-conversations.js").read_text(encoding="utf-8")
        self.assertIn("export function sidebarScrollForActive(", source)
        self.assertIn("sidebarScrollForActive(rows, offsets, st, tree.clientHeight || 0)", source)
        self.assertNotIn("if (idx >= 0) st = offsets[idx];", source, "又退回「强制置顶」的旧写法")

    def test_collapse_button_shares_mode_switch_row(self) -> None:
        """收起按钮必须与某个常驻控件同一行（独立空行很难看）。

        3.6.0 起它跟「对话 / 工作台」模式切换同一行：侧栏收放是两种视图共享的操作，
        桌面折叠后仍能通过展开按钮回到模式切换；工作台态只隐藏会话专用操作，
        不能连收起按钮一起藏掉。
        """
        index = (ROOT / "public/index.html").read_text(encoding="utf-8")
        self.assertNotIn('class="sidebar-top"', index, "独立的 .sidebar-top 空行又回来了")
        mode_row = index[index.index('class="sidebar-mode-row"'):]
        mode_row = mode_row[: mode_row.index('id="sidebarWorkspaceHeader"')]
        self.assertIn('id="collapseSidebar"', mode_row, "收起按钮应在模式切换行内（与模式按钮同一行）")
        self.assertIn('class="sidebar-mode-switch"', mode_row, "模式切换行缺少模式按钮组")
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        self.assertIn(
            'body[data-studio="on"] #sidebarWorkspaceHeader,',
            css,
            "工作台态只隐藏会话专用操作，收起按钮不跟着消失",
        )

    def test_conversation_item_click_area_covers_whole_row(self) -> None:
        """整条都可点（含上下边缘）：判定区必须覆盖整个条目元素。"""
        source = (ROOT / "public/js/08-conversations.js").read_text(encoding="utf-8")
        self.assertNotIn(
            "if (event.target.closest('.conversation-open')) openConversation",
            source,
            "又退回「只有中间文字能点」的旧写法",
        )
        self.assertIn("openConversation(item.dataset.conversationId)", source)
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        item_rule = css[css.index(".conversation-item {"):]
        item_rule = item_rule[: item_rule.index("}")]
        self.assertIn("cursor: pointer", item_rule, "条目缺少手型光标")

    def test_server_status_dot_is_the_only_indicator(self) -> None:
        """侧栏底部只留一个状态点：绿=已连接、红=连不上服务端。"""
        index = (ROOT / "public/index.html").read_text(encoding="utf-8")
        self.assertNotIn("serverLabel", index, "「服务已连接」文字又回来了")
        self.assertNotIn('class="server-state"', index, "旧的 server-state 状态块又回来了")
        dot_tag = index[: index.index('id="serverDot"')]
        dot_tag = dot_tag[dot_tag.rindex("<"):] + index[index.index('id="serverDot"'): index.index('id="serverDot"') + 120]
        self.assertIn("status-mark", dot_tag, "状态点缺少 status-mark 样式类")
        core = (ROOT / "public/js/01-core.js").read_text(encoding="utf-8")
        self.assertIn("export function setServerStatus(", core)
        api_block = core[core.index("export async function api("):]
        api_block = api_block[: api_block.index("\n}\n")]
        self.assertGreaterEqual(api_block.count("setServerStatus("), 2, "api() 未在两条路径刷新状态点")
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        self.assertIn(".status-mark.error", css)
        self.assertNotIn(".server-state", css, "旧状态块样式未清理")

    def test_collapsed_sidebar_keeps_chat_column_width(self) -> None:
        """收起侧栏只重新居中，不放大对话列（否则助手左移、用户气泡右移、整体错开）。"""
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        rule = css[css.index(".app-shell.sidebar-collapsed {"):]
        rule = rule[: rule.index("}")]
        self.assertNotIn("--chat-max", rule, "收起态又改了 --chat-max（对话列会跳宽）")
        self.assertIn("--sidebar-col: 0px", rule)

    def test_sidebar_scroll_clamp_uses_real_scroll_height(self) -> None:
        """虚拟窗口的滚动上限必须取浏览器真实 scrollHeight（含容器 padding）。

        旧实现用 sidebarTotalH - vh，少算了 .conversation-list 的上下 padding，
        每次滚动都被回写成偏小的值——滚轮“越滚越慢”、列表底部永远到不了。
        """
        source = (ROOT / "public/js/08-conversations.js").read_text(encoding="utf-8")
        self.assertIn("const maxScroll = Math.max(0, tree.scrollHeight - vh);", source)
        self.assertNotIn("Math.max(0, sidebarTotalH - vh)", source, "滚动上限又退回少算 padding 的旧写法")

    def test_sidebar_density_and_wheel_have_styles_and_handler(self) -> None:
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        self.assertIn("min-height: 34px", css)
        self.assertIn(".conversation-star", css)
        self.assertIn(".conversation-menu", css)
        self.assertIn("overflow-anchor: none", css)
        bind = (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")
        self.assertIn("'wheel'", bind)
        self.assertIn("* 1.5", bind)


if __name__ == "__main__":
    unittest.main()
