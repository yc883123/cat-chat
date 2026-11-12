"""拆书匠末章后续写：明确选稿、只读副本、原子导入和真实流水线重试。"""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from naiba.core.contracts import WRITER_SETUP_SNAPSHOT_KEY
from naiba.novel import manuscripts
from naiba.novel.service import NovelStore
from naiba.writer.service import WriterService
from naiba.writer.store import WriterStoreError
from tests.test_writer_service import _App


SUMMARY = {"characters_state": "沈砚：刚回到旧船，左手受伤。", "relations_state": "沈砚与老周仍然合作。",
           "plot_state": "船已靠岸，但陌生照片尚未解释。", "next_outline": "沈砚找到照片背面的日期，继续追查。"}


def fingerprints(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


class NovelContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = _App(self.root)
        self.novel = NovelStore(self.root)
        self.app.novel = SimpleNamespace(store=self.novel)
        self.service = WriterService(self.app)
        self.store = self.service.store
        text = "".join(f"第{no}章 标题{no}\n沈砚与老周在船上。第{no}章原文。\n" for no in range(1, 4))
        staged = self.novel.stage_import(filename="续写源.txt", data=text.encode("utf-8"), max_len=6000)
        self.source = self.novel.create_book(import_id=staged["import_id"], picked=[1, 2, 3])
        self.selection = {"book_id": self.source["id"], "source_kind": "original", "end_chapter_no": 3}

    def preview(self, **changes):
        selected = {**self.selection, **changes}
        payload, code = self.service.api_preview_novel_import(selected)
        self.assertEqual(code, 200, payload)
        self.assertTrue(payload["ready"], payload)
        self.assertNotIn("_chapters", payload)
        return selected, payload

    def commit(self, **changes):
        selected, preview = self.preview(**changes)
        body = {**selected, "fingerprint": preview["fingerprint"], "request_key": "continuation-request-0001",
                "summary": copy.deepcopy(SUMMARY), "confirmed": True}
        payload, code = self.service.api_import_novel(body)
        self.assertEqual(code, 201, payload)
        return body, payload

    def test_index_only_does_not_choose_manuscripts_or_write_source(self):
        before = fingerprints(self.novel.root())
        payload, status = self.service.api_preview_novel_import({"book_id": self.source["id"]})
        self.assertEqual(status, 200)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["source_kind"], "")
        self.assertEqual([row["no"] for row in payload["chapter_sources"]], [1, 2, 3])
        self.assertTrue(all(row["manuscripts"][0]["kind"] == "original" for row in payload["chapter_sources"]))
        self.assertEqual(fingerprints(self.novel.root()), before)
        self.assertEqual(self.store.list_books()["books"], [])

    def test_continuation_starts_with_empty_work_prompt(self):
        self.novel.save_system_prompt(self.source["id"], text="仅用于原书改编的规则", expected_revision=0)
        _, result = self.commit()
        book = result["book"]
        self.assertEqual(book["global_system_prompt"], "")
        self.assertEqual(book["global_system_prompt_revision"], 0)
        self.assertEqual(self.novel.get_book(self.source["id"])["book"]["global_system_prompt"], "仅用于原书改编的规则")

    def test_original_copy_is_read_only_and_new_chapter_after_end(self):
        before = fingerprints(self.novel.root())
        _, payload = self.commit()
        bid = payload["book"]["id"]
        detail = self.store.get_book(bid)
        self.assertEqual(payload["new_chapter_no"], 4)
        self.assertEqual([row["no"] for row in detail["chapters"]], [1, 2, 3, 4])
        self.assertEqual(detail["book"]["continuation"]["summary"], SUMMARY)
        for no in range(1, 4):
            chapter = self.store.get_chapter(bid, no)
            original = self.novel.get_chapter(self.source["id"], no)
            self.assertEqual(chapter["content"], original["content"])
            self.assertTrue(chapter["read_only"])
            self.assertEqual(chapter["source"]["source_book_id"], self.source["id"])
            self.assertEqual(chapter["source"]["source_chapter_no"], no)
            self.assertEqual(chapter["source"]["source_manuscript_id"], original["base_manuscript_id"])
            self.assertEqual(chapter["source"]["source_kind"], "original")
            self.assertIsInstance(chapter["source"]["imported_at"], int)
            self.assertEqual(chapter["versions"][0]["source"], chapter["source"])
        new = self.store.get_chapter(bid, 4)
        self.assertFalse(new["read_only"])
        self.assertEqual(new["content"], "")
        self.assertEqual(new["beat"], SUMMARY["next_outline"])
        self.assertEqual(fingerprints(self.novel.root()), before)

    def test_adapted_selection_is_per_chapter_and_cannot_fallback(self):
        chosen = {}
        for no in range(1, 4):
            chosen[str(no)] = self.novel.save_manuscript(self.source["id"], no, content=f"第{no}章分叉后的改编正文。")['manuscript']['id']
        payload, status = self.service.api_preview_novel_import({**self.selection, "source_kind": "manuscript"})
        self.assertEqual(status, 200)
        self.assertFalse(payload["ready"])
        self.assertEqual({issue["chapter_no"] for issue in payload["issues"]}, {1, 2, 3})
        _, result = self.commit(source_kind="manuscript", manuscripts_by_chapter=chosen)
        for no in range(1, 4):
            chapter = self.store.get_chapter(result["book"]["id"], no)
            self.assertEqual(chapter["content"], f"第{no}章分叉后的改编正文。")
            self.assertEqual(chapter["source"]["source_manuscript_id"], chosen[str(no)])
        self.assertEqual(result["book"]["continuation"]["coverage"]["divergent_chapters"], 3)

    def test_missing_corrupt_or_ambiguous_original_makes_comparison_unknown(self):
        chosen = {}
        originals = {}
        for no in range(1, 4):
            chapter = self.novel.get_chapter(self.source["id"], no)
            originals[no] = chapter["base_manuscript_id"]
            chosen[str(no)] = self.novel.save_manuscript(
                self.source["id"], no, content=chapter["content"],
            )["manuscript"]["id"]
        book_dir = self.novel._book_dir(self.source["id"])
        index_path = self.novel._chapter_path(book_dir, 2)
        original_path = self.novel._manuscript_path(book_dir, 2, originals[2])
        index_before, original_before = index_path.read_bytes(), original_path.read_bytes()
        selected = {**self.selection, "source_kind": "manuscript", "manuscripts_by_chapter": chosen}
        _, complete = self.preview(**selected)
        self.assertEqual(complete["coverage"]["divergent_chapters"], 0)
        for damage in ("missing", "corrupt", "ambiguous"):
            with self.subTest(damage=damage):
                index_path.write_bytes(index_before)
                original_path.write_bytes(original_before)
                if damage == "missing":
                    self.novel.delete_manuscript(self.source["id"], 2, originals[2])
                elif damage == "corrupt":
                    original_path.write_text("{损坏的 JSON", encoding="utf-8")
                else:
                    additional = manuscripts.build(chapter_no=2, kind=manuscripts.KIND_ORIGINAL,
                                                   content="另一份无法确认的原文", no=0)
                    self.novel._write_json(self.novel._manuscript_path(book_dir, 2, additional["id"]), additional)
                    index = self.novel._load_chapter_index(book_dir, 2)
                    index["manuscript_ids"].append(additional["id"])
                    self.novel._write_json(index_path, index)
                before = fingerprints(self.novel.root())
                _, preview = self.preview(**selected)
                self.assertEqual(preview["coverage"]["comparison"], "unknown")
                self.assertIsNone(preview["coverage"]["divergent_chapters"])
                self.assertEqual({str(row["no"]): row["manuscript_id"] for row in preview["selected_chapters"]}, chosen)
                self.assertEqual(fingerprints(self.novel.root()), before)

    def test_cutoff_imports_only_prefix_then_creates_cutoff_plus_one(self):
        _, result = self.commit(end_chapter_no=2)
        self.assertEqual(result["new_chapter_no"], 3)
        self.assertEqual([row["no"] for row in self.store.get_book(result["book"]["id"])["chapters"]], [1, 2, 3])
        self.assertEqual(self.store.get_chapter(result["book"]["id"], 3)["content"], "")
        self.assertTrue(self.novel.get_chapter(self.source["id"], 3)["content"])

    def test_missing_middle_chapter_allows_listing_then_shorter_cutoff_only(self):
        self.novel.delete_chapter(self.source["id"], 2)
        before = fingerprints(self.novel.root())
        listed, status = self.service.api_preview_novel_import({"book_id": self.source["id"]})
        self.assertEqual(status, 200, listed)
        self.assertFalse(listed["ready"])
        self.assertEqual([row["no"] for row in listed["chapter_sources"]], [1, 3])
        rejected, status = self.service.api_preview_novel_import(self.selection)
        self.assertEqual(status, 400, rejected)
        self.assertIn("缺少章节：2", rejected["error"])
        selected, preview = self.preview(end_chapter_no=1)
        rejected, status = self.service.api_import_novel({**self.selection, "fingerprint": preview["fingerprint"],
                                                        "request_key": "missing-middle-bad", "summary": SUMMARY,
                                                        "confirmed": True})
        self.assertEqual(status, 400, rejected)
        self.assertEqual(self.store.list_books()["books"], [])
        _, result = self.commit(**selected)
        self.assertEqual(result["new_chapter_no"], 2)
        self.assertEqual([row["no"] for row in self.store.get_book(result["book"]["id"])["chapters"]], [1, 2])
        self.assertEqual(fingerprints(self.novel.root()), before)

    def test_broken_selected_chapter_blocks_import_without_partial_book(self):
        body, preview = self.preview()
        body.update(fingerprint=preview["fingerprint"], request_key="broken-request-0001", summary=SUMMARY, confirmed=True)
        chapter = self.novel.get_chapter(self.source["id"], 2)
        self.novel._manuscript_path(self.novel._book_dir(self.source["id"]), 2, chapter["base_manuscript_id"]).write_text("{", encoding="utf-8")
        payload, status = self.service.api_import_novel(body)
        self.assertEqual(status, 400, payload)
        self.assertTrue(any(issue["chapter_no"] == 2 for issue in payload["issues"]))
        self.assertEqual(self.store.list_books()["books"], [])

    def test_changed_source_between_preview_and_commit_requires_new_confirmation(self):
        selection, preview = self.preview()
        chapter = self.novel.get_chapter(self.source["id"], 3)
        path = self.novel._manuscript_path(self.novel._book_dir(self.source["id"]), 3, chapter["base_manuscript_id"])
        changed = json.loads(path.read_text(encoding="utf-8"))
        changed["content"] = "源文件已被外部修复。"
        path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        result, status = self.service.api_import_novel({**selection, "fingerprint": preview["fingerprint"],
                                                       "request_key": "changed-request-0001", "summary": SUMMARY, "confirmed": True})
        self.assertEqual(status, 409, result)
        self.assertEqual(self.store.list_books()["books"], [])

    def test_confirmation_is_required_and_long_manual_input_not_truncated(self):
        selection, preview = self.preview()
        base = {**selection, "fingerprint": preview["fingerprint"], "request_key": "invalid-request-0001",
                "summary": SUMMARY, "confirmed": True}
        for change in ({"confirmed": False}, {"confirmed": "true"}, {"summary": {}},
                       {"summary": {**SUMMARY, "plot_state": "长" * 2001}}):
            result, status = self.service.api_import_novel({**base, **change})
            self.assertEqual(status, 400, result)
        self.assertEqual(self.store.list_books()["books"], [])

    def test_retry_after_lost_response_is_idempotent_even_after_restart(self):
        body, result = self.commit()
        service = WriterService(self.app)
        retried, status = service.api_import_novel(body)
        self.assertEqual(status, 200, retried)
        self.assertTrue(retried["duplicate"])
        self.assertEqual(retried["book"]["id"], result["book"]["id"])
        self.assertEqual(len(service.store.list_books()["books"]), 1)
        conflict, status = service.api_import_novel({**body, "summary": {**SUMMARY, "plot_state": "已改变"}})
        self.assertEqual(status, 409, conflict)
        self.assertEqual(len(service.store.list_books()["books"]), 1)

    def test_failed_staging_does_not_publish_a_partial_book(self):
        selection, preview = self.preview()
        real_write = self.store._write_json

        def fail_second_chapter(path, payload):
            if path.parent.name == "chapters" and path.name == "0002.json":
                raise OSError("模拟磁盘写入失败")
            return real_write(path, payload)

        with mock.patch.object(self.store, "_write_json", side_effect=fail_second_chapter):
            with self.assertRaises(OSError):
                self.service.api_import_novel({**selection, "fingerprint": preview["fingerprint"],
                                              "request_key": "disk-failure-request", "summary": SUMMARY, "confirmed": True})
        self.assertEqual(self.store.list_books()["books"], [])
        self.assertEqual(list(self.store._root.glob(".*.tmp")), [])

    def test_imported_chapters_reject_all_existing_mutations(self):
        _, result = self.commit()
        bid = result["book"]["id"]
        first = self.store.get_chapter(bid, 1)
        outline = self.store.get_book(bid)["outline"]["chapters"]
        before = fingerprints(self.store._book_dir(bid))
        actions = [lambda: self.store.save_chapter_version(bid, 1, content="覆盖"),
                   lambda: self.store.update_chapter_meta(bid, 1, title="覆盖"),
                   lambda: self.store.select_chapter_version(bid, 1, version_id=first["current_version_id"]),
                   lambda: self.store.delete_chapter_versions(bid, 1, [first["current_version_id"]]),
                   lambda: self.store.delete_chapter(bid, 1), lambda: self.store.clear_chapters(bid),
                   lambda: self.store.sync_chapters(bid, allow_drop=True),
                   lambda: self.store.save_outline(bid, chapters=outline, sync=True, allow_drop=True)]
        for action in actions:
            with self.assertRaisesRegex(WriterStoreError, "只读"):
                action()
            self.assertEqual(fingerprints(self.store._book_dir(bid)), before)
        for kind, params in (("chapter", {"chapter_no": 1}), ("chapter_continue", {"chapter_no": 1}),
                             ("chapter_all", {"from_no": 1, "want_count": 4})):
            data, status = self.service.api_start_pipeline(bid, {"kind": kind, "model_name": "probe", **params})
            self.assertEqual(status, 400, data)
        self.assertFalse(self.app.studio.jobs.submitted)
        self.assertEqual(fingerprints(self.store._book_dir(bid)), before)

    def test_new_chapter_uses_confirmed_context_and_retry_keeps_snapshot(self):
        _, result = self.commit()
        bid = result["book"]["id"]
        source_before = fingerprints(self.novel.root())
        started, status = self.service.api_start_pipeline(bid, {"kind": "chapter", "chapter_no": 4,
                                                              "model_key": "online:probe", "model_name": "probe"})
        self.assertEqual(status, 201, started)
        pipeline = started["pipeline"]
        snapshot = copy.deepcopy(pipeline["request_snapshot"])
        self.assertEqual(snapshot["previous_chapter"]["chapter_no"], 3)
        self.assertIn("第3章原文", snapshot["previous_chapter"]["excerpt"])
        task = self.app.studio.jobs.tasks[pipeline["current_task_id"]]
        self.assertIn(SUMMARY["plot_state"], json.dumps(task["input"], ensure_ascii=False))
        self.assertIn(SUMMARY["relations_state"], json.dumps(task["input"], ensure_ascii=False))
        self.app.studio.jobs.finish(task["id"], "", status="failed", error="模拟生成失败")
        self.service.pipeline.tick()
        retried, status = self.service.api_retry_pipeline(bid, {})
        self.assertEqual(status, 200, retried)
        self.assertEqual(retried["pipeline"]["request_snapshot"], snapshot)
        self.service.pipeline.tick()
        retry_pipeline = self.store.get_active_pipeline(bid)
        retry_task = self.app.studio.jobs.tasks[retry_pipeline["current_task_id"]]
        self.assertIn(SUMMARY["characters_state"], json.dumps(retry_task["input"], ensure_ascii=False))
        self.assertEqual(self.store.get_chapter(bid, 4)["beat"], SUMMARY["next_outline"])
        self.assertEqual(len(self.store.list_books()["books"]), 1)
        self.assertEqual(fingerprints(self.novel.root()), source_before)

    def test_settings_folder_import_preserves_summary_in_version_and_real_task(self):
        _, result = self.commit()
        bid = result["book"]["id"]
        imported = self.store.import_settings_folder(
            bid, [{"name": "11_目录摘要与投稿简介.md", "content": "简介：这是一部悬疑小说。"}])
        setup = self.store.load_setup(bid)
        self.assertEqual(setup["continuation_summary"], SUMMARY)
        persisted = json.loads(self.store._setup_path(self.store._book_dir(bid), setup["id"]).read_text(encoding="utf-8"))
        self.assertEqual(persisted["continuation_summary"], SUMMARY)
        preview, status = self.service.api_preview({"book_id": bid, "kind": "chapter", "chapter_no": 4,
                                                    "continuation_summary": {"plot_state": "请求体伪造摘要"}})
        self.assertEqual(status, 200, preview)
        for value in SUMMARY.values():
            self.assertIn(value, preview["content"])
        self.assertNotIn("请求体伪造摘要", preview["content"])
        started, status = self.service.api_start_pipeline(bid, {"kind": "chapter", "chapter_no": 4,
                                                              "model_key": "online:probe", "model_name": "probe"})
        self.assertEqual(status, 201, started)
        task = self.app.studio.jobs.tasks[started["pipeline"]["current_task_id"]]
        for value in SUMMARY.values():
            self.assertIn(value, json.dumps(task["input"], ensure_ascii=False))

    def test_legacy_missing_summary_and_deleted_setups_keep_read_only_fallback(self):
        _, result = self.commit()
        bid = result["book"]["id"]
        initial = self.store.load_setup(bid)
        path = self.store._setup_path(self.store._book_dir(bid), initial["id"])
        old_record = json.loads(path.read_text(encoding="utf-8"))
        old_record.pop("continuation_summary")
        path.write_text(json.dumps(old_record, ensure_ascii=False), encoding="utf-8")
        self.store.save_setup(bid, content={"title": "另存的新设定", "idea": "承接已有小说"})
        self.store.select_setup_version(bid, version_id=initial["id"])
        before = fingerprints(self.store._book_dir(bid))
        self.assertEqual(self.store.load_setup(bid)["continuation_summary"], SUMMARY)
        preview, status = self.service.api_preview({"book_id": bid, "kind": "chapter", "chapter_no": 4})
        self.assertEqual(status, 200, preview)
        for value in SUMMARY.values():
            self.assertIn(value, preview["content"])
        self.assertEqual(fingerprints(self.store._book_dir(bid)), before)
        manifest = self.store._load_manifest(bid)
        self.store.delete_setup_versions(bid, [row["id"] for row in manifest["setup_versions"]])
        self.assertEqual(self.store.load_setup(bid)["continuation_summary"], SUMMARY)
        preview, status = self.service.api_preview({"book_id": bid, "kind": "chapter", "chapter_no": 4})
        self.assertEqual(status, 200, preview)
        for value in SUMMARY.values():
            self.assertIn(value, preview["content"])

    def test_existing_version_summary_stays_frozen_after_later_summary_change(self):
        _, result = self.commit()
        bid = result["book"]["id"]
        initial = self.store.load_setup(bid)
        started, status = self.service.api_start_pipeline(bid, {"kind": "chapter", "chapter_no": 4,
                                                              "model_key": "online:probe", "model_name": "probe"})
        self.assertEqual(status, 201, started)
        pipeline = started["pipeline"]
        self.app.studio.jobs.finish(pipeline["current_task_id"], "", status="failed", error="模拟生成失败")
        self.service.pipeline.tick()
        changed = {key: "新确认：" + value for key, value in SUMMARY.items()}
        self.store.save_setup(bid, content={**initial, "continuation_summary": changed})
        self.assertEqual(self.store.load_setup(bid)["continuation_summary"], changed)
        self.assertEqual(self.store.load_setup(bid, version_id=initial["id"])["continuation_summary"], SUMMARY)
        retried, status = self.service.api_retry_pipeline(bid, {})
        self.assertEqual(status, 200, retried)
        self.service.pipeline.tick()
        retry_pipeline = self.store.get_active_pipeline(bid)
        retry_task = self.app.studio.jobs.tasks[retry_pipeline["current_task_id"]]
        content = json.dumps(retry_task["input"], ensure_ascii=False)
        for value in SUMMARY.values():
            self.assertIn(value, content)
        self.assertNotIn("新确认：", content)
        self.store.select_setup_version(bid, version_id=initial["id"])
        preview, status = self.service.api_preview({"book_id": bid, "kind": "chapter", "chapter_no": 4})
        self.assertEqual(status, 200, preview)
        self.assertNotIn("新确认：", preview["content"])

    def _check_fallback_setup_freezes_real_task(self, *, clear_before=False, clear_after=False):
        _, result = self.commit()
        bid = result["book"]["id"]
        initial = self.store.load_setup(bid)
        path = self.store._setup_path(self.store._book_dir(bid), initial["id"])
        record = json.loads(path.read_text(encoding="utf-8"))
        record.pop("continuation_summary")
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        if clear_before:
            self.store.delete_setup_versions(bid, [initial["id"]])
        started, status = self.service.api_start_pipeline(bid, {"kind": "chapter", "chapter_no": 4,
                                                              "model_key": "online:probe", "model_name": "probe",
                                                              WRITER_SETUP_SNAPSHOT_KEY: {"idea": "伪造设定"}})
        self.assertEqual(status, 201, started)
        pipeline = started["pipeline"]
        snapshot = copy.deepcopy(pipeline["request_snapshot"])
        self.assertEqual(snapshot[WRITER_SETUP_SNAPSHOT_KEY]["continuation_summary"], SUMMARY)
        task = self.app.studio.jobs.tasks[pipeline["current_task_id"]]
        for value in SUMMARY.values():
            self.assertIn(value, json.dumps(task["input"], ensure_ascii=False))
        self.assertNotIn("伪造设定", json.dumps(task["input"], ensure_ascii=False))
        self.app.studio.jobs.finish(task["id"], "", status="failed", error="模拟首次生成失败")
        self.service.pipeline.tick()
        changed = {key: "新确认：" + value for key, value in SUMMARY.items()}
        self.store.save_setup(bid, content={**initial, "continuation_summary": changed})
        if clear_after:
            manifest = self.store._load_manifest(bid)
            self.store.delete_setup_versions(bid, [row["id"] for row in manifest["setup_versions"]])
        retried, status = self.service.api_retry_pipeline(bid, {})
        self.assertEqual(status, 200, retried)
        self.assertEqual(retried["pipeline"]["request_snapshot"], snapshot)
        retry_task = self.app.studio.jobs.tasks[self.store.get_active_pipeline(bid)["current_task_id"]]
        content = json.dumps(retry_task["input"], ensure_ascii=False)
        for value in SUMMARY.values():
            self.assertIn(value, content)
        self.assertNotIn("新确认：", content)
        self.app.studio.jobs.finish(retry_task["id"], "", status="failed", error="模拟第二次生成失败")
        self.service.pipeline.tick()
        rebased, status = self.service.api_retry_pipeline(bid, {"rebase": True})
        self.assertEqual(status, 200, rebased)
        self.assertEqual(rebased["pipeline"]["request_snapshot"][WRITER_SETUP_SNAPSHOT_KEY]["continuation_summary"], changed)
        task = self.app.studio.jobs.tasks[self.store.get_active_pipeline(bid)["current_task_id"]]
        for value in changed.values():
            self.assertIn(value, json.dumps(task["input"], ensure_ascii=False))

    def test_legacy_setup_without_summary_freezes_manifest_fallback_before_retry(self):
        self._check_fallback_setup_freezes_real_task()

    def test_cleared_setup_freezes_manifest_fallback_before_retry(self):
        self._check_fallback_setup_freezes_real_task(clear_before=True)

    def test_deleting_all_setups_after_failure_does_not_change_frozen_retry(self):
        self._check_fallback_setup_freezes_real_task(clear_after=True)

    def test_legacy_pipeline_without_snapshot_uses_stored_summary_or_requires_rebase(self):
        _, result = self.commit()
        bid = result["book"]["id"]
        initial = self.store.load_setup(bid)
        started, status = self.service.api_start_pipeline(bid, {"kind": "chapter", "chapter_no": 4,
                                                              "model_key": "online:probe", "model_name": "probe"})
        self.assertEqual(status, 201, started)
        pipeline = started["pipeline"]
        self.app.studio.jobs.finish(pipeline["current_task_id"], "", status="failed", error="旧任务失败")
        self.service.pipeline.tick()
        self.store.update_pipeline(bid, pipeline["id"],
                                   lambda row: row["request_snapshot"].pop(WRITER_SETUP_SNAPSHOT_KEY))
        retried, status = self.service.api_retry_pipeline(bid, {})
        self.assertEqual(status, 200, retried)
        task = self.app.studio.jobs.tasks[self.store.get_active_pipeline(bid)["current_task_id"]]
        self.assertIn(SUMMARY["plot_state"], json.dumps(task["input"], ensure_ascii=False))
        self.app.studio.jobs.finish(task["id"], "", status="failed", error="旧任务再次失败")
        self.service.pipeline.tick()
        path = self.store._setup_path(self.store._book_dir(bid), initial["id"])
        record = json.loads(path.read_text(encoding="utf-8"))
        record.pop("continuation_summary")
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        rejected, status = self.service.api_retry_pipeline(bid, {})
        self.assertEqual(status, 409, rejected)
        self.assertIn("基于当前版本重新生成", rejected["error"])
        self.assertEqual(self.store.get_active_pipeline(bid)["status"], "failed")
        rebased, status = self.service.api_retry_pipeline(bid, {"rebase": True})
        self.assertEqual(status, 200, rebased)
        self.assertEqual(rebased["pipeline"]["request_snapshot"][WRITER_SETUP_SNAPSHOT_KEY]["continuation_summary"], SUMMARY)

    def test_no_fake_coverage_and_recent_name_counts_use_selected_text(self):
        source_dir = self.novel._book_dir(self.source["id"])
        manifest = self.novel._load_manifest(self.source["id"])
        manifest["elements"] = {"characters": ["沈砚：主角", "老周：船夫", "不出现的人：旧人物"]}
        self.novel._write_json(source_dir / "book.json", manifest)
        _, preview = self.preview()
        self.assertEqual(preview["coverage"]["characters"], 3)
        self.assertEqual(preview["coverage"]["recent_characters"], 2)
        self.assertIsNone(preview["coverage"]["relations"])
        self.assertEqual(preview["coverage"]["plot_status"], "needs_confirmation")


if __name__ == "__main__":
    unittest.main()
