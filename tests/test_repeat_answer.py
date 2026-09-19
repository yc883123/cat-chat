# -*- coding: utf-8 -*-
"""守门：重复答复检测（只提示、不拦截、不落 metadata）。

病历：失控思考被全量回灌后模型会被锚定在同一个循环里——用户最早看到的现象是
「MiMo 会话里 AI 的总结**每次都是同一条文字**」。Agent 循环本来有「同一工具连续失败/
无进展」的熔断，但最终答复与上一轮逐字相同此前没有任何感知。

三条纪律：
1. **只提示**（可能是用户真的要求复述），绝不拦截、不自动开新会话；
2. **不落 metadata**（改动消息契约 → 影响 build_model_history 的字节稳定 → 破前缀缓存）；
3. 规范化后**逐字相同**且长度 ≥ 20 字符才算命中（短应答如「好的」重复属正常对话）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.skills import agent as agent_module  # noqa: E402
from naiba.skills.agent import SkillAgent  # noqa: E402

LONG_ANSWER = "这一轮的结论是：需要把三张素材图按 9:16 重排后再生成分镜提示词。"


class _Catalog:
    def scan(self) -> list:
        return []


class RepeatAnswerHelperTests(unittest.TestCase):
    def test_normalization_folds_whitespace(self) -> None:
        normalize = agent_module._normalize_answer_text
        self.assertEqual(normalize("  第一行\n\n第二行  "), "第一行 第二行")
        self.assertEqual(normalize(None), "")
        self.assertEqual(normalize(["不是字符串"]), "['不是字符串']")

    def test_last_answer_skips_tool_call_turns(self) -> None:
        messages = [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": LONG_ANSWER},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "content": "工具结果"},
        ]
        self.assertEqual(agent_module._last_answer_before(messages), LONG_ANSWER)

    def test_repeat_is_detected(self) -> None:
        messages = [{"role": "assistant", "content": LONG_ANSWER}]
        self.assertTrue(agent_module._repeat_answer_notice(messages, LONG_ANSWER))
        self.assertTrue(agent_module._repeat_answer_notice(messages, f"  {LONG_ANSWER}\n"))

    def test_different_answer_is_not_a_repeat(self) -> None:
        messages = [{"role": "assistant", "content": LONG_ANSWER}]
        self.assertEqual(
            agent_module._repeat_answer_notice(messages, LONG_ANSWER + "（补充一句）"), ""
        )

    def test_short_answer_is_never_flagged(self) -> None:
        """短应答（「好的」「已完成」）重复属于正常对话，不得误伤。"""
        for short in ("好的", "已完成。", "ok"):
            self.assertEqual(
                agent_module._repeat_answer_notice(
                    [{"role": "assistant", "content": short}], short
                ),
                "",
            )

    def test_no_history_is_not_a_repeat(self) -> None:
        self.assertEqual(agent_module._repeat_answer_notice([], LONG_ANSWER), "")


class RepeatAnswerRunTests(unittest.TestCase):
    """端到端：答复照常返回，只多一条 status 事件。"""

    def _run(self, history: list[dict], answer: str) -> tuple[str, list[dict]]:
        events: list[dict] = []

        def complete(profile, messages, options, event):
            return answer

        worker = SkillAgent(_Catalog(), None, complete, None)
        response, _runs, _reasonings, _usage = worker.run(
            "再来一次",
            history,
            {"kind": "online", "model": "m", "context_window": 128000},
            {"max_steps": 1, "stream": False},
            {"mode": "auto", "skill_ids": []},
            [],
            "",
            [],
            lambda payload: events.append(payload),
            None,
        )
        return response, events

    def test_repeat_answer_is_reported_but_still_returned(self) -> None:
        response, events = self._run(
            [
                {"role": "user", "content": "帮我总结一下"},
                {"role": "assistant", "content": LONG_ANSWER},
            ],
            LONG_ANSWER,
        )
        self.assertEqual(response, LONG_ANSWER, "只提示，不得拦截或改写答复")
        notices = [
            str(item.get("message") or "")
            for item in events
            if item.get("type") == "status" and "上一轮完全相同" in str(item.get("message") or "")
        ]
        self.assertTrue(notices, f"必须发出一条重复提示，实际事件：{events}")
        self.assertIn("新会话", notices[0], "提示要给出可操作建议")

    def test_fresh_answer_has_no_notice(self) -> None:
        response, events = self._run(
            [
                {"role": "user", "content": "帮我总结一下"},
                {"role": "assistant", "content": LONG_ANSWER},
            ],
            "换一个角度：这次改为先排图再写分镜提示词，顺序不同效果也不同。",
        )
        self.assertNotIn("上一轮完全相同", response)
        self.assertFalse(
            [item for item in events if "上一轮完全相同" in str(item.get("message") or "")],
            "不同答复不得提示",
        )

    def test_empty_history_has_no_notice(self) -> None:
        _response, events = self._run([], LONG_ANSWER)
        self.assertFalse(
            [item for item in events if "上一轮完全相同" in str(item.get("message") or "")],
        )


if __name__ == "__main__":
    unittest.main()
