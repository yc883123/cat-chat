# -*- coding: utf-8 -*-
"""护栏：织书台 API 门面契约（实施计划 §8、§12.1）。

经过 `WriterService` 门面（HTTP 边界的状态码与字段形状）覆盖：
作品 CRUD、章节读取与人工保存、版本切换、预检预算、流水线启动/取消/重试、
导入导入确认、导出格式校验，以及计划点名的：
非 done、跨作品任务、空结果、旧 revision、重复写回、客户端伪造正文。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.writer.pipeline import WriterPipelineManager  # noqa: E402
from naiba.writer.service import WriterService  # noqa: E402
from naiba.writer.store import WriterStore  # noqa: E402


class _Jobs:
    def __init__(self):
        self.submitted: list[dict] = []
        self.tasks: dict[str, dict] = {}
        self.cancelled: list[str] = []

    def submit(self, input_data):
        task = {
            "id": f"st-{len(self.submitted) + 1:04d}", "status": "queued",
            "input": json.loads(json.dumps(input_data, ensure_ascii=False)),
            "title": input_data.get("title", ""), "result": "", "result_partial": "",
            "error": "", "finish_reason": "",
        }
        self.submitted.append(task["input"])
        self.tasks[task["id"]] = task
        return task

    def task(self, task_id):
        return self.tasks.get(task_id)

    def cancel(self, task_id):
        self.cancelled.append(task_id)
        task = self.tasks.get(task_id)
        if task is not None and task["status"] in ("queued", "running"):
            task["status"] = "cancelled"
            return True
        return False

    def finish(self, task_id, result, *, status="done", finish_reason="", error=""):
        task = self.tasks[task_id]
        task["status"] = status
        task["result"] = result
        task["finish_reason"] = finish_reason
        task["error"] = error
        return task


class _Config:
    def model_profiles(self):
        return [{"model_key": "online:probe", "model": "probe", "context_window": 100000}]


class _Paths:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _App:
    def __init__(self, data_dir):
        self.paths = _Paths(data_dir)
        self.config = _Config()
        self.studio = type("S", (), {"jobs": None})()
        self.studio.jobs = _Jobs()


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = _App(self.root)
        self.service = WriterService(self.app)
        self.service.pipeline = WriterPipelineManager(self.app, self.service.store)
        self.book = self.service.store.create_book(title="服务测")
        self.book_id = self.book["id"]
        rows = [{"no": i, "title": f"第{i}章", "beat": f"梗概{i}", "hook": ""} for i in range(1, 4)]
        self.service.store.save_outline(self.book_id, chapters=rows, kind="generated", sync=True)

    # ---- 作品 ----

    def test_book_crud_status_codes(self):
        payload, status = self.service.api_create_book({"title": "新书"})
        self.assertEqual(status, 201)
        payload, status = self.service.api_get_book(self.book_id)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["chapters"]), 3)
        payload, status = self.service.api_get_book("wb-20990101-0123456789ab")
        self.assertEqual(status, 404)
        current_revision = self.service.store.get_book(self.book_id)["book"]["revision"]
        payload, status = self.service.api_patch_book(
            self.book_id, {"title": "改名", "revision": current_revision})
        self.assertEqual(status, 200)
        # rename 把 revision 再推一版；拿改名前的 revision 写回 → 409。
        payload, status = self.service.api_patch_book(
            self.book_id, {"title": "旧版写回", "revision": current_revision})
        self.assertEqual(status, 409)
        payload, status = self.service.api_delete_book(self.book_id)
        self.assertEqual(status, 200)

    def test_manual_chapter_save_and_conflict(self):
        payload, status = self.service.api_save_chapter(self.book_id, 1, {"content": "人工正文"})
        self.assertEqual(status, 201)
        self.assertEqual(payload["version"]["kind"], "manual")
        revision = payload["chapter_revision"]
        payload, status = self.service.api_save_chapter(
            self.book_id, 1, {"content": "再次编辑", "revision": revision})
        self.assertEqual(status, 201)
        payload, status = self.service.api_save_chapter(
            self.book_id, 1, {"content": "旧版写回", "revision": revision})
        self.assertEqual(status, 409)
        payload, status = self.service.api_save_chapter(self.book_id, 1, {"content": "  "})
        self.assertEqual(status, 400)
        payload, status = self.service.api_save_chapter(self.book_id, 99, {"content": "x"})
        self.assertEqual(status, 404)

    def test_select_version_validates(self):
        setup = self.service.store.save_setup(self.book_id, content={"idea": "a"}, kind="manual")
        payload, status = self.service.api_select_version(
            self.book_id, {"kind": "setup", "version_id": setup["setup_version_id"]})
        self.assertEqual(status, 200)
        payload, status = self.service.api_select_version(
            self.book_id, {"kind": "setup", "version_id": "su-nonexistent"})
        self.assertEqual(status, 400)
        payload, status = self.service.api_select_version(
            self.book_id, {"kind": "bogus", "version_id": "x"})
        self.assertEqual(status, 400)

    def test_manual_outline_patch_syncs_chapters(self):
        """人工编辑大纲走 PATCH book（织书台前端「保存大纲修改」）：manual 版本 + 章节同步。"""
        current_revision = self.service.store.get_book(self.book_id)["book"]["revision"]
        rows = [
            {"no": 1, "title": "改名后的一章", "beat": "新梗概", "hook": "钩子"},
            {"no": 2, "title": "第二章", "beat": "梗概2", "hook": ""},
        ]
        payload, status = self.service.api_patch_book(
            self.book_id, {"outline": rows, "revision": current_revision, "sync": True})
        self.assertEqual(status, 200)
        book = payload["book"]
        version_id = book["current_outline_version_id"]
        row = next(item for item in book["outline_versions"] if item["id"] == version_id)
        self.assertEqual(row["kind"], "manual")
        self.assertEqual(row["chapter_count"], 2)
        # sync=True：章节索引按新大纲重建（原 3 章 → 2 章）。
        detail, _ = self.service.api_get_book(self.book_id)
        self.assertEqual([c["no"] for c in detail["chapters"]], [1, 2])
        self.assertEqual(detail["chapters"][0]["title"], "改名后的一章")
        # 章节号不连续 → 400，不静默放行。
        payload, status = self.service.api_patch_book(
            self.book_id, {"outline": [{"no": 1, "title": "只有一章", "beat": "", "hook": ""}, {"no": 5, "title": "跳号", "beat": "", "hook": ""}]})
        self.assertEqual(status, 400)

    # ---- 预检 ----

    def test_preview_reports_budget_and_slots(self):
        self.service.store.save_setup(self.book_id, content={"idea": "创意", "worldview": "世界观"}, kind="manual")
        payload, status = self.service.api_preview({
            "book_id": self.book_id, "kind": "chapter", "chapter_no": 1,
            "model_key": "online:probe",
        })
        self.assertEqual(status, 200)
        self.assertTrue(payload["budget"]["context_known"])
        self.assertTrue(payload["budget"]["fits"])
        self.assertIn("梗概1", payload["content"])
        # 章节与设定来源如实列出。
        self.assertEqual(payload["chapter"]["no"], 1)

    def test_preview_unknown_chapter(self):
        payload, status = self.service.api_preview({
            "book_id": self.book_id, "kind": "chapter", "chapter_no": 99})
        self.assertEqual(status, 404)

    # ---- 流水线 ----

    def test_start_pipeline_requires_model(self):
        payload, status = self.service.api_start_pipeline(self.book_id, {"kind": "chapter", "chapter_no": 1})
        self.assertEqual(status, 400)

    def test_start_pipeline_clamps_counts(self):
        # 数量参数夹回合法区间（UI 滑条本来就限 5–30，这里是 API 层的防御性收敛）。
        payload, status = self.service.api_start_pipeline(
            self.book_id, {"kind": "setup_all", "model_name": "probe", "chapter_count": 999})
        self.assertEqual(status, 201)
        self.assertEqual(payload["pipeline"]["request_snapshot"]["chapter_count"], 30)

    def test_start_and_cancel_pipeline(self):
        payload, status = self.service.api_start_pipeline(
            self.book_id, {"kind": "setup_all", "model_name": "probe", "char_count": 2,
                           "chapter_count": 5})
        self.assertEqual(status, 201)
        pipeline = payload["pipeline"]
        self.assertEqual(pipeline["status"], "running")
        # 查询是只读的。
        payload, status = self.service.api_get_pipeline(self.book_id)
        self.assertEqual(payload["pipeline"]["id"], pipeline["id"])
        payload, status = self.service.api_cancel_pipeline(self.book_id)
        self.assertEqual(status, 200)
        self.assertEqual(payload["pipeline"]["status"], "cancelled")
        self.assertTrue(self.app.studio.jobs.cancelled)

    def test_start_pipeline_requires_outline_for_chapter(self):
        service = WriterService(self.app)
        book = service.store.create_book(title="无大纲")
        payload, status = service.api_start_pipeline(
            book["id"], {"kind": "chapter", "chapter_no": 1, "model_name": "probe"})
        self.assertEqual(status, 400)
        self.assertIn("大纲", payload["error"])

    def test_retry_endpoint_maps_conflict_to_409(self):
        self.service.api_start_pipeline(
            self.book_id, {"kind": "setup_all", "model_name": "probe"})
        payload, status = self.service.api_retry_pipeline(self.book_id, {})
        self.assertEqual(status, 409)  # running 不可重试

    # ---- 写回语义（计划点名的场景） ----

    def _running_chapter_task(self, book_id=None):
        book_id = book_id or self.book_id
        payload, _status = self.service.api_start_pipeline(
            book_id, {"kind": "chapter", "chapter_no": 1, "model_name": "probe"})
        pipeline = payload["pipeline"]
        task_id = pipeline["current_task_id"]
        return pipeline, task_id

    def test_non_done_task_does_not_write(self):
        self.service.store.save_setup(self.book_id, content={"idea": "创意"}, kind="manual")
        pipeline, task_id = self._running_chapter_task()
        self.app.studio.jobs.finish(task_id, "正文", status="running")
        self.service.pipeline.tick()
        self.assertEqual(self.service.store.get_chapter(self.book_id, 1)["content"], "")
        record = self.service.store.get_active_pipeline(self.book_id)
        self.assertEqual(record["status"], "running")  # 仍在跑，不误判失败

    def test_cross_book_task_rejected(self):
        self.service.store.save_setup(self.book_id, content={"idea": "创意"}, kind="manual")
        other = self.service.store.create_book(title="别的书")
        rows = [{"no": i, "title": f"第{i}章", "beat": "梗概", "hook": ""} for i in range(1, 3)]
        self.service.store.save_outline(other["id"], chapters=rows, kind="generated", sync=True)
        self.service.store.save_setup(other["id"], content={"idea": "别的"}, kind="manual")
        pipeline, task_id = self._running_chapter_task(other["id"])
        task = self.app.studio.jobs.tasks[task_id]
        # 把任务结果伪造成属于另一本书 → 归属校验失败。
        task["input"]["writer"]["book_id"] = self.book_id
        self.app.studio.jobs.finish(task_id, "正文")
        self.service.pipeline.tick()
        self.assertEqual(self.service.store.get_active_pipeline(other["id"])["status"], "failed")
        self.assertEqual(self.service.store.get_chapter(other["id"], 1)["content"], "")
        self.assertEqual(self.service.store.get_chapter(self.book_id, 1)["content"], "")

    def test_empty_result_rejected(self):
        self.service.store.save_setup(self.book_id, content={"idea": "创意"}, kind="manual")
        pipeline, task_id = self._running_chapter_task()
        self.app.studio.jobs.finish(task_id, "   ")
        self.service.pipeline.tick()
        record = self.service.store.get_active_pipeline(self.book_id)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(self.service.store.get_chapter(self.book_id, 1)["content"], "")

    def test_duplicate_write_back_is_idempotent(self):
        self.service.store.save_setup(self.book_id, content={"idea": "创意"}, kind="manual")
        pipeline, task_id = self._running_chapter_task()
        self.app.studio.jobs.finish(task_id, "正文。")
        self.service.pipeline.tick()
        self.service.pipeline.tick()
        self.service.pipeline.tick()
        self.assertEqual(len(self.service.store.get_chapter(self.book_id, 1)["versions"]), 1)

    def test_client_forged_text_cannot_become_ai_version(self):
        """客户端没有「提交正文冒充任务结果」的入口：写回只走服务端流水线。

        人工保存的正文永远 kind=manual，不占用 generated 语义；
        伪造 task_id 的人工保存也不会骗到 AI 版本标记。
        """
        self.service.store.save_setup(self.book_id, content={"idea": "创意"}, kind="manual")
        payload, _status = self.service.api_save_chapter(
            self.book_id, 1, {"content": "伪造正文", "task_id": "st-fake"})
        self.assertEqual(payload["version"]["kind"], "manual")
        versions = self.service.store.get_chapter(self.book_id, 1)["versions"]
        self.assertTrue(all(row["kind"] == "manual" for row in versions))

    # ---- 导入与导出 ----

    def test_import_endpoints(self):
        archive = {"novel": {"prompt": "创意", "meta": {"title": "导入书"},
                             "outlineMeta": [{"no": 1, "title": "第一章", "beat": "梗概"}]},
                   "chapters": [{"title": "第一章", "content": "梗概"}]}
        payload, status = self.service.api_import(
            filename="作品存档.json", data=json.dumps(archive, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(status, 200)
        import_id = payload["import_id"]
        payload, status = self.service.api_commit_import({"import_id": import_id})
        self.assertEqual(status, 201)
        payload, status = self.service.api_commit_import({"import_id": import_id})
        self.assertEqual(status, 404)
        payload, status = self.service.api_import(filename="x.json", data=b"{bad")
        self.assertEqual(status, 400)

    def test_export_validation(self):
        self.service.store.save_chapter_version(self.book_id, 1, content="正文", kind="manual")
        payload, status = self.service.api_export_book(self.book_id, fmt="pdf")
        self.assertEqual(status, 400)
        payload, status = self.service.api_export_book(self.book_id, fmt="md", with_extras="true")
        self.assertEqual(status, 200)
        self.assertIn("第1章", payload["text"])
        self.assertEqual(payload["missing_chapters"], [2, 3])


if __name__ == "__main__":
    unittest.main()
