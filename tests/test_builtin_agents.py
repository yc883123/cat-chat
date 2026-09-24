# -*- coding: utf-8 -*-
"""出厂内置 Agent（6 样）与内置 Skill 的守门。

背景（2.8.9-beta）：原先把 `built_in_agents()` 置空、让所有 Agent 都可删；现在重启内置机制，
出厂 6 样——**教程助手排第一**（对外教程承诺"认准最上面的教程助手"）、`master` 是新装默认。

这份测试钉死四条对外承诺（改 `naiba/config.py` 的出厂清单前必读）：
1. **id 与顺序**：`tutor / master / director / prompter / coder / skills`，tutor 恒第一；
2. **工具档**：每个内置 scope 与对应 `TOOL_PRESETS` 展开逐项一致，且**绝不含**
   subagent / subagent_spawn / register_mcp / mcp__ 前缀 / http_request
   （空 scope 的语义是"不限制"，会把上面这些都放行，所以逐项比对是必须的）；
3. **出厂 Skill 绑定只许引用随包 Skill**：用真实 `skills/` 目录重算 id 比对，改名/挪目录即红；
4. **默认隐藏不得砸掉绑定**：`hidden_skill_ids` 是"完全不进 catalog"（SkillCatalog.scan 直接
   跳过），被隐藏的 Skill 连 `/` 引用与 Agent 固定绑定都一起失效——所以默认隐藏集里
   出现任何被出厂绑定的 Skill 就是 bug，不是配置口味问题。

变异核对（手工执行，见维护说明 §九.138）：
- 往任一内置 scope 里塞 `subagent` → `test_scope_never_grants_spawn_and_external_tools` 变红；
- 把 tutor 挪到第二位 → `test_ids_and_order_are_pinned` 变红；
- 把 tutor 的绑定换成不存在的 id 或非随包 id → `test_bindings_are_bundled_skills` 变红；
- 把 cat-chat-guide 的 id 加进 DEFAULTS['hidden_skill_ids'] → `test_default_hidden_skills_stay_usable` 变红；
- `_is_untouched_factory_agent()` 里去掉 `tool_scope` 那一项判断 → `test_narrowed_tool_scope_is_kept` 变红；
- 迁移里去掉改指 default 的那两行 → `test_default_agent_id_is_repointed_when_it_was_removed` 变红。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.config import (  # noqa: E402
    _RETIRED_FACTORY_AGENT_IDS,
    TOOL_PRESETS,
    ConfigStore,
    built_in_agent_ids,
    built_in_agents,
    default_config,
    resolve_tool_preset,
    tool_catalog_entries,
)
from naiba.skills.catalog import SkillCatalog  # noqa: E402

BUNDLED_SKILLS_DIR = ROOT / "skills"

EXPECTED_ORDER = ("tutor", "master", "director", "prompter", "coder", "skills")
# 老三样（已退役的出厂 Agent）：id 不能与内置 id 撞车（撞了会把老用户改过的 Agent 顶掉）。
LEGACY_AGENT_IDS = {"general", "coding", "drama"}

# 2.8.9-beta 之前 `default_config()["agents"]` 的出厂原样。**故意在测试里重写一遍**，
# 不从生产常量读——否则生产常量改错时测试会跟着一起错（同义反复）。
# 迁移只清「与这份定义逐字段完全一致」的条目，改过的必须留下（见 RetiredFactoryAgentCleanupTests）。
LEGACY_FACTORY_AGENTS = (
    {"id": "general", "name": "通用 Agent", "system_prompt": "", "skill_ids": []},
    {
        "id": "coding",
        "name": "编程 Agent",
        "system_prompt": "你是资深编程助手。先理解需求，再给出可直接运行、结构清晰的代码；涉及文件操作时先说明改动范围。",
        "skill_ids": [],
    },
    {
        "id": "drama",
        "name": "短剧 Agent",
        "system_prompt": "你是短剧创作助手。遵循所选短剧类 Skill 的交互收集流程，逐步确认主题、角色、分镜与风格后再产出内容。",
        "skill_ids": [],
    },
)

# 预设兜底的内置 Agent → TOOL_PRESETS 的 id。
PRESET_BACKED = {
    "tutor": "readonly",
    "master": "longsession",
    "director": "comfyui",
    "coder": "standard",
}
SKILL_MANAGER_TOOLS = {"install_skill", "unpack_skill_archive", "inspect_installed_skill"}
PROMPTER_SCOPE = {
    "read_file", "list_directory", "search_files", "read_pdf",
    "vision_analyze", "write_file", "edit_file",
}
# 出厂绑定：Agent id → 随包 Skill 的 ref（前端 `/` 索引里的可手输别名）。
EXPECTED_BINDINGS = {
    "tutor": {"cat-chat-guide"},
    "master": set(),
    "director": {"comfyui-shortdramav2", "shortdramav2-rh", "runninghub"},
    "prompter": {"h3-prompt-writing"},
    "coder": set(),
    "skills": set(),
}
# 一律不许出现在出厂工具档里的工具（含 MCP 动态工具前缀）。
FORBIDDEN_TOOLS = {
    "subagent", "subagent_spawn", "register_mcp", "http_request",
}


def _entries():
    from naiba.tools.registry import build_tool_registry

    return tool_catalog_entries(build_tool_registry().schemas())


def _preset_tools(preset_id: str) -> set[str]:
    preset = next(p for p in TOOL_PRESETS if p["id"] == preset_id)
    return set(resolve_tool_preset(preset, _entries()))


def _bundled_skill_rows(hidden_ids=()) -> list[dict]:
    catalog = SkillCatalog(
        [BUNDLED_SKILLS_DIR],
        base_dir=BUNDLED_SKILLS_DIR,
        hidden_ids=list(hidden_ids),
        package_dir=BUNDLED_SKILLS_DIR,
    )
    return catalog.scan()


class BuiltInAgentManifestTests(unittest.TestCase):
    """出厂清单本身：id / 顺序 / 字段完整性。"""

    def test_ids_and_order_are_pinned(self) -> None:
        ids = [agent["id"] for agent in built_in_agents()]
        self.assertEqual(ids, list(EXPECTED_ORDER),
                         "内置 id 集合与顺序是对外教程承诺（教程助手排第一），改动必须同步教程与守门")
        self.assertEqual(built_in_agent_ids(), set(EXPECTED_ORDER))

    def test_ids_do_not_collide_with_legacy_agents(self) -> None:
        self.assertEqual(built_in_agent_ids() & LEGACY_AGENT_IDS, set(),
                         "出厂 id 撞上老三样会把老用户改过的 Agent 顶掉（升级覆盖用户配置）")

    def test_names_prompts_and_avatars_are_complete(self) -> None:
        for agent in built_in_agents():
            with self.subTest(agent=agent["id"]):
                self.assertTrue(str(agent.get("name") or "").strip(), "内置 Agent 必须有名字")
                self.assertTrue(str(agent.get("system_prompt") or "").strip(),
                                "内置 Agent 必须有系统提示词（出厂不设空人设）")
                self.assertTrue(str(agent.get("avatar") or "").strip(),
                                "内置 Agent 必须带 emoji 头像（顶栏一眼区分）")
                # `built_in` 标记必须由**出厂定义自带**。前端 `agentCardMarkup` 据它显示「内置」
                # 徽标并隐藏 × 删除按钮；若漏写，卡片会出现一个「点了没反应」的删除按钮
                # （后端 delete_agent 的内置守卫是静默拒绝的），用户只会觉得软件坏了。
                self.assertIs(agent.get("built_in"), True,
                              "出厂定义必须自带 built_in=True 标记（徽标 + 不可删守卫都靠它）")

    def test_avatar_is_emoji_not_uploaded_filename(self) -> None:
        """avatar 字段是二义的：文件名走 <img>，字形走文本。

        内置 Agent 若写成 `<id>_<hash>.webp` 形态，前端会去请求一个不存在的头像文件
        （破图），所以这里必须钉死"内置头像不是文件名"。
        """
        for agent in built_in_agents():
            with self.subTest(agent=agent["id"]):
                avatar = str(agent.get("avatar") or "")
                self.assertFalse(avatar.endswith(".webp"),
                                 "内置 Agent 的头像必须是 emoji 字形，不能是上传头像的文件名")
                self.assertLessEqual(len(avatar), 8, "内置头像必须是一个短字形")

    def test_manifest_is_returned_as_a_fresh_copy(self) -> None:
        first = built_in_agents()
        first[0]["name"] = "被外部改坏了"
        first[0]["tool_scope"].append("subagent")
        again = built_in_agents()
        self.assertEqual(again[0]["name"], "教程助手 Agent", "built_in_agents() 必须每次返回新副本")


class BuiltInAgentScopeTests(unittest.TestCase):
    """工具档：与预设一致 + 不含越权工具。"""

    def test_scope_matches_tool_preset(self) -> None:
        agents = {agent["id"]: agent for agent in built_in_agents()}
        for agent_id, preset_id in PRESET_BACKED.items():
            with self.subTest(agent=agent_id, preset=preset_id):
                self.assertEqual(
                    set(agents[agent_id]["tool_scope"]), _preset_tools(preset_id),
                    f"{agent_id} 的工具档必须与「{preset_id}」预设展开逐项一致",
                )
        self.assertEqual(
            set(agents["skills"]["tool_scope"]),
            _preset_tools("standard") | SKILL_MANAGER_TOOLS,
            "Skill 助手 = 标准模式 + 装/拆/查三件套",
        )
        self.assertEqual(set(agents["prompter"]["tool_scope"]), PROMPTER_SCOPE,
                         "提示词优化 = 只读 + 写稿（7 件）")

    def test_scope_counts_match_plan(self) -> None:
        """个数是对外说明（教程里会写"N 件"），与计划书 §二 逐项对齐。"""
        expected = {"tutor": 6, "master": 17, "director": 18, "prompter": 7, "coder": 13, "skills": 16}
        for agent in built_in_agents():
            with self.subTest(agent=agent["id"]):
                self.assertEqual(len(agent["tool_scope"]), expected[agent["id"]])

    def test_scope_never_grants_spawn_and_external_tools(self) -> None:
        """subagent/subagent_spawn 会烧 token（鱼群），MCP 与联网一律不进出厂档。

        MCP 动态工具用前缀判定：`mcp__<server>__<tool>` 是用户后来接的，出厂 Agent
        不得因用户接了 MCP 就自动把它们纳入。
        """
        for agent in built_in_agents():
            scope = [str(item) for item in agent["tool_scope"]]
            with self.subTest(agent=agent["id"]):
                self.assertEqual(set(scope) & FORBIDDEN_TOOLS, set())
                self.assertEqual([n for n in scope if n.startswith("mcp__")], [])
                self.assertNotIn("group:*", scope, "出厂档必须显式列工具名，不许用分组通配")
                self.assertTrue(scope, "空 scope 的语义是「不限制」，出厂 Agent 绝不能用")
                self.assertEqual(len(scope), len(set(scope)), "工具名不许重复")

    def test_scope_tool_names_all_exist(self) -> None:
        known = {entry["name"] for entry in _entries()}
        for agent in built_in_agents():
            with self.subTest(agent=agent["id"]):
                unknown = sorted(set(agent["tool_scope"]) - known)
                self.assertEqual(unknown, [], "内置工具档引用了不存在的工具名（改名漏改即静默少选）")

    def test_scope_keeps_job_creator_closure(self) -> None:
        from naiba.run.session import JOB_CREATOR_TOOL_DEPS

        for agent in built_in_agents():
            scope = set(agent["tool_scope"])
            with self.subTest(agent=agent["id"]):
                for creator, deps in JOB_CREATOR_TOOL_DEPS.items():
                    if creator in scope:
                        self.assertTrue(set(deps) <= scope,
                                        f"{agent['id']} 缺少 {creator} 的依赖闭包 {sorted(deps)}")


class BuiltInAgentSkillBindingTests(unittest.TestCase):
    """出厂 Skill 绑定：只许引用随包 Skill，且默认隐藏不得砸掉它。"""

    def test_bindings_resolve_to_expected_bundled_skills(self) -> None:
        ref_by_id = {row["id"]: str(row["ref"]) for row in _bundled_skill_rows()}
        for agent in built_in_agents():
            with self.subTest(agent=agent["id"]):
                refs = {ref_by_id.get(sid, f"<未知 {sid}>") for sid in agent["skill_ids"]}
                self.assertEqual(refs, EXPECTED_BINDINGS[agent["id"]])

    def test_bindings_are_bundled_skills(self) -> None:
        bundled = {row["id"] for row in _bundled_skill_rows()}
        for agent in built_in_agents():
            for skill_id in agent["skill_ids"]:
                with self.subTest(agent=agent["id"], skill=skill_id):
                    self.assertIn(skill_id, bundled,
                                  "出厂绑定只许引用随包 Skill（改名/挪目录会换 id，这里就是漂移报警）")

    def test_default_hidden_skills_stay_usable(self) -> None:
        """默认隐藏集不能把出厂绑定砸掉。

        `hidden_skill_ids` 是"完全不进 catalog"：SkillCatalog.scan 直接 `continue`，
        于是 `/` 引用找不到、`normalize_skill_policy` 的 fixed 过滤也会静默丢弃该 id。
        换句话说「默认隐藏」和「能绑定」不可兼得——所以被绑定的 Skill 不许进默认隐藏集。
        """
        hidden = default_config()["hidden_skill_ids"]
        visible_ids = {row["id"] for row in _bundled_skill_rows()}
        after_hiding = {row["id"] for row in _bundled_skill_rows(hidden_ids=hidden)}
        bound = {sid for agent in built_in_agents() for sid in agent["skill_ids"]}
        self.assertEqual(bound - after_hiding, set(),
                         "默认隐藏集砸掉了出厂绑定（被隐藏的 Skill 连 / 引用都失效，见维护说明 §九.138）")
        self.assertTrue(bound <= visible_ids, "出厂绑定引用的 Skill 必须真的在 skills/ 目录里")
        # 反向也钉一下：默认隐藏集必须都是真实存在的随包 Skill（写错 id 会静默无效果）。
        self.assertEqual(set(hidden) - visible_ids, set(),
                         "DEFAULTS['hidden_skill_ids'] 里出现了不存在的 id")

    def test_cat_chat_guide_is_bound_to_tutor(self) -> None:
        """教程助手的价值全部押在这份指南上：绑定断了它就是空人设。"""
        by_name = {row["ref"]: row["id"] for row in _bundled_skill_rows()}
        self.assertIn("cat-chat-guide", by_name, "教程助手知识库 Skill 必须随包存在")
        tutor = next(agent for agent in built_in_agents() if agent["id"] == "tutor")
        self.assertEqual(tutor["skill_ids"], [by_name["cat-chat-guide"]],
                         "tutor 必须且只绑 cat-chat-guide（多绑会让教程回答跑偏）")

    def test_guide_has_reference_documents(self) -> None:
        references = BUNDLED_SKILLS_DIR / "cat-chat-guide" / "references"
        docs = sorted(references.glob("*.md")) if references.is_dir() else []
        self.assertTrue(docs, "教程助手知识库不能只有索引：references/ 下必须有可查的原文")
        for doc in docs:
            with self.subTest(doc=doc.name):
                self.assertGreater(doc.stat().st_size, 500, "知识库文档不能是空壳")


class PublicAgentsOrderTests(unittest.TestCase):
    """`public_agents()` 三段式：内置区恒在最前，覆盖版占原位次，自定义相对顺序不变。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _store(self, payload=None) -> ConfigStore:
        if payload is not None:
            self.config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return ConfigStore(self.config_path)

    def test_fresh_install_order_is_built_in_first(self) -> None:
        """全新安装的 Agent 下拉**恒为 6 项内置**（老三样自 2.8.9-beta 起不再出厂）。"""
        store = self._store()
        self.assertEqual([a["id"] for a in store.public_agents()],
                         list(EXPECTED_ORDER),
                         "出厂配置不含自定义 Agent，下拉必须正好是内置那 6 项")

    def test_custom_agents_land_after_built_in_section(self) -> None:
        store = self._store({"agents": [
            {"id": "mine", "name": "我的", "system_prompt": "", "skill_ids": []},
        ]})
        self.assertEqual([a["id"] for a in store.public_agents()], [*EXPECTED_ORDER, "mine"])

    def test_edited_built_in_keeps_its_position(self) -> None:
        """用户点开编辑过内置 Agent 后，覆盖版必须**留在原位次**。

        否则"教程助手永远排第一"这条对外承诺会在用户点过一次编辑后失效。
        """
        store = self._store({"agents": [
            {"id": "master", "name": "我的全能", "system_prompt": "改过", "skill_ids": [],
             "built_in": True},
        ]})
        agents = store.public_agents()
        self.assertEqual([a["id"] for a in agents], list(EXPECTED_ORDER),
                         "覆盖版不能重复出现、也不能掉到内置区之外")
        self.assertEqual(agents[1]["name"], "我的全能", "覆盖版内容优先于出厂定义")
        self.assertTrue(agents[1]["built_in"], "覆盖版仍须带 built_in 标记（前端隐藏删除按钮）")
        self.assertEqual(agents[0]["name"], "教程助手 Agent", "未被覆盖的内置仍用出厂定义")

    def test_built_in_agents_are_not_deletable(self) -> None:
        store = self._store({})
        for agent_id in EXPECTED_ORDER:
            with self.subTest(agent=agent_id):
                self.assertFalse(store.delete_agent(agent_id), "内置 Agent 不可删除")
        self.assertEqual([a["id"] for a in store.public_agents()], list(EXPECTED_ORDER))

    def test_upsert_of_built_in_id_marks_built_in(self) -> None:
        store = self._store({})
        saved = store.upsert_agent({"id": "tutor", "name": "我的教程助手", "system_prompt": "x", "skill_ids": []})
        self.assertTrue(saved.get("built_in"), "编辑内置 Agent 生成的覆盖版必须带 built_in 标记")

    def test_new_agent_id_never_collides_with_built_in(self) -> None:
        store = self._store({})
        for _ in range(20):
            self.assertNotIn(store.upsert_agent({"name": "新 Agent", "system_prompt": "", "skill_ids": []})["id"],
                             set(EXPECTED_ORDER))


class DefaultAgentTests(unittest.TestCase):
    """新装默认 Agent = master（教程第 1 篇的主角）。"""

    def test_fresh_install_default_is_master(self) -> None:
        self.assertEqual(default_config()["default_agent_id"], "master")

    def test_default_resolves_to_a_real_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            self.assertEqual(store.default_agent_id(), "master")
            agent = store.get_agent(store.default_agent_id())
        self.assertIsNotNone(agent, "默认 Agent 必须解析得到定义")
        self.assertEqual(agent["name"], "全能 Agent")

    def test_legacy_default_value_is_preserved(self) -> None:
        """老用户配置里已存过默认值 ⇒ 不回溯（只在缺失/失效时才回落到首个可用 Agent）。

        老用户要**保留自己设过的默认 Agent**（这里用用户自建 id 代表：老三样已退役，
        未改动的会在启动时被清掉并改指 master，见 RetiredFactoryAgentCleanupTests）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({
                "agents": [{"id": "mine", "name": "我的", "system_prompt": "", "skill_ids": []}],
                "default_agent_id": "mine",
            }, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(ConfigStore(config_path).default_agent_id(), "mine")

    def test_invalid_default_falls_back_to_a_resolvable_agent(self) -> None:
        """默认值失效（指向已删 Agent）时必须回落，不能返回一个解析不到的空 id。"""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({"default_agent_id": "已被删掉的 id"}, ensure_ascii=False),
                                   encoding="utf-8")
            store = ConfigStore(config_path)
            resolved = store.default_agent_id()
            self.assertTrue(resolved, "默认 Agent 不能为空串")
            self.assertIsNotNone(store.get_agent(resolved), "回落值必须解析得到")
            self.assertNotEqual(resolved, "已被删掉的 id")


class AgentHelpPopoverTests(unittest.TestCase):
    """顶栏 Agent 说明弹层（`#agentHelpPopover`）必须与内置阵容同步。

    存在理由：弹层是**用户点一次「?」就能看到的权威介绍**，教程 1 与教程 8 都引导用户去看它。
    2.9.0 上线 6 个内置 Agent 后，弹层仍写着「三种预设 Agent：通用 / 编程 / 短剧」——
    用户照着教程点开，看到的是另一套阵容，教程与软件当场对不上。
    判据取 `built_in_agents()` 这一个唯一真相：增删改名而不改弹层即红（维护说明 §九.141）。
    """

    def _popover_block(self) -> str:
        index = (ROOT / "public/index.html").read_text(encoding="utf-8")
        start = index.index('id="agentHelpPopover"')
        return index[start:index.index("</div>", start)]

    def test_every_built_in_agent_is_named_in_the_popover(self) -> None:
        block = self._popover_block()
        for agent in built_in_agents():
            with self.subTest(agent=agent["id"]):
                self.assertIn(
                    agent["name"], block,
                    "弹层必须逐个介绍内置 Agent（加第 7 样、改名都要同步这里）",
                )

    def test_popover_count_word_matches_the_manifest(self) -> None:
        """「内置 N 个 Agent」的 N 必须等于实际条数，避免加了 Agent 忘了改数字。"""
        block = self._popover_block()
        self.assertIn("内置 %d 个 Agent" % len(built_in_agents()), block)

    def test_popover_stops_recommending_the_legacy_three(self) -> None:
        """负向：旧文案（把老三样当成「三种预设 Agent」推荐）不得复活。"""
        block = self._popover_block()
        self.assertNotIn("三种预设 Agent", block)
        for legacy in ("通用 Agent", "编程 Agent", "短剧 Agent"):
            with self.subTest(legacy=legacy):
                self.assertNotIn(
                    "<b>%s</b>" % legacy, block,
                    "老三样已不是产品推荐的预设，弹层不得再把它们当预设介绍",
                )


class RetiredFactoryAgentCleanupTests(unittest.TestCase):
    """老用户配置里「从没被碰过」的老三样，启动时必须被清掉；改过的一个都不许动。

    为什么要有这条迁移（2.9.2-beta）：出厂配置自 2.8.9-beta 起不再预置 general / coding /
    drama（改由 `built_in_agents()` 提供 6 个内置），但 `ConfigStore.__init__` 是
    `defaults.update(loaded)` 而 `agents` 为**整体键替换**——只改出厂配置**只影响新装**，
    老用户 config.json 里的老三样会一直留在 Agent 下拉里（新装 6 项 / 老用户 9 项），
    与弹层写的「内置 6 个 Agent」和教程正文当场对不上。

    判据边界（这是本条守门真正要钉的东西）：
    - **未改动的判定是逐字段一致**——只改过 tool_scope、只改过名字、挂过 Skill、换过头像，
      都算"用户碰过"，必须保留。宁可留冗余，也不能升级覆盖用户自定义。
    - 被清掉的条目若正是 `default_agent_id`，必须改指 master，不能留悬空默认值。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _store(self, payload: dict) -> ConfigStore:
        self.config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return ConfigStore(self.config_path)

    def _ids(self, store: ConfigStore) -> list[str]:
        return [a["id"] for a in store.public_agents()]

    def test_retired_id_set_is_pinned(self) -> None:
        """生产常量必须正好覆盖老三样——多写一个 id 就可能误删用户的别的 Agent。"""
        self.assertEqual(set(_RETIRED_FACTORY_AGENT_IDS), LEGACY_AGENT_IDS)

    def test_untouched_three_are_removed_on_upgrade(self) -> None:
        store = self._store({"agents": [dict(item) for item in LEGACY_FACTORY_AGENTS]})
        self.assertEqual(self._ids(store), list(EXPECTED_ORDER),
                         "逐字段未改动的老三样必须被清掉，下拉与新装一致（6 项）")

    def test_default_agent_id_is_repointed_when_it_was_removed(self) -> None:
        store = self._store({
            "agents": [dict(item) for item in LEGACY_FACTORY_AGENTS],
            "default_agent_id": "general",
        })
        resolved = store.default_agent_id()
        self.assertEqual(resolved, "master", "被清掉的默认 Agent 必须改指 master，不能留悬空值")
        self.assertIsNotNone(store.get_agent(resolved), "改指后的默认 Agent 必须解析得到")
        # 必须**落盘**改指，而不是只靠 default_agent_id() 的兜底遮掩过去：
        # 兜底只在读的时候生效，配置文件里留着 "general" 会让下次启动再走一遍同样的回落，
        # 也让「用户设的默认是谁」这件事变得取决于兜底逻辑而不是数据本身。
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["default_agent_id"], "master",
                         "改指要写回配置文件，不能只靠运行时兜底")

    def test_renamed_agent_is_kept(self) -> None:
        """用户改过名字 ⇒ 用户资产，必须原样保留（含它自己仍是默认 Agent 这件事）。"""
        general = dict(LEGACY_FACTORY_AGENTS[0], name="我的助手")
        store = self._store({"agents": [general], "default_agent_id": "general"})
        self.assertIn("general", self._ids(store), "改过名的 Agent 不得被升级清掉")
        self.assertEqual(store.default_agent_id(), "general")

    def test_narrowed_tool_scope_is_kept(self) -> None:
        """用户收窄过工具集 ⇒ 保留。空数组（设置页存一次就会写成它）不算收窄。"""
        narrowed = dict(LEGACY_FACTORY_AGENTS[0], tool_scope=["read_file"])
        store = self._store({"agents": [narrowed]})
        self.assertIn("general", self._ids(store), "收窄过工具集的 Agent 不得被清掉")

    def test_form_saved_empty_tool_scope_is_still_treated_as_untouched(self) -> None:
        general = dict(LEGACY_FACTORY_AGENTS[0], tool_scope=[])
        store = self._store({"agents": [general]})
        self.assertEqual(self._ids(store), list(EXPECTED_ORDER),
                         "tool_scope=[] 与出厂（不带该键）语义相同，仍应视为未改动")

    def test_edited_prompt_or_skill_binding_is_kept(self) -> None:
        cases = {
            "system_prompt": dict(LEGACY_FACTORY_AGENTS[1], system_prompt="改成我的规则"),
            "skill_ids": dict(LEGACY_FACTORY_AGENTS[2], skill_ids=["97e6e849bd844ed2"]),
            "avatar": dict(LEGACY_FACTORY_AGENTS[0], avatar="🐱"),
        }
        for field, agent in cases.items():
            with self.subTest(field=field):
                store = self._store({"agents": [agent]})
                self.assertIn(agent["id"], self._ids(store),
                              f"改过 {field} 的 Agent 不得被清掉")

    def test_custom_agents_are_never_touched_by_the_cleanup(self) -> None:
        mine = {"id": "agent_0123456789ab", "name": "通用 Agent", "system_prompt": "", "skill_ids": []}
        store = self._store({"agents": [mine], "default_agent_id": "agent_0123456789ab"})
        self.assertIn("agent_0123456789ab", self._ids(store),
                      "清理只认老三样 id，用户自建的同名 Agent 一个都不能动")


if __name__ == "__main__":
    unittest.main()
