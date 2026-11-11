"""在线实际输出空闲闸门：协议噪声不续命，正常长思考与终态不误伤。"""

from __future__ import annotations

import json
import threading
import time
import unittest
from unittest import mock

from naiba.llm import runtime


def sse(chunk):
    return ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode()


def delta(**values):
    return sse({"choices": [{"delta": values}]})


class ScheduledResponse:
    def __init__(self, lines, delay=0.0):
        self.lines = lines
        self.delay = delay
        self.closed = threading.Event()
        self.consumed = 0

    def __iter__(self):
        for line in self.lines:
            if self.closed.wait(self.delay):
                raise ValueError("closed stream")
            self.consumed += 1
            yield line

    def close(self):
        self.closed.set()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class OnlineStreamIdleTests(unittest.TestCase):
    def setUp(self):
        self.watchdogs = {thread.ident for thread in threading.enumerate()
                          if thread.name == "naiba-stream-abort"}

    def tearDown(self):
        active = {thread.ident for thread in threading.enumerate()
                  if thread.name == "naiba-stream-abort"}
        self.assertFalse(active - self.watchdogs, "请求结束后不得遗留看门狗")

    def complete(self, response, status=None, profile=None):
        profile = profile or {
            "kind": "online", "model": "fixture", "name": "fixture",
            "base_url": "https://example.invalid/v1", "request_format": "openai_chat",
        }
        return runtime.ModelRuntime().complete(
            profile, [{"role": "user", "content": "fixture"}], {"stream": True}, status,
        )

    def test_noise_does_not_count_as_output(self):
        noise = [b": keepalive\n", b"\n", b"event: ping\n", b"data: [DONE]\n",
                 b"data: invalid\n", sse({"usage": {"total_tokens": 3}}),
                 delta(role="assistant"), delta(content="", reasoning_content=""),
                 delta(tool_calls=[{"index": 0, "function": {"arguments": ""}}])]
        for line in noise:
            with self.subTest(line=line):
                self.assertFalse(runtime._has_online_stream_output(line, "openai_chat"))
        self.assertFalse(runtime._has_online_stream_output(sse({
            "type": "response.function_call_arguments.delta", "item_id": "call-id", "delta": "",
        }), "codex_responses"))

    def test_all_online_sse_protocols_recognize_real_output(self):
        cases = [
            ("openai_chat", delta(content="正文")),
            ("openai_chat", delta(reasoning_content="思考")),
            ("openai_chat", delta(tool_calls=[{"function": {"arguments": "{}"}}])),
            ("codex_responses", sse({"type": "response.output_text.delta", "delta": "正文"})),
            ("codex_responses", sse({"type": "response.reasoning_text.delta", "delta": "思考"})),
            ("codex_responses", sse({"type": "response.function_call_arguments.delta", "delta": "{}"})),
            ("claude", sse({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "正文"}})),
            ("claude", sse({"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "思考"}})),
            ("claude", sse({"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{}"}})),
        ]
        for protocol, line in cases:
            with self.subTest(protocol=protocol, line=line):
                self.assertTrue(runtime._has_online_stream_output(line, protocol))

    def test_usage_and_keepalive_before_first_token_cannot_extend_wait(self):
        response = ScheduledResponse([
            b": keepalive\n", sse({"usage": {"total_tokens": 1}}), delta(role="assistant"),
        ] * 20, delay=0.03)
        with mock.patch.object(runtime.ModelRuntime, "_urlopen_cancelable", return_value=response) as opener, \
                mock.patch.object(runtime, "ONLINE_STREAM_IDLE_TIMEOUT_SECONDS", 0.2):
            started = time.perf_counter()
            with self.assertRaises(runtime.OnlineModelStreamIdleTimeout):
                self.complete(response)
        self.assertLess(time.perf_counter() - started, 2)
        self.assertLess(response.consumed, len(response.lines))
        self.assertEqual(opener.call_count, 1, "空闲超时不能隐式重放请求")

    def test_reasoning_then_stall_preserves_received_text_without_retry(self):
        response = ScheduledResponse([delta(reasoning_content="思" * 2749), delta(content="部分正文")]
                                     + [b": keepalive\n"] * 30, delay=0.02)
        events = []
        with mock.patch.object(runtime.ModelRuntime, "_urlopen_cancelable", return_value=response) as opener, \
                mock.patch.object(runtime, "ONLINE_STREAM_IDLE_TIMEOUT_SECONDS", 0.2):
            with self.assertRaises(runtime.OnlineModelStreamIdleTimeout):
                self.complete(response, events.append)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual("".join(e.get("content", "") for e in events if e["type"] == "reasoning_delta"), "思" * 2749)
        self.assertEqual("".join(e.get("content", "") for e in events if e["type"] == "delta"), "部分正文")

    def test_sustained_reasoning_longer_than_idle_budget_completes(self):
        response = ScheduledResponse([delta(reasoning_content="思考") for _ in range(8)]
                                     + [delta(content="结果"), b"data: [DONE]\n"], delay=0.04)
        with mock.patch.object(runtime.ModelRuntime, "_urlopen_cancelable", return_value=response), \
                mock.patch.object(runtime, "ONLINE_STREAM_IDLE_TIMEOUT_SECONDS", 0.2):
            self.assertEqual(self.complete(response), "结果")

    def test_normal_done_does_not_wait_for_transport_eof(self):
        cases = [
            ("openai_chat", [delta(content="结果"), b"data: [DONE]\n"]),
            ("claude", [sse({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "结果"}}),
                        sse({"type": "message_delta", "usage": {"output_tokens": 3}}), sse({"type": "message_stop"})]),
            ("codex_responses", [sse({"type": "response.completed", "response": {
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "结果"}]}],
                "usage": {"output_tokens": 3},
            }})]),
        ]
        for protocol, lines in cases:
            with self.subTest(protocol=protocol):
                response = ScheduledResponse(lines + [b": transport stays open\n"] * 30, delay=0.01)
                iterator = runtime.ModelRuntime._iter_stream_lines(response, idle_timeout=0.2, request_format=protocol)
                try:
                    result = runtime.ModelRuntime._read_sse_response(iterator, protocol, None)
                finally:
                    iterator.close()
                self.assertEqual(result["content"], "结果")
                self.assertTrue(result["saw_done"])
                self.assertEqual(response.consumed, len(lines))
                if protocol != "openai_chat":
                    self.assertEqual(result["usage"]["output_tokens"], 3)

    def test_online_idle_budget_does_not_apply_to_local_backend(self):
        response = ScheduledResponse([delta(content="结果"), b"data: [DONE]\n"], delay=0.1)
        with mock.patch.object(runtime.ModelRuntime, "_urlopen_cancelable", return_value=response), \
                mock.patch.object(runtime, "ONLINE_STREAM_IDLE_TIMEOUT_SECONDS", 0.03):
            self.assertEqual(self.complete(response, profile={
                "kind": "local", "model": "fixture", "base_url": "http://127.0.0.1:1",
                "request_format": "llama_cpp",
            }), "结果")

    def test_terminal_chunk_keeps_its_final_delta_before_stopping(self):
        cases = [
            {"content": "最后正文", "reasoning_content": "最后思考"},
            {"reasoning_content": "仅最后思考"},
            {"tool_calls": [{"index": 0, "id": "call-1", "function": {
                "name": "lookup", "arguments": '{"query":"final"}',
            }}]},
        ]
        for values in cases:
            with self.subTest(values=values):
                response = ScheduledResponse([sse({
                    "done": True, "choices": [{"delta": values}],
                    "usage": {"completion_tokens": 2},
                }), b": transport stays open\n"])
                result = runtime.ModelRuntime._read_sse_response(response, "openai_chat", None)
                self.assertEqual(response.consumed, 1)
                self.assertTrue(result["saw_done"])
                self.assertEqual(result["usage"]["output_tokens"], 2)
                self.assertEqual(result["reasoning"], values.get("reasoning_content", ""))
                if "tool_calls" in values:
                    action = json.loads(result["content"])
                    self.assertEqual(action["tool"], "lookup")
                    self.assertEqual(action["arguments"], {"query": "final"})
                else:
                    self.assertEqual(result["content"], values.get("content", ""))

    def test_default_idle_budget_matches_existing_read_timeout(self):
        self.assertEqual(runtime.ONLINE_STREAM_IDLE_TIMEOUT_SECONDS, 180)


if __name__ == "__main__":
    unittest.main()
