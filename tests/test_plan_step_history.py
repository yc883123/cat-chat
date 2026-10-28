# -*- coding: utf-8 -*-
"""守门：计划执行的历史构建按「本次实际可用工具名集合」收窄 PDF/视频处理指引。

背景（2026-10-28）：``_run_step_with_agent`` 原先先 ``build_model_history`` 再定稿
``allowed_tools``，指引只能按未过滤的默认全集（``pdf_tools=True``）展开——同一会话里
主对话按固化工具集收窄、计划执行却全量展开，回放字节两种形态（前缀缓存断）。

三个活调用点（对话 / 子代理 / 计划执行）必须传同一组值（``core.history`` docstring）：
本文件管计划执行这一处——工具集先定稿、再构建历史；对话与子代理的口径分别由
``tests/test_history_build.py`` / ``tests/test_subagent_fork.py`` 守门。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core.history import build_model_history  # noqa: E402
from naiba.plans import PlanManager  # noqa: E402


class _FakeStorage:
    def __init__(self, conversation: dict) -> None:
        self.conversation = conversation
        self.messages: list[tuple] = []

    def get_conversation(self, conversation_id: str):
        if conversation_id != self.conversation["id"]:
            return None
        return self.conversation

    def add_message(self, conversation_id: str, role: str, content: str, metadata: dict) -> None:
        self.messages.append((conversation_id, role, content, metadata))


class _FakeConfig:
    def __init__(self, agent_tools: list[str]) -> None:
        self.data = {"agent_system_prompt": "全局兜底人设", "agent_tools": agent_tools}

    def reasoning_replay_options(self) -> dict:
        return {}

    def image_encode_cache_options(self) -> dict:
        return {}

    def profile(self, model_key: str) -> dict:
        return {"id": model_key or "test-model", "context_window": 128000}

    def generation_options(self, model_key: str | None = None) -> dict:
        return {}

    def get_agent(self, agent_id: str) -> dict:
        return {"id": agent_id, "system_prompt": "Agent 人设"}


def _make_app(conversation: dict, agent_tools: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        storage=_FakeStorage(conversation),
        config=_FakeConfig(agent_tools),
        executor=SimpleNamespace(),
        catalog=SimpleNamespace(),
        models=SimpleNamespace(complete=lambda *a, **k: None),
        tool_registry=None,
        jobs=None,
    )


def _conversation(messages: list[dict]) -> dict:
    return {
        "id": "conv-1",
        "messages": messages,
        "model_key": "online:test",
        "provider_id": "",
        "stream_enabled": 0,
        "agent_id": "agent-1",
        "workspace_dir": "",
        "permission_mode": "auto",
    }


class PlanStepHistoryToolSwitchTests(unittest.TestCase):
    """计划执行：工具集先定稿再构建历史，指引按定稿集合展开。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = _make_app(
            _conversation([
                {"role": "user", "content": "把之前整理的资料出个分镜"},
                {"role": "assistant", "content": "好的"},
            ]),
            agent_tools=["read_pdf", "extract_frames", "read_file"],
        )
        self.plan = {
            "id": "plan-1",
            "conversation_id": "conv-1",
            "title": "出分镜",
            "content": "1. 出图",
            "steps": [{"id": 1, "title": "第一步", "detail": "做点事", "status": "todo"}],
        }
        self.step = self.plan["steps"][0]

    def _run(self, snapshot: dict | None) -> tuple[dict, dict]:
        """跑一次步骤执行器，返回 build_model_history 的 kwargs 与 SkillAgent.run 参数。"""
        captured_kwargs: dict = {}

        real_build = build_model_history

        def spy_build(messages, **kwargs):
            captured_kwargs.update(kwargs)
            return real_build(messages, **kwargs)

        with mock.patch("naiba.plans.SkillAgent") as agent_cls, \
                mock.patch("naiba.plans.build_model_history", side_effect=spy_build):
            agent_cls.return_value.run.return_value = ("步骤完成", [], [], {})
            PlanManager(self.app)._run_step_with_agent(
                self.plan, self.step, 0, 1,
                cancel_event=None, event=lambda payload: None, snapshot=snapshot,
            )
            run_args = agent_cls.return_value.run.call_args
        return captured_kwargs, run_args

    def test_frozen_tools_settle_before_history(self) -> None:
        """快照里的定稿集合决定指引：read_pdf 在、extract_frames 不在 → 只开 PDF。"""
        kwargs, run_args = self._run({"allowed_tools": ["read_pdf", "read_file", "show_media"]})
        self.assertTrue(kwargs["pdf_tools"])
        self.assertFalse(kwargs["video_tools"])
        # 指名出卡照旧显式剔除：快照带着也不进模型可见工具集。
        self.assertEqual(run_args.args[7], ["read_pdf", "read_file"])

    def test_video_tool_only_settles_video_switch(self) -> None:
        kwargs, _ = self._run({"allowed_tools": ["extract_frames", "read_file"]})
        self.assertFalse(kwargs["pdf_tools"])
        self.assertTrue(kwargs["video_tools"])

    def test_fallback_resolves_from_configured_tools(self) -> None:
        """旧记录没有快照：退回按模式解析全局 agent_tools，开关跟**解析后**的集合走。

        全局配置里带了 read_pdf / extract_frames，但 craft 解析器只放行旧版白名单——
        指引必须与定稿集合同口径（False），不得直接读全局配置或按默认全集展开。
        """
        kwargs, run_args = self._run({"allowed_tools": None})
        self.assertFalse(kwargs["pdf_tools"])
        self.assertFalse(kwargs["video_tools"])
        self.assertIn("read_file", run_args.args[7])
        self.assertNotIn("read_pdf", run_args.args[7])
        self.assertNotIn("show_media", run_args.args[7])

    def test_history_replays_conversation(self) -> None:
        kwargs, run_args = self._run({"allowed_tools": ["read_file"]})
        history = run_args.args[1]
        self.assertEqual([item["content"] for item in history], [
            "把之前整理的资料出个分镜",
            "好的",
        ])


if __name__ == "__main__":
    unittest.main()
