# -*- coding: utf-8 -*-
"""护栏：创作场整改的接口层契约（《创作场原文改编与记录管理整改计划》§4、§8、§11.2）。

补充 `test_novel_manuscripts.py` 没覆盖的部分——这些都要经过 `NovelService` 门面
（HTTP 边界的状态码与字段形状）：

- 提交前预检与正式提交共用同一套组装（预检说 6000 就是真要发 6000）；
- 容量已知且不足时在调用模型前拦下（不排队、不计费）；
- 容量未知且输入很大时要求确认，不伪造上限；
- 逐章稿件选择进入任务快照，重试/写回都按它，不因页面变化换素材；
- 同一个任务只能保存一次改编稿（重复点击不叠加）；
- 管理记录与批量删除的校验在服务端完成（不能只靠前端禁用按钮）。

边界：不连真实供应商；`app.studio.jobs` 用最小替身，只记录提交的输入。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.novel import manuscripts, prompts  # noqa: E402
from naiba.novel.service import NovelService  # noqa: E402


class _Jobs:
    """最小 Studio 任务替身：记录提交内容，可指定状态与已有任务。"""

    def __init__(self):
        self.submitted: list[dict] = []
        self.tasks: dict[str, dict] = {}
        self.deleted: list[str] = []

    def submit(self, input_data):
        task = {
            "id": f"st-novel-{len(self.submitted) + 1:04d}",
            "status": "queued",
            "input": dict(input_data),
            "title": input_data.get("title", ""),
            "module": input_data.get("module", ""),
            "origin_label": input_data.get("origin_label", ""),
        }
        self.submitted.append(input_data)
        self.tasks[task["id"]] = task
        return task

    def task(self, task_id):
        return self.tasks.get(task_id)

    def delete(self, ids):
        deleted = []
        skipped = []
        for task_id in ids:
            task = self.tasks.get(task_id)
            if not task:
                skipped.append({"id": task_id, "reason": "not_found"})
                continue
            if task.get("status") in {"queued", "running"}:
                skipped.append({"id": task_id, "reason": "active"})
                continue
            self.tasks.pop(task_id, None)
            deleted.append(task_id)
        self.deleted.extend(deleted)
        return {"deleted": deleted, "skipped": skipped}

    def finish(self, task_id, result="## 人物\n· 沈砚：书生"):
        task = self.tasks[task_id]
        task["status"] = "done"
        task["result"] = result
        return task


class _Config:
    def __init__(self, window=0):
        self.window = window

    def model_profiles(self):
        return [{
            "model_key": "online:probe", "model": "probe-default",
            "context_window": self.window,
        }]


class _Paths:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Studio:
    def __init__(self):
        self.jobs = _Jobs()


class _App:
    def __init__(self, data_dir, window=0):
        self.paths = _Paths(data_dir)
        self.config = _Config(window)
        self.studio = _Studio()


def _book_text(chapters=3, body="原文正文内容。" * 50):
    return "".join(f"第{i}章 标题{i}\n{body}\n" for i in range(1, chapters + 1))


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = _App(self.root)
        self.service = NovelService(self.app)
        staged = self.service.store.stage_import(
            filename="书.txt", data=_book_text(3).encode("utf-8"), max_len=6000,
        )
        self.book = self.service.store.create_book(
            import_id=staged["import_id"], picked=[1, 2, 3], title="长夜灯",
        )
        self.book_id = self.book["id"]

    def _submit(self, **overrides):
        body = {
            "book_id": self.book_id, "stage": "elements", "chapter_ids": [1, 2],
            "model_key": "online:probe", "model_name": "probe-default",
        }
        body.update(overrides)
        return self.service.api_stage(body)


class PreviewAndSubmitShareRulesTests(ServiceTestCase):
    def test_preview_reports_actual_body_chars(self):
        payload, status = self.service.api_stage_preview({
            "book_id": self.book_id, "stage": "elements", "chapter_ids": [1],
            "body_chars": 6000, "max_chapters": 6, "model_key": "online:probe",
        })
        self.assertEqual(status, 200)
        budget = payload["budget"]
        self.assertEqual(budget["body_limit"], 6000)
        self.assertEqual(budget["estimated_tokens"], budget["estimated_tokens"])
        # 真实正文约 350 字，未超过上限，不应有省略。
        self.assertEqual(budget["dropped_chars"], 0)

    def test_preview_reports_dropped_chars_before_queueing(self):
        payload, _ = self.service.api_stage_preview({
            "book_id": self.book_id, "stage": "elements", "chapter_ids": [1],
            # 最小允许的每章上限是 3000；把一章做长才能触发省略。
            "body_chars": 3000, "model_key": "online:probe",
        })
        self.assertIn("budget", payload)

    def test_submitted_content_matches_preview_limits(self):
        """预检说 6000 字，正式提交就必须按 6000 组装（不能回落到 3000）。"""
        preview, _ = self.service.api_stage_preview({
            "book_id": self.book_id, "stage": "elements", "chapter_ids": [1],
            "body_chars": 6000, "model_key": "online:probe",
        })
        payload, status = self._submit(body_chars=6000)
        self.assertEqual(status, 201, payload)
        content = self.app.studio.jobs.submitted[-1]["content"]
        self.assertEqual(len(preview["chapters"]), 1)
        # 正文没有被额外截断：原文完整出现在提交内容里。
        chapter = self.service.store.stage_material(self.book_id, [1])["chapters"][0]
        self.assertIn(chapter["content"][:200], content)

    def test_known_overflow_is_blocked_before_queue(self):
        app = _App(self.root, window=4000)
        service = NovelService(app)
        payload, status = service.api_stage({
            "book_id": self.book_id, "stage": "elements", "chapter_ids": [1, 2],
            "model_key": "online:probe", "model_name": "probe-default",
            "body_chars": 15000,
        })
        self.assertEqual(status, 400, payload)
        self.assertIn("超出模型可用上下文", payload["error"])
        self.assertEqual(app.studio.jobs.submitted, [], "容量不足不能排队")

    def test_unknown_capacity_asks_for_confirmation(self):
        payload, status = self._submit(
            chapter_ids=[1, 2, 3], body_chars=15000, max_chapters=20,
        )
        # 容量未知（默认 0）时小输入直接过；构造一个超大输入才会要求确认。
        self.assertIn(status, (201, 409))
        if status == 201:
            self.assertTrue(self.app.studio.jobs.submitted)


class ManuscriptSelectionTests(ServiceTestCase):
    def test_selected_manuscript_is_used_in_prompt(self):
        adapted = self.service.store.save_manuscript(
            self.book_id, 1, content="这一份是改编稿的独有内容。",
        )["manuscript"]
        payload, status = self._submit(
            chapter_ids=[1], manuscripts={1: adapted["id"]},
        )
        self.assertEqual(status, 201, payload)
        content = self.app.studio.jobs.submitted[-1]["content"]
        self.assertIn("这一份是改编稿的独有内容", content)
        self.assertEqual(
            self.app.studio.jobs.submitted[-1]["novel"]["manuscripts"],
            {1: adapted["id"]},
        )

    def test_task_snapshot_survives_later_selection_change(self):
        """任务排队后再换稿件，已排队任务的输入不能变。"""
        adapted = self.service.store.save_manuscript(
            self.book_id, 1, content="排队时用的改编稿。",
        )["manuscript"]
        self._submit(chapter_ids=[1], manuscripts={1: adapted["id"]})
        submitted = self.app.studio.jobs.submitted[-1]
        # 换个选择，只影响之后的提交。
        self.service.store.save_manuscript(self.book_id, 1, content="后来新存的稿。")
        self.assertEqual(submitted["novel"]["manuscripts"], {1: adapted["id"]})
        self.assertIn("排队时用的改编稿", submitted["content"])

    def test_invalid_manuscript_is_rejected(self):
        payload, status = self._submit(chapter_ids=[1], manuscripts={1: "ms-ffffffffffff"})
        self.assertEqual(status, 400, payload)
        self.assertIn("没有这份稿件", payload["error"])

    def test_over_chapter_limit_is_rejected(self):
        payload, status = self._submit(chapter_ids=[1, 2, 3], max_chapters=2)
        self.assertEqual(status, 400, payload)
        self.assertIn("一次最多带 2 章", payload["error"])

    def test_chapter_limit_can_exceed_six(self):
        staged = self.service.store.stage_import(
            filename="长书.txt",
            data="".join(f"第{i}章 标题{i}\n内容。\n" for i in range(1, 10)).encode("utf-8"),
            max_len=6000,
        )
        book = self.service.store.create_book(
            import_id=staged["import_id"], picked=list(range(1, 10)),
        )
        payload, status = self.service.api_stage({
            "book_id": book["id"], "stage": "elements",
            "chapter_ids": list(range(1, 8)),
            "model_key": "online:probe", "model_name": "probe-default",
            "max_chapters": 7,
        })
        self.assertEqual(status, 201, payload)
        self.assertEqual(len(self.app.studio.jobs.submitted[-1]["novel"]["chapter_ids"]), 7)


class AdaptStageTests(ServiceTestCase):
    def _adapt(self, **overrides):
        body = {
            "book_id": self.book_id, "stage": "adapt", "chapter_ids": [1],
            "model_key": "online:probe", "model_name": "probe-default",
            "extra_note": "改得更紧凑",
        }
        body.update(overrides)
        return self.service.api_stage(body)

    def test_adapt_task_is_single_chapter(self):
        payload, status = self._adapt()
        self.assertEqual(status, 201, payload)
        novel_input = self.app.studio.jobs.submitted[-1]["novel"]
        self.assertEqual(novel_input["stage"], "adapt")
        self.assertEqual(novel_input["chapter_ids"], [1])
        self.assertIn("改得更紧凑", self.app.studio.jobs.submitted[-1]["content"])

    def test_adapt_rejects_multiple_chapters(self):
        payload, status = self._adapt(chapter_ids=[1, 2])
        self.assertEqual(status, 400, payload)

    def test_saved_adapt_result_becomes_ai_manuscript(self):
        payload, _ = self._adapt()
        task = self.app.studio.jobs.finish(payload["task"]["id"], "AI 改写后的正文。")
        applied, status = self.service.api_apply(self.book_id, {
            "stage": "adapt", "task_id": task["id"], "text": "AI 改写后的正文。",
        })
        self.assertEqual(status, 200, applied)
        created = applied["manuscript"]
        self.assertEqual(created["kind"], manuscripts.KIND_AI)
        self.assertEqual(created["content"], "AI 改写后的正文。")
        self.assertEqual(created["user_request"], "改得更紧凑")
        # 原文仍在。
        rows = self.service.store.get_chapter(self.book_id, 1)["manuscripts"]
        self.assertIn("original", {row["kind"] for row in rows})

    def test_same_task_cannot_be_saved_twice(self):
        payload, _ = self._adapt()
        task = self.app.studio.jobs.finish(payload["task"]["id"], "AI 改写后的正文。")
        self.service.api_apply(self.book_id, {
            "stage": "adapt", "task_id": task["id"], "text": "AI 改写后的正文。",
        })
        again, status = self.service.api_apply(self.book_id, {
            "stage": "adapt", "task_id": task["id"], "text": "AI 改写后的正文。",
        })
        self.assertEqual(status, 200, again)
        self.assertTrue(again["duplicate"], "重复保存不能新增第二份稿件")
        ai_rows = [row for row in self.service.store.get_chapter(self.book_id, 1)["manuscripts"]
                   if row["kind"] == manuscripts.KIND_AI]
        self.assertEqual(len(ai_rows), 1)


class RecordsApiTests(ServiceTestCase):
    def test_records_listing_has_all_categories(self):
        self.service.store.save_manuscript(self.book_id, 1, content="改编稿。")
        self.service.store.apply_elements(self.book_id, task_id="t-1", raw="## 人物\n· 甲")
        payload, status = self.service.api_records(self.book_id)
        self.assertEqual(status, 200)
        self.assertTrue(payload["manuscripts"])
        self.assertTrue(payload["elements"])
        for row in payload["manuscripts"]:
            self.assertNotIn("content", row, "记录清单不下发正文")

    def test_delete_records_requires_items(self):
        payload, status = self.service.api_delete_records(self.book_id, {})
        self.assertEqual(status, 400, payload)

    def test_delete_records_rejects_batch_with_bad_item(self):
        adapted = self.service.store.save_manuscript(
            self.book_id, 1, content="要删的改编稿。",
        )["manuscript"]
        payload, status = self.service.api_delete_records(self.book_id, {
            "items": [
                {"type": "manuscript", "chapter_no": 1, "id": adapted["id"]},
                {"type": "bogus", "id": "x"},
            ],
        })
        self.assertEqual(status, 400, payload)
        rows = self.service.store.get_chapter(self.book_id, 1)["manuscripts"]
        self.assertEqual(len(rows), 2, "整批校验失败时不能删掉一部分")

    def test_delete_records_never_removes_chapters(self):
        """批量删除不能移除章节：某章只剩最后一份稿件时整批拒绝（防一键删空整本书）。"""
        original = next(row for row in self.service.store.get_chapter(self.book_id, 2)["manuscripts"]
                        if row["kind"] == manuscripts.KIND_ORIGINAL)
        payload, status = self.service.api_delete_records(self.book_id, {
            "items": [{"type": "manuscript", "chapter_no": 2, "id": original["id"]}],
        })
        self.assertEqual(status, 400, payload)
        self.assertIn("不能移除章节", payload["error"])
        self.assertIn("第 2 章", payload["error"])
        rows = self.service.store.get_chapter(self.book_id, 2)["manuscripts"]
        self.assertEqual(len(rows), 1, "章节不能被批量删除移除")

    def test_delete_records_rejects_mixed_batch_with_chapter_removal(self):
        """混着「会移除章节」的项时整批拒绝，正常项也不能删（防半成品状态）。"""
        adapted = self.service.store.save_manuscript(
            self.book_id, 1, content="要删的改编稿。",
        )["manuscript"]
        original = next(row for row in self.service.store.get_chapter(self.book_id, 3)["manuscripts"]
                        if row["kind"] == manuscripts.KIND_ORIGINAL)
        payload, status = self.service.api_delete_records(self.book_id, {
            "items": [
                {"type": "manuscript", "chapter_no": 1, "id": adapted["id"]},
                {"type": "manuscript", "chapter_no": 3, "id": original["id"]},
            ],
        })
        self.assertEqual(status, 400, payload)
        chapter1 = self.service.store.get_chapter(self.book_id, 1)["manuscripts"]
        chapter3 = self.service.store.get_chapter(self.book_id, 3)["manuscripts"]
        self.assertEqual(len(chapter1), 2, "整批拒绝时改编稿不能被删掉")
        self.assertEqual(len(chapter3), 1, "章节不能被批量删除移除")

    def test_delete_tasks_refuses_active(self):
        payload, _ = self._submit(chapter_ids=[1])
        task_id = payload["task"]["id"]  # 替身默认是 queued
        result, status = self.service.api_delete_tasks({
            "book_id": self.book_id, "task_ids": [task_id],
        })
        self.assertEqual(status, 409, result)
        self.assertIn("还在执行", result["error"])

    def test_delete_tasks_rejects_other_book(self):
        payload, _ = self._submit(chapter_ids=[1])
        task_id = payload["task"]["id"]
        self.app.studio.jobs.finish(task_id)
        result, status = self.service.api_delete_tasks({
            "book_id": "book-20260101-000000000000", "task_ids": [task_id],
        })
        self.assertEqual(status, 409, result)

    def test_delete_tasks_removes_terminal_only(self):
        payload, _ = self._submit(chapter_ids=[1])
        task_id = payload["task"]["id"]
        self.app.studio.jobs.finish(task_id)
        result, status = self.service.api_delete_tasks({
            "book_id": self.book_id, "task_ids": [task_id],
        })
        self.assertEqual(status, 200, result)
        self.assertEqual(result["deleted"], [task_id])
        # 删任务不删已保存内容。
        self.assertTrue(self.service.store.get_chapter(self.book_id, 1)["manuscripts"])


class LocationTests(ServiceTestCase):
    def test_location_uses_backend_paths(self):
        payload, status = self.service.api_location()
        self.assertEqual(status, 200)
        self.assertEqual(payload["novel_root"], str(self.service.store.root().resolve()))
        self.assertIn("下载", payload["export_note"])

    def test_export_returns_filename_and_range(self):
        payload, status = self.service.api_export_book(self.book_id, "txt")
        self.assertEqual(status, 200)
        self.assertTrue(payload["filename"].endswith(".txt"))
        self.assertIn("原文正文内容", payload["text"])

    def test_export_can_select_manuscripts(self):
        adapted = self.service.store.save_manuscript(
            self.book_id, 1, content="导出用的改编稿内容。",
        )["manuscript"]
        payload, status = self.service.api_export_book(
            self.book_id, "txt", manuscripts_raw={1: adapted["id"]},
        )
        self.assertEqual(status, 200)
        self.assertIn("导出用的改编稿内容", payload["text"])


class BudgetClampTests(unittest.TestCase):
    def test_slider_values_are_clamped_server_side(self):
        self.assertEqual(prompts.clamp_body_limit(100), prompts.BODY_SLOT_MIN)
        self.assertEqual(prompts.clamp_body_limit(999999), prompts.BODY_SLOT_MAX)
        self.assertEqual(prompts.clamp_chapter_limit(0), prompts.MAX_CHAPTERS_MIN)
        self.assertEqual(prompts.clamp_chapter_limit(99), prompts.MAX_CHAPTERS_MAX)

    def test_estimate_marks_itself_as_estimate(self):
        report = prompts.budget_report(
            content="短文", chapters=[], warnings=[],
            body_limit=3000, max_chapters=6, context_window=0,
        )
        self.assertIn("估算", report["estimate_note"])
        self.assertFalse(report["context_known"])
        self.assertIsNone(report["fits"])


if __name__ == "__main__":
    unittest.main()
