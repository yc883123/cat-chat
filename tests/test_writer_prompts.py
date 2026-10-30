# -*- coding: utf-8 -*-
"""护栏：织书台提示词与严格解析（实施计划 §6、§12.1）。

覆盖：纯 JSON / 单代码围栏接受、解释文字拒绝；缺字段、错误类型、数量不足、
重复章号、空正文一律如实失败；上下文槽位限长与预算报告（含全书正文永不进入）。
"""

import sys
import unittest

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.writer import prompts  # noqa: E402
from naiba.writer.prompts import PromptError  # noqa: E402


class ExtractJsonTests(unittest.TestCase):
    def test_pure_json_and_single_fence_accepted(self):
        self.assertEqual(prompts.extract_json_payload('{"a":1}'), {"a": 1})
        self.assertEqual(
            prompts.extract_json_payload('```json\n["x"]\n```'), ["x"])
        self.assertEqual(prompts.extract_json_payload('```\n{"b":2}\n```'), {"b": 2})

    def test_prose_and_multi_fence_rejected(self):
        with self.assertRaises(PromptError):
            prompts.extract_json_payload("好的，这是结果：{\"a\":1}")
        with self.assertRaises(PromptError):
            prompts.extract_json_payload("```json\n[1]\n```\n```json\n[2]\n```")
        with self.assertRaises(PromptError):
            prompts.extract_json_payload("")
        with self.assertRaises(PromptError):
            prompts.extract_json_payload("不是 JSON 的回答")


class ParseStepTests(unittest.TestCase):
    def test_genre_requires_cat(self):
        parsed = prompts.parse_genre('{"channel":"male","cat":"玄幻","subCat":"东方玄幻",'
                                     '"tone":"热血","hook":"卖点","tags":["a","a","b","c","d","e"]}')
        self.assertEqual(parsed["cat"], "玄幻")
        self.assertEqual(parsed["channel"], "male")
        self.assertEqual(len(parsed["tags"]), 5)  # 去重 + 上限 5
        with self.assertRaises(PromptError):
            prompts.parse_genre('{"channel":"male"}')
        with self.assertRaises(PromptError):
            prompts.parse_genre('["数组不算对象"]')

    def test_title_candidates(self):
        rows = prompts.parse_title_candidates('["书名一","书名二","书名三"]')
        self.assertEqual(rows, ["书名一", "书名二", "书名三"])
        with self.assertRaises(PromptError):
            prompts.parse_title_candidates('[]')
        with self.assertRaises(PromptError):
            prompts.parse_title_candidates('{"not":"array"}')

    def test_blurb_candidates(self):
        rows = prompts.parse_blurb_candidates('["简介一","简介二","简介三"]')
        self.assertEqual(len(rows), 3)
        with self.assertRaises(PromptError):
            prompts.parse_blurb_candidates('[]')

    def test_world_plain_text_and_fence(self):
        self.assertEqual(prompts.parse_world("直接正文"), "直接正文")
        self.assertEqual(prompts.parse_world("```\n围栏正文\n```"), "围栏正文")
        with self.assertRaises(PromptError):
            prompts.parse_world("   ")

    def test_characters_count_must_be_met(self):
        rows = prompts.parse_characters(
            '[{"name":"甲","role":"主角"},{"name":"乙"}]', wanted=2)
        self.assertEqual([r["name"] for r in rows], ["甲", "乙"])
        # 数量不足如实失败，不补空角色。
        with self.assertRaises(PromptError):
            prompts.parse_characters('[{"name":"甲"}]', wanted=4)
        # dict 包裹形式兼容。
        rows = prompts.parse_characters('{"characters":[{"name":"丙"}]}', wanted=1)
        self.assertEqual(rows[0]["name"], "丙")
        # 缺名的条目不计入数量。
        with self.assertRaises(PromptError):
            prompts.parse_characters('[{"role":"无名"},{"name":"丁"}]', wanted=2)

    def test_outline_requires_chapters_and_count(self):
        payload = '{"chapters":[' + ",".join(
            f'{{"no":{i},"title":"第{i}章","beat":"梗概{i}","hook":"钩{i}"}}'
            for i in range(1, 11)) + "]}"
        rows = prompts.parse_outline(payload, wanted=10)
        self.assertEqual([row["no"] for row in rows], list(range(1, 11)))
        with self.assertRaises(PromptError):
            prompts.parse_outline(payload, wanted=12)  # 数量不足如实失败
        with self.assertRaises(PromptError):
            prompts.parse_outline('{"chapters":[]}', wanted=5)
        with self.assertRaises(PromptError):
            prompts.parse_outline('[1,2,3]', wanted=3)

    def test_segs_two_to_four(self):
        self.assertEqual(prompts.parse_segs('["一","二"]'), ["一", "二"])
        with self.assertRaises(PromptError):
            prompts.parse_segs('["只有一段"]')
        # 超过 4 段只保留前 4 段（分段正文最多 4 段的硬上限）。
        self.assertEqual(prompts.parse_segs('["一","二","三","四","五"]'),
                         ["一", "二", "三", "四"])

    def test_draft_truncation_and_empty(self):
        self.assertEqual(prompts.parse_draft("正文"), "正文")
        with self.assertRaises(PromptError):
            prompts.parse_draft("  ")
        with self.assertRaises(PromptError):
            prompts.parse_draft("写到一半", finish_reason="length")


class BuildStepUserTests(unittest.TestCase):
    def _setup(self):
        return {
            "idea": "一句话创意",
            "genre": {"cat": "玄幻", "sub_cat": "东方玄幻", "tone": "热血"},
            "genre_tags": ["废柴流"],
            "title": "书名",
            "blurb": "简介",
            "worldview": "世界观" * 100,
            "characters": [{"name": "张三", "role": "主角", "identity": "身份", "goal": "目标"}],
            "style": "web",
            "constraints": {"vibe": "", "avoid": "不要系统文"},
        }

    def test_steps_embed_context_slots(self):
        setup = self._setup()
        for step in prompts.SETUP_STEPS:
            text = prompts.build_step_user(step, setup, char_count=3, chapter_count=12)
            self.assertIn("一句话创意", text)
        outline_text = prompts.build_step_user("outline", setup, char_count=3, chapter_count=12)
        self.assertIn("世界观", outline_text)
        self.assertIn("张三", outline_text)
        self.assertIn("12 章", outline_text)
        chars_text = prompts.build_step_user("chars", setup, char_count=3)
        self.assertIn("3 个角色", chars_text)

    def test_unknown_step_rejected(self):
        with self.assertRaises(PromptError):
            prompts.build_step_user("not-a-step", {})

    def test_long_worldview_is_clamped(self):
        setup = self._setup()
        setup["worldview"] = "长" * 5000
        text = prompts.build_step_user("outline", setup)
        self.assertLess(len(text), 3000)
        self.assertIn("…", text)


class DraftContextTests(unittest.TestCase):
    def _setup(self):
        return {
            "idea": "创意",
            "worldview": "世界观内容",
            "characters": [{"name": f"角色{i}", "role": "配角"} for i in range(8)],
            "style": "sus",
            "constraints": {"vibe": "旧工业城", "avoid": ""},
        }

    def test_slots_and_omissions(self):
        setup = self._setup()
        built = prompts.build_draft_context(
            setup, {"no": 3, "title": "第三章", "beat": "本章梗概", "hook": "章末钩子"},
            mode="whole", prev_tail="上一章结尾内容",
        )
        keys = [slot["key"] for slot in built["slots"]]
        self.assertIn("idea", keys)
        self.assertIn("worldview", keys)
        self.assertIn("characters", keys)
        self.assertIn("prev_tail", keys)
        self.assertIn("第三章", built["content"])
        self.assertIn("本章梗概", built["content"])
        self.assertIn("上一章结尾内容", built["content"])

    def test_empty_setup_slots_marked_omitted(self):
        built = prompts.build_draft_context(
            {}, {"no": 1, "title": "第一章"}, mode="whole")
        omitted = [slot["key"] for slot in built["slots"] if not slot["included"]]
        self.assertIn("worldview", omitted)
        self.assertIn("characters", omitted)

    def test_continue_mode_includes_draft_tail(self):
        built = prompts.build_draft_context(
            self._setup(), {"no": 1, "title": "第一章", "beat": "梗概"},
            mode="continue", draft_tail="当前稿的最后一段")
        self.assertIn("当前稿的最后一段", built["content"])
        self.assertIn("继续写", built["content"])

    def test_segment_mode_marks_current_point(self):
        points = ["第一段要点", "第二段要点", "第三段要点"]
        built = prompts.build_draft_context(
            self._setup(), {"no": 1, "title": "第一章", "beat": "梗概"},
            mode="segment", seg_points=points, seg_index=1)
        self.assertIn("▶ 第二段要点", built["content"])
        self.assertIn("· 第一段要点", built["content"])
        self.assertIn("第 1 章第 2 段", built["content"])

    def test_chapter_head_summary_is_excerpt_not_summary(self):
        text = "首" * 150 + "中" * 300 + "尾" * 250
        summary = prompts.chapter_head_summary(text)
        self.assertTrue(summary.startswith("首"))
        self.assertTrue(summary.endswith("尾"))
        self.assertIn("……", summary)
        short = prompts.chapter_head_summary("短文")
        self.assertEqual(short, "短文")


class BudgetTests(unittest.TestCase):
    def test_budget_known_fits(self):
        report = prompts.budget_report(
            content="字" * 1000, slots=[{"key": "idea", "label": "创意", "included": True}],
            context_window=100000)
        self.assertTrue(report["fits"])
        self.assertTrue(report["context_known"])
        self.assertEqual(report["omitted"], [])
        self.assertGreater(report["estimated_tokens"], 0)

    def test_budget_known_overflow_blocks(self):
        report = prompts.budget_report(
            content="字" * 500000, slots=[], context_window=10000)
        self.assertFalse(report["fits"])
        self.assertTrue(report["suggestions"])

    def test_budget_unknown_window(self):
        report = prompts.budget_report(content="字" * 100, slots=[], context_window=0)
        self.assertFalse(report["context_known"])
        self.assertTrue(report["fits"])  # 未知时不在预算层拦截，交给确认流程

    def test_budget_reports_omitted_slots(self):
        report = prompts.budget_report(
            content="x", slots=[{"key": "worldview", "label": "世界观", "included": False,
                                 "reason": "设定里还没有世界观"}],
            context_window=8000)
        self.assertEqual(len(report["omitted"]), 1)
        self.assertEqual(report["omitted"][0]["reason"], "设定里还没有世界观")


class StyleTests(unittest.TestCase):
    def test_style_presets(self):
        self.assertEqual(prompts.style_prompt("web"), "")
        self.assertIn("金庸式", prompts.style_prompt("jinyong"))
        self.assertIn("悬疑", prompts.style_prompt("sus"))
        self.assertEqual(prompts.style_prompt("unknown-style"), "")

    def test_labels(self):
        self.assertEqual(prompts.style_label("light"), "轻松幽默")


if __name__ == "__main__":
    unittest.main()
