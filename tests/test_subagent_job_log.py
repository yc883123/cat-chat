# -*- coding: utf-8 -*-
"""守门：子代理的过程必须能落到任务面板（日志 + 当前步骤）。

背景（用户上报，打标场景）：拖入 76 张图后主 Agent 拆出多个审核子任务，子任务面板
全程「任务日志：暂无输出」「当前步骤：已启动」，直到五分多钟后变成「已取消」。

根因是**翻译层缺失**，不是子代理没有过程：

1. 子代理发的是 agent 域事件（status / tool_requested / tool_result …），**没有一个带
   ``line`` 字段**，而前端任务日志只认 ``line`` ⇒ 日志恒为空；
2. ``current_step`` 只有 ``JobRegistry._set_status`` 会写，子代理运行器只在启动时写过
   一次「已启动」⇒ 面板全程停在启动态。

修复：``_subagent_event_sink`` 把 agent 事件翻译成 ``job_log`` 行 + ``current_step``，
并**原样透传**原始事件（契约校验 / job_output / 取证都依赖它）。
全量验证口径见 `项目维护说明（修改代码前必读）.md` §六。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.jobs import (  # noqa: E402
    JobRegistry,
    JobSpec,
    _SUBAGENT_LOG_MAX_CHARS,
    _subagent_current_step,
    _subagent_event_sink,
    _subagent_log_line,
)

AGENT = {"id": "", "name": "Job", "system_prompt": "", "skill_ids": []}


class FakeRegistry:
    """只记录「发了什么」的替身：翻译层单测不碰数据库。"""

    def __init__(self) -> None:
        self.emitted: list[dict] = []
        self.steps: list[str] = []

    def _emit(self, job_id: str, payload: dict) -> None:
        self.emitted.append(payload)

    def _set_status(self, job_id: str, status: str, **fields) -> None:
        # 与真实实现同形：_set_status 除了写库还会发一条 job_status 事件。
        self.steps.append(str(fields.get("current_step") or ""))
        self.emitted.append({"type": "job_status", "status": status, **fields})


class SubagentEventTranslationTests(unittest.TestCase):
    """翻译表逐行断言（对应维护说明 §九.129）。"""

    def test_status_becomes_log_line_and_step(self) -> None:
        payload = {"type": "status", "message": "正在思考（第 3 轮）"}
        self.assertEqual(_subagent_log_line(payload), "正在思考（第 3 轮）")
        self.assertEqual(_subagent_current_step(payload), "正在思考（第 3 轮）")

    def test_tool_requested_becomes_log_line_and_step(self) -> None:
        payload = {"type": "tool_requested", "tool": "vision_analyze", "arguments": {}, "seq": 0}
        self.assertEqual(_subagent_log_line(payload), "→ 调用工具 vision_analyze")
        self.assertEqual(_subagent_current_step(payload), "正在执行 vision_analyze")

    def test_tool_result_ok_keeps_result_out_of_log(self) -> None:
        """成功的工具结果可能含图片 base64 / 大段文本：一个字都不许进日志。"""
        huge = "data:image/jpeg;base64," + "A" * 5000
        payload = {"type": "tool_result", "tool": "read_file", "success": True, "result": huge}
        line = _subagent_log_line(payload)
        self.assertEqual(line, "✓ 工具 read_file 完毕")
        self.assertNotIn("A" * 32, line)
        self.assertEqual(_subagent_current_step(payload), "推理中")

    def test_tool_result_failure_keeps_truncated_reason(self) -> None:
        payload = {
            "type": "tool_result", "tool": "vision_analyze", "success": False,
            "result": "错误：" + "长" * 900,
        }
        line = _subagent_log_line(payload)
        self.assertTrue(line.startswith("✗ 工具 vision_analyze 失败："))
        self.assertLessEqual(len(line), _SUBAGENT_LOG_MAX_CHARS)
        self.assertTrue(line.endswith("…"), "超长失败原因必须带省略号自述")

    def test_run_failed_becomes_error_line(self) -> None:
        payload = {"type": "run_failed", "error": "工具调用格式连续三次无法自动纠正"}
        self.assertEqual(_subagent_log_line(payload), "错误：工具调用格式连续三次无法自动纠正")
        # 终态由 _finish 统一写，翻译层不动步骤（否则面板先闪一个中途文案）。
        self.assertEqual(_subagent_current_step(payload), "")

    def test_noisy_events_stay_out_of_log(self) -> None:
        for kind in (
            "reasoning", "usage", "skills", "interjection_consumed",
            "skill_warning", "tool_started", "context_full",
        ):
            with self.subTest(kind=kind):
                payload = {"type": kind, "content": "思考原文", "message": "提示", "usage": {}}
                self.assertEqual(_subagent_log_line(payload), "", f"{kind} 不应落日志")
                self.assertEqual(_subagent_current_step(payload), "", f"{kind} 不应改步骤")

    def test_single_line_flattening(self) -> None:
        """状态文案里的换行会被压平：日志一行一条，多行会被渲染成拼接文本。"""
        payload = {"type": "status", "message": "第一行\n第二行\r\n第三行"}
        self.assertEqual(_subagent_log_line(payload), "第一行 第二行 第三行")


class SubagentEventSinkTests(unittest.TestCase):
    """sink 的三件事：翻译、更新步骤、原样透传（顺序也要对）。"""

    def test_sink_emits_job_log_then_status_then_raw(self) -> None:
        registry = FakeRegistry()
        sink = _subagent_event_sink(registry, "job-1")
        sink({"type": "tool_requested", "tool": "read_file", "arguments": {}, "seq": 0})
        self.assertEqual(
            [item["type"] for item in registry.emitted],
            ["job_log", "job_status", "tool_requested"],
            "顺序必须是：先落日志行、再同步当前步骤、最后原样透传",
        )
        self.assertEqual(registry.emitted[0]["line"], "→ 调用工具 read_file")
        self.assertEqual(registry.steps, ["正在执行 read_file"])

    def test_sink_passes_reasoning_through_without_log(self) -> None:
        registry = FakeRegistry()
        sink = _subagent_event_sink(registry, "job-1")
        sink({"type": "reasoning", "content": "思考原文"})
        self.assertEqual([item["type"] for item in registry.emitted], ["reasoning"])
        self.assertEqual(registry.steps, [])

    def test_sink_ignores_non_dict(self) -> None:
        registry = FakeRegistry()
        sink = _subagent_event_sink(registry, "job-1")
        sink(None)  # type: ignore[arg-type]
        self.assertEqual(registry.emitted, [])


class SubagentJobPipelineTests(unittest.TestCase):
    """端到端：真的走 ``_run_subagent``，事件必须既进 run_events 又更新 task 行。"""

    def setUp(self) -> None:
        from naiba.storage.store import ChatStorage

        self.tmp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.conversation = self.storage.create_conversation()
        self.app = SimpleNamespace(storage=self.storage)
        self.registry = JobRegistry(self.app)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _subagent_job(self) -> str:
        run = self.storage.create_run(
            self.conversation["id"], "job", AGENT, {}, kind="subagent", parent_job_id="parent-1"
        )
        return str(run["id"])

    def _run(self, emit_sequence: list[dict]) -> tuple[str, list[dict], dict]:
        job_id = self._subagent_job()

        def agent_runner(job_id_, spec, cancel, emit) -> None:
            for payload in emit_sequence:
                emit(payload)

        self.registry.agent_runner = agent_runner
        self.registry._run_subagent(job_id, JobSpec(kind="subagent", conversation_id="c"), threading.Event())
        events = self.storage.list_run_events(job_id, 0) if hasattr(self.storage, "list_run_events") else []
        return job_id, events, self.storage.get_background_task(job_id) or {}

    def test_pipeline_produces_log_lines_and_step(self) -> None:
        job_id, events, job = self._run([
            {"type": "status", "message": "正在思考（第 1 轮）"},
            {"type": "tool_requested", "tool": "vision_analyze", "arguments": {}, "seq": 0},
            {"type": "tool_result", "tool": "vision_analyze", "success": True, "result": "ok"},
        ])
        kinds = [str(item.get("type") or "") for item in events]
        self.assertIn("job_log", kinds, "任务日志必须真的收到 job_log 行")
        self.assertIn("tool_requested", kinds, "原始事件必须原样透传")
        lines = [str(item.get("line") or "") for item in events if item.get("type") == "job_log"]
        self.assertIn("正在思考（第 1 轮）", lines)
        self.assertIn("→ 调用工具 vision_analyze", lines)
        self.assertIn("✓ 工具 vision_analyze 完毕", lines)
        self.assertEqual(job.get("status"), "completed")
        # 当前步骤必须动过（不能全程停在「已启动」）。
        self.assertIn(job.get("current_step"), {"推理中", "正在执行 vision_analyze"})

    def test_step_frozen_after_cancel(self) -> None:
        """取消闸门：停止请求之后不得再把面板回写成运行中（对齐 _set_status 语义）。"""
        job_id = self._subagent_job()
        self.storage.update_background_task(job_id, status="stopping", cancel_requested=1)
        self.registry._set_status(job_id, "running", current_step="不得写入")
        job = self.storage.get_background_task(job_id) or {}
        self.assertNotEqual(job.get("current_step"), "不得写入")

    def test_real_start_path_feeds_panel_contract(self) -> None:
        """走产品真入口（``start`` 起 worker 线程 → ``read`` 供 /api/jobs/<id>/events）。

        前端任务日志拿的就是 ``jobs.read`` 的 ``events[].line``，所以这里按**面板口径**
        断言，而不是按内部函数返回值断言。
        """
        def agent_runner(job_id_, spec, cancel, emit) -> None:
            emit({"type": "status", "message": "正在思考（第 1 轮）"})
            emit({"type": "tool_requested", "tool": "read_image", "arguments": {}, "seq": 0})
            emit({"type": "tool_result", "tool": "read_image", "success": False, "result": "文件不存在"})
            self.storage.update_job(job_id_, result={"response": "done"})

        self.registry.agent_runner = agent_runner
        job_id = self.registry.start(JobSpec(
            kind="subagent",
            conversation_id=str(self.conversation["id"]),
            params={"instruction": "审核 10 张图"},
            label="子任务：审核",
        ), owner="owner-1")
        self.assertIsNotNone(self.registry.wait(job_id, timeout=10))
        payload = self.registry.read(job_id)
        lines = [str(event.get("line") or "") for event in payload["events"]]
        self.assertEqual(
            [line for line in lines if line],
            [
                "正在思考（第 1 轮）",
                "→ 调用工具 read_image",
                "✗ 工具 read_image 失败：文件不存在",
            ],
            "任务面板该看到的过程行（顺序：思考 → 调用 → 结果）",
        )
        job = self.registry.get(job_id) or {}
        self.assertEqual(job.get("status"), "completed")
        self.assertEqual(job.get("current_step"), "推理中")


if __name__ == "__main__":
    unittest.main()
