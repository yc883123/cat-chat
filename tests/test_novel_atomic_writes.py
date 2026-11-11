"""拆书匠读改写并发：用事件固定交错，成功响应必须能从磁盘重新读到。"""
from __future__ import annotations

import threading
import unittest
from unittest import mock

from naiba.novel import manuscripts
from naiba.novel.service import NovelConflictError, NovelStore
from tests.test_novel_manuscripts import ManuscriptTestCase


class _ObservedLock:
    """第二个写入要么已提交，要么确实在等待第一个写入持有的锁。"""

    def __init__(self, lock, second_boundary):
        self.lock = lock
        self.second_boundary = second_boundary
        self.signal_on_exit = False

    def __enter__(self):
        if threading.current_thread().name == "novel-second":
            if not self.lock.acquire(blocking=False):
                self.second_boundary.set()
                self.lock.acquire()
            else:
                self.signal_on_exit = True
        else:
            self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()
        if threading.current_thread().name == "novel-second" and self.signal_on_exit:
            self.second_boundary.set()


class AtomicNovelWriteTests(ManuscriptTestCase):
    def interleave(self, first, second, *, read_method="_load_manifest"):
        """暂停首个写入的首次读取，再让另一个写入抵达提交/等待锁的边界。"""
        snapshot_read = threading.Event()
        resume_first = threading.Event()
        second_boundary = threading.Event()
        results, errors = {}, {}
        original_lock = self.store._lock
        original_read = getattr(self.store, read_method)

        def read(*args, **kwargs):
            snapshot = original_read(*args, **kwargs)
            if threading.current_thread().name == "novel-first" and not snapshot_read.is_set():
                snapshot_read.set()
                if not resume_first.wait(5):
                    raise AssertionError("首个写入未获准继续")
            return snapshot

        def run(key, action):
            try:
                results[key] = action()
            except Exception as exc:
                errors[key] = exc

        threads = [threading.Thread(target=run, name="novel-first", args=("first", first)),
                   threading.Thread(target=run, name="novel-second", args=("second", second))]
        self.store._lock = _ObservedLock(original_lock, second_boundary)
        try:
            with mock.patch.object(self.store, read_method, side_effect=read):
                threads[0].start()
                self.assertTrue(snapshot_read.wait(5), "首个写入没有读取快照")
                threads[1].start()
                try:
                    self.assertTrue(second_boundary.wait(5), "第二个写入未抵达锁边界")
                finally:
                    resume_first.set()
                    for thread in threads:
                        thread.join(5)
                        self.assertFalse(thread.is_alive(), "写入线程未退出")
        finally:
            resume_first.set()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(5)
            self.store._lock = original_lock
        return results, errors

    def test_two_saves_without_revision_both_survive_with_distinct_numbers(self):
        book = self._create()
        bid = book["id"]
        results, errors = self.interleave(
            lambda: self.store.save_manuscript(bid, 1, content="人工改编正文"),
            lambda: self.store.save_manuscript(bid, 1, content="AI 改编正文", kind=manuscripts.KIND_AI),
            read_method="_load_chapter_index",
        )
        self.assertEqual(errors, {})
        saved = [value["manuscript"] for value in results.values()]
        reopened = NovelStore(self.root).get_chapter(bid, 1)
        self.assertEqual({row["no"] for row in saved}, {1, 2})
        self.assertTrue({row["id"] for row in saved} <= {row["id"] for row in reopened["manuscripts"]})
        self.assertEqual(len(reopened["manuscripts"]), 3)

    def test_same_revision_only_one_save_succeeds(self):
        bid = self._create()["id"]
        revision = self.store.get_chapter(bid, 1)["revision"]
        results, errors = self.interleave(
            lambda: self.store.save_manuscript(bid, 1, content="第一份", expected_revision=revision),
            lambda: self.store.save_manuscript(bid, 1, content="第二份", expected_revision=revision),
            read_method="_load_chapter_index",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(next(iter(errors.values())), NovelConflictError)
        reopened = NovelStore(self.root).get_chapter(bid, 1)
        self.assertEqual(len(reopened["manuscripts"]), 2)
        self.assertEqual(reopened["revision"], revision + 1)

    def test_update_chapter_and_save_keep_both_manuscripts_and_title(self):
        bid = self._create()["id"]
        results, errors = self.interleave(
            lambda: self.store.update_chapter(bid, 1, title="人工新标题", content="人工修改正文"),
            lambda: self.store.save_manuscript(bid, 1, content="后台 AI 正文", kind=manuscripts.KIND_AI),
            read_method="_load_chapter_index",
        )
        self.assertEqual(errors, {})
        reopened = NovelStore(self.root).get_chapter(bid, 1)
        self.assertEqual(reopened["title"], "人工新标题")
        self.assertEqual(len(reopened["manuscripts"]), 3)
        self.assertEqual({row["no"] for row in reopened["manuscripts"]}, {0, 1, 2})
        ids = {row["id"] for row in reopened["manuscripts"]}
        self.assertIn(results["first"]["created_manuscript"]["id"], ids)
        self.assertIn(results["second"]["manuscript"]["id"], ids)

    def test_save_interleaved_with_source_delete_does_not_resurrect_index(self):
        bid = self._create()["id"]
        original = self.store.get_chapter(bid, 1)["base_manuscript_id"]
        self.store.save_manuscript(bid, 1, content="保留的改编稿")
        results, errors = self.interleave(
            lambda: self.store.save_manuscript(bid, 1, content="并发新增稿", source_manuscript_id=original),
            lambda: self.store.delete_manuscript(bid, 1, original),
            read_method="_load_chapter_index",
        )
        self.assertEqual(errors, {})
        reopened = NovelStore(self.root).get_chapter(bid, 1)
        ids = {row["id"] for row in reopened["manuscripts"]}
        self.assertNotIn(original, ids)
        self.assertIn(results["first"]["manuscript"]["id"], ids)
        self.assertFalse(any(row.get("broken") for row in reopened["manuscripts"]))

    def test_duplicate_concurrent_content_creates_one_manuscript(self):
        bid = self._create()["id"]
        results, errors = self.interleave(
            lambda: self.store.save_manuscript(bid, 1, content="相同正文"),
            lambda: self.store.save_manuscript(bid, 1, content="相同正文"),
            read_method="_load_chapter_index",
        )
        self.assertEqual(errors, {})
        self.assertEqual({result["duplicate"] for result in results.values()}, {True, False})
        self.assertEqual(len({result["manuscript"]["id"] for result in results.values()}), 1)
        self.assertEqual(len(NovelStore(self.root).get_chapter(bid, 1)["manuscripts"]), 2)

    def test_all_manifest_writers_keep_concurrent_summary(self):
        actions = {
            "update_chapter": lambda bid: self.store.update_chapter(bid, 1, title="改后章名"),
            "save_manuscript": lambda bid: self.store.save_manuscript(bid, 1, content="新稿正文"),
            "delete_chapter": lambda bid: self.store.delete_chapter(bid, 3),
            "apply_elements": lambda bid: self.store.apply_elements(bid, task_id="el-new", raw="人物：沈砚"),
            "apply_outline": lambda bid: self.store.apply_outline(bid, task_id="ol-new", text="新大纲"),
            "apply_script": lambda bid: self.store.apply_script(bid, task_id="sc-new", text="新场次正文", from_chapter=3),
            "update_scene": lambda bid: self.store.update_scene(bid, 1, content="修改场次"),
            "delete_scene": lambda bid: self.store.delete_scene(bid, 1),
            "reorder_script": lambda bid: self.store.reorder_script(bid, [2, 1]),
            "update_summaries": lambda bid: self.store.update_summaries(bid, {"1": "首项摘要"}),
        }
        for name, action in actions.items():
            with self.subTest(writer=name):
                bid = self._create()["id"]
                self.store.apply_script(bid, task_id="sc-one", text="场次一", from_chapter=1)
                self.store.apply_script(bid, task_id="sc-two", text="场次二", from_chapter=2)
                _, errors = self.interleave(
                    lambda: action(bid),
                    lambda: self.store.update_summaries(bid, {"2": "并发摘要"}),
                )
                self.assertEqual(errors, {})
                reloaded = NovelStore(self.root)._load_manifest(bid)
                self.assertEqual(reloaded["summaries"].get("2"), "并发摘要")
                if name == "update_summaries":
                    self.assertEqual(reloaded["summaries"].get("1"), "首项摘要")

    def test_failed_index_write_is_not_reported_as_success(self):
        bid = self._create()["id"]
        before = self.store.get_chapter(bid, 1)
        target = self.store._chapter_path(self.store._book_dir(bid), 1)
        write = self.store._write_json

        def fail_index(path, payload):
            if path == target:
                raise OSError("磁盘写入失败")
            return write(path, payload)

        with mock.patch.object(self.store, "_write_json", side_effect=fail_index):
            with self.assertRaisesRegex(OSError, "磁盘写入失败"):
                self.store.save_manuscript(bid, 1, content="未提交正文")
        after = NovelStore(self.root).get_chapter(bid, 1)
        self.assertEqual(after["manuscripts"], before["manuscripts"])
        self.assertEqual(after["revision"], before["revision"])

    def test_reorder_with_scene_summary_and_elements_mutations_keeps_order(self):
        actions = {
            "edit": lambda bid: self.store.update_scene(bid, 1, content="重排后的首场修改"),
            "delete": lambda bid: self.store.delete_scene(bid, 1),
            "summary": lambda bid: self.store.update_summaries(bid, {"2": "新的摘要"}),
            "elements": lambda bid: self.store.apply_elements(bid, task_id="el", raw="人物：并发人物"),
        }
        for name, action in actions.items():
            with self.subTest(mutation=name):
                bid = self._create()["id"]
                for no in (1, 2, 3):
                    self.store.apply_script(bid, task_id=f"sc-{no}", text=f"场次正文 {no}", from_chapter=no)
                _, errors = self.interleave(
                    lambda: self.store.reorder_script(bid, [3, 1, 2]),
                    lambda: action(bid),
                )
                self.assertEqual(errors, {})
                current = NovelStore(self.root)._load_manifest(bid)
                expected = [1, 2] if name == "delete" else [3, 1, 2]
                self.assertEqual([row["from_chapter"] for row in current["script"]], expected)
                self.assertEqual(current["script_versions"][-1]["script"], current["script"])
                if name == "edit":
                    self.assertEqual(current["script"][0]["content"], "重排后的首场修改")


if __name__ == "__main__":
    unittest.main()
