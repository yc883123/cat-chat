# -*- coding: utf-8 -*-
"""护栏：织书台第三次反馈（2026-10-31，7 条）的服务端部分。

覆盖：
- 拆章自动分批（第三次反馈⑦）：want_count 按 SPLIT_STEP_CHAPTERS 切成多步连拆，
  每步只拆自己那一批、带上已拆前文衔接，批结果按步解析写回；
- 分部大纲写回保留空部（第三次反馈②）：生成一部不再吞掉其他部目录，
  part_plan 里的部标题也要铺进目录骨架；
- 多部统一复核梳理（第三次反馈④）：新 kind outline_review 的校验、
  提示词（本部全文 + 其余各部前后节选）与逐部写回；
- 单章字数新档 8000/9000（第三次反馈⑤：阅读平台常见单章 4000–9000 字）。

边界：只碰临时目录与假 Jobs，不连供应商、不起服务。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.writer import prompts  # noqa: E402
from naiba.writer.pipeline import WriterPipelineError, WriterPipelineManager  # noqa: E402
from naiba.writer.store import WriterStore, WriterStoreError  # noqa: E402


class _Jobs:
    """最小 Studio 任务替身：记录提交内容，测试用例手工驱动任务状态。"""

    def __init__(self):
        self.submitted: list[dict] = []
        self.tasks: dict[str, dict] = {}

    def submit(self, input_data):
        task = {
            "id": f"st-{len(self.submitted) + 1:04d}",
            "status": "queued",
            "input": json.loads(json.dumps(input_data, ensure_ascii=False)),
            "title": input_data.get("title", ""),
            "result": "",
            "result_partial": "",
            "error": "",
            "finish_reason": "",
        }
        self.submitted.append(task["input"])
        self.tasks[task["id"]] = task
        return task

    def task(self, task_id):
        return self.tasks.get(task_id)

    def cancel(self, task_id):
        task = self.tasks.get(task_id)
        if task is not None and task["status"] in ("queued", "running"):
            task["status"] = "cancelled"
            return True
        return False

    def finish(self, task_id, result, *, status="done", finish_reason="", error=""):
        task = self.tasks[task_id]
        task["status"] = status
        task["result"] = result
        task["finish_reason"] = finish_reason
        task["error"] = error
        return task


class _Config:
    def model_profiles(self):
        return [{"model_key": "online:probe", "model": "probe",
                 "context_window": 100000}]


class _Paths:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _App:
    def __init__(self, data_dir):
        self.paths = _Paths(data_dir)
        self.config = _Config()
        self.studio = type("S", (), {"jobs": None})()
        self.studio.jobs = _Jobs()


class Round3Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = WriterStore(self.root)
        self.app = _App(self.root)
        self.manager = WriterPipelineManager(self.app, self.store)
        self.book = self.store.create_book(title="第三次反馈测")
        self.book_id = self.book["id"]
        self.store.save_setup(self.book_id, content={"idea": "一句话创意"}, kind="manual")

    def current_pipeline(self):
        return self.store.get_active_pipeline(self.book_id)

    def last_task(self):
        return self.app.studio.jobs.tasks[list(self.app.studio.jobs.tasks)[-1]]

    def tick(self):
        self.manager.tick()

    def split_rows(self, start: int, count: int) -> str:
        return json.dumps({"chapters": [
            {"no": no, "title": f"第{no}章 测试", "beat": "梗概", "hook": "钩子"}
            for no in range(start, start + count)
        ]}, ensure_ascii=False)


class SplitAutoBatchTests(Round3Case):
    """第三次反馈⑦：拆 180 章不再一把喂给模型，按批连拆。"""

    def setUp(self):
        super().setUp()
        self.store.save_outline(self.book_id, parts=[
            {"no": 1, "title": "第 1 部", "text": "第一部的完整大纲"},
        ])

    def test_want_count_slices_into_batch_steps(self):
        pipeline = self.manager.start_pipeline(
            self.book_id, kind="split", part_no=1, want_count=45,
            model_key="online:probe", model_name="probe")
        self.assertEqual(pipeline["steps"], ["split"] * 3)
        self.assertEqual(int(pipeline["request_snapshot"]["want_count"]), 45)

    def test_small_batch_stays_single_step(self):
        pipeline = self.manager.start_pipeline(
            self.book_id, kind="split", part_no=1, want_count=15,
            model_key="online:probe", model_name="probe")
        self.assertEqual(pipeline["steps"], ["split"])
        self.assertIn("第 1 章起 15 章", self.last_task()["title"])

    def test_999_chapters_yield_fifty_steps(self):
        pipeline = self.manager.start_pipeline(
            self.book_id, kind="split", part_no=1, want_count=prompts.SPLIT_BATCH_MAX,
            model_key="online:probe", model_name="probe")
        self.assertEqual(len(pipeline["steps"]), 50)

    def test_each_step_covers_its_own_slice(self):
        self.manager.start_pipeline(
            self.book_id, kind="split", part_no=1, want_count=45,
            model_key="online:probe", model_name="probe")
        first = self.last_task()
        self.assertIn("从第 1 章开始，拆 20 章", first["input"]["content"])
        self.assertIn("第 1/3 批", first["title"])
        self.app.studio.jobs.finish(first["id"], self.split_rows(1, 20))
        self.tick()
        second = self.last_task()
        self.assertIn("从第 21 章开始，拆 20 章", second["input"]["content"])
        self.assertIn("第 2/3 批", second["title"])
        # 接续衔接：带上第 13–20 章（最近 8 章），不重讲、不跳跃。
        self.assertIn("第20章 测试：梗概", second["input"]["content"])
        self.app.studio.jobs.finish(second["id"], self.split_rows(21, 20))
        self.tick()
        third = self.last_task()
        self.assertIn("从第 41 章开始，拆 5 章", third["input"]["content"])
        self.assertIn("第 3/3 批", third["title"])
        self.app.studio.jobs.finish(third["id"], self.split_rows(41, 5))
        self.tick()
        self.assertEqual(str(self.current_pipeline()["status"]), "done")
        chapters = self.store.get_book(self.book_id)["chapters"]
        self.assertEqual([row["no"] for row in chapters], list(range(1, 46)))

    def test_batch_result_over_cap_is_trimmed_per_step(self):
        self.manager.start_pipeline(
            self.book_id, kind="split", part_no=1, want_count=45,
            model_key="online:probe", model_name="probe")
        first = self.last_task()
        self.app.studio.jobs.finish(first["id"], self.split_rows(1, 25))
        self.tick()
        pipeline = self.current_pipeline()
        self.assertEqual(str(pipeline["status"]), "running")
        self.assertEqual(int(pipeline["step_index"]), 1)
        chapters = self.store.get_book(self.book_id)["chapters"]
        self.assertEqual([row["no"] for row in chapters], list(range(1, 21)))

    def test_split_slice_math(self):
        snapshot = {"from_no": 31, "want_count": 45}
        self.assertEqual(self.manager._split_slice(snapshot, 0), (31, 20))
        self.assertEqual(self.manager._split_slice(snapshot, 1), (51, 20))
        self.assertEqual(self.manager._split_slice(snapshot, 2), (71, 5))


class OutlineWritebackKeepsPartsTests(Round3Case):
    """第三次反馈②：生成一部，其他部的目录（含空部与标题）必须原样保留。"""

    def test_single_part_generation_keeps_empty_parts(self):
        self.store.save_outline(self.book_id, parts=[
            {"no": 1, "title": "第一部 雾港", "text": "旧的第 1 部大纲"},
            {"no": 2, "title": "第二部 潮声", "text": ""},
            {"no": 3, "title": "第三部 灯塔", "text": ""},
        ])
        self.manager.start_pipeline(
            self.book_id, kind="outline_text", part_no=1, part_title="",
            model_key="online:probe", model_name="probe")
        self.app.studio.jobs.finish(self.last_task()["id"], "新的第 1 部大纲")
        self.tick()
        outline = self.store.load_outline(self.book_id)
        self.assertEqual([row["text"] for row in outline["parts"]],
                         ["新的第 1 部大纲", "", ""])
        self.assertEqual([row["title"] for row in outline["parts"]],
                         ["第一部 雾港", "第二部 潮声", "第三部 灯塔"])

    def test_outline_all_plan_titles_fill_empty_parts(self):
        self.manager.start_pipeline(
            self.book_id, kind="outline_all",
            part_plan=[{"title": "甲部"}, {"title": "乙部"}, {"title": "丙部"}],
            model_key="online:probe", model_name="probe")
        # 第 1 步刚写回、还没轮到后两步时，目录里就该有三部、后两部带计划标题。
        self.app.studio.jobs.finish(self.last_task()["id"], "第 1 部大纲")
        self.tick()
        pipeline = self.current_pipeline()
        self.assertEqual(str(pipeline["status"]), "running")
        self.assertEqual(int(pipeline["step_index"]), 1)
        outline = self.store.load_outline(self.book_id)
        self.assertEqual([row["title"] for row in outline["parts"]], ["甲部", "乙部", "丙部"])
        self.assertEqual([row["text"] for row in outline["parts"]],
                         ["第 1 部大纲", "", ""])

    def test_seven_part_catalog_survives_one_part_generation(self):
        """回归锁：7 部目录 + 只生成第 1 部 → 7 个标签一个不少（用户实测丢 6 部）。"""
        self.store.save_outline(self.book_id, parts=[
            {"no": no, "title": f"第 {no} 部", "text": ("有内容" if no == 1 else "")}
            for no in range(1, 8)
        ])
        self.manager.start_pipeline(
            self.book_id, kind="outline_text", part_no=1, part_title="",
            model_key="online:probe", model_name="probe")
        self.app.studio.jobs.finish(self.last_task()["id"], "重写的第 1 部")
        self.tick()
        outline = self.store.load_outline(self.book_id)
        self.assertEqual(len(outline["parts"]), 7)
        self.assertEqual(outline["parts"][0]["text"], "重写的第 1 部")
        self.assertEqual([row["text"] for row in outline["parts"][1:]], [""] * 6)


class OutlineReviewTests(Round3Case):
    """第三次反馈④：多部统一复核梳理（outline_review）。"""

    def setUp(self):
        super().setUp()
        self.store.save_outline(self.book_id, parts=[
            {"no": 1, "title": "第 1 部", "text": "第一部大纲：主角出海。结尾：船沉了。"},
            {"no": 2, "title": "第 2 部", "text": "第二部大纲：荒岛求生。"},
            {"no": 3, "title": "第 3 部", "text": "第三部大纲：重返大陆。"},
        ])

    def plan(self, nos=(1, 2, 3)):
        outline = self.store.load_outline(self.book_id)
        return [{"no": row["no"], "title": row["title"]}
                for row in outline["parts"] if int(row["no"]) in set(nos)]

    def test_requires_at_least_two_parts(self):
        with self.assertRaises(WriterPipelineError):
            self.manager.start_pipeline(
                self.book_id, kind="outline_review", part_plan=self.plan(nos=(1,)),
                model_key="online:probe", model_name="probe")

    def test_review_steps_and_prompt_carry_neighbors(self):
        pipeline = self.manager.start_pipeline(
            self.book_id, kind="outline_review", part_plan=self.plan(nos=(1, 2)),
            model_key="online:probe", model_name="probe")
        self.assertEqual(pipeline["steps"], ["outline_review", "outline_review"])
        content = self.last_task()["input"]["content"]
        self.assertIn("【本部（要复核梳理的就是这一部）】第 1 部", content)
        self.assertIn("第一部大纲：主角出海。结尾：船沉了。", content)
        self.assertIn("（在它之后，节选）", content)
        self.assertIn("第二部大纲：荒岛求生。", content)
        # 复核计划只覆盖 1、2 部，但第 3 部也是这本书的一部分：上下文照样带。
        self.assertIn("第三部大纲：重返大陆。", content)

    def test_review_writes_back_only_the_target_part(self):
        self.manager.start_pipeline(
            self.book_id, kind="outline_review", part_plan=self.plan(nos=(1, 2)),
            model_key="online:probe", model_name="probe")
        self.app.studio.jobs.finish(self.last_task()["id"], "修订后的第一部：主角出海，船沉前藏好罗盘。")
        self.tick()
        outline = self.store.load_outline(self.book_id)
        self.assertEqual(outline["parts"][0]["text"], "修订后的第一部：主角出海，船沉前藏好罗盘。")
        self.assertEqual(outline["parts"][1]["text"], "第二部大纲：荒岛求生。")
        self.assertEqual(outline["parts"][2]["text"], "第三部大纲：重返大陆。")

    def test_review_refuses_part_without_text(self):
        self.store.save_outline(self.book_id, parts=[
            {"no": 1, "title": "第 1 部", "text": "第一部大纲"},
            {"no": 2, "title": "第 2 部", "text": ""},
        ])
        # 复核顺序以部清单为准：把没内容的部排在第一步，排队时就如实拒绝。
        with self.assertRaises(WriterPipelineError):
            self.manager.start_pipeline(
                self.book_id, kind="outline_review",
                part_plan=[{"no": 2, "title": "第 2 部"}, {"no": 1, "title": "第 1 部"}],
                model_key="online:probe", model_name="probe")


class PromptAndLimitTests(unittest.TestCase):
    def test_review_prompt_sections(self):
        setup = {"idea": "一句话创意", "title": "书名"}
        content = prompts.build_outline_review_user(
            setup, part_no=2, part_title="乙部", part_text="本部全文",
            prev_parts=[{"no": 1, "title": "甲部", "text": "甲" * 1500}],
            next_parts=[{"no": 3, "title": "丙部", "text": "丙"}],
        )
        self.assertIn("【本部（要复核梳理的就是这一部）】第 2 部 · 乙部", content)
        self.assertIn("本部全文", content)
        self.assertIn("（在它之前，节选）", content)
        self.assertIn("（在它之后，节选）", content)
        # 节选帽：前部 1500 字只带 1200。
        self.assertNotIn("甲" * 1300, content)

    def test_split_step_chapters_constant(self):
        self.assertEqual(prompts.SPLIT_STEP_CHAPTERS, 20)
        self.assertLess(prompts.SPLIT_STEP_CHAPTERS, prompts.SPLIT_BATCH_MAX)

    def test_chapter_chars_new_tiers(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = WriterStore(Path(tmp.name))
        book = store.create_book(title="字数档")
        result = store.set_chapter_plan(book["id"], chars=9000)
        self.assertEqual(result["chapter_plan"]["chars"], 9000)
        with self.assertRaises(WriterStoreError):
            store.set_chapter_plan(book["id"], chars=7777)


if __name__ == "__main__":
    unittest.main()
