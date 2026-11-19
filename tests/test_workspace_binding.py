# -*- coding: utf-8 -*-
"""Regression coverage for registered workspace name-to-directory bindings."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import ConfigStore  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402


class WorkspaceBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.materials = self.root / "materials-4"
        self.input_dir = self.root / "input"
        self.materials.mkdir()
        self.input_dir.mkdir()
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps({
            "workspaces": [
                {"name": "素材4", "dir": str(self.materials)},
                {"name": "input", "dir": str(self.input_dir)},
            ]
        }, ensure_ascii=False), encoding="utf-8")
        self.config = ConfigStore(self.config_path)
        self.storage = ChatStorage(self.root / "chat.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_registered_group_resolves_to_its_registered_directory(self):
        self.assertEqual(self.config.workspace_dir_for_group("素材4"), str(self.materials))
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.config.workspace_dir_for_group("missing")

    def test_startup_repair_corrects_mismatched_registered_group(self):
        conversation = self.storage.create_conversation(
            workspace_group="素材4", workspace_dir=str(self.input_dir)
        )
        self.assertEqual(
            self.storage.synchronize_workspace_bindings(self.config.workspace_bindings()), 1
        )
        repaired = self.storage.get_conversation(conversation["id"], include_messages=False)
        self.assertEqual(repaired["workspace_group"], "素材4")
        self.assertEqual(repaired["workspace_dir"], str(self.materials))

    def test_ungrouped_and_removed_groups_keep_their_directories(self):
        ungrouped = self.storage.create_conversation(workspace_dir=str(self.input_dir))
        removed = self.storage.create_conversation(
            workspace_group="removed", workspace_dir=str(self.input_dir)
        )
        workspace_file = self.input_dir / "keep-after-delete.txt"
        workspace_file.write_text("用户交付物", encoding="utf-8")
        self.assertEqual(
            self.storage.synchronize_workspace_bindings(self.config.workspace_bindings()), 0
        )
        self.assertEqual(
            self.storage.get_conversation(ungrouped["id"], include_messages=False)["workspace_dir"],
            str(self.input_dir),
        )
        self.assertEqual(
            self.storage.get_conversation(removed["id"], include_messages=False)["workspace_dir"],
            str(self.input_dir),
        )
        self.assertTrue(self.storage.delete_conversation(ungrouped["id"]))
        self.assertTrue(workspace_file.is_file(), "删除会话不得删除 workspace 用户文件")

