# -*- coding: utf-8 -*-
"""护栏：分部大纲 + 拆章 + 章节计划（3.9.13 批次二）。

覆盖：
- 大纲从「章节数组」变成「分部文字」：schema 2 版本、parts 归一与上限、与 sync 互斥；
- 拆章追加：按批追加、已存在章号跳过、已有正文不动、part_no 落库；
- 章节简述 / 章节计划（单章字数、结尾策略）的校验与归一；
- 提示词：整篇大纲（不要求 JSON）与拆章（要求 JSON）的构造与解析；
- 流水线：新 kind 的参数校验与快照固化。

边界：只碰临时目录，不连供应商、不起服务。
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.writer import prompts  # noqa: E402
from naiba.writer.store import (  # noqa: E402
    WriterStore,
    WriterStoreError,
    normalize_chapter_plan,
    normalize_outline_parts,
    outline_parts_text,
)


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = WriterStore(Path(self.tmp.name))
        self.book = self.store.create_book(title="分部测")
        self.book_id = self.book["id"]


class OutlinePartsStoreTests(_StoreCase):
    def test_parts_roundtrip_and_schema(self):
        result = self.store.save_outline(
            self.book_id,
            parts=[{"no": 1, "title": "第一部 雾港", "text": "主线和第一部目标……"}],
            kind="manual",
        )
        self.assertEqual(result["parts_count"], 1)
        outline = self.store.load_outline(self.book_id)
        self.assertEqual(outline["schema"], 2)
        self.assertEqual(outline["parts"][0]["title"], "第一部 雾港")
        self.assertEqual(outline["chapters"], [])
        entry = result["book"]["outline_versions"][-1]
        self.assertEqual(entry["schema"], 2)
        self.assertEqual(entry["parts_count"], 1)
        self.assertEqual(entry["chapter_count"], 0)

    def test_two_parts_do_not_overwrite_each_other(self):
        self.store.save_outline(self.book_id, parts=[{"no": 1, "title": "一", "text": "甲"}])
        self.store.save_outline(
            self.book_id,
            parts=[{"no": 1, "title": "一", "text": "甲"}, {"no": 2, "title": "二", "text": "乙"}],
        )
        outline = self.store.load_outline(self.book_id)
        self.assertEqual([row["text"] for row in outline["parts"]], ["甲", "乙"])
        self.assertIn("【二】", outline_parts_text(outline["parts"]))

    def test_parts_and_chapters_are_mutually_exclusive(self):
        with self.assertRaises(WriterStoreError):
            self.store.save_outline(self.book_id)
        with self.assertRaises(WriterStoreError):
            self.store.save_outline(
                self.book_id,
                parts=[{"no": 1, "text": "甲"}],
                chapters=[{"no": 1, "title": "第1章"}],
            )

    def test_parts_never_sync_chapters(self):
        with self.assertRaises(WriterStoreError):
            self.store.save_outline(
                self.book_id, parts=[{"no": 1, "text": "甲"}], sync=True)
        self.assertEqual(self.store.get_book(self.book_id)["chapters"], [])

    def test_legacy_chapter_outline_still_works(self):
        """老作品（逐章大纲）必须继续能读能存——迁移不是强制的。"""
        self.store.save_outline(
            self.book_id,
            chapters=[{"no": 1, "title": "第1章", "beat": "梗概", "hook": ""}],
            sync=True,
        )
        outline = self.store.load_outline(self.book_id)
        self.assertEqual(outline["schema"], 1)
        self.assertEqual(len(outline["chapters"]), 1)
        self.assertEqual(self.store.get_book(self.book_id)["chapters"][0]["title"], "第1章")


class NormalizePartsTests(unittest.TestCase):
    def test_requires_continuous_unique_numbers(self):
        self.assertEqual([row["no"] for row in normalize_outline_parts(
            [{"no": 1, "text": "a"}, {"no": 2, "text": "b"}])], [1, 2])
        for bad in ([{"no": 1, "text": "a"}, {"no": 3, "text": "b"}],
                    [{"no": 1, "text": "a"}, {"no": 1, "text": "b"}],
                    []):
            with self.assertRaises(WriterStoreError):
                normalize_outline_parts(bad)

    def test_limits(self):
        with self.assertRaises(WriterStoreError):
            normalize_outline_parts([{"no": no, "text": "x"} for no in range(1, 22)])
        with self.assertRaises(WriterStoreError):
            normalize_outline_parts([{"no": 1, "text": "字" * 200_001}])

    def test_default_title(self):
        rows = normalize_outline_parts([{"no": 1, "text": "a"}])
        self.assertEqual(rows[0]["title"], "第 1 部")


class AppendChaptersTests(_StoreCase):
    def _parts(self):
        self.store.save_outline(
            self.book_id, parts=[{"no": 1, "title": "一", "text": "甲"}])

    def test_append_keeps_order_and_part(self):
        self._parts()
        first = self.store.append_chapters(
            self.book_id,
            [{"no": 1, "title": "第1章 归港", "beat": "梗概1"},
             {"no": 2, "title": "第2章 照片", "beat": "梗概2"}],
            part_no=1,
        )
        self.assertEqual(first["appended"], [1, 2])
        second = self.store.append_chapters(
            self.book_id, [{"no": 3, "title": "第3章 注销", "beat": "梗概3"}], part_no=1)
        self.assertEqual(second["appended"], [3])
        chapters = self.store.get_book(self.book_id)["chapters"]
        self.assertEqual([row["no"] for row in chapters], [1, 2, 3])
        self.assertTrue(all(row["part_no"] == 1 for row in chapters))

    def test_existing_chapters_are_skipped_not_overwritten(self):
        self._parts()
        self.store.append_chapters(
            self.book_id, [{"no": 1, "title": "第1章 原名", "beat": "原梗概"}], part_no=1)
        self.store.save_chapter_version(self.book_id, 1, content="已有正文", kind="manual")
        again = self.store.append_chapters(
            self.book_id, [{"no": 1, "title": "第1章 覆盖名", "beat": "新梗概"}], part_no=1)
        self.assertEqual(again["appended"], [])
        chapters = self.store.get_book(self.book_id)["chapters"]
        self.assertEqual(chapters[0]["title"], "第1章 原名")
        self.assertEqual(chapters[0]["len"], len("已有正文"))

    def test_rejects_empty_and_out_of_range(self):
        self._parts()
        with self.assertRaises(WriterStoreError):
            self.store.append_chapters(self.book_id, [])
        with self.assertRaises(WriterStoreError):
            self.store.append_chapters(
                self.book_id, [{"no": 10000, "title": "第10000章"}], part_no=1)


class ChapterPlanAndMetaTests(_StoreCase):
    def setUp(self):
        super().setUp()
        self.store.save_outline(
            self.book_id, parts=[{"no": 1, "title": "一", "text": "甲"}])
        self.store.append_chapters(
            self.book_id, [{"no": 1, "title": "第1章", "beat": "旧梗概"}], part_no=1)

    def test_plan_defaults_and_validation(self):
        self.assertEqual(normalize_chapter_plan({}), {"chars": 0, "ending": "suspense"})
        payload = self.store.set_chapter_plan(self.book_id, chars=3000, ending="hook")
        self.assertEqual(payload["chapter_plan"], {"chars": 3000, "ending": "hook"})
        self.assertEqual(
            self.store.get_book(self.book_id)["book"]["chapter_plan"],
            {"chars": 3000, "ending": "hook"},
        )
        with self.assertRaises(WriterStoreError):
            self.store.set_chapter_plan(self.book_id, chars=1234)
        with self.assertRaises(WriterStoreError):
            self.store.set_chapter_plan(self.book_id, ending="cliff")

    def test_meta_update_touches_only_meta(self):
        self.store.save_chapter_version(self.book_id, 1, content="正文内容", kind="manual")
        payload = self.store.update_chapter_meta(
            self.book_id, 1, title="第1章 新名", beat="新简述")
        self.assertEqual(payload["chapter"]["title"], "第1章 新名")
        detail = self.store.get_book(self.book_id)["chapters"][0]
        self.assertEqual(detail["beat"], "新简述")
        self.assertEqual(self.store.get_chapter(self.book_id, 1)["content"], "正文内容")
        with self.assertRaises(LookupError):
            self.store.update_chapter_meta(self.book_id, 99, title="x")


class OutlinePromptsTests(unittest.TestCase):
    def test_outline_text_prompt_asks_for_plain_text(self):
        text = prompts.build_outline_text_user(
            {"idea": "修船工查旧照片", "worldview": "雾港"}, part_no=2, part_title="归墟",
            target_chars=9000)
        self.assertIn("【本部】第 2 部 · 归墟", text)
        self.assertIn("9000", text)
        self.assertIn("不要 JSON", text)

    def test_parse_outline_text_strips_fence(self):
        fenced = "```\n第一部主线……\n```"
        self.assertEqual(prompts.parse_outline_text(fenced), "第一部主线……")
        with self.assertRaises(prompts.PromptError):
            prompts.parse_outline_text("   ")

    def test_split_prompt_and_parser(self):
        text = prompts.build_split_user(
            {"idea": "x"}, part_text="大纲正文", want_count=20, from_no=101)
        self.assertIn("从第 101 章开始", text)
        self.assertIn("拆 20 章", text)
        rows = prompts.parse_split(
            '{"chapters":[{"title":"第1章 A","beat":"b"},{"title":"第2章 B"}]}',
            from_no=101, want_count=10)
        self.assertEqual([row["no"] for row in rows], [101, 102])
        # 少于 want_count 允许（一批拆不完），一条没有则失败。
        self.assertEqual(len(prompts.parse_split(
            '{"chapters":[{"title":"第1章 A"}]}', from_no=1, want_count=200)), 1)
        with self.assertRaises(prompts.PromptError):
            prompts.parse_split('{"chapters":[]}', from_no=1, want_count=10)


if __name__ == "__main__":
    unittest.main()


class SetupBatch3Tests(unittest.TestCase):
    """批次三：标签 20 / 参考作品 / 角色主配 / 30+ 风格。"""

    def test_tags_cap_20(self):
        from naiba.writer.store import TAGS_MAX, normalize_setup
        setup = normalize_setup({"idea": "x", "genre_tags": [f"t{i}" for i in range(30)]})
        self.assertEqual(TAGS_MAX, 20)
        self.assertEqual(len(setup["genre_tags"]), 20)

    def test_references_normalized_and_capped(self):
        from naiba.writer.store import REFS_MAX, normalize_setup
        setup = normalize_setup({
            "idea": "x",
            "references": [
                {"title": "长夜难明", "dims": ["故事背景", "叙事节奏", "不存在的维度"], "note": "学结构"},
                {"title": "   "},
                {"title": "同人书", "same_fanfic": True},
            ],
        })
        self.assertEqual(REFS_MAX, 8)
        self.assertEqual(len(setup["references"]), 2)
        self.assertEqual(setup["references"][0]["dims"], ["故事背景", "叙事节奏"])
        self.assertTrue(setup["references"][1]["same_fanfic"])

    def test_characters_lead_side_limits(self):
        from naiba.writer.store import LEAD_MAX, SIDE_MAX, normalize_setup
        self.assertEqual((LEAD_MAX, SIDE_MAX), (9, 99))
        payload = {"idea": "x", "characters": (
            [{"name": f"主角{i}", "role": "lead"} for i in range(1, 12)]
            + [{"name": f"配角{i}", "role": "side"} for i in range(1, 3)]
        )}
        setup = normalize_setup(payload)
        leads = [row for row in setup["characters"] if row["role"] == "lead"]
        sides = [row for row in setup["characters"] if row["role"] == "side"]
        self.assertEqual(len(leads), 9, "超过 9 的主角降级为配角")
        self.assertEqual(len(sides), 4)

    def test_character_legacy_fields_map_to_new(self):
        from naiba.writer.store import normalize_setup
        setup = normalize_setup({"idea": "x", "characters": [{
            "name": "陈砚", "traits": "沉默", "speech": "低声", "background": "修船工之子",
        }]})
        row = setup["characters"][0]
        self.assertEqual(row["personality"], "沉默")
        self.assertEqual(row["voice"], "低声")
        self.assertEqual(row["intro"], "修船工之子")
        # 旧键仍在，老页面/老提示词不炸
        self.assertEqual(row["traits"], "沉默")

    def test_style_presets_30_and_prompt(self):
        self.assertGreaterEqual(len(prompts.STYLE_PRESETS), 30)
        self.assertIn("hardboiled", prompts.STYLE_PRESETS)
        self.assertTrue(prompts.style_prompt("hardboiled"))
        self.assertTrue(prompts.style_prompt("no-such-style") == "")  # 未知回落到默认（空约束）

    def test_references_feed_the_prompt(self):
        setup = {
            "idea": "x",
            "references": [{"title": "长夜难明", "dims": ["故事背景"], "note": "学结构", "same_fanfic": False}],
            "constraints": {"avoid": "血腥"},
        }
        block = prompts._constraints_block(setup)
        self.assertIn("【参考作品】", block)
        self.assertIn("《长夜难明》", block)
        self.assertIn("只参考故事背景", block)

    def test_character_slot_prefers_leads(self):
        setup = {"characters": [
            {"name": "配角甲", "role": "side", "identity": "档案员"},
            {"name": "主角乙", "role": "lead", "age": "34"},
        ]}
        text = prompts._characters_text(setup, count=1)
        self.assertIn("主角乙", text)
        self.assertNotIn("配角甲", text)
