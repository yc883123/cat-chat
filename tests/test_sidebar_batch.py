# -*- coding: utf-8 -*-
"""对话侧栏批量管理（归档 / 取消归档 / 删除）的守门。

覆盖四层：

1. 存储 ``set_conversations_archived``：一条事务改完、不动 ``updated_at``、只碰主对话；
2. 存储 ``delete_conversation_if_idle``：判定与删除同事务，活跃任务一律 ``busy``；
3. ``POST /api/conversations/batch`` 的参数校验与逐项结果形状（部分失败仍 200）；
4. 前端静态结构：入口位置、分包上限与契约常量同源、两条栏的样式与 ``[hidden]`` 规则。

运行时报错拦不住（维护说明 9.167），浏览器行为另由 ``verify/sidebar_batch_smoke.cjs`` 验证。
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.app import NaibaChatApp  # noqa: E402
from naiba.core.contracts import (  # noqa: E402
    CONVERSATION_BATCH_ACTIONS,
    CONVERSATION_BATCH_FAILURE_CODES,
    CONVERSATION_BATCH_MAX_IDS,
)
from naiba.storage.store import ChatStorage  # noqa: E402


def _stub(storage: ChatStorage) -> SimpleNamespace:
    """业务层判定只用到 storage —— 与 test_message_search_api 的 stub 同口径。"""
    return SimpleNamespace(storage=storage)


class SidebarBatchTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_sidebar_batch_")
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.app = _stub(self.storage)

    def tearDown(self):
        self.tmp.cleanup()

    def _announced_at(self, conversation_id: str) -> int:
        conversation = self.storage.get_conversation(conversation_id, include_messages=False)
        return int(conversation["updated_at"])

    def _batch(self, **body):
        return NaibaChatApp.api_batch_conversations(self.app, body)


class BatchArchiveStorageTests(SidebarBatchTestBase):
    def test_archive_and_unarchive_keep_updated_at_and_messages(self):
        conversation = self.storage.create_conversation("归档对象")
        self.storage.add_message(conversation["id"], "user", "还在吗")
        before = self._announced_at(conversation["id"])

        changed = self.storage.set_conversations_archived([conversation["id"]], True)
        self.assertEqual(changed, [conversation["id"]])
        archived = self.storage.get_conversation(conversation["id"], include_messages=False)
        self.assertEqual(int(archived["archived"]), 1)
        self.assertEqual(int(archived["updated_at"]), before, "归档不能推进 updated_at")
        self.assertEqual(self.storage.message_count(conversation["id"]), 1, "归档只隐藏，不删消息")

        changed = self.storage.set_conversations_archived([conversation["id"]], False)
        self.assertEqual(changed, [conversation["id"]])
        restored = self.storage.get_conversation(conversation["id"], include_messages=False)
        self.assertEqual(int(restored["archived"]), 0)

    def test_batch_archive_skips_aux_and_missing_ids(self):
        main = self.storage.create_conversation("主对话")
        aux = self.storage.create_conversation("辅助对话", conv_kind="aux")

        changed = self.storage.set_conversations_archived(
            [main["id"], aux["id"], "does-not-exist"], True
        )
        self.assertEqual(changed, [main["id"]])
        aux_row = self.storage.get_conversation(aux["id"], include_messages=False)
        self.assertEqual(int(aux_row["archived"]), 0, "辅助对话不该被批量入口改状态")


class BatchDeleteStorageTests(SidebarBatchTestBase):
    def test_idle_conversation_is_deleted_with_messages(self):
        conversation = self.storage.create_conversation("待删")
        self.storage.add_message(conversation["id"], "user", "内容")
        self.storage.add_message(conversation["id"], "assistant", "回复")

        self.assertEqual(self.storage.delete_conversation_if_idle(conversation["id"]), "deleted")
        self.assertIsNone(self.storage.get_conversation(conversation["id"]))
        self.assertEqual(self.storage.message_count(conversation["id"]), 0, "消息必须级联删掉")

    def test_active_run_blocks_delete_until_finished(self):
        conversation = self.storage.create_conversation("正在回复")
        run = self.storage.create_chat_run(
            conversation["id"],
            "在吗",
            [],
            {"id": "agent"},
            {"interaction_mode": "craft"},
            "craft",
        )

        self.assertEqual(self.storage.delete_conversation_if_idle(conversation["id"]), "busy")
        self.assertIsNotNone(self.storage.get_conversation(conversation["id"]))
        self.assertEqual(self.storage.message_count(conversation["id"]), 1, "跳过删除时消息不动")

        self.storage.update_background_task(run["id"], status="completed", finished=True)
        self.assertEqual(self.storage.delete_conversation_if_idle(conversation["id"]), "deleted")

    def test_aux_and_missing_results(self):
        aux = self.storage.create_conversation("辅助", conv_kind="aux")
        self.assertEqual(self.storage.delete_conversation_if_idle(aux["id"]), "not_main")
        self.assertEqual(self.storage.delete_conversation_if_idle("nope"), "missing")


class BatchApiTests(SidebarBatchTestBase):
    def test_contract_registry(self):
        self.assertEqual(set(CONVERSATION_BATCH_ACTIONS), {"archive", "unarchive", "delete"})
        self.assertEqual(CONVERSATION_BATCH_MAX_IDS, 100)
        self.assertEqual(set(CONVERSATION_BATCH_FAILURE_CODES), {"not_main", "missing", "busy"})

    def test_invalid_requests_are_400(self):
        conversation = self.storage.create_conversation("会话")
        cases = [
            {"action": "purge", "ids": [conversation["id"]]},
            {"action": "delete", "ids": "not-a-list"},
            {"action": "delete", "ids": [conversation["id"], 42]},
            {"action": "delete", "ids": []},
            {"action": "delete", "ids": ["   "]},
            {"action": "delete", "ids": [f"id-{i}" for i in range(CONVERSATION_BATCH_MAX_IDS + 1)]},
        ]
        for body in cases:
            payload, status = self._batch(**body)
            self.assertEqual(status, 400, f"{body!r} 应回 400，实际 {status} {payload}")
            self.assertIn("error", payload)

    def test_archive_response_shape_and_dedupe(self):
        first = self.storage.create_conversation("一")
        second = self.storage.create_conversation("二")

        payload, status = self._batch(
            action="archive", ids=[first["id"], first["id"], second["id"]]
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["action"], "archive")
        self.assertEqual(payload["succeeded"].count(first["id"]), 1, "重复 id 只算一次")
        self.assertEqual(set(payload["succeeded"]), {first["id"], second["id"]})
        self.assertEqual(payload["failed"], [])

    def test_archive_missing_id_reports_missing(self):
        conversation = self.storage.create_conversation("在")
        payload, status = self._batch(action="archive", ids=[conversation["id"], "gone"])
        self.assertEqual(status, 200)
        self.assertEqual(payload["succeeded"], [conversation["id"]])
        self.assertEqual(payload["failed"][0]["code"], "missing")
        self.assertEqual(payload["failed"][0]["id"], "gone")

    def test_delete_mixes_idle_busy_and_missing(self):
        idle = self.storage.create_conversation("空闲")
        busy = self.storage.create_conversation("忙")
        self.storage.create_chat_run(
            busy["id"], "在吗", [], {"id": "agent"}, {"interaction_mode": "craft"}, "craft"
        )

        payload, status = self._batch(action="delete", ids=[idle["id"], busy["id"], "gone"])
        self.assertEqual(status, 200)
        self.assertEqual(set(payload["succeeded"]), {idle["id"], "gone"})
        self.assertEqual([item["id"] for item in payload["failed"]], [busy["id"]])
        self.assertEqual(payload["failed"][0]["code"], "busy")
        self.assertIsNotNone(self.storage.get_conversation(busy["id"]), "忙碌会话必须保留")


class SidebarBatchFrontendTests(unittest.TestCase):
    def setUp(self):
        self.index_html = (ROOT / "public/index.html").read_text(encoding="utf-8")
        self.conversations_js = (ROOT / "public/js/08-conversations.js").read_text(encoding="utf-8")
        self.styles = (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def test_entry_sits_between_sort_and_add_workspace(self):
        block = self.index_html[
            self.index_html.index('id="workspaceSort"'): self.index_html.index('id="addWorkspace"')
        ]
        self.assertIn('id="workspaceBatchToggle"', block, "批量管理入口应在排序与新建工作区之间")
        self.assertIn("批量管理", block)
        self.assertNotIn("M7 4v16", block, "入口图标不能复用旧双向箭头")

    def test_status_row_and_action_bar_exist(self):
        for element_id in ("workspaceBatchStatus", "workspaceBatchAll", "workspaceBatchCount", "workspaceBatchDone"):
            self.assertIn(f'id="{element_id}"', self.index_html, f"缺少 {element_id}")
        for element_id in ("workspaceBatchActions", "workspaceBatchArchive", "workspaceBatchDelete"):
            self.assertIn(f'id="{element_id}"', self.index_html, f"缺少 {element_id}")
        # 两条栏默认隐藏，由批量模式开合：取整个开标签再找 hidden。
        for element_id in ("workspaceBatchStatus", "workspaceBatchActions"):
            start = self.index_html.index(f'id="{element_id}"')
            tag_start = self.index_html.rindex("<", 0, start)
            tag_end = self.index_html.index(">", start)
            self.assertIn("hidden", self.index_html[tag_start:tag_end], f"{element_id} 应默认 hidden")

    def test_frontend_posts_to_batch_endpoint(self):
        self.assertIn("/api/conversations/batch", self.conversations_js)
        # 单条删除接口在批量链路里不得被循环调用（守门 test_sidebar_favorites 的既有约束）
        self.assertNotIn("delete-conversation", self.conversations_js)

    def test_frontend_chunks_within_contract_limit(self):
        archive = re.search(r"BATCH_ARCHIVE_CHUNK\s*=\s*(\d+)", self.conversations_js)
        delete = re.search(r"BATCH_DELETE_CHUNK\s*=\s*(\d+)", self.conversations_js)
        self.assertIsNotNone(archive, "归档每批上限必须显式声明")
        self.assertIsNotNone(delete, "删除每批上限必须显式声明")
        for match in (archive, delete):
            self.assertLessEqual(int(match.group(1)), CONVERSATION_BATCH_MAX_IDS)
        self.assertLessEqual(int(delete.group(1)), int(archive.group(1)), "删除批更小，进度才动")

    def test_bars_have_styles_and_hidden_rules(self):
        for selector in (".workspace-batch-status", ".workspace-batch-actions"):
            self.assertIn(selector, self.styles, f"缺少 {selector} 样式")
            self.assertRegex(
                self.styles,
                re.escape(selector) + r"\[hidden\]\s*\{[^}]*display:\s*none",
                f"{selector} 必须自带 [hidden] 规则（display:flex 会盖掉 UA 的 hidden）",
            )
        self.assertIn(".conversation-check", self.styles, "勾选框缺少样式")
        self.assertIn("is-batch-selected", self.styles, "选中行缺少底色")


if __name__ == "__main__":
    unittest.main()
