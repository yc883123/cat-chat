# -*- coding: utf-8 -*-
"""精确逐帧（extract_video_frames_exact）守门：意图门 / 范围校验 / 逐帧解码 / 清单分页 / 工具入口。

三条边界（计划 2026-10-22 §4.2）：

1. **意图门是服务端判定**：只有用户原文明确要求逐帧才能创建 Job；模型传参不能自证；
2. **范围在创建前整项校验**：时间/帧区间互斥、越界、超过单次上限一律拒绝，不截断；
3. **精确模式不做时间网格**：按解码顺序逐帧落盘，帧号连续无洞（对照实测的 interval=1/fps 漏帧）。

夹具沿用 ``tests/fixtures/sample_cut.mp4``（320x240、24 fps、6.0 秒，第 5 秒硬切镜）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from naiba import video as video_svc
from naiba.core.attachments import video_frames_intent
from naiba.mcp import MCPRegistry
from naiba.tools.providers import core as core_provider
from naiba.tools.providers.video import (
    EXACT_CONTEXT_REQUIRED,
    EXACT_INTENT_REQUIRED,
    EXACT_JOB_REQUIRED,
    VideoToolProvider,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_cut.mp4"
BASE_FPS = 24.0
BASE_DURATION = 6.0


def _ctx(workspace: Path) -> core_provider.ToolContext:
    return core_provider.ToolContext(
        workspace=workspace,
        python_executable=sys.executable,
        command_timeout=60,
        mcp_registry=MCPRegistry([]),
        mcp_register=None,
    )


class VideoFramesIntentTests(unittest.TestCase):
    """用户原文的逐帧意图判定（fail closed：拿不准一律 False）。"""

    def test_explicit_frame_requests_pass(self) -> None:
        for text in (
            "把这段视频逐帧导出成图片",
            "我要每一帧都截下来",
            "连续帧全部导出来，放进 outputs",
            "一帧不漏地提取出来",
            "按帧号 0 到 100 导出",
            "please extract it frame by frame",
            "导出所有帧",
        ):
            with self.subTest(text=text):
                self.assertTrue(video_frames_intent(text), text)

    def test_negations_and_sampling_requests_are_rejected(self) -> None:
        for text in (
            "不要逐帧，抽几帧看看就够了",
            "不需要逐帧，给我概览",
            "先别逐帧导出，关键帧就行",
            "帮我抽几帧看看画面",
            "这一段的关键帧有哪些",
            "给我一个整体概览",
            "他说要逐帧导出",
            "原文里提到过逐帧",
            "「逐帧」是什么意思？",
            "",
            "随便看看这个视频",
        ):
            with self.subTest(text=text):
                self.assertFalse(video_frames_intent(text), text)


class ExactRangeTests(unittest.TestCase):
    """创建 Job 之前的范围校验：互斥、越界、超限都必须整项拒绝。"""

    def test_time_range_resolves_with_estimate(self) -> None:
        plan = video_svc.resolve_exact_range(FIXTURE, start=0, end=1)
        self.assertEqual(plan["mode"], "time")
        self.assertEqual(plan["start"], 0.0)
        self.assertEqual(plan["end"], 1.0)
        self.assertAlmostEqual(plan["fps"], BASE_FPS, delta=0.01)
        self.assertEqual(plan["estimated_frames"], 24)

    def test_frame_range_resolves_without_estimate_guess(self) -> None:
        plan = video_svc.resolve_exact_range(FIXTURE, start_frame=0, end_frame=24)
        self.assertEqual(plan["mode"], "frame")
        self.assertEqual(plan["start_frame"], 0)
        self.assertEqual(plan["end_frame"], 24)
        self.assertEqual(plan["estimated_frames"], 24)

    def test_time_and_frame_ranges_are_mutually_exclusive(self) -> None:
        with self.assertRaises(video_svc.ExactRangeError):
            video_svc.resolve_exact_range(FIXTURE, start=0, end=1, start_frame=0, end_frame=24)

    def test_missing_range_is_rejected(self) -> None:
        with self.assertRaises(video_svc.ExactRangeError):
            video_svc.resolve_exact_range(FIXTURE)

    def test_inverted_and_negative_ranges_are_rejected(self) -> None:
        with self.assertRaises(video_svc.ExactRangeError):
            video_svc.resolve_exact_range(FIXTURE, start=3, end=1)
        with self.assertRaises(video_svc.ExactRangeError):
            video_svc.resolve_exact_range(FIXTURE, start=-1, end=1)
        with self.assertRaises(video_svc.ExactRangeError):
            video_svc.resolve_exact_range(FIXTURE, start_frame=-1, end_frame=10)
        with self.assertRaises(video_svc.ExactRangeError):
            video_svc.resolve_exact_range(FIXTURE, start_frame=10, end_frame=10)

    def test_out_of_bounds_and_over_limit_are_rejected(self) -> None:
        # 时长 6 秒：end=7 越界（留 0.05 秒容差，7 明显越界）
        with self.assertRaises(video_svc.ExactRangeError):
            video_svc.resolve_exact_range(FIXTURE, start=0, end=7)
        # 帧区间超过 EXACT_MAX_FRAMES：整项拒绝而不是截断
        over = video_svc.EXACT_MAX_FRAMES + 1
        with self.assertRaises(video_svc.ExactRangeError):
            video_svc.resolve_exact_range(FIXTURE, start_frame=0, end_frame=over)
        # start_frame 越界：声明帧数只有 144
        with self.assertRaises(video_svc.ExactRangeError):
            video_svc.resolve_exact_range(FIXTURE, start_frame=500, end_frame=510)


class ExactExtractionTests(unittest.TestCase):
    """逐帧解码：数量/帧号连续性/清单分页/取消语义。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_frame_range_extracts_every_frame_without_gaps(self) -> None:
        plan = video_svc.resolve_exact_range(FIXTURE, start_frame=0, end_frame=24)
        result = video_svc.extract_frames_exact(FIXTURE, self.data_dir, plan=plan)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["range_complete"])
        self.assertEqual(result["frame_count"], 24, "24 fps 的 1 秒帧区间必须恰好 24 帧")
        payload = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        frames = payload["frames"]
        self.assertEqual(len(frames), 24)
        self.assertEqual([item["frame_index"] for item in frames], list(range(24)),
                         "帧号必须连续无洞（精确模式的唯一判据）")
        self.assertEqual(len({item["path"] for item in frames}), 24, "不得重复写同一张图")
        timestamps = [item["timestamp"] for item in frames]
        self.assertEqual(timestamps, sorted(timestamps), "时间戳必须单调递增")
        self.assertTrue(all(item["status"] == "extracted" for item in frames))

    def test_second_range_keeps_absolute_frame_numbers(self) -> None:
        """帧号是"这条视频的第几帧"，与请求区间无关（便于后续按帧号定位）。"""
        plan = video_svc.resolve_exact_range(FIXTURE, start_frame=24, end_frame=48)
        result = video_svc.extract_frames_exact(FIXTURE, self.data_dir, plan=plan)
        self.assertEqual(result["first_frame"], 24)
        self.assertEqual(result["last_frame"], 47)
        self.assertEqual(result["frame_count"], 24)

    def test_cancel_keeps_extracted_frames_and_never_claims_complete(self) -> None:
        plan = video_svc.resolve_exact_range(FIXTURE, start_frame=0, end_frame=48)
        seen = {"count": 0}

        def _cancel() -> bool:
            seen["count"] += 1
            return seen["count"] > 12  # 第 12 帧之后请求取消

        result = video_svc.extract_frames_exact(
            FIXTURE, self.data_dir, plan=plan, cancel_check=_cancel
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(result["range_complete"], "取消绝不写成完整")
        self.assertGreaterEqual(result["frame_count"], 12, "已提交块必须保留")
        self.assertLess(result["frame_count"], 48)
        page = video_svc.read_exact_manifest(result["manifest"])
        self.assertEqual(page["status"], "cancelled")
        self.assertEqual(len(page["frames"]), result["frame_count"])

    def test_manifest_pagination_has_no_duplicates_or_gaps(self) -> None:
        plan = video_svc.resolve_exact_range(FIXTURE, start_frame=0, end_frame=24)
        result = video_svc.extract_frames_exact(FIXTURE, self.data_dir, plan=plan)
        collected: list[dict] = []
        offset = 0
        pages = 0
        while offset is not None:
            page = video_svc.read_exact_manifest(result["manifest"], offset=offset, limit=10)
            collected.extend(page["frames"])
            offset = page["next_offset"]
            pages += 1
            self.assertLess(pages, 10, "分页必须有终止")
        self.assertEqual(pages, 3, "24 帧 / 每页 10 = 3 页（末页 4 条）")
        self.assertEqual([item["frame_index"] for item in collected], list(range(24)))
        self.assertIsNone(
            video_svc.read_exact_manifest(result["manifest"], offset=24, limit=10)["next_offset"],
            "末页 next_offset 必须为 None",
        )

    def test_time_range_completes_on_cfr_fixture(self) -> None:
        plan = video_svc.resolve_exact_range(FIXTURE, start=0, end=1)
        result = video_svc.extract_frames_exact(FIXTURE, self.data_dir, plan=plan)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["range_complete"])
        self.assertEqual(result["frame_count"], 24)


class _FakeRegistry:
    """只记录 JobSpec，不启动 worker（工具入口的守门用）。"""

    def __init__(self, storage: object | None = None) -> None:
        self.started: list[object] = []
        self.app = SimpleNamespace(storage=storage)

    def start(self, spec, owner=None) -> str:
        self.started.append(spec)
        return "job_fake_1"

    def list(self, owner=None, active_only=False):
        return []


class _FakeStorage:
    """幂等复用用：返回一条同范围的活动 Job 与它的 run snapshot。"""

    def __init__(self, param_rows: list[dict], snapshots: dict[str, dict]) -> None:
        self._rows = param_rows
        self._snapshots = snapshots

    def list_background_tasks(self, conversation_id="", active_only=False):
        return list(self._rows)

    def get_run_snapshot(self, job_id: str):
        return self._snapshots.get(job_id)


class ExactToolEntryTests(unittest.TestCase):
    """工具入口的四道门：意图 → 上下文 → 范围/容量 → 幂等（都不写盘）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.provider = VideoToolProvider(_ctx(FIXTURE.parent), lambda: self.data_dir)
        self.spec = next(
            item for item in self.provider.tools() if item.name == "extract_video_frames_exact"
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, args: dict, run_context: dict) -> tuple[bool, str]:
        return self.spec.execute(args, [], run_context)

    def _base_ctx(self, registry: _FakeRegistry) -> dict:
        return {
            "run_id": "run_1",
            "conversation_id": "conv_1",
            "owner_session_id": "conv_1",
            "depth": 0,
            "interaction_mode": "craft",
            "job_registry": registry,
            "video_frames_intent": True,
        }

    def test_intent_and_context_gates_reject_without_creating_job(self) -> None:
        registry = _FakeRegistry()
        args = {"path": str(FIXTURE), "start_frame": 0, "end_frame": 24}
        cases = {
            EXACT_INTENT_REQUIRED: {**self._base_ctx(registry), "video_frames_intent": False},
            EXACT_CONTEXT_REQUIRED: {**self._base_ctx(registry), "depth": 1},
            EXACT_JOB_REQUIRED: {**self._base_ctx(registry), "job_registry": None},
        }
        for code, ctx in cases.items():
            with self.subTest(code=code):
                ok, text = self._run(args, ctx)
                self.assertFalse(ok)
                self.assertEqual(json.loads(text)["error"], code)
        # plan 模式同样拒绝
        ok, text = self._run(args, {**self._base_ctx(registry), "interaction_mode": "plan"})
        self.assertFalse(ok)
        self.assertEqual(json.loads(text)["error"], EXACT_CONTEXT_REQUIRED)
        self.assertEqual(registry.started, [], "被拒绝时绝不能创建 Job")

    def test_range_validation_rejects_before_job_creation(self) -> None:
        registry = _FakeRegistry()
        ok, text = self._run(
            {"path": str(FIXTURE), "start_frame": 0, "end_frame": video_svc.EXACT_MAX_FRAMES + 1},
            self._base_ctx(registry),
        )
        self.assertFalse(ok)
        self.assertIn("上限", text)
        self.assertEqual(registry.started, [])

    def test_creates_detached_job_with_compact_payload(self) -> None:
        registry = _FakeRegistry()
        ok, text = self._run(
            {"path": str(FIXTURE), "start_frame": 0, "end_frame": 24, "max_edge": 800},
            self._base_ctx(registry),
        )
        self.assertTrue(ok, text)
        payload = json.loads(text)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["job_id"], "job_fake_1")
        self.assertEqual(payload["estimated_frames"], 24)
        self.assertEqual(payload["manifest_api"], "/api/jobs/job_fake_1/manifest")
        self.assertNotIn("frames", payload, "工具结果只给摘要，不返回帧路径清单")
        self.assertEqual(len(registry.started), 1)
        spec = registry.started[0]
        self.assertEqual(spec.kind, "video_frames")
        self.assertTrue(spec.detached, "逐帧 Job 必须与主聊天并行")
        self.assertIsNone(spec.parent_job_id, "不建立父子关系（避免取消级联）")
        self.assertEqual(spec.origin_run_id, "run_1")
        self.assertEqual(spec.params["plan"]["mode"], "frame")
        self.assertEqual(spec.params["max_edge"], 800)
        self.assertTrue(spec.params["video_key"])

    def test_same_video_and_range_reuses_active_job(self) -> None:
        plan = video_svc.resolve_exact_range(FIXTURE, start_frame=0, end_frame=24)
        video_key = video_svc.video_fingerprint(FIXTURE)
        storage = _FakeStorage(
            [{"id": "job_existing", "kind": "video_frames", "owner_session_id": "conv_1",
              "status": "running"}],
            {"job_existing": {"params": {"video_key": video_key, "plan": plan}}},
        )
        registry = _FakeRegistry(storage)
        ok, text = self._run(
            {"path": str(FIXTURE), "start_frame": 0, "end_frame": 24}, self._base_ctx(registry)
        )
        self.assertTrue(ok, text)
        payload = json.loads(text)
        self.assertTrue(payload["reused"])
        self.assertEqual(payload["job_id"], "job_existing")
        self.assertEqual(registry.started, [], "幂等复用不得再创建一个 Job")


class _FakeBus:
    """JobRegistry 只需要事件出口与条件变量；逐帧测试不落事件库。"""

    def emit(self, run_id, payload, db=None, raise_on_error=False):
        return payload

    def ensure(self, run_id):
        import threading

        return threading.Condition()

    def drop(self, run_id):
        return None


class VideoFramesJobWorkerTests(unittest.TestCase):
    """worker 级：一次 Job 处理完整范围，终态结果带 manifest，checkpoint 预写清单路径。"""

    def setUp(self) -> None:
        from naiba.jobs import JobRegistry, JobSpec
        from naiba.storage.store import ChatStorage

        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.app = SimpleNamespace(
            paths=SimpleNamespace(data_dir=self.data_dir),
            storage=self.storage,
            event_bus=_FakeBus(),
            job_media_writer=None,
        )
        self.registry = JobRegistry(self.app)
        self.JobSpec = JobSpec
        conv = self.storage.create_conversation(agent_id="master")
        self.conversation_id = conv["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_job(self, plan: dict, max_edge: int = 1600) -> tuple[str, object]:
        run = self.storage.create_run(
            self.conversation_id, "逐帧抽取", {"id": "master", "name": "Job", "system_prompt": "", "skill_ids": []},
            {}, kind="video_frames", detached=True, origin_run_id="run_origin",
        )
        job_id = str(run["id"])
        spec = self.JobSpec(
            kind="video_frames",
            conversation_id=self.conversation_id,
            params={"path": str(FIXTURE), "plan": plan, "max_edge": max_edge,
                    "video_key": video_svc.video_fingerprint(FIXTURE)},
            label="逐帧抽取",
            owner_session_id=self.conversation_id,
            detached=True,
            origin_run_id="run_origin",
        )
        return job_id, spec

    def test_worker_finishes_with_manifest_and_readable_checkpoint(self) -> None:
        import threading

        plan = video_svc.resolve_exact_range(FIXTURE, start_frame=0, end_frame=24)
        job_id, spec = self._make_job(plan)
        self.registry._run_video_frames(job_id, spec, threading.Event())
        job = self.storage.get_background_task(job_id)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["frame_count"], 24)
        self.assertTrue(job["result"]["range_complete"])
        # checkpoint 在开始抽取前就写入了确定性的清单路径（运行中也能分页读）。
        manifest = str(job["checkpoint"]["exact"]["manifest"])
        self.assertTrue(manifest.endswith("manifest.json"))
        page = video_svc.read_exact_manifest(manifest, limit=5)
        self.assertEqual(page["total"], 24)
        self.assertEqual(page["status"], "completed")

    def test_worker_reports_failure_without_claiming_complete(self) -> None:
        import threading

        plan = video_svc.resolve_exact_range(FIXTURE, start_frame=0, end_frame=24)
        # 不存在的视频：范围已解析（测试里直接给 plan），worker 必须明确失败。
        job_id, spec = self._make_job(plan)
        spec.params = {**spec.params, "path": str(FIXTURE.with_name("missing.mp4"))}
        self.registry._run_video_frames(job_id, spec, threading.Event())
        job = self.storage.get_background_task(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("视频文件不存在", job["error"])


if __name__ == "__main__":
    unittest.main()
