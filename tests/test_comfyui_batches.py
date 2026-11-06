"""造梦机批量生成（batches.py）契约：mock-only，不联网。

覆盖（docs/plans/2026-11-05-造梦机批量生成-开工交接.md §七 1）：
- 条目 → 任务载荷合成正确（方案固定 + 条目值按映射合并、未映射不写入）；
- 校验失败 / 探活不可达整单拒绝，不留半批；
- 创建中断回滚：已建任务删除、批次记录不落盘；
- request_key 形状（<批次id>-<条目序号>）与逐次卸载冻结；
- 批次详情逐条目 join 任务状态。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from naiba.comfyui import client
from naiba.comfyui.batches import ComfyUIBatchError, ComfyUIBatchManager
from naiba.comfyui.jobs import ComfyUIJobManager
from naiba.comfyui.runner import ComfyUIRunner
from naiba.comfyui.store import ComfyUIStore
from naiba.storage.media import is_uploads_path


class ComfyUIBatchesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = SimpleNamespace(
            paths=SimpleNamespace(data_dir=self.root, public_dir=self.root),
            config=SimpleNamespace(data={"comfyui_url": "http://127.0.0.1:8188"}),
        )
        self.store = ComfyUIStore(self.root)
        self.runner = ComfyUIRunner(self.app, self.store)
        self.jobs = ComfyUIJobManager(self.app, self.store, self.runner)
        self.batches = ComfyUIBatchManager(self.app, self.store, self.jobs)
        self.params = [
            {"node_id": "5", "field_name": "text", "field_type": "text",
             "control": "multiline", "role": "positive_prompt", "label": "正文"},
            {"node_id": "6", "field_name": "length", "field_type": "number",
             "default": 5, "label": "时长"},
            {"node_id": "7", "field_name": "res", "field_type": "text",
             "candidates": ["848x480", "480x848"], "label": "分辨率"},
            {"node_id": "8", "field_name": "image", "field_type": "image",
             "control": "media", "media": "image", "label": "参考图"},
            {"node_id": "9", "field_name": "lora", "field_type": "text", "label": "LoRA"},
        ]
        self.prompt = {"5": {"class_type": "Fake", "inputs": {"text": "hello", "seed": -1}},
                       "6": {"class_type": "Fake", "inputs": {"length": 5}},
                       "7": {"class_type": "Fake", "inputs": {"res": "848x480"}},
                       "8": {"class_type": "LoadImage", "inputs": {"image": "default.png"}},
                       "9": {"class_type": "Fake", "inputs": {"lora": "base.safetensors"}}}
        self.store.save_workflow({"id": "cw-batch", "name": "批量测试流", "prompt": self.prompt,
                                  "params": self.params})
        # Any accidental network access makes these focused tests fail immediately.
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(client._DIRECT_OPENER, "open",
                          side_effect=AssertionError("network forbidden")).start()
        self.reachable = mock.patch.object(client, "reachable", return_value={}).start()
        self.enqueue = mock.patch.object(self.runner, "submit", return_value=None).start()
        self.mapping = {"prompt_slot": {"node_id": "5", "field_name": "text"},
                        "duration_slot": {"node_id": "6", "field_name": "length"},
                        "resolution_slot": {"node_id": "7", "field_name": "res"},
                        "media_slots": [{"node_id": "8", "field_name": "image", "media": "image"}]}
        self.scheme = {"workflow_id": "cw-batch", "overrides": [], "mapping": self.mapping,
                       "items": [self._item("条目一", prompt="场景A", duration_seconds=8,
                                            resolution="848x480")]}

    @staticmethod
    def _item(name, **fields):
        return {"name": name, "prompt": "", "duration_seconds": None, "resolution": "",
                "shots": 1, "media_inputs": [], **fields}

    def body(self, **overrides):
        return {"title": "验收批次", "unload_after": False,
                "schemes": [dict(self.scheme)], **overrides}

    def test_items_merge_overrides_and_unmapped_values_are_dropped(self):
        batch = self.batches.create(self.body(schemes=[dict(self.scheme, items=[
            self._item("条目一", prompt="场景A", duration_seconds=8, resolution="848x480"),
            self._item("条目二", prompt="场景B"),
        ])]))
        self.assertEqual(len(batch["items"]), 2)
        first = self.store.get_task(batch["items"][0]["task_id"])
        second = self.store.get_task(batch["items"][1]["task_id"])
        first_values = {(o["node_id"], o["field_name"]): o["value"] for o in first["input"]["overrides"]}
        self.assertEqual(first_values[("5", "text")], "场景A")
        self.assertEqual(first_values[("6", "length")], 8)
        self.assertEqual(first_values[("7", "res")], "848x480")
        second_values = {(o["node_id"], o["field_name"]): o["value"] for o in second["input"]["overrides"]}
        self.assertEqual(second_values[("5", "text")], "场景B")
        self.assertNotIn(("6", "length"), second_values)
        self.assertNotIn(("7", "res"), second_values)
        self.assertEqual(first["input"]["unload_after"], False)
        # 任务卡显示「批次名 · 条目名」；入队发生在全部创建成功之后。
        self.assertEqual(first["input"]["label"], "验收批次 · 条目一")
        self.assertEqual(self.enqueue.call_count, 2)

    def test_request_keys_are_batch_scoped_and_unique(self):
        batch = self.batches.create(self.body(schemes=[dict(self.scheme, items=[
            self._item("条目一"), self._item("条目二")])]))
        keys = [item["request_key"] for item in batch["items"]]
        self.assertEqual(keys[0], f"{batch['id']}-1")
        self.assertEqual(keys[1], f"{batch['id']}-2")
        self.assertEqual(len(set(keys)), 2)

    def test_unmapped_item_value_rejects_whole_batch(self):
        body = {"title": "验收批次", "unload_after": False, "schemes": [{
            "workflow_id": "cw-batch", "overrides": [],
            "mapping": {"prompt_slot": None, "duration_slot": None,
                        "resolution_slot": None, "media_slots": []},
            "items": [self._item("条目一", prompt="场景A")],
        }]}
        with self.assertRaises(ComfyUIBatchError):
            self.batches.create(body)
        self.assertEqual(self.store.list_tasks(limit=100), [])
        self.enqueue.assert_not_called()

    def test_unreachable_comfyui_rejects_before_any_creation(self):
        self.reachable.return_value = None
        with self.assertRaises(ComfyUIBatchError):
            self.batches.create(self.body())
        self.assertEqual(self.store.list_tasks(limit=100), [])
        self.assertEqual(list(self.batches._dir.glob("cfb-*.json")), [])

    def test_validation_failure_during_creation_rolls_back_tasks(self):
        # 干跑全过、创建中途失败（存储错误）：已建任务删除、批次记录不落盘。
        batch_holder: dict = {}
        real_submit = self.jobs.submit
        def flaky(payload, **kwargs):
            task = real_submit(payload, **kwargs)
            if len(batch_holder) == 0:
                batch_holder["first"] = task
                raise OSError("disk full")
            return task
        with mock.patch.object(self.jobs, "submit", side_effect=flaky):
            with self.assertRaises(ComfyUIBatchError) as ctx:
                self.batches.create(self.body(schemes=[dict(self.scheme, items=[
                    self._item("条目一"), self._item("条目二")])]))
        self.assertIn("整批已撤销", str(ctx.exception))
        self.assertIsNone(self.store.get_task(batch_holder["first"]["id"]))
        self.assertEqual(list(self.batches._dir.glob("cfb-*.json")), [])
        self.enqueue.assert_not_called()

    def test_record_write_failure_rolls_back_created_tasks(self):
        # 任务都建好了但批次记录落盘失败：不能留下「有任务、没批次」的半批。
        with mock.patch("naiba.comfyui.batches._write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.batches.create(self.body())
        self.assertEqual(self.store.list_tasks(limit=100), [])
        self.assertEqual(list(self.batches._dir.glob("cfb-*")), [])
        self.enqueue.assert_not_called()

    def test_snapshot_write_failure_also_removes_main_record(self):
        # 主记录写成功、快照写失败：两条都要清掉，不能留下没有快照的半批。
        from naiba.comfyui import batches as batches_module
        real_write = batches_module._write_json
        calls = {"count": 0}

        def flaky(path, payload):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("disk full")
            real_write(path, payload)

        with mock.patch("naiba.comfyui.batches._write_json", side_effect=flaky):
            with self.assertRaises(OSError):
                self.batches.create(self.body())
        self.assertEqual(self.store.list_tasks(limit=100), [])
        self.assertEqual(list(self.batches._dir.glob("cfb-*")), [])
        self.enqueue.assert_not_called()

    def test_invalid_media_input_rejected_by_dry_run(self):
        body = self.body(schemes=[dict(self.scheme, items=[self._item(
            "条目一", media_inputs=[{"node_id": "8", "field_name": "image",
                                     "path": str(self.root / "uploads/missing.png"),
                                     "media": "image"}])])])
        with self.assertRaises(ComfyUIBatchError):
            self.batches.create(body)
        self.assertEqual(self.store.list_tasks(limit=100), [])

    def test_media_inputs_pass_through_when_managed_file_exists(self):
        uploads = self.root / "uploads" / "batch"
        uploads.mkdir(parents=True)
        (uploads / "ref.png").write_bytes(b"89504e470d0a1a0a0000000d49484452")
        self.assertTrue(is_uploads_path(self.root, uploads / "ref.png"))
        batch = self.batches.create(self.body(schemes=[dict(self.scheme, items=[self._item(
            "条目一", media_inputs=[{"node_id": "8", "field_name": "image",
                                     "path": str(uploads / "ref.png"), "media": "image"}])])]))
        task = self.store.get_task(batch["items"][0]["task_id"])
        self.assertEqual(task["input"]["media_inputs"][0]["node_id"], "8")
        self.assertEqual(task["input"]["media_inputs"][0]["name"], "ref.png")

    def test_unload_after_is_frozen_into_every_task(self):
        batch = self.batches.create(self.body(unload_after=True))
        for item in batch["items"]:
            task = self.store.get_task(item["task_id"])
            self.assertTrue(task["input"]["unload_after"])

    def test_batch_id_is_frozen_into_tasks_and_joined_back(self):
        # 结果页按来源分档：批量条目带 batch_id，单任务没有——字段随 input 冻结，
        # 精简行原样带回，前端不过问 request_key 形状。
        batch = self.batches.create(self.body())
        task = self.store.get_task(batch["items"][0]["task_id"])
        self.assertEqual(task["input"]["batch_id"], batch["id"])
        self.assertEqual(self.jobs.task(task["id"])["batch_id"], batch["id"])
        single = self.jobs.submit({"workflow_id": "cw-batch", "overrides": [],
                                   "media_inputs": [], "shots": 1})
        self.assertEqual(self.jobs.task(single["id"])["batch_id"], "")

    def test_detail_joins_task_state_and_counts(self):
        batch = self.batches.create(self.body())
        task_id = batch["items"][0]["task_id"]
        detail = self.batches.get(batch["id"])
        row = detail["items"][0]
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["done"], 0)
        self.assertEqual(row["total"], 1)
        row_list = self.batches.list()[0]["items"][0]
        self.assertEqual(row_list["status"], "queued")
        self.assertNotIn("outputs", row_list)
        # 任务完成后 join 出产物计数；记录缺失时如实标注 missing。
        self.store.update_task(task_id, status="completed")
        self.assertEqual(self.batches.get(batch["id"])["items"][0]["status"], "completed")
        self.store.delete_task(task_id)
        self.assertEqual(self.batches.get(batch["id"])["items"][0]["status"], "missing")

    def test_get_rejects_bad_ids_and_lists_are_time_desc(self):
        self.assertIsNone(self.batches.get(""))
        self.assertIsNone(self.batches.get("cfb/../../etc"))
        self.assertIsNone(self.batches.get("cf-other"))
        first = self.batches.create(self.body())
        second = self.batches.create(self.body())
        ids = [row["id"] for row in self.batches.list()]
        self.assertEqual(ids, [second["id"], first["id"]])

    def test_editable_snapshot_is_stored_and_only_returned_on_request(self):
        # 「编辑这一批」不靠任务状态反推：提交时把映射 / 固定参数 / 条目字段
        # 原样存进批次记录；跑批轮询和列表都不带，避免每 2 秒重复传正文。
        mapping = {**self.mapping, "media_ports": [
            {"node_id": "8", "field_name": "image", "checked": True}]}
        batch = self.batches.create(self.body(schemes=[dict(
            self.scheme, mapping=mapping, overrides=[
                {"node_id": "9", "field_name": "lora", "value": "other.safetensors"}],
            items=[dict(self.scheme["items"][0], script="剧本A")])]))
        full = self.batches.get(batch["id"], with_schemes=True)
        snapshot = full["schemes"][0]
        self.assertEqual(snapshot["workflow_id"], "cw-batch")
        self.assertEqual(snapshot["workflow_name"], "批量测试流")
        self.assertEqual(snapshot["overrides"],
                         [{"node_id": "9", "field_name": "lora", "value": "other.safetensors"}])
        self.assertEqual(snapshot["mapping"]["prompt_slot"], {"node_id": "5", "field_name": "text"})
        self.assertEqual(snapshot["mapping"]["media_ports"],
                         [{"node_id": "8", "field_name": "image", "checked": True}])
        item = snapshot["items"][0]
        self.assertEqual(item["name"], "条目一")
        self.assertEqual(item["script"], "剧本A")
        self.assertEqual(item["prompt"], "场景A")
        self.assertEqual(item["duration_seconds"], 8)
        self.assertEqual(item["resolution"], "848x480")
        self.assertEqual(item["shots"], 1)
        # 默认详情与列表都不带快照，主记录不塞正文；快照在独立文件里。
        self.assertNotIn("schemes", self.batches.get(batch["id"]))
        self.assertNotIn("schemes", self.batches.list()[0])
        self.assertEqual([row["id"] for row in self.batches.list()], [batch["id"]])
        self.assertTrue(self.batches.schemes_path(batch["id"]).exists())
        self.assertNotIn('"schemes"',
                         self.batches.batch_path(batch["id"]).read_text(encoding="utf-8"))

    def test_legacy_batch_exposes_task_input_for_restore(self):
        # 升级前落盘的批次没有 schemes 文件；详情带 with_schemes=1 时用任务输入兜底，
        # 让「编辑这一批」至少能把能对上的字段捞回来。
        batch = self.batches.create(self.body())
        self.batches.schemes_path(batch["id"]).unlink(missing_ok=True)
        detail = self.batches.get(batch["id"], with_schemes=True)
        self.assertEqual(detail["schemes"], [])
        restore = detail["items"][0]["restore"]
        self.assertEqual(restore["workflow_id"], "cw-batch")
        self.assertEqual(restore["shots"], 1)
        values = {(row["node_id"], row["field_name"]): row["value"]
                  for row in restore["overrides"]}
        self.assertEqual(values[("5", "text")], "场景A")
        self.assertNotIn("restore", self.batches.get(batch["id"])["items"][0])

    def test_clear_outputs_deletes_tasks_but_keeps_batch(self):
        batch = self.batches.create(self.body())
        task_ids = [item["task_id"] for item in batch["items"]]
        for task_id in task_ids:
            self.store.update_task(task_id, status="completed")
        result = self.batches.clear_outputs(batch["id"])
        self.assertEqual(result["deleted"], len(task_ids))
        self.assertIsNone(self.store.get_task(task_ids[0]))
        detail = self.batches.get(batch["id"])
        self.assertIsNotNone(detail)
        self.assertEqual(detail["items"][0]["status"], "missing")

    def test_delete_batch_removes_record_and_tasks(self):
        batch = self.batches.create(self.body())
        task_id = batch["items"][0]["task_id"]
        self.store.update_task(task_id, status="completed")
        result = self.batches.delete(batch["id"])
        self.assertEqual(result["deleted"], 1)
        self.assertIsNone(self.store.get_task(task_id))
        self.assertIsNone(self.batches.get(batch["id"]))
        self.assertFalse(self.batches.batch_path(batch["id"]).exists())
        self.assertFalse(self.batches.schemes_path(batch["id"]).exists())


if __name__ == "__main__":
    unittest.main()
