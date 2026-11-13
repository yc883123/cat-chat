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
            {"node_id": "10", "field_name": "steps", "field_type": "number",
             "default": 24, "label": "步数"},
            {"node_id": "11", "field_name": "flag", "field_type": "bool",
             "default": True, "label": "开关"},
        ]
        self.prompt = {"5": {"class_type": "Fake", "inputs": {"text": "hello", "seed": -1}},
                       "6": {"class_type": "Fake", "inputs": {"length": 5}},
                       "7": {"class_type": "Fake", "inputs": {"res": "848x480"}},
                       "8": {"class_type": "LoadImage", "inputs": {"image": "default.png"}},
                       "9": {"class_type": "Fake", "inputs": {"lora": "base.safetensors"}},
                       "10": {"class_type": "Fake", "inputs": {"steps": 24}},
                       "11": {"class_type": "Fake", "inputs": {"flag": True}}}
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

    def test_convert_settings_survive_restore_without_entering_graph(self):
        convert = {"template": "catalog:h3-007-h3-i2va", "provider": "online:test",
                   "model": "fake", "system_prompt": "本批统一规则"}
        batch = self.batches.create(self.body(convert=convert))
        convert["system_prompt"] = "已修改"
        restored = self.batches.get(batch["id"], with_schemes=True)
        self.assertEqual(restored["convert"]["system_prompt"], "本批统一规则")
        task = self.store.get_task(batch["items"][0]["task_id"])
        self.assertNotIn("convert", task["input"])
        self.assertNotIn("system_prompt", task["input"])
        with self.assertRaises(ComfyUIBatchError):
            self.batches.create(self.body(convert={"system_prompt": "x" * 100001}))

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

    # ---- ＋增加指认（extra_points / item.extras）----

    def test_extra_designations_apply_per_item(self):
        scheme = dict(self.scheme, mapping=dict(self.mapping, extra_points=[
            {"id": "bx1", "kind": "prompt", "slot": {"node_id": "9", "field_name": "lora"}},
            {"id": "bx2", "kind": "duration", "slot": {"node_id": "10", "field_name": "steps"}},
            {"id": "bx3", "kind": "spare", "slot": {"node_id": "11", "field_name": "flag"}},
        ]), items=[
            self._item("条目一", prompt="场景A", duration_seconds=8,
                       extras={"9|lora": "cute.safetensors", "10|steps": "30", "11|flag": "true"}),
            self._item("条目二", prompt="场景B"),
        ])
        batch = self.batches.create(self.body(schemes=[scheme]))
        first = self.store.get_task(batch["items"][0]["task_id"])
        first_values = {(o["node_id"], o["field_name"]): o["value"] for o in first["input"]["overrides"]}
        self.assertEqual(first_values[("9", "lora")], "cute.safetensors")
        self.assertEqual(first_values[("10", "steps")], 30)
        self.assertEqual(first_values[("11", "flag")], True)
        second = self.store.get_task(batch["items"][1]["task_id"])
        second_values = {(o["node_id"], o["field_name"]) for o in second["input"]["overrides"]}
        self.assertNotIn(("9", "lora"), second_values)
        self.assertNotIn(("10", "steps"), second_values)
        self.assertNotIn(("11", "flag"), second_values)
        # 快照把指认行与逐条补充值都记下来，「编辑这一批」原样回填。
        detail = self.batches.get(batch["id"], with_schemes=True)
        self.assertEqual(detail["schemes"][0]["mapping"]["extra_points"][0]["kind"], "prompt")
        self.assertEqual(detail["schemes"][0]["mapping"]["extra_points"][2]["spec"], "bool")
        self.assertEqual(detail["schemes"][0]["items"][0]["extras"],
                         {"9|lora": "cute.safetensors", "10|steps": 30, "11|flag": True})
        self.assertEqual(detail["schemes"][0]["items"][1]["extras"], {})

    def test_extras_for_undesignated_slot_rejects_whole_batch(self):
        scheme = dict(self.scheme, items=[self._item("条目一", extras={"13|nope": "值"})])
        with self.assertRaises(ComfyUIBatchError):
            self.batches.create(self.body(schemes=[scheme]))
        self.assertEqual(self.store.list_tasks(limit=100), [])
        self.enqueue.assert_not_called()

    def test_extra_number_must_be_numeric(self):
        scheme = dict(self.scheme, mapping=dict(self.mapping, extra_points=[
            {"id": "bx1", "kind": "duration", "slot": {"node_id": "10", "field_name": "steps"}},
        ]), items=[self._item("条目一", extras={"10|steps": "不三不四"})])
        with self.assertRaises(ComfyUIBatchError) as caught:
            self.batches.create(self.body(schemes=[scheme]))
        self.assertIn("要填数字", str(caught.exception))

    def test_duplicate_extra_designation_rejects(self):
        mapping = dict(self.mapping, extra_points=[
            {"id": "bx1", "kind": "spare", "slot": {"node_id": "9", "field_name": "lora"}},
            {"id": "bx2", "kind": "spare", "slot": {"node_id": "9", "field_name": "lora"}},
        ])
        scheme = dict(self.scheme, mapping=mapping)
        with self.assertRaises(ComfyUIBatchError) as caught:
            self.batches.create(self.body(schemes=[scheme]))
        self.assertIn("互相覆盖", str(caught.exception))

    def test_extra_kind_must_match_slot_type(self):
        mapping = dict(self.mapping, extra_points=[
            {"id": "bx1", "kind": "prompt", "slot": {"node_id": "10", "field_name": "steps"}},
        ])
        scheme = dict(self.scheme, mapping=mapping)
        with self.assertRaises(ComfyUIBatchError) as caught:
            self.batches.create(self.body(schemes=[scheme]))
        self.assertIn("只能指认文本参数", str(caught.exception))

    def test_extra_slot_matching_primary_mapping_rejects(self):
        mapping = dict(self.mapping, extra_points=[
            {"id": "bx1", "kind": "duration", "slot": {"node_id": "6", "field_name": "length"}},
        ])
        scheme = dict(self.scheme, mapping=mapping)
        with self.assertRaises(ComfyUIBatchError) as caught:
            self.batches.create(self.body(schemes=[scheme]))
        self.assertIn("互相覆盖", str(caught.exception))

    def test_extra_on_media_slot_rejects(self):
        mapping = dict(self.mapping, extra_points=[
            {"id": "bx1", "kind": "spare", "slot": {"node_id": "8", "field_name": "image"}},
        ])
        scheme = dict(self.scheme, mapping=mapping)
        with self.assertRaises(ComfyUIBatchError) as caught:
            self.batches.create(self.body(schemes=[scheme]))
        self.assertIn("素材口", str(caught.exception))

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

    def test_media_skip_passes_through_and_snapshot_restores(self):
        # 条目手动 ⊘ 的媒体地址随任务冻结；快照与老批次兜底都要能回填跳过状态。
        scheme = dict(self.scheme, items=[self._item(
            "条目一", media_skip=[{"node_id": "8", "field_name": "image"}])])
        batch = self.batches.create(self.body(schemes=[scheme]))
        task = self.store.get_task(batch["items"][0]["task_id"])
        self.assertEqual(task["input"]["media_skip"],
                         [{"node_id": "8", "field_name": "image"}])
        self.assertEqual(task["input"]["media_inputs"], [])
        detail = self.batches.get(batch["id"], with_schemes=True)
        self.assertEqual(detail["schemes"][0]["items"][0]["media_skip"],
                         [{"node_id": "8", "field_name": "image"}])
        # 老批次没有 schemes 快照：详情兜底把 media_skip 一起交给前端回填。
        self.batches.schemes_path(batch["id"]).unlink(missing_ok=True)
        legacy = self.batches.get(batch["id"], with_schemes=True)
        self.assertEqual(legacy["items"][0]["restore"]["media_skip"],
                         [{"node_id": "8", "field_name": "image"}])

    def test_invalid_media_skip_rejects_whole_batch(self):
        scheme = dict(self.scheme, items=[self._item(
            "条目一", media_skip=[{"node_id": "5", "field_name": "text"}])])
        with self.assertRaises(ComfyUIBatchError):
            self.batches.create(self.body(schemes=[scheme]))
        self.assertEqual(self.store.list_tasks(limit=100), [])
        self.enqueue.assert_not_called()

    def test_batch_timing_uses_persisted_task_times_and_freezes(self):
        # 批次耗时只汇总任务 started_at/finished_at：排队时没有起点，完成后固定。
        batch = self.batches.create(self.body(schemes=[dict(self.scheme, items=[
            self._item("条目一"), self._item("条目二")])]))
        first, second = (item["task_id"] for item in batch["items"])
        detail = self.batches.get(batch["id"])
        self.assertIsNone(detail["started_at"])
        self.assertIsNone(detail["finished_at"])
        self.assertFalse(detail["elapsed_known"])
        self.assertTrue(detail["active"])
        self.store.update_task(first, status="running", started_at=1000)
        detail = self.batches.get(batch["id"])
        self.assertEqual(detail["started_at"], 1000)
        self.assertIsNone(detail["finished_at"])
        self.assertTrue(detail["active"])
        self.store.update_task(first, status="completed", finished_at=3000)
        self.store.update_task(second, status="running", started_at=2000)
        detail = self.batches.get(batch["id"])
        self.assertEqual(detail["started_at"], 1000)
        self.assertIsNone(detail["finished_at"], "还有条目在跑时不能提前冻结")
        self.store.update_task(second, status="completed", finished_at=5000)
        detail = self.batches.get(batch["id"])
        self.assertEqual(detail["started_at"], 1000)
        self.assertEqual(detail["finished_at"], 5000)
        self.assertFalse(detail["active"])
        self.assertTrue(detail["elapsed_known"])
        # 老记录缺时间：缺的那条不参与汇总，不伪造。
        self.store.update_task(first, started_at=None, finished_at=None)
        detail = self.batches.get(batch["id"])
        self.assertEqual(detail["started_at"], 2000)
        self.assertEqual(detail["finished_at"], 5000)
        self.assertFalse(detail["active"])
        self.store.update_task(second, started_at=None, finished_at=None)
        detail = self.batches.get(batch["id"])
        self.assertIsNone(detail["started_at"])
        self.assertIsNone(detail["finished_at"])
        self.assertFalse(detail["elapsed_known"])

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

    def _history_record(self, index):
        batch_id = f"cfb-history-{index:04d}"
        items = []
        for item_no in range(2):
            task_id = f"cf-batch-history-{index:04d}-{item_no}"
            self.store._write_json(self.store.task_record_path(task_id), {
                "id": task_id, "status": "completed", "created_at": index,
                "input": {"batch_id": batch_id, "shots": 1},
                "checkpoint": {"completed": [0], "errors": []},
            })
            items.append({"index": item_no + 1, "name": f"item {item_no}", "task_id": task_id})
        self.store._write_json(self.batches.batch_path(batch_id), {
            "id": batch_id, "title": f"history {index}", "created_at": index // 3, "items": items,
        })
        return batch_id

    def test_batch_history_pages_reach_beyond_fifty_and_join_only_current_page(self):
        ids = [self._history_record(index) for index in range(67)]
        expected = sorted(ids, key=lambda key: (int(key.rsplit("-", 1)[-1]) // 3, key), reverse=True)
        self.assertEqual(len(self.batches.list()), 50, "老调用仍返回原默认页")
        with mock.patch.object(self.store, "get_task", wraps=self.store.get_task) as reads:
            page = self.batches.page(13)
        self.assertEqual(reads.call_count, 26, "只join当前页，前瞻批次也不能join任务")
        seen = [row["id"] for row in page["rows"]]
        self.assertEqual(seen, expected[:13])
        self.batches.batch_path(seen[-1]).unlink()
        while page["has_more"]:
            page = self.batches.page(13, page["next_cursor"])
            seen.extend(row["id"] for row in page["rows"])
        self.assertEqual(seen, expected)
        self.assertEqual(len(set(seen)), 67)
        self.assertEqual(page["next_cursor"], "")

    def test_batch_index_detects_external_creation_deletion_and_does_not_cache_snapshots(self):
        ids = [self._history_record(index) for index in range(12)]
        self.batches.page(3)
        original = Path.read_text
        with mock.patch.object(Path, "read_text", autospec=True, side_effect=original) as reads:
            self.batches.page(3)
        self.assertEqual(reads.call_count, 10, "重读只取4份批次主记录和当前页6份任务，不解析全部历史")
        summaries = self.batches._history_index.summaries()
        self.assertTrue(all(set(row) == {"id", "created_at"} for row in summaries))
        self.store._write_json(self.batches.batch_path("cfb-history-external"), {
            "id": "cfb-history-external", "created_at": 900, "items": [],
        })
        other = ComfyUIBatchManager(self.app, self.store, self.jobs)
        self.assertEqual(self.batches.page(1)["rows"][0]["id"], "cfb-history-external")
        other.batch_path("cfb-history-external").unlink()
        self.assertEqual(self.batches.page(1)["rows"][0]["id"], ids[-1])
        self.assertEqual(other.page(1)["rows"][0]["id"], ids[-1])
        with mock.patch.object(self.batches._history_index, "summaries",
                               side_effect=AssertionError("定点刷新不能扫描历史目录")):
            rows, missing = self.batches.summaries_by_ids([ids[0], "cfb-gone"])
        self.assertEqual([row["id"] for row in rows], [ids[0]])
        self.assertEqual(missing, ["cfb-gone"])
        self.store.update_task(rows[0]["items"][0]["task_id"], status="running")
        rows, _ = self.batches.summaries_by_ids([ids[0]])
        self.assertEqual(rows[0]["items"][0]["status"], "running", "列表摘要必须重新联读当前任务状态")

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
