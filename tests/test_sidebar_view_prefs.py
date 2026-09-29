# -*- coding: utf-8 -*-
"""侧栏「分组与排序」升级的守门（照 DeepSeek Harness 口径）。

本轮三段互相关联的改动必须在同一处被钉住：

1. 迁移 v19/v20：``conversations.archived`` 与 ``conversations.sort_order`` 都是
   纯增量列（幂等），归档/排序位都**不得推进 ``updated_at``**（否则侧栏重排）；
2. 归档语义：仅从侧栏与全局全文搜索隐藏、不删数据、可打开继续聊、不进收藏组；
3. 三项偏好（分组方式/排序方式/筛选会话）走服务端 ``settings.sidebar``，
   枚举校验与部分更新合并；前端面板/单列表/拖拽的静态结构不许退化。
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


class SidebarMigrationTests(unittest.TestCase):
    """迁移 v19（归档）/ v20（手动排序位）：列存在、幂等、旧库可升级。"""

    def test_schema_version_registers_v19_v20(self) -> None:
        self.assertGreaterEqual(CURRENT_SCHEMA_VERSION, 20)
        self.assertIn(19, MIGRATIONS)
        self.assertIn(20, MIGRATIONS)

    def test_migrations_add_columns_and_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="naiba_sidebar_mig_") as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            conn = sqlite3.connect(storage.db_path)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
                self.assertIn("archived", columns, "初始化后必须已有 archived 列")
                self.assertIn("sort_order", columns, "初始化后必须已有 sort_order 列")
                # 重复执行不得抛错（列已存在时跳过）。
                MIGRATIONS[19](conn)
                MIGRATIONS[20](conn)
                MIGRATIONS[19](conn)
                MIGRATIONS[20](conn)
                again = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
                self.assertIn("archived", again)
                self.assertIn("sort_order", again)
            finally:
                conn.close()

    def test_legacy_database_upgrades(self) -> None:
        """模拟 v18 老库：删列后重跑迁移必须补回且默认 0。"""
        with tempfile.TemporaryDirectory(prefix="naiba_sidebar_legacy_") as tmp:
            db_path = Path(tmp) / "chat.db"
            storage = ChatStorage(db_path)
            conv = storage.create_conversation("legacy", "老会话")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("ALTER TABLE conversations DROP COLUMN archived")
                conn.execute("ALTER TABLE conversations DROP COLUMN sort_order")
                conn.commit()
                MIGRATIONS[19](conn)
                MIGRATIONS[20](conn)
                conn.commit()
                row = conn.execute(
                    "SELECT archived, sort_order FROM conversations WHERE id = ?",
                    (str(conv["id"]),),
                ).fetchone()
                self.assertEqual(int(row[0]), 0)
                self.assertEqual(int(row[1]), 0)
            finally:
                conn.close()


class ArchiveStorageTests(unittest.TestCase):
    """归档的读写回路与「不影响其他字段」的不变量。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="naiba_archive_ws_")
        self.addCleanup(self._tmp.cleanup)
        self.storage = ChatStorage(Path(self._tmp.name) / "chat.db")
        self.conv_id = str(self.storage.create_conversation("c1", "归档测试")["id"])

    def test_defaults_to_not_archived(self) -> None:
        row = self.storage.get_conversation(self.conv_id, include_messages=False)
        self.assertEqual(int(row["archived"]), 0)

    def test_toggle_round_trip_through_list_and_get(self) -> None:
        updated = self.storage.set_conversation_archived(self.conv_id, True)
        self.assertEqual(int(updated["archived"]), 1)
        listed = next(
            item for item in self.storage.list_conversations() if item["id"] == self.conv_id
        )
        self.assertEqual(int(listed["archived"]), 1)
        self.assertEqual(
            int(self.storage.get_conversation(self.conv_id, include_messages=False)["archived"]), 1
        )
        again = self.storage.set_conversation_archived(self.conv_id, False)
        self.assertEqual(int(again["archived"]), 0)

    def test_archive_does_not_bump_updated_at(self) -> None:
        """归档不得推进 updated_at：否则切「全部对话」时整列顺序被打乱。"""
        before = self.storage.get_conversation(self.conv_id, include_messages=False)
        self.storage.set_conversation_archived(self.conv_id, True)
        after = self.storage.get_conversation(self.conv_id, include_messages=False)
        self.assertEqual(before["updated_at"], after["updated_at"], "归档改动了 updated_at（会重排侧栏）")

    def test_archive_keeps_messages_alive(self) -> None:
        """归档=仅隐藏：消息必须原样保留（打开继续聊 / 取消归档后完整可见）。"""
        self.storage.add_message(self.conv_id, "user", "归档前的一句话")
        self.storage.set_conversation_archived(self.conv_id, True)
        conversation = self.storage.get_conversation(self.conv_id)
        self.assertEqual(len(conversation["messages"]), 1)
        self.assertEqual(conversation["messages"][0]["content"], "归档前的一句话")

    def test_global_full_text_search_excludes_archived(self) -> None:
        """全局全文搜索默认排除归档；指定会话 id 的站内搜索不排除。"""
        self.storage.add_message(self.conv_id, "user", "独特的干饭宣言正文")
        self.storage.set_conversation_archived(self.conv_id, True)
        self.assertEqual(self.storage.search_messages("干饭宣言")["total_hits"], 0)
        scoped = self.storage.search_messages("干饭宣言", conversation_id=self.conv_id)
        self.assertEqual(scoped["total_hits"], 1, "归档会话内部的搜索不应被排除")
        self.storage.set_conversation_archived(self.conv_id, False)
        self.assertEqual(self.storage.search_messages("干饭宣言")["total_hits"], 1)


class ManualSortStorageTests(unittest.TestCase):
    """手动排序位：整体重排 1..N、单事务、不动 updated_at。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="naiba_sort_ws_")
        self.addCleanup(self._tmp.cleanup)
        self.storage = ChatStorage(Path(self._tmp.name) / "chat.db")
        self.ids = [
            str(self.storage.create_conversation(f"c{i}", f"排序测试 {i}")["id"])
            for i in range(3)
        ]

    def test_reorder_assigns_dense_ranks(self) -> None:
        written = self.storage.set_conversation_sort_order(list(reversed(self.ids)))
        self.assertEqual(written, 3)
        listed = self.storage.list_conversations()
        rank = {item["id"]: int(item["sort_order"]) for item in listed}
        self.assertEqual([rank[cid] for cid in reversed(self.ids)], [1, 2, 3])

    def test_unknown_ids_are_skipped(self) -> None:
        written = self.storage.set_conversation_sort_order(["ghost-id", self.ids[0]])
        self.assertEqual(written, 1)
        row = self.storage.get_conversation(self.ids[0], include_messages=False)
        self.assertEqual(int(row["sort_order"]), 1)

    def test_empty_order_is_noop(self) -> None:
        self.assertEqual(self.storage.set_conversation_sort_order([]), 0)

    def test_reorder_does_not_bump_updated_at(self) -> None:
        before = {
            cid: self.storage.get_conversation(cid, include_messages=False)["updated_at"]
            for cid in self.ids
        }
        self.storage.set_conversation_sort_order(list(reversed(self.ids)))
        for cid in self.ids:
            after = self.storage.get_conversation(cid, include_messages=False)["updated_at"]
            self.assertEqual(before[cid], after, f"重排推进了 {cid} 的 updated_at")


class SidebarPrefsApiTests(unittest.TestCase):
    """HTTP 层口径：archived 只收布尔；reorder 校验数组；sidebar 偏好枚举校验。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="naiba_sidebar_api_")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        from naiba.app import NaibaChatApp
        from naiba.paths import PathContext

        self.paths = PathContext.local(root, root / "config.json")
        self.app = NaibaChatApp(paths=self.paths)
        created, status = self.app.api_create_conversation({"title": "接口归档"})
        self.assertEqual(int(status), 201)
        self.conv_id = str(created["id"])

    def test_archived_rejects_non_boolean(self) -> None:
        payload, status = self.app.api_update_conversation_settings(self.conv_id, {"archived": "yes"})
        self.assertEqual(int(status), 400)
        self.assertIn("archived", payload.get("error", ""))

    def test_archived_accepts_boolean_and_returns_field(self) -> None:
        payload, status = self.app.api_update_conversation_settings(self.conv_id, {"archived": True})
        self.assertEqual(int(status), 200)
        self.assertEqual(int(payload["archived"]), 1)
        payload, status = self.app.api_update_conversation_settings(self.conv_id, {"archived": False})
        self.assertEqual(int(status), 200)
        self.assertEqual(int(payload["archived"]), 0)

    def test_reorder_rejects_non_list(self) -> None:
        payload, status = self.app.api_reorder_conversations({"order": "nope"})
        self.assertEqual(int(status), 400)
        payload, status = self.app.api_reorder_conversations({"order": [1, 2]})
        self.assertEqual(int(status), 400)

    def test_reorder_accepts_id_list(self) -> None:
        payload, status = self.app.api_reorder_conversations({"order": [self.conv_id]})
        self.assertEqual(int(status), 200)
        self.assertEqual(int(payload["written"]), 1)
        row = self.app.storage.get_conversation(self.conv_id, include_messages=False)
        self.assertEqual(int(row["sort_order"]), 1)

    def test_sidebar_prefs_validation_and_merge(self) -> None:
        from naiba.config import ConfigStore

        config = self.app.config
        config.update_settings({"sidebar": {"group": "flat"}})
        self.assertEqual(config.public()["sidebar"], {"group": "flat", "sort": "updated", "filter": "hide"})
        config.update_settings({"sidebar": {"sort": "manual", "filter": "only"}})
        self.assertEqual(
            config.public()["sidebar"],
            {"group": "flat", "sort": "manual", "filter": "only"},
        )
        for bad, key in (
            ({"group": "tree"}, "group"),
            ({"sort": "random"}, "sort"),
            ({"filter": "sometimes"}, "filter"),
            ({"hacker": True}, "sidebar 包含不支持的字段"),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as ctx:
                    config.update_settings({"sidebar": bad})
                self.assertIn(key if key != "sidebar 包含不支持的字段" else key, str(ctx.exception) or key)


class SidebarViewFrontendTests(unittest.TestCase):
    """前端静态结构：图标、面板三段、归档入口、单列表与拖拽不许退化。"""

    def test_sort_button_uses_sliders_icon_and_menu(self) -> None:
        index = (ROOT / "public/index.html").read_text(encoding="utf-8")
        block = index[index.index('id="workspaceSort"'): index.index('id="addWorkspace"')]
        self.assertIn("分组与排序", block)
        self.assertIn("M4 8h16M4 16h16", block, "滑杆图标（双横线）不见了")
        self.assertIn('r="2.4"', block, "滑杆图标（两个圆钮）不见了")
        self.assertNotIn("M7 4v16", block, "旧的双向箭头图标还在")

    def test_view_menu_has_three_sections(self) -> None:
        index = (ROOT / "public/index.html").read_text(encoding="utf-8")
        menu = index[index.index('id="sidebarViewMenu"'):]
        menu = menu[: menu.index("</div>\n  </div>") if "</div>\n  </div>" in menu else menu.index("conversation-menu")]
        for label in ("分组方式", "排序方式", "筛选会话"):
            self.assertIn(label, menu, f"面板缺少「{label}」段")
        for value in ("workspace", "flat", "manual", "updated", "name", "hide", "all", "only"):
            self.assertIn(f'data-view-value="{value}"', menu, f"面板缺少选项 {value}")

    def test_conversation_menu_has_archive_action(self) -> None:
        index = (ROOT / "public/index.html").read_text(encoding="utf-8")
        menu = index[index.index('id="conversationItemMenu"'):]
        menu = menu[: menu.index("</div>")]
        self.assertIn('data-conversation-action="archive"', menu)
        self.assertIn("归档", menu)

    def test_frontend_renders_filter_and_flat_and_drag(self) -> None:
        source = (ROOT / "public/js/08-conversations.js").read_text(encoding="utf-8")
        self.assertIn("sidebarFilter", source, "筛选三档没有接入渲染")
        self.assertIn("sidebarGroup === 'flat'", source, "单列表模式没有接入渲染")
        self.assertIn("is-archived", source, "归档会话行没有标记")
        self.assertIn("archived-tag", source, "归档标签没有渲染")
        self.assertIn("export function bindSidebarDragDrop(", source, "拖拽绑定入口不见了")
        self.assertIn("sort_order", source, "手动排序口径没有接入")
        self.assertIn("api('/api/conversations/reorder'", source, "排序落库请求没有接入")
        # 收藏组必须排除归档：不能让「已收藏」成为绕过筛选的出口。
        favorites_block = source[source.index("const favoriteList = sortConv("):]
        favorites_block = favorites_block[: favorites_block.index(";")]
        self.assertIn("visibleConversations", favorites_block, "收藏组仍在包含归档会话的列表上取数")

    def test_frontend_menu_wiring(self) -> None:
        bind = (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")
        self.assertIn("openSidebarViewMenu", bind, "排序按钮没有接「分组与排序」面板")
        self.assertNotIn(
            "state.workspaceSort = state.workspaceSort === 'updated' ? 'name' : 'updated'",
            bind,
            "旧的时间/名称硬切换 handler 还在",
        )
        self.assertIn("toggleConversationArchive", bind, "「⋯」菜单的归档动作没有接线")
        self.assertIn("bindSidebarDragDrop()", bind, "拖拽绑定没有初始化")

    def test_bootstrap_syncs_sidebar_prefs(self) -> None:
        bootstrap = (ROOT / "public/js/05-bootstrap.js").read_text(encoding="utf-8")
        self.assertIn("syncSidebarPrefsFromBootstrap(state.bootstrap)", bootstrap)
        core = (ROOT / "public/js/01-core.js").read_text(encoding="utf-8")
        self.assertIn("export function syncSidebarPrefsFromBootstrap(", core)
        self.assertIn("export async function saveSidebarPrefs(", core)
        self.assertIn("body: { sidebar: next }", core, "偏好没有走服务端 settings.sidebar")

    def test_menu_styles_exist(self) -> None:
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        for selector in (".sidebar-view-menu", ".archived-tag", ".conversation-item.is-dragging",
                         ".conversation-item.drop-target"):
            self.assertIn(selector, css, f"缺少样式 {selector}")


if __name__ == "__main__":
    unittest.main()
