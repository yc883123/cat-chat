"""TXT integrity, immutable acknowledgements and crash/recovery boundaries."""
from __future__ import annotations

import json
import hashlib
import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor

from naiba.core.contracts import (STUDIO_LOGICAL_GENERATION_ID_KEY, STUDIO_RESUMED_FROM_KEY,
                                  STUDIO_RESULT_ARTIFACT_KEY)
from naiba.core.exceptions import TaskCancelled
from naiba.studio.artifacts import ResultArtifactError
from naiba.studio.jobs import StudioJobManager
from naiba.studio.store import StudioStore
from naiba.studio.service import StudioService
from naiba.studio.model_identity import configured_identity


class StudioArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = StudioStore(self.root)
        self.files = self.store.artifacts
        runner = SimpleNamespace(_registry_lock=threading.Lock(), _cancel_events={}, submit=lambda _id: None)
        self.profile = {"kind": "online", "provider_id": "fixture", "base_url": "https://fixture.invalid/v1",
                        "request_format": "openai_chat", "api_key": "PRIVATE_TEST_KEY"}
        self.app = SimpleNamespace(config=SimpleNamespace(profile=lambda _key: dict(self.profile)))
        self.jobs = StudioJobManager(self.app, self.store, runner)

    def task(self, **extra):
        return self.jobs.submit({"module": "novel", "model_key": "online:test", "model_name": "test",
                                 "content": "冻结输入", **extra})

    def complete(self, task, text, block=1):
        receipt = self.files.write_segment(task, block, 1, text)
        self.files.complete_block(task, block, receipt, finish_reason="tool_calls")
        return receipt

    def test_exact_utf8_newlines_and_idempotent_receipts(self):
        task = self.task()
        text = '  中文😀\r\n\r\n{"键":"值"}\n尾部  '
        first = self.files.write_segment(task, 1, 1, text)
        self.assertEqual(first, self.files.write_segment(task, 1, 1, text))
        self.files.complete_block(task, 1, first, finish_reason="tool_calls")
        self.assertEqual(self.files.read(task)[0], text)
        self.assertEqual(first["chars"], len(text))
        self.assertEqual(self.files._path(task["id"], "result.txt").read_bytes(), text.encode("utf-8"))
        self.assertEqual(first, self.files.write_segment(task, 1, 1, text))

    def test_conflicts_holes_bad_receipt_and_length_are_rejected(self):
        task = self.task()
        with self.assertRaises(ResultArtifactError):
            self.files.write_segment(task, 1, 2, "跳段")
        receipt = self.files.write_segment(task, 1, 1, "正文")
        with self.assertRaises(ResultArtifactError):
            self.files.write_segment(task, 1, 1, "改正文")
        with self.assertRaises(ResultArtifactError):
            self.files.complete_block(task, 1, {**receipt, "chars": 999})
        with self.assertRaises(ResultArtifactError):
            self.files.complete_block(task, 1, receipt, finish_reason="length")
        self.assertEqual(self.files.read(task, require_final=False)[1]["status"], "partial")
        with self.assertRaises(ResultArtifactError):
            self.files.read(task)

    def test_cancelled_write_does_not_acknowledge(self):
        task = self.task()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(TaskCancelled):
            self.files.write_segment(task, 1, 1, "不能写", cancel=cancel)
        self.assertEqual(self.files.read(task, require_final=False)[0], "")

    def test_manifest_and_segment_corruption_never_fall_back(self):
        task = self.task()
        self.complete(task, "有效正文")
        self.store.update_task(task["id"], result="不可信旧投影", status="done")
        self.files._path(task["id"], "block-0001-segment-000001.txt").write_bytes(b"broken")
        with self.assertRaises(ResultArtifactError):
            self.jobs.result_text(task["id"])
        view = self.jobs.task(task["id"])
        self.assertEqual(view["result"], "")
        self.assertEqual(view[STUDIO_RESULT_ARTIFACT_KEY]["status"], "invalid")
        self.files._path(task["id"]).unlink()
        with self.assertRaises(ResultArtifactError):
            self.jobs.result_text(task["id"])

    def test_multiple_blocks_preserve_separator_and_all_whitespace(self):
        task = self.task(chunked={"blocks": ["一", "二"]})
        self.complete(task, " 第一块 \r\n", 1)
        self.assertEqual(self.files.read(task, require_final=False)[0], " 第一块 \r\n")
        self.complete(task, "\n 第二块 ", 2)
        self.assertEqual(self.files.read(task)[0], " 第一块 \r\n\n\n\n 第二块 ")

    def test_orphan_segment_does_not_count_until_manifest_ack(self):
        task = self.task()
        with patch.object(self.files, "_save", side_effect=OSError("crash-before-manifest")):
            with self.assertRaises(OSError):
                self.files.write_segment(task, 1, 1, "已写文件未确认")
        self.assertEqual(self.files.read(task, require_final=False)[0], "")
        receipt = self.files.write_segment(task, 1, 1, "已写文件未确认")
        self.assertEqual(receipt["segments"], 1)

    def test_published_snapshot_without_manifest_can_retry_confirmation(self):
        task = self.task()
        receipt = self.files.write_segment(task, 1, 1, "正文")
        with patch.object(self.files, "_save", side_effect=OSError("crash-after-snapshot")):
            with self.assertRaises(OSError):
                self.files.complete_block(task, 1, receipt)
        self.assertEqual(self.files.read(task, require_final=False)[1]["status"], "partial")
        self.files.complete_block(task, 1, receipt)
        self.assertEqual(self.files.read(task)[0], "正文")

    def test_resume_clones_confirmed_bytes_and_stable_generation(self):
        task = self.task()
        receipt = self.files.write_segment(task, 1, 1, "保留部分")
        self.store.update_task(task["id"], status="interrupted")
        child = self.jobs.prepare_resume(task["id"], request_key="resume-once")
        again = self.jobs.prepare_resume(task["id"], request_key="resume-once")
        self.assertEqual(child["id"], again["id"])
        self.assertNotEqual(child["id"], task["id"])
        self.assertEqual(child[STUDIO_LOGICAL_GENERATION_ID_KEY], task["id"])
        self.assertEqual(child[STUDIO_RESUMED_FROM_KEY], task["id"])
        self.assertEqual(self.files.read(child, require_final=False)[0], "保留部分")
        next_receipt = self.files.write_segment(child, 1, 2, "后半")
        self.files.complete_block(child, 1, next_receipt)
        self.assertEqual(self.files.read(child)[0], "保留部分后半")
        self.assertEqual(self.files.read(task, require_final=False)[0], "保留部分")
        self.assertEqual(receipt["segments"], 1)
        self.assertEqual(self.jobs.delete([task["id"]])["skipped"][0]["reason"], "referenced")

    def test_resume_after_prepared_child_interrupted_is_new_queued_execution(self):
        task = self.task()
        self.files.write_segment(task, 1, 1, "第一段")
        self.store.update_task(task["id"], status="interrupted")
        child = self.jobs.prepare_resume(task["id"], request_key="same")
        self.files.write_segment(child, 1, 2, "第二段")
        self.store.update_task(child["id"], status="interrupted")
        grandchild = self.jobs.prepare_resume(task["id"], request_key="same")
        self.assertNotEqual(grandchild["id"], child["id"])
        self.assertEqual(grandchild["status"], "queued")
        self.assertEqual(grandchild[STUDIO_LOGICAL_GENERATION_ID_KEY], task["id"])
        self.assertEqual(self.files.read(grandchild, require_final=False)[0], "第一段第二段")

    def test_writer_resume_requires_pipeline_and_active_source_is_rejected(self):
        task = self.task(module="writer")
        with self.assertRaises(ValueError):
            self.jobs.prepare_resume(task["id"], request_key="x", allow_writer=True)
        self.store.update_task(task["id"], status="interrupted")
        with self.assertRaisesRegex(ValueError, "流水线"):
            self.jobs.prepare_resume(task["id"], request_key="x")
        child = self.jobs.prepare_resume(task["id"], request_key="x", allow_writer=True)
        self.assertEqual(child["status"], "queued")

    def test_old_task_result_is_compatible_and_new_marker_is_fail_closed(self):
        task = self.store.create_task({"module": "vi", "content": "x"})
        self.store.update_task(task["id"], result="旧结果", status="done")
        self.assertEqual(self.jobs.result_text(task["id"]), "旧结果")
        task["input"]["output_contract_version"] = 1
        self.store._write_json(self.store._record_path(task["id"]), task)
        with self.assertRaises(ResultArtifactError):
            self.jobs.result_text(task["id"])

    def test_resume_concurrent_requests_and_pointer_write_crash_are_idempotent(self):
        task = self.task()
        self.files.write_segment(task, 1, 1, "已确认")
        self.store.update_task(task["id"], status="interrupted")
        original = self.store.update_task
        def crash_at_pointer(task_id, **fields):
            if task_id == task["id"] and "result_resumed_into" in fields:
                raise OSError("crash-before-source-pointer")
            return original(task_id, **fields)
        with patch.object(self.store, "update_task", side_effect=crash_at_pointer):
            with self.assertRaises(OSError):
                self.jobs.prepare_resume(task["id"], request_key="once")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _n: self.jobs.prepare_resume(task["id"], request_key="once"), range(2)))
        self.assertEqual(results[0]["id"], results[1]["id"])
        self.assertEqual(len(self.store.list_tasks(limit=100)), 2)
        self.assertEqual(self.files.read(results[0], require_final=False)[0], "已确认")

    def test_path_traversal_and_cross_task_manifest_are_rejected(self):
        task = self.task()
        with self.assertRaises(ResultArtifactError):
            self.files._path("../escape")
        other = self.task(content="另一个冻结输入")
        self.files._path(other["id"]).write_bytes(self.files._path(task["id"]).read_bytes())
        with self.assertRaises(ResultArtifactError):
            self.files.read(other, require_final=False)

    def test_malformed_manifest_structures_are_invalid_domain_errors(self):
        mutators = [
            lambda row: row.update(blocks=["bad-block"]),
            lambda row: row.update(separator=[]),
            lambda row: row["blocks"][0].update(segments=[{"segment": 1}]),
            lambda row: row["blocks"][0]["segments"][0].update(receipt="bad-receipt"),
            lambda row: row["blocks"][0]["segments"][0]["receipt"].pop("receipt_id"),
            lambda row: row["blocks"][0]["segments"][0]["receipt"].update(chars=999),
            lambda row: row.update(attempts="bad-attempts"),
        ]
        for mutate in mutators:
            with self.subTest(mutate=mutate):
                task = self.task()
                self.complete(task, "有效正文")
                manifest = self.files.manifest(task)
                mutate(manifest)
                self.files._path(task["id"]).write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(ResultArtifactError):
                    self.jobs.result_text(task["id"])
                result = self.jobs.task(task["id"])
                self.assertEqual(result["result_artifact"]["status"], "invalid")
                self.assertEqual(result["result"], "")

    def test_new_task_identity_is_secret_free_and_changed_config_blocks_resume(self):
        original = dict(self.profile)
        for field, value in (("base_url", "https://another.invalid/v1"),
                             ("request_format", "anthropic_messages"),
                             ("provider_id", "another-provider"), ("kind", "local")):
            with self.subTest(field=field):
                self.profile = dict(original)
                task = self.task(module="writer")
                identity = task["input"]["model_snapshot"]
                self.assertNotIn("PRIVATE_TEST_KEY", json.dumps(identity))
                self.assertNotIn("api_key", identity)
                self.assertEqual(identity["request_format"], "openai_chat")
                self.files.write_segment(task, 1, 1, "已确认")
                self.store.update_task(task["id"], status="interrupted")
                before = len(self.store.list_tasks(limit=1000))
                self.profile[field] = value
                with self.assertRaisesRegex(ValueError, "模型配置已改变"):
                    self.jobs.prepare_resume(task["id"], request_key="identity-change", allow_writer=True)
                self.assertEqual(len(self.store.list_tasks(limit=1000)), before)
        self.profile = original

    def test_legacy_missing_model_identity_is_not_silently_rebound(self):
        task = self.store.create_task({"module": "novel", "content": "旧任务", "model_key": "online:test", "model_name": "test"})
        self.store.update_task(task["id"], status="interrupted")
        with self.assertRaisesRegex(ValueError, "未冻结完整模型身份"):
            self.jobs.prepare_resume(task["id"], request_key="legacy")
        self.assertNotIn("model_snapshot", self.store.get_task(task["id"])["input"])

    def http_server(self):
        from naiba.http import RequestHandler
        class Handler(RequestHandler):
            def _is_local_request(self):
                return False

            def log_message(self, *_args):
                return

        service = StudioService.__new__(StudioService)
        service.jobs = self.jobs
        service._store = self.store
        app = SimpleNamespace(studio=service, config=SimpleNamespace(data={"access_token": "fixture-token"}))
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.app = app
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.addCleanup(close)
        return server

    def request(self, server, path, token="fixture-token", method="GET", body=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_http_large_download_is_complete_utf8_and_requires_authentication(self):
        task = self.task()
        first = "中文😀\r\n" * 30000
        second = "第二段\n" * 20000
        self.files.write_segment(task, 1, 1, first)
        receipt = self.files.write_segment(task, 1, 2, second)
        self.files.complete_block(task, 1, receipt)
        server = self.http_server()
        path = f"/api/studio/tasks/{task['id']}/artifact?download=1"
        self.assertEqual(self.request(server, path, token="")[0], 401)
        self.assertEqual(self.request(server, path, token="wrong")[0], 401)
        status, headers, body = self.request(server, path)
        self.assertEqual(status, 200)
        expected = (first + second).encode("utf-8")
        self.assertGreater(len(expected), 512 * 1024)
        self.assertEqual(len(body), int(headers["Content-Length"]))
        self.assertEqual(hashlib.sha256(body).hexdigest(), hashlib.sha256(expected).hexdigest())
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertNotIn("partial", headers["Content-Disposition"])
        self.assertEqual(body, expected)

    def test_http_partial_download_is_labeled_and_writer_resume_cannot_bypass_owner(self):
        task = self.task(module="writer")
        self.files.write_segment(task, 1, 1, "已确认未完成")
        self.store.update_task(task["id"], status="interrupted")
        server = self.http_server()
        prefix = f"/api/studio/tasks/{task['id']}"
        status, headers, body = self.request(server, prefix + "/artifact?download=1")
        self.assertEqual(status, 200)
        self.assertIn("-partial.txt", headers["Content-Disposition"])
        self.assertEqual(body.decode("utf-8"), "已确认未完成")
        status, _headers, body = self.request(server, prefix + "/resume-result", method="POST",
                                             body={"request_key": "x", "allow_writer": True})
        self.assertEqual(status, 409)
        self.assertIn("流水线", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
