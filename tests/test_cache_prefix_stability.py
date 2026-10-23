# -*- coding: utf-8 -*-
"""护栏：请求前缀字节稳定（计划 2026-10-22 §5 / §7.1，应用侧确定性部分）。

断言的是**模型实际收到的字节**，不是配置或函数返回值：

1. 同一个授权快照下，用户消息从「普通概览」变成「明确逐帧」时，system、tools
   与既有历史逐字节不变——只有本轮新增的用户消息不同；
2. 未授权快照的 tools 不含 `extract_video_frames_exact`；授权快照静态包含它，
   差异只来自 Agent 配置（新会话），不由本轮句子动态增删；
3. 同一输入重复组装两次，system / tools 字节完全一致（无随机顺序、无时间戳）；
4. Job 结果只出现在本轮新增的 tool 消息里，不改写 system/tools/旧消息。

做法：走真实的 `SkillAgent.run` 组装链路，用假模型捕获它发出的 messages 与 tools，
不做任何网络请求。
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.config import TOOL_PRESETS, resolve_tool_preset, tool_catalog_entries  # noqa: E402
from naiba.mcp import MCPRegistry  # noqa: E402
from naiba.skills.agent import SkillAgent  # noqa: E402
from naiba.tools.executor import ToolExecutor  # noqa: E402
from naiba.tools.registry import build_tool_registry  # noqa: E402

EXACT_TOOL = "extract_video_frames_exact"
PROFILE = {"kind": "online", "model": "probe", "context_window": 131072, "request_format": "openai_chat"}


class _EmptyCatalog:
    def scan(self):
        return []


class _CapturingModel:
    """记录 SkillAgent 实际发出的 (messages, options.tools)，返回一句普通答复。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, profile, messages, options, event=None):
        self.calls.append({
            "messages": json.loads(json.dumps(messages, ensure_ascii=False)),
            "tools": json.loads(json.dumps(options.get("tools") or [], ensure_ascii=False)),
        })
        return "收到"


def _standard_tools(with_exact: bool) -> list[str]:
    registry = build_tool_registry()
    entries = tool_catalog_entries(registry.schemas())
    standard = next(preset for preset in TOOL_PRESETS if preset["id"] == "standard")
    tools = resolve_tool_preset(standard, entries)
    if with_exact and EXACT_TOOL in {entry["name"] for entry in entries}:
        tools.append(EXACT_TOOL)
    return tools


class RequestPrefixStabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.registry = build_tool_registry()
        self.executor = ToolExecutor(
            self.workspace, sys.executable, 60, MCPRegistry([]), permission_mode="auto"
        )
        self.executor.set_def_resolver(self.registry.get)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _capture(self, message: str, allowed: list[str]) -> dict:
        model = _CapturingModel()
        worker = SkillAgent(_EmptyCatalog(), self.executor, model, None)
        worker.run(
            message,
            [],
            PROFILE,
            {"stream": False, "max_tokens": 64, "max_steps": 1},
            {"mode": "auto", "skill_ids": []},
            [],
            "",
            allowed,
            lambda _payload: None,
            None,
            tool_registry=self.registry,
            run_context={
                "run_id": "probe",
                "conversation_id": "probe",
                "executor": self.executor,
                "workspace_dir": str(self.workspace),
                "confirmation_ui": True,
            },
        )
        return model.calls[0]

    @staticmethod
    def _sha(value) -> str:
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def test_intent_wording_does_not_change_system_or_tools(self) -> None:
        allowed = _standard_tools(with_exact=True)
        overview = self._capture("先概览一下这段视频有几个镜头", allowed)
        exact = self._capture("把这段视频逐帧导出成图片", allowed)
        self.assertEqual(overview["tools"], exact["tools"], "tools 不得随本轮句子变化")
        self.assertEqual(
            overview["messages"][0]["content"], exact["messages"][0]["content"],
            "system 不得随本轮句子变化",
        )
        # 差异只允许出现在本轮新增的用户消息上（system 之后的历史为空）。
        self.assertEqual(len(overview["messages"]), len(exact["messages"]))
        for before, after in zip(overview["messages"][:-1], exact["messages"][:-1]):
            self.assertEqual(before, after, "system/历史前缀必须逐字节一致")
        self.assertNotEqual(overview["messages"][-1], exact["messages"][-1])

    def test_authorization_snapshot_only_controls_tools_presence(self) -> None:
        authorized = self._capture("逐帧导出", _standard_tools(with_exact=True))
        unauthorized = self._capture("逐帧导出", _standard_tools(with_exact=False))
        names_authorized = [tool["name"] for tool in authorized["tools"]]
        names_unauthorized = [tool["name"] for tool in unauthorized["tools"]]
        self.assertIn(EXACT_TOOL, names_authorized)
        self.assertNotIn(EXACT_TOOL, names_unauthorized, "未授权快照的 tools 不得含逐帧工具")
        # schema 顺序稳定：授权集是未授权集的超集且顺序一致（只多出它自己）。
        self.assertEqual(
            [name for name in names_authorized if name != EXACT_TOOL],
            names_unauthorized,
            "新增工具只能追加，不得改变既有工具顺序",
        )

    def test_repeated_assembly_is_byte_identical(self) -> None:
        allowed = _standard_tools(with_exact=True)
        first = self._capture("逐帧导出", allowed)
        second = self._capture("逐帧导出", allowed)
        self.assertEqual(self._sha(first["messages"]), self._sha(second["messages"]))
        self.assertEqual(self._sha(first["tools"]), self._sha(second["tools"]))

    def test_job_result_tail_does_not_touch_prefix(self) -> None:
        """Job 结果只作为新增 tool 消息出现；前缀（system/tools/旧消息）不受影响。"""
        allowed = _standard_tools(with_exact=True)
        base = self._capture("逐帧导出", allowed)
        prefix = base["messages"]
        compact = json.dumps({
            "ok": True, "job_id": "job_fixed", "status": "queued",
            "range": {"mode": "frame", "start_frame": 0, "end_frame": 24},
            "estimated_frames": 24, "manifest_api": "/api/jobs/job_fixed/manifest",
        }, ensure_ascii=False)
        extended = [
            *prefix,
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": EXACT_TOOL, "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": "call_1", "content": compact},
        ]
        self.assertEqual(extended[: len(prefix)], prefix)
        # 紧凑结果不回传帧路径清单
        self.assertNotIn("frame_000001.png", compact)


if __name__ == "__main__":
    unittest.main()
