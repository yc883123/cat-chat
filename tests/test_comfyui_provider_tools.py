# -*- coding: utf-8 -*-
"""comfyui_batch 工具处理器守门：负 seed 随机上限 + shots 必须生效。

2026-09-20 事故的两个根因（用户要 10 张图，内置批量工具 6 次提交全军覆没）：
1. 负 seed 替换取 randbelow(2^63)，超出 rgthree「Seed (rgthree)」节点声明的 ±2^50，
   ComfyUI 整单 400 value_bigger_than_max；同一文件 MCP 原样提交却成功。
2. shots 只在单 workflow 分支生效，workflow_paths + shots=10 被静默吞成 1 段
   （模型看到返回的 total=1 才发现："total=1 — but shots=10"）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.tools.providers.comfyui import (  # noqa: E402
    _COMFYUI_SEED_RANDOM_MAX,
    _comfyui_batch_handler,
    _normalize_comfyui_runtime_workflow,
)


class _JobsStub:
    def __init__(self) -> None:
        self.started: list[tuple[object, str]] = []
        self.next_id = 0

    def start(self, spec, owner=None) -> str:
        self.next_id += 1
        job_id = f"job{self.next_id}"
        self.started.append((spec, owner or ""))
        return job_id


class _AppStub:
    def __init__(self) -> None:
        self.jobs = _JobsStub()


class NegativeSeedBoundaryTests(unittest.TestCase):
    def test_negative_seed_stays_within_conservative_max(self) -> None:
        workflow = {
            "20": {"class_type": "Seed (rgthree)", "inputs": {"seed": -1}},
            "3": {"class_type": "KSampler", "inputs": {"seed": -1, "noise_seed": -5}},
        }
        normalized = _normalize_comfyui_runtime_workflow(workflow)
        checked = 0
        for node_id, keys in (("20", ("seed",)), ("3", ("seed", "noise_seed"))):
            for key in keys:
                value = normalized[node_id]["inputs"][key]
                self.assertIsInstance(value, int)
                self.assertGreaterEqual(value, 0)
                # rgthree 节点把 seed 上限声明为 ±2^50：随机值必须落在 [0, 2^50]，
                # 否则 ComfyUI 400 拒收整个工作流。
                self.assertLessEqual(value, _COMFYUI_SEED_RANDOM_MAX)
                checked += 1
        self.assertEqual(checked, 3)

    def test_positive_seed_and_linked_seed_untouched(self) -> None:
        workflow = {
            "3": {"class_type": "KSampler", "inputs": {"seed": 123, "noise_seed": ["20", 0]}},
        }
        normalized = _normalize_comfyui_runtime_workflow(workflow)
        self.assertEqual(normalized["3"]["inputs"]["seed"], 123)
        self.assertEqual(normalized["3"]["inputs"]["noise_seed"], ["20", 0], "节点连线引用不得当成 seed 改写")


class ComfyuiBatchShotsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba_comfy_tools_"))
        self.workflow_file = self.tmp / "wf.json"
        self.workflow_file.write_text(
            json.dumps({"3": {"class_type": "KSampler", "inputs": {"seed": 1}}}), encoding="utf-8"
        )
        self.app = _AppStub()
        self.ctx = {"conversation_id": "conv1", "run_id": "run1"}

    def _last_spec(self):
        spec, owner = self.app.jobs.started[-1]
        return spec, owner

    def test_workflow_paths_with_shots_expands(self) -> None:
        ok, payload = _comfyui_batch_handler(
            self.app,
            {"workflow_paths": [str(self.workflow_file)], "shots": 10, "wait": False},
            None,
            self.ctx,
        )
        self.assertTrue(ok, payload)
        result = json.loads(payload)
        self.assertEqual(result["total"], 10, "shots=10 必须展开成 10 段（事故里被静默吞成 1）")
        spec, owner = self._last_spec()
        self.assertEqual(len(spec.params["workflows"]), 10)
        self.assertEqual(spec.parent_job_id, "run1")
        self.assertEqual(owner, "conv1")

    def test_multiple_paths_times_shots(self) -> None:
        second = self.tmp / "wf2.json"
        second.write_text(json.dumps({"4": {"class_type": "EmptyLatent", "inputs": {}}}), encoding="utf-8")
        ok, payload = _comfyui_batch_handler(
            self.app,
            {"workflow_paths": [str(self.workflow_file), str(second)], "shots": 3},
            None,
            self.ctx,
        )
        self.assertTrue(ok, payload)
        self.assertEqual(json.loads(payload)["total"], 6, "两个路径 × shots=3 = 6 段")
        spec, _ = self._last_spec()
        self.assertEqual(len(spec.params["workflows"]), 6)

    def test_single_workflow_with_shots_still_works(self) -> None:
        workflow = {"3": {"class_type": "KSampler", "inputs": {"seed": 2}}}
        ok, payload = _comfyui_batch_handler(self.app, {"workflow": workflow, "shots": 4}, None, self.ctx)
        self.assertTrue(ok, payload)
        self.assertEqual(json.loads(payload)["total"], 4)

    def test_explicit_array_with_shots_rejected_not_silently_dropped(self) -> None:
        ok, message = _comfyui_batch_handler(
            self.app,
            {"workflows": [{"3": {"class_type": "KSampler", "inputs": {}}}], "shots": 5},
            None,
            self.ctx,
        )
        self.assertFalse(ok)
        self.assertIn("shots", message)
        self.assertEqual(self.app.jobs.started, [], "拒绝路径不得创建 Job")

    def test_bad_shots_reports_error(self) -> None:
        ok, message = _comfyui_batch_handler(
            self.app, {"workflow_paths": [str(self.workflow_file)], "shots": "abc"}, None, self.ctx
        )
        self.assertFalse(ok)
        self.assertIn("shots", message)

    def test_runtime_normalization_applied_per_expanded_workflow(self) -> None:
        # 展开后的每一段都要过负 seed 规范化（文件里 seed=-1 → 每段独立随机且不超上限）。
        workflow_file = self.tmp / "wf_neg.json"
        workflow_file.write_text(
            json.dumps({"20": {"class_type": "Seed (rgthree)", "inputs": {"seed": -1}}}), encoding="utf-8"
        )
        ok, payload = _comfyui_batch_handler(
            self.app, {"workflow_paths": [str(workflow_file)], "shots": 3}, None, self.ctx
        )
        self.assertTrue(ok, payload)
        spec, _ = self._last_spec()
        seeds = [wf["20"]["inputs"]["seed"] for wf in spec.params["workflows"]]
        self.assertEqual(len(seeds), 3)
        for seed in seeds:
            self.assertIsInstance(seed, int)
            self.assertGreaterEqual(seed, 0)
            self.assertLessEqual(seed, _COMFYUI_SEED_RANDOM_MAX)


if __name__ == "__main__":
    unittest.main()
