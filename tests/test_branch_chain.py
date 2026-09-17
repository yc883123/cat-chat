# -*- coding: utf-8 -*-
"""护栏：分支来源登记与分支链（迁移 v17 → v18、`ChatStorage.branch_chain`、侧栏徽标数据）。

保护对象：
- 迁移 v18 **纯增量、幂等**：两列默认空串；存量分支会话不回溯（按标题 `(N)` 弱推断不可靠，
  宁可显示为无关联）；重复跑迁移不报错、不清空既有值；
- `branch_conversation` 必须把「源 id + 分支点消息 id」写进新会话的两列
  —— 这是侧栏徽标与分支链面板唯一的数据来源；
- `list_conversations` 的 `branch_count` / `branch_source_title` 与
  `get_conversation` 的同一字段**口径一致**（列表与详情不能各说各话）；
- `branch_chain` 三种角色：`branch`（自己挂在某个源上，链 = 源 + 兄弟，含自己）、
  `source`（链 = 自己的全部分支，**自己不在链里**）、`none`（既不是分支也没分支 → 空链）；
- 源会话被删后 `branched_from_id` 悬空**不是错误**：`source_deleted` 为真、链里没有源项、
  会话本身仍能正常返回；只有「会话本身不存在」才抛 `LookupError`（路由层转 404）；
- 侧栏不画树的决策护栏：`branch_chain` 只回「平铺一串 + 谁是我的源」，不含任何深层嵌套字段。
"""

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.storage.store import (  # noqa: E402
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    ChatStorage,
)

ROOT = Path(__file__).resolve().parents[1]


class BranchSchemaMigrationTests(unittest.TestCase):
    """迁移 v18：两列纯增量、幂等，存量数据不回溯。"""

    def test_schema_version_and_migration_registered(self):
        self.assertGreaterEqual(CURRENT_SCHEMA_VERSION, 18)
        self.assertIn(18, MIGRATIONS)

    def test_migration_adds_both_columns_idempotently(self):
        with tempfile.TemporaryDirectory(prefix="naiba_branch_mig_") as tmp:
            db_path = Path(tmp) / "chat.db"
            storage = ChatStorage(db_path)  # 首次建库即跑到 v18
            with closing(sqlite3.connect(db_path)) as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(conversations)")}
            self.assertIn("branched_from_id", columns)
            self.assertIn("branch_message_id", columns)
            # 再跑一遍迁移：必须不报错（幂等），且不清空既有值。
            conversation = storage.create_conversation(title="甲")
            storage.apply_pending_migrations()
            storage.apply_pending_migrations()
            with closing(sqlite3.connect(db_path)) as db:
                row = db.execute(
                    "SELECT branched_from_id, branch_message_id FROM conversations WHERE id = ?",
                    (conversation["id"],),
                ).fetchone()
            self.assertEqual(tuple(row), ("", ""), "幂等重跑不能炸也不能改写")

    def test_legacy_branches_are_not_backfilled(self):
        """v18 之前的存量分支没有来源记录：明确不回溯，默认空串。"""
        with tempfile.TemporaryDirectory(prefix="naiba_branch_legacy_") as tmp:
            db_path = Path(tmp) / "chat.db"
            storage = ChatStorage(db_path)
            legacy = storage.create_conversation(title="旧分支 (1)")
            with closing(sqlite3.connect(db_path)) as db:
                row = db.execute(
                    "SELECT branched_from_id, branch_message_id FROM conversations WHERE id = ?",
                    (legacy["id"],),
                ).fetchone()
            self.assertEqual(tuple(row), ("", ""), "存量分支不得按标题弱推断来源")

    def test_migration_tolerates_preexisting_columns(self):
        """列已存在时 ALTER 必须被跳过（与其它迁移同口径的守卫写法）。"""
        with tempfile.TemporaryDirectory(prefix="naiba_branch_guard_") as tmp:
            db_path = Path(tmp) / "chat.db"
            ChatStorage(db_path)
            with closing(sqlite3.connect(db_path)) as db:
                MIGRATIONS[18](db)  # 列已存在 → 两个 try 都进 except，不得抛
            self.assertTrue(True)


class BranchConversationWriteTests(unittest.TestCase):
    """`branch_conversation` 必须把来源登记进新会话。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_branch_write_")
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.source = self.storage.create_conversation(title="源会话")
        self.first = self.storage.add_message(self.source["id"], "user", "第一轮问题")
        self.storage.add_message(self.source["id"], "assistant", "第一轮回答")
        self.second = self.storage.add_message(self.source["id"], "user", "第二轮问题")
        self.storage.add_message(self.source["id"], "assistant", "第二轮回答")

    def tearDown(self):
        self.tmp.cleanup()

    def _branch(self, message_id=None):
        """`branch_conversation` 回 `{conversation, branch_message}`，这里只取会话本体。"""
        result = self.storage.branch_conversation(self.source["id"], message_id or self.second["id"])
        self.assertIn("branch_message", result, "分支点消息随会话一起回传（预填输入框用）")
        return result["conversation"]

    def test_branch_records_source_and_message(self):
        branch = self._branch()
        self.assertEqual(branch["branched_from_id"], self.source["id"])
        self.assertEqual(branch["branch_message_id"], self.second["id"])

    def test_source_conversation_is_untouched(self):
        self._branch()
        source = self.storage.get_conversation(self.source["id"], include_messages=False)
        self.assertEqual(source["branched_from_id"], "", "源会话自己不挂来源")
        self.assertEqual(len(self.storage.get_conversation(self.source["id"])["messages"]), 4,
                         "分支是非破坏性的：源会话消息一条不少")

    def test_branch_copies_history_before_branch_point(self):
        branch = self._branch()
        contents = [m["content"] for m in self.storage.get_conversation(branch["id"])["messages"]]
        self.assertEqual(contents, ["第一轮问题", "第一轮回答"], "只复制分支点之前")

    def test_branch_count_and_source_title_in_list(self):
        branch = self._branch()
        listed = {c["id"]: c for c in self.storage.list_conversations()}
        self.assertEqual(listed[self.source["id"]]["branch_count"], 1, "源会话数到 1 个分支")
        self.assertEqual(listed[branch["id"]]["branch_count"], 0, "分支自身没有下级分支")
        # 源标题取库里**当前**值：首条 user 消息会顶掉 create_conversation 的初始标题。
        live_title = self.storage.get_conversation(self.source["id"], include_messages=False)["title"]
        self.assertEqual(listed[branch["id"]]["branch_source_title"], live_title)
        self.assertEqual(listed[self.source["id"]]["branched_from_id"], "")

    def test_branch_count_survives_multiple_branches(self):
        b1 = self._branch(self.first["id"])
        b2 = self._branch(self.second["id"])
        listed = {c["id"]: c for c in self.storage.list_conversations()}
        self.assertEqual(listed[self.source["id"]]["branch_count"], 2)
        for branch_id in (b1["id"], b2["id"]):
            self.assertEqual(listed[branch_id]["branched_from_id"], self.source["id"])

    def test_detail_and_list_agree_on_branch_fields(self):
        branch = self._branch()
        listed = {c["id"]: c for c in self.storage.list_conversations()}[branch["id"]]
        detail = self.storage.get_conversation(branch["id"], include_messages=False)
        for key in ("branched_from_id", "branch_message_id", "branch_count", "branch_source_title"):
            with self.subTest(key=key):
                self.assertEqual(detail[key], listed[key], "列表与详情必须同口径")

    def test_branch_only_from_user_message(self):
        assistant_ids = [
            m["id"] for m in self.storage.get_conversation(self.source["id"])["messages"]
            if m["role"] == "assistant"
        ]
        with self.assertRaises(ValueError):
            self.storage.branch_conversation(self.source["id"], assistant_ids[0])

    def test_branch_from_unknown_message_raises_lookup(self):
        with self.assertRaises(LookupError):
            self.storage.branch_conversation(self.source["id"], "missing-id")


class BranchChainTests(unittest.TestCase):
    """`branch_chain` 三角色 + 源删除降级。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_branch_chain_")
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.source = self.storage.create_conversation(title="源会话")
        self.anchor = self.storage.add_message(self.source["id"], "user", "分支点")
        self.storage.add_message(self.source["id"], "assistant", "回答")

    def tearDown(self):
        self.tmp.cleanup()

    def _branch(self, message_id=None):
        return self.storage.branch_conversation(
            self.source["id"], message_id or self.anchor["id"]
        )["conversation"]

    def test_role_none_when_no_branch_relation(self):
        lone = self.storage.create_conversation(title="独立会话")
        chain = self.storage.branch_chain(lone["id"])
        self.assertEqual(chain["role"], "none")
        self.assertEqual(chain["items"], [])
        self.assertFalse(chain["source_deleted"])
        self.assertIsNone(chain["source"])

    def test_role_source_lists_branches_without_self(self):
        b1 = self._branch()
        b2 = self._branch()
        chain = self.storage.branch_chain(self.source["id"])
        self.assertEqual(chain["role"], "source")
        ids = [item["id"] for item in chain["items"]]
        self.assertEqual(ids, [b1["id"], b2["id"]], "按创建时间正序")
        self.assertNotIn(self.source["id"], ids, "用户已经在源会话上，链里不该再出现它")

    def test_role_branch_lists_source_and_siblings(self):
        b1 = self._branch()
        b2 = self._branch()
        chain = self.storage.branch_chain(b1["id"])
        self.assertEqual(chain["role"], "branch")
        self.assertFalse(chain["source_deleted"])
        ids = [item["id"] for item in chain["items"]]
        self.assertEqual(ids, [self.source["id"], b1["id"], b2["id"]], "源在前、兄弟按序、含自己")
        self.assertEqual(chain["source"]["id"], self.source["id"])
        flags = {item["id"]: (item["is_source"], item["is_current"]) for item in chain["items"]}
        self.assertEqual(flags[self.source["id"]], (True, False))
        self.assertEqual(flags[b1["id"]], (False, True), "viewing 的那个是自己")
        self.assertEqual(flags[b2["id"]], (False, False))

    def test_items_carry_branch_message_id(self):
        branch = self._branch()
        chain = self.storage.branch_chain(branch["id"])
        mine = next(item for item in chain["items"] if item["id"] == branch["id"])
        self.assertEqual(mine["branch_message_id"], self.anchor["id"])

    def test_source_deleted_degrades_without_error(self):
        branch = self._branch()
        self.storage.delete_conversation(self.source["id"])
        chain = self.storage.branch_chain(branch["id"])
        self.assertEqual(chain["role"], "branch", "源删了也不算它变成 source")
        self.assertTrue(chain["source_deleted"], "悬空引用要显式标注，供徽标降级显示")
        self.assertIsNone(chain["source"])
        self.assertEqual([item["id"] for item in chain["items"]], [branch["id"]],
                         "链里没有源项，只剩尚存的自己")

    def test_unknown_conversation_raises_lookup(self):
        with self.assertRaises(LookupError):
            self.storage.branch_chain("no-such-conversation")

    def test_chain_is_flat_no_nested_children(self):
        """侧栏不画树的护栏：链项只描述自己，绝不嵌套下行数组。"""
        self._branch()
        chain = self.storage.branch_chain(self.source["id"])
        for item in chain["items"]:
            with self.subTest(item=item["id"]):
                self.assertNotIn("children", item)
                self.assertNotIn("depth", item)
        for key in ("role", "source_deleted", "source", "items", "conversation_id"):
            self.assertIn(key, chain)

    def test_source_title_reported_for_sidebar_badge(self):
        branch = self._branch()
        listed = {c["id"]: c for c in self.storage.list_conversations()}
        live_title = self.storage.get_conversation(self.source["id"], include_messages=False)["title"]
        self.assertEqual(live_title, "分支点", "源会话标题已被首条 user 消息顶掉")
        self.assertEqual(listed[branch["id"]]["branch_source_title"], live_title,
                         "徽标 tooltip 用源标题")


class BranchChainRouteTests(unittest.TestCase):
    """路由接线：`/branch_chain` 必须排在 `/api/conversations/<id>` 兜底之前。"""

    def test_route_registered_before_conversation_fallback(self):
        http = (ROOT / "naiba" / "http.py").read_text(encoding="utf-8")
        self.assertIn('path.endswith("/branch_chain")', http)
        self.assertIn("api_branch_chain", http)
        # 「兜底」专指不带 and 条件的那个 `elif path.startswith("/api/conversations/"):`
        # （GET 里它直接调 get_conversation）；带 `/file/open` 等后缀的前置分支不算。
        fallback = http.index('path.startswith("/api/conversations/"):')
        self.assertLess(
            http.index('path.endswith("/branch_chain")'),
            fallback,
            "排在兜底之后会被当成会话 id 吃掉",
        )
        self.assertIn("api_branch_chain", http[fallback - 900:fallback], "紧邻兜底之前")

    def test_unknown_conversation_maps_to_404(self):
        from types import SimpleNamespace

        from naiba.app import NaibaChatApp

        with tempfile.TemporaryDirectory(prefix="naiba_branch_404_") as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            payload, status = NaibaChatApp.api_branch_chain(SimpleNamespace(storage=storage), "nope")
            self.assertEqual(status, 404)
            self.assertIn("error", payload)

    def test_empty_conversation_id_is_400(self):
        from types import SimpleNamespace

        from naiba.app import NaibaChatApp

        payload, status = NaibaChatApp.api_branch_chain(SimpleNamespace(storage=None), "")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
