# -*- coding: utf-8 -*-
"""坏章预检、当前成果裁决与批量删除的实际落盘回归。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from naiba.novel.service import NovelIntegrityError, NovelServiceError
from tests.test_novel_records_api import ServiceTestCase


class ChapterPreflightTests(ServiceTestCase):
    def test_export_reports_all_broken_chapters_without_partial_text(self):
        book_dir = self.service.store._book_dir(self.book_id)
        self.service.store._chapter_path(book_dir, 2).write_text("not json", encoding="utf-8")
        base = self.service.store.get_chapter(self.book_id, 3)["base_manuscript_id"]
        self.service.store._manuscript_path(book_dir, 3, base).unlink()
        result, status = self.service.api_export_book(self.book_id)
        self.assertEqual(status, 400, result)
        self.assertEqual([row["chapter_no"] for row in result["issues"]], [2, 3])
        self.assertTrue(all(row["reason"] for row in result["issues"]))
        self.assertNotIn("text", result)
        self.assertNotIn("filename", result)

    def test_blank_manuscript_is_an_error(self):
        store = self.service.store
        book_dir = store._book_dir(self.book_id)
        chapter = store.get_chapter(self.book_id, 1)
        path = store._manuscript_path(book_dir, 1, chapter["base_manuscript_id"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["content"] = " \n\t "
        store._write_json(path, payload)
        with self.assertRaises(NovelIntegrityError) as caught:
            store.preflight_chapters(self.book_id)
        self.assertEqual(caught.exception.issues[0]["chapter_no"], 1)
        self.assertIn("为空", str(caught.exception))

    def test_explicit_missing_manuscript_does_not_fall_back(self):
        with self.assertRaises(NovelIntegrityError) as caught:
            self.service.store.preflight_chapters(self.book_id, [1], {1: "ms-ffffffffffff"})
        self.assertEqual(caught.exception.issues[0]["manuscript_id"], "ms-ffffffffffff")
        self.assertIn("没有这份稿件", str(caught.exception))

    def test_explicit_selection_requires_every_selected_chapter(self):
        base = self.service.store.get_chapter(self.book_id, 1)["base_manuscript_id"]
        with self.assertRaises(NovelIntegrityError) as caught:
            self.service.store.preflight_chapters(self.book_id, [1, 2, 3], {1: base}, require_explicit=True)
        self.assertEqual([row["chapter_no"] for row in caught.exception.issues], [2, 3])

    def test_snapshot_preserves_source_and_is_independent_of_later_writes(self):
        store = self.service.store
        adapted = store.save_manuscript(self.book_id, 1, content="选定改编正文。")["manuscript"]
        selected = {number: store.get_chapter(self.book_id, number)["base_manuscript_id"] for number in (1, 2)}
        selected[1] = adapted["id"]
        snapshot = store.preflight_chapters(self.book_id, [2, 1], selected, require_explicit=True)
        self.assertEqual(snapshot["chapter_ids"], [1, 2])
        self.assertEqual(snapshot["chapters"][0]["content"], "选定改编正文。")
        self.assertEqual(snapshot["chapters"][0]["manuscript_kind"], "manual")
        self.assertEqual(snapshot["chapters"][0]["manuscript_no"], adapted["no"])
        store.delete_manuscript(self.book_id, 1, adapted["id"])
        self.assertEqual(snapshot["chapters"][0]["content"], "选定改编正文。")
        self.assertIn(adapted["id"], snapshot["chapters"][0]["manuscript_id"])

    def test_preflight_does_not_upgrade_or_rewrite_old_source(self):
        store = self.service.store
        book_dir = store._book_dir(self.book_id)
        manifest_path = book_dir / "book.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["schema_version"] = 1
        store._write_json(manifest_path, manifest)
        before = {str(path.relative_to(book_dir)): path.read_bytes()
                  for path in book_dir.rglob("*") if path.is_file()}
        with self.assertRaises(NovelIntegrityError):
            store.preflight_chapters(self.book_id)
        after = {str(path.relative_to(book_dir)): path.read_bytes()
                 for path in book_dir.rglob("*") if path.is_file()}
        self.assertEqual(before, after)


class CurrentVersionTests(ServiceTestCase):
    def test_deleting_latest_outline_updates_display_material_and_export(self):
        store = self.service.store
        store.apply_outline(self.book_id, task_id="outline-1", text="剩余旧大纲")
        newest = store.apply_outline(self.book_id, task_id="outline-2", text="已删除新大纲")
        latest_id = newest["outline_versions"][-1]["id"]
        result = store.delete_records(self.book_id, [{"type": "outline", "id": latest_id}])
        self.assertTrue(result["ok"])
        self.assertEqual(result["book"]["outline"], "剩余旧大纲")
        self.assertEqual(result["book"]["outline_version"], 1)
        material = store.stage_material(self.book_id, [1])
        self.assertEqual(material["manifest"]["outline"], "剩余旧大纲")
        exported = store.export_book(self.book_id, with_extras=True)
        self.assertIn("剩余旧大纲", exported)
        self.assertNotIn("已删除新大纲", exported)
        payload, status = self._submit(stage="script", chapter_ids=[1])
        self.assertEqual(status, 201, payload)
        self.assertIn("剩余旧大纲", self.app.studio.jobs.submitted[-1]["content"])
        self.assertNotIn("已删除新大纲", self.app.studio.jobs.submitted[-1]["content"])

    def test_deleting_last_versions_clears_every_current_cache(self):
        store = self.service.store
        store.apply_elements(self.book_id, task_id="elements", raw="## 人物\n· 临时人物")
        store.apply_outline(self.book_id, task_id="outline", text="临时大纲")
        current = store.apply_script(self.book_id, task_id="script", text="INT. 房间\n临时场次", from_chapter=1)
        items = [{"type": kind, "id": current[f"{kind}_versions"][0]["id"]}
                 for kind in ("elements", "outline", "script")]
        result = store.delete_records(self.book_id, items)
        self.assertTrue(result["ok"])
        book = store.get_book(self.book_id)["book"]
        self.assertEqual(book["elements"]["raw"], "")
        self.assertEqual(book["elements"]["characters"], [])
        self.assertEqual(book["outline"], "")
        self.assertEqual(book["outline_version"], 0)
        self.assertEqual(book["script"], [])
        self.assertEqual(book["script_version"], 0)
        exported = store.export_book(self.book_id, with_extras=True)
        self.assertNotIn("临时大纲", exported)
        self.assertNotIn("临时人物", exported)

    def test_script_deletion_restores_remaining_snapshot_and_metadata(self):
        store = self.service.store
        old = store.apply_script(self.book_id, task_id="script-1", text="原场次", from_chapter=1)
        old_script = old["script"]
        newest = store.apply_script(self.book_id, task_id="script-2", text="新场次", from_chapter=1)
        store.delete_records(self.book_id, [{"type": "script", "id": newest["script_versions"][-1]["id"]}])
        manifest = store._load_manifest(self.book_id)
        self.assertEqual(manifest["script"], old_script)
        self.assertEqual(manifest["script_version"], 1)
        self.assertEqual(manifest["script_task_id"], "script-1")

    def test_next_version_number_does_not_collide_after_earlier_deletion(self):
        store = self.service.store
        for number in range(1, 4):
            state = store.apply_outline(self.book_id, task_id=f"outline-{number}", text=f"大纲{number}")
        store.delete_records(self.book_id, [{"type": "outline", "id": state["outline_versions"][0]["id"]}])
        result = store.apply_outline(self.book_id, task_id="outline-4", text="大纲4")
        self.assertEqual([row["version"] for row in result["outline_versions"]], [2, 3, 4])


class PartialDeleteTests(ServiceTestCase):
    def _ids(self, chapter_no):
        return [row["id"] for row in self.service.store.get_chapter(self.book_id, chapter_no)["manuscripts"]]

    def test_runtime_failure_returns_successes_and_remaining_failures(self):
        store = self.service.store
        first = store.save_manuscript(self.book_id, 1, content="删除成功")["manuscript"]
        second = store.save_manuscript(self.book_id, 2, content="删除失败")["manuscript"]
        original = store.delete_manuscript

        def delete(book_id, chapter_no, manuscript_id):
            if chapter_no == 2:
                raise OSError("测试磁盘删除失败")
            return original(book_id, chapter_no, manuscript_id)

        items = [{"type": "manuscript", "chapter_no": number, "id": row["id"]}
                 for number, row in ((1, first), (2, second))]
        with mock.patch.object(store, "delete_manuscript", side_effect=delete):
            payload, status = self.service.api_delete_records(self.book_id, {"items": items})
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["partial"])
        self.assertEqual(payload["succeeded"], [items[0]])
        self.assertEqual(payload["failed"][0]["id"], second["id"])
        self.assertNotIn(first["id"], self._ids(1))
        self.assertIn(second["id"], self._ids(2))

    def test_collectively_removing_last_manuscripts_rejects_before_any_write(self):
        store = self.service.store
        store.save_manuscript(self.book_id, 1, content="仅有的改编稿")
        chapter = store.get_chapter(self.book_id, 1)
        items = [{"type": "manuscript", "chapter_no": 1, "id": record["id"]}
                 for record in chapter["manuscripts"]]
        with self.assertRaises(NovelServiceError):
            store.delete_records(self.book_id, items)
        self.assertEqual(self._ids(1), [row["id"] for row in chapter["manuscripts"]])

    def test_unknown_version_rejects_mixed_batch_before_any_write(self):
        store = self.service.store
        adapted = store.save_manuscript(self.book_id, 1, content="仍保留")["manuscript"]
        with self.assertRaises(NovelServiceError):
            store.delete_records(self.book_id, [
                {"type": "manuscript", "chapter_no": 1, "id": adapted["id"]},
                {"type": "outline", "id": "ol-ffffffffffff"},
            ])
        self.assertIn(adapted["id"], self._ids(1))

    def test_manifest_write_failure_restores_index_before_reporting_failure(self):
        store = self.service.store
        adapted = store.save_manuscript(self.book_id, 1, content="磁盘错误后仍保留")["manuscript"]
        before = self._ids(1)
        with mock.patch.object(store, "_bump_manifest", side_effect=OSError("清单写入失败")):
            result = store.delete_records(self.book_id, [{"type": "manuscript", "chapter_no": 1, "id": adapted["id"]}])
        self.assertFalse(result["ok"])
        self.assertEqual(result["succeeded"], [])
        self.assertEqual(self._ids(1), before)
        self.assertEqual(store.get_manuscript(self.book_id, 1, adapted["id"])["content"], "磁盘错误后仍保留")

    def test_duplicate_ids_are_deleted_and_counted_once(self):
        store = self.service.store
        state = store.apply_outline(self.book_id, task_id="outline", text="大纲")
        item = {"type": "outline", "id": state["outline_versions"][0]["id"]}
        result = store.delete_records(self.book_id, [item, item])
        self.assertEqual(result["succeeded"], [item])

    def test_post_commit_cleanup_failure_reports_actual_removal_with_warning(self):
        store = self.service.store
        adapted = store.save_manuscript(self.book_id, 1, content="清理失败的改编稿")["manuscript"]
        path = store._manuscript_path(store._book_dir(self.book_id), 1, adapted["id"])
        revision = store.get_book(self.book_id)["book"]["revision"]
        original_unlink = Path.unlink

        def unlink(target, *args, **kwargs):
            if target == path:
                raise OSError("测试残留文件无法清理")
            return original_unlink(target, *args, **kwargs)

        with mock.patch.object(Path, "unlink", autospec=True, side_effect=unlink):
            result = store.delete_records(self.book_id, [{"type": "manuscript", "chapter_no": 1, "id": adapted["id"]}])
        self.assertTrue(result["ok"])
        self.assertEqual(result["failed"], [])
        self.assertIn("warning", result["succeeded"][0])
        self.assertNotIn(adapted["id"], self._ids(1))
        self.assertGreater(result["book"]["revision"], revision)
        self.assertTrue(path.exists(), "残留文件不能被宣称已物理删除")

    def test_version_write_failure_preserves_current_pointer_and_previous_success(self):
        store = self.service.store
        old = store.apply_outline(self.book_id, task_id="outline-1", text="旧版本")
        latest = store.apply_outline(self.book_id, task_id="outline-2", text="仍应是当前版本")
        original_bump = store._bump_manifest
        calls = 0

        def bump(book_dir, manifest):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("测试第二项写入失败")
            return original_bump(book_dir, manifest)

        first = {"type": "outline", "id": old["outline_versions"][0]["id"]}
        second = {"type": "outline", "id": latest["outline_versions"][-1]["id"]}
        with mock.patch.object(store, "_bump_manifest", side_effect=bump):
            result = store.delete_records(self.book_id, [first, second])
        self.assertTrue(result["partial"])
        self.assertEqual(result["succeeded"], [first])
        self.assertEqual(result["failed"][0]["id"], second["id"])
        self.assertEqual(result["book"]["outline"], "仍应是当前版本")
        self.assertEqual(result["book"]["outline_version"], 2)

    def test_task_delete_runtime_failure_keeps_success_ids(self):
        task_ids = []
        for number in range(2):
            payload, status = self._submit(chapter_ids=[1])
            self.assertEqual(status, 201, payload)
            task_ids.append(payload["task"]["id"])
            self.app.studio.jobs.finish(task_ids[-1])
        original = self.app.studio.jobs.delete

        def delete(ids):
            if ids == [task_ids[1]]:
                raise OSError("测试任务存储失败")
            return original(ids)

        with mock.patch.object(self.app.studio.jobs, "delete", side_effect=delete):
            result, status = self.service.api_delete_tasks({"book_id": self.book_id, "task_ids": task_ids})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["partial"])
        self.assertEqual(result["deleted"], [task_ids[0]])
        self.assertEqual(result["failed"][0]["id"], task_ids[1])
        self.assertIsNone(self.app.studio.jobs.task(task_ids[0]))
        self.assertIsNotNone(self.app.studio.jobs.task(task_ids[1]))

    def test_task_post_delete_error_is_verified_before_reporting_failure(self):
        payload, _ = self._submit(chapter_ids=[1])
        task_id = payload["task"]["id"]
        self.app.studio.jobs.finish(task_id)
        original = self.app.studio.jobs.delete

        def delete(ids):
            original(ids)
            raise OSError("测试删除后的收尾失败")

        with mock.patch.object(self.app.studio.jobs, "delete", side_effect=delete):
            result, status = self.service.api_delete_tasks({"book_id": self.book_id, "task_ids": [task_id]})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["deleted"], [task_id])
        self.assertEqual(result["failed"], [])
        self.assertTrue(result["succeeded"][0]["warning"])


if __name__ == "__main__":
    unittest.main()
