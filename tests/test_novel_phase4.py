# -*- coding: utf-8 -*-
"""护栏：拆书匠（module=novel）阶段四——分块续写（4A）、图谱反哺（4B-1）、分镜生图（4C）。

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

    def profile(self, key):
        return {"kind": "online", "provider_id": "probe", "model": "probe-default",
                "base_url": "https://fixture.invalid/v1", "request_format": "openai_chat"}


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

    def test_script_sources_match_selected_characters_and_prior_summaries(self):
        self.service.store.apply_elements(
            self.book_id, task_id="scene-only", raw="## 场景\n· 老宅书房：旧宅里的书房，是保存关键密信的地方",
        )
        self._save_elements()
        self.service.store.update_summaries(self.book_id, {"3": "未来章节的摘要"})
        first, status = self.service.api_stage({
            "book_id": self.book_id, "stage": "script", "chapter_ids": [1],
            "elements_version": 1, "model_key": "online:probe", "model_name": "probe-default",
        })
        self.assertEqual(status, 201, first)
        self.assertFalse(first["sources"]["uses_characters"])
        self.assertFalse(first["sources"]["has_summaries"])
        self.assertNotIn("【人物设定", first["task"]["input"]["content"])
        self.assertNotIn("【前文摘要", first["task"]["input"]["content"])
        self.assertIn("· 场景：老宅书房", first["task"]["input"]["content"])

        self.service.store.update_summaries(self.book_id, {"1": "沈砚在老宅发现信件。"})
        second, status = self.service.api_stage({
            "book_id": self.book_id, "stage": "script", "chapter_ids": [2],
            "elements_version": 2, "model_key": "online:probe", "model_name": "probe-default",
        })
        self.assertEqual(status, 201, second)
        self.assertTrue(second["sources"]["uses_characters"])
        self.assertTrue(second["sources"]["has_summaries"])
        self.assertIn("【人物设定", second["task"]["input"]["content"])
        self.assertIn("第 1 章摘要：沈砚在老宅发现信件。", second["task"]["input"]["content"])
        preview, status = self.service.api_stage_preview({
            "book_id": self.book_id, "stage": "script", "chapter_ids": [1],
            "elements_version": 1, "model_key": "online:probe",
        })
        self.assertEqual(status, 200, preview)
        self.assertFalse(preview["sources"]["uses_characters"])
        self.assertFalse(preview["sources"]["has_summaries"])

    def test_saved_historical_script_connects_import_versions_and_storyboard(self):
        from types import SimpleNamespace

        task_store = StudioStore(self.root)
        self.app.studio.jobs = SimpleNamespace(
            submit=task_store.create_task, task=task_store.get_task,
            result_text=lambda task_id, require_final=True: task_store.artifacts.read(
                task_store.get_task(task_id), require_final=require_final)[0],
        )
        imported, status = self.service.api_import(
            filename="接续验收.txt", data="第1章 雨夜来客\n沈砚推门，翻看油灯下的信件。\n".encode("utf-8"),
        )
        self.assertEqual(status, 200, imported)
        created, status = self.service.api_create_book({
            "import_id": imported["import_id"], "picked": [1], "title": "接续验收",
        })
        self.assertEqual(status, 201, created)
        book_id = created["book"]["id"]

        def submit(stage, **selection):
            payload, code = self.service.api_stage({
                "book_id": book_id, "stage": stage, "chapter_ids": [1],
                "model_key": "online:probe", "model_name": "probe-default", **selection,
            })
            self.assertEqual(code, 201, payload)
            return payload

        def finish_artifact(task_id, text):
            task = task_store.get_task(task_id)
            receipt = task_store.artifacts.write_segment(task, 1, 1, text)
            task_store.artifacts.complete_block(task, 1, receipt, finish_reason="tool_calls")
            return task_store.update_task(task_id, status="done", result="浏览器旧缓存不能替代TXT")

        def finish_and_save(stage, text):
            payload = submit(stage)
            task = finish_artifact(payload["task"]["id"], text)
            saved, code = self.service.api_apply(book_id, {
                "stage": stage, "task_id": task["id"], "text": task["result"],
                "revision": payload["book_revision"],
            })
            self.assertEqual(code, 200, saved)
            return saved["book"]

        finish_and_save("elements", "## 人物\n· 沈砚：旧版书生")
        finish_and_save("elements", "## 人物\n· 陆行：新版行商")
        finish_and_save("outline", "## 第一幕 建置\n旧版大纲：沈砚在老宅发现密信。")
        finish_and_save("outline", "## 第一幕 建置\n新版大纲：陆行到市集买货。")
        historical = submit("script", elements_version=1, outline_version=1)
        frozen = historical["task"]["input"]
        self.assertEqual(frozen["novel"]["elements_version"], 1)
        self.assertEqual(frozen["novel"]["outline_version"], 1)
        self.assertTrue(frozen["novel"]["sources"]["uses_characters"])
        self.assertTrue(frozen["novel"]["sources"]["uses_outline"])
        self.assertIn("沈砚：旧版书生", frozen["content"])
        self.assertIn("旧版大纲：沈砚在老宅发现密信。", frozen["content"])
        self.assertNotIn("陆行", frozen["content"])
        script_text = "INT. 老宅书房 - 夜\n沈砚推门翻看信件。\n\nEXT. 长桥 - 黄昏\n沈砚穿过长桥。"
        finish_artifact(historical["task"]["id"], script_text)
        newer = submit("script", elements_version=2, outline_version=2)

        # 重开后从真实任务存储读取旧任务；保存不能依赖前端仍有 active 指针。
        reopened_store = StudioStore(self.root)
        self.app.studio.jobs = SimpleNamespace(
            submit=reopened_store.create_task, task=reopened_store.get_task,
            result_text=lambda task_id, require_final=True: reopened_store.artifacts.read(
                reopened_store.get_task(task_id), require_final=require_final)[0],
        )
        reopened = NovelService(self.app)
        old_task = reopened_store.get_task(historical["task"]["id"])
        saved, status = reopened.api_apply(book_id, {
            "stage": "script", "task_id": old_task["id"], "text": old_task["result"],
            "revision": newer["book_revision"],
        })
        self.assertEqual(status, 200, saved)
        self.assertEqual(len(saved["book"]["script"]), 2)
        self.assertEqual(saved["book"]["script_versions"][-1]["task_id"], old_task["id"])
        self.assertEqual(reopened_store.get_task(newer["task"]["id"])["status"], "queued")
        storyboard, status = reopened.api_stage({
            "book_id": book_id, "stage": "storyboard",
            "model_key": "online:probe", "model_name": "probe-default",
        })
        self.assertEqual(status, 201, storyboard)
        storyboard_input = storyboard["task"]["input"]
        self.assertEqual(storyboard_input["novel"]["scene_ids"], [1, 2])
        self.assertEqual(storyboard_input["novel"]["chapter_ids"], [])
        self.assertIn("沈砚推门翻看信件。", storyboard_input["content"])
        self.assertIn("沈砚穿过长桥。", storyboard_input["content"])

    def test_storyboard_requires_saved_scenes(self):
        payload, status = self.service.api_stage({
            "book_id": self.book_id, "stage": "storyboard",
            "model_key": "online:probe", "model_name": "probe-default",
        })
        self.assertEqual(status, 400)
        self.assertIn("剧本场次", payload["error"])

    def test_storyboard_preview_and_submit_select_saved_script_version(self):
        self._save_script()
        self.service.store.apply_script(
            self.book_id, task_id="st-script-new",
            text="INT. 新稿场景 - 日\n新版独有正文。",
        )
        base = {"book_id": self.book_id, "stage": "storyboard",
                "model_key": "online:probe", "model_name": "probe-default"}
        preview, status = self.service.api_stage_preview({**base, "script_version": "1"})
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["sources"]["script_version"], 1)
        self.assertEqual(preview["sources"]["scene_ids"], [1, 2])
        selected, status = self.service.api_stage({**base, "script_version": 1})
        self.assertEqual(status, 201, selected)
        self.assertEqual(selected["sources"], preview["sources"])
        self.assertEqual(selected["budget"], preview["budget"])
        frozen = selected["task"]["input"]
        self.assertEqual(frozen["novel"]["script_version"], 1)
        self.assertEqual(frozen["novel"]["sources"]["script_version"], 1)
        self.assertEqual(frozen["novel"]["scene_ids"], [1, 2])
        self.assertIn("沈砚推门而入", frozen["content"])
        self.assertNotIn("新版独有正文", frozen["content"])
        for options in ({}, {"script_version": None}, {"script_version": ""}):
            with self.subTest(options=options):
                preview, status = self.service.api_stage_preview({**base, **options})
                self.assertEqual(status, 200, preview)
                self.assertEqual(preview["sources"]["script_version"], 2)
                self.assertEqual(preview["sources"]["scene_ids"], [1])
                current, status = self.service.api_stage({**base, **options})
                self.assertEqual(status, 201, current)
                self.assertEqual(current["task"]["input"]["novel"]["script_version"], 2)
                self.assertIn("新版独有正文", current["task"]["input"]["content"])
                self.assertNotIn("沈砚推门而入", current["task"]["input"]["content"])
        # 场次号只属于所选版本；当前版没有第 2 场时不能借用历史版的同号场次。
        for version, expected in ((1, 200), (2, 400)):
            result, status = self.service.api_stage_preview({
                **base, "script_version": version, "scene_ids": [2]})
            self.assertEqual(status, expected, result)
            if expected == 400:
                self.assertIn("场次不存在", result["error"])

    def test_storyboard_rejects_invalid_deleted_and_empty_script_versions(self):
        self._save_script()
        book = self.service.store.apply_script(
            self.book_id, task_id="st-script-new", text="INT. 新稿场景 - 日\n新版正文。")
        old_id = book["script_versions"][0]["id"]
        base = {"book_id": self.book_id, "stage": "storyboard",
                "model_key": "online:probe", "model_name": "probe-default"}
        for value in (0, -1, 99, True, False, 1.5, 1.0, "bad", "1.0", [], {}):
            for action in (self.service.api_stage_preview, self.service.api_stage):
                with self.subTest(value=value, action=action.__name__):
                    result, status = action({**base, "script_version": value})
                    self.assertEqual(status, 400, result)
                    self.assertIn("script_version", result["error"])
        self.assertEqual(self.app.studio.jobs.submitted, [])
        # 当前版本的场次删空后，历史版仍可生成，选空版要明确指出版号。
        self.service.store.delete_scene(self.book_id, 1)
        result, status = self.service.api_stage_preview({**base, "script_version": 2})
        self.assertEqual(status, 400, result)
        self.assertIn("所选剧本第 2 版没有场次", result["error"])
        result, status = self.service.api_stage_preview({**base, "script_version": 1})
        self.assertEqual(status, 200, result)
        deleted = self.service.store.delete_records(self.book_id, [{"type": "script", "id": old_id}])
        self.assertTrue(deleted["ok"], deleted)
        for action in (self.service.api_stage_preview, self.service.api_stage):
            result, status = action({**base, "script_version": 1})
            self.assertEqual(status, 400, result)
            self.assertIn("script_version 指定的版本不存在", result["error"])
        self.assertEqual(self.app.studio.jobs.submitted, [])

    def test_storyboard_legacy_script_version_is_readonly_and_preserved_on_save(self):
        self._save_script()
        legacy = self.service.store._load_manifest(self.book_id)
        legacy.pop("script_versions")
        path = self.service.store._book_dir(self.book_id) / "book.json"
        base = {"book_id": self.book_id, "stage": "storyboard",
                "model_key": "online:probe", "model_name": "probe-default"}
        for stored_version, effective in ((None, 1), (0, 1), (7, 7)):
            with self.subTest(stored_version=stored_version):
                if stored_version is None:
                    legacy.pop("script_version", None)
                else:
                    legacy["script_version"] = stored_version
                self.service.store._write_json(path, legacy)
                before = path.read_bytes()
                public = self.service.store.get_book(self.book_id)["book"]
                self.assertEqual(public["script_version"], effective)
                self.assertEqual(public["script_versions"], [])
                for options in ({}, {"script_version": None}, {"script_version": ""},
                                {"script_version": effective}):
                    preview, status = self.service.api_stage_preview({**base, **options})
                    self.assertEqual(status, 200, preview)
                    self.assertEqual(preview["sources"]["script_version"], effective)
                    self.assertEqual(preview["sources"]["scene_ids"], [1, 2])
                self.assertEqual(path.read_bytes(), before)
                saved = self.service.store.apply_script(
                    self.book_id, task_id=f"st-after-legacy-{effective}",
                    text="INT. 新稿场景 - 日\n追加的新稿正文。")
                self.assertEqual([row["version"] for row in saved["script_versions"]],
                                 [effective, effective + 1])
                self.assertIn("沈砚推门而入", saved["script_versions"][0]["script"][0]["content"])

    def test_storyboard_explicit_empty_versions_does_not_revive_stale_script(self):
        self._save_script()
        manifest = self.service.store._load_manifest(self.book_id)
        manifest["script_versions"] = []
        path = self.service.store._book_dir(self.book_id) / "book.json"
        self.service.store._write_json(path, manifest)
        before = path.read_bytes()
        public = self.service.store.get_book(self.book_id)["book"]
        self.assertEqual(public["script_version"], 0)
        base = {"book_id": self.book_id, "stage": "storyboard",
                "model_key": "online:probe", "model_name": "probe-default"}
        for options in ({}, {"script_version": 1}):
            for action in (self.service.api_stage_preview, self.service.api_stage):
                result, status = action({**base, **options})
                self.assertEqual(status, 400, result)
        self.assertEqual(self.app.studio.jobs.submitted, [])
        self.assertEqual(path.read_bytes(), before)

    def test_storyboard_real_retry_keeps_selected_version_after_source_changes(self):
        from types import SimpleNamespace
        from naiba.studio.jobs import StudioJobManager
        self._save_script()
        store = StudioStore(self.root)
        scheduled = []
        jobs = StudioJobManager(self.app, store, SimpleNamespace(submit=scheduled.append))
        self.app.studio.jobs = jobs
        response, status = self.service.api_stage({
            "book_id": self.book_id, "stage": "storyboard", "script_version": 1,
            "model_key": "online:probe", "model_name": "probe-default"})
        self.assertEqual(status, 201, response)
        task = response["task"]
        original_input = store.get_task(task["id"])["input"]
        current = self.service.store.apply_script(
            self.book_id, task_id="st-source-new", text="INT. 新稿场景 - 日\n重试不能用的新正文。")
        self.service.store.update_scene(self.book_id, 1, content="重试不能用的修改正文。")
        deleted = self.service.store.delete_records(self.book_id, [
            {"type": "script", "id": current["script_versions"][0]["id"]}])
        self.assertTrue(deleted["ok"], deleted)
        self.assertEqual([row["version"] for row in deleted["book"]["script_versions"]], [2])
        store.update_task(task["id"], status="failed", error="模拟可重试失败")
        retried = jobs.retry(task["id"])
        self.assertNotEqual(retried["id"], task["id"])
        self.assertEqual(scheduled, [task["id"], retried["id"]])
        self.assertEqual(retried["source_task_id"], task["id"])
        # 从真正持久化的新任务读取，而不是只检查原来的提交字典。
        reopened = StudioStore(self.root).get_task(retried["id"])
        self.assertEqual(reopened["input"]["content"], original_input["content"])
        self.assertEqual(reopened["input"]["novel"], original_input["novel"])
        self.assertEqual(reopened["input"]["novel"]["script_version"], 1)
        self.assertEqual(reopened["input"]["novel"]["sources"]["script_version"], 1)
        self.assertIn("沈砚推门而入", reopened["input"]["content"])
        self.assertNotIn("重试不能用", reopened["input"]["content"])

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
        # 显示名已从「创作场」改为「拆书匠」（内部标识 module="novel" 不变）。
        self.assertEqual(input_data["origin_label"], "拆书匠")
        self.assertEqual(input_data["novel"]["stage"], "storyboard")
        self.assertEqual(input_data["novel"]["scene_ids"], [1, 2])
        self.assertEqual(len(input_data["novel"]["scene_snapshot"]), 2)
        self.assertIn("【全书设定参考", input_data["content"])
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

    def test_storyboard_system_prompt_custom_vs_builtin(self):
        """新模板任务的写法进入 user，system 只保留必要执行约定。"""
        self._save_script()
        template = "你是 H3 出图提示词写手：每场一条，画面描述按镜头语言组织。"
        built = prompts.build_storyboard_content(
            book_title="书", scenes=[{"i": 1, "scene": "INT. A - 夜", "content": "内容"}],
            system_prompt=template,
        )
        self.assertIn(template, built["content"])
        self.assertNotIn(template, built["system"])
        self.assertTrue(built["system_custom"])
        self.assertIn("输出格式", built["content"])
        self.assertIn("【第 1 场】INT. A - 夜", built["content"])
        self.assertNotIn("你是分镜师", built["content"], "自定义写法时内置指令不再发送")
        default = prompts.build_storyboard_content(
            book_title="书", scenes=[{"i": 1, "scene": "INT. A - 夜", "content": "内容"}],
        )
        self.assertIn("你是分镜师", default["content"])
        self.assertNotIn("你是分镜师", default["system"])
        self.assertFalse(default["system_custom"])
        self.assertIn("输出格式", default["content"])

    def test_storyboard_template_over_limit_is_rejected(self):
        self._save_script()
        big = "很" * (prompts.STORYBOARD_SYSTEM_CHARS + 300)
        with self.assertRaisesRegex(prompts.PromptError, "不能超过"):
            prompts.build_storyboard_content(
                book_title="书", scenes=[{"i": 1, "scene": "INT. A - 夜", "content": "内容"}],
                system_prompt=big,
            )

    def test_storyboard_submit_with_template_system_prompt(self):
        self._save_script()
        self._save_elements()
        template = "你是 H3 出图提示词写手：每场一条。"
        payload, status = self.service.api_stage({
            "book_id": self.book_id, "stage": "storyboard",
            "system_prompt": template,
            "model_key": "online:probe", "model_name": "probe-default",
        })
        self.assertEqual(status, 201)
        input_data = payload["task"]["input"]
        self.assertIn(template, input_data["content"])
        self.assertNotIn(template, input_data["system"])
        self.assertIn("【全书设定参考", input_data["content"])
        self.assertTrue(payload["sources"]["system_custom"])
        preview, _ = self.service.api_stage_preview({
            "book_id": self.book_id, "stage": "storyboard",
            "system_prompt": template, "model_key": "online:probe",
        })
        self.assertTrue(preview["sources"]["system_custom"])
        self.assertEqual(preview["budget"]["system_chars"], len(input_data["system"]))

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

    def test_elements_injected_as_reference_material_for_stages(self):
        self._save_elements()
        for stage in ("elements", "outline", "script"):
            payload, status = self.service.api_stage({
                "book_id": self.book_id, "stage": stage, "chapter_ids": [1],
                "model_key": "online:probe", "model_name": "probe-default",
            })
            self.assertEqual(status, 201)
            input_data = payload["task"]["input"]
            self.assertIn("【全书设定参考", input_data["content"])
            self.assertIn("沈砚", input_data["content"])
            self.assertNotIn("沈砚", input_data.get("system", ""))
            self.assertGreater(payload["budget"]["kg_entities"], 0)
            self.assertIn("kg_entities", payload["sources"])

    def test_global_prompt_all_stages_and_independent_config_save(self):
        self._save_script()
        self._save_elements()
        before = self.service.store.get_book(self.book_id)["book"]["revision"]
        response, status = self.service.api_save_system_prompt(self.book_id, {
            "global_system_prompt": "全书固定规则", "global_system_prompt_revision": 0})
        self.assertEqual(status, 200, response)
        self.assertEqual(response["book"]["revision"], before)
        for stage in ("elements", "outline", "script", "adapt", "storyboard"):
            with self.subTest(stage=stage):
                request = {"book_id": self.book_id, "stage": stage, "chapter_ids": [1],
                           "model_key": "online:probe", "model_name": "probe-default"}
                preview, status = self.service.api_stage_preview(request)
                self.assertEqual(status, 200, preview)
                result, status = self.service.api_stage(request)
                self.assertEqual(status, 201, result)
                data = result["task"]["input"]
                self.assertEqual(data["system"].count("全书固定规则"), 1)
                self.assertNotIn("全书固定规则", data["content"])
                self.assertEqual(preview["budget"]["estimated_tokens"], result["budget"]["estimated_tokens"])
                self.assertEqual(data["novel"]["global_system_prompt_revision"], 1)
        before = self.service.store.get_book(self.book_id)["book"]["revision"]
        config = {"prompt_source": "skill", "template_draft": "模板草稿", "skill_ids": ["a", "b", "a"], "revision": 0}
        result, status = self.service.api_save_storyboard_prompt_config(self.book_id, config)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["book"]["revision"], before)
        self.assertEqual(result["book"]["storyboard_prompt_config"]["skill_ids"], ["a", "b"])
        self.assertEqual(self.service.api_save_storyboard_prompt_config(self.book_id, config)[1], 409)
        self.assertEqual(self.service.api_save_system_prompt(self.book_id, {
            "global_system_prompt": "旧窗口", "global_system_prompt_revision": 0})[1], 409)

    def test_storyboard_skill_snapshots_and_both_history_sources(self):
        from naiba.studio.content import ContentSnapshots
        self._save_script()
        self._save_elements()
        rows = []
        for name in ("first", "second"):
            root = self.root / name
            root.mkdir()
            (root / "SKILL.md").write_text(f"{name}完整主文，阅读references/guide.md", encoding="utf-8")
            (root / "references").mkdir()
            (root / "references" / "guide.md").write_text(f"{name}专用参考", encoding="utf-8")
            rows.append({"id": name, "name": name, "root": str(root), "path": str(root / "SKILL.md")})
        catalog = type("Catalog", (), {"scan": lambda _self: rows})()
        snapshots = ContentSnapshots(self.root)
        self.app.studio.freeze_content_input = lambda data: snapshots.normalize_and_freeze(data, catalog)
        self.service.api_save_system_prompt(self.book_id, {
            "global_system_prompt": "全书固定规则", "global_system_prompt_revision": 0})
        body = {"book_id": self.book_id, "stage": "storyboard", "scene_ids": [2],
                "model_key": "online:probe", "model_name": "probe-default"}
        template, status = self.service.api_stage({**body, "prompt_source": "template", "system_prompt": "独有模板规则"})
        self.assertEqual(status, 201, template)
        request = {**body, "prompt_source": "skill", "skill_ids": ["second", "first", "second"]}
        preview, code = self.service.api_stage_preview(request)
        self.assertEqual(code, 200, preview)
        skill, status = self.service.api_stage(request)
        self.assertEqual(status, 201, skill)
        data = skill["task"]["input"]
        self.assertEqual(data["module"], "novel")
        self.assertEqual(data["novel"]["scene_ids"], [2])
        self.assertEqual(data["skill_ids"], ["second", "first"])
        for part in ("全书固定规则", "first完整主文", "second完整主文"):
            self.assertIn(part, data["system"])
        for part in ("独有模板规则", "你是分镜师"):
            self.assertNotIn(part, data["content"] + data["system"])
        self.assertIn("【第 2 场】", data["content"])
        self.assertIn("【全书设定参考", data["content"])
        self.assertEqual(preview["budget"]["estimated_tokens"], skill["budget"]["estimated_tokens"])
        # Both source tasks retain their independent immutable input/history.
        self.assertNotEqual(template["task"]["id"], skill["task"]["id"])
        self.assertIn("独有模板规则", self.app.studio.jobs.tasks[template["task"]["id"]]["input"]["content"])
        (self.root / "first" / "SKILL.md").write_text("更新后的主文", encoding="utf-8")
        frozen = snapshots.normalize_and_freeze(data, catalog)
        self.assertEqual(frozen["system"], data["system"])
        current, status = self.service.api_stage(request)
        self.assertEqual(status, 201, current)
        self.assertIn("更新后的主文", current["task"]["input"]["system"])

    def test_real_studio_skill_loop_reads_both_documents_and_retries_original(self):
        import copy
        import json
        import re
        from naiba.skills.catalog import SkillCatalog
        from naiba.studio.service import StudioService
        from tests.test_studio import _write_min_catalog
        from tests.studio_result_fixture import complete_result_reply, write_result_reply
        self._save_script()
        self._save_elements()
        self.app.paths.public_dir = self.root / "public"
        _write_min_catalog(self.app.paths.public_dir / "studio")
        skill_root = self.root / "skills"
        for name in ("one", "two"):
            folder = skill_root / name
            (folder / "references").mkdir(parents=True)
            (folder / "SKILL.md").write_text(f"---\nname: {name}\n---\n{name}主文。阅读references/same.md。", encoding="utf-8")
            (folder / "references" / "same.md").write_text(f"{name}独立文档", encoding="utf-8")
        self.app.catalog = SkillCatalog([skill_root])
        self.app.config.profile = lambda key: {"kind": "online", "name": "offline", "model": "probe", "context_window": 120000}
        self.app.config.runtime_guard_options = lambda: {"max_steps": 40}
        captured = []
        models = _RoundModels([])
        def complete(_models, profile, messages, options, event):
            captured.append(copy.deepcopy(messages))
            _models.last_usage = {"input_tokens": 2, "output_tokens": 3}
            _models.last_finish_reason = "tool_calls"
            self.assertEqual({row["name"] for row in options["tools"]},
                             {"read_file", "list_directory", "search_files", "write_result_txt", "complete_result_txt"})
            confirmed = complete_result_reply(messages)
            if confirmed:
                return confirmed
            roots = re.findall(r'<skill name="([^"]+)" root="([^"]+)">', messages[0]["content"])
            history = "\n".join(str(message.get("content")) for message in messages[1:])
            for name, root in roots:
                if f"{name}独立文档" not in history:
                    return json.dumps({"type": "tool", "tool": "read_file", "arguments": {"path": str(Path(root) / "references" / "same.md")}})
            answer = "【第 2 场】EXT. 长桥 - 黄昏\n林晚与沈砚的双人远景。"
            return write_result_reply(answer)
        from types import MethodType
        models.complete = MethodType(complete, models)
        self.app.models = models
        studio = StudioService(self.app, lambda **kw: {}, lambda **kw: {})
        self.app.studio = studio
        studio._runner.submit = lambda task_id: None
        self.addCleanup(studio.shutdown)
        ids = [row["id"] for row in self.app.catalog.scan()]
        self.service.api_save_system_prompt(self.book_id, {
            "global_system_prompt": "作品固定规则", "global_system_prompt_revision": 0})
        response, status = self.service.api_stage({
            "book_id": self.book_id, "stage": "storyboard", "scene_ids": [2],
            "prompt_source": "skill", "skill_ids": ids, "model_key": "online:probe", "model_name": "probe"})
        self.assertEqual(status, 201, response)
        task_id = response["task"]["id"]
        studio._runner._run_task(task_id)
        task = studio.jobs.task(task_id)
        self.assertEqual(task["status"], "done", task.get("error"))
        rows, parsed = prompts.parse_storyboard(task["result"])
        self.assertTrue(parsed)
        self.assertEqual([row["scene_no"] for row in rows], [2])
        self.assertIn("作品固定规则", captured[-1][0]["content"])
        self.assertEqual(len(captured), 4)
        self.assertEqual(task["usage"]["requests"], 4)
        self.assertEqual(task["result_artifact"]["status"], "final")
        self.assertEqual(task["input"]["module"], "novel")
        self.service.api_save_system_prompt(self.book_id, {
            "global_system_prompt": "新规则", "global_system_prompt_revision": 1})
        import shutil
        shutil.rmtree(skill_root)
        studio._store.update_task(task_id, status="failed", error="模拟可重试失败")
        retried = studio.jobs.retry(task_id)
        studio._runner._run_task(retried["id"])
        current = studio.jobs.task(retried["id"])
        self.assertEqual(current["status"], "done", current.get("error"))
        self.assertEqual(current["input"]["system"], task["input"]["system"])
        self.assertEqual(current["input"]["novel"], task["input"]["novel"])
        self.assertEqual(len(captured), 8)
        self.assertIn("one独立文档", str(captured[-1]))
        self.assertIn("two独立文档", str(captured[-1]))

    def test_storyboard_source_validation_rejects_mixed_and_empty(self):
        self._save_script()
        base = {"book_id": self.book_id, "stage": "storyboard", "model_name": "probe"}
        for extra in ({"prompt_source": "skill", "skill_ids": []},
                      {"prompt_source": "skill", "skill_ids": ["a"], "system_prompt": "模板"},
                      {"prompt_source": "skill", "skill_ids": ["a"], "template_draft": "隐藏模板"},
                      {"prompt_source": "skill", "skill_ids": ["a"], "item_id": "模板选择"},
                      {"prompt_source": "template", "skill_ids": ["a"]},
                      {"prompt_source": "unknown"}, {"skill_ids": "a"}):
            with self.subTest(extra=extra):
                result, status = self.service.api_stage({**base, **extra})
                self.assertEqual(status, 400, result)
        result, status = self.service.api_stage({**base, "stage": "outline", "chapter_ids": [1],
                                                "prompt_source": "skill", "skill_ids": ["a"]})
        self.assertEqual(status, 400, result)
        self.assertEqual(self.app.studio.jobs.submitted, [])

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
