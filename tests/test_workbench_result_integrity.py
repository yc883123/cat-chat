"""Artifact consumers, manual drafts and recoverable Writer writeback."""
from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from naiba.novel.service import NovelService
from naiba.studio.jobs import StudioJobManager
from naiba.studio.store import StudioStore
from naiba.writer import drafts, writeback
from naiba.writer.store import WriterStore, WriterStoreError, WriterConflictError
from tests.test_writer_pipeline import PipelineTestCase


class WriterFileTransactions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = WriterStore(Path(self.tmp.name))
        self.book_id = self.store.create_book(title="事务测试")["id"]
        self.store.append_chapters(self.book_id, [{"no": 1, "title": "第一章", "beat": "开端"}])

    def chapter(self):
        return self.store.get_chapter(self.book_id, 1)

    def test_manual_draft_preserves_exact_utf8_bytes(self):
        text = "\r\n  中文🙂\r\n\r\n结尾 \t\r\n"
        result = drafts.save(self.store, self.book_id, 1, content=text)
        self.assertEqual(result["version"]["content"], text)
        self.assertEqual(result["manual_draft"]["sha256"], writeback.digest(text.encode("utf-8")))
        self.assertEqual(self.chapter()["content"], text)
        self.assertFalse(list(self.store._book_dir(self.book_id).glob("manual-drafts/**/*.txt")))

    def test_revision_conflict_does_not_write_draft_or_version(self):
        with self.assertRaises(WriterConflictError):
            drafts.save(self.store, self.book_id, 1, content="手改正文", expected_revision=999)
        self.assertFalse(list(self.store._book_dir(self.book_id).glob("manual-drafts/**/*.txt")))
        self.assertEqual(self.chapter()["content"], "")

    def test_failed_save_keeps_snapshot_and_original_pointer(self):
        with mock.patch.object(self.store, "_write_chapter_index", side_effect=OSError("disk full")):
            with self.assertRaises(WriterStoreError):
                drafts.save(self.store, self.book_id, 1, content="尚未提交的手改正文")
        self.assertEqual(self.chapter()["content"], "")
        files = list(self.store._book_dir(self.book_id).glob("manual-drafts/**/*.txt"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes().decode("utf-8"), "尚未提交的手改正文")

    def test_transaction_abort_does_not_publish_staged_versions(self):
        with self.assertRaises(RuntimeError):
            with self.store.writeback_transaction(self.book_id):
                self.store.save_chapter_version(self.book_id, 1, content="暂存正文", task_id="generation-a")
                raise RuntimeError("before commit")
        self.assertEqual(self.chapter()["content"], "")
        self.assertFalse(list(self.store._book_dir(self.book_id).glob("chapter-versions/**/*.json")))

    def test_crash_between_version_and_pipeline_publication_recovers_once(self):
        original = writeback.durable_write
        def fail_book(path, data):
            if path.name == "book.json":
                raise OSError("crash before book publication")
            original(path, data)
        with mock.patch.object(writeback, "durable_write", side_effect=fail_book):
            with self.assertRaises(OSError):
                with self.store.writeback_transaction(self.book_id):
                    self.store.save_chapter_version(self.book_id, 1, content="可恢复正文", task_id="generation-a")
        self.assertTrue((self.store._book_dir(self.book_id) / writeback.JOURNAL_NAME).exists())
        reopened = WriterStore(Path(self.tmp.name))
        self.assertEqual(reopened.get_chapter(self.book_id, 1)["content"], "可恢复正文")
        self.assertEqual(len(reopened.get_chapter(self.book_id, 1)["versions"]), 1)
        self.assertFalse((reopened._book_dir(self.book_id) / writeback.JOURNAL_NAME).exists())

    def test_recovery_rejects_outside_path(self):
        book = self.store._book_dir(self.book_id)
        payload = {"version": 1, "files": [{"path": "../outside.json", "text": "{}",
                                            "before": None, "after": writeback.digest(b"{}")} ]}
        writeback.durable_write(book / writeback.JOURNAL_NAME, writeback.json_bytes(payload))
        with self.assertRaises(WriterStoreError):
            self.store._load_manifest(self.book_id)
        self.assertFalse((book.parent / "outside.json").exists())

    def test_concurrent_reader_cannot_see_a_transaction_that_aborts(self):
        self.store.save_chapter_version(self.book_id, 1, content="已提交原文")
        loaded, staged, read_allowed, abort, read_done = [threading.Event() for _ in range(5)]
        original_load = self.store._load_manifest
        observed, errors = [], []
        def load(book_id):
            result = original_load(book_id)
            if threading.current_thread().name == "draft-reader":
                loaded.set()
                if not read_allowed.wait(5):
                    raise TimeoutError("reader gate")
            return result
        def reader():
            try:
                observed.append(self.chapter()["content"])
            except Exception as exc:
                errors.append(exc)
            finally:
                read_done.set()
        def writer():
            try:
                if not loaded.wait(5):
                    raise TimeoutError("manifest gate")
                with self.store.writeback_transaction(self.book_id):
                    self.store.save_chapter_version(self.book_id, 1, content="最终回滚的正文")
                    staged.set()
                    if not abort.wait(5):
                        raise TimeoutError("abort gate")
                    raise RuntimeError("abort transaction")
            except RuntimeError:
                pass
            except Exception as exc:
                errors.append(exc)
        with mock.patch.object(self.store, "_load_manifest", side_effect=load):
            a = threading.Thread(target=reader, name="draft-reader")
            b = threading.Thread(target=writer, name="draft-writer")
            a.start()
            b.start()
            try:
                self.assertTrue(staged.wait(5))
                read_allowed.set()
                self.assertFalse(read_done.wait(0.1), "不能读出仍未提交的暂存区")
            finally:
                abort.set()
                read_allowed.set()
                a.join(5)
                b.join(5)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(observed, ["已提交原文"])


class PipelineArtifactConsumers(PipelineTestCase):
    def setUp(self):
        super().setUp()
        self.store.save_setup(self.book_id, content={"idea": "冒险"})
        self.scheduled = []
        self.app.studio.jobs.runner = SimpleNamespace(submit=self.scheduled.append)

    def prepared(self, *, done=True):
        pipeline = self.start(kind="setup_step", step="world")
        task_id = pipeline["current_task_id"]
        task = self.app.studio.jobs.tasks[task_id]
        task.update(output_contract_version=1, logical_generation_id="original-generation")
        self.app.studio.jobs.finish(task_id, "浏览器或旧result里的伪造正文", status="done" if done else "failed")
        self.app.studio.jobs.result_text = lambda _id, require_final=True: "TXT 文件中的真实世界设定"
        return pipeline, task

    def test_pipeline_reads_verified_file_and_writes_stable_identity(self):
        self.prepared()
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "done")
        setup = self.store.load_setup(self.book_id)
        self.assertEqual(setup["worldview"], "TXT 文件中的真实世界设定")
        self.assertEqual(setup["task_id"], "original-generation")
        self.assertIn("original-generation", self.current_pipeline()["completed_generation_ids"])

    def test_missing_file_stops_without_falling_back(self):
        self.prepared()
        def corrupt(*_args, **_kwargs):
            raise ValueError("TXT 摘要校验失败")
        self.app.studio.jobs.result_text = corrupt
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "failed")
        self.assertEqual(self.store.load_setup(self.book_id)["worldview"], "")

    def resume_child(self, task_id, **_kwargs):
        task = copy.deepcopy(self.app.studio.jobs.tasks[task_id])
        task.update(id="resumed-execution", status="queued", result="")
        self.app.studio.jobs.tasks[task["id"]] = task
        return task

    def test_resume_keeps_attempt_and_logical_writeback_identity(self):
        old, task = self.prepared(done=False)
        self.tick()
        self.app.studio.jobs.prepare_resume = self.resume_child
        resumed = self.manager.resume_pipeline(self.book_id)
        self.assertEqual(resumed["attempt"], old["attempt"])
        self.assertEqual(resumed["step_index"], old["step_index"])
        self.assertEqual(resumed["current_task_id"], "resumed-execution")
        self.assertEqual(self.scheduled, ["resumed-execution"])
        self.app.studio.jobs.finish("resumed-execution", "忽略此兼容投影")
        self.tick()
        self.assertEqual(self.current_pipeline()["status"], "done")
        self.assertEqual(self.store.load_setup(self.book_id)["task_id"], task["logical_generation_id"])

    def test_cancel_during_resume_creation_never_schedules_child(self):
        self.prepared(done=False)
        self.tick()
        entered, release = threading.Event(), threading.Event()
        errors = []
        def prepare(task_id, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test release")
            return self.resume_child(task_id, **kwargs)
        self.app.studio.jobs.prepare_resume = prepare
        def run():
            try:
                self.manager.resume_pipeline(self.book_id)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.manager.cancel_pipeline(self.book_id)
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.scheduled, [])
        self.assertIn("resumed-execution", self.app.studio.jobs.cancelled)
        self.assertEqual(self.current_pipeline()["status"], "cancelled")


class NovelArtifactConsumers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = SimpleNamespace(paths=SimpleNamespace(data_dir=Path(self.tmp.name)))
        self.studio_store = StudioStore(Path(self.tmp.name))
        runner = SimpleNamespace(submit=lambda _id: None)
        self.app.studio = SimpleNamespace(jobs=StudioJobManager(self.app, self.studio_store, runner))
        self.service = NovelService(self.app)
        staged = self.service.store.stage_import(filename="书.txt", data="第1章 开端\n原文".encode("utf-8"), max_len=6000)
        self.book = self.service.store.create_book(import_id=staged["import_id"], picked=[1])

    def task(self, stage="adapt", final=True):
        chapter = self.service.store.get_chapter(self.book["id"], 1)
        task = self.studio_store.create_task({"module": "novel", "content": "冻结输入", "model_name": "fixture",
            "novel": {"book_id": self.book["id"], "stage": stage, "chapter_ids": [1],
                      "manuscripts": {"1": chapter["base_manuscript_id"]}}})
        receipt = self.studio_store.artifacts.write_segment(task, 1, 1, "经过文件校验的真实正文。")
        if final:
            self.studio_store.artifacts.complete_block(task, 1, receipt)
        self.studio_store.update_task(task["id"], status="done", result="伪造完成确认")
        return task

    def test_manuscript_save_ignores_browser_body_and_source_override(self):
        task = self.task()
        payload, status = self.service.api_save_manuscript(self.book["id"], 1,
            {"kind": "ai", "task_id": task["id"], "content": "浏览器伪造正文", "source_manuscript_id": "wrong"})
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["manuscript"]["content"], "经过文件校验的真实正文。")
        self.assertEqual(payload["manuscript"]["source_manuscript_id"], task["input"]["novel"]["manuscripts"]["1"])

    def test_apply_ignores_browser_body(self):
        task = self.task(stage="outline")
        payload, status = self.service.api_apply(self.book["id"],
            {"stage": "outline", "task_id": task["id"], "text": "伪造大纲"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["book"]["outline"], "经过文件校验的真实正文。")

    def test_partial_cannot_save_as_manuscript(self):
        task = self.task(final=False)
        payload, status = self.service.api_save_manuscript(self.book["id"], 1,
            {"kind": "ai", "task_id": task["id"], "content": "不能回退到此正文"})
        self.assertEqual(status, 400, payload)
        self.assertIn("TXT", payload["error"])

    def test_wrong_chapter_is_rejected_before_file_consumption(self):
        task = self.task()
        _, status = self.service.api_save_manuscript(self.book["id"], 2,
            {"kind": "ai", "task_id": task["id"], "content": "错误章节"})
        self.assertEqual(status, 409)

    def test_deleted_ai_task_cannot_fall_back_to_browser_cache(self):
        task = self.task()
        self.studio_store.delete_task(task["id"])
        payload, status = self.service.api_save_manuscript(self.book["id"], 1,
            {"kind": "ai", "task_id": task["id"], "content": "旧页面的缓存正文"})
        self.assertEqual(status, 404, payload)
        self.assertIn("来源任务不存在", payload["error"])

    def test_manual_edit_keeps_current_editor_snapshot(self):
        task = self.task()
        payload, status = self.service.api_save_manuscript(self.book["id"], 1,
            {"kind": "manual", "task_id": task["id"], "content": "人工修改后的正文"})
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["manuscript"]["content"], "人工修改后的正文")
