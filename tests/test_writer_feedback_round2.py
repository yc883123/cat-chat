# -*- coding: utf-8 -*-
"""护栏：织书台第二次反馈的改动。

覆盖：
- 设定集文件夹导入的解析（人物群像 / 分卷大纲 / 章节框架 / 创意与世界观识别）
  与入库（新增版本、追加章节、单章字数收敛、同章号跳过）；
- 版本删除（设定 / 大纲 / 正文版本；删当前版本时指针落到最新一版）；
- 清空章节（覆盖重拆的前置动作）。

边界：只碰临时目录，不启动服务、不连供应商。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.writer.store import (  # noqa: E402
    WriterConflictError,
    WriterStore,
    WriterStoreError,
)
from naiba.writer.settings_import import (  # noqa: E402
    _parse_character_file,
    _parse_chapter_framework,
    parse_settings_folder,
)


def _md(name: str, content: str) -> dict[str, str]:
    return {"name": name, "content": content}


CHAR_FILE = _md("02_人物群像档案_上_1-10.md", """# 人物群像档案

## 主角：灰烬玫瑰·伊索尔德

- 身份：守誓流放骑士，七枚铁誓的持有者
- 性格：沉默坚忍，负罪克制
- 声音：极少解释真相，惯用短句
- 目标：查清圣裁庭白灰名单的真相

## 配角：蕾娜

- 身份：旧圣殿的档案官
- 性格：谨慎
- 背景：为爱放弃圣殿的位置
""")


class SettingsImportParseTests(unittest.TestCase):
    def test_characters_parsed_from_sections(self):
        rows = _parse_character_file(CHAR_FILE["name"], CHAR_FILE["content"])
        self.assertEqual([row["name"] for row in rows], ["灰烬玫瑰·伊索尔德", "蕾娜"])
        lead = rows[0]
        self.assertEqual(lead["role"], "主角")
        self.assertEqual(lead["identity"], "守誓流放骑士，七枚铁誓的持有者")
        self.assertEqual(lead["personality"], "沉默坚忍，负罪克制")
        self.assertEqual(lead["voice"], "极少解释真相，惯用短句")
        self.assertEqual(lead["goal"], "查清圣裁庭白灰名单的真相")
        # 没有标记的默认按配角处理。
        self.assertEqual(rows[1]["role"], "配角")
        self.assertEqual(rows[1]["intro"], "为爱放弃圣殿的位置")

    def test_chapter_framework_with_zh_numbers_and_plan_chars(self):
        content = "\n".join([
            "## 章节框架",
            "第一章 灰烬季的第七声钟响",
            "本章要点：守誓人身份的回归审判；全章 3000 字。",
            "第十二章 净名刑场落灰",
            "本章要点：销名钉上 Lists；全章 4000 字。",
        ])
        rows, counts = _parse_chapter_framework(content)
        self.assertEqual([row["no"] for row in rows], [1, 12])
        self.assertEqual(rows[0]["title"], "灰烬季的第七声钟响")
        self.assertIn("守誓人身份", rows[0]["beat"])
        self.assertEqual(counts, [3000, 4000])

    def test_full_folder_parse(self):
        files = [
            _md("00_主线档案_与副线总设计.md", "# 主线档案\n全书走七部结构。"),
            CHAR_FILE,
            _md("03_分卷大纲_卷一_至卷四.md", "【主线】伊索尔德踏上灰烬季。\n一、北塔第七门。"),
            _md("04_分卷大纲_卷五_至卷七.md", "【主线】白灰名单揭开。"),
            _md("11_目录摘要与投稿简介.md", "一个关于守誓骑士的群像故事。"),
        ]
        parsed = parse_settings_folder(files)
        self.assertEqual(parsed["preview"]["character_count"], 2)
        self.assertEqual(len(parsed["parts"]), 2)
        self.assertEqual([row["no"] for row in parsed["parts"]], [1, 2])
        self.assertIn("分卷大纲_卷一", parsed["parts"][0]["title"])
        self.assertTrue(parsed["has_setup"])
        self.assertEqual(parsed["setup"]["idea"], "一个关于守誓骑士的群像故事。")
        # 未被专项识别的文件进世界观素材，不丢内容。
        self.assertIn("主线档案", parsed["setup"]["worldview"])
        self.assertIn("全书走七部结构", parsed["setup"]["worldview"])

    def test_empty_and_garbage_folders(self):
        with self.assertRaises(WriterStoreError):
            parse_settings_folder([])
        # 认不出的文件不丢字：整份进世界观素材，但要如实警告没有结构化内容。
        parsed = parse_settings_folder([_md("随手笔记.md", "今天天气不错。")])
        self.assertTrue(parsed["has_setup"])
        self.assertEqual(parsed["parts"], [])
        self.assertEqual(parsed["chapters"], [])
        self.assertEqual(parsed["preview"]["character_count"], 0)
        self.assertTrue(any("分卷大纲或章节框架" in w for w in parsed["preview"]["warnings"]))
        self.assertTrue(any("角色档案" in w for w in parsed["preview"]["warnings"]))

    def test_limits_are_enforced(self):
        with self.assertRaises(WriterStoreError):
            parse_settings_folder([_md(f"{i:02d}.md", "x") for i in range(81)])
        with self.assertRaises(WriterStoreError):
            parse_settings_folder([_md("huge.md", "x" * 300_001)])


class SettingsImportStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = WriterStore(self.root)
        self.book = self.store.create_book(title="设定集导入")
        self.book_id = self.book["id"]

    def test_import_creates_versions_and_appends_chapters(self):
        files = [
            CHAR_FILE,
            _md("03_分卷大纲_卷一.md", "【主线】伊索尔德踏上灰烬季。"),
            _md("06_章节框架.md",
                "第一章 钟响\n本章要点：审判开场；全章 3000 字。\n第二章 盲眼账房\n本章要点：旧账。"),
            _md("11_目录摘要与投稿简介.md", "守誓骑士群像。"),
        ]
        payload = self.store.import_settings_folder(self.book_id, files)
        detail = self.store.get_book(self.book_id)
        self.assertEqual(len(detail["book"]["setup_versions"]), 1)
        self.assertEqual(detail["book"]["setup_versions"][0]["kind"], "imported")
        self.assertEqual(len(detail["book"]["outline_versions"]), 1)
        self.assertEqual([row["no"] for row in detail["chapters"]], [1, 2])
        self.assertEqual(detail["book"]["chapter_plan"]["chars"], 3000)
        preview = payload["preview"]
        self.assertEqual(preview["character_count"], 2)
        self.assertEqual(preview["appended_chapters"], [1, 2])

    def test_import_twice_appends_and_skips_existing(self):
        files = [_md("06_章节框架.md", "第一章 钟响\n要点。\n第二章 盲眼账房\n要点。")]
        self.store.import_settings_folder(self.book_id, files)
        payload = self.store.import_settings_folder(self.book_id, files)
        self.assertEqual(payload["preview"]["appended_chapters"], [])
        self.assertEqual(payload["preview"]["skipped_chapters"], [1, 2])
        detail = self.store.get_book(self.book_id)
        self.assertEqual([row["no"] for row in detail["chapters"]], [1, 2])

    def test_import_respects_revision(self):
        with self.assertRaises(WriterConflictError):
            self.store.import_settings_folder(
                self.book_id, [_md("a.md", "x")], expected_revision=999)


class VersionDeletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = WriterStore(self.root)
        self.book = self.store.create_book(title="版本删除")
        self.book_id = self.book["id"]

    def test_delete_setup_versions_and_pointer_falls_back(self):
        v1 = self.store.save_setup(self.book_id, content={"idea": "一"}, kind="manual")
        v2 = self.store.save_setup(self.book_id, content={"idea": "二"}, kind="manual")
        v3 = self.store.save_setup(self.book_id, content={"idea": "三"}, kind="manual")
        detail = self.store.get_book(self.book_id)
        self.assertEqual(detail["setup"]["idea"], "三")
        payload = self.store.delete_setup_versions(
            self.book_id, [v3["setup_version_id"]], expected_revision=detail["book"]["revision"])
        detail = self.store.get_book(self.book_id)
        self.assertEqual(detail["setup"]["idea"], "二")
        self.assertEqual([row["version"] for row in detail["book"]["setup_versions"]], [1, 2])
        self.assertEqual(payload["deleted"], [v3["setup_version_id"]])
        # 未知版本拒绝；当前指针删到空后归零。
        with self.assertRaises(WriterStoreError):
            self.store.delete_setup_versions(self.book_id, ["su-doesnotexist"])
        self.store.delete_setup_versions(
            self.book_id, [v1["setup_version_id"], v2["setup_version_id"]])
        detail = self.store.get_book(self.book_id)
        self.assertEqual(detail["book"]["current_setup_version_id"], "")
        self.assertEqual(detail["book"]["setup_versions"], [])

    def test_delete_outline_versions(self):
        self.store.save_setup(self.book_id, content={"idea": "创意"}, kind="manual")
        o1 = self.store.save_outline(self.book_id, parts=[{"no": 1, "title": "第 1 部", "text": "甲"}])
        o2 = self.store.save_outline(self.book_id, parts=[{"no": 1, "title": "第 1 部", "text": "乙"}])
        detail = self.store.get_book(self.book_id)
        self.store.delete_outline_versions(
            self.book_id, [o1["outline_version_id"]], expected_revision=detail["book"]["revision"])
        detail = self.store.get_book(self.book_id)
        self.assertEqual(len(detail["book"]["outline_versions"]), 1)
        self.assertEqual(detail["book"]["current_outline_version_id"], o2["outline_version_id"])

    def test_delete_chapter_versions_and_clear_chapters(self):
        self.store.save_setup(self.book_id, content={"idea": "创意"}, kind="manual")
        self.store.save_outline(
            self.book_id, parts=[{"no": 1, "title": "第 1 部", "text": "大纲"}])
        self.store.append_chapters(self.book_id, [
            {"no": 1, "title": "一", "beat": "b1"}, {"no": 2, "title": "二", "beat": "b2"}])
        c1 = self.store.save_chapter_version(self.book_id, 1, content="第一章正文甲")
        c1b = self.store.save_chapter_version(self.book_id, 1, content="第一章正文乙")
        self.store.save_chapter_version(self.book_id, 2, content="第二章正文")
        detail = self.store.get_book(self.book_id)
        revision = next(row["revision"] for row in detail["chapters"] if row["no"] == 1)
        self.store.delete_chapter_versions(
            self.book_id, 1, [c1["version"]["id"]], expected_revision=revision)
        detail = self.store.get_book(self.book_id)
        row = next(row for row in detail["chapters"] if row["no"] == 1)
        self.assertEqual(row["current_version_id"], c1b["version"]["id"])
        # 清空章节：全部章节与正文一起消失（覆盖重拆的前置）。
        payload = self.store.clear_chapters(
            self.book_id, expected_revision=detail["book"]["revision"])
        self.assertEqual(payload["removed"], 2)
        detail = self.store.get_book(self.book_id)
        self.assertEqual(detail["chapters"], [])
        self.assertEqual(detail["book"]["chapter_count"], 0)


if __name__ == "__main__":
    unittest.main()
