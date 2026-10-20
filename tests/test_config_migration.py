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


class BuiltInAgentNewToolsMigrationTests(unittest.TestCase):
    """内置 Agent「新增工具」合并迁移：原始出厂 → 补齐；碰过 → 原样保留。

    背景（计划 2026-10-20）：这一版给内置 Agent 加了两批工具——后台任务五件套与
    `show_media`（指名出卡）。`public_agents()` 对已保存的覆盖项是原样优先，所以
    用户点开编辑过一次内置 Agent 后，新出厂的工具就再也进不去。

    判据边界（本组要钉的东西）：
    - 只对「`tool_scope` 里没有基线之外的工具，且基线一个不缺」的条目补齐；
    - 用户加过/删过任何工具都算定制，一个字段都不许动；
    - `tool_scope` 为空/缺省 = 「不限制」，保持原样（新工具对它自然可用）；
    - 只改 `tool_scope`，名称/提示词/Skill/头像/顺序逐字段不变；
    - 重复加载幂等；A/B 两批任意先后都能收敛到同一集合。
    """

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

    def _scope(self, store, agent_id):
        for agent in store.data["agents"]:
            if str(agent.get("id") or "") == agent_id:
                return agent.get("tool_scope")
        raise AssertionError(f"配置里找不到 Agent：{agent_id}")

    def test_baseline_matches_historical_factory_scopes(self) -> None:
        """冻结基线必须正好等于「本次改动前」的出厂 scope（多一个少一个都会误判定制）。"""
        from naiba.config import _BUILTIN_AGENT_BASELINE_SCOPE

        expected_counts = {
            "tutor": 6, "master": 17, "director": 18, "prompter": 7, "coder": 13, "skills": 16,
        }
        self.assertEqual(
            {key: len(value) for key, value in _BUILTIN_AGENT_BASELINE_SCOPE.items()},
            expected_counts,
        )
        for agent_id, baseline in _BUILTIN_AGENT_BASELINE_SCOPE.items():
            with self.subTest(agent=agent_id):
                self.assertEqual(len(baseline), len(set(baseline)), "基线里不许有重复工具名")

    def test_untouched_factory_scope_gets_both_batches(self) -> None:
        from naiba.config import _BUILTIN_AGENT_BASELINE_SCOPE, _BUILTIN_AGENT_NEW_TOOLS, built_in_agents

        factory = {agent["id"]: set(agent["tool_scope"]) for agent in built_in_agents()}
        payload = {"agents": [
            {"id": agent_id, "name": "我的名字", "system_prompt": "我的提示词",
             "skill_ids": ["x"], "avatar": "🐱", "tool_scope": list(baseline)}
            for agent_id, baseline in _BUILTIN_AGENT_BASELINE_SCOPE.items()
        ]}
        store = self._store(payload)
        for agent_id, baseline in _BUILTIN_AGENT_BASELINE_SCOPE.items():
            with self.subTest(agent=agent_id):
                self.assertEqual(set(self._scope(store, agent_id)), factory[agent_id],
                                 "原始出厂 scope 必须补齐到当前出厂集合")
                self.assertTrue(set(_BUILTIN_AGENT_NEW_TOOLS[agent_id]) <= set(self._scope(store, agent_id)))

    def test_only_tool_scope_changes(self) -> None:
        from naiba.config import _BUILTIN_AGENT_BASELINE_SCOPE

        original = {
            "id": "coder", "name": "我的编程助手", "system_prompt": "自定义提示词",
            "skill_ids": ["97e6e849bd844ed2"], "avatar": "🙂",
            "tool_scope": list(_BUILTIN_AGENT_BASELINE_SCOPE["coder"]),
        }
        store = self._store({"agents": [dict(original)]})
        migrated = next(a for a in store.data["agents"] if a["id"] == "coder")
        for key in ("name", "system_prompt", "skill_ids", "avatar", "id"):
            with self.subTest(field=key):
                self.assertEqual(migrated[key], original[key], "迁移只许改 tool_scope")
        self.assertEqual(migrated["tool_scope"][: len(original["tool_scope"])],
                         original["tool_scope"], "工具原有顺序不得重排（只追加）")

    def test_show_media_only_first_then_background(self) -> None:
        """B 先跑的中间态（原始 + show_media）只补后台工具，不重复追加 show_media。"""
        from naiba.config import _BUILTIN_AGENT_BASELINE_SCOPE

        baseline = list(_BUILTIN_AGENT_BASELINE_SCOPE["master"])
        store = self._store({"agents": [
            {"id": "master", "name": "x", "system_prompt": "p", "skill_ids": [],
             "tool_scope": [*baseline, "show_media"]},
        ]})
        scope = self._scope(store, "master")
        self.assertEqual(scope.count("show_media"), 1, "幂等：不得重复追加")
        for tool in ("run_in_background", "job_output", "job_status", "job_wait", "job_kill"):
            self.assertIn(tool, scope)

    def test_background_only_first_then_show_media(self) -> None:
        """A 先跑的中间态（原始 + 后台五件）只补 show_media。"""
        from naiba.config import _BUILTIN_AGENT_BASELINE_SCOPE

        baseline = list(_BUILTIN_AGENT_BASELINE_SCOPE["coder"])
        background = ["run_in_background", "job_output", "job_status", "job_wait", "job_kill"]
        store = self._store({"agents": [
            {"id": "coder", "name": "x", "system_prompt": "p", "skill_ids": [],
             "tool_scope": [*baseline, *background]},
        ]})
        scope = self._scope(store, "coder")
        self.assertEqual(scope.count("show_media"), 1)

    def test_customized_scope_is_kept_verbatim(self) -> None:
        from naiba.config import _BUILTIN_AGENT_BASELINE_SCOPE

        baseline = list(_BUILTIN_AGENT_BASELINE_SCOPE["coder"])
        cases = {
            "added_other": [*baseline, "web_search"],
            "removed_factory": [tool for tool in baseline if tool != "pwsh"],
            "empty": [],
        }
        for label, scope in cases.items():
            with self.subTest(case=label):
                self.config_path.unlink(missing_ok=True)
                store = self._store({"agents": [
                    {"id": "coder", "name": "x", "system_prompt": "p", "skill_ids": [],
                     "tool_scope": list(scope)},
                ]})
                self.assertEqual(self._scope(store, "coder"), list(scope),
                                 "用户定制/未限制的 scope 必须逐项原样保留")

    def test_missing_tool_scope_is_kept(self) -> None:
        store = self._store({"agents": [
            {"id": "tutor", "name": "x", "system_prompt": "p", "skill_ids": []},
        ]})
        agent = next(a for a in store.data["agents"] if a["id"] == "tutor")
        self.assertNotIn("tool_scope", agent, "缺省 = 不限制，不得凭空写入固化集")

    def test_custom_and_retired_agents_are_untouched(self) -> None:
        from naiba.config import _BUILTIN_AGENT_BASELINE_SCOPE

        mine_scope = list(_BUILTIN_AGENT_BASELINE_SCOPE["coder"])
        store = self._store({"agents": [
            {"id": "agent_0123456789ab", "name": "我的", "system_prompt": "",
             "skill_ids": [], "tool_scope": mine_scope},
        ]})
        self.assertEqual(self._scope(store, "agent_0123456789ab"), mine_scope,
                         "自定义 Agent 一个字都不许动")

    def test_migration_is_idempotent_across_reloads(self) -> None:
        from naiba.config import _BUILTIN_AGENT_BASELINE_SCOPE

        self._store({"agents": [
            {"id": "skills", "name": "x", "system_prompt": "p", "skill_ids": [],
             "tool_scope": list(_BUILTIN_AGENT_BASELINE_SCOPE["skills"])},
        ]})
        first = list(self._scope(self._store(), "skills"))
        second = list(self._scope(self._store(), "skills"))
        self.assertEqual(first, second, "重复加载必须稳定（不重复追加、不重排）")
        self.assertEqual(first.count("show_media"), 1)


if __name__ == "__main__":
    unittest.main()
