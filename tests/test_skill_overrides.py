# -*- coding: utf-8 -*-
"""护栏：Skill 用户覆盖层（别名/备注，Skills 管理页改版 §1）。

保护对象：
- SkillOverrides 存取：设置/部分更新/清除/整条删除/残留清理/持久化往返；
- scan 合并：alias/note 出现在条目里、未设置时为空串；合并是读侧纯函数
  （缓存底稿不被污染，别名改动不重扫目录也能生效）；
- 别名/备注**不进注入 prompt**：注入 system 的 ``<skill name=…>`` 块在设置
  别名前后必须逐字节一致（前缀缓存红线，改版计划 §4）；
- PATCH /api/skills/{id}/override：正常保存、超限 400、未知 id 404。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from http import HTTPStatus  # noqa: E402

from naiba.app import NaibaChatApp  # noqa: E402
from naiba.paths import PathContext  # noqa: E402
from naiba.skills.catalog import SkillCatalog  # noqa: E402
from naiba.skills.overrides import (  # noqa: E402
    ALIAS_MAX_CHARS, NOTE_MAX_CHARS, SkillOverrides, validate_alias, validate_note,
)

SKILL_MD = "---\nname: demo\ndescription: 演示技能\n---\n\n# 演示\n"


def _make_skill(root: Path, name: str = "demo") -> Path:
    skill_dir = root / "skills" / f"{name}-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    return skill_dir


class _TmpCase(unittest.TestCase):
    def setUp(self):
        # 扫描会跳过路径中以 "." 开头的目录段，临时目录必须建在非点开头的位置。
        self._tmp = tempfile.TemporaryDirectory(dir=str(ROOT))
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()


class OverrideStoreTests(_TmpCase):
    def test_set_and_get_roundtrip(self):
        store = SkillOverrides(self.root / "skill-overrides.json")
        entry = store.set_override("abc", alias="出图助手", note="先确认比例")
        self.assertEqual(entry, {"alias": "出图助手", "note": "先确认比例"})
        self.assertEqual(store.get_all()["abc"], entry)

    def test_persists_across_instances(self):
        store = SkillOverrides(self.root / "skill-overrides.json")
        store.set_override("abc", alias="别名", note="备注")
        reloaded = SkillOverrides(self.root / "skill-overrides.json")
        self.assertEqual(reloaded.get_all().get("abc"), {"alias": "别名", "note": "备注"})

    def test_partial_update_keeps_other_field(self):
        store = SkillOverrides(self.root / "skill-overrides.json")
        store.set_override("abc", alias="别名")
        entry = store.set_override("abc", note="备注")
        self.assertEqual(entry, {"alias": "别名", "note": "备注"})
        # 空串 = 清除该字段；全空 = 整条删除
        entry = store.set_override("abc", note="")
        self.assertEqual(entry, {"alias": "别名"})
        entry = store.set_override("abc", alias="")
        self.assertEqual(entry, {})
        self.assertNotIn("abc", store.get_all())

    def test_alias_validation(self):
        with self.assertRaises(ValueError):
            validate_alias("a" * (ALIAS_MAX_CHARS + 1))
        with self.assertRaises(ValueError):
            validate_alias("含/斜杠")
        self.assertEqual(validate_alias("  多  段 空白 "), "多 段 空白")
        self.assertEqual(validate_alias(None), "")

    def test_note_validation(self):
        with self.assertRaises(ValueError):
            validate_note("x" * (NOTE_MAX_CHARS + 1))
        self.assertEqual(validate_note("  备注  "), "备注")


class _CatalogCase(_TmpCase):
    def _catalog(self, store):
        return SkillCatalog(
            [self.root / "skills"],
            override_merger=store.apply_to_entries,
        )


class ScanMergeTests(_CatalogCase):
    def test_merged_entries_carry_alias_and_note(self):
        _make_skill(self.root)
        store = SkillOverrides(self.root / "skill-overrides.json")
        items = self._catalog(store).scan()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["alias"], "")
        self.assertEqual(items[0]["note"], "")
        skill_id = items[0]["id"]
        store.set_override(skill_id, alias="演示别名", note="我的备注")
        items = self._catalog(store).scan()
        self.assertEqual(items[0]["alias"], "演示别名")
        self.assertEqual(items[0]["note"], "我的备注")
        # id / name / ref / description 不受覆盖层影响
        self.assertEqual(items[0]["name"], "demo")
        self.assertEqual(items[0]["ref"], "demo")
        self.assertEqual(items[0]["description"], "演示技能")

    def test_cache_hit_path_also_merges(self):
        """别名改动即时生效：缓存命中路径同样要过合并器（读侧纯函数）。"""
        _make_skill(self.root)
        store = SkillOverrides(self.root / "skill-overrides.json")
        catalog = self._catalog(store)
        first = catalog.scan()
        self.assertEqual(first[0]["alias"], "")
        store.set_override(first[0]["id"], alias="即时生效")
        second = catalog.scan()
        self.assertEqual(second[0]["alias"], "即时生效")
        # 底稿（缓存）没有被污染：换一个空合并器重新读，仍是原始条目
        bare = SkillCatalog([self.root / "skills"]).scan()
        self.assertEqual(bare[0].get("alias", ""), "")

    def test_prune_missing_removes_stale_ids(self):
        store = SkillOverrides(self.root / "skill-overrides.json")
        store.set_override("gone", alias="旧别名")
        stale = store.prune_missing({"kept"})
        self.assertEqual(stale, ["gone"])
        self.assertNotIn("gone", store.get_all())


class InjectionByteStabilityTests(_CatalogCase):
    """别名不泄漏进 prompt：设置别名前后注入的 <skill> 块逐字节一致。"""

    def test_skill_block_ignores_alias(self):
        _make_skill(self.root)
        store = SkillOverrides(self.root / "skill-overrides.json")
        catalog = self._catalog(store)
        skill = catalog.scan()[0]

        def render(item):
            content = catalog.read_skill_content(item["path"])
            return f"<skill name=\"{item['name']}\" root=\"{item['root']}\">\n{content}\n</skill>"

        before = render(skill)
        store.set_override(skill["id"], alias="完全不同的名字", note="备注内容")
        after = render(catalog.by_id()[skill["id"]])
        self.assertEqual(before, after)


class OverrideEndpointTests(_TmpCase):
    """PATCH /api/skills/{id}/override 判定层（app 方法直调，与 test_skill_size_limit 同法）。"""

    def setUp(self):
        super().setUp()
        # Skill 目录必须先于 NaibaChatApp 装配存在（app 启动时把存在的目录编入 catalog）。
        _make_skill(self.root, "patched")
        (self.root / "data" / "skills").mkdir(parents=True, exist_ok=True)
        self.app = NaibaChatApp(paths=PathContext.local(self.root, self.root / "config.json"))
        self.skill_id = self.app.catalog.scan()[0]["id"]

    def test_set_override_ok(self):
        payload, status = self.app.api_skill_override(
            self.skill_id, {"alias": "出图助手", "note": "先确认比例"}
        )
        self.assertEqual(int(status), 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["override"], {"alias": "出图助手", "note": "先确认比例"})
        merged = self.app.catalog.scan()[0]
        self.assertEqual(merged["alias"], "出图助手")

    def test_overlong_alias_is_400(self):
        payload, status = self.app.api_skill_override(self.skill_id, {"alias": "a" * 41})
        self.assertEqual(int(status), 400, payload)
        self.assertIn("40", str(payload.get("error")))

    def test_overlong_note_is_400(self):
        payload, status = self.app.api_skill_override(self.skill_id, {"note": "x" * 501})
        self.assertEqual(int(status), 400, payload)

    def test_alias_with_slash_is_400(self):
        payload, status = self.app.api_skill_override(self.skill_id, {"alias": "a/b"})
        self.assertEqual(int(status), 400, payload)

    def test_unknown_skill_id_is_404(self):
        payload, status = self.app.api_skill_override("deadbeef", {"alias": "x"})
        self.assertEqual(int(status), 404, payload)

    def test_clear_both_fields_deletes_entry(self):
        self.app.api_skill_override(self.skill_id, {"alias": "x"})
        payload, status = self.app.api_skill_override(self.skill_id, {"alias": "", "note": ""})
        self.assertEqual(int(status), 200, payload)
        self.assertEqual(payload["override"], {})
        self.assertNotIn(self.skill_id, self.app.skill_overrides.get_all())


if __name__ == "__main__":
    unittest.main()
