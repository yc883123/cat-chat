# -*- coding: utf-8 -*-
"""护栏：创作场阶段四——分块续写（4A）、图谱反哺（4B-1）、分镜生图（4C）。

覆盖（对应《2026-10-28-创作场阶段四》计划 §五）：

- 4A：段落打包切块、单段超长硬切、块数上限与剩余字数明示、轮次组装（衔接头
  只从第 2 块起）、runner 单任务多轮（进度事件、轮间分隔、取消保留已完成块、
  用量跨轮累加）、超限任务自动走分块且重试快照带分块契约。
- 4B-1：Top-20 实体选取与裁剪、空要素不注入、runner system 消息前置、
  预算计入 system。
- 4C：逐场解析（成功/失败不臆造切分）、组装与校验、api_stage 分镜提交契约、
  api_apply 明确拒绝分镜写回。

边界：不连模型、不跑真实供应商；runner 用桩模型，service 用最小 Studio 替身。
"""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core.exceptions import TaskCancelled  # noqa: E402
from naiba.novel import prompts  # noqa: E402
from naiba.novel.service import NovelService  # noqa: E402
from naiba.studio.runner import StudioRunner  # noqa: E402
from naiba.studio.store import StudioStore  # noqa: E402


# ============================================================
# 4A 分块切分
# ============================================================

class SplitBodyChunksTests(unittest.TestCase):
    def test_short_text_stays_single_chunk(self):
        blocks, dropped = prompts.split_body_chunks("短正文", 3000)
        self.assertEqual(blocks, ["短正文"])
        self.assertEqual(dropped, 0)

    def test_paragraphs_pack_within_limit(self):
        text = ("一段内容。" * 20 + "\n") * 8  # 每行 101 字，共 808 字
        blocks, dropped = prompts.split_body_chunks(text, 200)
        self.assertEqual(dropped, 0)
        self.assertGreater(len(blocks), 1)
        for block in blocks:
            self.assertLessEqual(len(block), 200)
        # 不丢内容：拼回去字数一致（这里没有空白块）。
        self.assertEqual(sum(len(block) for block in blocks), len(text))

    def test_oversized_paragraph_hard_split_by_sentence(self):
        text = "句子很长的整段没有换行。" * 400  # 3600 字无换行
        blocks, dropped = prompts.split_body_chunks(text, 1000)
        self.assertEqual(dropped, 0)
        for block in blocks:
            self.assertLessEqual(len(block), 1000)
        self.assertEqual(sum(len(block) for block in blocks), len(text))

    def test_chunk_cap_reports_dropped_chars(self):
        text = "字" * (3000 * prompts.ADAPT_CHUNK_LIMIT + 500)
        blocks, dropped = prompts.split_body_chunks(text, 3000)
        self.assertEqual(len(blocks), prompts.ADAPT_CHUNK_LIMIT)
        self.assertEqual(dropped, 500, "超块部分必须精确报告，不静默丢")


class BuildAdaptChunksTests(unittest.TestCase):
    def test_chunks_carry_header_and_continuity_contract(self):
        text = ("段落内容，用来撑长度。" * 30 + "\n") * 10
        built = prompts.build_adapt_chunks(
            book_title="长夜灯", chapter_no=3, chapter_title="夜访",
            manuscript_label="原文", content=text, user_request="更紧凑",
            body_limit=3000,
        )
        chunked = built["chunked"]
        self.assertGreater(len(chunked["blocks"]), 1)
        for block in chunked["blocks"]:
            self.assertLessEqual(len(block), 3000)
        self.assertIn("切成", chunked["prefix"])
        self.assertIn("【本次稿件】第 3 章 夜访 · 原文", chunked["prefix"])
        self.assertIn("更紧凑", chunked["prefix"])
        self.assertEqual(chunked["continuity_chars"], prompts.CONTINUITY_TAIL_CHARS)
        self.assertIn("【已生成改编稿的结尾", chunked["continuity_label"])
        # 警告必须说明分块；overhead 供预算补足衔接开销。
        self.assertTrue(any("块顺序生成" in row for row in built["warnings"]))
        self.assertGreater(built["overhead_chars"], 0)
        self.assertEqual(built["chapters"][0]["dropped_chars"], 0)

    def test_chunk_cap_warning_names_remaining_chars(self):
        text = "字" * (3000 * prompts.ADAPT_CHUNK_LIMIT + 500)
        built = prompts.build_adapt_chunks(
            book_title="书", chapter_no=1, chapter_title="题", manuscript_label="原文",
            content=text, user_request="", body_limit=3000,
        )
        self.assertEqual(len(built["chunked"]["blocks"]), prompts.ADAPT_CHUNK_LIMIT)
        self.assertTrue(any("500 字未纳入" in row for row in built["warnings"]))
        self.assertEqual(built["chapters"][0]["dropped_chars"], 500)

    def test_budget_counts_system_and_overhead(self):
        base = prompts.budget_report(
            content="正文", chapters=[], warnings=[], body_limit=3000, max_chapters=6,
        )
        with_kg = prompts.budget_report(
            content="正文", chapters=[], warnings=[], body_limit=3000, max_chapters=6,
            system="【全书设定参考】\n· 人物：甲", overhead_chars=1200, kg_count=1,
        )
        self.assertGreater(with_kg["estimated_tokens"], base["estimated_tokens"])
        self.assertEqual(with_kg["kg_entities"], 1)
        self.assertEqual(with_kg["system_chars"], len("【全书设定参考】\n· 人物：甲"))
        self.assertEqual(with_kg["overhead_chars"], 1200)


# ============================================================
# 4B-1 图谱反哺
# ============================================================

class KgContextTests(unittest.TestCase):
    def test_empty_elements_return_empty_string(self):
        self.assertEqual(prompts.build_kg_context({}), "")
        self.assertEqual(prompts.build_kg_context(None), "")
        self.assertEqual(prompts.build_kg_context({"characters": [], "scenes": [], "props": []}), "")

    def test_characters_first_and_capped_at_limit(self):
        elements = {
            "characters": [f"人物{i}：描述" for i in range(25)],
            "scenes": [f"场景{i}" for i in range(5)],
            "props": ["道具一"],
        }
        rows = prompts.kg_entities(elements)
        self.assertEqual(len(rows), prompts.KG_ENTITY_LIMIT)
        self.assertTrue(rows[0].startswith("· 人物："))
        self.assertFalse(any("场景" in row for row in rows), "人物够数时场景道具不再入选")

    def test_long_lines_are_clipped(self):
        elements = {"characters": ["很" * 200]}
        rows = prompts.kg_entities(elements)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), len("· 人物：") + prompts.KG_LINE_CHARS + 1)

    def test_context_has_header(self):
        text = prompts.build_kg_context({"characters": ["沈砚：书生"]})
        self.assertIn("【全书设定参考", text)
        self.assertIn("· 人物：沈砚：书生", text)


# ============================================================
# runner：system 消息 + 分块多轮
# ============================================================

class _RunnerConfig:
    def profile(self, key):
        return {"kind": "online", "name": "stub", "model": "stub-model"}

    def runtime_guard_options(self):
        return {}


class _RoundModels:
    """按轮次脚本返回输出的桩模型；`fail_on` 指定第几轮抛取消。

    与真实 ModelRuntime 一致：正文经 status 回调流式产出（partial 重建依赖它）。
    """

    def __init__(self, outputs, fail_on=0):
        self.outputs = outputs
        self.fail_on = fail_on
        self.calls: list[list[dict]] = []
        self.last_usage = {"input_tokens": 2, "output_tokens": 3}
        self.last_reasoning = ""
        self.last_diagnostics = {}
        self.last_finish_reason = "stop"

    def complete(self, profile, messages, options, status_cb):
        self.calls.append(messages)
        index = len(self.calls)
        if index == self.fail_on:
            status_cb({"type": "delta", "content": "半截"})
            raise TaskCancelled("任务已取消")
        status_cb({"type": "delta", "content": self.outputs[index - 1]})
        return self.outputs[index - 1]


class _RunnerApp:
    def __init__(self, models):
        self.models = models
        self.config = _RunnerConfig()


def _run_task(store, models, input_data, timeout=10.0):
    runner = StudioRunner(_RunnerApp(models), store)
    task = store.create_task(input_data)
    runner.submit(task["id"])
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = store.get_task(task["id"])
        if state["status"] in {"done", "failed", "cancelled"}:
            break
        time.sleep(0.02)
    runner.shutdown()
    return state, task["id"]


def _chunked_input(blocks):
    return {
        "content": "快照内容",
        "model_key": "online:probe",
        "model_name": "stub-model",
        "chunked": {
            "blocks": blocks,
            "prefix": "固定头",
            "continuity_chars": 500,
            "continuity_label": "【已生成改编稿的结尾】",
        },
    }


class RunnerChunkedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StudioStore(Path(self.tmp.name))

    def test_chunked_runs_rounds_and_concatenates(self):
        models = _RoundModels(["输出一", "输出二", "输出三"])
        state, task_id = _run_task(self.store, models, _chunked_input(["块一", "块二", "块三"]))
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["result"], "输出一\n\n输出二\n\n输出三")
        self.assertEqual(len(models.calls), 3)
        # system 缺省时没有 system 消息；第 1 轮不带衔接，第 2/3 轮带。
        self.assertEqual(models.calls[0][0]["role"], "user")
        self.assertNotIn("【已生成改编稿的结尾】", models.calls[0][-1]["content"])
        for index in (1, 2):
            message = models.calls[index][-1]["content"]
            self.assertIn("【已生成改编稿的结尾】", message)
            self.assertIn(f"输出{['一', '二', '三'][index - 1]}", message)
        self.assertNotIn("块一", models.calls[1][-1]["content"], "每轮只带自己的块")
        self.assertIn("块三", models.calls[2][-1]["content"])
        # 事件流重建的 partial 与最终结果同构；用量跨轮累加。
        self.assertEqual(self.store.partial_from_events(task_id), state["result"])
        self.assertEqual(state["usage"]["input_tokens"], 6)
        statuses = [row["message"] for row in self.store.read_events(task_id, after=0)
                    if row["type"] == "status"]
        self.assertIn("正在生成第 2/3 块", statuses)
        self.assertIn("正在生成第 3/3 块", statuses)

    def test_chunked_cancel_keeps_completed_rounds(self):
        models = _RoundModels(["输出一", "输出二", "输出三"], fail_on=3)
        state, _ = _run_task(self.store, models, _chunked_input(["块一", "块二", "块三"]))
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["result_partial"], "输出一\n\n输出二\n\n半截")

    def test_system_message_prepended_for_single_round(self):
        models = _RoundModels(["结果"])
        state, _ = _run_task(self.store, models, {
            "content": "正文", "model_key": "online:probe", "model_name": "stub-model",
            "system": "【全书设定参考】\n· 人物：沈砚",
        })
        self.assertEqual(state["status"], "done")
        self.assertEqual(models.calls[0][0], {"role": "system", "content": "【全书设定参考】\n· 人物：沈砚"})
        self.assertEqual(models.calls[0][1]["role"], "user")

    def test_no_system_key_keeps_legacy_shape(self):
        models = _RoundModels(["结果"])
        state, _ = _run_task(self.store, models, {
            "content": "正文", "model_key": "online:probe", "model_name": "stub-model",
        })
        self.assertEqual(state["status"], "done")
        self.assertEqual(models.calls[0], [{"role": "user", "content": "正文"}])


# ============================================================
# service：分镜生图 + 改编分块 + 图谱注入
# ============================================================

class _Jobs:
    def __init__(self):
        self.submitted: list[dict] = []
        self.tasks: dict[str, dict] = {}

    def submit(self, input_data):
        task = {
            "id": f"st-novel-{len(self.submitted) + 1:04d}",
            "status": "queued",
            "input": dict(input_data),
            "title": input_data.get("title", ""),
        }
        self.submitted.append(input_data)
        self.tasks[task["id"]] = task
        return task

    def task(self, task_id):
        return self.tasks.get(task_id)


class _Config:
    def model_profiles(self):
        return [{"model_key": "online:probe", "model": "probe-default", "context_window": 0}]


class _Paths:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Studio:
    def __init__(self):
        self.jobs = _Jobs()


class _App:
    def __init__(self, data_dir):
        self.paths = _Paths(data_dir)
        self.config = _Config()
        self.studio = _Studio()


class Phase4ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = _App(self.root)
        self.service = NovelService(self.app)
        body = "场景描写正文。" * 40
        text = "".join(f"第{i}章 标题{i}\n{body}\n" for i in range(1, 3 + 1))
        staged = self.service.store.stage_import(
            filename="书.txt", data=text.encode("utf-8"), max_len=6000,
        )
        self.book = self.service.store.create_book(
            import_id=staged["import_id"], picked=[1, 2, 3], title="长夜灯",
        )
        self.book_id = self.book["id"]

    def _seed_task(self, task_id, stage):
        """api_apply 会核对任务：直接在替身里登记一条该阶段的已完成任务。"""
        self.app.studio.jobs.tasks[task_id] = {
            "id": task_id, "status": "done",
            "input": {"novel": {"book_id": self.book_id, "stage": stage}, "user_request": ""},
        }
        return task_id

    def _save_script(self, scenes=None):
        scenes = scenes or [
            "INT. 老宅书房 - 夜\n沈砚推门而入，油灯下翻看信件。",
            "EXT. 长桥 - 黄昏\n沈砚与林晚并肩走过长桥，夕阳把影子拉得很长。",
        ]
        self._seed_task("st-script-1", "script")
        _, status = self.service.api_apply(self.book_id, {
            "stage": "script", "task_id": "st-script-1", "text": "\n\n".join(scenes),
        })
        self.assertEqual(status, 200)

    def _save_elements(self):
        self._seed_task("st-elements-1", "elements")
        _, status = self.service.api_apply(self.book_id, {
            "stage": "elements", "task_id": "st-elements-1",
            "text": "## 人物\n· 沈砚：书生，性格沉稳，负责查明真相\n## 场景\n· 老宅书房：藏有信件的关键地点\n## 道具\n· 油灯：夜里唯一的光源",
        })
        self.assertEqual(status, 200)

    # ---- 4C 分镜生图 ----

    def test_storyboard_requires_saved_scenes(self):
        payload, status = self.service.api_stage({
            "book_id": self.book_id, "stage": "storyboard",
            "model_key": "online:probe", "model_name": "probe-default",
        })
        self.assertEqual(status, 400)
        self.assertIn("剧本场次", payload["error"])

    def test_storyboard_submit_carries_scenes_and_system(self):
        self._save_script()
        self._save_elements()
        payload, status = self.service.api_stage({
            "book_id": self.book_id, "stage": "storyboard",
            "model_key": "online:probe", "model_name": "probe-default",
        })
        self.assertEqual(status, 201)
        task = payload["task"]
        self.assertEqual(task["title"], "《长夜灯》· 分镜生图")
        input_data = task["input"]
        self.assertEqual(input_data["module"], "novel")
        self.assertEqual(input_data["origin_label"], "创作场")
        self.assertEqual(input_data["novel"]["stage"], "storyboard")
        self.assertEqual(input_data["novel"]["scene_ids"], [1, 2])
        self.assertEqual(len(input_data["novel"]["scene_snapshot"]), 2)
        self.assertIn("【全书设定参考", input_data["system"])
        self.assertIn("【第 1 场】INT. 老宅书房 - 夜", input_data["content"])
        self.assertNotIn("chunked", input_data)
        # 预检与提交同口径：budget 带实体数（人物+场景+道具共 3 条）。
        preview, _ = self.service.api_stage_preview({
            "book_id": self.book_id, "stage": "storyboard",
            "model_key": "online:probe",
        })
        self.assertEqual(preview["budget"]["kg_entities"], 3)

    def test_storyboard_scene_subset_and_unknown_scene(self):
        self._save_script()
        payload, status = self.service.api_stage_preview({
            "book_id": self.book_id, "stage": "storyboard", "scene_ids": [2],
            "model_key": "online:probe",
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["budget"]["kg_entities"], 0)
        # 选了 1 场就只按 1 场组装；场次内容本身在 prompts 层已覆盖。
        self.assertEqual(payload["stage"], "storyboard")
        bad, bad_status = self.service.api_stage_preview({
            "book_id": self.book_id, "stage": "storyboard", "scene_ids": [9],
            "model_key": "online:probe",
        })
        self.assertEqual(bad_status, 400)
        self.assertIn("场次不存在", bad["error"])

    def test_storyboard_apply_is_rejected(self):
        self._save_script()
        payload, status = self.service.api_apply(self.book_id, {
            "stage": "storyboard", "task_id": "st-x", "text": "【第 1 场】……",
        })
        self.assertEqual(status, 400)
        self.assertIn("不写回作品", payload["error"])

    def test_parse_storyboard_failure_is_not_fabricated(self):
        rows, parsed = prompts.parse_storyboard("模型没有按格式输出的一段话")
        self.assertFalse(parsed)
        self.assertEqual(rows, [])

    # ---- 4A 改编分块 ----

    def test_long_adapt_goes_chunked_with_snapshot(self):
        long_body = "需要改写的正文句子。" * 900  # ≫ 3000
        text = f"第1章 标题1\n{long_body}\n" + "".join(
            f"第{i}章 标题{i}\n短。\n" for i in range(2, 4))
        staged = self.service.store.stage_import(
            filename="长书.txt", data=text.encode("utf-8"), max_len=6000,
        )
        long_book = self.service.store.create_book(
            import_id=staged["import_id"], picked=[1], title="长书",
        )
        payload, status = self.service.api_stage({
            "book_id": long_book["id"], "stage": "adapt", "chapter_ids": [1],
            "body_chars": 3000,
            "model_key": "online:probe", "model_name": "probe-default",
        })
        self.assertEqual(status, 201)
        input_data = payload["task"]["input"]
        chunked = input_data["chunked"]
        self.assertGreater(len(chunked["blocks"]), 1)
        for block in chunked["blocks"]:
            self.assertLessEqual(len(block), 3000)
        self.assertTrue(any("块顺序生成" in row for row in payload["warnings"]))
        # content 快照保留静态拼合，重试原样重放。
        self.assertIn("【本次稿件】", input_data["content"])
        retry = self.service.api_stage({
            "book_id": long_book["id"], "stage": "adapt", "chapter_ids": [1],
            "body_chars": 3000,
            "model_key": "online:probe", "model_name": "probe-default",
        })
        self.assertEqual(retry[1], 201)

    def test_short_adapt_stays_single_shot(self):
        payload, status = self.service.api_stage({
            "book_id": self.book_id, "stage": "adapt", "chapter_ids": [1],
            "model_key": "online:probe", "model_name": "probe-default",
        })
        self.assertEqual(status, 201)
        self.assertNotIn("chunked", payload["task"]["input"])

    # ---- 4B-1 图谱注入 ----

    def test_elements_injected_as_system_for_stages(self):
        self._save_elements()
        for stage in ("elements", "outline", "script"):
            payload, status = self.service.api_stage({
                "book_id": self.book_id, "stage": stage, "chapter_ids": [1],
                "model_key": "online:probe", "model_name": "probe-default",
            })
            self.assertEqual(status, 201)
            input_data = payload["task"]["input"]
            self.assertIn("【全书设定参考", input_data["system"])
            self.assertIn("沈砚", input_data["system"])
            self.assertGreater(payload["budget"]["kg_entities"], 0)
            self.assertIn("kg_entities", payload["sources"])

    def test_no_elements_no_system(self):
        payload, status = self.service.api_stage({
            "book_id": self.book_id, "stage": "elements", "chapter_ids": [1],
            "model_key": "online:probe", "model_name": "probe-default",
        })
        self.assertEqual(status, 201)
        self.assertNotIn("system", payload["task"]["input"])
        self.assertEqual(payload["budget"]["kg_entities"], 0)


if __name__ == "__main__":
    unittest.main()
