# -*- coding: utf-8 -*-
"""护栏：阶段三的剧本场次与全书导出（《阶段三》§六.1）。

覆盖：
- 剧本 prompt 必带大纲槽 + 人物槽（不带就会同一个角色换个名字）
- 场次解析成功与失败两条路径（解析失败整段原样存，**不臆造切分**）
- 场次增删改序；重排保留 `from_chapter`
- 全书导出顺序、标题去重、附录（要素/大纲）
- 同章重新生成替换旧场次，不叠加

边界：不连模型、不跑 runner；任务记录用 StudioStore 直接造。
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.novel import prompts  # noqa: E402
from naiba.novel.service import NovelConflictError, NovelServiceError, NovelStore  # noqa: E402

SCRIPT_OK = """INT. 老宅书房 - 夜

沈砚推门进来，桌上的灯芯已经烧到底。

沈砚
这封信是谁送来的？

老周
桥上捡的。

EXT. 长桥 - 清晨

雾还没散，桥面上只有一行湿脚印。
"""


def _book_text(chapters: int = 5) -> str:
    return "".join(f"第{i}章 标题{i}\n" + "正文内容。" * 40 + "\n" for i in range(1, chapters + 1))


def _chapters(count: int = 2) -> list[dict]:
    return [{"i": index, "title": f"第{index}章", "content": "甲" * 200} for index in range(1, count + 1)]


class ScriptPromptTests(unittest.TestCase):
    def test_script_prompt_includes_outline_and_characters(self):
        built = prompts.build_stage_content(
            stage="script", book_title="长夜灯", chapters=_chapters(1),
            chapter_ids=[1], summaries={},
            outline="## 第一幕 建置\n- 沈砚收到无名信",
            characters=["沈砚：书生", "老周：船夫"],
        )
        content = built["content"]
        self.assertIn("改编大纲", content)
        self.assertIn("沈砚收到无名信", content)
        self.assertIn("人物设定", content)
        self.assertIn("老周：船夫", content)
        self.assertIn("INT.", content, "剧本 prompt 必须要求场景行格式")

    def test_script_prompt_without_outline_still_works(self):
        built = prompts.build_stage_content(
            stage="script", book_title="书", chapters=_chapters(1),
            chapter_ids=[1], summaries={},
        )
        self.assertNotIn("改编大纲", built["content"], "没有大纲时不该留空标题")

    def test_outline_slot_truncated_with_warning(self):
        built = prompts.build_stage_content(
            stage="script", book_title="书", chapters=_chapters(1),
            chapter_ids=[1], summaries={},
            outline="纲" * (prompts.OUTLINE_SLOT_CHARS + 200),
        )
        self.assertTrue(any("大纲" in w and "上限" in w for w in built["warnings"]), built["warnings"])

    def test_characters_slot_truncated_with_warning(self):
        built = prompts.build_stage_content(
            stage="script", book_title="书", chapters=_chapters(1),
            chapter_ids=[1], summaries={},
            characters=["人物" + "x" * 200] * 10,
        )
        self.assertTrue(any("人物设定" in w for w in built["warnings"]), built["warnings"])

    def test_elements_stage_does_not_carry_outline(self):
        """要素/大纲阶段不该带大纲槽：它们本来就是产出这两样的。"""
        built = prompts.build_stage_content(
            stage="elements", book_title="书", chapters=_chapters(1),
            chapter_ids=[1], summaries={}, outline="旧大纲", characters=["沈砚"],
        )
        self.assertNotIn("旧大纲", built["content"])
        self.assertNotIn("人物设定", built["content"])


class SceneParseTests(unittest.TestCase):
    def test_parse_standard_script(self):
        scenes, parsed = prompts.parse_scenes(SCRIPT_OK, from_chapter=3)
        self.assertTrue(parsed)
        self.assertEqual(len(scenes), 2, [row["scene"] for row in scenes])
        self.assertIn("老宅书房", scenes[0]["scene"])
        self.assertIn("长桥", scenes[1]["scene"])
        self.assertEqual(scenes[0]["from_chapter"], 3)
        self.assertIn("这封信是谁送来的", scenes[0]["content"])

    def test_parse_failure_returns_empty_and_flag(self):
        """解析失败必须回 parsed=False，让调用方整段保存，绝不臆造切分。"""
        scenes, parsed = prompts.parse_scenes("这是一段没有场景行的普通文字。\n第二行。")
        self.assertFalse(parsed)
        self.assertEqual(scenes, [])

    def test_parse_handles_chinese_punctuation(self):
        text = "INT． 老宅书房 — 夜\n\n动作描写。\n"
        scenes, parsed = prompts.parse_scenes(text)
        self.assertTrue(parsed)
        self.assertIn("老宅书房", scenes[0]["scene"])

    def test_parse_keeps_text_before_first_scene(self):
        text = "先说明一句。\n\nINT. 桥上 - 日\n\n动作。\n"
        scenes, parsed = prompts.parse_scenes(text)
        self.assertTrue(parsed)
        self.assertEqual(len(scenes), 2)
        self.assertEqual(scenes[0]["scene"], "", "场景行之前的文字不该丢")
        self.assertIn("先说明一句", scenes[0]["content"])


class ScriptStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = NovelStore(Path(self.tmp.name))
        staged = self.store.stage_import(
            filename="书.txt", data=_book_text(5).encode("utf-8"), max_len=6000,
        )
        self.book = self.store.create_book(
            import_id=staged["import_id"], picked=[1, 2, 3], title="长夜灯",
        )

    def test_apply_script_parses_scenes(self):
        book = self.store.apply_script(self.book["id"], task_id="st-1", text=SCRIPT_OK, from_chapter=1)
        self.assertEqual(len(book["script"]), 2)
        self.assertTrue(book["script_parsed"])
        self.assertEqual(book["script"][0]["i"], 1)
        self.assertEqual(book["script"][1]["i"], 2)

    def test_apply_script_unparsable_stores_whole_text(self):
        raw = "完全不符合剧本格式的一段文字。"
        book = self.store.apply_script(self.book["id"], task_id="st-1", text=raw, from_chapter=1)
        self.assertFalse(book["script_parsed"])
        self.assertEqual(len(book["script"]), 1)
        self.assertEqual(book["script"][0]["scene"], "", "不能臆造场景行")
        self.assertEqual(book["script"][0]["content"], raw)

    def test_regenerating_same_chapter_replaces_not_appends(self):
        self.store.apply_script(self.book["id"], task_id="st-1", text=SCRIPT_OK, from_chapter=1)
        book = self.store.apply_script(self.book["id"], task_id="st-2", text=SCRIPT_OK, from_chapter=1)
        self.assertEqual(len(book["script"]), 2, "同一章重新生成应当替换，不该叠加")

    def test_regenerating_multiple_chapters_replaces_all_selected(self):
        self.store.apply_script(self.book["id"], task_id="st-1", text=SCRIPT_OK, from_chapter=2)
        self.store.apply_script(self.book["id"], task_id="st-2", text=SCRIPT_OK, from_chapter=3)
        book = self.store.apply_script(self.book["id"], task_id="st-3", text=SCRIPT_OK,
                                       from_chapter=2, chapter_ids=[2, 3])
        self.assertEqual(len(book["script"]), 2)
        self.assertEqual(book["script_versions"][-1]["chapter_ids"], [2, 3])

    def test_legacy_script_metadata_is_preserved_in_first_snapshot(self):
        book_dir = self.store._book_dir(self.book["id"])
        manifest = self.store._load_manifest(self.book["id"])
        manifest["script"] = [{"i": 1, "scene": "INT. 旧场景 - 夜", "content": "旧内容", "from_chapter": 1}]
        manifest["script_task_id"] = "old-task"
        manifest["script_updated_at"] = 123456
        self.store._write_json(book_dir / "book.json", manifest)
        result = self.store.apply_script(self.book["id"], task_id="new-task", text=SCRIPT_OK, from_chapter=2)
        self.assertEqual(result["script_versions"][0]["task_id"], "old-task")
        self.assertEqual(result["script_versions"][0]["updated_at"], 123456)

    def test_same_task_cannot_create_duplicate_script_version(self):
        self.store.apply_script(self.book["id"], task_id="same-task", text=SCRIPT_OK, from_chapter=1)
        with self.assertRaises(NovelConflictError):
            self.store.apply_script(self.book["id"], task_id="same-task", text=SCRIPT_OK, from_chapter=1)

    def test_different_chapters_accumulate(self):
        self.store.apply_script(self.book["id"], task_id="st-1", text=SCRIPT_OK, from_chapter=1)
        book = self.store.apply_script(self.book["id"], task_id="st-2", text=SCRIPT_OK, from_chapter=2)
        self.assertEqual(len(book["script"]), 4)
        self.assertEqual([row["from_chapter"] for row in book["script"]], [1, 1, 2, 2])

    def test_update_scene(self):
        self.store.apply_script(self.book["id"], task_id="st-1", text=SCRIPT_OK, from_chapter=1)
        result = self.store.update_scene(self.book["id"], 1, scene="INT. 新地点 - 日")
        self.assertIn("新地点", result["scene"]["scene"])
        # 改一条不影响另一条
        listing = self.store.list_script(self.book["id"])
        self.assertIn("长桥", listing["script"][1]["scene"])

    def test_update_scene_rejects_both_empty(self):
        self.store.apply_script(self.book["id"], task_id="st-1", text=SCRIPT_OK, from_chapter=1)
        with self.assertRaises(NovelServiceError):
            self.store.update_scene(self.book["id"], 1, scene="", content="   ")

    def test_update_missing_scene_raises_lookup(self):
        self.store.apply_script(self.book["id"], task_id="st-1", text=SCRIPT_OK, from_chapter=1)
        with self.assertRaises(LookupError):
            self.store.update_scene(self.book["id"], 99, scene="INT. x - 日")

    def test_delete_scene_renumbers(self):
        self.store.apply_script(self.book["id"], task_id="st-1", text=SCRIPT_OK, from_chapter=1)
        result = self.store.delete_scene(self.book["id"], 1)
        self.assertEqual(result["count"], 1)
        listing = self.store.list_script(self.book["id"])
        self.assertEqual([row["i"] for row in listing["script"]], [1], "删除后编号必须连续")

    def test_reorder_keeps_from_chapter(self):
        self.store.apply_script(self.book["id"], task_id="st-1", text=SCRIPT_OK, from_chapter=2)
        before = self.store.list_script(self.book["id"])["script"]
        result = self.store.reorder_script(self.book["id"], [2, 1])
        after = result["script"]
        self.assertEqual([row["from_chapter"] for row in after],
                         [before[1]["from_chapter"], before[0]["from_chapter"]])
        self.assertIn("长桥", after[0]["scene"], "重排后第一场应是原来的第二场")

    def test_reorder_rejects_incomplete_order(self):
        self.store.apply_script(self.book["id"], task_id="st-1", text=SCRIPT_OK, from_chapter=1)
        with self.assertRaises(NovelServiceError):
            self.store.reorder_script(self.book["id"], [1])
        with self.assertRaises(NovelServiceError):
            self.store.reorder_script(self.book["id"], [1, 1])
        with self.assertRaises(NovelServiceError):
            self.store.reorder_script(self.book["id"], [])

    def test_script_revision_conflict(self):
        current = self.store.get_book(self.book["id"])["book"]["revision"]
        self.store.apply_script(self.book["id"], task_id="st-1", text=SCRIPT_OK,
                                from_chapter=1, expected_revision=current)
        with self.assertRaises(NovelConflictError):
            self.store.apply_script(self.book["id"], task_id="st-2", text=SCRIPT_OK,
                                    from_chapter=2, expected_revision=current)

    def test_apply_script_rejects_empty(self):
        with self.assertRaises(NovelServiceError):
            self.store.apply_script(self.book["id"], task_id="st-1", text="   ", from_chapter=1)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = NovelStore(Path(self.tmp.name))
        staged = self.store.stage_import(
            filename="书.txt", data=_book_text(4).encode("utf-8"), max_len=6000,
        )
        self.book = self.store.create_book(
            import_id=staged["import_id"], picked=[1, 2, 3], title="长夜灯",
        )

    def test_txt_export_order_and_no_duplicate_titles(self):
        text = self.store.export_book(self.book["id"], fmt="txt")
        positions = [text.find(f"第{i}章") for i in (1, 2, 3)]
        self.assertTrue(all(p >= 0 for p in positions), positions)
        self.assertEqual(positions, sorted(positions))
        for number in (1, 2, 3):
            self.assertEqual(text.count(f"第{number}章 标题{number}"), 1, f"第{number}章标题重复了")

    def test_md_export_uses_headings(self):
        text = self.store.export_book(self.book["id"], fmt="md")
        self.assertTrue(text.startswith("# 长夜灯"))
        self.assertIn("## 第1章 标题1", text)

    def test_export_without_extras_has_no_appendix(self):
        text = self.store.export_book(self.book["id"], fmt="md", with_extras=False)
        self.assertNotIn("附录", text)

    def test_export_with_extras_appends_elements_and_outline(self):
        self.store.apply_elements(self.book["id"], task_id="st-1",
                                  raw="## 人物\n· 沈砚：书生")
        self.store.apply_outline(self.book["id"], task_id="st-2", text="## 第一幕 建置\n- 要点")
        text = self.store.export_book(self.book["id"], fmt="md", with_extras=True)
        self.assertIn("附录：要素", text)
        self.assertIn("附录：改编大纲", text)
        # 附录在正文之后
        self.assertGreater(text.find("附录：要素"), text.find("第3章"))

    def test_export_extras_skips_empty_sections(self):
        text = self.store.export_book(self.book["id"], fmt="md", with_extras=True)
        self.assertNotIn("附录：要素", text, "没有要素时不该写空附录")
        self.assertNotIn("附录：改编大纲", text)


if __name__ == "__main__":
    unittest.main()
