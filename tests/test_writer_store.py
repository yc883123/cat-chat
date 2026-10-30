# -*- coding: utf-8 -*-
"""护栏：织书台的作品与版本持久化（实施计划 §5、§12.1）。

覆盖：作品 CRUD、版本不可变与指针切换、章节索引同步（正文保留 / 删除保护）、
单章正文版本（task_id 幂等、逐字相同不重复建版本、revision 乐观并发）、
路径安全（ID 白名单、越界拒绝）、活动流水线记录、导入暂存清理。

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
    BOOK_ID_PATTERN,
    WriterConflictError,
    WriterStore,
    WriterStoreError,
    parse_qiyuan_archive,
)


class WriterStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = WriterStore(self.root)

    def _book(self, title="织书记"):
        return self.store.create_book(title=title)


class BookCrudTests(WriterStoreTestCase):
    def test_create_book_returns_safe_id(self):
        book = self._book()
        self.assertRegex(book["id"], BOOK_ID_PATTERN)
        self.assertEqual(book["title"], "织书记")
        self.assertEqual(book["revision"], 1)
        self.assertIsNone(book["active_pipeline"])

    def test_list_books_hides_corrupt_and_foreign_dirs(self):
        self._book()
        # 空目录 / 坏清单都进 corrupt；不相干目录不进列表也不炸接口。
        (self.root / "studio" / "writer" / "wb-20260101-aaaaaaaaaaaa").mkdir(parents=True)
        (self.root / "studio" / "writer" / "wb-20260101-bbbbbbbbbbbb").mkdir(parents=True)
        (self.root / "studio" / "writer" / "wb-20260101-bbbbbbbbbbbb" / "book.json").write_text(
            "{broken", encoding="utf-8")
        (self.root / "studio" / "writer" / "totally-unrelated").mkdir(parents=True)
        listing = self.store.list_books()
        self.assertEqual(len(listing["books"]), 1)
        self.assertEqual(sorted(listing["corrupt"]),
                         ["wb-20260101-aaaaaaaaaaaa", "wb-20260101-bbbbbbbbbbbb"])

    def test_rename_and_delete_book(self):
        book = self._book()
        renamed = self.store.rename_book(book["id"], title="新书名")
        self.assertEqual(renamed["title"], "新书名")
        self.assertEqual(renamed["revision"], 2)
        self.store.delete_book(book["id"])
        self.assertEqual(self.store.list_books()["books"], [])

    def test_delete_nonexistent_raises_lookup(self):
        with self.assertRaises(LookupError):
            self.store.delete_book("wb-20260101-cccccccccccc")

    def test_id_whitelist_rejects_traversal(self):
        for bad in ("../etc", "wb-..-..", "", "wb-20260101-ZZZZZZZZZZZZ"):
            with self.assertRaises(WriterStoreError):
                self.store._book_dir(bad)


class SetupVersionTests(WriterStoreTestCase):
    def test_save_setup_creates_immutable_versions(self):
        book = self._book()
        first = self.store.save_setup(book["id"], content={"idea": "第一版创意"}, kind="manual")
        second = self.store.save_setup(book["id"], content={"idea": "第二版创意"}, kind="manual")
        self.assertNotEqual(first["setup_version_id"], second["setup_version_id"])
        self.assertEqual(
            [row["version"] for row in second["book"]["setup_versions"]], [1, 2])
        # 切回第一版：内容仍可读，指针切换不改动文件。
        self.store.select_setup_version(book["id"], version_id=first["setup_version_id"])
        current = self.store.load_setup(book["id"])
        self.assertEqual(current["idea"], "第一版创意")

    def test_setup_normalizes_unknown_fields_and_char_limit(self):
        book = self._book()
        payload = self.store.save_setup(book["id"], content={
            "idea": "创意", "characters": [
                {"name": "张三", "role": "主角"},
                {"name": "", "role": "无名"},
                "纯字符串角色",
            ],
            "genre_tags": ["a", "b", "c", "d", "e", "f"],
        })
        characters = payload["setup"]["characters"]
        self.assertEqual([row["name"] for row in characters], ["张三", "纯字符串角色"])
        # 3.9.13 起标签上限 5 → 20（反馈「五个太少」）。
        self.assertEqual(len(payload["setup"]["genre_tags"]), 6)
        # 角色分区：写了「主角」→ lead；纯字符串角色没写角色 → 默认补主角。
        self.assertEqual([row["role"] for row in characters], ["lead", "lead"])

    def test_revision_conflict_rejected(self):
        book = self._book()
        with self.assertRaises(WriterConflictError):
            self.store.rename_book(book["id"], title="X", expected_revision=999)


class OutlineAndChapterTests(WriterStoreTestCase):
    def _outline_rows(self, count=5):
        return [{"no": i, "title": f"第{i}章", "beat": f"梗概{i}", "hook": "钩子"}
                for i in range(1, count + 1)]

    def test_outline_sequence_must_be_unique_and_continuous(self):
        book = self._book()
        with self.assertRaises(WriterStoreError):
            self.store.save_outline(book["id"], chapters=[
                {"no": 1, "title": "a"}, {"no": 1, "title": "b"}])
        with self.assertRaises(WriterStoreError):
            self.store.save_outline(book["id"], chapters=[
                {"no": 2, "title": "a"}, {"no": 3, "title": "b"}])

    def test_sync_creates_chapter_index_without_content(self):
        book = self._book()
        payload = self.store.save_outline(
            book["id"], chapters=self._outline_rows(3), kind="generated",
            sync=True, source_setup_version_id="su-x",
        )
        self.assertEqual(payload["synced_chapter_ids"], [1, 2, 3])
        detail = self.store.get_book(book["id"])
        self.assertEqual([row["no"] for row in detail["chapters"]], [1, 2, 3])
        self.assertEqual(detail["book"]["current_outline_version_id"],
                         payload["outline_version_id"])

    def test_sync_preserves_versions_and_protects_content_chapters(self):
        book = self._book()
        self.store.save_outline(book["id"], chapters=self._outline_rows(3), sync=True)
        self.store.save_chapter_version(book["id"], 2, content="第二章已有正文", kind="manual")
        # 大纲缩到只剩第 1 章：第 2 章有正文 → 默认拒绝；allow_drop 显式确认才删。
        with self.assertRaises(WriterStoreError):
            self.store.save_outline(book["id"], chapters=self._outline_rows(1), sync=True)
        payload = self.store.save_outline(
            book["id"], chapters=self._outline_rows(1), sync=True, allow_drop=True)
        self.assertEqual(payload["synced_chapter_ids"], [1])
        # 第 1 章的版本不受影响。
        chapter = self.store.get_chapter(book["id"], 1)
        self.assertEqual(chapter["content"], "")
        with self.assertRaises(LookupError):
            self.store.get_chapter(book["id"], 2)

    def test_chapter_version_immutability_and_pointer(self):
        book = self._book()
        self.store.save_outline(book["id"], chapters=self._outline_rows(2), sync=True)
        first = self.store.save_chapter_version(book["id"], 1, content="初稿", kind="generated")
        second = self.store.save_chapter_version(book["id"], 1, content="二稿", kind="manual")
        detail = self.store.get_chapter(book["id"], 1)
        self.assertEqual(detail["content"], "二稿")
        self.assertEqual([row["version"] for row in detail["versions"]], [1, 2])
        self.store.select_chapter_version(book["id"], 1, version_id=first["version"]["id"])
        self.assertEqual(self.store.get_chapter(book["id"], 1)["content"], "初稿")

    def test_same_task_id_writes_only_one_version(self):
        book = self._book()
        self.store.save_outline(book["id"], chapters=self._outline_rows(2), sync=True)
        first = self.store.save_chapter_version(
            book["id"], 1, content="AI 稿", kind="generated", task_id="st-1")
        again = self.store.save_chapter_version(
            book["id"], 1, content="AI 稿重复写回", kind="generated", task_id="st-1")
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["version"]["id"], first["version"]["id"])
        detail = self.store.get_chapter(book["id"], 1)
        self.assertEqual(len(detail["versions"]), 1)
        self.assertEqual(detail["content"], "AI 稿")

    def test_identical_content_does_not_duplicate(self):
        book = self._book()
        self.store.save_outline(book["id"], chapters=self._outline_rows(2), sync=True)
        self.store.save_chapter_version(book["id"], 1, content="同样内容", kind="manual")
        again = self.store.save_chapter_version(book["id"], 1, content="同样内容", kind="manual")
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(self.store.get_chapter(book["id"], 1)["versions"]), 1)

    def test_chapter_revision_conflict(self):
        book = self._book()
        self.store.save_outline(book["id"], chapters=self._outline_rows(2), sync=True)
        payload = self.store.save_chapter_version(book["id"], 1, content="一稿")
        with self.assertRaises(WriterConflictError):
            self.store.save_chapter_version(
                book["id"], 1, content="二稿", expected_revision=payload["chapter_revision"] - 1)

    def test_empty_content_rejected(self):
        book = self._book()
        self.store.save_outline(book["id"], chapters=self._outline_rows(2), sync=True)
        with self.assertRaises(WriterStoreError):
            self.store.save_chapter_version(book["id"], 1, content="   ")
        with self.assertRaises(LookupError):
            self.store.save_chapter_version(book["id"], 9, content="有正文")

    def test_export_uses_current_versions_and_reports_missing(self):
        book = self._book("导出测")
        self.store.save_outline(book["id"], chapters=self._outline_rows(3), sync=True)
        self.store.save_chapter_version(book["id"], 1, content="第一章正文", kind="manual")
        self.store.save_chapter_version(book["id"], 3, content="第三章正文", kind="manual")
        result = self.store.export_book(book["id"], fmt="txt", with_extras=True)
        self.assertIn("第一章正文", result["text"])
        self.assertIn("第三章正文", result["text"])
        self.assertEqual(result["missing_chapters"], [2])
        self.assertTrue(result["filename"].endswith(".txt"))

    def test_export_md_format(self):
        book = self._book("md书")
        self.store.save_outline(book["id"], chapters=self._outline_rows(1), sync=True)
        self.store.save_chapter_version(book["id"], 1, content="内容", kind="manual")
        result = self.store.export_book(book["id"], fmt="md", with_extras=True)
        self.assertTrue(result["text"].startswith("# md书"))
        self.assertIn("## 第1章", result["text"])

    def test_export_reports_has_body_separately(self):
        """「有没有正文」与「文件是否为空」是两件事：md 光标题就非空，前端不能靠字节数猜。"""
        book = self._book("空书")
        self.store.save_outline(book["id"], chapters=self._outline_rows(2), sync=True)
        empty = self.store.export_book(book["id"], fmt="md", with_extras=True)
        self.assertFalse(empty["has_body"])
        self.assertGreater(len(empty["text"]), 0)
        self.assertEqual(empty["missing_chapters"], [1, 2])
        self.store.save_chapter_version(book["id"], 1, content="有正文了", kind="manual")
        filled = self.store.export_book(book["id"], fmt="md", with_extras=True)
        self.assertTrue(filled["has_body"])
        self.assertEqual(filled["missing_chapters"], [2])

    def test_export_to_file_writes_real_bytes_and_never_overwrites(self):
        """桌面端「另存为」要靠这个真文件：同一秒连导两次不能互相覆盖。"""
        book = self._book("落盘书")
        self.store.save_outline(book["id"], chapters=self._outline_rows(1), sync=True)
        self.store.save_chapter_version(book["id"], 1, content="第一章正文", kind="manual")
        first = self.store.export_to_file(book["id"], fmt="txt")
        path = Path(first["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent.name, "_exports")
        self.assertEqual(first["bytes"], path.stat().st_size)
        self.assertGreater(first["bytes"], 0)
        self.assertTrue(first["has_content"])
        self.assertEqual(first["suggested_name"], "落盘书.txt")
        self.assertEqual(path.read_text(encoding="utf-8"),
                         self.store.export_book(book["id"], fmt="txt")["text"])
        second = self.store.export_to_file(book["id"], fmt="txt")
        self.assertNotEqual(first["path"], second["path"])
        self.assertTrue(Path(second["path"]).is_file())

    def test_export_to_file_flags_empty_book(self):
        book = self._book("还没写")
        self.store.save_outline(book["id"], chapters=self._outline_rows(2), sync=True)
        result = self.store.export_to_file(book["id"], fmt="txt")
        self.assertFalse(result["has_content"])
        self.assertEqual(result["bytes"], 0)
        self.assertEqual(result["missing_chapters"], [1, 2])

    def test_exports_dir_is_not_listed_as_book(self):
        """_exports 落在作品根下：不能被作品列表当成一本损坏的书。"""
        book = self._book("落盘书")
        self.store.save_outline(book["id"], chapters=self._outline_rows(1), sync=True)
        self.store.save_chapter_version(book["id"], 1, content="正文", kind="manual")
        self.store.export_to_file(book["id"], fmt="txt")
        listing = self.store.list_books()
        self.assertEqual(listing["corrupt"], [])
        self.assertEqual([row["id"] for row in listing["books"]], [book["id"]])


class PipelineRecordTests(WriterStoreTestCase):
    def test_update_pipeline_guarded_by_id_and_attempt(self):
        book = self._book()
        manifest = self.store._load_manifest(book["id"])
        manifest["active_pipeline"] = {
            "id": "pl-aaaaaaaaaaaa", "kind": "setup_all", "attempt": 1,
            "status": "running", "steps": ["genre"], "step_index": 0,
        }
        self.store._save_manifest(self.store._book_dir(book["id"]), manifest)
        # ID 不符 → None。
        self.assertIsNone(self.store.update_pipeline(
            book["id"], "pl-bbbbbbbbbbbb", lambda r: r.__setitem__("status", "done")))
        # attempt 不符 → None。
        self.assertIsNone(self.store.update_pipeline(
            book["id"], "pl-aaaaaaaaaaaa", lambda r: r.__setitem__("status", "done"),
            expected_attempt=2))
        updated = self.store.update_pipeline(
            book["id"], "pl-aaaaaaaaaaaa", lambda r: r.__setitem__("status", "done"),
            expected_attempt=1)
        self.assertEqual(updated["status"], "done")
        self.assertEqual(
            self.store.get_active_pipeline(book["id"])["status"], "done")

    def test_clear_pipeline(self):
        book = self._book()
        manifest = self.store._load_manifest(book["id"])
        manifest["active_pipeline"] = {"id": "pl-cccccccccccc", "status": "done"}
        self.store._save_manifest(self.store._book_dir(book["id"]), manifest)
        self.assertFalse(self.store.clear_pipeline(book["id"], "pl-dddddddddddd"))
        self.assertTrue(self.store.clear_pipeline(book["id"], "pl-cccccccccccc"))
        self.assertIsNone(self.store.get_active_pipeline(book["id"]))

    def test_recover_marks_running_interrupted(self):
        book = self._book()
        manifest = self.store._load_manifest(book["id"])
        manifest["active_pipeline"] = {"id": "pl-eeeeeeeeeeee", "status": "running"}
        self.store._save_manifest(self.store._book_dir(book["id"]), manifest)
        self.assertEqual(self.store.recover_pipelines(), 1)
        self.assertEqual(self.store.get_active_pipeline(book["id"])["status"], "interrupted")
        self.assertEqual(self.store.recover_pipelines(), 0)


class ImportTests(WriterStoreTestCase):
    def _archive(self, *, with_content=True):
        novel = {
            "prompt": "一个关于灯塔的故事",
            "meta": {"title": "灯塔", "lane": "male", "subLane": "悬疑",
                     "tags": ["a", "b"], "blurb": "简介文本"},
            "worldview": "世界观内容",
            "chars": [{"name": "张三", "role": "主角", "identity": "守塔人"}],
            "outlineMeta": [
                {"no": 1, "title": "第一章", "beat": "开局", "hook": "钩"},
                {"no": 2, "title": "第二章", "beat": "发展", "hook": ""},
            ],
        }
        chapters = [
            {"title": "第一章", "content": "开局"},
            {"title": "第二章", "content": "第二章完整正文" if with_content else "发展"},
        ]
        return {"novel": novel, "chapters": chapters}

    def test_stage_and_commit_creates_new_book(self):
        staged = self.store.stage_import(
            filename="作品存档.json",
            data=json.dumps(self._archive(), ensure_ascii=False).encode("utf-8"))
        self.assertEqual(staged["title"], "灯塔")
        self.assertEqual(staged["outline_count"], 2)
        self.assertEqual(staged["chapter_count"], 1)
        self.assertEqual(staged["sketch_only_count"], 1)
        committed = self.store.commit_import(staged["import_id"])
        book_id = committed["book"]["id"]
        detail = self.store.get_book(book_id)
        self.assertEqual(detail["book"]["title"], "灯塔")
        # 第 1 章只有梗概 → 无正文版本；第 2 章正文保留为 imported 版本。
        self.assertEqual(detail["chapters"][0]["version_count"], 0)
        self.assertEqual(detail["chapters"][1]["version_count"], 1)
        chapter = self.store.get_chapter(book_id, 2)
        self.assertEqual(chapter["content"], "第二章完整正文")
        self.assertEqual(chapter["versions"][0]["kind"], "imported")
        # 入库后暂存被清掉。
        with self.assertRaises((LookupError, WriterStoreError)):
            self.store.load_import(staged["import_id"])

    def test_stage_rejects_invalid_json(self):
        with self.assertRaises(WriterStoreError):
            self.store.stage_import(filename="x.json", data=b"{not json")

    def test_parse_ignores_settings_and_reports_unknown(self):
        payload = self._archive()
        payload["settings"] = {"api_key": "sk-secret", "baseUrl": "http://x"}
        payload["vendorThing"] = 1
        parsed = parse_qiyuan_archive(payload)
        # 密钥等字段绝不进解析结果。
        blob = json.dumps(parsed, ensure_ascii=False)
        self.assertNotIn("sk-secret", blob)
        self.assertNotIn("baseUrl", blob)
        self.assertTrue(any("vendorThing" in w for w in parsed["preview"]["warnings"]))

    def test_parse_empty_archive_warns(self):
        parsed = parse_qiyuan_archive({"novel": {}, "chapters": []})
        self.assertTrue(any("空架子" in w for w in parsed["preview"]["warnings"]))


if __name__ == "__main__":
    unittest.main()
