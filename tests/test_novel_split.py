# -*- coding: utf-8 -*-
"""护栏：拆书匠的纯算法层（docs/plans/2026-10-25-小说拆章模块移植计划.md §5.1）。

边界：本模块只处理字符串、不做 IO（读文件/编码嗅探/缓存都在服务与工具层），
所以这里全部用字符串直接验证，不碰临时文件。

覆盖四级降级、卷合并、剥头、超长切块、符号分节、等分兜底、异常输入，
以及两段式 API 的核心约束（outline 绝不带正文）。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core import novel_split as ns  # noqa: E402


class NovelHeadingTests(unittest.TestCase):
    """标题识别：字符集与常见写法都不能漏。"""

    def test_arabic_and_chinese_numerals(self):
        for title in ("第1章 离乡", "第一章 离乡", "第 12 章 归途", "第一百章 终局"):
            self.assertTrue(ns._looks_like_heading(title), title)

    def test_fullwidth_and_liang(self):
        # 全角数字与「两」是实战细节（第１２章 / 第两章），字符集不能漏。
        for title in ("第１２章 风起", "第两章 云涌", "第两百章 大结局"):
            self.assertTrue(ns._looks_like_heading(title), title)

    def test_volume_special_and_english(self):
        for title in ("卷三 风起", "第三卷 云涌", "楔子", "序章", "尾声", "番外一",
                      "Chapter 3 The Road", "CHAPTER IV", "12. 归途"):
            self.assertTrue(ns._looks_like_heading(title), title)

    def test_plain_prose_is_not_heading(self):
        for line in ("他走进屋子，看见桌上放着一封信。",
                     "这是一段很长的正文，" * 5,
                     "***", "===", ""):
            self.assertFalse(ns._looks_like_heading(line), line[:20])

    def test_overlong_line_is_not_heading(self):
        long_title = "第一章 " + "很长的副标题" * 10
        self.assertFalse(ns._looks_like_heading(long_title), "超长行应当正文，不当标题")


class NovelMainMarkerTests(unittest.TestCase):
    """主章节标记：一本书用一种写法，混进来的异构行属于正文。

    真书取证（《恐怖副本：大BOSS是恋爱脑》157 个「第N章」+ 29 条编号规则列表）：
    副本里的「1.需要穿着正装…」与「第9节 是秦西西的英语课。」都会被宽松规则当成章节，
    把正文切成 190 章（比真实的 157 多出 33 条假章节）。
    """

    def test_dominant_marker_detected(self):
        text = "".join(f"第{i}章 标题{i}\n正文。\n" for i in range(1, 12))
        self.assertEqual(ns._pick_heading_marker(text), "章")

    def test_marker_undecided_on_mixed_forms(self):
        """两种写法势均力敌时不下判断，退回宽松行为（避免误删真章节）。"""
        text = "".join(f"第{i}章 标题{i}\n正文。\n" for i in range(1, 6))
        text += "".join(f"第{i}节 标题{i}\n正文。\n" for i in range(1, 6))
        self.assertEqual(ns._pick_heading_marker(text), "")

    def test_marker_undecided_on_short_book(self):
        text = "第1章 甲\n正文。\n第2章 乙\n正文。\n"
        self.assertEqual(ns._pick_heading_marker(text), "")

    def test_numbered_list_not_split_when_chapters_dominate(self):
        """副本规则列表（1. 2. 3.）是正文，不是章节。"""
        rules = "".join(f"{i}.第{i}条规则内容。\n" for i in range(1, 6))
        text = "".join(f"第{i}章 标题{i}\n正文{i}。\n" for i in range(1, 12))
        mixed = text.replace("正文3。", f"正文3。\n{rules}")
        rows = ns.split_chapters(mixed)
        titles = [r["title"] for r in rows]
        self.assertEqual(len(rows), 11, f"规则列表被当成章节了：{titles}")
        self.assertIn("1.第1条规则内容。", str(rows[2]["body"]), "规则原文必须留在正文里")

    def test_section_heading_not_split_when_chapter_dominates(self):
        """「第9节 是秦西西的英语课。」这类正文句子不当章节。"""
        text = "".join(f"第{i}章 标题{i}\n正文{i}。\n" for i in range(1, 12))
        mixed = text.replace("正文3。", "正文3。\n第9节 是秦西西的英语课。\n课上有几个刺头学生。")
        rows = ns.split_chapters(mixed)
        self.assertEqual(len(rows), 11, [r["title"] for r in rows])
        self.assertIn("秦西西", str(rows[2]["body"]))

    def test_numeric_title_still_works_without_dominant_marker(self):
        """整本用「12. 标题」写法时仍按章节切（不能被主标记规则误伤）。"""
        text = "".join(f"{i}. 标题{i}\n正文{i}。\n" for i in range(1, 7))
        rows = ns.split_chapters(text)
        self.assertEqual(len(rows), 6, [r["title"] for r in rows])


class NovelSplitTests(unittest.TestCase):
    def test_two_headings_split(self):
        text = "第一章 离乡\n少年走出村口。\n\n第二章 进城\n城门口人来人往。"
        rows = ns.split_chapters(text)
        self.assertEqual([r["title"] for r in rows], ["第一章 离乡", "第二章 进城"])
        self.assertIn("少年走出村口", rows[0]["body"])
        self.assertIn("城门口", rows[1]["body"])

    def test_volume_title_merged_into_chapter(self):
        text = "第一卷 平庸少年\n第一章 离乡\n内容A。\n\n第二章 进城\n内容B。"
        rows = ns.split_chapters(text)
        self.assertEqual(
            [r["title"] for r in rows],
            ["第一卷 平庸少年 · 第一章 离乡", "第一卷 平庸少年 · 第二章 进城"],
        )
        self.assertTrue(all(r["body"].strip() for r in rows), "卷合并后不应产生空章")

    def test_heading_only_trailing_marker_is_dropped(self):
        """只有标题没有正文的收尾标记行不算章节。

        真书里常见末尾一行「（番外完）」「全文完」：它被标题正则命中，但后面没有正文。
        留着会得到一个 0 字章节——导出一片空白，还会占掉「一次带 N 章」的额度。
        """
        text = "第1章 甲\n正文甲。\n第2章 乙\n正文乙。\n（番外完）\n"
        rows = ns.split_chapters(text)
        self.assertEqual([r["title"] for r in rows], ["第1章 甲", "第2章 乙"])
        self.assertTrue(all(r["body"].strip() for r in rows), "不能留下空正文的章节")

    def test_single_heading_still_splits(self):
        # 只识别到一个标题：仍按它切，好过乱切（第③级降级）。
        text = "第一章 唯一\n内容一。\n第二章没有空格所以不算标题的正文？其实算。"
        rows = ns.split_chapters(text)
        self.assertGreaterEqual(len(rows), 1)
        self.assertTrue(rows[0]["title"].startswith("第一章"))

    def test_no_heading_falls_back_to_length(self):
        text = "这是一段没有章节标题的长文本。" * 300
        rows = ns.split_chapters(text, max_len=1000)
        self.assertGreater(len(rows), 1)
        self.assertTrue(all(len(r["body"]) <= 1000 for r in rows), "等分兜底不得超过上限")

    def test_length_fallback_prefers_line_break(self):
        line = "这是一行完整的话。" * 20 + "\n"
        text = line * 40
        rows = ns.split_chapters(text, max_len=1200)
        # 尽量在换行处收尾：除最后一段，其余段落不应以句子中间截断的标志结尾
        self.assertGreater(len(rows), 1)
        self.assertTrue(all(r["body"] for r in rows))

    def test_symbol_section_with_inline_title(self):
        text = ("◎ 第一节 起始\n内容一\n◎ 第二节 发展\n内容二\n◎ 第三节 高潮\n内容三\n"
                "◎ 第四节 回落\n内容四\n◎ 第五节 结束\n内容五")
        rows = ns.split_chapters(text)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["title"], "第一节 起始")
        self.assertEqual(rows[0]["body"], "内容一")

    def test_symbol_section_with_title_on_next_line(self):
        # 纯符号分隔线：下一行当标题。
        text = "***\n起始\n***\n发展\n***\n高潮\n***\n回落\n***\n结束"
        rows = ns.split_chapters(text)
        self.assertEqual([r["title"] for r in rows], ["起始", "发展", "高潮", "回落", "结束"])

    def test_strip_head_removes_copyright(self):
        text = ("版权所有 侵权必究\n本书由某网站提供\n\n====正文====\n"
                "第一章 开始\n内容一。\n\n第二章 继续\n内容二。")
        rows = ns.split_chapters(text)
        self.assertEqual([r["title"] for r in rows], ["第一章 开始", "第二章 继续"])
        self.assertNotIn("正文", [r["title"] for r in rows], "剥头后不应残留空章")

    def test_body_mark_after_20_percent_is_ignored(self):
        # 正文中间的分隔线不能被当成正文起点（否则会把开头整段吃掉）。
        long_intro = "开头内容。" * 200
        text = long_intro + "\n====\n第一章 后面\n内容。\n第二章 再后面\n内容。"
        rows = ns.split_chapters(text)
        joined = "".join(str(r["body"]) for r in rows)
        self.assertIn("开头内容", joined, "前 20% 之外的标记不得触发剥头")

    def test_long_chapter_is_capped_with_suffix(self):
        text = "第一章 很长\n" + ("很长的内容。" * 900) + "\n第二章 短\n短内容。"
        rows = ns.split_chapters(text, max_len=2000)
        titles = [r["title"] for r in rows]
        self.assertTrue(any("(1/" in t for t in titles), titles)
        self.assertTrue(any("(2/" in t for t in titles), titles)
        self.assertIn("第二章 短", titles)
        self.assertTrue(all(len(r["body"]) <= 2000 for r in rows))

    def test_empty_and_blank_input(self):
        self.assertEqual(ns.split_chapters(""), [])
        self.assertEqual(ns.split_chapters("   \n\n  "), [])

    def test_single_line_input(self):
        rows = ns.split_chapters("只有一行")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["body"], "只有一行")

    def test_garbage_does_not_crash(self):
        rows = ns.split_chapters("\x00\x01乱码\ufffd\ufffd" * 100, max_len=50)
        self.assertIsInstance(rows, list)

    def test_bodies_are_verbatim(self):
        """正文必须原样保留，不做任何改写（算法只切，不改）。"""
        body_text = "　　少年背着行囊，走出村口。\n　　他回头看了一眼。"
        rows = ns.split_chapters("第一章 离乡\n" + body_text + "\n第二章 进城\n内容。")
        self.assertIn("少年背着行囊，走出村口。", rows[0]["body"])
        self.assertIn("他回头看了一眼。", rows[0]["body"])


class NovelOutlineTests(unittest.TestCase):
    def test_outline_has_no_body(self):
        """两段式 API 的核心约束：索引绝不带正文，否则长会话会被整本书撑爆。"""
        text = "\n".join(
            f"第{i + 1}章 标题{i}\n" + ("内容。" * 200) for i in range(20)
        )
        rows = ns.outline(text)
        self.assertEqual(len(rows), 20)
        for row in rows:
            self.assertNotIn("body", row, "outline 不得包含正文字段")
            self.assertEqual(set(row), {"i", "title", "len"})

    def test_outline_length_grows_with_chapters_not_file_size(self):
        # 正文变长只影响 len 数值，不影响索引条数与字段数。
        # 注：单章超 max_len 会再切块（_cap_chunks），所以这里给足 max_len 保持两章。
        small = ns.outline("第一章 A\n短\n第二章 B\n短")
        big = ns.outline("第一章 A\n" + ("很长。" * 5000) + "\n第二章 B\n短", max_len=200000)
        self.assertEqual(len(small), len(big), "章节数相同则索引条数相同")
        self.assertGreater(big[0]["len"], small[0]["len"], "正文更长则 len 更大")
        self.assertEqual(set(big[0]), {"i", "title", "len"})

    def test_outline_indexes_are_sequential(self):
        rows = ns.outline("第一章 A\n内容\n第二章 B\n内容\n第三章 C\n内容")
        self.assertEqual([r["i"] for r in rows], [0, 1, 2])


class NovelConstantsTests(unittest.TestCase):
    def test_limits_match_plan(self):
        self.assertEqual(ns.DEFAULT_MAX_LEN, 6000)
        self.assertEqual(ns.MAX_FILE_BYTES, 60 * 1024 * 1024)
        self.assertEqual(ns.MAX_TITLE_CHARS, 60)

    def test_pure_algorithm_has_no_io(self):
        """纯算法层不得 import IO/业务模块（评审要求：算法与 IO 边界分开）。"""
        src = (ROOT / "naiba" / "core" / "novel_split.py").read_text(encoding="utf-8")
        for forbidden in ("import json", "from pathlib", "open(", "os.path",
                          "from naiba.storage", "from naiba.tools", "import requests"):
            self.assertNotIn(forbidden, src, f"纯算法层不应出现 {forbidden}")


if __name__ == "__main__":
    unittest.main()
