# -*- coding: utf-8 -*-
"""护栏：交互选项识别（core/choices.py）的行为规格。

保护对象：
- 阶段 1 把 `_detect_choice_groups` 迁出 server.py 到 core/choices.py 时的行为等价性
  （意图收紧、编号剥离、紧凑选项拆分）；server.py 仍保留同名 re-export。
- 2026-09-15「前端选择交互模块」改造新增的规格：长度放宽到 300/1000、选项缩进续行、
  「可多选」→ mode=multi、每题「单选/多选」标识可替代"请选择"、显式 `naiba-choices`
  结构化块优先、无效块回退、以及历史读取不得用空解析结果覆盖有效 metadata。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.core.choices import (  # noqa: E402
    MAX_CHOICE_LEN,
    MAX_PROMPT_LEN,
    _detect_choice_groups,
    _detect_choices,
    detect_choice_groups,
    explicit_choice_groups,
    normalize_choice_groups,
    resolve_message_choice_groups,
)
from server import _detect_choice_groups as server_detect_groups  # noqa: E402  re-export 守门
from server import _detect_choices as server_detect_choices  # noqa: E402  re-export 守门


def _explicit_block(payload: str) -> str:
    return f"这是回复正文。\n\n```naiba-choices\n{payload}\n```\n"


class ChoiceDetectionTests(unittest.TestCase):
    """自然语言识别的既有规格（1.6.6-beta 收紧后的口径，本改造不得回退）。"""

    def test_compact_cjk_same_line(self):
        groups = _detect_choice_groups("请选择语言：1. 中文 2. 英文")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["choices"], ["中文", "英文"])
        self.assertIn("请选择", groups[0]["prompt"])

    def test_numbered_list_with_cue(self):
        groups = _detect_choice_groups("请选择一个方案：\n1. 方案A\n2. 方案B\n3. 方案C")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["choices"], ["方案A", "方案B", "方案C"])

    def test_bullet_with_number_residue_stripped(self):
        groups = _detect_choice_groups("请选择：\n- 1. 安装依赖\n- 2. 跳过")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["choices"], ["安装依赖", "跳过"])

    def test_lettered_english_phrase(self):
        groups = _detect_choice_groups("Pick one:\nA. Option 1\nB. Option 2")
        self.assertEqual(groups[0]["choices"], ["Option 1", "Option 2"])

    def test_which_one_phrase(self):
        groups = _detect_choice_groups("Which one do you prefer?\n1. X\n2. Y")
        self.assertEqual(groups[0]["choices"], ["X", "Y"])

    def test_bare_select_not_a_cue(self):
        groups = _detect_choice_groups("SELECT * FROM users\n1. a\n2. b")
        self.assertEqual(groups, [])

    def test_cue_too_far_from_choices(self):
        groups = _detect_choice_groups("请选择：\n\n\n1. A\n2. B")
        self.assertEqual(groups, [])

    def test_blank_line_gap_within_distance(self):
        groups = _detect_choice_groups("请选择：\n\n1. A\n2. B")
        self.assertEqual(groups[0]["choices"], ["A", "B"])

    def test_non_consecutive_numbering_rejected(self):
        groups = _detect_choice_groups("请选择：\n1. A\n3. C")
        self.assertEqual(groups, [])

    def test_circled_numbers(self):
        groups = _detect_choice_groups("请选择：\n① A\n② B")
        self.assertEqual(groups[0]["choices"], ["A", "B"])

    def test_fenced_code_excluded(self):
        text = "这里没有选项\n```\n1. 代码行\n2. 代码行二\n```"
        self.assertEqual(_detect_choice_groups(text), [])

    def test_skill_manual_not_choices(self):
        text = '使用说明 <skill name="x">……</skill>\n1. 步骤A\n2. 步骤B'
        self.assertEqual(_detect_choice_groups(text), [])

    def test_legacy_first_group_helper(self):
        self.assertEqual(_detect_choices("请选择：\n1. A\n2. B"), ["A", "B"])
        self.assertEqual(_detect_choices("无选项的普通文本"), [])

    def test_server_reexport_still_available(self):
        self.assertIs(server_detect_groups, _detect_choice_groups)
        self.assertIs(server_detect_choices, _detect_choices)


class ChoiceLengthRelaxationTests(unittest.TestCase):
    """题目/选项长度放宽到 300/1000（原 40/40 会把正常提问整组漏掉）。"""

    def test_long_prompt_accepted(self):
        prompt = "请选择（这一题的题目带了不少说明，" + "补充" * 90 + "）："
        self.assertLess(len(prompt), MAX_PROMPT_LEN)
        groups = _detect_choice_groups(f"{prompt}\n1. A\n2. B")
        self.assertEqual(groups[0]["choices"], ["A", "B"])

    def test_over_max_prompt_rejected(self):
        prompt = "请选择：" + "说明" * (MAX_PROMPT_LEN // 2 + 5)
        self.assertGreater(len(prompt), MAX_PROMPT_LEN)
        self.assertEqual(_detect_choice_groups(f"{prompt}\n1. A\n2. B"), [])

    def test_long_choice_accepted(self):
        option = "选项说明" + "细节" * 200
        self.assertLess(len(option), MAX_CHOICE_LEN)
        groups = _detect_choice_groups(f"请选择：\n1. {option}\n2. 短选项")
        self.assertEqual(groups[0]["choices"], [option, "短选项"])

    def test_over_max_choice_rejected(self):
        option = "选项说明" + "细节" * (MAX_CHOICE_LEN // 2 + 5)
        self.assertGreater(len(option), MAX_CHOICE_LEN)
        self.assertEqual(_detect_choice_groups(f"请选择：\n1. {option}\n2. 短选项"), [])

    def test_indented_continuation_joins_previous_choice(self):
        groups = _detect_choice_groups("请选择：\n1. 第一段说明\n   第二段补充\n2. 另一个")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["choices"], ["第一段说明 第二段补充", "另一个"])

    def test_continuation_not_swallowed_beyond_limit(self):
        option = "长" * (MAX_CHOICE_LEN - 2)
        text = f"请选择：\n1. {option}\n   {'续' * 40}\n2. B"
        groups = _detect_choice_groups(text)
        # 续行会让选项越界 → 该行被丢弃（不并入），但**不打断**选项组：
        # 组仍是 [长…, B] 两项，选项原文不截断。
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["choices"], [option, "B"])

    def test_circled_and_named_patterns_untouched(self):
        groups = _detect_choice_groups("请选择：\n方案一：甲\n方案二：乙")
        self.assertEqual(groups[0]["choices"], ["甲", "乙"])


class ChoiceModeTests(unittest.TestCase):
    """mode：自然语言里的"可多选"提示，以及每题自带的单选/多选标识。"""

    def test_default_mode_is_single(self):
        groups = _detect_choice_groups("请选择：\n1. A\n2. B")
        self.assertEqual(groups[0]["mode"], "single")

    def test_multi_hint_marks_group_multi(self):
        groups = _detect_choice_groups("请选择以下几项（可多选）：\n1. A\n2. B")
        self.assertEqual(groups[0]["mode"], "multi")

    def test_select_multiple_english_hint(self):
        groups = _detect_choice_groups("Please select multiple:\n1. A\n2. B")
        self.assertEqual(groups[0]["mode"], "multi")

    def test_mode_marker_replaces_cue_for_later_questions(self):
        text = "请选择首题：\n1. A\n2. B\n\n第二题：文章（多选）\n1. 教程\n2. 评测"
        groups = _detect_choice_groups(text)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["mode"], "single")
        self.assertEqual(groups[1]["prompt"], "第二题：文章（多选）")
        self.assertEqual(groups[1]["choices"], ["教程", "评测"])
        self.assertEqual(groups[1]["mode"], "multi")

    def test_mode_marker_alone_is_enough(self):
        groups = _detect_choice_groups("第一题：视觉（单选）\n1. 写实\n2. 动漫")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["mode"], "single")

    def test_plain_chapter_list_not_a_question(self):
        for text in (
            "## 多选\n1. 事项一\n2. 事项二",
            "## 3. 多选组件实现\n1. 事项一\n2. 事项二",
            "文档正文：这里讲多选组件的用法。\n1. 事项一\n2. 事项二",
        ):
            self.assertEqual(_detect_choice_groups(text), [], text)

    def test_natural_groups_are_marked_source_natural(self):
        groups = detect_choice_groups("请选择：\n1. A\n2. B")
        self.assertTrue(all(group["source"] == "natural" for group in groups))


class ExplicitChoiceBlockTests(unittest.TestCase):
    """```naiba-choices 结构化块：优先于自然语言识别，无效块回退。"""

    BLOCK = _explicit_block(
        '{"choice_groups":['
        '{"prompt":"视觉","choices":["写实","动漫"],"mode":"single"},'
        '{"prompt":"文章","choices":["教程","评测"],"mode":"multi"}]}'
    )

    def test_explicit_block_parsed(self):
        groups = explicit_choice_groups(self.BLOCK)
        self.assertEqual([g["prompt"] for g in groups], ["视觉", "文章"])
        self.assertEqual(groups[0]["choices"], ["写实", "动漫"])
        self.assertEqual(groups[1]["mode"], "multi")
        self.assertTrue(all(g["source"] == "explicit" for g in groups))

    def test_explicit_block_beats_natural_language(self):
        text = '请选择：\n1. A\n2. B\n' + self.BLOCK
        groups = detect_choice_groups(text)
        self.assertEqual([g["prompt"] for g in groups], ["视觉", "文章"])
        self.assertTrue(all(g["source"] == "explicit" for g in groups))

    def test_explicit_block_mode_defaults_to_single(self):
        groups = explicit_choice_groups(
            _explicit_block('{"choice_groups":[{"prompt":"视觉","choices":["A","B"]}]}')
        )
        self.assertEqual(groups[0]["mode"], "single")

    def test_invalid_json_falls_back_to_natural_language(self):
        text = "请选择：\n1. A\n2. B\n" + _explicit_block("{不是 json}")
        groups = detect_choice_groups(text)
        self.assertEqual([g["source"] for g in groups], ["natural"])

    def test_invalid_structure_falls_back_and_is_not_hidden(self):
        body = _explicit_block('{"choice_groups":[]}')
        self.assertEqual(explicit_choice_groups(body), [])
        self.assertEqual(detect_choice_groups(body), [])

    def test_non_block_fence_ignored(self):
        text = '```json\n{"choice_groups":[{"prompt":"视觉","choices":["A","B"]}]}\n```'
        self.assertEqual(explicit_choice_groups(text), [])


class NormalizeChoiceGroupsTests(unittest.TestCase):
    """normalize_choice_groups：三条路径（事件/metadata/历史）共用的唯一规范化入口。"""

    def test_legacy_string_array_becomes_single_group(self):
        groups = normalize_choice_groups(["A", "B"])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["choices"], ["A", "B"])
        self.assertEqual(groups[0]["prompt"], "")
        self.assertEqual(groups[0]["mode"], "single")
        self.assertEqual(groups[0]["source"], "natural")

    def test_wrapper_dict_accepted(self):
        groups = normalize_choice_groups({"choice_groups": [{"prompt": "P", "choices": ["A"]}]})
        self.assertEqual(groups, [{"prompt": "P", "choices": ["A"], "mode": "single", "source": "natural"}])

    def test_mode_normalized_and_unknown_falls_back(self):
        groups = normalize_choice_groups({"choice_groups": [
            {"prompt": "P", "choices": ["A"], "mode": "MULTI"},
            {"prompt": "Q", "choices": ["B"], "mode": "??? "},
        ]})
        self.assertEqual([g["mode"] for g in groups], ["multi", "single"])

    def test_source_argument_and_override(self):
        self.assertEqual(normalize_choice_groups([{"prompt": "P", "choices": ["A"]}], source="explicit")[0]["source"], "explicit")
        self.assertEqual(normalize_choice_groups([{"prompt": "P", "choices": ["A"], "source": "explicit"}])[0]["source"], "explicit")

    def test_choices_are_trimmed_and_empty_dropped(self):
        groups = normalize_choice_groups([{"prompt": " P ", "choices": [" A ", "", None, 3]}])
        self.assertEqual(groups[0]["prompt"], "P")
        self.assertEqual(groups[0]["choices"], ["A", "3"])

    def test_group_without_choices_dropped(self):
        self.assertEqual(normalize_choice_groups([{"prompt": "P", "choices": []}]), [])

    def test_long_text_not_truncated(self):
        long_choice = "长" * 5000
        groups = normalize_choice_groups([{"prompt": "P" * 900, "choices": [long_choice]}])
        self.assertEqual(groups[0]["choices"], [long_choice])
        self.assertEqual(groups[0]["prompt"], "P" * 900)

    def test_invalid_inputs_return_empty(self):
        for value in (None, "", "x", 3, [], {}, {"choice_groups": "x"}):
            self.assertEqual(normalize_choice_groups(value), [], repr(value))


class HistoryChoiceGroupsTests(unittest.TestCase):
    """历史读取口径：有效 metadata 优先，禁止用空解析结果覆盖。"""

    def test_valid_metadata_kept(self):
        metadata = {
            "choice_groups": [{"prompt": "视觉", "choices": ["写实", "动漫"], "mode": "single", "source": "explicit"}],
            "choices": ["写实", "动漫"],
        }
        groups = resolve_message_choice_groups(metadata, "正文里根本没有选项文本。")
        self.assertEqual(groups[0]["prompt"], "视觉")
        self.assertEqual(groups[0]["source"], "explicit")

    def test_legacy_metadata_upgraded_not_lost(self):
        groups = resolve_message_choice_groups({"choices": ["A", "B"]}, "正文里没有选项。")
        self.assertEqual(groups[0]["choices"], ["A", "B"])
        self.assertEqual(groups[0]["mode"], "single")

    def test_legacy_group_without_mode_or_source_upgraded(self):
        groups = resolve_message_choice_groups(
            {"choice_groups": [{"prompt": "视觉", "choices": ["A", "B"]}]}, "请选择：\n1. X\n2. Y"
        )
        self.assertEqual(groups, [{"prompt": "视觉", "choices": ["A", "B"], "mode": "single", "source": "natural"}])

    def test_missing_metadata_falls_back_to_content(self):
        groups = resolve_message_choice_groups({}, "请选择：\n1. A\n2. B")
        self.assertEqual(groups[0]["choices"], ["A", "B"])

    def test_invalid_metadata_falls_back_to_content(self):
        groups = resolve_message_choice_groups({"choice_groups": []}, "请选择：\n1. A\n2. B")
        self.assertEqual(groups[0]["choices"], ["A", "B"])

    def test_both_empty_stays_empty(self):
        self.assertEqual(resolve_message_choice_groups({}, "普通回复，没有选项。"), [])
        self.assertEqual(resolve_message_choice_groups(None, ""), [])


if __name__ == "__main__":
    unittest.main()
