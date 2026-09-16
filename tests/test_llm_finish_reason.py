"""终止原因采集（finish_reason）与截断判定。

为什么值得单独立守门：正文中途停住时，「模型自己收尾」与「撞输出上限被上游掐断」在界面
上完全一样。一次实测事故（272 字符正文停在「：」、前文还有一整段重复）之所以只能靠反向
推断收场，就是因为全链路没有记录任何终止原因。这里把采集口径与判定规则钉死。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.llm.protocols import ProtocolMixins  # noqa: E402
from naiba.llm.runtime import ModelRuntime  # noqa: E402
from naiba.skills.agent import _truncation_info  # noqa: E402


class FinishReasonParsingTests(unittest.TestCase):
    def test_openai_choice_finish_reason(self):
        chunk = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        self.assertEqual(ProtocolMixins._online_finish_reason("openai_chat", [chunk]), "stop")

    def test_length_synonyms_normalize_to_length(self):
        for raw in ("length", "max_tokens", "max_output_tokens", "token_limit", "incomplete"):
            with self.subTest(raw=raw):
                chunk = {"choices": [{"finish_reason": raw}]}
                self.assertEqual(ProtocolMixins._online_finish_reason("openai_chat", [chunk]), "length")

    def test_ollama_done_reason(self):
        chunk = {"done": True, "done_reason": "length"}
        self.assertEqual(ProtocolMixins._online_finish_reason("ollama", [chunk]), "length")

    def test_codex_response_status_from_nested_holder(self):
        chunk = {"type": "response.incomplete", "response": {"status": "incomplete"}}
        self.assertEqual(ProtocolMixins._online_finish_reason("codex_responses", [chunk]), "length")

    def test_missing_reason_returns_empty(self):
        chunk = {"choices": [{"delta": {}}]}
        self.assertEqual(ProtocolMixins._online_finish_reason("openai_chat", [chunk]), "")

    def test_last_non_empty_value_wins(self):
        chunks = [
            {"choices": [{"finish_reason": "length"}]},
            {"choices": [{"finish_reason": "stop"}]},
        ]
        self.assertEqual(ProtocolMixins._online_finish_reason("openai_chat", chunks), "stop")


class LastFinishReasonPropertyTests(unittest.TestCase):
    def test_defaults_to_empty(self):
        self.assertEqual(ModelRuntime().last_finish_reason, "")

    def test_roundtrip(self):
        runtime = ModelRuntime()
        runtime.last_finish_reason = "length"
        self.assertEqual(runtime.last_finish_reason, "length")

    def test_thread_local_is_isolated(self):
        runtime = ModelRuntime()
        runtime.last_finish_reason = "length"
        seen: list[str] = []

        def worker() -> None:
            seen.append(runtime.last_finish_reason)
            runtime.last_finish_reason = "stop"
            seen.append(runtime.last_finish_reason)

        import threading

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(seen, ["", "stop"])
        self.assertEqual(runtime.last_finish_reason, "length")


class TruncationVerdictTests(unittest.TestCase):
    def test_length_marks_truncated(self):
        info = _truncation_info("length", "正文被切")
        self.assertTrue(info["truncated"])
        self.assertEqual(info["finish_reason"], "length")
        self.assertFalse(info["continued"])

    def test_missing_reason_with_unfinished_tail(self):
        # 实测事故口径：272 字符正文停在「：」，且供应商没给任何终止原因。
        info = _truncation_info("", "先让子 Agent 生成修正后的 6 份工作流：")
        self.assertTrue(info["truncated"])
        self.assertTrue(info["unfinished_tail"])

    def test_stop_with_colon_is_not_treated_as_truncated(self):
        # 供应商明确回报 stop 时，冒号收尾是模型自己的选择，不替它续写。
        self.assertFalse(_truncation_info("stop", "如下：")["truncated"])

    def test_stop_with_length_wording_still_truncated(self):
        self.assertTrue(_truncation_info("max_output_tokens", "被切")["truncated"])

    def test_finished_sentence_is_not_truncated(self):
        self.assertFalse(_truncation_info("", "任务已经完成，结果写在工作区里。")["truncated"])

    def test_empty_content_is_not_truncated(self):
        self.assertFalse(_truncation_info("", "")["truncated"])


if __name__ == "__main__":
    unittest.main()
