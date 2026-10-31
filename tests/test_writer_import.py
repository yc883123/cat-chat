# -*- coding: utf-8 -*-
"""护栏：织书台导入启元存档（实施计划 §10、§12.1）。

覆盖：只读上传内容不读启元目录；beat 与正文逐字相同时按「只有梗概」处理；
导入始终创建新作品；不迁入密钥与供应商信息；源 JSON 结构异常时如实报错。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.writer.store import (  # noqa: E402
    WriterStore,
    WriterStoreError,
    parse_qiyuan_archive,
)


def _archive(*, chapters_content=None, outline_count=3, outline_nos=None):
    """构造一份最小启元存档（完整应用 state 的形状）。

    `outline_nos` 用来模拟真档变体：真档的 `no` 可能是中文数字字符串（'一'）。
    """
    outline = [{"no": i, "title": f"第{i}章 标题{i}", "beat": f"梗概{i}", "hook": f"钩{i}"}
               for i in range(1, outline_count + 1)]
    if outline_nos is not None:
        for row, no in zip(outline, outline_nos):
            row["no"] = no
    chapters = []
    for index, row in enumerate(outline, start=1):
        content = chapters_content.get(index, row["beat"]) if chapters_content else row["beat"]
        chapters.append({"title": row["title"], "content": content})
    return {
        "settings": {"api_key": "sk-should-not-migrate", "baseUrl": "http://relay.example"},
        "novel": {
            "mode": "create",
            "prompt": "灯塔守夜人的故事",
            "meta": {"title": "长夜灯", "lane": "male", "subLane": "悬疑",
                     "tags": ["灯", "雾"], "blurb": "守塔人的第三年"},
            "worldview": "北岸有一座老灯塔。",
            "chars": [{"name": "陈默", "role": "主角", "identity": "守塔人",
                       "goal": "查明上一任失踪"}],
            "outlineMeta": outline,
            "style": "sus",
        },
        "chapters": chapters,
    }


class ParseArchiveTests(unittest.TestCase):
    def test_beat_identical_chapters_are_sketch_only(self):
        parsed = parse_qiyuan_archive(_archive())
        self.assertEqual(parsed["preview"]["outline_count"], 3)
        self.assertEqual(parsed["preview"]["chapter_count"], 0)
        self.assertEqual(parsed["preview"]["sketch_only_count"], 3)
        self.assertEqual(parsed["chapters"], [])

    def test_real_content_imported_as_chapters(self):
        parsed = parse_qiyuan_archive(_archive(chapters_content={2: "第二章的完整正文，不是梗概。"}))
        self.assertEqual(parsed["preview"]["chapter_count"], 1)
        self.assertEqual(parsed["preview"]["sketch_only_count"], 2)
        self.assertEqual(parsed["chapters"][0]["no"], 2)
        self.assertIn("完整正文", parsed["chapters"][0]["content"])

    def test_never_migrates_secrets(self):
        parsed = parse_qiyuan_archive(_archive())
        blob = json.dumps(parsed, ensure_ascii=False)
        self.assertNotIn("sk-should-not-migrate", blob)
        self.assertNotIn("relay.example", blob)

    def test_setup_fields_mapped(self):
        parsed = parse_qiyuan_archive(_archive())
        setup = parsed["setup"]
        self.assertEqual(setup["idea"], "灯塔守夜人的故事")
        self.assertEqual(setup["title"], "长夜灯")
        self.assertEqual(setup["blurb"], "守塔人的第三年")
        self.assertEqual(setup["worldview"], "北岸有一座老灯塔。")
        self.assertEqual(setup["genre"]["cat"], "悬疑")
        self.assertEqual(setup["genre"]["channel"], "male")
        self.assertEqual([row["name"] for row in setup["characters"]], ["陈默"])
        self.assertEqual(len(parsed["outline_chapters"]), 3)

    def test_unrecognized_structure_rejected(self):
        with self.assertRaises(WriterStoreError):
            parse_qiyuan_archive({"totally": "different"})
        with self.assertRaises(WriterStoreError):
            parse_qiyuan_archive({"novel": "不是对象"})

    def test_chinese_numeral_outline_nos(self):
        """真档回归：启元把 outlineMeta 的 no 写成中文数字字符串（'一'~'八'）。"""
        parsed = parse_qiyuan_archive(
            _archive(outline_nos=["一", "二", "三"], chapters_content={2: "第二章正文。"}))
        self.assertEqual([row["no"] for row in parsed["outline_chapters"]], [1, 2, 3])
        self.assertEqual(parsed["chapters"][0]["no"], 2)
        # 数字字符串与混合形态同样按数值理解。
        parsed = parse_qiyuan_archive(_archive(outline_nos=["1", "二", 3]))
        self.assertEqual([row["no"] for row in parsed["outline_chapters"]], [1, 2, 3])

    def test_garbage_outline_no_falls_back_to_position(self):
        """章号完全解不出：按数组位置编号，绝不抛原始异常（真档曾以 500 退出）。"""
        parsed = parse_qiyuan_archive(_archive(outline_nos=["第？章", None, ""]))
        self.assertEqual([row["no"] for row in parsed["outline_chapters"]], [1, 2, 3])

    def test_stage_import_accepts_utf8_bom(self):
        """真档可能被 Windows 记事本另存成带 BOM 的 UTF-8，导入不得报「不是有效的 JSON」。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = WriterStore(Path(tmp))
            staged = store.stage_import(
                filename="作品存档.json",
                data=b"\xef\xbb\xbf" + json.dumps(_archive(), ensure_ascii=False).encode("utf-8"))
            self.assertEqual(staged["title"], "长夜灯")


class ImportFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = WriterStore(self.root)

    def test_stage_preview_then_commit(self):
        staged = self.store.stage_import(
            filename="作品存档.json",
            data=json.dumps(_archive(chapters_content={1: "第一章完整正文。"}),
                            ensure_ascii=False).encode("utf-8"))
        self.assertEqual(staged["title"], "长夜灯")
        self.assertEqual(staged["chapter_count"], 1)
        committed = self.store.commit_import(staged["import_id"])
        book_id = committed["book"]["id"]
        detail = self.store.get_book(book_id)
        # 导入创建的是**新**作品，书名与字段来自存档。
        self.assertEqual(detail["book"]["title"], "长夜灯")
        self.assertEqual(len(detail["book"]["setup_versions"]), 1)
        self.assertEqual(detail["book"]["setup_versions"][0]["kind"], "imported")
        self.assertEqual(detail["book"]["outline_versions"][0]["kind"], "imported")
        # 第 1 章有 imported 正文；第 2、3 章只有梗概。
        self.assertEqual(detail["chapters"][0]["version_count"], 1)
        self.assertEqual(detail["chapters"][1]["version_count"], 0)
        chapter = self.store.get_chapter(book_id, 1)
        self.assertEqual(chapter["content"], "第一章完整正文。")
        self.assertEqual(chapter["versions"][0]["kind"], "imported")

    def test_import_does_not_touch_novel_domain(self):
        novel_root = self.root / "studio" / "novel"
        novel_root.mkdir(parents=True)
        (novel_root / "marker.json").write_text("{}", encoding="utf-8")
        staged = self.store.stage_import(
            filename="a.json", data=json.dumps(_archive()).encode("utf-8"))
        self.store.commit_import(staged["import_id"])
        # 拆书匠目录原样未动；writer 域才有新作品。
        self.assertTrue((novel_root / "marker.json").exists())
        self.assertEqual([p.name for p in novel_root.iterdir()], ["marker.json"])

    def test_double_commit_fails_cleanly(self):
        staged = self.store.stage_import(
            filename="a.json", data=json.dumps(_archive()).encode("utf-8"))
        self.store.commit_import(staged["import_id"])
        with self.assertRaises((LookupError, WriterStoreError)):
            self.store.commit_import(staged["import_id"])
        # 只产生一个作品。
        self.assertEqual(len(self.store.list_books()["books"]), 1)

    def test_archive_without_novel_data_rejected(self):
        with self.assertRaises(WriterStoreError):
            self.store.stage_import(filename="x.json", data=b'{"foo": 1}')

    def test_oversize_rejected(self):
        from naiba.writer.store import MAX_IMPORT_BYTES
        with self.assertRaises(WriterStoreError):
            self.store.stage_import(filename="x.json", data=b"x" * (MAX_IMPORT_BYTES + 1))


if __name__ == "__main__":
    unittest.main()
