# -*- coding: utf-8 -*-
"""护栏：ConfigStore 配置加载与迁移行为规格。

保护对象：阶段 3 将 ConfigStore 迁出 server.py 到 naiba/config.py 时的行为等价性。
覆盖：全新安装默认、死工具名映射（run_command→pwsh）、历史默认 MCP 入口移除、
MCP 去重与 comfyui 退役、vision 超时迁移、skills 目录清理、损坏 JSON 兜底、save 语义。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import ConfigStore  # noqa: E402


class ConfigMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self, payload=None):
        if payload is not None:
            self.config_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        return ConfigStore(self.config_path)

    def test_fresh_install_defaults_and_save(self):
        store = self._store()
        # 全新安装：代理默认强制直连（与"跟随系统代理"的旧配置升级语义区分）。
        self.assertEqual(store.data["proxy"]["enabled"], False)
        self.assertTrue(self.config_path.exists())
        reloaded = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(reloaded["port"], 8765)

    def test_legacy_default_tools_migrate_run_command_and_drop_mcp_entries(self):
        store = self._store({
            "agent_tools": [
                "read_file", "write_file", "list_directory", "search_files",
                "run_skill_script", "http_request", "run_command",
                "register_mcp", "call_mcp",
            ]
        })
        tools = store.data["agent_tools"]
        self.assertIn("pwsh", tools)
        self.assertNotIn("run_command", tools)
        self.assertNotIn("register_mcp", tools)
        self.assertNotIn("call_mcp", tools)

    def test_customized_tools_keep_register_mcp(self):
        store = self._store({
            "agent_tools": [
                "read_file", "write_file", "list_directory", "search_files",
                "run_skill_script", "http_request", "run_command",
                "register_mcp", "custom_check",
            ]
        })
        tools = store.data["agent_tools"]
        self.assertIn("pwsh", tools)
        self.assertIn("register_mcp", tools)
        self.assertIn("custom_check", tools)
        self.assertNotIn("run_command", tools)

    def test_mcp_servers_deduped_and_legacy_comfyui_retired(self):
        store = self._store({
            "mcp_servers": [
                # 旧版捆绑桥接（退役脚本签名）：必须剥离
                {"id": "comfyui", "command": "py", "args": ["skills/comfyui-mcp/comfyui_mcp_server.py"]},
                {"id": "comfy-mcp", "command": "y"},
                {"id": "comfy-mcp", "command": "y"},
                {"id": "other", "command": "z"},
            ]
        })
        ids = [item["id"] for item in store.data["mcp_servers"]]
        self.assertEqual(ids, ["comfy-mcp", "other"])

    def test_user_registered_comfyui_server_survives(self):
        # 用户自行注册的官方 comfy-mcp 恰好取名 comfyui：不是旧桥接，不许误删
        #（真实事故：环境目录含 "comfyui-mcp" 子串，也不能按目录名误判）。
        store = self._store({
            "mcp_servers": [
                {
                    "id": "comfyui",
                    "command": "H:\\ComfyUI\\comfyui-mcp-env\\Scripts\\comfy-mcp.exe",
                    "args": [],
                    "env": {"COMFY_BIN": "H:\\ComfyUI\\comfy.exe"},
                },
            ]
        })
        ids = [item["id"] for item in store.data["mcp_servers"]]
        self.assertEqual(ids, ["comfyui"])

    def test_vision_default_timeout_migrated(self):
        store = self._store({"vision": {"timeout_ms": 120000}})
        self.assertEqual(store.data["vision"]["timeout_ms"], 180000)

    def test_custom_vision_timeout_preserved(self):
        store = self._store({"vision": {"timeout_ms": 130000}})
        self.assertEqual(store.data["vision"]["timeout_ms"], 130000)

    def test_retired_vision_auto_route_cleaned(self):
        # 自动路由已移除（视觉统一由模型按需调用 vision_analyze）：旧配置残留键必须被清洗。
        store = self._store({"vision": {"auto_route": False, "timeout_ms": 150000}})
        self.assertNotIn("auto_route", store.data["vision"])
        self.assertEqual(store.data["vision"]["timeout_ms"], 150000)

    def test_bundled_comfyui_mcp_dir_removed_from_skill_roots(self):
        store = self._store({"skills_dirs": ["skills", "data/comfyui-mcp"]})
        self.assertEqual(store.data["skills_dirs"], ["skills"])

    def test_corrupt_json_falls_back_to_defaults(self):
        self.config_path.write_text("{ 这不是合法的 JSON", encoding="utf-8")
        store = self._store()
        self.assertEqual(store.data["port"], 8765)


class ToolSetStorageTests(unittest.TestCase):
    """「我的工具集」必须落在 config.json（localStorage 在冻结版每次退出都会被清空）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self, payload=None):
        if payload is not None:
            self.config_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        return ConfigStore(self.config_path)

    def test_fresh_install_has_empty_tool_sets(self):
        store = self._store()
        self.assertEqual(store.get_tool_sets(), [])

    def test_add_update_delete_roundtrip(self):
        store = self._store()
        item = store.upsert_tool_set("标准加强版", ["read_file", "write_file", "read_file"])
        self.assertEqual(item["name"], "标准加强版")
        self.assertEqual(item["tools"], ["read_file", "write_file"], "去重且保持顺序")
        self.assertTrue(item["id"])
        # 原地更新（同名不新增）
        updated = store.upsert_tool_set("标准加强版 v2", ["pwsh"], item["id"])
        self.assertEqual(updated["id"], item["id"])
        self.assertEqual(len(store.get_tool_sets()), 1)
        self.assertEqual(store.get_tool_sets()[0]["name"], "标准加强版 v2")
        # 落到磁盘：重新加载仍在
        reloaded = ConfigStore(self.config_path).get_tool_sets()
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0]["tools"], ["pwsh"])
        # 删除
        self.assertTrue(store.delete_tool_set(item["id"]))
        self.assertEqual(store.get_tool_sets(), [])
        self.assertFalse(store.delete_tool_set(item["id"]), "重复删除返回 False")

    def test_empty_tools_rejected(self):
        store = self._store()
        for bad in ([], None, "read_file", [{"x": 1}]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    store.upsert_tool_set("空集合", bad)

    def test_new_set_is_prepended_and_capped(self):
        store = self._store()
        for index in range(35):
            store.upsert_tool_set(f"集合{index}", ["read_file"])
        items = store.get_tool_sets()
        self.assertEqual(len(items), 30, "上限 30 条")
        self.assertEqual(items[0]["name"], "集合34", "最新的排最前")
        self.assertNotIn("集合0", [item["name"] for item in items])

    def test_legacy_config_is_normalized(self):
        store = self._store({
            "tool_sets": [
                {"name": "旧格式", "tools": ["read_file", "", "read_file"]},
                {"tools": []},          # 空集合丢弃
                "not-a-dict",           # 非字典丢弃
                {"id": "fixed", "name": "有 ID", "tools": ["pwsh"]},
            ],
        })
        items = store.get_tool_sets()
        self.assertEqual([item["name"] for item in items], ["旧格式", "有 ID"])
        self.assertEqual(items[0]["tools"], ["read_file"])
        self.assertEqual(items[1]["id"], "fixed")
        self.assertTrue(items[0]["id"], "缺 id 的条目补一个")

    def test_settings_payload_excludes_tool_sets(self):
        store = self._store()
        store.upsert_tool_set("不暴露", ["read_file"])
        self.assertNotIn("tool_sets", store.public(), "走 bootstrap / 专用接口，不进 settings")


if __name__ == "__main__":
    unittest.main()
