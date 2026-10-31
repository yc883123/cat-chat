# -*- coding: utf-8 -*-
"""护栏：织书台服务端流水线（实施计划 §7、§12.1）。

用假 Jobs（最小 Studio 任务替身）逐步证明：未写回不前进、解析失败停链、
取消后无下一步、旧 attempt 不可写、刷新无副作用、重试不跳步、
版本冲突不覆盖、分段正文走 chunked、重复写回幂等。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.writer import prompts  # noqa: E402
from naiba.writer.pipeline import WriterPipelineManager  # noqa: E402
from naiba.writer.store import WriterStore  # noqa: E402


class _Jobs:
    """最小 Studio 任务替身：记录提交内容，测试用例手工驱动任务状态。"""

    def __init__(self):
        self.submitted: list[dict] = []
        self.tasks: dict[str, dict] = {}
        self.cancelled: list[str] = []

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
        self.cancelled.append(task_id)
        task = self.tasks.get(task_id)
        if task is not None and task["status"] in ("queued", "running"):
            task["status"] = "cancelled"
            return True
        return False

    # ---- 测试驱动 ----

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


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = WriterStore(self.root)
        self.app = _App(self.root)
        self.manager = WriterPipelineManager(self.app, self.store)
        self.book = self.store.create_book(title="流水线测")
        self.book_id = self.book["id"]

    # ---- 驱动辅助 ----

    def start(self, **kwargs):
        defaults = dict(kind="setup_all", model_key="online:probe", model_name="probe",
                        char_count=2, chapter_count=5)
        defaults.update(kwargs)
        return self.manager.start_pipeline(self.book_id, **defaults)

    def current_pipeline(self):
        return self.store.get_active_pipeline(self.book_id)

    def last_task(self):
        return self.app.studio.jobs.tasks[self.app.studio.jobs.submitted and
                                          list(self.app.studio.jobs.tasks)[-1]]

    def tick(self):
        self.manager.tick()


class SetupChainTests(PipelineTestCase):
    def setUp(self):
        super().setUp()
        # 设定链的起点是已保存设定里的创意（空创意在排队前会被如实拒绝：
        # 「请先在『设定 → 故事创意』里写一句话…」）——这里按真实使用先存一版。
        self.store.save_setup(self.book_id, content={"idea": "一句话创意"}, kind="manual")

    def test_empty_idea_refused_before_queue(self):
        """空创意的设定链在排队前拒绝，不白烧 token 换占位结果（真实反馈的回归锁）。"""
        fresh = self.store.create_book(title="没有创意")
        with self.assertRaises(Exception) as ctx:
            self.manager.start_pipeline(
                fresh["id"], kind="setup_all", model_key="online:probe", model_name="probe")
        self.assertIn("创意", str(ctx.exception))
        self.assertIsNone(self.store.get_active_pipeline(fresh["id"]))

    def test_five_steps_advance_in_order_and_write_versions(self):
        """设定链五步化（第二次反馈）：设定只出设定，不再带大纲步、不凭空出章节。"""
        pipeline = self.start()
        self.assertEqual(pipeline["steps"], list(prompts.SETUP_STEPS))
        self.assertNotIn("outline", pipeline["steps"])
        results = {
            "genre": '{"channel":"male","cat":"玄幻","subCat":"东方","tone":"热血","hook":"卖点","tags":["a"]}',
            "title": '["书名甲","书名乙","书名丙"]',
            "blurb": '["简介甲","简介乙","简介丙"]',
            "world": "世界观正文",
            "chars": json.dumps([{"name": f"角色{i}", "role": "主角"} for i in range(1, 3)],
                                ensure_ascii=False),
        }
        for index, step in enumerate(prompts.SETUP_STEPS):
            record = self.current_pipeline()
            self.assertEqual(record["status"], "running")
            self.assertEqual(record["steps"][record["step_index"]], step)
            task_id = record["current_task_id"]
            task = self.app.studio.jobs.tasks[task_id]
            # 任务归属固化在 input.writer（book/pipeline/attempt/step）。
            writer = task["input"]["writer"]
            self.assertEqual(writer["book_id"], self.book_id)
            self.assertEqual(writer["pipeline_id"], record["id"])
            self.assertEqual(writer["attempt"], 1)
            self.assertEqual(writer["step"], step)
            self.assertEqual(task["input"]["module"], "writer")
            self.assertEqual(task["input"]["origin_label"], "织书台")
            self.app.studio.jobs.finish(task_id, results[step])
            self.tick()
        record = self.current_pipeline()
        self.assertEqual(record["status"], "done")
        # 每步都落了设定版本；设定链不再产生大纲版本与章节。
        detail = self.store.get_book(self.book_id)
        # 种子 1 版 + 设定五步各落一份设定版本。
        self.assertEqual(len(detail["book"]["setup_versions"]), 6)
        self.assertEqual(len(detail["book"]["outline_versions"]), 0)
        self.assertEqual(detail["chapters"], [])
        setup = detail["setup"]
        self.assertEqual(setup["genre"]["cat"], "玄幻")
        self.assertEqual(setup["title"], "书名甲")
        self.assertEqual(setup["worldview"], "世界观正文")
        self.assertEqual(len(setup["characters"]), 2)

    def test_chars_step_requests_leads_and_sides(self):
        """配角数量要进角色步的请求与校验：只报主角数会让配角一个都不出（第二次反馈）。"""
        pipeline = self.start(side_count=20)
        task_id = self.current_pipeline()["current_task_id"]
        # 依次喂完前四步，看角色步的任务内容。
        results = {
            "genre": '{"channel":"male","cat":"玄幻"}',
            "title": '["书名甲"]',
            "blurb": '["简介甲"]',
            "world": "世界观正文",
        }
        for step in ("genre", "title", "blurb", "world"):
            record = self.current_pipeline()
            task_id = record["current_task_id"]
            self.assertEqual(record["steps"][record["step_index"]], step)
            self.app.studio.jobs.finish(task_id, results[step])
            self.tick()
        record = self.current_pipeline()
        self.assertEqual(record["steps"][record["step_index"]], "chars")
        content = self.app.studio.jobs.tasks[record["current_task_id"]]["input"]["content"]
        self.assertIn("2 个主角和 20 个配角", content)
        self.assertIn("共 22 个", content)
        # 模型只回 2 个角色 → 不足 22 个，如实失败不落版本。
        self.app.studio.jobs.finish(
            record["current_task_id"],
            json.dumps([{"name": f"角色{i}", "role": "主角"} for i in range(1, 3)],
                       ensure_ascii=False))
        self.tick()
        record = self.current_pipeline()
        self.assertEqual(record["status"], "failed")
        self.assertIn("22", record["error"])
        # 回满 22 个 → 通过。
        self.manager.retry_pipeline(self.book_id)
        record = self.current_pipeline()
        rows = [{"name": f"主角{i}", "role": "主角"} for i in range(1, 3)]
        rows += [{"name": f"配角{i}", "role": "配角"} for i in range(1, 21)]
        self.app.studio.jobs.finish(
            record["current_task_id"], json.dumps(rows, ensure_ascii=False))
        self.tick()
        record = self.current_pipeline()
        self.assertEqual(record["status"], "done")
        setup = self.store.get_book(self.book_id)["setup"]
        self.assertEqual(len(setup["characters"]), 22)

    def test_queued_task_does_not_advance(self):
        self.start()
        record = self.current_pipeline()
        self.tick()
        self.assertEqual(self.current_pipeline()["step_index"], 0)
        self.assertEqual(self.current_pipeline()["current_task_id"], record["current_task_id"])

    def test_parse_failure_stops_chain_and_keeps_previous(self):
        pipeline = self.start()
        # 第 1 步成功。
        task_id = self.current_pipeline()["current_task_id"]
        self.app.studio.jobs.finish(task_id, '{"channel":"male","cat":"玄幻"}')
        self.tick()
        # 第 2 步返回解释文字 → 停链。
        task_id = self.current_pipeline()["current_task_id"]
        self.app.studio.jobs.finish(task_id, "抱歉，我无法输出 JSON。")
        self.tick()
        record = self.current_pipeline()
        self.assertEqual(record["status"], "failed")
        self.assertIn("JSON", record["error"])
        # 前序成果保留：种子版 + 第 1 步的设定版本都在。
        detail = self.store.get_book(self.book_id)
        self.assertEqual(len(detail["book"]["setup_versions"]), 2)
        self.assertEqual(detail["setup"]["genre"]["cat"], "玄幻")

    def test_non_done_task_stops_with_real_status(self):
        self.start()
        task_id = self.current_pipeline()["current_task_id"]
        self.app.studio.jobs.finish(task_id, "任意", status="failed", error="模型超时")
        self.tick()
        record = self.current_pipeline()
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"], "模型超时")

    def test_mismatched_task_cannot_write_back(self):
        self.start()
        record = self.current_pipeline()
        task_id = record["current_task_id"]
        # 伪造一个不属于本流水线的任务结果（pipeline_id 不同）。
        task = self.app.studio.jobs.tasks[task_id]
        task["input"]["writer"]["pipeline_id"] = "pl-deadbeefdead"
        task["status"] = "done"
        task["result"] = '{"channel":"male","cat":"玄幻"}'
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "failed")
        detail = self.store.get_book(self.book_id)
        # 只有种子版；被伪造的任务结果没有写回。
        self.assertEqual(len(detail["book"]["setup_versions"]), 1)

    def test_cancel_stops_chain_and_late_result_ignored(self):
        self.start()
        record = self.current_pipeline()
        task_id = record["current_task_id"]
        self.manager.cancel_pipeline(self.book_id)
        self.assertEqual(self.current_pipeline()["status"], "cancelled")
        self.assertIn(task_id, self.app.studio.jobs.cancelled)
        # 迟到结果：任务转 done，但流水线已取消 → 不写回、不提交下一步。
        self.app.studio.jobs.finish(task_id, '{"channel":"male","cat":"玄幻"}')
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "cancelled")
        self.assertEqual(len(self.store.get_book(self.book_id)["book"]["setup_versions"]), 1)
        self.assertEqual(len(self.app.studio.jobs.submitted), 1, "取消后不得提交下一步")

    def test_retry_resubmits_failed_step_only(self):
        self.start()
        first_task = self.current_pipeline()["current_task_id"]
        self.app.studio.jobs.finish(first_task, '{"channel":"male","cat":"玄幻"}')
        self.tick()  # 第 1 步完成 → 第 2 步
        second_task = self.current_pipeline()["current_task_id"]
        self.assertEqual(self.current_pipeline()["steps"][self.current_pipeline()["step_index"]],
                         "title")
        self.app.studio.jobs.finish(second_task, "垃圾输出", status="failed", error="坏结果")
        self.tick()
        record = self.current_pipeline()
        self.assertEqual(record["status"], "failed")
        submitted_before = len(self.app.studio.jobs.submitted)
        self.manager.retry_pipeline(self.book_id)
        record = self.current_pipeline()
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["attempt"], 2)
        # 重试的是失败步（title），不是跳到下一步。
        self.assertEqual(record["steps"][record["step_index"]], "title")
        self.assertEqual(len(self.app.studio.jobs.submitted), submitted_before + 1)
        # 旧 attempt 的任务结果不能推进新 attempt。
        self.app.studio.jobs.finish(second_task, '["书名"]')
        self.tick()
        self.assertEqual(self.current_pipeline()["step_index"], 1)
        self.assertEqual(len(self.store.get_book(self.book_id)["book"]["setup_versions"]), 2)

    def test_retry_requires_rebase_for_conflict(self):
        self.start()
        task_id = self.current_pipeline()["current_task_id"]
        self.app.studio.jobs.finish(task_id, '{"channel":"male","cat":"玄幻"}')
        self.tick()
        # 生成期间用户改了作品（revision 前进）→ 下一步写回时 conflict。
        self.store.rename_book(self.book_id, title="改名触发冲突")
        task_id = self.current_pipeline()["current_task_id"]
        self.app.studio.jobs.finish(task_id, '["书名甲"]')
        self.tick()
        record = self.current_pipeline()
        self.assertEqual(record["status"], "conflict")
        with self.assertRaises(Exception):
            self.manager.retry_pipeline(self.book_id)
        # rebase=True 显式基于当前版本重来。
        self.manager.retry_pipeline(self.book_id, rebase=True)
        record = self.current_pipeline()
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["steps"][record["step_index"]], "title")

    def test_recover_marks_interrupted_and_does_not_continue(self):
        self.start()
        task_id = self.current_pipeline()["current_task_id"]
        self.app.studio.jobs.finish(task_id, '{"channel":"male","cat":"玄幻"}')
        # 模拟重启：先推进一步，再 recover（recover 前 running）。
        self.tick()
        self.store.recover_pipelines()
        record = self.current_pipeline()
        self.assertEqual(record["status"], "interrupted")
        submitted = len(self.app.studio.jobs.submitted)
        self.tick()
        self.assertEqual(len(self.app.studio.jobs.submitted), submitted, "interrupted 不自动续跑")

    def test_get_pipeline_is_read_only(self):
        self.start()
        before = json.dumps(self.current_pipeline(), ensure_ascii=False, sort_keys=True)
        self.manager.get_pipeline(self.book_id)
        self.manager.get_pipeline(self.book_id)
        self.assertEqual(
            json.dumps(self.current_pipeline(), ensure_ascii=False, sort_keys=True), before)

    def test_second_pipeline_refused_while_running(self):
        self.start()
        with self.assertRaises(Exception):
            self.start()


class ChapterPipelineTests(PipelineTestCase):
    def _prepare_outline(self, chapters=3):
        self.store.save_setup(self.book_id, content={"idea": "一句话创意"}, kind="manual")
        rows = [{"no": i, "title": f"第{i}章", "beat": f"梗概{i}", "hook": "钩"} for i in range(1, chapters + 1)]
        self.store.save_outline(self.book_id, chapters=rows, kind="generated", sync=True)

    def test_whole_chapter_writes_generated_version(self):
        self._prepare_outline()
        pipeline = self.start(kind="chapter", chapter_no=2)
        record = self.current_pipeline()
        self.assertEqual(record["steps"], ["draft"])
        task_id = record["current_task_id"]
        task = self.app.studio.jobs.tasks[task_id]
        self.assertEqual(task["input"]["writer"]["chapter_no"], 2)
        self.assertEqual(task["input"]["writer"]["step"], "draft")
        self.assertIn("梗概2", task["input"]["content"])
        self.app.studio.jobs.finish(task_id, "第二章的完整正文。")
        self.tick()
        record = self.current_pipeline()
        self.assertEqual(record["status"], "done")
        chapter = self.store.get_chapter(self.book_id, 2)
        self.assertEqual(chapter["content"], "第二章的完整正文。")
        self.assertEqual(chapter["versions"][0]["kind"], "generated")
        self.assertEqual(chapter["versions"][0]["task_id"], task_id)
        # 来源版本固化在完整版本记录里（索引视图只带元数据）。
        record = self.store._read_json(
            self.store._chapter_version_path(
                self.store._book_dir(self.book_id), 2, chapter["versions"][0]["id"]),
            what="正文版本")
        outline = self.store.load_outline(self.book_id)
        self.assertEqual(record["source_outline_version_id"], outline["id"])
        self.assertTrue(record["source_setup_version_id"])

    def test_truncated_draft_rejected(self):
        self._prepare_outline()
        self.start(kind="chapter", chapter_no=1)
        task_id = self.current_pipeline()["current_task_id"]
        self.app.studio.jobs.finish(task_id, "写到一半", finish_reason="length")
        self.tick()
        record = self.current_pipeline()
        self.assertEqual(record["status"], "failed")
        self.assertIn("截断", record["error"])
        self.assertEqual(self.store.get_chapter(self.book_id, 1)["content"], "")

    def test_segmented_chapter_uses_chunked_blocks(self):
        self._prepare_outline()
        self.start(kind="chapter_segs", chapter_no=1)
        # 第 1 步 segs。
        record = self.current_pipeline()
        self.assertEqual(record["steps"], ["segs", "draft"])
        task_id = record["current_task_id"]
        self.assertEqual(self.app.studio.jobs.tasks[task_id]["input"]["writer"]["step"], "segs")
        self.app.studio.jobs.finish(task_id, '["第一段要点","第二段要点","第三段要点"]')
        self.tick()
        # 第 2 步 draft：单任务 chunked 多轮（块数 = 段落数）。
        record = self.current_pipeline()
        task_id = record["current_task_id"]
        task = self.app.studio.jobs.tasks[task_id]
        self.assertEqual(task["input"]["writer"]["step"], "draft")
        chunked = task["input"].get("chunked") or {}
        self.assertEqual(len(chunked.get("blocks") or []), 3)
        self.assertIn("第一段要点", chunked["blocks"][0])
        self.assertTrue(chunked.get("continuity_chars"))
        self.app.studio.jobs.finish(task_id, "第一段。\n\n第二段。\n\n第三段。")
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "done")
        chapter = self.store.get_chapter(self.book_id, 1)
        self.assertIn("第三段", chapter["content"])

    def test_chapter_revision_conflict(self):
        self._prepare_outline()
        self.start(kind="chapter", chapter_no=1)
        task_id = self.current_pipeline()["current_task_id"]
        # 生成期间用户手动保存了这一章 → 冲突。
        self.store.save_chapter_version(self.book_id, 1, content="用户手改", kind="manual")
        self.app.studio.jobs.finish(task_id, "AI 的正文")
        self.tick()
        record = self.current_pipeline()
        self.assertEqual(record["status"], "conflict")
        # 用户手改稿未被覆盖。
        self.assertEqual(self.store.get_chapter(self.book_id, 1)["content"], "用户手改")

    def test_duplicate_tick_is_idempotent(self):
        self._prepare_outline()
        self.start(kind="chapter", chapter_no=1)
        task_id = self.current_pipeline()["current_task_id"]
        self.app.studio.jobs.finish(task_id, "正文内容。")
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "done")
        versions_before = len(self.store.get_chapter(self.book_id, 1)["versions"])
        self.tick()
        self.tick()
        self.assertEqual(len(self.store.get_chapter(self.book_id, 1)["versions"]), versions_before)


if __name__ == "__main__":
    unittest.main()
