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

    # ---- 迁移 v25 ----

    def test_fresh_db_reaches_v25_with_conv_kind(self):
        self.assertEqual(CURRENT_SCHEMA_VERSION, 25)
        with self.storage._connect() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(conversations)")}
        self.assertIn("conv_kind", columns)
        conv = self.storage.create_conversation()
        self.assertEqual(conv["conv_kind"], "main")

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


if __name__ == "__main__":
    unittest.main()
