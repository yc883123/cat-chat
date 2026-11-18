# -*- coding: utf-8 -*-
"""造梦机对话域（dream_*，计划 docs/plans/2026-11-17-造梦机对话预设Agent与工具集设计.md）
契约：mock-only，不联网、不提交真实生成。

覆盖：
- 对话参数配置存储：保存/读取/重置、绑定校验、指纹绑定（改工作流即失效）；
- 尺寸换算：比例+MP、沿用当前像素、直接宽高、节点上下限夹取、偏差待确认；
- 预检快照：单次/批量展开、必答项缺失、keep_default、request_key 冻结；
- 提交幂等：同快照重放同任务、过期拒绝、指纹变化拒绝、缺失未解决拒绝；
- 批次外层幂等：同键同内容重放返回原批次、同键不同内容拒绝；
- 类型化等待目标：解析/汇总（造梦机任务/批次/普通 Job 各走各的口径）；
- 工具形状与执行归属：side_effect/retryable/policy、辅助对话与子代理拒绝。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.comfyui import client  # noqa: E402
from naiba.comfyui.batches import ComfyUIBatchError, ComfyUIBatchManager  # noqa: E402
from naiba.comfyui.dream import (  # noqa: E402
    ChatConfigStore,
    DreamConfigError,
    DreamPrepareError,
    DreamService,
    DreamSubmitError,
    DreamSnapshotStore,
    check_bindings_against_params,
    encode_wait_target,
    parse_ratio,
    resolve_size,
    validate_bindings,
)
from naiba.comfyui.jobs import ComfyUIJobManager  # noqa: E402
from naiba.comfyui.runner import ComfyUIRunner  # noqa: E402
from naiba.comfyui.store import ComfyUIStore  # noqa: E402
from naiba.run import job_waits  # noqa: E402
from naiba.tools.providers import dream as dream_provider  # noqa: E402
from naiba.tools.registry import build_dream_tool_specs  # noqa: E402

WORKFLOW_ID = "cw-dream0001"


def _bindings(workflow_id: str = WORKFLOW_ID):
    return [
        {"id": "b1", "purpose": "positive_prompt", "display_name": "主体提示词",
         "description": "", "slots": [{"node_id": "5", "field_name": "text", "name": ""}],
         "ask": "always", "per_item": False},
        {"id": "b2", "purpose": "size", "display_name": "输出尺寸",
         "description": "", "slots": [{"node_id": "6", "field_name": "width", "name": ""},
                                      {"node_id": "6", "field_name": "height", "name": ""}],
         "ask": "on_mention", "per_item": False},
        {"id": "b3", "purpose": "seed", "display_name": "种子",
         "description": "", "slots": [{"node_id": "5", "field_name": "seed", "name": ""}],
         "ask": "on_mention", "per_item": False},
    ]


class DreamHarness(unittest.TestCase):
    """公共桩：临时数据目录 + 工作流库 + 断网探活。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = SimpleNamespace(
            paths=SimpleNamespace(data_dir=self.root, public_dir=self.root),
            config=SimpleNamespace(data={"comfyui_url": "http://127.0.0.1:8188"}),
            storage=None,
        )
        self.store = ComfyUIStore(self.root)
        self.runner = ComfyUIRunner(self.app, self.store)
        self.jobs = ComfyUIJobManager(self.app, self.store, self.runner)
        self.batches = ComfyUIBatchManager(self.app, self.store, self.jobs)
        self.params = [
            {"node_id": "5", "field_name": "text", "field_type": "text",
             "control": "multiline", "role": "positive_prompt", "label": "正文"},
            {"node_id": "5", "field_name": "seed", "field_type": "seed", "label": "种子"},
            {"node_id": "6", "field_name": "width", "field_type": "number",
             "default": 832, "role": "resolution", "label": "宽"},
            {"node_id": "6", "field_name": "height", "field_type": "number",
             "default": 1216, "role": "resolution", "label": "高"},
        ]
        self.prompt = {
            "5": {"class_type": "FakePrompt", "inputs": {"text": "hello", "seed": -1}},
            "6": {"class_type": "EmptyLatentImage", "inputs": {"width": 832, "height": 1216}},
        }
        self.store.save_workflow({"id": WORKFLOW_ID, "name": "对话测试流", "prompt": self.prompt,
                                  "params": self.params})
        # 组装根形态：app.comfyui 挂服务面（DreamService / ComfyNativeAdapter 都从这里取组件）。
        self.app.comfyui = SimpleNamespace(store=self.store, jobs=self.jobs, batches=self.batches)
        self.service = DreamService(self.app)
        self.service.chat_configs.save(
            WORKFLOW_ID, self.store.payload_hash(self.prompt), _bindings(),
            workflow_name="对话测试流",
        )
        self.ownership = {
            "kind": "conversation", "conversation_id": "conv-1",
            "owner_id": "conv-1", "origin_run_id": "run-1",
        }
        # 任何意外联网立即失败；reachable 返回 None = ComfyUI 离线（limits 未核）。
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(client._DIRECT_OPENER, "open",
                          side_effect=AssertionError("network forbidden")).start()
        self.reachable = mock.patch.object(client, "reachable", return_value=None).start()
        self.enqueue = mock.patch.object(self.runner, "submit", return_value=None).start()

    def fingerprint(self) -> str:
        return self.store.payload_hash(self.prompt)


class ChatConfigStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ChatConfigStore(Path(self.tmp.name))

    def test_save_and_load_roundtrip(self):
        record = self.store.save(WORKFLOW_ID, "a" * 64, _bindings(), workflow_name="测试流")
        self.assertEqual(record["workflow_fingerprint"], "a" * 64)
        loaded = self.store.load(WORKFLOW_ID)
        self.assertEqual(len(loaded["bindings"]), 3)
        self.assertEqual(loaded["workflow_name"], "测试流")

    def test_delete(self):
        self.store.save(WORKFLOW_ID, "a" * 64, _bindings())
        self.assertTrue(self.store.delete(WORKFLOW_ID))
        self.assertIsNone(self.store.load(WORKFLOW_ID))
        self.assertFalse(self.store.delete(WORKFLOW_ID))

    def test_invalid_id_rejected(self):
        with self.assertRaises(DreamConfigError):
            self.store.save("../escape", "a" * 64, [])
        # 读取路径容错：非法 id 一律返回 None（不抛），删除返回 False。
        self.assertIsNone(self.store.load(""))
        self.assertIsNone(self.store.load("cw-不存在"))
        self.assertFalse(self.store.delete("cw-不存在"))

    def test_validate_bindings_purpose_and_slots(self):
        with self.assertRaises(DreamConfigError):
            validate_bindings([{"purpose": "nope", "display_name": "x",
                                "slots": [{"node_id": "5", "field_name": "text"}]}])
        with self.assertRaises(DreamConfigError):
            validate_bindings([{"purpose": "positive_prompt", "display_name": "x", "slots": []}])
        with self.assertRaises(DreamConfigError):
            validate_bindings([{"purpose": "size", "display_name": "x",
                                "slots": [{"node_id": "6", "field_name": "width"},
                                          {"node_id": "6", "field_name": "height"},
                                          {"node_id": "6", "field_name": "depth"}]}])
        cleaned = validate_bindings(_bindings())
        self.assertEqual([item["ask"] for item in cleaned], ["always", "on_mention", "on_mention"])

    def test_duplicate_ids_rejected(self):
        rows = _bindings() + [_bindings()[0]]
        with self.assertRaises(DreamConfigError):
            validate_bindings(rows)

    def test_check_bindings_against_params_reports_drift(self):
        problems = check_bindings_against_params(_bindings(), [
            {"node_id": "5", "field_name": "text", "field_type": "text"},
            # width 槽位消失：配置失效要说清楚
        ])
        self.assertTrue(any("6 · width" in item for item in problems))

    def test_fingerprint_mismatch_detected_by_check(self):
        problems = check_bindings_against_params(_bindings(), [])
        # b1 一槽 + b2 两槽（宽高）+ b3 一槽 = 4 条槽位级失效说明。
        self.assertEqual(len(problems), 4)


class ResolveSizeTests(unittest.TestCase):
    WIDTH = {"node_id": "6", "field_name": "width", "field_type": "number", "default": 832}
    HEIGHT = {"node_id": "6", "field_name": "height", "field_type": "number", "default": 1216}

    def test_parse_ratio(self):
        self.assertAlmostEqual(parse_ratio("9:16"), 9 / 16)
        self.assertAlmostEqual(parse_ratio("16:9"), 16 / 9)
        self.assertAlmostEqual(parse_ratio("1.5"), 1.5)
        self.assertIsNone(parse_ratio("abc"))
        self.assertIsNone(parse_ratio("0:0"))
        self.assertIsNone(parse_ratio("-1:2"))

    def test_ratio_plus_mp(self):
        result = resolve_size({"ratio": "9:16", "mp": 1.0}, self.WIDTH, self.HEIGHT)
        self.assertIsNone(result.get("error"))
        width, height = result["width"], result["height"]
        self.assertEqual(width * height, pytest_approx_mp(width, height, 1.0))
        self.assertLess(width, height, "9:16 是竖图：宽必须小于高")

    def test_ratio_only_uses_current_pixels(self):
        result = resolve_size({"ratio": "1:1"}, self.WIDTH, self.HEIGHT)
        self.assertIsNone(result.get("error"))
        self.assertTrue(any("沿用工作流当前尺寸" in note for note in result["notes"]))
        self.assertEqual(result["width"], result["height"])

    def test_explicit_width_height(self):
        result = resolve_size({"width": 1024, "height": 768}, self.WIDTH, self.HEIGHT)
        self.assertEqual((result["width"], result["height"]), (1024, 768))
        self.assertAlmostEqual(result["ratio_actual"], 1024 / 768, places=3)

    def test_missing_request_is_an_error(self):
        result = resolve_size({}, self.WIDTH, self.HEIGHT)
        self.assertIn("比例", str(result.get("error")))

    def test_limits_clamped_and_reported(self):
        limits = {"width": {"min": 256, "max": 512}, "height": {"min": 256, "max": 2048}}
        result = resolve_size({"ratio": "9:16", "mp": 1.0}, self.WIDTH, self.HEIGHT, limits)
        self.assertLessEqual(result["width"], 512)
        self.assertTrue(any("已夹取" in note for note in result["notes"]))
        self.assertTrue(result.get("needs_user_choice"), "夹取后比例偏差超 5% 必须请用户确认")

    def test_missing_slots_is_error(self):
        result = resolve_size({"ratio": "9:16", "mp": 1.0}, None, None)
        self.assertIn("宽高", str(result.get("error")))

    def test_step_aligns_to_declared_multiple(self):
        """节点声明 step=8（真实 EmptyLatentImage）时，宽高对齐到 8 的倍数。"""
        limits = {"width": {"min": 16, "max": 16384, "step": 8},
                  "height": {"min": 16, "max": 16384, "step": 8}}
        result = resolve_size({"width": 833, "height": 1217}, self.WIDTH, self.HEIGHT, limits)
        self.assertEqual(result["width"] % 8, 0)
        self.assertEqual(result["height"] % 8, 0)
        self.assertTrue(any("步长" in note for note in result["notes"]))

    def test_step_never_breaks_bounds(self):
        """step 对齐后不得越过节点上下限。"""
        limits = {"width": {"min": 100, "max": 103, "step": 8},
                  "height": {"min": 100, "max": 103, "step": 8}}
        result = resolve_size({"width": 102, "height": 102}, self.WIDTH, self.HEIGHT, limits)
        self.assertGreaterEqual(result["width"], 100)
        self.assertLessEqual(result["width"], 103)
        self.assertLessEqual(result["height"], 103)

    def test_no_step_does_not_hardcode_multiple(self):
        """节点没声明 step 时绝不硬编码倍数（对齐前后的值一致）。"""
        limits = {"width": {"min": 16, "max": 16384}, "height": {"min": 16, "max": 16384}}
        result = resolve_size({"width": 833, "height": 1217}, self.WIDTH, self.HEIGHT, limits)
        self.assertEqual((result["width"], result["height"]), (833, 1217))
        self.assertFalse(any("步长" in note for note in result["notes"]))


def pytest_approx_mp(width: int, height: int, mp: float) -> int:
    """1.0MP = 约 1,000,000 总像素；允许取整 ±2% 误差（计划 §5.5）。"""
    total = width * height
    assert abs(total - mp * 1_000_000) <= mp * 1_000_000 * 0.02, (width, height, total)
    return total


class PrepareTests(DreamHarness):
    def test_single_prepare_freezes_overrides_and_request_key(self):
        record = self.service.prepare(
            ownership=self.ownership, workflow_id=WORKFLOW_ID, mode="single",
            values={"b1": "一只猫", "b2": {"ratio": "9:16", "mp": 1.0}, "b3": 42},
            shots=2,
        )
        self.assertEqual(record["mode"], "single")
        single = record["single"]
        self.assertEqual(single["shots"], 2)
        overrides = {(row["node_id"], row["field_name"]): row["value"] for row in single["overrides"]}
        self.assertEqual(overrides[("5", "text")], "一只猫")
        self.assertEqual(overrides[("5", "seed")], 42)
        self.assertEqual(overrides[("6", "width")], record["size_summary"]["width"])
        self.assertEqual(record["summary"]["total_executions"], 2)
        self.assertEqual(record["request_key"][:6], "dream-")
        self.assertFalse(record["missing"])
        self.assertEqual(record["ownership"]["conversation_id"], "conv-1")

    def test_missing_collect_items_returned_not_raised(self):
        record = self.service.prepare(
            ownership=self.ownership, workflow_id=WORKFLOW_ID, mode="single", values={},
        )
        self.assertTrue(any("主体提示词" in item for item in record["missing"]))

    def test_keep_default_records_choice_without_overrides(self):
        record = self.service.prepare(
            ownership=self.ownership, workflow_id=WORKFLOW_ID, mode="single",
            values={"b1": {"keep_default": True}},
        )
        self.assertEqual(record["single"]["overrides"], [])
        self.assertFalse(record["missing"], "keep_default 等于显式回答，必答项不再缺")

    def test_unknown_binding_rejected(self):
        with self.assertRaises(DreamPrepareError):
            self.service.prepare(
                ownership=self.ownership, workflow_id=WORKFLOW_ID, mode="single",
                values={"b9": "x"},
            )

    def test_fingerprint_change_invalidates_config(self):
        # 工作流被重新保存（内容变化）→ 指纹变化 → 配置整条失效，必须重新指认。
        self.prompt["6"]["inputs"]["width"] = 640
        self.store.save_workflow({"id": WORKFLOW_ID, "name": "对话测试流", "prompt": self.prompt,
                                  "params": self.params})
        with self.assertRaises(DreamPrepareError):
            self.service.prepare(
                ownership=self.ownership, workflow_id=WORKFLOW_ID, mode="single",
                values={"b1": "x"},
            )

    def test_batch_expand_uses_prompt_binding_per_item(self):
        record = self.service.prepare(
            ownership=self.ownership, workflow_id=WORKFLOW_ID, mode="batch",
            values={"b2": {"ratio": "1:1", "mp": 0.5}},
            items=[
                {"name": "甲", "values": {"b1": "场景一"}, "shots": 1},
                {"name": "乙", "values": {"b1": "场景二"}, "shots": 2},
            ],
        )
        self.assertEqual(record["mode"], "batch")
        batch = record["batch"]
        self.assertEqual(len(batch["schemes"]), 1)
        scheme = batch["schemes"][0]
        self.assertEqual(scheme["mapping"]["prompt_slot"], {"node_id": "5", "field_name": "text"})
        self.assertEqual([item["prompt"] for item in scheme["items"]], ["场景一", "场景二"])
        self.assertEqual([item["shots"] for item in scheme["items"]], [1, 2])
        self.assertEqual(record["summary"]["total_executions"], 3, "总执行次数=条目 shots 之和，不是条目数")
        overrides = {(row["node_id"], row["field_name"]): row["value"] for row in scheme["overrides"]}
        self.assertEqual(overrides[("6", "width")], overrides[("6", "height")], "1:1 批量共享尺寸")

    def test_batch_requires_positive_prompt_binding(self):
        cfg = [row for row in _bindings() if row["id"] != "b1"]
        self.service.chat_configs.save(WORKFLOW_ID, self.fingerprint(), cfg)
        with self.assertRaises(DreamPrepareError):
            self.service.prepare(
                ownership=self.ownership, workflow_id=WORKFLOW_ID, mode="batch", values={},
                items=[{"name": "甲", "values": {}, "shots": 1}],
            )


class SubmitTests(DreamHarness):
    def setUp(self):
        super().setUp()
        # 批量提交走既有整单拒绝语义：探活必须通过（单次提交不需要）。
        self.reachable.return_value = {}

    def _prepare_single(self, **kwargs):
        return self.service.prepare(
            ownership=self.ownership, workflow_id=WORKFLOW_ID, mode="single",
            values={"b1": "一只猫"}, **kwargs,
        )

    def test_submit_creates_native_task_with_owner_and_key(self):
        record = self._prepare_single()
        receipt = self.service.submit(record["id"])
        self.assertFalse(receipt["already_submitted"])
        task_id = receipt["task_id"]
        self.assertTrue(task_id.startswith("cf-"))
        stored = self.store.get_task(task_id)
        self.assertEqual(stored.get("owner_id"), "conv-1")
        self.assertEqual(stored.get("request_key"), record["request_key"])
        self.assertEqual(stored["input"]["overrides"][0]["value"], "一只猫")
        self.assertIn("queued", receipt["task_status"])

    def test_submit_replay_returns_same_task(self):
        record = self._prepare_single()
        first = self.service.submit(record["id"])
        second = self.service.submit(record["id"])
        self.assertTrue(second["already_submitted"])
        self.assertEqual(second["task_id"], first["task_id"])

    def test_submit_refuses_when_missing_unresolved(self):
        record = self.service.prepare(
            ownership=self.ownership, workflow_id=WORKFLOW_ID, mode="single", values={},
        )
        with self.assertRaises(DreamSubmitError):
            self.service.submit(record["id"])

    def test_submit_refuses_after_fingerprint_change(self):
        record = self._prepare_single()
        self.prompt["5"]["inputs"]["text"] = "changed"
        self.store.save_workflow({"id": WORKFLOW_ID, "name": "对话测试流", "prompt": self.prompt,
                                  "params": self.params})
        with self.assertRaises(DreamSubmitError):
            self.service.submit(record["id"])

    def test_submit_refuses_expired_snapshot(self):
        record = self._prepare_single()
        expired = DreamSnapshotStore(self.root).get(record["id"])
        expired["expires_at"] = 1
        path = self.root / "comfyui" / "dream-snapshots" / f"{record['id']}.json"
        path.write_text(json.dumps(expired, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(DreamSubmitError):
            self.service.submit(record["id"])

    def test_submit_batch_creates_batch_with_outer_key(self):
        record = self.service.prepare(
            ownership=self.ownership, workflow_id=WORKFLOW_ID, mode="batch",
            values={},
            items=[{"name": "甲", "values": {"b1": "场景一"}, "shots": 1}],
        )
        receipt = self.service.submit(record["id"])
        batch_id = receipt["batch_id"]
        self.assertTrue(batch_id.startswith("cfb-"))
        stored = self.batches.get(batch_id)
        self.assertEqual(stored.get("owner_id"), "conv-1")
        self.assertEqual(stored.get("request_key"), record["request_key"])

    def test_status_and_cancel_ownership(self):
        record = self._prepare_single()
        receipt = self.service.submit(record["id"])
        target = encode_wait_target("comfy-task", receipt["task_id"])
        payload = self.service.task_status([target], self.ownership)
        self.assertEqual(payload["targets"][0]["task"]["id"], receipt["task_id"])
        # 别的会话查不了这条任务
        other = {**self.ownership, "conversation_id": "conv-2", "owner_id": "conv-2"}
        payload = self.service.task_status([target], other)
        self.assertIn("不是当前对话发起的", payload["targets"][0]["error"])
        with self.assertRaises(PermissionError):
            self.service.cancel(target, other)
        # 页面提交（无 owner）的任务同样不可见
        page_task = self.jobs.submit({"workflow_id": WORKFLOW_ID, "shots": 1})
        payload = self.service.task_status([page_task["id"]], self.ownership)
        self.assertIn("不是当前对话发起的", payload["targets"][0]["error"])

    def test_sweep_keeps_submitted_receipt_but_purges_stale_unsubmitted(self):
        """提交回执是幂等重放的唯一凭据，24h 清理绝不能删它（计划 §7.2/§8）。"""
        record = self._prepare_single()
        receipt = self.service.submit(record["id"])
        snap_dir = self.root / "comfyui" / "dream-snapshots"
        submitted_path = snap_dir / f"{record['id']}.json"
        stale = self.service.prepare(
            ownership=self.ownership, workflow_id=WORKFLOW_ID, mode="single",
            values={"b1": "过期的未提交预检"},
        )
        stale_path = snap_dir / f"{stale['id']}.json"
        old = time.time() - 25 * 3600
        for path in (submitted_path, stale_path):
            os.utime(path, (old, old))
        # 下一次预检顺手 sweep：老回执必须还在，未提交的过期快照可以清掉。
        self.service.prepare(
            ownership=self.ownership, workflow_id=WORKFLOW_ID, mode="single",
            values={"b1": "触发清理"},
        )
        self.assertTrue(submitted_path.is_file(), "已提交回执不得按 mtime 清理")
        self.assertFalse(stale_path.is_file(), "过期未提交的快照应被清理")
        replay = self.service.submit(record["id"])
        self.assertTrue(replay["already_submitted"])
        self.assertEqual(replay["task_id"], receipt["task_id"], "重放不得生成第二个任务")

    def test_forged_snapshot_id_rejected(self):
        for forged in ("../../etc/passwd", "ds-00000000000X", "", "ds-000000000001"):
            with self.subTest(forged=forged):
                with self.assertRaises(DreamSubmitError):
                    self.service.submit(forged)

    def test_unknown_submission_result_leaves_no_false_receipt(self):
        """受理结果未知时不写假回执、不自动重提（计划 §6/§8）。"""
        record = self._prepare_single()
        with mock.patch.object(self.jobs, "submit", side_effect=RuntimeError("response lost")):
            with self.assertRaises(RuntimeError):
                self.service.submit(record["id"])
        saved = self.service.snapshots.get(record["id"])
        self.assertFalse(saved["submitted"], "受理未知时不能写假回执")


class BatchOuterIdempotencyTests(DreamHarness):
    def setUp(self):
        super().setUp()
        # 批次创建要求 ComfyUI 可达（整单拒绝语义）；桩返回真值表示探活通过。
        self.reachable.return_value = {}

    def _body(self):
        return {
            "title": "对话批量", "unload_after": False,
            "schemes": [{
                "workflow_id": WORKFLOW_ID,
                "mapping": {"prompt_slot": {"node_id": "5", "field_name": "text"}},
                "overrides": [],
                "items": [{"name": "甲", "prompt": "场景一", "extras": {}, "shots": 1,
                           "media_inputs": [], "media_skip": []}],
            }],
        }

    def test_same_key_same_body_returns_existing(self):
        first = self.batches.create(self._body(), request_key="dream-x", owner_id="conv-1")
        second = self.batches.create(self._body(), request_key="dream-x", owner_id="conv-1")
        self.assertEqual(second["id"], first["id"])

    def test_same_key_different_body_rejected(self):
        self.batches.create(self._body(), request_key="dream-y", owner_id="conv-1")
        other = self._body()
        other["title"] = "不一样的批量"
        with self.assertRaises(ComfyUIBatchError):
            self.batches.create(other, request_key="dream-y", owner_id="conv-1")

    def test_owner_scoped_lookup(self):
        self.batches.create(self._body(), request_key="dream-z", owner_id="conv-1")
        row = self.batches.lookup_request("dream-z", owner_id="conv-2")
        self.assertIsNone(row, "owner 不同不算同一请求")


class WaitTargetTests(unittest.TestCase):
    def test_parse_and_roundtrip(self):
        self.assertEqual(job_waits.parse_wait_target("j-1"), ("job", "j-1"))
        self.assertEqual(job_waits.parse_wait_target("comfy-task:cf-1"), ("comfy-task", "cf-1"))
        self.assertEqual(job_waits.parse_wait_target("comfy-batch:cfb-1"), ("comfy-batch", "cfb-1"))
        target = encode_wait_target("comfy-task", "cf-1")
        self.assertEqual(target, "comfy-task:cf-1")
        self.assertEqual(encode_wait_target("comfy-task", target), target, "重复编码幂等")

    def test_resolve_wait_target_jobs(self):
        jobs = SimpleNamespace(
            get=lambda job_id: ({"id": job_id, "kind": "shell", "status": "completed"} if job_id == "j1" else None),
        )
        app = SimpleNamespace(jobs=jobs, comfyui=None)
        self.assertEqual(job_waits.resolve_wait_target(app, "j1")["status"], "completed")
        self.assertIsNone(job_waits.resolve_wait_target(app, "j-missing"))
        self.assertIsNone(job_waits.resolve_wait_target(app, "comfy-task:cf-1"))

    def test_resolve_wait_target_comfy(self):
        comfy = SimpleNamespace(
            jobs=SimpleNamespace(task=lambda task_id: ({"id": task_id, "status": "running", "progress": {"done": 1, "total": 2, "failed": 0}} if task_id == "cf-1" else None)),
            batches=SimpleNamespace(get=lambda batch_id: ({"id": batch_id, "items": [{"status": "completed"}, {"status": "failed"}]} if batch_id == "cfb-1" else None)),
        )
        app = SimpleNamespace(jobs={"get": lambda _id: None}, comfyui=comfy)
        task = job_waits.resolve_wait_target(app, "comfy-task:cf-1")
        self.assertEqual(task["kind"], "comfy-task")
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["id"], "comfy-task:cf-1")
        batch = job_waits.resolve_wait_target(app, "comfy-batch:cfb-1")
        self.assertEqual(batch["status"], "failed", "批次聚合：有失败条目不报完成")
        self.assertIsNone(job_waits.resolve_wait_target(app, "comfy-batch:cfb-404"))

    def test_comfy_batch_status_aggregation(self):
        pick = lambda statuses: job_waits.comfy_batch_status({"items": [{"status": s} for s in statuses]})
        self.assertEqual(pick(["queued", "completed"]), "running")
        self.assertEqual(pick(["completed", "completed"]), "completed")
        self.assertEqual(pick(["completed", "failed"]), "failed")
        self.assertEqual(pick(["cancelled", "completed"]), "cancelled")
        self.assertEqual(pick(["interrupted"]), "interrupted")
        self.assertEqual(job_waits.comfy_batch_status({"items": []}), "running")

    def test_resume_message_covers_comfy_targets(self):
        wait = {"id": "w1", "target_job_ids": ["comfy-task:cf-1", "comfy-batch:cfb-1"]}
        targets = [
            {"id": "comfy-task:cf-1", "kind": "comfy-task", "status": "completed",
             "workflow_name": "测试流", "label": "对话生成 · 测试流",
             "progress": {"done": 2, "total": 2, "failed": 0},
             "result": {"outputs": ["a.png", "b.png"], "completed": []}},
            {"id": "comfy-batch:cfb-1", "kind": "comfy-batch", "status": "failed",
             "title": "对话批量", "items": [{"name": "甲", "status": "failed"}, {"name": "乙", "status": "completed"}]},
        ]
        text = job_waits.build_resume_message(wait, targets, [])
        self.assertIn("造梦机任务 comfy-task:cf-1", text)
        self.assertIn("产物 2 个文件", text)
        self.assertIn("甲: failed", text)
        self.assertIn("失败 1", text)
        self.assertIn("不要重新执行", text)

    def test_resume_message_normal_jobs_unchanged(self):
        wait = {"id": "w1", "target_job_ids": ["j1"]}
        targets = [{"id": "j1", "kind": "shell", "status": "completed", "progress": 100, "result": {"ok": 1}}]
        text = job_waits.build_resume_message(wait, targets, [])
        self.assertIn("Job j1（shell）", text)


class ToolContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_specs_shape_and_policies(self):
        specs = {spec.name: spec for spec in build_dream_tool_specs()}
        self.assertEqual(set(specs), {
            "dream_workflows", "dream_workflow_details", "dream_prepare", "dream_submit",
            "dream_task_status", "dream_task_cancel", "dream_task_wait",
        })
        # submit/cancel 是唯一有副作用的：走 ToolExecutor 真实权限链（计划 §7.3/§7.8）。
        for name, spec in specs.items():
            with self.subTest(tool=name):
                self.assertFalse(spec.system, f"{name} 不得是 system 工具（绕过确认链）")
                self.assertIsNotNone(spec.policy, f"{name} 缺 def 级 policy")
                if name in ("dream_submit", "dream_task_cancel"):
                    self.assertTrue(spec.side_effect)
                    self.assertFalse(spec.retryable, f"{name} 绝不自动重试（防第二次生成）")
                else:
                    self.assertFalse(spec.side_effect)
                if name == "dream_submit":
                    self.assertFalse(spec.retryable)

    def test_media_declarations_registered(self):
        from naiba.tools.registry import MEDIA_DECLARATIONS

        for name in ("dream_workflows", "dream_workflow_details", "dream_prepare", "dream_submit", "dream_task_cancel"):
            self.assertEqual(MEDIA_DECLARATIONS[name], {"policy": "never", "extract": "none"})
        self.assertEqual(MEDIA_DECLARATIONS["dream_task_status"], {"policy": "inline", "extract": "structured"})
        self.assertEqual(MEDIA_DECLARATIONS["dream_task_wait"], {"policy": "inline", "extract": "structured"})

    def test_opt_in_group_registered(self):
        from naiba.config import OPT_IN_TOOLS, _DREAM_TOOLS

        for name in _DREAM_TOOLS:
            self.assertIn(name, OPT_IN_TOOLS)

    def test_orchestration_guide_gating(self):
        """编排提示按会话工具集条件注入（计划 §7.4）：不点名会话里没有的工具。"""
        from naiba.skills.agent import dream_tools_guide_text

        full = {"dream_workflows", "dream_workflow_details", "dream_prepare",
                "dream_submit", "dream_task_status", "dream_task_cancel", "dream_task_wait"}
        text = dream_tools_guide_text(full)
        self.assertIn("dream_workflows", text)
        self.assertIn("对话参数", text)
        self.assertIn("禁止猜节点地址", text)
        # 缺依赖时明确说 submit 会被拒绝，不暗示能补。
        incomplete = dream_tools_guide_text({"dream_submit", "dream_task_wait"})
        self.assertIn("不完整", incomplete)
        self.assertIn("dream_prepare", incomplete)
        # 没有 submit 就不注入（只读 Agent 不该看到生成编排）。
        self.assertEqual(dream_tools_guide_text({"dream_workflows", "dream_prepare"}), "")

    def test_handlers_refuse_non_main_conversation(self):
        app = SimpleNamespace(
            config=SimpleNamespace(data={"comfyui_url": ""}),
            paths=SimpleNamespace(data_dir=Path(self.tmp.name)),
            storage=None,
        )
        handler = dream_provider._make_handler(app, "dream_workflows")
        cases = [
            ({"conv_kind": "aux", "run_origin": "user", "depth": 0, "conversation_id": "c1"}, "普通主对话"),
            ({"conv_kind": "main", "run_origin": "job_wait", "depth": 0, "conversation_id": "c1"}, "普通主对话"),
            ({"conv_kind": "main", "run_origin": "user", "depth": 1, "conversation_id": "c1"}, "普通主对话"),
            ({"conv_kind": "main", "run_origin": "user", "depth": 0, "conversation_id": ""}, "无法确定当前对话"),
            # 缺归属字段必须 fail-closed：不能默认成普通主对话（计划 §7.3/§7.4）。
            ({"conversation_id": "c1"}, "缺少对话形态字段"),
            ({"conv_kind": "main", "run_origin": "user", "conversation_id": "c1"}, "缺少对话形态字段"),
            ({"conv_kind": "main", "run_origin": "user", "depth": None, "conversation_id": "c1"}, "运行层级不合法"),
            (None, "缺少对话形态字段"),
        ]
        for index, (run_context, expected) in enumerate(cases):
            with self.subTest(index=index):
                ok, text = handler({}, [], run_context)
                self.assertFalse(ok)
                self.assertIn(expected, text)

    def test_submit_requires_dependency_tools(self):
        app = SimpleNamespace(
            config=SimpleNamespace(data={"comfyui_url": ""}),
            paths=SimpleNamespace(data_dir=Path(self.tmp.name)),
            storage=None,
        )
        handler = dream_provider._make_handler(app, "dream_submit")
        run_context = {
            "conv_kind": "main", "run_origin": "user", "depth": 0,
            "conversation_id": "c1", "allowed_tools": ["dream_submit"],
        }
        ok, text = handler({"snapshot_id": "ds-000000000001"}, [], run_context)
        self.assertFalse(ok)
        self.assertIn("缺少", text)
        self.assertIn("dream_prepare", text)

    def test_notify_switch_is_not_exposed_to_model(self):
        """通知登记是宿主动作：schema 不开放 notify 开关（计划 §7.5）。"""
        specs = {spec.name: spec for spec in build_dream_tool_specs()}
        for name in ("dream_prepare", "dream_submit"):
            with self.subTest(tool=name):
                self.assertNotIn("notify", specs[name].parameters["properties"])

    def test_submit_registers_notification_even_if_model_says_no(self):
        """伪造 notify=false 也不能绕过登记；登记结果随回执如实返回。"""
        app = SimpleNamespace(paths=SimpleNamespace(data_dir=Path(self.tmp.name)))
        receipt = {
            "kind": "comfy-task", "task_id": "cf-1", "task_status": "queued",
            "snapshot_id": "ds-000000000001", "already_submitted": False,
        }
        with mock.patch.object(dream_provider, "DreamService") as svc_cls, \
                mock.patch.object(dream_provider, "_register_notification",
                                  return_value=(True, "w1", "")) as register:
            svc_cls.return_value.submit.return_value = receipt
            handler = dream_provider._make_handler(app, "dream_submit")
            ctx = {
                "conv_kind": "main", "run_origin": "user", "depth": 0,
                "conversation_id": "c1",
                "allowed_tools": ["dream_prepare", "dream_workflow_details",
                                  "dream_task_status", "dream_submit"],
            }
            ok, text = handler(
                {"snapshot_id": "ds-000000000001", "notify": False}, [], ctx,
            )
        self.assertTrue(ok)
        self.assertEqual(register.call_count, 1)
        payload = json.loads(text)
        self.assertTrue(payload["notify_registered"])
        self.assertEqual(payload["notify_wait_id"], "w1")

    def test_reused_wait_is_reported_as_registered(self):
        """同目标重复登记返回 reuse：不能误报成「通知没登记上」。"""
        app = SimpleNamespace(
            job_waits=SimpleNamespace(register=lambda **kwargs: ({"id": "w1"}, "reuse")),
            storage=SimpleNamespace(get_run_snapshot=lambda rid: {}),
        )
        with mock.patch("naiba.subagent.job_wait_execution_snapshot", return_value={}):
            ok, wait_id, _note = dream_provider._register_notification(
                app, ownership={"conversation_id": "c1", "origin_run_id": "r1"},
                run_context={}, targets=["comfy-task:cf-1"],
            )
        self.assertTrue(ok)
        self.assertEqual(wait_id, "w1")


class ComfyNativeAdapterTests(DreamHarness):
    """总管原生薄适配：确定性提交 / 状态映射 / 取消（计划 §7.2/§7.7）。"""

    def _adapter(self):
        from naiba.super_agent.adapters import ComfyNativeAdapter

        service = SimpleNamespace(
            get_thread=lambda *a, **k: None,
            get_node=self._get_node,
            store=SimpleNamespace(update_node=self._update_node),
            emit_event=lambda *a, **k: None,
            artifacts=lambda *a, **k: [],
        )
        self._nodes = {}
        return ComfyNativeAdapter(service, self.app)

    def _mknode(self, node_id="n1"):
        from naiba.super_agent.models import TaskNode

        node = TaskNode(
            node_id=node_id, thread_id="t1", kind="generate", owner_id="conv-1",
            status="ready", attempt=1, executor_type="comfy_native",
        )
        self._nodes = {node_id: node}
        return node

    def _get_node(self, _owner, node_id):
        return self._nodes[node_id]

    def _update_node(self, node_id, *, owner_id, expected_attempt=None, **updates):
        node = self._nodes[node_id]
        for key, value in updates.items():
            setattr(node, key, value)
        return node

    def test_start_maps_single_task(self):
        adapter = self._adapter()
        self._mknode()
        result = adapter.start(
            "conv-1", "t1", "n1",
            submit_body={"kind": "single", "single": {"workflow_id": WORKFLOW_ID, "shots": 1}},
            request_key="dream-adapter-1",
        )
        self.assertEqual(result.status, "running", "queued 映射为节点 running")
        task_id = result.executor_id
        self.assertTrue(task_id.startswith("cf-"))
        stored = self.store.get_task(task_id)
        self.assertEqual(stored.get("owner_id"), "conv-1")
        self.assertEqual(stored.get("request_key"), "dream-adapter-1")

    def test_start_requires_body_and_ready(self):
        adapter = self._adapter()
        self._mknode()
        with self.assertRaises(Exception):
            adapter.start("conv-1", "t1", "n1")
        result_probe = adapter.start(
            "conv-1", "t1", "n1",
            submit_body={"kind": "single", "single": {"workflow_id": WORKFLOW_ID}},
        )
        # 已绑定 executor：重复 start 返回原执行状态，不重复提交。
        again = adapter.start(
            "conv-1", "t1", "n1",
            submit_body={"kind": "single", "single": {"workflow_id": WORKFLOW_ID}},
            request_key="dream-adapter-2",
        )
        self.assertEqual(again.executor_id, result_probe.executor_id)

    def test_retry_resume_refused(self):
        adapter = self._adapter()
        self._mknode()
        with self.assertRaises(Exception):
            adapter.retry("conv-1", "t1", "n1")
        with self.assertRaises(Exception):
            adapter.resume("conv-1", "t1", "n1")

    def test_adapter_registered_in_domain(self):
        from naiba.super_agent.adapters import ComfyNativeAdapter
        from naiba.super_agent.router import AdapterRegistry

        # 注册表只按 executor_type 收录（路由不回退到模型）；coordinator 装配在
        # app 组装测试与总管回归里覆盖，这里直接验类型可注册。
        registry = AdapterRegistry()
        registry.register(ComfyNativeAdapter(SimpleNamespace(), self.app))
        self.assertEqual(registry.get("comfy_native").executor_type, "comfy_native")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
