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

    # ---- 章末状态步驱动辅助（draft 之后统一跟 state 步） ----

    @staticmethod
    def state_json(chapter_no: int = 1, **overrides) -> str:
        """一份最小合法章末状态 JSON（提取步的任务结果）。"""
        payload = {
            "characters": [{"name": "张三", "presence": "出场",
                            "location": "北境大营", "condition": "左臂受伤"}],
            "relations": [],
            "knowledge": [],
            "events": [f"第{chapter_no}章的关键事件"],
            "open_threads": [],
            "next_hooks": [],
        }
        payload.update(overrides)
        return json.dumps(payload, ensure_ascii=False)

    def finish_state(self, chapter_no: int = 1, **overrides) -> None:
        """把当前 state 步的任务喂一份合法状态并推进。"""
        record = self.current_pipeline()
        self.assertEqual(record["steps"][record["step_index"]], "state")
        self.app.studio.jobs.finish(
            record["current_task_id"], self.state_json(chapter_no, **overrides))
        self.tick()


class SetupChainTests(PipelineTestCase):
    def setUp(self):
        super().setUp()
        # 设定链的起点是已保存设定里的创意（空创意在排队前会被如实拒绝：
        # 「请先在『设定 → 故事创意』里写一句话…」）——这里按真实使用先存一版。
        self.store.save_setup(self.book_id, content={"idea": "一句话创意"}, kind="manual")

    def test_system_prompt_freezes_across_steps_retry_and_rebase(self):
        self.store.save_system_prompt(self.book_id, text="最初作品规则", expected_revision=0)
        self.start()
        self.assertIn("最初作品规则", self.app.studio.jobs.submitted[-1]["system"])
        baseline = self.current_pipeline()["base_revision"]
        self.store.save_system_prompt(self.book_id, text="修改后的规则", expected_revision=1)
        self.assertEqual(self.store.get_book(self.book_id)["book"]["revision"], baseline)
        task_id = self.current_pipeline()["current_task_id"]
        self.app.studio.jobs.finish(task_id, '{"channel":"male","cat":"玄幻"}')
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "running")
        self.assertIn("最初作品规则", self.app.studio.jobs.submitted[-1]["system"])
        self.manager.cancel_pipeline(self.book_id)
        self.manager.retry_pipeline(self.book_id)
        self.assertNotIn("修改后的规则", self.app.studio.jobs.submitted[-1]["system"])
        self.manager.cancel_pipeline(self.book_id)
        self.manager.retry_pipeline(self.book_id, rebase=True)
        self.assertIn("修改后的规则", self.app.studio.jobs.submitted[-1]["system"])
        self.assertEqual(self.current_pipeline()["request_snapshot"]["global_system_prompt_revision"], 2)

    def test_legacy_pipeline_retry_does_not_load_current_prompt(self):
        self.start()
        self.manager.cancel_pipeline(self.book_id)
        with self.store._lock:
            current = self.store._load_manifest(self.book_id)
            snapshot = current["active_pipeline"]["request_snapshot"]
            snapshot.pop("global_system_prompt", None)
            snapshot.pop("global_system_prompt_revision", None)
            self.store._save_manifest(self.store._book_dir(self.book_id), current)
        self.store.save_system_prompt(self.book_id, text="后来的作品规则", expected_revision=0)
        self.manager.retry_pipeline(self.book_id)
        self.assertNotIn("后来的作品规则", self.app.studio.jobs.submitted[-1]["system"])

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

    def test_retry_after_written_step_keeps_advanced_base_revision(self):
        initial = self.start()
        self.app.studio.jobs.finish(self.last_task()["id"], '{"channel":"male","cat":"玄幻"}')
        self.tick()
        advanced = self.current_pipeline()
        self.assertGreater(advanced["base_revision"], initial["base_revision"])
        self.app.studio.jobs.finish(self.last_task()["id"], "", status="failed", error="超时")
        self.tick()
        self.manager.retry_pipeline(self.book_id)
        retried = self.current_pipeline()
        self.assertEqual(retried["base_revision"], advanced["base_revision"])
        self.assertEqual(retried["step_index"], advanced["step_index"])
        self.app.studio.jobs.finish(self.last_task()["id"], '["书名"]')
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "running")
        self.assertEqual(self.current_pipeline()["step_index"], 2)

    def test_rebase_discards_old_accumulated_setup_overlay(self):
        self.start()
        self.app.studio.jobs.finish(self.last_task()["id"], '{"channel":"male","cat":"玄幻"}')
        self.tick()
        self.app.studio.jobs.finish(self.last_task()["id"], "", status="failed", error="超时")
        self.tick()
        self.store.save_setup(self.book_id, content={"idea": "换基后的创意"})
        self.manager.retry_pipeline(self.book_id, rebase=True)
        self.assertIn("换基后的创意", self.last_task()["input"]["content"])
        self.assertNotIn("setup", self.current_pipeline()["step_outputs"])

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

    def test_segmented_and_state_steps_keep_same_frozen_prompt(self):
        self._prepare_outline()
        self.store.save_system_prompt(self.book_id, text="固定全书风格", expected_revision=0)
        self.start(kind="chapter_segs", chapter_no=2)
        self.assertIn("固定全书风格", self.app.studio.jobs.submitted[-1]["system"])
        self.app.studio.jobs.finish(self.current_pipeline()["current_task_id"], '["第一段要点","第二段要点"]')
        self.store.save_system_prompt(self.book_id, text="新风格", expected_revision=1)
        self.tick()
        data = self.app.studio.jobs.submitted[-1]
        self.assertIn("chunked", data)
        self.assertIn("固定全书风格", data["system"])
        self.assertNotIn("新风格", data["system"])
        self.app.studio.jobs.finish(self.current_pipeline()["current_task_id"], "完整新正文")
        self.tick()
        data = self.app.studio.jobs.submitted[-1]
        self.assertEqual(data["writer"]["step"], "state")
        self.assertIn("固定全书风格", data["system"])

    def test_whole_chapter_writes_generated_version(self):
        self._prepare_outline()
        pipeline = self.start(kind="chapter", chapter_no=2)
        record = self.current_pipeline()
        self.assertEqual(record["steps"], ["draft", "state"])
        task_id = record["current_task_id"]
        task = self.app.studio.jobs.tasks[task_id]
        self.assertEqual(task["input"]["writer"]["chapter_no"], 2)
        self.assertEqual(task["input"]["writer"]["step"], "draft")
        self.assertIn("梗概2", task["input"]["content"])
        self.app.studio.jobs.finish(task_id, "第二章的完整正文。")
        self.tick()
        # draft 写回后进入 state 步：提取任务读的是刚保存的正文。
        record = self.current_pipeline()
        self.assertEqual(record["steps"][record["step_index"]], "state")
        state_task = self.app.studio.jobs.tasks[record["current_task_id"]]
        self.assertEqual(state_task["input"]["writer"]["step"], "state")
        self.assertIn("第二章的完整正文。", state_task["input"]["content"])
        chapter_mid = self.store.get_chapter(self.book_id, 2)
        self.assertEqual(chapter_mid["content"], "第二章的完整正文。")
        self.finish_state(chapter_no=2)
        record = self.current_pipeline()
        self.assertEqual(record["status"], "done")
        chapter = self.store.get_chapter(self.book_id, 2)
        self.assertEqual(chapter["content"], "第二章的完整正文。")
        self.assertEqual(chapter["versions"][0]["kind"], "generated")
        self.assertEqual(chapter["versions"][0]["task_id"], task_id)
        state = self.store.load_chapter_state(self.book_id, 2)
        self.assertEqual(state["effective_status"], "ready")
        self.assertEqual(state["source_version_id"], chapter["versions"][0]["id"])
        self.assertEqual(state["state"]["events"], ["第2章的关键事件"])
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
        self.assertEqual(record["steps"], ["segs", "draft", "state"])
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
        # 第 3 步 state。
        self.finish_state(chapter_no=1)
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
        self.finish_state(chapter_no=1)
        self.assertEqual(self.current_pipeline()["status"], "done")
        versions_before = len(self.store.get_chapter(self.book_id, 1)["versions"])
        self.tick()
        self.tick()
        self.assertEqual(len(self.store.get_chapter(self.book_id, 1)["versions"]), versions_before)


class ChapterStateFlowTests(PipelineTestCase):
    """章末状态（跨章连贯性修复计划 §三.B）：多章传递、失败兜底、失效与重建。"""

    MEET_RELATIONS = [{"between": ["张三", "李四"], "change": "荒庙相遇，结为同盟"}]
    MEET_KNOWLEDGE = [{"name": "李四", "known": [], "unknown": ["张三的真实身份"]}]

    def _prepare(self, chapters=4):
        self.store.save_setup(self.book_id, content={
            "idea": "创意",
            "characters": [{"name": "张三", "role": "lead"}, {"name": "李四", "role": "side"}],
        }, kind="manual")
        rows = [{"no": i, "title": f"第{i}章", "beat": f"梗概{i}", "hook": ""}
                for i in range(1, chapters + 1)]
        self.store.save_outline(self.book_id, chapters=rows, kind="generated", sync=True)

    def _meet_state(self, chapter_no: int) -> str:
        return self.state_json(
            chapter_no, relations=self.MEET_RELATIONS,
            knowledge=self.MEET_KNOWLEDGE,
            open_threads=["残图的另一半在谁手里"],
        )

    def test_states_flow_across_chapters(self):
        """验收 §五：连续 4 章——第 2 章建立相识，第 3、4 章请求必须带着这些事实。"""
        self._prepare(4)
        pipeline = self.start(kind="chapter_all", from_no=1, want_count=4)
        # 批量开始时没有任何状态记录：快照里的状态表为空，后续状态每步现读。
        self.assertEqual(pipeline["request_snapshot"].get("chapter_states"), [])
        for no in range(1, 5):
            record = self.current_pipeline()
            task = self.app.studio.jobs.tasks[record["current_task_id"]]
            content = task["input"]["content"]
            if no == 1:
                self.assertNotIn("前情状态", content)
            else:
                self.assertIn("【前情状态（截至上一章结束的已确认事实）】", content)
                self.assertIn(f"第{no - 1}章末", content)
            if no >= 3:
                # 第 2 章建立的关系与「不知道」，后续章节的请求必须延续/不得翻案。
                self.assertIn("荒庙相遇，结为同盟", content)
                self.assertIn("李四不知道：张三的真实身份", content)
                self.assertIn("未决：残图的另一半在谁手里", content)
                self.assertIn("不得无故回退", content)
            self.app.studio.jobs.finish(task["id"], f"第{no}章正文。")
            self.tick()
            self.app.studio.jobs.finish(
                self.current_pipeline()["current_task_id"], self._meet_state(no))
            self.tick()
        record = self.current_pipeline()
        self.assertEqual(record["status"], "done")
        states = self.store.chapter_context_states(self.book_id, 4)
        self.assertEqual([row["no"] for row in states], [1, 2, 3])

    def test_extraction_failure_keeps_content_and_chain(self):
        """验收 §五：状态提取失败不丢正文、不落空白有效状态；链继续走完。"""
        self._prepare(2)
        self.store.save_chapter_version(self.book_id, 1, content="第一章正文。")
        for bad_result, bad_status in (("不是 JSON 的输出", "done"),
                                       ("", "failed")):
            with self.subTest(bad_result=bad_result[:6], bad_status=bad_status):
                self.start(kind="chapter", chapter_no=2)
                draft_id = self.current_pipeline()["current_task_id"]
                self.app.studio.jobs.finish(draft_id, "第二章的正文。")
                self.tick()
                # 正文已在 state 之前保存。
                self.assertEqual(self.store.get_chapter(self.book_id, 2)["content"],
                                 "第二章的正文。")
                record = self.current_pipeline()
                task = self.app.studio.jobs.tasks[record["current_task_id"]]
                if bad_status == "done":
                    self.app.studio.jobs.finish(task["id"], bad_result)
                else:
                    self.app.studio.jobs.finish(task["id"], bad_result,
                                                status=bad_status, error="模型超时")
                self.tick()
                record = self.current_pipeline()
                self.assertEqual(record["status"], "done")
                self.assertTrue(record.get("state_errors"))
                self.assertEqual(self.store.get_chapter(self.book_id, 2)["content"],
                                 "第二章的正文。")
                state = self.store.load_chapter_state(self.book_id, 2)
                self.assertEqual(state["status"], "failed")
                self.assertEqual(state["effective_status"], "failed")
                self.assertIsNone(state["state"])
                self.assertEqual(self.store.chapter_context_states(self.book_id, 3), [])
                # 失败状态可重建：流水线已 done，走 state_rebuild 入口补建（不重写正文）。
                pl = self.manager.start_pipeline(
                    self.book_id, kind="state_rebuild", from_no=2, want_count=1,
                    model_key="online:probe", model_name="probe")
                self.assertEqual(pl["steps"], ["state"])
                rebuild_task = self.app.studio.jobs.tasks[pl["current_task_id"]]
                self.assertEqual(rebuild_task["input"]["writer"]["step"], "state")
                self.assertIn("第二章的正文。", rebuild_task["input"]["content"])
                self.app.studio.jobs.finish(rebuild_task["id"], self.state_json(2))
                self.tick()
                state = self.store.load_chapter_state(self.book_id, 2)
                self.assertEqual(state["effective_status"], "ready")

    def test_cancel_during_state_stops_chain(self):
        self._prepare(2)
        self.start(kind="chapter", chapter_no=1)
        draft_id = self.current_pipeline()["current_task_id"]
        self.app.studio.jobs.finish(draft_id, "第一章正文。")
        self.tick()
        self.manager.cancel_pipeline(self.book_id)
        # 迟到的 state 结果不写回、不提交下一步。
        state_id = self.app.studio.jobs.submitted and list(self.app.studio.jobs.tasks)[-1]
        self.app.studio.jobs.finish(state_id, self.state_json(1))
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "cancelled")
        self.assertIsNone(self.store.load_chapter_state(self.book_id, 1))

    def test_rewrite_invalidates_states_and_rebuild_uses_new_content(self):
        """验收 §五：重写第 2 章后，第 2 章及后续旧状态失效；重建基于新版本正文。"""
        self._prepare(4)
        versions = {}
        for no in range(1, 5):
            saved = self.store.save_chapter_version(self.book_id, no, content=f"第{no}章旧正文。")
            versions[no] = saved["version"]["id"]
            self.store.save_chapter_state(
                self.book_id, no, state={"events": [f"旧状态{no}"]},
                source_version_id=versions[no], task_id=f"st-old-{no}")
        for no in range(1, 5):
            self.assertEqual(self.store.load_chapter_state(self.book_id, no)["effective_status"],
                             "ready")
        # 重写第 2 章（产生新版本并切换指针）。
        rewritten = self.store.save_chapter_version(self.book_id, 2, content="第二章重写稿。")
        self.assertEqual(self.store.load_chapter_state(self.book_id, 1)["effective_status"], "ready")
        for no in (2, 3, 4):
            self.assertEqual(self.store.load_chapter_state(self.book_id, no)["effective_status"],
                             "stale", f"第 {no} 章旧状态应失效")
        self.assertEqual([row["no"] for row in self.store.chapter_context_states(self.book_id, 4)],
                         [1])
        # 第 3、4 章正文保持原样；重建不自动改稿。
        pl = self.start(kind="state_rebuild", from_no=2, want_count=3)
        self.assertEqual(pl["steps"], ["state", "state", "state"])
        self.assertEqual([row["no"] for row in pl["request_snapshot"]["chapter_plan"]], [2, 3, 4])
        for no in (2, 3, 4):
            record = self.current_pipeline()
            task = self.app.studio.jobs.tasks[record["current_task_id"]]
            self.assertEqual(task["input"]["writer"]["step"], "state")
            self.assertEqual(task["input"]["writer"]["chapter_no"], no)
            if no == 2:
                self.assertIn("第二章重写稿。", task["input"]["content"])
                self.assertNotIn("第2章旧正文。", task["input"]["content"])
            self.app.studio.jobs.finish(
                task["id"], self.state_json(no, events=[f"重建后事实{no}"]))
            self.tick()
        self.assertEqual(self.current_pipeline()["status"], "done")
        for no in (2, 3, 4):
            state = self.store.load_chapter_state(self.book_id, no)
            self.assertEqual(state["effective_status"], "ready")
            self.assertEqual(state["state"]["events"], [f"重建后事实{no}"])
            expected_content = "第二章重写稿。" if no == 2 else f"第{no}章旧正文。"
            self.assertEqual(self.store.get_chapter(self.book_id, no)["content"],
                             expected_content, "重建不改正文")
        self.assertEqual([row["no"] for row in self.store.chapter_context_states(self.book_id, 5)],
                         [1, 2, 3, 4])

    def test_version_switch_marks_state_stale_until_switched_back(self):
        self._prepare(1)
        first = self.store.save_chapter_version(self.book_id, 1, content="第一版。")
        self.store.save_chapter_version(self.book_id, 1, content="第二版。")
        self.store.save_chapter_state(self.book_id, 1, state={"events": ["事实"]},
                                      source_version_id=first["version"]["id"])
        self.assertEqual(self.store.load_chapter_state(self.book_id, 1)["effective_status"],
                         "stale")
        self.store.select_chapter_version(self.book_id, 1, version_id=first["version"]["id"])
        self.assertEqual(self.store.load_chapter_state(self.book_id, 1)["effective_status"],
                         "ready")

    def test_state_rebuild_skips_chapters_without_content(self):
        self._prepare(3)
        self.store.save_chapter_version(self.book_id, 2, content="只有第二章有正文。")
        pl = self.start(kind="state_rebuild", from_no=1, want_count=3)
        self.assertEqual([row["no"] for row in pl["request_snapshot"]["chapter_plan"]], [2])
        self.assertEqual(pl["steps"], ["state"])
        fresh = self.store.create_book(title="空书")
        rows = [{"no": 1, "title": "章", "beat": "", "hook": ""}]
        self.store.save_outline(fresh["id"], chapters=rows, kind="generated", sync=True)
        with self.assertRaisesRegex(Exception, "没有已写正文"):
            self.manager.start_pipeline(fresh["id"], kind="state_rebuild", from_no=1,
                                        want_count=1, model_key="online:probe",
                                        model_name="probe")


class ChapterAllPipelineTests(PipelineTestCase):
    """方案A（2026-11-04 拍板）：链的正文步骤写完拆章范围内全部章节。

    逐章落盘、每章一次模型调用、可中途取消；前章衔接每步现读，
    不能用流水线创建时的旧快照。
    """

    def _prepare_outline(self, chapters=5):
        self.store.save_setup(self.book_id, content={"idea": "一句话创意"}, kind="manual")
        rows = [{"no": i, "title": f"第{i}章", "beat": f"梗概{i}", "hook": "钩"} for i in range(1, chapters + 1)]
        self.store.save_outline(self.book_id, chapters=rows, kind="generated", sync=True)

    def test_batch_writes_every_existing_chapter_in_range(self):
        self._prepare_outline(5)
        pipeline = self.start(kind="chapter_all", from_no=1, want_count=10)
        record = self.current_pipeline()
        # 范围 1–10 只写拆章实际落下的 1–5 章：模型少拆不硬造。
        # 步骤是 (draft, state) × 5 交错：每章正文落盘后立刻提取章末状态。
        self.assertEqual(record["steps"], ["draft", "state"] * 5)
        self.assertEqual([row["no"] for row in record["request_snapshot"]["chapter_plan"]], [1, 2, 3, 4, 5])
        for expected_no in range(1, 6):
            record = self.current_pipeline()
            self.assertEqual(record["status"], "running")
            task_id = record["current_task_id"]
            task = self.app.studio.jobs.tasks[task_id]
            writer = task["input"]["writer"]
            self.assertEqual(writer["chapter_no"], expected_no)
            self.assertEqual(writer["step"], "draft")
            self.assertIn(f"梗概{expected_no}", task["input"]["content"])
            if expected_no > 1:
                # 前章衔接用上一步刚落盘的正文，不是创建时的旧快照。
                self.assertIn(f"第{expected_no - 1}章的正文", task["input"]["content"])
            if expected_no == 3:
                self.assertIn("批量 5/10", task["title"])
            self.assertEqual(task["input"]["writer"]["chapter_no"], int(
                record["request_snapshot"]["chapter_no"]))
            self.app.studio.jobs.finish(task_id, f"第{expected_no}章的正文。")
            self.tick()
            # state 步同章：提取任务归属与 snapshot 一致。
            record = self.current_pipeline()
            self.assertEqual(record["steps"][record["step_index"]], "state")
            state_task = self.app.studio.jobs.tasks[record["current_task_id"]]
            self.assertEqual(state_task["input"]["writer"]["chapter_no"], expected_no)
            self.assertEqual(state_task["input"]["writer"]["step"], "state")
            self.assertIn(f"第{expected_no}章的正文。", state_task["input"]["content"])
            self.finish_state(chapter_no=expected_no)
        record = self.current_pipeline()
        self.assertEqual(record["status"], "done")
        for no in range(1, 6):
            chapter = self.store.get_chapter(self.book_id, no)
            self.assertEqual(chapter["content"], f"第{no}章的正文。")
            self.assertEqual(chapter["versions"][0]["kind"], "generated")
            state = self.store.load_chapter_state(self.book_id, no)
            self.assertEqual(state["effective_status"], "ready")
            self.assertEqual(state["source_version_id"], chapter["versions"][0]["id"])

    def test_empty_range_refused_without_pipeline(self):
        self._prepare_outline(3)
        with self.assertRaises(Exception) as ctx:
            self.start(kind="chapter_all", from_no=10, want_count=5)
        self.assertIn("可写的章节", str(ctx.exception))
        self.assertIsNone(self.store.get_active_pipeline(self.book_id))

    def test_partial_range_writes_intersection_only(self):
        self._prepare_outline(5)
        pipeline = self.start(kind="chapter_all", from_no=4, want_count=3)
        record = self.current_pipeline()
        self.assertEqual([row["no"] for row in record["request_snapshot"]["chapter_plan"]], [4, 5])
        for expected_no in (4, 5):
            task_id = record["current_task_id"]
            self.assertEqual(self.app.studio.jobs.tasks[task_id]["input"]["writer"]["chapter_no"], expected_no)
            self.app.studio.jobs.finish(task_id, f"第{expected_no}章的正文。")
            self.tick()
            self.finish_state(chapter_no=expected_no)
            record = self.current_pipeline()
        self.assertEqual(record["status"], "done")
        self.assertEqual(self.store.get_chapter(self.book_id, 4)["content"], "第4章的正文。")
        self.assertEqual(self.store.get_chapter(self.book_id, 5)["content"], "第5章的正文。")
        self.assertEqual(self.store.get_chapter(self.book_id, 1)["content"], "")

    def test_cancel_midway_keeps_written_chapters(self):
        self._prepare_outline(3)
        self.start(kind="chapter_all", from_no=1, want_count=3)
        first_id = self.current_pipeline()["current_task_id"]
        self.app.studio.jobs.finish(first_id, "第1章的正文。")
        self.tick()
        # 第 1 章的 state 步（取消前先走完，模拟正常推进）。
        self.finish_state(chapter_no=1)
        record = self.current_pipeline()
        self.assertEqual(record["step_index"], 2)
        self.assertEqual(self.app.studio.jobs.tasks[record["current_task_id"]]["input"]["writer"]["chapter_no"], 2)
        self.manager.cancel_pipeline(self.book_id)
        record = self.current_pipeline()
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(self.store.get_chapter(self.book_id, 1)["content"], "第1章的正文。")
        self.assertEqual(self.store.get_chapter(self.book_id, 2)["content"], "")
        submitted = len(self.app.studio.jobs.submitted)
        self.tick()
        self.assertEqual(len(self.app.studio.jobs.submitted), submitted)

    def test_failed_step_retry_resubmits_same_chapter(self):
        self._prepare_outline(3)
        self.start(kind="chapter_all", from_no=1, want_count=3)
        first_id = self.current_pipeline()["current_task_id"]
        self.app.studio.jobs.finish(first_id, "第1章的正文。")
        self.tick()
        self.finish_state(chapter_no=1)
        second_id = self.current_pipeline()["current_task_id"]
        self.app.studio.jobs.finish(second_id, "", status="failed", error="线路抖动")
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "failed")
        # retry_pipeline 的返回值是提交前快照；current_task_id 以存储里的权威记录为准。
        self.manager.retry_pipeline(self.book_id)
        task_id = self.current_pipeline()["current_task_id"]
        self.assertEqual(self.app.studio.jobs.tasks[task_id]["input"]["writer"]["chapter_no"], 2)
        self.app.studio.jobs.finish(task_id, "第2章重试正文。")
        self.tick()
        # 重试成功后接着走的是第 2 章的 state 步（draft/state 交错），不是第 3 章。
        third_id = self.current_pipeline()["current_task_id"]
        third_task = self.app.studio.jobs.tasks[third_id]
        self.assertEqual(third_task["input"]["writer"]["step"], "state")
        self.assertEqual(third_task["input"]["writer"]["chapter_no"], 2)
        self.app.studio.jobs.finish(third_id, self.state_json(2))
        self.tick()
        fourth_id = self.current_pipeline()["current_task_id"]
        self.assertEqual(self.app.studio.jobs.tasks[fourth_id]["input"]["writer"]["chapter_no"], 3)
        self.assertEqual(self.app.studio.jobs.tasks[fourth_id]["input"]["writer"]["step"], "draft")


class ContinuationRegressionTests(PipelineTestCase):
    def setUp(self):
        super().setUp()
        self.store.save_setup(self.book_id, content={"idea": "创意"})
        self.store.save_outline(self.book_id, chapters=[
            {"no": no, "title": f"章{no}", "beat": "梗概"} for no in range(1, 4)
        ], sync=True)

    def test_freezes_full_baseline_sends_tail_and_appends(self):
        body = "不可丢失开头" + "甲" * (prompts.CONTINUE_TAIL_CHARS + 20) + "尾段"
        first = self.store.save_chapter_version(self.book_id, 2, content=body)
        self.store.save_chapter_version(self.book_id, 1, content="上章未决事件")
        pipeline = self.start(kind="chapter_continue", chapter_no=2)
        baseline = pipeline["request_snapshot"]["chapter_baseline"]
        self.assertEqual(baseline["content"], body)
        self.assertEqual(baseline["current_version_id"], first["version"]["id"])
        task = self.last_task()
        self.assertNotIn("不可丢失开头", json.dumps(task["input"], ensure_ascii=False))
        self.assertIn("尾段", task["input"]["content"])
        self.assertIn("上章未决事件", task["input"]["content"])
        self.app.studio.jobs.finish(task["id"], "新增正文")
        self.tick()
        chapter = self.store.get_chapter(self.book_id, 2)
        self.assertEqual(chapter["content"], body + "\n\n新增正文")
        self.assertEqual(len(chapter["versions"]), 2)
        self.tick()
        self.assertEqual(len(self.store.get_chapter(self.book_id, 2)["versions"]), 2)
        self.store.select_chapter_version(self.book_id, 2, version_id=first["version"]["id"])
        self.assertEqual(self.store.get_chapter(self.book_id, 2)["content"], body)

    def test_all_modes_and_chunk_prefix_include_previous_excerpt(self):
        for kind in ("chapter", "chapter_rewrite", "chapter_continue", "chapter_segs"):
            with self.subTest(kind=kind):
                self.store.save_chapter_version(self.book_id, 1, content="前章当前原文")
                self.store.save_chapter_version(self.book_id, 2, content="本章基线")
                self.start(kind=kind, chapter_no=2)
                task = self.last_task()
                self.assertIn("上一章原文节选", task["input"]["content"])
                self.assertIn("前章当前原文", task["input"]["content"])
                if kind == "chapter_segs":
                    self.app.studio.jobs.finish(task["id"], '["要点一","要点二"]')
                    self.tick()
                    self.assertIn("前章当前原文", self.last_task()["input"]["chunked"]["prefix"])
                self.manager.cancel_pipeline(self.book_id)

    def test_retry_preserves_baseline_revisions_rebase_refreshes_all(self):
        self.store.save_chapter_version(self.book_id, 1, content="旧前章")
        self.store.save_chapter_version(self.book_id, 2, content="旧基线")
        original = self.start(kind="chapter_continue", chapter_no=2)
        self.app.studio.jobs.finish(self.last_task()["id"], "", status="failed", error="超时")
        self.tick()
        self.store.save_chapter_version(self.book_id, 1, content="新前章")
        self.store.save_chapter_version(self.book_id, 2, content="新基线")
        self.store.save_setup(self.book_id, content={"idea": "新设定"})
        retried = self.manager.retry_pipeline(self.book_id)
        for key in ("request_snapshot", "base_revision", "chapter_revision"):
            self.assertEqual(retried[key], original[key])
        self.assertIn("旧基线", self.last_task()["input"]["content"])
        self.assertNotIn("新基线", self.last_task()["input"]["content"])
        self.app.studio.jobs.finish(self.last_task()["id"], "新增")
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "conflict")
        rebased = self.manager.retry_pipeline(self.book_id, rebase=True)
        snap = rebased["request_snapshot"]
        self.assertEqual(snap["chapter_baseline"]["content"], "新基线")
        self.assertEqual(snap["previous_chapter"]["excerpt"], "新前章")
        self.assertEqual(snap["setup_version_id"], self.store._load_manifest(self.book_id)["current_setup_version_id"])
        self.app.studio.jobs.finish(self.last_task()["id"], "新续写")
        self.tick()
        self.assertEqual(self.store.get_chapter(self.book_id, 2)["content"], "新基线\n\n新续写")

    def test_legacy_missing_baseline_fails_closed_until_rebase(self):
        self.store.save_chapter_version(self.book_id, 2, content="基线")
        pl = self.start(kind="chapter_continue", chapter_no=2)
        self.store.update_pipeline(self.book_id, pl["id"],
                                   lambda record: record["request_snapshot"].pop("chapter_baseline"))
        self.app.studio.jobs.finish(self.last_task()["id"], "旧任务增量")
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "failed")
        self.assertEqual(self.store.get_chapter(self.book_id, 2)["content"], "基线")
        with self.assertRaisesRegex(Exception, "基线"):
            self.manager.retry_pipeline(self.book_id)
        self.manager.retry_pipeline(self.book_id, rebase=True)
        self.assertIn("基线", self.last_task()["input"]["content"])

    def test_total_length_gate_and_truncation_preserve_old_version(self):
        body = "原" * 199999
        self.store.save_chapter_version(self.book_id, 2, content=body)
        self.start(kind="chapter_continue", chapter_no=2)
        self.app.studio.jobs.finish(self.last_task()["id"], "超限")
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "failed")
        self.assertIn("上限", self.current_pipeline()["error"])
        self.assertEqual(self.store.get_chapter(self.book_id, 2)["content"], body)
        self.manager.retry_pipeline(self.book_id)
        self.app.studio.jobs.finish(self.last_task()["id"], "半截", finish_reason="length")
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "failed")
        self.assertEqual(len(self.store.get_chapter(self.book_id, 2)["versions"]), 1)

    def test_empty_baseline_refused_before_submit(self):
        with self.assertRaisesRegex(Exception, "基线"):
            self.start(kind="chapter_continue", chapter_no=2)
        self.assertEqual(len(self.app.studio.jobs.submitted), 0)

    def test_continue_preserves_existing_paragraph_boundaries(self):
        for suffix in ("\n\n", "\n", "\r\n\r\n"):
            with self.subTest(suffix=repr(suffix)):
                body = "原文保留空格  " + suffix
                self.store.save_chapter_version(self.book_id, 2, content=body)
                self.start(kind="chapter_continue", chapter_no=2)
                self.app.studio.jobs.finish(self.last_task()["id"], "新增")
                self.tick()
                expected = body + ("\n" if suffix == "\n" else "") + "新增"
                self.assertEqual(self.store.get_chapter(self.book_id, 2)["content"], expected)
                # 走完 state 步，下一轮子用例才不会被「已有进行中的生成」挡住。
                self.finish_state(chapter_no=2)

    def test_continuation_conflict_never_appends_to_changed_baseline(self):
        self.store.save_chapter_version(self.book_id, 2, content="旧基线")
        self.start(kind="chapter_continue", chapter_no=2)
        self.store.save_chapter_version(self.book_id, 2, content="用户手改")
        self.app.studio.jobs.finish(self.last_task()["id"], "旧增量")
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "conflict")
        chapter = self.store.get_chapter(self.book_id, 2)
        self.assertEqual(chapter["content"], "用户手改")
        self.assertEqual(len(chapter["versions"]), 2)

    def test_previous_empty_current_does_not_fall_back_to_history(self):
        self.store.save_chapter_version(self.book_id, 1, content="历史候选不应带入")
        index = self.store._load_chapter_index(self.store._book_dir(self.book_id), 1)
        index["current_version_id"] = ""
        self.store._write_chapter_index(self.store._book_dir(self.book_id), index)
        self.start(kind="chapter", chapter_no=2)
        self.assertNotIn("历史候选不应带入", self.last_task()["input"]["content"])
        previous = self.current_pipeline()["request_snapshot"]["previous_chapter"]
        self.assertEqual(previous["status"], "no_current_version")
        self.assertEqual(previous["excerpt"], "")

    def test_segment_retry_keeps_current_step_base_and_frozen_excerpt(self):
        self.store.save_chapter_version(self.book_id, 1, content="冻结前章")
        original = self.start(kind="chapter_segs", chapter_no=2)
        self.app.studio.jobs.finish(self.last_task()["id"], '["要点一","要点二"]')
        self.tick()
        advanced = self.current_pipeline()
        self.assertEqual(advanced["step_index"], 1)
        self.app.studio.jobs.finish(self.last_task()["id"], "", status="failed", error="超时")
        self.tick()
        self.manager.retry_pipeline(self.book_id)
        retried = self.current_pipeline()
        self.assertEqual(retried["base_revision"], advanced["base_revision"])
        self.assertEqual(retried["chapter_revision"], advanced["chapter_revision"])
        self.assertEqual(retried["request_snapshot"], original["request_snapshot"])
        self.assertIn("冻结前章", self.last_task()["input"]["chunked"]["prefix"])
        self.app.studio.jobs.finish(self.last_task()["id"], "分段全文")
        self.tick()
        self.finish_state(chapter_no=2)
        self.assertEqual(self.current_pipeline()["status"], "done")

    def test_stale_chapter_revision_refused_before_submit(self):
        with self.assertRaisesRegex(Exception, "修改"):
            self.start(kind="chapter", chapter_no=2, chapter_revision=999)
        self.assertEqual(len(self.app.studio.jobs.submitted), 0)

    def test_snapshot_reads_are_locked_at_start_and_rebase(self):
        original = self.store.chapter_writing_inputs
        def checked(*args, **kwargs):
            self.assertTrue(self.store._lock._is_owned())
            return original(*args, **kwargs)
        self.store.chapter_writing_inputs = checked
        self.start(kind="chapter", chapter_no=2)
        self.manager.cancel_pipeline(self.book_id)
        self.manager.retry_pipeline(self.book_id, rebase=True)


if __name__ == "__main__":
    unittest.main()
