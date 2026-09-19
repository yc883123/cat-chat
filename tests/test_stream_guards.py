# -*- coding: utf-8 -*-
"""守门：思考预算熔断（§A）/ LM Studio 空流降档（§C）/ 单请求总时长兜底（§D）。

三条都源自同一份病历：**长思考 + 长对话**下，模型陷入推理循环时界面上没有任何信号。
- ``ONLINE_MODEL_TIMEOUT_SECONDS=180`` 是 urllib **每次读操作的空闲超时**，推理 delta
  持续到达就不断重置它 ⇒ 一段 43 万字符的思考循环可以无限流下去（§九.102 的 10 万字符
  事故就是它的现实形态，且思考 token 按输出计费）。
- LM Studio 的「有推理零正文」直接 ``RuntimeError``，不走 ``EmptyModelStreamError``、
  没有降档重试，与在线路径不对齐。
- 在线没有总时长兜底，「合法长思考」与「死循环」在界面上无法区分。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.llm.runtime import (  # noqa: E402
    REASONING_STREAM_BREAK_CHARS,
    EmptyModelStreamError,
    ModelRuntime,
    StreamTotalTimeout,
)

MESSAGES = [{"role": "user", "content": "ping"}]
# 在线 openai_chat + GPT：思考强度在线可控，便于断言降档落在 wire 上。
PROFILE_GPT = {
    "kind": "online",
    "name": "gpt",
    "base_url": "https://api.openai.com",
    "model": "gpt-5",
    "request_format": "openai_chat",
    "api_key": "sk-test",
}
# 本地 LM Studio：非 tools 路径走原生 payload（reasoning 字段）。
PROFILE_LM_STUDIO = {
    "kind": "local",
    "name": "lmstudio",
    "base_url": "http://127.0.0.1:1234",
    "model": "qwen3-30b",
    "request_format": "lm_studio",
    "reasoning_effort": "high",
}
# 在线 codex + DeepSeek（思考强度映射 high→max / medium→high）。
PROFILE_DEEPSEEK_CODEX = {
    "kind": "online",
    "name": "deepseek",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-v4.1-flash",
    "request_format": "codex_responses",
    "reasoning_effort": "high",
    "api_key": "sk-test",
}


def sse(chunk: dict) -> bytes:
    return ("data: " + json.dumps(chunk, ensure_ascii=False)).encode("utf-8")


def reasoning_chunk(text: str) -> bytes:
    return sse({"choices": [{"delta": {"reasoning_content": text}}]})


def text_chunk(text: str) -> bytes:
    return sse({"choices": [{"delta": {"content": text}}]})


class TrackedStreamResponse:
    """可迭代 SSE 响应替身：记录**已被消费的行数**，用来证明「提前中断」真的提前了。"""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines
        self.consumed = 0
        self.closed = False
        self.headers = {"Content-Type": "text/event-stream"}

    def __iter__(self):
        for line in self._lines:
            if self.closed:
                raise ValueError("I/O operation on closed file")
            self.consumed += 1
            yield line

    def read(self) -> bytes:
        return b""

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "TrackedStreamResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class SlowStreamResponse(TrackedStreamResponse):
    """每行之间真实耗时（自旋等待，避免与运行时被 mock 的 time.sleep 相互干扰）。"""

    def __init__(self, lines: list[bytes], per_line: float) -> None:
        super().__init__(lines)
        self.per_line = float(per_line)

    def __iter__(self):
        for line in self._lines:
            deadline = time.perf_counter() + self.per_line
            while time.perf_counter() < deadline:
                if self.closed:
                    raise ValueError("I/O operation on closed file")
            self.consumed += 1
            yield line


class FlatResponse:
    def __init__(self, payload: dict) -> None:
        self._data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.headers = {"Content-Type": "application/json"}

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        return None

    def __enter__(self) -> "FlatResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def http_error(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.openai.com/v1/chat/completions", code, "Bad Request",
        {"Content-Type": "application/json"}, __import__("io").BytesIO(body.encode("utf-8")),
    )


class _Harness(unittest.TestCase):
    """按调用顺序回放 outcome：异常→抛出，list[bytes]→流式响应，dict→非流式响应。"""

    def _run(
        self,
        outcomes: list,
        options: dict | None = None,
        profile: dict | None = None,
        status=None,
    ) -> tuple[list, list, str, BaseException | None]:
        requests: list = []
        responses: list = []
        dump_dir = tempfile.mkdtemp(prefix="naiba-stream-guard-")
        self.addCleanup(shutil.rmtree, dump_dir, ignore_errors=True)

        def fake_open(request, timeout, cancel_event=None, opener=None):
            requests.append(request)
            outcome = outcomes[min(len(requests) - 1, len(outcomes) - 1)]
            if isinstance(outcome, BaseException):
                raise outcome
            if isinstance(outcome, list):
                response = TrackedStreamResponse(outcome)
            elif callable(outcome):
                response = outcome()
            else:
                response = FlatResponse(outcome)
            responses.append(response)
            return response

        with mock.patch.object(ModelRuntime, "_urlopen_cancelable", fake_open), \
                mock.patch("naiba.llm.runtime.time.sleep"), \
                mock.patch.dict(os.environ, {"NAIBA_ERROR_DUMP_DIR": dump_dir}, clear=False):
            try:
                content = ModelRuntime().complete(
                    profile or PROFILE_GPT,
                    MESSAGES,
                    options if options is not None else {"stream": True},
                    status,
                )
                error = None
            except RuntimeError as exc:
                content, error = "", exc
        return requests, responses, content, error

    @staticmethod
    def _payload(request) -> dict:
        return json.loads(request.data.decode("utf-8"))


class ReasoningBreakTests(_Harness):
    """§A：思考预算熔断（只在**正文仍为空**时生效）。"""

    def test_runaway_reasoning_is_cut_early_and_retried_lowered(self) -> None:
        long_run = [reasoning_chunk("思" * 200) for _ in range(500)]  # 10 万字符
        requests, responses, content, error = self._run(
            [long_run, [text_chunk("答复")]],
            options={"stream": True},
            profile={**PROFILE_GPT, "reasoning_effort": "high"},
        )
        self.assertIsNone(error)
        self.assertEqual(content, "答复")
        self.assertEqual(len(requests), 2)
        consumed = responses[0].consumed
        self.assertLess(
            consumed, 400,
            f"必须在超预算时提前中断，而不是把整个推理循环读完（实际读了 {consumed} 行）",
        )
        efforts = [self._payload(item).get("reasoning_effort") for item in requests]
        self.assertEqual(efforts, ["high", "medium"], "熔断重试必须按词表降一档")

    def test_reasoning_over_budget_with_content_is_not_cut(self) -> None:
        """正文一旦出现即解除熔断：思考 + 正文正常输出是合法形态，绝不误伤。"""
        stream = [reasoning_chunk("思" * 200) for _ in range(400)]
        stream.insert(10, text_chunk("开头就出正文"))  # 早期就有正文，之后推理再多也不熔断
        requests, responses, content, error = self._run(
            [stream], options={"stream": True},
            profile={**PROFILE_GPT, "reasoning_effort": "high"},
        )
        self.assertIsNone(error)
        self.assertEqual(len(requests), 1, "正文已出现 ⇒ 不得重试")
        self.assertEqual(responses[0].consumed, len(stream), "整条流必须被读完")
        self.assertIn("开头就出正文", content)

    def test_zero_disables_the_breaker(self) -> None:
        """``reasoning_break_chars=0`` ⇒ 行为与改动前完全一致（不中断）。"""
        stream = [reasoning_chunk("思" * 200) for _ in range(400)]
        requests, responses, _content, error = self._run(
            [stream], options={"stream": True, "reasoning_stream_break_chars": 0},
            profile={**PROFILE_GPT, "reasoning_effort": "high"},
        )
        self.assertEqual(responses[0].consumed, len(stream), "关掉熔断后必须读完")
        self.assertIn("降低思考强度", str(error), "仍走既有的空流降档重试语义")
        self.assertIn("没有文本内容", str(error))

    def test_default_threshold_matches_documented_budget(self) -> None:
        self.assertEqual(REASONING_STREAM_BREAK_CHARS, 48000)

    def test_override_option_name_is_the_documented_one(self) -> None:
        """覆盖项必须叫 ``reasoning_stream_break_chars``（写成别的名字会静默失效）。"""
        stream = [reasoning_chunk("思" * 200) for _ in range(500)]
        requests, responses, _content, _error = self._run(
            [stream, [text_chunk("答复")]],
            options={"stream": True, "reasoning_stream_break_chars": 2000},
            profile={**PROFILE_GPT, "reasoning_effort": "high"},
        )
        self.assertLess(responses[0].consumed, 30, "更小的阈值必须更早中断")
        self.assertEqual(len(requests), 2)

    def test_break_reports_progress_to_user(self) -> None:
        events: list[dict] = []
        long_run = [reasoning_chunk("思" * 200) for _ in range(500)]
        self._run(
            [long_run, [text_chunk("答复")]],
            options={"stream": True}, profile={**PROFILE_GPT, "reasoning_effort": "high"},
            status=events.append,
        )
        notes = [str(e.get("message") or "") for e in events if e.get("type") == "status"]
        self.assertTrue(any("思考异常冗长" in note for note in notes), f"必须告知用户，实际：{notes}")


class LmStudioEmptyStreamTests(_Harness):
    """§C：LM Studio「有推理零正文」必须与在线路径对齐（抛空流异常 → 降档重试）。"""

    @staticmethod
    def _lm_event(kind: str, content: str) -> bytes:
        return sse({"type": kind, "content": content})

    def test_reasoning_only_stream_is_retried_with_lowered_effort(self) -> None:
        first = [
            self._lm_event("reasoning.delta", "先核对一遍参数…"),
            self._lm_event("chat.end", ""),
        ]
        second = [self._lm_event("message.delta", "答复"), self._lm_event("chat.end", "")]
        requests, _responses, content, error = self._run(
            [first, second], options={"stream": True}, profile=PROFILE_LM_STUDIO,
        )
        self.assertIsNone(error, f"LM Studio 的有推理零正文必须可自愈，实际：{error}")
        self.assertEqual(content, "答复")
        self.assertEqual(len(requests), 2)
        efforts = [self._payload(item).get("reasoning") for item in requests]
        self.assertEqual(efforts, ["high", "medium"], "重试必须降档（lm_studio 方言的 reasoning 字段）")

    def test_pure_empty_stream_keeps_original_error(self) -> None:
        """完全无内容（无推理）仍保留原 RuntimeError，不进入降档链。"""
        calls, _responses, _content, error = self._run(
            [[self._lm_event("chat.end", "")]], options={"stream": True}, profile=PROFILE_LM_STUDIO,
        )
        self.assertIsNotNone(error)
        self.assertNotIsInstance(error, EmptyModelStreamError)
        self.assertEqual(len(calls), 1)

    def test_exhausted_retries_report_diagnosis(self) -> None:
        first = [
            self._lm_event("reasoning.delta", "先核对一遍参数…"),
            self._lm_event("chat.end", ""),
        ]
        calls, _responses, _content, error = self._run(
            [first], options={"stream": True}, profile=PROFILE_LM_STUDIO,
        )
        self.assertIsNotNone(error)
        self.assertIn("没有文本内容", str(error))
        self.assertIn("降低思考强度", str(error), "穷尽后必须给出可操作建议")
        self.assertEqual(len(calls), 2, "LM Studio 至少要有一次降档重试")

    def test_breaker_partial_reasoning_is_not_a_failure(self) -> None:
        """熔断后若「关掉思考重试」仍无正文，才报错；有正文则正常返回。"""
        stream = [self._lm_event("reasoning.delta", "思" * 500) for _ in range(200)]
        second = [self._lm_event("message.delta", "答复"), self._lm_event("chat.end", "")]
        requests, _responses, content, error = self._run(
            [stream, second], options={"stream": True}, profile=PROFILE_LM_STUDIO,
        )
        self.assertIsNone(error)
        self.assertEqual(content, "答复")
        self.assertEqual(self._payload(requests[1]).get("reasoning"), "medium")


class StreamTotalTimeoutTests(_Harness):
    """§D：在线单请求墙钟总时长（900 秒兜底；合法长思考与死循环必须分得开）。"""

    @staticmethod
    def _slow(lines: list[bytes], per_line: float):
        return lambda: SlowStreamResponse(lines, per_line)

    def test_slow_stream_is_cut_and_retried(self) -> None:
        slow = [reasoning_chunk("思" * 50) for _ in range(20)]  # 20 × 0.03s = 0.6s
        requests, responses, _content, error = self._run(
            [self._slow(slow, 0.03), [text_chunk("答复")]],
            options={"stream": True, "stream_total_timeout_seconds": 0.15},
            profile={**PROFILE_GPT, "reasoning_effort": "high"},
        )
        self.assertLess(
            responses[0].consumed, len(slow),
            "总时长到点必须中断，而不是把慢流读完",
        )
        self.assertIsNone(error)
        self.assertEqual(len(requests), 2, "超时后必须重试")
        # 全程只有推理、零正文 ⇒ 与空流同病：重试时降档打破循环。
        efforts = [self._payload(item).get("reasoning_effort") for item in requests]
        self.assertEqual(efforts, ["high", "medium"], "推理型超时必须降档重试")

    def test_fast_stream_is_unaffected(self) -> None:
        requests, _responses, content, error = self._run(
            [[text_chunk("答"), text_chunk("复")]],
            options={"stream": True, "stream_total_timeout_seconds": 5},
            profile=PROFILE_GPT,
        )
        self.assertIsNone(error)
        self.assertEqual(content, "答复")
        self.assertEqual(len(requests), 1)

    def test_zero_disables_total_timeout(self) -> None:
        slow = [text_chunk("答") for _ in range(6)]
        requests, responses, content, error = self._run(
            [self._slow(slow, 0.03)],
            options={"stream": True, "stream_total_timeout_seconds": 0},
            profile=PROFILE_GPT,
        )
        self.assertIsNone(error)
        self.assertEqual(content, "答答答答答答")
        self.assertEqual(responses[0].consumed, len(slow))
        self.assertEqual(len(requests), 1)

    def test_exhausted_timeout_reports_actionable_error(self) -> None:
        slow = [reasoning_chunk("思" * 50) for _ in range(20)]
        requests, _responses, _content, error = self._run(
            [self._slow(slow, 0.03)],
            options={"stream": True, "stream_total_timeout_seconds": 0.15},
            profile={**PROFILE_GPT, "reasoning_effort": "high"},
        )
        self.assertIsNotNone(error)
        self.assertIn("单次请求超过", str(error))
        self.assertIn("思考", str(error), "必须给出可操作建议")
        self.assertEqual(len(requests), 4, "注入启用时 attempts = 3 + 1")

    def test_timeout_error_type_is_distinct(self) -> None:
        """总时长超时是**独立异常类型**，不能与首字节超时混为一谈。"""
        slow = [text_chunk("答") for _ in range(20)]
        events: list[dict] = []
        self._run(
            [self._slow(slow, 0.03)],
            options={"stream": True, "stream_total_timeout_seconds": 0.15},
            profile=PROFILE_GPT, status=events.append,
        )
        notes = [str(e.get("message") or "") for e in events if e.get("type") == "status"]
        self.assertTrue(any("已停止等待" in note for note in notes), f"必须告知用户，实际：{notes}")
        self.assertTrue(issubclass(StreamTotalTimeout, RuntimeError))

    def test_deepseek_codex_timeout_lowers_by_declared_order(self) -> None:
        slow = [sse({"type": "response.reasoning_summary_text.delta", "delta": "想" * 50}) for _ in range(20)]
        requests, _responses, _content, _error = self._run(
            [self._slow(slow, 0.03)],
            options={"stream": True, "stream_total_timeout_seconds": 0.15},
            profile=PROFILE_DEEPSEEK_CODEX,
        )
        efforts = [self._payload(item).get("reasoning") for item in requests]
        self.assertEqual(
            efforts[:2], [{"effort": "max"}, {"effort": "high"}],
            "DeepSeek codex 的降档必须走自己的词表（high→max、medium→high）",
        )


class EmptyStreamSourceLabelTests(unittest.TestCase):
    """空流报错必须**自报来源**：本地后端不许自称「在线模型」（真实复测发现）。"""

    def test_labels_follow_the_profile(self) -> None:
        from naiba.llm.runtime import _empty_stream_label, _empty_stream_phrase

        self.assertEqual(_empty_stream_label("lm_studio", True), "LM Studio")
        self.assertEqual(_empty_stream_label("ollama", True), "Ollama")
        self.assertEqual(_empty_stream_label("openai_chat", True), "本地模型")
        self.assertEqual(_empty_stream_label("llama_cpp", True), "本地模型")
        self.assertEqual(_empty_stream_label("openai_chat", False), "在线模型")
        self.assertEqual(_empty_stream_label("codex_responses", False), "在线模型")
        # Latin 标签后留空格，中文标签不留（中文里插空格会显得别扭）
        self.assertEqual(_empty_stream_phrase("lm_studio", True), "LM Studio 流式响应中没有文本内容")
        self.assertEqual(_empty_stream_phrase("openai_chat", False), "在线模型流式响应中没有文本内容")

    def test_source_module_does_not_hardcode_online_wording(self) -> None:
        """源码里不得再出现写死的「在线模型流式响应…」——必须走来源标签。

        负向断言先剔掉整行注释（§九.96 的教训），并且只认**带 ASCII 引号的字符串字面量**：
        函数 docstring 里的「在线模型流式响应中没有文本内容」用「」引用，不会误伤。
        """
        source = (Path(__file__).resolve().parents[1] / "naiba" / "llm" / "runtime.py").read_text(
            encoding="utf-8"
        )
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        self.assertNotIn('"在线模型流式响应中没有文本内容"', code)


if __name__ == "__main__":
    unittest.main()
