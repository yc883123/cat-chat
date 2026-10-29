# -*- coding: utf-8 -*-
"""护栏：章节原文与改编稿（《创作场原文改编与记录管理整改计划》§3–§4）。

覆盖：
- 原文与改编稿并存：编辑新增改编稿、原文逐字不变；
- 改编稿编号只增不重排、来源稿件可追溯、无改动不重复建卡；
- 单章按需读取：索引不带正文，单稿接口才给正文；
- 逐章稿件选择：要素/大纲/剧本按选定稿件取材，无效稿件 ID 报错不静默换原文；
- 摘要按「章节 + 稿件」记，换稿不串用另一份稿件的前文；
- 删除契约：删改编稿不动原文与下游成果；删掉该章全部稿件按「移除章节」处理；
- 老作品升级：能核对才恢复原文，否则标成「升级前保存的正文」，且可重试幂等。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.novel import manuscripts, prompts  # noqa: E402
from naiba.novel.service import NovelServiceError, NovelStore  # noqa: E402


def _book_text(chapters: int = 3, body: str = "原文正文内容。" * 40) -> str:
    return "".join(f"第{i}章 标题{i}\n{body}\n" for i in range(1, chapters + 1))


class ManuscriptTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = NovelStore(self.root)

    def _create(self, chapters: int = 3) -> dict:
        staged = self.store.stage_import(
            filename="测试书.txt", data=_book_text(chapters).encode("utf-8"), max_len=6000,
        )
        return self.store.create_book(
            import_id=staged["import_id"], picked=list(range(1, chapters + 1)),
        )


class OriginalAndAdaptedTests(ManuscriptTestCase):
    def test_new_book_starts_with_one_original_manuscript(self):
        book = self._create(chapters=2)
        chapter = self.store.get_chapter(book["id"], 1)
        self.assertEqual(len(chapter["manuscripts"]), 1)
        row = chapter["manuscripts"][0]
        self.assertEqual(row["kind"], manuscripts.KIND_ORIGINAL)
        self.assertEqual(row["label"], "原文")
        self.assertEqual(row["id"], chapter["base_manuscript_id"])
        # 原文正文由单稿接口给，不是索引里带出来的。
        self.assertTrue(chapter["content"])

    def test_edit_creates_adapted_and_keeps_original(self):
        book = self._create(chapters=2)
        original = self.store.get_chapter(book["id"], 1)["content"]
        result = self.store.update_chapter(book["id"], 1, content="改编后的正文。")
        created = result["created_manuscript"]
        self.assertEqual(created["kind"], manuscripts.KIND_MANUAL)
        self.assertEqual(created["no"], 1)
        self.assertEqual(created["label"], "改编稿 1")
        self.assertEqual(created["source_manuscript_id"], result["base_manuscript_id"])
        # 原文逐字不变。
        self.assertEqual(self.store.get_chapter(book["id"], 1)["content"], original)
        rows = self.store.get_chapter(book["id"], 1)["manuscripts"]
        self.assertEqual({row["kind"] for row in rows}, {"original", "manual"})

    def test_platform_numbering_only_increases(self):
        """删掉改编稿 1 后新建的稿件不能复用编号。"""
        book = self._create(chapters=1)
        first = self.store.save_manuscript(book["id"], 1, content="第一版改编。")["manuscript"]
        self.assertEqual(first["no"], 1)
        self.store.delete_manuscript(book["id"], 1, first["id"])
        second = self.store.save_manuscript(book["id"], 1, content="第二版改编。")["manuscript"]
        self.assertEqual(second["no"], 2, "编号只增不重排，不能复用已删除的编号")

    def test_unchanged_content_is_not_saved_twice(self):
        book = self._create(chapters=1)
        base = self.store.get_chapter(book["id"], 1)["base_manuscript_id"]
        first = self.store.save_manuscript(book["id"], 1, content="同样的改编正文。")
        second = self.store.save_manuscript(book["id"], 1, content="同样的改编正文。")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"], "内容未改变不新增重复卡片")
        self.assertEqual(first["manuscript"]["id"], second["manuscript"]["id"])
        self.assertEqual(
            len([r for r in self.store.get_chapter(book["id"], 1)["manuscripts"]
                 if r["kind"] == manuscripts.KIND_MANUAL]),
            1,
        )
        self.assertEqual(base, self.store.get_chapter(book["id"], 1)["base_manuscript_id"])

    def test_adapted_from_adapted_keeps_source_chain(self):
        book = self._create(chapters=1)
        one = self.store.save_manuscript(book["id"], 1, content="改编稿一的正文。")["manuscript"]
        two = self.store.save_manuscript(
            book["id"], 1, content="改编稿二的正文。", source_manuscript_id=one["id"],
        )["manuscript"]
        self.assertEqual(two["source_manuscript_id"], one["id"])
        self.assertEqual(two["no"], 2)

    def test_invalid_manuscript_id_is_an_error_not_a_fallback(self):
        book = self._create(chapters=1)
        with self.assertRaises(NovelServiceError):
            self.store.save_manuscript(
                book["id"], 1, content="正文。",
                source_manuscript_id="ms-ffffffffffff",
            )

    def test_only_adapted_kinds_can_be_saved(self):
        book = self._create(chapters=1)
        for kind in (manuscripts.KIND_ORIGINAL, manuscripts.KIND_LEGACY, "bogus"):
            with self.assertRaises(NovelServiceError, msg=f"应拒绝：{kind}"):
                self.store.save_manuscript(book["id"], 1, content="正文。", kind=kind)

    def test_empty_content_is_refused(self):
        book = self._create(chapters=1)
        with self.assertRaises(NovelServiceError):
            self.store.save_manuscript(book["id"], 1, content="   ")


class OnDemandBodyTests(ManuscriptTestCase):
    def test_chapter_payload_omits_other_bodies(self):
        book = self._create(chapters=1)
        self.store.save_manuscript(book["id"], 1, content="第二份稿件的独有正文。")
        chapter = self.store.get_chapter(book["id"], 1)
        # 稿件清单只给元数据，正文要按单稿取。
        for row in chapter["manuscripts"]:
            self.assertNotIn("content", row)
        payload = json.dumps(chapter, ensure_ascii=False)
        self.assertNotIn("第二份稿件的独有正文", payload)

    def test_manuscript_endpoint_returns_body(self):
        book = self._create(chapters=1)
        saved = self.store.save_manuscript(book["id"], 1, content="按需取的正文。")["manuscript"]
        row = self.store.get_manuscript(book["id"], 1, saved["id"])
        self.assertEqual(row["content"], "按需取的正文。")
        self.assertEqual(row["id"], saved["id"])

    def test_missing_manuscript_raises_lookup_or_error(self):
        book = self._create(chapters=1)
        with self.assertRaises(NovelServiceError):
            self.store.get_manuscript(book["id"], 1, "ms-000000000000")


class ManuscriptSelectionTests(ManuscriptTestCase):
    def test_default_selection_is_original(self):
        book = self._create(chapters=2)
        material = self.store.stage_material(book["id"], [1, 2])
        for row in material["chapters"]:
            self.assertEqual(row["manuscript_kind"], manuscripts.KIND_ORIGINAL)

    def test_selected_manuscript_is_used(self):
        book = self._create(chapters=2)
        adapted = self.store.save_manuscript(book["id"], 1, content="用这份改编稿生成。")["manuscript"]
        material = self.store.stage_material(book["id"], [1, 2], {1: adapted["id"]})
        first = next(row for row in material["chapters"] if row["i"] == 1)
        second = next(row for row in material["chapters"] if row["i"] == 2)
        self.assertEqual(first["content"], "用这份改编稿生成。")
        self.assertEqual(first["manuscript_kind"], manuscripts.KIND_MANUAL)
        self.assertEqual(second["manuscript_kind"], manuscripts.KIND_ORIGINAL, "未指定的章仍用原文")

    def test_mixed_selection_across_chapters(self):
        book = self._create(chapters=3)
        adapted = self.store.save_manuscript(book["id"], 2, content="第二章的改编稿。")["manuscript"]
        material = self.store.stage_material(book["id"], [1, 2, 3], {2: adapted["id"]})
        kinds = {row["i"]: row["manuscript_kind"] for row in material["chapters"]}
        self.assertEqual(kinds, {1: "original", 2: "manual", 3: "original"})

    def test_manuscript_from_another_chapter_is_refused(self):
        book = self._create(chapters=2)
        adapted = self.store.save_manuscript(book["id"], 2, content="第二章改编。")["manuscript"]
        with self.assertRaises(NovelServiceError):
            self.store.stage_material(book["id"], [1], {1: adapted["id"]})


class SummaryKeyTests(ManuscriptTestCase):
    def test_summary_key_includes_manuscript(self):
        self.assertEqual(prompts.summary_key(3, "ms-abcdefabcdef"), "3@ms-abcdefabcdef")
        self.assertEqual(prompts.summary_key(3), "3")

    def test_chain_uses_selected_manuscript_summary(self):
        """换稿不能误用另一份稿件的前文摘要。"""
        summaries = {
            prompts.summary_key(1, "ms-aaaaaaaaaaaa"): "原文摘要",
            prompts.summary_key(1, "ms-bbbbbbbbbbbb"): "改编稿摘要",
        }
        chain = prompts.build_summary_chain(summaries, [2], {2: "ms-cccccccccccc", 1: "ms-bbbbbbbbbbbb"})
        self.assertIn("改编稿摘要", chain)
        self.assertNotIn("原文摘要", chain)

    def test_chain_falls_back_to_legacy_key(self):
        summaries = {"1": "老数据摘要"}
        chain = prompts.build_summary_chain(summaries, [2], {1: "ms-aaaaaaaaaaaa"})
        self.assertIn("老数据摘要", chain)


class DeleteContractTests(ManuscriptTestCase):
    def test_delete_adapted_keeps_original_and_versions(self):
        book = self._create(chapters=2)
        adapted = self.store.save_manuscript(book["id"], 1, content="要删的改编稿。")["manuscript"]
        self.store.apply_outline(book["id"], task_id="t-1", text="第一版大纲")
        result = self.store.delete_manuscript(book["id"], 1, adapted["id"])
        self.assertFalse(result["removed_chapter"])
        chapter = self.store.get_chapter(book["id"], 1)
        self.assertEqual([row["kind"] for row in chapter["manuscripts"]], ["original"])
        self.assertEqual(chapter["content"], self.store.get_chapter(book["id"], 1)["content"])
        book_state = self.store.get_book(book["id"])["book"]
        self.assertEqual(book_state["outline_versions"][0]["text"], "第一版大纲",
                         "删改编稿不能级联删掉下游成果")

    def test_deleting_last_manuscript_removes_chapter(self):
        book = self._create(chapters=2)
        chapter = self.store.get_chapter(book["id"], 1)
        result = self.store.delete_manuscript(book["id"], 1, chapter["base_manuscript_id"])
        self.assertTrue(result["removed_chapter"])
        detail = self.store.get_book(book["id"])
        self.assertEqual([row["i"] for row in detail["chapters"]], [2])

    def test_removing_last_chapter_is_refused(self):
        book = self._create(chapters=1)
        chapter = self.store.get_chapter(book["id"], 1)
        with self.assertRaises(NovelServiceError):
            self.store.delete_manuscript(book["id"], 1, chapter["base_manuscript_id"])

    def test_delete_records_rejects_unknown_type_before_writing(self):
        book = self._create(chapters=1)
        with self.assertRaises(NovelServiceError):
            self.store.delete_records(book["id"], [{"type": "bogus", "id": "x"}])

    def test_delete_records_batch_is_atomic(self):
        """有一项无效就整批不动，不出现「删了一半」。"""
        book = self._create(chapters=1)
        adapted = self.store.save_manuscript(book["id"], 1, content="改编稿。")["manuscript"]
        with self.assertRaises(NovelServiceError):
            self.store.delete_records(book["id"], [
                {"type": "manuscript", "chapter_no": 1, "id": adapted["id"]},
                {"type": "manuscript", "chapter_no": 1, "id": "ms-ffffffffffff"},
            ])
        self.assertEqual(len(self.store.get_chapter(book["id"], 1)["manuscripts"]), 2,
                         "校验失败时不能真的删掉一部分")

    def test_delete_records_removes_versions(self):
        book = self._create(chapters=1)
        state = self.store.apply_outline(book["id"], task_id="t-1", text="大纲一")
        version_id = state["outline_versions"][0]["id"]
        self.assertTrue(version_id)
        result = self.store.delete_records(book["id"], [{"type": "outline", "id": version_id}])
        self.assertEqual([row["type"] for row in result["removed"]], ["outline"])
        self.assertEqual(result["book"]["outline_versions"], [])

    def test_records_listing_has_ids_for_every_category(self):
        book = self._create(chapters=2)
        self.store.save_manuscript(book["id"], 1, content="改编稿。")
        self.store.apply_elements(book["id"], task_id="t-1", raw="## 人物\n· 甲")
        self.store.apply_outline(book["id"], task_id="t-2", text="大纲")
        self.store.apply_script(book["id"], task_id="t-3", text="INT. 屋子 - 夜\n甲说话。",
                                chapter_ids=[1])
        records = self.store.list_records(book["id"])
        self.assertTrue(records["manuscripts"])
        self.assertTrue(all(row["id"] for row in records["manuscripts"]))
        self.assertTrue(all(row["id"] for row in records["elements"]))
        self.assertTrue(all(row["id"] for row in records["outlines"]))
        self.assertTrue(all(row["id"] for row in records["scripts"]))
        for row in records["manuscripts"]:
            self.assertNotIn("content", row, "记录清单不下发正文")


class ExportSelectionTests(ManuscriptTestCase):
    def test_export_uses_original_by_default(self):
        book = self._create(chapters=1)
        self.store.save_manuscript(book["id"], 1, content="改编稿的独有句子。")
        text = self.store.export_book(book["id"], fmt="txt")
        self.assertIn("原文正文内容", text)
        self.assertNotIn("改编稿的独有句子", text)

    def test_export_can_select_adapted(self):
        book = self._create(chapters=1)
        adapted = self.store.save_manuscript(book["id"], 1, content="改编稿的独有句子。")["manuscript"]
        text = self.store.export_book(book["id"], fmt="txt", manuscript_ids={1: adapted["id"]})
        self.assertIn("改编稿的独有句子", text)


class LegacyUpgradeTests(unittest.TestCase):
    """老作品（schema 1）升级：诚实标注恢复情况。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = NovelStore(self.root)

    def _make_legacy_book(self, *, keep_import: bool, edited: str = "") -> str:
        """造一个 schema 1 的作品（章节文件里只有 content）。"""
        data = _book_text(2).encode("utf-8")
        staged = self.store.stage_import(filename="老书.txt", data=data, max_len=6000)
        book = self.store.create_book(import_id=staged["import_id"], picked=[1, 2])
        book_dir = self.root / "studio" / "novel" / book["id"]
        # 退回 schema 1：清单与章节文件都只留旧字段。
        manifest = json.loads((book_dir / "book.json").read_text(encoding="utf-8"))
        manifest["schema_version"] = 1
        manifest.pop("origin_recovery", None)
        (book_dir / "book.json").write_text(json.dumps(manifest), encoding="utf-8")
        for chapter_no in (1, 2):
            chapter = manuscripts  # 占位，避免误用
            index_path = book_dir / "chapters" / f"{chapter_no:04d}.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            body = "原文正文内容。" * 40
            if chapter_no == 1 and edited:
                body = edited
            old = {
                "schema_version": 1,
                "revision": 1,
                "i": chapter_no,
                "title": f"第{chapter_no}章 标题{chapter_no}",
                "len": len(body),
                "content": body,
            }
            index_path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        import shutil
        shutil.rmtree(book_dir / "manuscripts", ignore_errors=True)
        if not keep_import:
            shutil.rmtree(self.root / "studio" / "novel" / "_imports" / staged["import_id"],
                          ignore_errors=True)
        return book["id"]

    def test_upgrade_restores_original_when_source_matches(self):
        book_id = self._make_legacy_book(keep_import=True)
        book = self.store.get_book(book_id)["book"]
        self.assertEqual(book["schema_version"], 2)
        self.assertEqual(book["origin_recovery"]["restored"], [1, 2])
        chapter = self.store.get_chapter(book_id, 1)
        self.assertEqual(chapter["manuscripts"][0]["kind"], manuscripts.KIND_ORIGINAL)

    def test_upgrade_marks_legacy_when_source_missing(self):
        book_id = self._make_legacy_book(keep_import=False, edited="用户改过的正文。")
        book = self.store.get_book(book_id)["book"]
        self.assertEqual(book["origin_recovery"]["restored"], [])
        self.assertEqual(book["origin_recovery"]["legacy"], [1, 2])
        self.assertTrue(book["origin_recovery"]["reason"])
        chapter = self.store.get_chapter(book_id, 1)
        self.assertEqual(chapter["manuscripts"][0]["kind"], manuscripts.KIND_LEGACY)
        self.assertEqual(chapter["manuscripts"][0]["label"], "升级前保存的正文")
        self.assertEqual(chapter["content"], "用户改过的正文。", "升级不能丢正文")

    def test_upgrade_does_not_forge_original_when_text_differs(self):
        """正文被改过：即使源文件在，也不能把改编内容伪称原文。"""
        book_id = self._make_legacy_book(keep_import=True, edited="用户改过的正文。")
        book = self.store.get_book(book_id)["book"]
        self.assertEqual(book["origin_recovery"]["restored"], [])
        self.assertEqual(book["origin_recovery"]["legacy"], [1, 2])
        chapter = self.store.get_chapter(book_id, 1)
        self.assertEqual(chapter["content"], "用户改过的正文。")

    def test_upgrade_keeps_existing_versions(self):
        book_id = self._make_legacy_book(keep_import=True)
        # 先写一个成果，再退回 schema 1 验证升级不丢它。
        self.store.apply_outline(book_id, task_id="t-1", text="旧大纲")
        book_dir = self.root / "studio" / "novel" / book_id
        manifest = json.loads((book_dir / "book.json").read_text(encoding="utf-8"))
        manifest["schema_version"] = 1
        (book_dir / "book.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        book = self.store.get_book(book_id)["book"]
        self.assertEqual(book["outline_versions"][0]["text"], "旧大纲")

    def test_upgrade_is_idempotent(self):
        book_id = self._make_legacy_book(keep_import=False)
        first = self.store.get_book(book_id)["book"]
        ids_first = [row["id"] for row in self.store.get_chapter(book_id, 1)["manuscripts"]]
        second = self.store.get_book(book_id)["book"]
        ids_second = [row["id"] for row in self.store.get_chapter(book_id, 1)["manuscripts"]]
        self.assertEqual(first["schema_version"], second["schema_version"])
        self.assertEqual(ids_first, ids_second, "重复读取不能重复建稿")

    def test_upgrade_retries_after_failure(self):
        """升级中途失败时清单仍是 schema 1，重跑能完成。"""
        book_id = self._make_legacy_book(keep_import=False)
        book_dir = self.root / "studio" / "novel" / book_id
        # 模拟残留计划文件（上次崩溃留下的），重跑应复用并完成。
        from naiba.novel import migration
        manifest = json.loads((book_dir / "book.json").read_text(encoding="utf-8"))
        plan = migration.build_plan(book_dir, manifest, self.root / "studio" / "novel" / "_imports")
        (book_dir / migration.UPGRADE_PLAN).write_text(json.dumps(plan, ensure_ascii=False),
                                                       encoding="utf-8")
        book = self.store.get_book(book_id)["book"]
        self.assertEqual(book["schema_version"], 2)
        self.assertFalse((book_dir / migration.UPGRADE_PLAN).exists(), "完成后要清掉计划文件")

    def test_broken_chapter_blocks_upgrade(self):
        book_id = self._make_legacy_book(keep_import=False)
        book_dir = self.root / "studio" / "novel" / book_id
        (book_dir / "chapters" / "0001.json").write_text("坏掉的内容", encoding="utf-8")
        with self.assertRaises(NovelServiceError):
            self.store.get_book(book_id)
        # 清单没被改动，旧数据仍可抢救。
        manifest = json.loads((book_dir / "book.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)


class BudgetTests(unittest.TestCase):
    def test_body_limit_is_clamped(self):
        self.assertEqual(prompts.clamp_body_limit(1000), prompts.BODY_SLOT_MIN)
        self.assertEqual(prompts.clamp_body_limit(99999), prompts.BODY_SLOT_MAX)
        self.assertEqual(prompts.clamp_body_limit("abc"), prompts.BODY_SLOT_CHARS)
        self.assertEqual(prompts.clamp_body_limit(6000), 6000)

    def test_chapter_limit_allows_more_than_six(self):
        self.assertEqual(prompts.clamp_chapter_limit(7), 7)
        self.assertEqual(prompts.clamp_chapter_limit(21), prompts.MAX_CHAPTERS_MAX)
        self.assertEqual(prompts.clamp_chapter_limit(0), prompts.MAX_CHAPTERS_MIN)

    def test_body_limit_reaches_assembly(self):
        chapters = [{"i": 1, "title": "第一章", "content": "甲" * 8000}]
        small = prompts.build_stage_content(
            stage="elements", book_title="书", chapters=chapters, chapter_ids=[1],
            summaries={}, body_limit=3000,
        )
        large = prompts.build_stage_content(
            stage="elements", book_title="书", chapters=chapters, chapter_ids=[1],
            summaries={}, body_limit=8000,
        )
        self.assertEqual(small["chapters"][0]["used_chars"], 3000)
        self.assertEqual(large["chapters"][0]["used_chars"], 8000)
        self.assertLess(len(small["content"]), len(large["content"]),
                        "滑条值必须真的改变发送内容")

    def test_budget_report_flags_known_overflow(self):
        chapters = [{"i": 1, "title": "第一章", "content": "甲" * 15000,
                     "total_chars": 15000, "used_chars": 15000, "dropped_chars": 0}]
        report = prompts.budget_report(
            content="甲" * 15000, chapters=chapters, warnings=[],
            body_limit=15000, max_chapters=6, context_window=4000,
        )
        self.assertTrue(report["context_known"])
        self.assertFalse(report["fits"])
        self.assertTrue(report["suggestions"])

    def test_budget_report_marks_unknown_context(self):
        report = prompts.budget_report(
            content="短文", chapters=[], warnings=[], body_limit=3000, max_chapters=6,
            context_window=0,
        )
        self.assertFalse(report["context_known"])
        self.assertIsNone(report["fits"], "容量未知时不能伪造能否容纳")
        self.assertIn("估算", report["estimate_note"])

    def test_budget_report_explains_dropped_chars(self):
        chapters = [{"i": 1, "title": "第一章", "content": "甲" * 9000,
                     "total_chars": 9000, "used_chars": 3000, "dropped_chars": 6000}]
        report = prompts.budget_report(
            content="甲" * 3000, chapters=chapters, warnings=[], body_limit=3000, max_chapters=6,
        )
        self.assertEqual(report["dropped_chars"], 6000)
        self.assertTrue(any("省略" in row for row in report["suggestions"]))

    def test_chapter_limit_reaches_assembly(self):
        chapters = [{"i": n, "title": f"第{n}章", "content": "文"} for n in range(1, 8)]
        with self.assertRaises(prompts.PromptError):
            prompts.build_stage_content(
                stage="elements", book_title="书", chapters=chapters,
                chapter_ids=list(range(1, 8)), summaries={}, max_chapters=6,
            )
        built = prompts.build_stage_content(
            stage="elements", book_title="书", chapters=chapters,
            chapter_ids=list(range(1, 8)), summaries={}, max_chapters=7,
        )
        self.assertIn("第 7 章", built["content"])


class SplitStageBodyLimitUITests(unittest.TestCase):
    """拆章阶段的 AI 润色也要能调「正文上限」（body_chars 就是它的截取上限）。

    契约：润色面板里有一份同状态的滑条；阶段面板里的那份在拆章是整块隐藏的。
    实现上**不能**把阶段面板的滑条节点搬进润色面板——阶段面板是按阶段克隆的
    （`activateStagePanel` 每轮重挂 id），搬节点会让两个面板各留一份、把剧本场次区
    挤成 0 高（实测点不到场次）。
    """

    def _read(self, rel: str) -> str:
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_ai_polish_panel_carries_its_own_body_limit_slider(self):
        index = self._read("public/index.html")
        ai_block = index[index.index('id="novelAiEdit"'):]
        ai_block = ai_block[: ai_block.index('id="novelStagePanel"')]
        self.assertIn('id="novelAiLimits"', ai_block, "润色面板要有自己的正文上限滑条")
        self.assertIn('id="novelAiBodyChars"', ai_block)
        self.assertIn("本次正文上限", ai_block, "文案要说清这是这一次润色的上限")

    def test_both_sliders_share_one_state_and_handler(self):
        js = self._read("public/js/25-novel.js")
        self.assertIn('event?.target?.id === "novelAiBodyChars"', js, "两份滑条要写同一份 novel.bodyChars")
        self.assertIn('$("#novelAiBodyChars")?.addEventListener("input", onLimits)', js)
        self.assertIn('const aiBody = $("#novelAiBodyChars")', js, "renderLimits 要同步润色滑条的取值")
        self.assertNotIn("function placeLimits()", js, "不要再搬移阶段面板的滑条节点（面板是克隆的）")
        self.assertIn("renderLimits();", js.split("function renderEditor(")[1].split("if (stageMode)")[0],
                      "拆章分支也要同步滑条")

    def test_ai_polish_request_carries_body_limit(self):
        js = self._read("public/js/25-novel.js")
        submit = js[js.index("async function submitAiAdapt("):]
        submit = submit[: submit.index("\n}")] if "\n}" in submit else submit
        self.assertIn("body_chars: novel.bodyChars", submit, "润色请求必须带上滑条值")


class ResultVisibilityUITests(unittest.TestCase):
    """「生成结果在哪看」：AI 润色结果就地显示；管理记录点一行就地看内容。

    两处都是「内容其实已经在内存/接口里，但界面上没有出口」的缺口：
    - 润色结果此前只能先保存成改编稿、再点「编辑」才看得到；
    - 管理记录的行只能勾选删除，看不到稿件正文/版本内容/任务结果。
    """

    def _read(self, rel: str) -> str:
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_ai_polish_result_is_rendered_in_the_panel(self):
        index = self._read("public/index.html")
        ai_block = index[index.index('id="novelAiEdit"'):]
        ai_block = ai_block[: ai_block.index('id="novelStagePanel"')]
        self.assertIn('id="novelAiResult"', ai_block, "润色面板要有结果区")
        self.assertIn('id="novelAiNote"', ai_block)
        js = self._read("public/js/25-novel.js")
        editor = js[js.index("function renderAiEditor("):]
        editor = editor[: editor.index("\nfunction renderEditor(")]
        self.assertIn('const draft = String(slot.streamText || task?.result || "")', editor)
        self.assertIn('result.textContent = draft', editor, "结果要写进面板里的结果区")
        self.assertIn('result.hidden = !draft.trim()', editor)
        # 以前把说明写进「手动编辑」面板的 note 里，润色面板自己什么都没有。
        self.assertNotIn("novelManuscriptEditNoteBody", editor)

    def test_manage_records_open_content_in_place(self):
        index = self._read("public/index.html")
        dialog = index[index.index('id="novelManageDialog"'):]
        self.assertIn('id="novelManagePreview"', dialog, "管理记录要有查看区")
        self.assertIn('id="novelManagePreviewBody"', dialog)
        js = self._read("public/js/25-novel.js")
        self.assertIn('copy.dataset.novelManageOpen = key', js, "整行要可点开查看")
        self.assertIn('$("#novelManageList")?.addEventListener("click"', js)
        self.assertIn("async function openManageRecord(row)", js)
        # 三类记录各走各的读法：稿件读正文、版本读作品里的对应版本、任务读结果。
        self.assertIn("await ensureManuscriptBody(row)", js)
        self.assertIn("function manageVersionText(row)", js)
        self.assertIn('row.result || row.result_partial', js)


class CreativeFieldLayoutUITests(unittest.TestCase):
    """创作场排版：页面头承载全局操作，五阶段横条 + 三栏工作区，主操作区固定在分组底部。

    这批断言钉的是「结构与落点」，不是像素：按钮搬家后 id 不变（事件绑定按 id），
    三栏比例与主操作区的粘底行为写进 CSS，避免以后改样式时又把主按钮挤到看不见。
    """

    def _read(self, rel: str) -> str:
        return (ROOT / rel).read_text(encoding="utf-8")

    def _page(self) -> str:
        index = self._read("public/index.html")
        return index[index.index('id="studioPageNovel"'):]

    def test_page_head_holds_global_actions_with_same_ids(self):
        page = self._page()
        head = page[page.index('class="novel-page-head"'):]
        head = head[: head.index('class="novel-stagebar"')]
        self.assertNotIn("novel-breadcrumb", head, "上方已有工作台标识、下方有标题，中间不再放面包屑")
        self.assertIn('id="novelPageState"', head, "页面头要显示当前作品与阶段")
        for ident in ("novelManageBtn", "novelImportBtn", "novelBooksBtn",
                      "novelExportBtn", "novelExportMenu"):
            self.assertIn(f'id="{ident}"', head, f"{ident} 应搬到页面头")
        # 导出菜单跟按钮一起进 .novel-page-actions，菜单才有正确的定位基准。
        actions = head[head.index('class="novel-page-actions"'):]
        self.assertIn('id="novelExportMenu"', actions)
        self.assertNotIn('id="novelImportBtn"', page[page.index('class="novel-toolbar"'):][:400],
                         "左栏工具栏只是保留容器，按钮已经搬走")

    def test_columns_and_stage_bar_follow_the_new_grid(self):
        css = self._read("public/styles.css")
        head = css[css.index(".novel-page-head {"):]
        head = head[: head.index("}")]
        self.assertIn("min-height: 56px", head, "页面头只有标题与操作一行，不再留面包屑的高度")
        actions = css[css.index(".novel-page-actions {"):]
        actions = actions[: actions.index("}")]
        self.assertIn("position: relative", actions, "导出菜单以页面头操作区为定位基准")
        columns = css[css.index(".novel-columns {"):]
        columns = columns[: columns.index("}")]
        for piece in ("248px minmax(0, 1fr) 280px", "gap: 16px", "padding: 20px"):
            self.assertIn(piece, columns, "桌面三栏比例与间距")
        stage = css[css.index(".novel-stage {"):]
        stage = stage[: stage.index("}")]
        self.assertIn("height: 32px", stage)
        active = css[css.index(".novel-stage.active {"):]
        active = active[: active.index("}")]
        self.assertIn("background: var(--accent-solid)", active, "当前阶段用主色实底，换肤跟着走")
        self.assertIn("color: #fff", active)
        self.assertIn(".novel-toolbar { display: none; }", css, "左栏工具栏保留但不占位")

    def test_primary_action_bar_sticks_to_the_editor_bottom(self):
        css = self._read("public/styles.css")
        bar = css[css.index(".novel-action-bar, .novel-stage-actions {"):]
        bar = bar[: bar.index("}")]
        self.assertIn("position: sticky", bar)
        self.assertIn("bottom: 0", bar)
        self.assertIn("background: var(--accent-soft)", bar, "主操作区用弱强调底色，不写死颜色")
        primary = css[css.index("\n.novel-primary {"):]
        primary = primary[: primary.index("}")]
        self.assertIn("background: var(--accent-solid)", primary)
        self.assertIn("min-height: 40px", primary)
        page = self._page()
        panel = page[page.index('class="novel-stage-panel"'):]
        generate = panel[: panel.index('id="novelStageWarning"')]
        self.assertIn('id="novelStageGenerate"', generate)
        self.assertIn("novel-primary", generate, "生成按钮是各阶段唯一的主按钮")
        self.assertIn("novel-action-bar", generate)

    def test_storyboard_results_sit_after_the_primary_action(self):
        """分镜结果与「发送到造梦机」放在主操作区之后：先点生成，再在下面逐场勾选。"""
        page = self._page()
        panel = page[page.index('class="novel-stage-panel"'):]
        panel = panel[: panel.index('id="novelStageTask"')]
        generate_at = panel.index('id="novelStageGenerate"')
        note_at = panel.index("novelStoryboardNote")
        send_at = panel.index("novelStoryboardSend")
        self.assertLess(generate_at, note_at)
        self.assertLess(note_at, send_at)
        self.assertEqual(panel.count("novelStoryboardNote"), 1, "结果说明只出现一次")

    def test_page_state_tracks_book_and_stage(self):
        js = self._read("public/js/25-novel.js")
        plan = js[js.index("function renderPlan("):]
        plan = plan[: plan.index("const blocks = [];")]
        self.assertIn('$("#novelPageState")', plan)
        self.assertIn("stageLabel(novel.stage)", plan)

    def test_result_and_saved_blocks_collapse_repeatably(self):
        """生成结果与「已保存版本」要能反复收起/展开，且不能被轮询重新撑开。

        坑：这三块共用按阶段克隆的面板，折叠开关不能绑单个面板节点（切阶段后 id 会搬家），
        所以用 class + 事件委托，状态存在 novel.collapsed 里。
        """
        index = self._read("public/index.html")
        block = index[index.index('id="novelStageTask"'):]
        block = block[: block.index('id="novelSceneBlock"')]
        self.assertIn('data-novel-collapse="task"', block, "生成结果标题要是折叠开关")
        self.assertIn('data-novel-collapse="saved"', block, "已保存版本标题要是折叠开关")
        js = self._read("public/js/25-novel.js")
        self.assertIn("function applyStageCollapse()", js)
        self.assertIn("novel.collapsed", js)
        self.assertIn('$("#novelEditorBody")?.addEventListener("click"', js, "折叠开关要委托在稳定的编辑区容器上")
        self.assertIn('collapsed: new Set()', js)
        # 只有「提交新任务 / 刚保存新版本」才自动展开，轮询重画不动折叠态。
        self.assertIn('novel.collapsed.delete(`${stage}:task`)', js)
        self.assertIn('novel.collapsed.delete(`${stage}:saved`)', js)
        # 收起态必须退出「铺满剩余高度」，否则卡片里留一大块灰底空白。
        css = self._read("public/styles.css")
        self.assertIn(".novel-stage-task.is-collapsed, .novel-stage-saved.is-collapsed { flex: 0 0 auto; }", css)

    def test_long_result_scrolls_instead_of_overlapping_scene_block(self):
        """结果正文被压到比 min-height 还矮时会溢出任务区、压在场次区上（实测 60+px）。

        三处必须一起成立：阶段面板与任务区都不许被压缩到内容以下（flex-shrink: 0），
        结果区自己收住高度并内部滚动；超出部分交给外层编辑区滚动。
        """
        css = self._read("public/styles.css")
        panel = css[css.index(".novel-stage-panel {"):]
        panel = panel[: panel.index("}")]
        self.assertIn("flex: 1 0 auto", panel, "阶段面板不能被压缩到内容以下")
        task = css[css.index(".novel-stage-task, .novel-stage-saved {"):]
        task = task[: task.index("}")]
        self.assertIn("flex: 1 0 auto", task, "任务区不能被压缩到比结果框还矮")
        result = css[css.index(".novel-stage-result {"):]
        result = result[: result.index("}")]
        self.assertIn("max-height: 42vh", result, "结果区要自己收高度并在内部滚动")


if __name__ == "__main__":
    unittest.main()
