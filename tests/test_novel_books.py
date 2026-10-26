# -*- coding: utf-8 -*-
"""护栏：小说域的作品与章节持久化（《小说改编接入工作台实施计划》§八）。

覆盖：作品 CRUD、章节读写、坏文件容错、越界拒绝、两段式（索引不含正文）、
原子写范式、体积闸门、导入暂存、乐观并发 revision。

边界：本模块只碰临时目录，不启动服务、不连供应商。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.novel.service import (  # noqa: E402
    BOOK_ID_PATTERN,
    NovelConflictError,
    NovelServiceError,
    NovelStore,
)


def _book_text(chapters: int = 5, body: str = "这是一段正文内容。" * 30) -> str:
    return "".join(f"第{i}章 标题{i}\n{body}\n" for i in range(1, chapters + 1))


class NovelStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = NovelStore(self.root)

    def _import(self, chapters: int = 5, name: str = "测试书.txt", max_len: int = 6000) -> dict:
        data = _book_text(chapters).encode("utf-8")
        return self.store.stage_import(filename=name, data=data, max_len=max_len)

    def _create(self, chapters: int = 5, picked=None) -> dict:
        staged = self._import(chapters)
        picked = picked if picked is not None else list(range(1, chapters + 1))
        return self.store.create_book(import_id=staged["import_id"], picked=picked)


class NovelImportTests(NovelStoreTestCase):
    def test_import_returns_index_without_body(self):
        """两段式：导入只回索引，绝不带正文。"""
        staged = self._import(chapters=4)
        self.assertEqual(staged["total"], 4)
        self.assertEqual(len(staged["chapters"]), 4)
        for row in staged["chapters"]:
            self.assertNotIn("content", row, "导入响应不能带正文")
            self.assertNotIn("body", row, "导入响应不能带正文")
            self.assertEqual(set(row), {"i", "title", "len"})

    def test_import_reports_split_level_and_encoding(self):
        """拆分层级必须明示（降级时用户要知道这不是常规识别）。"""
        staged = self._import()
        self.assertEqual(staged["split_level"], "heading")
        self.assertIn("标题", staged["split_level_label"])
        self.assertTrue(staged["encoding"])

    def test_import_indexes_start_at_one(self):
        staged = self._import(chapters=3)
        self.assertEqual([row["i"] for row in staged["chapters"]], [1, 2, 3])

    def test_import_rejects_empty_and_oversize(self):
        with self.assertRaises(NovelServiceError):
            self.store.stage_import(filename="x.txt", data=b"", max_len=6000)
        with self.assertRaises(NovelServiceError):
            self.store.stage_import(filename="x.txt", data=b"x" * (60 * 1024 * 1024 + 1), max_len=6000)

    def test_import_without_chapters_is_rejected_and_cleaned(self):
        """没有可拆内容时拒绝，且不留暂存目录（失败的导入不出现在列表里）。"""
        with self.assertRaises(NovelServiceError):
            self.store.stage_import(filename="blank.txt", data=b"   \n\n  ", max_len=6000)
        imports = self.root / "studio" / "novel" / "_imports"
        leftovers = [p for p in imports.iterdir()] if imports.exists() else []
        self.assertEqual(leftovers, [], "失败的导入必须清掉暂存目录")

    def test_staged_chapter_returns_body(self):
        staged = self._import(chapters=3)
        chapter = self.store.load_import(staged["import_id"])["chapters"][1]
        self.assertIn("正文", str(chapter["body"]))

    def test_purge_imports_removes_expired_only(self):
        fresh = self._import()
        old = self._import()
        old_dir = self.root / "studio" / "novel" / "_imports" / old["import_id"]
        import os
        import time

        past = time.time() - 48 * 3600
        os.utime(old_dir, (past, past))
        removed = self.store.purge_imports(ttl_seconds=3600)
        self.assertEqual(removed, 1)
        remaining = self.root / "studio" / "novel" / "_imports"
        self.assertTrue((remaining / fresh["import_id"]).exists())
        self.assertFalse(old_dir.exists())


class NovelBookCrudTests(NovelStoreTestCase):
    def test_create_book_writes_manifest_and_chapters(self):
        book = self._create(chapters=3)
        self.assertTrue(BOOK_ID_PATTERN.match(book["id"]))
        self.assertEqual(book["chapter_count"], 3)
        book_dir = self.root / "studio" / "novel" / book["id"]
        self.assertTrue((book_dir / "book.json").is_file())
        for no in (1, 2, 3):
            self.assertTrue((book_dir / "chapters" / f"{no:04d}.json").is_file())

    def test_create_book_only_keeps_picked_chapters(self):
        book = self._create(chapters=5, picked=[2, 4])
        detail = self.store.get_book(book["id"])
        self.assertEqual([row["i"] for row in detail["chapters"]], [2, 4])
        self.assertEqual(detail["book"]["chapter_count"], 2)

    def test_create_book_dedupes_and_sorts(self):
        book = self._create(chapters=5, picked=[3, 1, 3, 2])
        detail = self.store.get_book(book["id"])
        self.assertEqual([row["i"] for row in detail["chapters"]], [1, 2, 3])

    def test_create_book_rejects_bad_picks(self):
        staged = self._import(chapters=3)
        for bad in ([], [0], [4], ["1"], [True]):
            with self.assertRaises(NovelServiceError, msg=f"应拒绝：{bad!r}"):
                self.store.create_book(import_id=staged["import_id"], picked=bad)

    def test_create_book_custom_title(self):
        staged = self._import(chapters=2)
        book = self.store.create_book(import_id=staged["import_id"], picked=[1], title="自定义书名")
        self.assertEqual(book["title"], "自定义书名")

    def test_list_books_is_newest_first(self):
        first = self._create(chapters=2)
        second = self._create(chapters=2)
        rows = self.store.list_books()["books"]
        self.assertEqual({row["id"] for row in rows}, {first["id"], second["id"]})

    def test_get_book_index_has_no_body(self):
        """两段式：作品详情仍只给索引（不含正文与稿件正文）。"""
        book = self._create(chapters=3)
        detail = self.store.get_book(book["id"])
        for row in detail["chapters"]:
            self.assertIn("i", row)
            self.assertIn("title", row)
            self.assertIn("len", row)
            self.assertNotIn("content", row, "详情不能带正文")
            self.assertNotIn("manuscripts", row, "详情不能带稿件正文")
        self.assertNotIn("正文内容", json.dumps(detail["chapters"]))

    def test_get_chapter_returns_body_and_revision(self):
        book = self._create(chapters=3)
        chapter = self.store.get_chapter(book["id"], 2)
        self.assertEqual(chapter["i"], 2)
        self.assertIn("正文", chapter["content"])
        self.assertGreaterEqual(chapter["revision"], 1)

    def test_get_missing_chapter_raises_lookup(self):
        book = self._create(chapters=2)
        with self.assertRaises(LookupError):
            self.store.get_chapter(book["id"], 9)

    def test_update_chapter_bumps_revision_and_manifest(self):
        """改标题：改的是章节索引；改正文新增改编稿，不覆盖原文。"""
        book = self._create(chapters=2)
        before = self.store.get_book(book["id"])["book"]["revision"]
        original = self.store.get_chapter(book["id"], 1)["content"]
        chapter = self.store.update_chapter(book["id"], 1, title="新标题", content="新的正文。")
        self.assertEqual(chapter["title"], "新标题")
        # 界面上的「当前正文」是刚保存的改编稿，但原文逐字未变。
        self.assertEqual(self.store.get_chapter(book["id"], 1)["content"], original,
                         "原文不能被编辑覆盖")
        self.assertEqual(chapter["created_manuscript"]["content"], "新的正文。")
        self.assertEqual(chapter["created_manuscript"]["kind"], "manual")
        self.assertGreater(chapter["revision"], 1)
        self.assertGreater(self.store.get_book(book["id"])["book"]["revision"], before)

    def test_update_chapter_rejects_empty_values(self):
        book = self._create(chapters=2)
        with self.assertRaises(NovelServiceError):
            self.store.update_chapter(book["id"], 1, title="   ")
        with self.assertRaises(NovelServiceError):
            self.store.update_chapter(book["id"], 1, content="   ")

    def test_update_chapter_optimistic_revision(self):
        """乐观并发：revision 对不上就拒绝，避免覆盖别处的写回。"""
        book = self._create(chapters=2)
        current = self.store.get_chapter(book["id"], 1)
        self.store.update_chapter(book["id"], 1, content="第一次改。",
                                  expected_revision=current["revision"])
        with self.assertRaises(NovelConflictError):
            self.store.update_chapter(book["id"], 1, content="基于旧版本的改。",
                                      expected_revision=current["revision"])

    def test_delete_chapter_removes_file_and_manifest_entry(self):
        book = self._create(chapters=3)
        result = self.store.delete_chapter(book["id"], 2)
        self.assertTrue(result["ok"])
        detail = self.store.get_book(book["id"])
        self.assertEqual([row["i"] for row in detail["chapters"]], [1, 3])
        book_dir = self.root / "studio" / "novel" / book["id"]
        self.assertFalse((book_dir / "chapters" / "0002.json").exists())

    def test_delete_last_chapter_is_refused(self):
        book = self._create(chapters=2, picked=[1])
        with self.assertRaises(NovelServiceError):
            self.store.delete_chapter(book["id"], 1)

    def test_delete_book_only_affects_that_book(self):
        keep = self._create(chapters=2)
        drop = self._create(chapters=2)
        self.store.delete_book(drop["id"])
        ids = {row["id"] for row in self.store.list_books()["books"]}
        self.assertEqual(ids, {keep["id"]})

    def test_delete_missing_book_raises_lookup(self):
        with self.assertRaises(LookupError):
            self.store.delete_book("book-20260101-000000000000")


class NovelPathSafetyTests(NovelStoreTestCase):
    def test_illegal_book_ids_rejected(self):
        for bad in ("", "../etc", "book-x", "custom-20260101000000-abcdef123456", "book-20260101-../../x"):
            with self.assertRaises(NovelServiceError, msg=f"应拒绝：{bad!r}"):
                self.store.get_book(bad)

    def test_illegal_import_ids_rejected(self):
        for bad in ("", "../x", "import-x", "book-20260101-abcdefabcdef"):
            with self.assertRaises(NovelServiceError, msg=f"应拒绝：{bad!r}"):
                self.store.load_import(bad)

    def test_chapter_number_bounds(self):
        book = self._create(chapters=2)
        for bad in (0, -1, 10000):
            # 越界既可以是参数错误（400），也可以是「章节不存在」（404）——都不许读出来。
            with self.assertRaises((NovelServiceError, LookupError), msg=f"应拒绝：{bad}"):
                self.store.get_chapter(book["id"], bad)

    def test_book_dir_stays_under_root(self):
        """作品目录必须落在 data/studio/novel/ 下（不越界）。"""
        book = self._create(chapters=1)
        book_dir = (self.root / "studio" / "novel" / book["id"]).resolve()
        root = (self.root / "studio" / "novel").resolve()
        self.assertTrue(book_dir.is_relative_to(root))


class NovelBrokenDataTests(NovelStoreTestCase):
    def test_broken_chapter_is_reported_not_filled(self):
        """已登记但损坏的章节进 broken，禁止静默补空正文。"""
        book = self._create(chapters=3)
        book_dir = self.root / "studio" / "novel" / book["id"]
        (book_dir / "chapters" / "0002.json").write_text("{ 坏掉的 JSON", encoding="utf-8")
        detail = self.store.get_book(book["id"])
        self.assertEqual(detail["broken"], [2])
        self.assertEqual([row["i"] for row in detail["chapters"]], [1, 3])

    def test_missing_chapter_file_is_reported(self):
        book = self._create(chapters=3)
        book_dir = self.root / "studio" / "novel" / book["id"]
        (book_dir / "chapters" / "0003.json").unlink()
        detail = self.store.get_book(book["id"])
        self.assertEqual(detail["broken"], [3])

    def test_corrupt_manifest_is_not_listed_as_record(self):
        book = self._create(chapters=2)
        book_dir = self.root / "studio" / "novel" / book["id"]
        (book_dir / "book.json").write_text("not json", encoding="utf-8")
        listing = self.store.list_books()
        self.assertEqual(listing["books"], [])
        self.assertEqual(listing["corrupt"], [book["id"]])

    def test_unsupported_schema_version_is_rejected(self):
        book = self._create(chapters=2)
        book_dir = self.root / "studio" / "novel" / book["id"]
        payload = json.loads((book_dir / "book.json").read_text(encoding="utf-8"))
        payload["schema_version"] = 99
        (book_dir / "book.json").write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(NovelServiceError):
            self.store.get_book(book["id"])

    def test_orphan_chapter_file_is_ignored(self):
        """清单没登记的孤儿章节不参与索引（崩溃残留可清理）。"""
        book = self._create(chapters=2)
        book_dir = self.root / "studio" / "novel" / book["id"]
        (book_dir / "chapters" / "0009.json").write_text(
            json.dumps({"schema_version": 1, "revision": 1, "i": 9, "title": "孤儿", "len": 2, "content": "孤儿"}),
            encoding="utf-8",
        )
        detail = self.store.get_book(book["id"])
        self.assertEqual([row["i"] for row in detail["chapters"]], [1, 2])


class NovelExportTests(NovelStoreTestCase):
    def test_export_txt_contains_all_chapters_in_order(self):
        book = self._create(chapters=3)
        text = self.store.export_book(book["id"], fmt="txt")
        positions = [text.find(f"第{i}章") for i in (1, 2, 3)]
        self.assertTrue(all(p >= 0 for p in positions))
        self.assertEqual(positions, sorted(positions))

    def test_export_md_uses_headings(self):
        book = self._create(chapters=2)
        text = self.store.export_book(book["id"], fmt="md")
        self.assertIn("## 第1章", text)


if __name__ == "__main__":
    unittest.main()
