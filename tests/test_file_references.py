# -*- coding: utf-8 -*-
"""护栏：输入框 @ 工作区引用（文件/目录）的解析、浏览与提交规格。

保护对象：
- ``resolve_file_references``：只替换"会话工作区内真实存在"的 @token（文件→绝对路径，
  目录→绝对路径+尾分隔符），邮箱/普通 @ 提及/越界/不存在一律原样保留；
- ``browse_workspace_tree``：只读浅层浏览 + 越界拒绝 + 相对路径/返回上级 + 条目上限；
- ``submit_chat``：落库的模型可见文本已解析为绝对路径，气泡展示与首轮标题仍取用户原文。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.config import ConfigStore  # noqa: E402
from naiba.core.conv_files import (  # noqa: E402
    browse_workspace_tree,
    resolve_file_references,
)
from naiba.run.manager import ConversationRunManager  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402


class _VisionStub:
    def resolve_brain_supports_images(self, profile, probe_if_unknown=False):
        return False


class _CatalogStub:
    def scan(self):
        return {}


class _RegistryStub:
    def schemas(self):
        return []

    def readonly_mcp_tools(self):
        return []


class _SearchStub:
    def is_available(self):
        return False


class ResolveFileReferencesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.ws = self.root / "ws"
        (self.ws / "docs" / "api").mkdir(parents=True)
        (self.ws / "docs" / "api" / "README.md").write_text("hi", encoding="utf-8")
        (self.ws / "notes.md").write_text("note", encoding="utf-8")
        (self.ws / "my file.md").write_text("space", encoding="utf-8")
        (self.root / "outside.md").write_text("outside", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_plain_text_untouched(self):
        self.assertEqual(resolve_file_references("普通消息，没有引用", self.ws), "普通消息，没有引用")

    def test_none_root_passthrough(self):
        self.assertEqual(resolve_file_references("看 @notes.md", None), "看 @notes.md")

    def test_file_reference_becomes_absolute_path(self):
        resolved = resolve_file_references("看 @notes.md 谢谢", self.ws)
        self.assertEqual(resolved, f"看 {self.ws / 'notes.md'} 谢谢")

    def test_nested_relative_path(self):
        resolved = resolve_file_references("@docs/api/README.md", self.ws)
        self.assertEqual(resolved, str(self.ws / "docs" / "api" / "README.md"))

    def test_directory_reference_keeps_trailing_separator(self):
        resolved = resolve_file_references("处理 @docs/api/ 下所有文件", self.ws)
        self.assertEqual(resolved, f"处理 {self.ws / 'docs' / 'api'}{os.sep} 下所有文件")

    def test_directory_without_trailing_slash_also_resolves(self):
        resolved = resolve_file_references("@docs/api", self.ws)
        self.assertEqual(resolved, f"{self.ws / 'docs' / 'api'}{os.sep}")

    def test_quoted_path_with_spaces(self):
        resolved = resolve_file_references('看 @"my file.md" 谢谢', self.ws)
        self.assertEqual(resolved, f"看 {self.ws / 'my file.md'} 谢谢")

    def test_trailing_punctuation_kept(self):
        resolved = resolve_file_references("见 @notes.md。", self.ws)
        self.assertEqual(resolved, f"见 {self.ws / 'notes.md'}。")

    def test_missing_file_untouched(self):
        self.assertEqual(resolve_file_references("@不存在.md", self.ws), "@不存在.md")

    def test_outside_workspace_untouched(self):
        text = "@../outside.md"
        self.assertEqual(resolve_file_references(text, self.ws), text)

    def test_email_not_treated_as_reference(self):
        text = "联系 a@b.com 获取"
        self.assertEqual(resolve_file_references(text, self.ws), text)

    def test_absolute_path_inside_workspace_resolves_to_itself(self):
        target = str(self.ws / "notes.md")
        self.assertEqual(resolve_file_references(f"@{target}", self.ws), target)

    def test_multiple_references(self):
        resolved = resolve_file_references("@notes.md 与 @docs/api/README.md", self.ws)
        self.assertIn(str(self.ws / "notes.md"), resolved)
        self.assertIn(str(self.ws / "docs" / "api" / "README.md"), resolved)


class BrowseWorkspaceTreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "docs" / "api").mkdir(parents=True)
        (self.root / "docs" / "guide.md").write_text("g", encoding="utf-8")
        (self.root / "notes.md").write_text("n", encoding="utf-8")
        (self.root / ".hidden").write_text("h", encoding="utf-8")
        (self.root / ".git").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_root_listing_dirs_first_and_rel(self):
        listing = browse_workspace_tree(self.root, "", hide_dotfiles=True)
        self.assertEqual(listing["rel"], "")
        self.assertEqual(listing["parent_rel"], "")
        names = [item["name"] for item in listing["entries"]]
        self.assertEqual(names, ["docs", "notes.md"], "目录优先且隐藏点号条目")
        self.assertEqual(listing["entries"][0]["kind"], "directory")
        self.assertEqual(listing["entries"][0]["rel"], "docs")
        self.assertEqual(listing["entries"][1]["rel"], "notes.md")
        self.assertEqual(listing["entries"][1]["size"], 1)

    def test_subdir_listing_has_parent_rel(self):
        listing = browse_workspace_tree(self.root, "docs", hide_dotfiles=True)
        self.assertEqual(listing["rel"], "docs")
        self.assertEqual(listing["parent_rel"], "")
        listing = browse_workspace_tree(self.root, "docs/api", hide_dotfiles=True)
        self.assertEqual(listing["rel"], "docs/api")
        self.assertEqual(listing["parent_rel"], "docs")
        self.assertEqual(listing["entries"], [])

    def test_hidden_entries_kept_when_not_hiding_dotfiles(self):
        listing = browse_workspace_tree(self.root, "", hide_dotfiles=False)
        names = [item["name"] for item in listing["entries"]]
        self.assertIn(".hidden", names)
        self.assertNotIn(".git", names, "VCS 目录恒定隐藏")

    def test_outside_path_rejected(self):
        with self.assertRaises(ValueError):
            browse_workspace_tree(self.root, "../", hide_dotfiles=True)
        with self.assertRaises(ValueError):
            browse_workspace_tree(self.root, str(self.root.parent), hide_dotfiles=True)

    def test_missing_dir_rejected(self):
        with self.assertRaises(ValueError):
            browse_workspace_tree(self.root, "不存在", hide_dotfiles=True)

    def test_entry_limit_marks_truncated(self):
        for index in range(5):
            (self.root / f"f{index}.txt").write_text("x", encoding="utf-8")
        listing = browse_workspace_tree(self.root, "", hide_dotfiles=True, limit=3)
        self.assertEqual(len(listing["entries"]), 3)
        self.assertTrue(listing["truncated"])


class SubmitChatFileReferenceTests(unittest.TestCase):
    """提交链路：模型可见文本已解析、展示文本与标题保留用户原文。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.workspace = root / "ws"
        self.workspace.mkdir()
        (self.workspace / "notes.md").write_text("note", encoding="utf-8")
        self.storage = ChatStorage(root / "chat.db")
        app = SimpleNamespace(
            storage=self.storage,
            config=ConfigStore(root / "config.json"),
            catalog=_CatalogStub(),
            vision=_VisionStub(),
            tool_registry=_RegistryStub(),
            web_search=_SearchStub(),
            # submit_chat 的附件落地校验（missing_cache_attachment）需要数据目录。
            paths=SimpleNamespace(data_dir=root / "data"),
        )
        self.manager = ConversationRunManager(app)
        app.config.upsert_model_profile({
            "id": "submit-fixture", "kind": "online", "name": "提交夹具",
            "base_url": "http://127.0.0.1:9/v1", "model": "test-model",
        })
        self.conversation = self.storage.create_conversation(
            workspace_dir=str(self.workspace), model_name="test-model"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _submit(self, body):
        with mock.patch.object(self.manager, "_start", lambda run, target: None):
            return self.manager.submit_chat(body)

    def test_reference_resolved_for_model_and_kept_for_display(self):
        self._submit({
            "conversation_id": self.conversation["id"],
            "message": "帮我看看 @notes.md",
            "display_message": "帮我看看 @notes.md",
        })
        conversation = self.storage.get_conversation(self.conversation["id"])
        user = [item for item in conversation["messages"] if item["role"] == "user"][0]
        self.assertEqual(user["content"], f"帮我看看 {self.workspace / 'notes.md'}", "模型可见文本已解析")
        self.assertEqual(user["metadata"]["display_content"], "帮我看看 @notes.md", "气泡保留用户原文")
        self.assertEqual(conversation["title"], "帮我看看 @notes.md", "标题取用户原文")

    def test_unresolvable_reference_left_as_is(self):
        self._submit({
            "conversation_id": self.conversation["id"],
            "message": "见 @不存在.md",
        })
        conversation = self.storage.get_conversation(self.conversation["id"])
        user = [item for item in conversation["messages"] if item["role"] == "user"][0]
        self.assertEqual(user["content"], "见 @不存在.md")


if __name__ == "__main__":
    unittest.main()
