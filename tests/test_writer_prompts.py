# -*- coding: utf-8 -*-
"""护栏：织书台提示词与严格解析（实施计划 §6、§12.1）。

覆盖：纯 JSON / 单代码围栏接受、解释文字拒绝；缺字段、错误类型、数量不足
或空正文保留诊断，章号按目录顺序整理；上下文槽位限长与预算报告。
"""

import json
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

    def test_genre_accepts_explicit_aliases_but_not_subcat(self):
        for key in ("category", "大类"):
            parsed = prompts.parse_genre('{"' + key + '":"悬疑","subCat":"本格"}')
            self.assertEqual(parsed["cat"], "悬疑")
        with self.assertRaises(PromptError):
            prompts.parse_genre('{"subCat":"本格"}')

    def test_wrapped_candidate_lists(self):
        self.assertEqual(prompts.parse_title_candidates('{"titles":["甲"]}'), ["甲"])
        self.assertEqual(prompts.parse_title_candidates('{"candidates":["乙"]}'), ["乙"])
        self.assertEqual(prompts.parse_blurb_candidates('{"descriptions":["简介"]}'), ["简介"])

        rows = prompts.parse_title_candidates('["书名一","书名二","书名三"]')
        self.assertEqual(rows, ["书名一", "书名二", "书名三"])
        with self.assertRaises(PromptError):
            prompts.parse_title_candidates('[]')
        with self.assertRaises(PromptError):
            prompts.parse_title_candidates('{"not":"array"}')

    def test_wrapped_characters_outline_and_segments(self):
        self.assertEqual(prompts.parse_characters('{"characters":[{"name":"甲"}]}', wanted=1)[0]["name"], "甲")
        outline = '{"outline":[' + ','.join(
            f'{{"no":{i},"title":"第{i}章"}}' for i in range(1, 6)
        ) + ']}'
        self.assertEqual(len(prompts.parse_outline(outline, wanted=5)), 5)
        self.assertEqual(prompts.parse_segs('{"points":["一","二"]}'), ["一", "二"])

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

    def test_characters_keep_relations_and_cap(self):
        """角色生成的 relations 必须被解析保留（跨章连贯性修复计划 §三.A）。"""
        rows = prompts.parse_characters(
            '[{"name":"甲","role":"主角","relations":"与乙：同门师兄，互相提防"},'
            '{"name":"乙","relations":"' + "长" * 1200 + '"}]',
            wanted=2)
        self.assertEqual(rows[0]["relations"], "与乙：同门师兄，互相提防")
        self.assertEqual(rows[1]["relations"], "长" * 1000)  # 上限与 store 归一化一致
        # 旧输出没有 relations 也不炸。
        rows = prompts.parse_characters('[{"name":"丙"}]', wanted=1)
        self.assertEqual(rows[0]["relations"], "")

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


class ContentIssueTests(unittest.TestCase):
    def issue_for(self, parse, raw, **kwargs):
        with self.assertRaises(prompts.ContentIssue) as caught:
            parse(raw, **kwargs)
        issue = caught.exception
        self.assertEqual(issue.raw, raw[:prompts.CONTENT_ISSUE_RAW_MAX])
        self.assertEqual(issue.raw_truncated, len(raw) > prompts.CONTENT_ISSUE_RAW_MAX)
        self.assertTrue(issue.missing)
        self.assertIn("supplement", issue.available_actions)
        self.assertEqual("accept" in issue.available_actions, issue.accepted is not None)
        return issue

    def test_raw_keeps_whitespace_fences_and_full_editor_range(self):
        bad = '  \r\n```json\r\n{"titles":["缺结尾"]\r\n```  \n'
        issue = self.issue_for(prompts.parse_title_candidates, bad)
        self.assertEqual(issue.category, "unrecognized_format")
        self.assertIsNone(issue.partial)
        self.assertIsNone(issue.accepted)
        body = "长" * 200_001
        raw = " \r\n```text\r\n" + body + "\r\n```\t\n"
        issue = self.issue_for(prompts.parse_outline_text, raw)
        self.assertEqual(issue.category, "too_long")
        self.assertEqual(issue.partial, body)
        self.assertEqual(issue.counts, {"actual": len(body), "target": 200_000})
        self.assertFalse(issue.partial_truncated)
        self.assertIsNone(issue.accepted)

        raw = " \t" + "长" * (prompts.CONTENT_ISSUE_RAW_MAX + 10)
        issue = self.issue_for(prompts.parse_freeform, raw)
        self.assertTrue(issue.partial_truncated)
        self.assertEqual(len(issue.partial), prompts.CONTENT_ISSUE_RAW_MAX)
        self.assertEqual(issue.counts["actual"], len(raw.strip()))
        self.assertIsNone(issue.accepted)

    def test_genre_explicit_precedence_and_editable_category(self):
        result = prompts.parse_genre('{"cat":"悬疑","category":"科幻","大类":"奇幻","subCat":"本格"}')
        self.assertEqual((result["cat"], result["sub_cat"]), ("悬疑", "本格"))
        raw = ' \n```json\n{"subCat":"本格","tone":"冷冽","tags":["密室"]}\n``` \t'
        issue = self.issue_for(prompts.parse_genre, raw)
        self.assertEqual(issue.partial, {"cat": "", "subCat": "本格", "tone": "冷冽", "tags": ["密室"]})
        self.assertEqual(issue.missing, ["cat"])
        self.assertEqual(issue.counts, {"actual": 0, "target": 1})
        self.assertIsNone(issue.accepted)
        edited = {**issue.partial, "cat": "悬疑"}
        self.assertEqual(prompts.parse_genre(json.dumps(edited))["sub_cat"], "本格")
        for value in ({"name": "不猜"}, True, "   "):
            with self.subTest(value=value):
                issue = self.issue_for(prompts.parse_genre, json.dumps({"cat": value, "category": "备选"}))
                self.assertIsNone(issue.accepted)

    def test_only_explicit_wrappers_and_strict_json_types(self):
        for parser, raw, kwargs in (
            (prompts.parse_title_candidates, '{"titles":{},"candidates":["乙"]}', {}),
            (prompts.parse_blurb_candidates, '{"titles":["不是简介包装"]}', {}),
            (prompts.parse_characters, '{"other":[{"name":"甲"}]}', {"wanted": 1}),
            (prompts.parse_outline, '{"chapters":null,"outline":[{"title":"甲"}]}', {"wanted": 1}),
            (prompts.parse_split, '{"chapters":{"title":"甲"}}', {}),
            (prompts.parse_segs, '{"points":{"first":"甲"}}', {}),
        ):
            with self.subTest(parser=parser.__name__):
                issue = self.issue_for(parser, raw, **kwargs)
                self.assertEqual(issue.category, "wrong_shape")
                self.assertEqual(issue.partial, json.loads(raw))
                self.assertIsNone(issue.accepted)
        for raw in ('[NaN]', '[Infinity]', '[1e1000]', '{"titles":["甲"],"titles":["乙"]}',
                    r'{"titles":["\ud800"]}'):
            with self.subTest(raw=raw):
                with self.assertRaises(prompts.ContentIssue) as caught:
                    prompts.extract_json_payload(raw)
                self.assertEqual(caught.exception.category, "unrecognized_format")
                self.assertIsNone(caught.exception.accepted)

    def test_string_candidates_count_only_valid_strings(self):
        raw = ' {"titles":[{"title":"不猜"},"书名",null,4]} \n'
        issue = self.issue_for(prompts.parse_title_candidates, raw)
        self.assertEqual(issue.partial, [{"title": "不猜"}, "书名", None, 4])
        self.assertEqual(issue.accepted, ["书名"])
        self.assertEqual(issue.counts, {"actual": 1, "target": 1, "invalid_rows": 3})
        self.assertIsNone(self.issue_for(prompts.parse_blurb_candidates, '{"blurbs":[false]}').accepted)
        empty = self.issue_for(prompts.parse_blurb_candidates, '  {"descriptions":[]}  ')
        self.assertEqual(empty.category, "empty")
        self.assertEqual(empty.counts, {"actual": 0, "target": 1})
        self.assertEqual(empty.partial, [])
        self.assertIsNone(empty.accepted)

    def test_roles_keep_original_order_and_separate_accepted_rows(self):
        edited = [
            {"name": " 甲 ", "identity": "船工"},
            {"name": "  ", "role": "侧写", "goal": "查旧案", "unmodeled": {"note": "不丢"}},
            "乙",
            {"name": ["不转成名字"]},
        ]
        raw = json.dumps({"characters": edited})
        issue = self.issue_for(prompts.parse_characters, raw, wanted=4)
        self.assertEqual(issue.partial, edited)
        self.assertEqual([row["name"] for row in issue.accepted], ["甲", "乙"])
        self.assertEqual(issue.accepted, prompts.parse_characters(json.dumps(issue.accepted), wanted=4,
                                                                require_count=False))
        self.assertEqual(issue.counts, {"actual": 2, "target": 4, "missing_name": 1, "invalid_rows": 1})
        self.assertIn("还缺 2 个有名字的角色", issue.missing)
        self.assertIn("姓名", " ".join(issue.missing))
        # Relaxing the count does not mean discarding a bad row.
        self.issue_for(prompts.parse_characters, raw, wanted=2, require_count=False)
        edited[1]["name"] = "补名"
        self.assertEqual([row["name"] for row in prompts.parse_characters(
            json.dumps(edited[:3]), wanted=4, require_count=False)], ["甲", "补名", "乙"])

    def test_chapters_preserve_missing_title_fields_and_renumber_only_accepted(self):
        rows = [
            {"no": 14, "title": "出港"},
            {"no": 28, "beat": "不能丢的梗概", "hook": "遗失船票"},
            {"no": 14, "title": "返港", "beat": "团聚"},
        ]
        expected = [{"no": 50, "title": "出港", "beat": "", "hook": ""},
                    {"no": 51, "title": "返港", "beat": "团聚", "hook": ""}]
        issue = self.issue_for(prompts.parse_split, json.dumps({"outline": rows}), from_no=50, want_count=3)
        self.assertEqual(issue.partial, rows)
        self.assertEqual(issue.accepted, expected)
        self.assertEqual(issue.counts, {"actual": 2, "target": 3, "missing_title": 1})
        self.assertNotIn("还缺", " ".join(issue.missing))  # default split did not require count
        old = self.issue_for(prompts.parse_outline, json.dumps(rows), wanted=3)
        self.assertEqual(old.partial, rows)
        self.assertEqual(old.accepted, [{**row, "no": i} for i, row in enumerate(expected, 1)])
        self.assertIn("还缺 1 章有标题的内容", old.missing)
        self.issue_for(prompts.parse_outline, json.dumps(rows), wanted=3, require_count=False)
        self.assertEqual(prompts.parse_outline(json.dumps(expected), wanted=3, require_count=False), old.accepted)
        self.assertEqual(prompts.parse_outline(json.dumps(rows[::2]), wanted=2), old.accepted)

    def test_clean_partial_split_requires_opt_in_to_pause(self):
        raw = '[{"title":"出港"}]'
        expected = [{"no": 50, "title": "出港", "beat": "", "hook": ""}]
        self.assertEqual(prompts.parse_split(raw, from_no=50, want_count=3), expected)
        issue = self.issue_for(prompts.parse_split, raw, from_no=50, want_count=3, require_count=True)
        self.assertEqual(issue.accepted, expected)
        self.assertEqual(issue.counts, {"actual": 1, "target": 3})
        self.assertIn("还缺 2 章有标题的内容", issue.missing)
        empty = self.issue_for(prompts.parse_split, '{"chapters":[]}', want_count=3, require_count=True)
        self.assertEqual(empty.category, "empty")
        self.assertIsNone(empty.accepted)

    def test_segments_one_point_is_editable_and_can_be_accepted(self):
        point = "甲" * (prompts.SEG_POINT_CHARS + 2)
        raw = json.dumps({"segments": [point]})
        issue = self.issue_for(prompts.parse_segs, raw)
        self.assertEqual(issue.partial, [point])
        self.assertEqual(issue.counts, {"actual": 1, "target": 2})
        self.assertEqual(issue.accepted, [point[:prompts.SEG_POINT_CHARS]])
        self.assertEqual(issue.accepted, prompts.parse_segs(raw, min_count=1))
        self.assertIsNone(self.issue_for(prompts.parse_segs, '{"points":["",{}]}', min_count=1).accepted)

    def test_state_requires_a_normalizable_fact_without_stringifying_objects(self):
        for payload in ({}, {"events": ["  "]}, {"characters": [{"condition": "受伤"}]},
                        {"relations": [{"between": ["甲"], "change": " "}]},
                        {"knowledge": [{"name": "甲", "known": [{}]}]},
                        {"open_threads": [{"not": "a fact"}]}, {"next_hooks": [1]}):
            with self.subTest(payload=payload):
                issue = self.issue_for(prompts.parse_chapter_state, json.dumps(payload))
                self.assertEqual(issue.category, "empty")
                self.assertEqual(issue.partial, payload)
                self.assertEqual(issue.counts, {"actual": 0, "target": 1})
                self.assertIsNone(issue.accepted)
        payload = {"events": ["发现船票"], "characters": [{"name": "甲"}], "next_hooks": []}
        self.assertEqual(prompts.parse_chapter_state(json.dumps(payload)), payload)
        malformed = {**payload, "next_hooks": [{}]}
        issue = self.issue_for(prompts.parse_chapter_state, json.dumps(malformed))
        self.assertEqual(issue.partial, malformed)
        self.assertEqual(issue.accepted["events"], ["发现船票"])
        self.assertNotIn("next_hooks", issue.accepted)
        self.assertEqual(prompts.parse_chapter_state(json.dumps(issue.accepted)), issue.accepted)

    def test_text_problems_do_not_offer_accept(self):
        for parse, raw, kwargs in (
            (prompts.parse_draft, " \r\n半截正文。 \t", {"finish_reason": " LENGTH "}),
            (prompts.parse_draft, "半截正文。", {"finish_reason": "MAX_TOKENS"}),
            (prompts.parse_draft, "半截正文。", {"finish_reason": "max_output_tokens"}),
            (prompts.parse_world, "  " + "长" * 30_001, {}),
            (prompts.parse_world, " \r\n```md\r\n   \r\n``` \t", {}),
            (prompts.parse_outline_text, " \t", {}),
        ):
            with self.subTest(parse=parse.__name__, kwargs=kwargs):
                issue = self.issue_for(parse, raw, **kwargs)
                self.assertIsNone(issue.accepted)
                if kwargs:
                    self.assertEqual(issue.category, "truncated")
                    self.assertEqual(issue.partial, "半截正文。")
                elif parse is prompts.parse_world and len(raw) > 30_000:
                    self.assertEqual(issue.category, "too_long")
                    self.assertEqual(issue.partial, raw.strip())
                else:
                    self.assertEqual(issue.category, "empty")
        self.assertEqual(prompts.parse_freeform('  ```json\n{"cat":"原样"}\n```  '),
                         '```json\n{"cat":"原样"}\n```')
        raw = "字" * prompts.CHAPTER_CONTENT_MAX_CHARS
        self.assertEqual(prompts.parse_draft(raw), raw)
        issue = self.issue_for(prompts.parse_draft, raw + "多", finish_reason="LENGTH")
        self.assertEqual(issue.category, "too_long")
        self.assertIn("补完正文结尾", issue.missing)
        self.assertIsNone(issue.accepted)


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

    def test_mode_prompts_match_parser_expectations(self):
        genre = prompts.build_step_user("genre", self._setup())
        self.assertIn('"cat":"大类"', genre)
        self.assertIn("cat（题材大类）必须是非空字符串", genre)
        plain_genre = prompts.build_step_user("genre", self._setup(), output_mode_value="freeform")
        self.assertNotIn('"cat"', plain_genre)
        self.assertIn("不要输出 JSON", plain_genre)
        for step in ("world", "outline_text", "outline_review", "draft"):
            with self.subTest(step=step):
                self.assertIn("不要套 JSON", prompts.stage_protocol(step, "json"))
        state = prompts.build_state_user(chapter_no=1, content="正文")
        self.assertIn("open_threads", state)
        plain_state = prompts.build_state_user(chapter_no=1, content="正文", output_mode_value="freeform")
        self.assertIn("自然文本", plain_state)
        self.assertNotIn("open_threads", plain_state)

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

    def test_segment_planning_prompt_does_not_ask_for_chapter_prose(self):
        chapter = {"no": 2, "title": "第二章", "beat": "本章要发生的事"}
        structured = prompts.build_draft_context(self._setup(), chapter, mode="whole", step="segs")
        self.assertIn("当前阶段：段落要点", structured["content"])
        self.assertIn('["第一段要点", "第二段要点"]', structured["content"])
        self.assertIn("本章要发生的事", structured["content"])
        self.assertNotIn("完整正文", structured["content"])
        self.assertIn("chapter_brief", [slot["key"] for slot in structured["slots"]])
        freeform = prompts.build_draft_context(self._setup(), chapter, mode="whole", step="segs",
                                               output_mode_value="freeform")
        self.assertIn("不要写正文或输出 JSON", freeform["content"])
        self.assertNotIn("JSON 数组", freeform["content"])

    def test_previous_excerpt_label_continuity_and_omitted_reason(self):
        for mode in ("whole", "segment", "continue"):
            built = prompts.build_draft_context(
                self._setup(), {"no": 2, "title": "章二"}, mode=mode,
                previous_chapter={"status": "included", "chapter_no": 1,
                                  "current_version_id": "cv-aaaaaaaaaaaa", "revision": 3,
                                  "title": "章一", "excerpt": "原文首尾"})
            self.assertIn("首尾截取，非总结", built["content"])
            self.assertIn("人物位置、时间、情绪与未决事件", built["content"])
            slot = next(slot for slot in built["slots"] if slot["key"] == "prev_tail")
            self.assertEqual(slot["current_version_id"], "cv-aaaaaaaaaaaa")
        built = prompts.build_draft_context({}, {"no": 2}, mode="whole",
                                           previous_chapter={"status": "unavailable", "excerpt": ""})
        slot = next(slot for slot in built["slots"] if slot["key"] == "prev_tail")
        self.assertFalse(slot["included"])
        self.assertIn("不可读取", slot["reason"])
        report = prompts.budget_report(content=built["content"], slots=built["slots"], context_window=100000)
        self.assertIn(slot["reason"], [item["reason"] for item in report["omitted"]])

    def test_chapter_head_summary_is_excerpt_not_summary(self):
        text = "首" * 150 + "中" * 300 + "尾" * 250
        summary = prompts.chapter_head_summary(text)
        self.assertTrue(summary.startswith("首"))
        self.assertTrue(summary.endswith("尾"))
        self.assertIn("……", summary)
        short = prompts.chapter_head_summary("短文")
        self.assertEqual(short, "短文")


class DraftCharacterSelectionTests(unittest.TestCase):
    """正文人物摘要：背景/关系进提示词、点名配角不丢、无关配角可按预算排除。"""

    def _setup(self, *, sides=8, background_len=0, relations="与张三是旧识"):
        chars = [{"name": "张三", "role": "lead", "identity": "船匠",
                  "background": "船坞出身", "relations": "与李四结盟"}]
        for i in range(1, sides + 1):
            chars.append({
                "name": f"配角{i}", "role": "side", "identity": f"行当{i}",
                "background": "背" * (background_len or 30),
                "relations": relations,
            })
        return {"idea": "创意", "characters": chars}

    def test_background_and_relations_enter_draft_context(self):
        setup = self._setup(sides=1)
        setup["characters"][0]["intro"] = "船坞出身，藏着一段旧案"
        built = prompts.build_draft_context(
            setup, {"no": 2, "title": "第二章", "beat": "梗概"}, mode="whole")
        self.assertIn("身份：船匠", built["content"])
        self.assertIn("背景：船坞出身，藏着一段旧案", built["content"])
        self.assertIn("关系：与李四结盟", built["content"])
        slot = next(s for s in built["slots"] if s["key"] == "characters")
        self.assertTrue(slot["included"])

    def test_mentioned_side_survives_six_plus_roster(self):
        """超过 6 人时，本章梗概点名的配角必须进上下文（验收 §五）。"""
        setup = self._setup(sides=8)
        setup["characters"][3]["name"] = "王五"
        built = prompts.build_draft_context(
            setup, {"no": 3, "title": "第三章", "beat": "王五在码头拦下张三"}, mode="whole")
        self.assertIn("王五", built["content"])
        slot = next(s for s in built["slots"] if s["key"] == "characters")
        self.assertNotIn("王五", slot.get("excluded_names") or [])

    def test_irrelevant_sides_excluded_by_budget_and_reported(self):
        """预算不足时无关配角被排除且如实报告；点名配角仍要带上。"""
        setup = self._setup(sides=30, background_len=200)
        setup["characters"][5]["name"] = "赵六"
        built = prompts.build_draft_context(
            setup, {"no": 3, "title": "第三章", "beat": "赵六现身"}, mode="whole")
        slot = next(s for s in built["slots"] if s["key"] == "characters")
        self.assertIn("赵六", built["content"])
        self.assertTrue(slot.get("excluded_names"), "无关配角应被预算排除并报告")
        self.assertNotIn("赵六", slot["excluded_names"])
        self.assertLessEqual(len(built["content"]), 6000)

    def test_long_fields_keep_head_not_random_chop(self):
        """单字段超长保留开头加省略号；身份/背景/关系不被整段挤掉（验收 §五）。"""
        setup = {"idea": "创意", "characters": [{
            "name": "张三", "role": "lead", "identity": "身" * 300,
            "background": "背" * 500, "relations": "关" * 500,
        }]}
        built = prompts.build_draft_context(
            setup, {"no": 1, "title": "第一章", "beat": "梗概"}, mode="whole")
        self.assertIn("身份：" + "身" * 100 + "…", built["content"])
        self.assertIn("背景：" + "背" * 160 + "…", built["content"])
        self.assertIn("关系：" + "关" * 160 + "…", built["content"])

    def test_alias_and_extra_note_mention_counts(self):
        setup = self._setup(sides=2)
        setup["characters"][1]["name"] = "老周"
        setup["characters"][1]["alias"] = "周师傅、铁算盘"
        built = prompts.build_draft_context(
            setup, {"no": 2, "title": "第二章", "beat": "铁算盘来算账"},
            mode="whole", extra_note="多写周师傅的算盘声")
        self.assertIn("老周", built["content"])


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
