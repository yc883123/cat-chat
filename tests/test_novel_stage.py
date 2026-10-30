# -*- coding: utf-8 -*-
"""护栏：阶段二的要素/大纲生成（《阶段二》§九.1）。

覆盖：
- 长文策略：分章节 + 分槽上限 + 摘要链 + 超限明示（截断绝不静默）
- 任务契约：input 必须带 module=novel / origin_label=拆书匠 / novel 字段
- 分区：module / exclude_module 先过滤再 limit，各分区互不挤占
- 字段提升：列表 slim 的 module / origin_label 从 input 取（修「来源未记录」）
- 写回：任务 id 与阶段核对、revision 乐观并发、要素分类拆分

边界：不连模型、不跑真实 runner；任务用 StudioStore 直接造记录。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.novel import prompts  # noqa: E402
from naiba.novel.service import (  # noqa: E402
    NovelConflictError,
    NovelServiceError,
    NovelStore,
)
from naiba.studio.store import StudioStore  # noqa: E402


def _book_text(chapters: int = 8, body: str = "这是一段正文内容。" * 60) -> str:
    return "".join(f"第{i}章 标题{i}\n{body}\n" for i in range(1, chapters + 1))


def _chapters(count: int = 3, length: int = 500) -> list[dict]:
    return [
        {"i": index, "title": f"第{index}章", "content": "甲" * length}
        for index in range(1, count + 1)
    ]


class PromptSlotTests(unittest.TestCase):
    """分槽上限与截断明示。"""

    def test_body_slot_truncates_and_reports(self):
        chapters = _chapters(1, length=prompts.BODY_SLOT_CHARS + 500)
        built = prompts.build_stage_content(
            stage="elements", book_title="书", chapters=chapters,
            chapter_ids=[1], summaries={},
        )
        self.assertEqual(len(built["warnings"]), 1)
        warning = built["warnings"][0]
        self.assertIn("超出单章上限", warning)
        self.assertIn("500", warning, "必须说明超了多少字，不能只说超了")

    def test_short_chapter_has_no_warning(self):
        chapters = _chapters(1, length=100)
        built = prompts.build_stage_content(
            stage="elements", book_title="书", chapters=chapters,
            chapter_ids=[1], summaries={},
        )
        self.assertEqual(built["warnings"], [])

    def test_too_many_chapters_is_refused_not_silently_trimmed(self):
        """超章数上限必须拒绝并说明差多少，不静默减章。"""
        count = prompts.MAX_CHAPTERS_PER_CALL + 2
        with self.assertRaises(prompts.PromptError) as ctx:
            prompts.build_stage_content(
                stage="elements", book_title="书", chapters=_chapters(count),
                chapter_ids=list(range(1, count + 1)), summaries={},
            )
        message = str(ctx.exception)
        self.assertIn(str(prompts.MAX_CHAPTERS_PER_CALL), message)
        self.assertIn("2 章", message, "要说明多了几章")

    def test_empty_chapter_list_refused(self):
        with self.assertRaises(prompts.PromptError):
            prompts.build_stage_content(
                stage="elements", book_title="书", chapters=[], chapter_ids=[], summaries={},
            )

    def test_extra_note_truncated_with_warning(self):
        built = prompts.build_stage_content(
            stage="outline", book_title="书", chapters=_chapters(1),
            chapter_ids=[1], summaries={}, extra_note="补" * (prompts.EXTRA_NOTE_CHARS + 30),
        )
        self.assertTrue(any("补充要求" in w for w in built["warnings"]), built["warnings"])

    def test_stage_instructions_differ(self):
        elements = prompts.build_stage_content(
            stage="elements", book_title="书", chapters=_chapters(1),
            chapter_ids=[1], summaries={},
        )["content"]
        outline = prompts.build_stage_content(
            stage="outline", book_title="书", chapters=_chapters(1),
            chapter_ids=[1], summaries={},
        )["content"]
        self.assertIn("人物", elements)
        self.assertIn("三幕", outline)
        self.assertNotEqual(elements, outline)

    def test_unknown_stage_refused(self):
        with self.assertRaises(prompts.PromptError):
            prompts.build_stage_content(
                stage="storyboard", book_title="书", chapters=_chapters(1),
                chapter_ids=[1], summaries={},
            )


class SummaryChainTests(unittest.TestCase):
    def test_summary_keeps_head_and_tail(self):
        text = "头" * 200 + "中" * 500 + "尾" * 400
        summary = prompts.summarize_chapter(text)
        self.assertTrue(summary.startswith("头" * prompts.SUMMARY_HEAD_CHARS))
        self.assertTrue(summary.endswith("尾" * prompts.SUMMARY_TAIL_CHARS))
        self.assertIn("…", summary)

    def test_short_chapter_summary_is_whole(self):
        self.assertEqual(prompts.summarize_chapter("很短的一章。"), "很短的一章。")

    def test_chain_only_includes_prior_chapters(self):
        """批次内的章节不算「前文」——它们的正文已经在正文槽里。"""
        summaries = {str(n): f"摘要{n}" for n in range(1, 8)}
        chain = prompts.build_summary_chain(summaries, [6, 7])
        self.assertNotIn("摘要6", chain)
        self.assertNotIn("摘要7", chain)
        for number in (3, 4, 5):
            self.assertIn(f"摘要{number}", chain)

    def test_chain_takes_only_last_three(self):
        summaries = {str(n): f"摘要{n}" for n in range(1, 8)}
        chain = prompts.build_summary_chain(summaries, [8])
        for number in (1, 2):
            self.assertNotIn(f"摘要{number}", chain)
        for number in (5, 6, 7):
            self.assertIn(f"摘要{number}", chain)

    def test_no_prior_chapters_gives_empty_chain(self):
        self.assertEqual(prompts.build_summary_chain({str(n): "x" for n in (1, 2)}, [1, 2]), "")


class TaskFieldContractTests(unittest.TestCase):
    """任务 input 字段契约（总计划 §5.3）。"""

    def test_task_title_format(self):
        self.assertEqual(prompts.task_title("长夜灯", "elements"), "《长夜灯》· 要素")
        self.assertEqual(prompts.task_title("长夜灯", "outline"), "《长夜灯》· 改编大纲")


class PartitionTests(unittest.TestCase):
    """任务分区：module / exclude_module，先过滤再 limit。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StudioStore(Path(self.tmp.name))

    def _make(self, module: str, origin: str = "", title: str = "t") -> str:
        record = self.store.create_task({
            "content": "x", "model_key": "online:k", "model_name": "m",
            "title": title, "module": module, "origin_label": origin,
        })
        return record["id"]

    def test_filter_by_module(self):
        novel_id = self._make("novel", "创作场")
        other_id = self._make("global", "通用")
        rows = self.store.list_tasks(module="novel")
        self.assertEqual([row["id"] for row in rows], [novel_id])
        self.assertNotIn(other_id, [row["id"] for row in rows])

    def test_exclude_module(self):
        novel_id = self._make("novel", "创作场")
        other_id = self._make("global", "通用")
        rows = self.store.list_tasks(exclude_module="novel")
        self.assertEqual([row["id"] for row in rows], [other_id])
        self.assertNotIn(novel_id, [row["id"] for row in rows])

    def test_no_param_keeps_full_list(self):
        self._make("novel")
        self._make("global")
        self.assertEqual(len(self.store.list_tasks()), 2)

    def test_filter_applies_before_limit(self):
        """先过滤后 limit：模块库任务再多也不挤掉创作场的 60 条额度。"""
        for _ in range(5):
            self._make("global", title="模板任务")
        novel_ids = [self._make("novel", "创作场", title="小说任务") for _ in range(3)]
        rows = self.store.list_tasks(limit=3, module="novel")
        self.assertEqual({row["id"] for row in rows}, set(novel_ids))

    def test_empty_module_string_means_no_filter(self):
        self._make("novel")
        self.assertEqual(len(self.store.list_tasks(module="")), 1)


class TaskFieldPromotionTests(unittest.TestCase):
    """修既有 bug：列表 slim 的 module / origin_label 从 input 提升。"""

    def test_slim_promotes_fields_from_input(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StudioStore(Path(tmp.name))
        store.create_task({
            "content": "x", "model_key": "online:k", "model_name": "m",
            "title": "《书》· 要素", "module": "novel", "origin_label": "拆书匠",
        })

        # api_tasks 只需要 jobs.tasks；用最小替身装配，不启动整个 App。
        class _Jobs:
            def __init__(self, backing):
                self.backing = backing

            def tasks(self, active_only=False, module="", exclude_module="", exclude_modules=""):
                return self.backing.list_tasks(
                    active_only=active_only, module=module, exclude_module=exclude_module,
                    exclude_modules=exclude_modules,
                )

        from naiba.studio.service import StudioService

        service = object.__new__(StudioService)
        service.jobs = _Jobs(store)
        payload, status = service.api_tasks()
        self.assertEqual(status, 200)
        row = payload["tasks"][0]
        self.assertEqual(row["module"], "novel", "module 必须从 input 提升")
        self.assertEqual(row["origin_label"], "拆书匠", "origin_label 必须从 input 提升")
        self.assertEqual(row["title"], "《书》· 要素", "title 必须从 input 提升，否则列表全退化为「生成任务」")

    def test_missing_fields_fall_back_to_empty(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StudioStore(Path(tmp.name))
        store.create_task({"content": "x", "model_key": "online:k", "model_name": "m", "title": "t"})

        class _Jobs:
            def __init__(self, backing):
                self.backing = backing

            def tasks(self, active_only=False, module="", exclude_module="", exclude_modules=""):
                return self.backing.list_tasks(
                    active_only=active_only, module=module, exclude_module=exclude_module,
                    exclude_modules=exclude_modules,
                )

        from naiba.studio.service import StudioService

        service = object.__new__(StudioService)
        service.jobs = _Jobs(store)
        row = service.api_tasks()[0]["tasks"][0]
        self.assertEqual(row["module"], "")
        self.assertEqual(row["origin_label"], "")


class ApplyResultTests(unittest.TestCase):
    """结果写回：要素分类、revision 乐观并发、空结果拒绝。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = NovelStore(Path(self.tmp.name))
        staged = self.store.stage_import(
            filename="书.txt", data=_book_text(6).encode("utf-8"), max_len=6000,
        )
        self.book = self.store.create_book(
            import_id=staged["import_id"], picked=[1, 2, 3], title="长夜灯",
        )

    def test_apply_elements_splits_categories(self):
        raw = (
            "## 人物\n· 沈砚：书生，谨慎\n· 老周：船夫\n"
            "## 场景\n· 长桥：第1章\n"
            "## 道具\n· 无名信：推动调查\n"
        )
        book = self.store.apply_elements(self.book["id"], task_id="st-1", raw=raw)
        self.assertEqual(book["elements"]["characters"], ["沈砚：书生，谨慎", "老周：船夫"])
        self.assertEqual(book["elements"]["scenes"], ["长桥：第1章"])
        self.assertEqual(book["elements"]["props"], ["无名信：推动调查"])
        self.assertEqual(book["elements"]["raw"], raw, "原文必须整段留存")

    def test_apply_elements_appends_versions(self):
        first = self.store.apply_elements(self.book["id"], task_id="st-1", raw="## 人物\n- 旧版人物")
        second = self.store.apply_elements(self.book["id"], task_id="st-2", raw="## 人物\n- 新版人物")
        self.assertEqual([row["version"] for row in second["elements_versions"]], [1, 2])
        self.assertIn("旧版人物", second["elements_versions"][0]["raw"])
        self.assertIn("新版人物", second["elements_versions"][1]["raw"])
        self.assertEqual(second["elements"]["version"], 2)

    def test_same_task_cannot_create_duplicate_version(self):
        self.store.apply_elements(self.book["id"], task_id="same-task", raw="## 人物\n- 人物")
        with self.assertRaises(NovelConflictError):
            self.store.apply_elements(self.book["id"], task_id="same-task", raw="## 人物\n- 人物")

    def test_book_list_omits_version_payloads_but_detail_keeps_them(self):
        self.store.apply_elements(self.book["id"], task_id="st-1", raw="旧版完整正文")
        listing = self.store.list_books()["books"][0]
        detail = self.store.get_book(self.book["id"])["book"]
        self.assertEqual(listing["elements_versions"], [])
        self.assertEqual(detail["elements_versions"][0]["raw"], "旧版完整正文")

    def test_stage_version_selection_rejects_invalid_values(self):
        from naiba.novel.service import NovelService
        versions = [{"version": 1}, {"version": 2}]
        with self.assertRaises(NovelServiceError):
            NovelService._selected_version({"outline_version": "abc"}, "outline_version", versions, 2)
        with self.assertRaises(NovelServiceError):
            NovelService._selected_version({"outline_version": 99}, "outline_version", versions, 2)
        self.assertEqual(NovelService._selected_version({}, "outline_version", versions, 2), 2)

    def test_apply_elements_keeps_unparsable_raw(self):
        raw = "这段没有标题也没有列表项，只是普通文字。"
        book = self.store.apply_elements(self.book["id"], task_id="st-1", raw=raw)
        self.assertEqual(book["elements"]["characters"], [])
        self.assertEqual(book["elements"]["raw"], raw)

    def test_apply_rejects_empty_result(self):
        with self.assertRaises(NovelServiceError):
            self.store.apply_elements(self.book["id"], task_id="st-1", raw="   ")
        with self.assertRaises(NovelServiceError):
            self.store.apply_outline(self.book["id"], task_id="st-1", text="")

    def test_apply_outline_stores_text_and_task(self):
        book = self.store.apply_outline(self.book["id"], task_id="st-9", text="## 第一幕 建置\n- 要点")
        self.assertIn("第一幕", book["outline"])
        self.assertEqual(book["outline_task_id"], "st-9")

    def test_apply_outline_appends_versions(self):
        self.store.apply_outline(self.book["id"], task_id="st-1", text="第一版")
        book = self.store.apply_outline(self.book["id"], task_id="st-2", text="第二版")
        self.assertEqual([row["version"] for row in book["outline_versions"]], [1, 2])
        self.assertEqual(book["outline_versions"][0]["text"], "第一版")
        self.assertEqual(book["outline"], "第二版")

    def test_apply_revision_conflict(self):
        """A 未完又生成 B：revision 对不上就拒绝覆盖。"""
        current = self.store.get_book(self.book["id"])["book"]["revision"]
        self.store.apply_outline(self.book["id"], task_id="st-1", text="第一版大纲",
                                 expected_revision=current)
        with self.assertRaises(NovelConflictError):
            self.store.apply_outline(self.book["id"], task_id="st-2", text="基于旧版本的大纲",
                                     expected_revision=current)

    def test_summaries_merge_and_persist(self):
        self.store.update_summaries(self.book["id"], {"1": "第一章摘要", "2": "第二章摘要"})
        book = self.store.get_book(self.book["id"])["book"]
        self.assertEqual(book["summaries"]["1"], "第一章摘要")
        # 再合并一次不能丢已有条目。
        self.store.update_summaries(self.book["id"], {"3": "第三章摘要"})
        book = self.store.get_book(self.book["id"])["book"]
        self.assertEqual(set(book["summaries"]), {"1", "2", "3"})

    def test_stage_material_returns_selected_chapters_only(self):
        material = self.store.stage_material(self.book["id"], [2])
        self.assertEqual(material["chapter_ids"], [2])
        self.assertEqual(len(material["chapters"]), 1)
        self.assertIn("正文", material["chapters"][0]["content"])

    def test_stage_material_rejects_unknown_chapter(self):
        with self.assertRaises(LookupError):
            self.store.stage_material(self.book["id"], [99])

    def test_stage_material_rejects_non_int(self):
        with self.assertRaises(NovelServiceError):
            self.store.stage_material(self.book["id"], [True])


if __name__ == "__main__":
    unittest.main()
