"""Opt-in recipe evidence uses actual runtime parsing, not cleaned/status text."""
from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from naiba.llm.runtime import ModelRuntime


SECRET = "private provider reasoning"
VALID = '{"integrated_multimodal_description":"A quiet room"}'
RAW = " \tUnexpected explanation</think>" + VALID + "\n "
LOCAL = {"ollama", "lm_studio", "llama_cpp", "unsloth"}


def sse(*chunks):
    return b"".join(("data: " + json.dumps(chunk) + "\n\n").encode() for chunk in chunks)


class Response(io.BytesIO):
    def __init__(self, body):
        super().__init__(body)
        self.headers = {"Content-Type": "application/json"}


class RecipeRawContentTests(unittest.TestCase):
    def run_runtime(self, fmt, responses, *, stream=False, sink=None, options=None, status=None, effort="off"):
        calls = []
        runtime = ModelRuntime()
        def open_response(request, *_args):
            calls.append(json.loads(request.data))
            outcome = responses[len(calls) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return Response(outcome if isinstance(outcome, bytes) else json.dumps(outcome).encode())
        opts = {"stream": stream, "stream_total_timeout_seconds": 0,
                "first_byte_timeout_seconds": 0, **(options or {})}
        if sink is not None:
            opts["raw_content_sink"] = sink
        profile = {"kind": "local" if fmt in LOCAL else "online", "request_format": fmt,
                   "base_url": "http://127.0.0.1:1", "model": "fixture-model", "reasoning_effort": effort}
        with mock.patch.object(ModelRuntime, "_urlopen_cancelable", side_effect=open_response), \
                mock.patch("naiba.llm.runtime.time.sleep"):
            result = runtime.complete(profile, [{"role": "user", "content": "test"}], opts, status)
        self.assertTrue(all("raw_content_sink" not in json.dumps(call) for call in calls))
        return result, runtime, calls

    def assert_raw(self, events, expected=RAW):
        self.assertEqual(events[0], {"type": "reset", "content": ""})
        self.assertEqual(events[-1], {"type": "done", "content": expected})
        self.assertEqual("".join(e["content"] for e in events if e["type"] == "delta"), expected)
        self.assertNotIn(SECRET, str(events))

    def test_nonstream_all_formats_capture_verbatim_without_reasoning(self):
        formats = {
            "openai_chat": {"choices": [{"message": {"content": RAW, "reasoning_content": SECRET}}]},
            "llama_cpp": {"choices": [{"message": {"content": RAW, "reasoning_content": SECRET}}]},
            "unsloth": {"choices": [{"message": {"content": RAW, "reasoning_content": SECRET}}]},
            "ollama": {"message": {"content": RAW, "thinking": SECRET}},
            "claude": {"content": [{"type": "thinking", "thinking": SECRET}, {"type": "text", "text": RAW}]},
            "gemini": {"candidates": [{"content": {"parts": [{"thought": True, "text": SECRET}, {"text": RAW}]}}]},
            "codex_responses": {"output": [{"type": "reasoning", "content": [{"type": "reasoning_text", "text": SECRET}]},
                                             {"type": "message", "content": [{"type": "output_text", "text": RAW}]}]},
            "lm_studio": {"output": [{"type": "reasoning", "content": SECRET}, {"type": "message", "content": RAW}]},
        }
        for fmt, response in formats.items():
            with self.subTest(fmt=fmt):
                events = []
                result, _, calls = self.run_runtime(fmt, [response], sink=events.append)
                self.assert_raw(events)
                self.assertEqual(result, VALID)
                self.assertEqual(len(calls), 1)

    def test_stream_all_formats_capture_before_inline_parser(self):
        pieces = [" \tUnexpected explanation</thi", "nk>" + VALID, "\n "]
        for fmt in ["openai_chat", "llama_cpp", "unsloth", "codex_responses", "claude", "ollama", "lm_studio", "gemini"]:
            with self.subTest(fmt=fmt):
                if fmt in {"openai_chat", "llama_cpp", "unsloth"}:
                    body = sse({"choices": [{"delta": {"reasoning_content": SECRET}}]},
                               *[{"choices": [{"delta": {"content": p}}]} for p in pieces]) + b"data: [DONE]\n"
                elif fmt == "codex_responses":
                    body = sse({"type": "response.reasoning_text.delta", "delta": SECRET},
                               *[{"type": "response.output_text.delta", "delta": p} for p in pieces],
                               {"type": "response.completed", "response": {}})
                elif fmt == "claude":
                    body = sse({"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": SECRET}},
                               *[{"type": "content_block_delta", "delta": {"type": "text_delta", "text": p}} for p in pieces],
                               {"type": "message_stop"})
                elif fmt == "ollama":
                    body = b"".join((json.dumps(c) + "\n").encode() for c in [
                        {"message": {"thinking": SECRET}}, *[{"message": {"content": p}} for p in pieces], {"done": True}])
                elif fmt == "lm_studio":
                    body = sse({"type": "reasoning.delta", "content": SECRET},
                               *[{"type": "message.delta", "content": p} for p in pieces], {"type": "chat.end"})
                else:
                    body = sse(*[{"candidates": [{"content": {"parts": [{"thought": True, "text": SECRET}, {"text": p}]}}]} for p in pieces])
                events = []
                result, _, calls = self.run_runtime(fmt, [body], stream=True, sink=events.append)
                self.assert_raw(events)
                # Gemini's legacy display extractor also includes thought parts;
                # the new evidence channel deliberately does not change it.
                if fmt != "gemini":
                    self.assertEqual(result, VALID)
                self.assertEqual(len(calls), 1)

    def test_inline_content_tags_preserved_but_reasoning_field_excluded(self):
        raw = "<think>inline content</think>" + VALID
        body = sse({"choices": [{"delta": {"reasoning_content": SECRET, "content": raw}}]}) + b"data: [DONE]\n"
        events = []
        result, runtime, _ = self.run_runtime("openai_chat", [body], stream=True, sink=events.append)
        self.assert_raw(events, raw)
        self.assertIn("inline content", runtime.last_reasoning)
        self.assertEqual(result, VALID)

    def test_responses_aggregate_content_has_no_added_separators_or_reasoning(self):
        response = {"output": [{"type": "reasoning", "content": [{"type": "reasoning_text", "text": SECRET}]},
                               {"id": "m", "type": "message", "content": [{"type": "output_text", "text": "Unexpected "},
                                                                            {"type": "output_text", "text": "explanation</think>" + VALID}]}]}
        body = sse({"type": "response.output_item.done", "item": response["output"][1]},
                   {"type": "response.completed", "response": response})
        events = []
        self.run_runtime("codex_responses", [body], stream=True, sink=events.append)
        self.assert_raw(events, "Unexpected explanation</think>" + VALID)

    def test_retry_discards_failed_attempt_and_done_only_once(self):
        first = sse({"choices": [{"delta": {"content": "failed prefix"}}]})  # cut stream
        second = sse({"choices": [{"delta": {"content": RAW}}]}) + b"data: [DONE]\n"
        events = []
        _, _, calls = self.run_runtime("openai_chat", [first, second], stream=True, sink=events.append)
        self.assertEqual(len(calls), 2)
        self.assertEqual([e["type"] for e in events], ["reset", "delta", "reset", "delta", "done"])
        self.assertEqual(events[-1]["content"], RAW)

    def test_ollama_nested_retry_resets_raw(self):
        first = (json.dumps({"message": {"thinking": SECRET}, "done": True}) + "\n").encode()
        second = (json.dumps({"message": {"content": RAW}, "done": True}) + "\n").encode()
        events = []
        _, _, calls = self.run_runtime("ollama", [first, second], stream=True, sink=events.append, effort="auto")
        self.assertEqual(len(calls), 2)
        self.assertEqual([e["type"] for e in events], ["reset", "reset", "delta", "done"])
        self.assertEqual(events[-1]["content"], RAW)

    def test_empty_raw_is_authoritative_for_native_tool_action(self):
        response = {"choices": [{"message": {"content": "", "reasoning_content": SECRET,
                    "tool_calls": [{"id": "1", "function": {"name": "test", "arguments": "{}"}}]}}]}
        events = []
        result, _, calls = self.run_runtime("openai_chat", [response], sink=events.append)
        self.assertIn('"tool"', result)
        self.assert_raw(events, "")
        self.assertEqual(len(calls), 1)

    def test_whitespace_or_inline_only_content_does_not_trigger_recovery_request(self):
        for raw in [" \t\n ", "<think>not separate reasoning</think>"]:
            with self.subTest(raw=raw):
                body = sse({"choices": [{"delta": {"content": raw}}]}) + b"data: [DONE]\n"
                events = []
                result, _, calls = self.run_runtime("openai_chat", [body], stream=True, sink=events.append)
                self.assertEqual(result, "")
                self.assert_raw(events, raw)
                self.assertEqual(len(calls), 1)

    def test_sink_failure_aborts_without_retry(self):
        seen = []
        def sink(event):
            seen.append(event)
            if event["type"] == "delta":
                raise OSError("consumer failed")
        with self.assertRaisesRegex(RuntimeError, "raw_content_sink callback failed"):
            self.run_runtime("openai_chat", [{"choices": [{"message": {"content": RAW}}]}], sink=sink)
        self.assertEqual([e["type"] for e in seen], ["reset", "delta"])

    def test_unknown_content_block_fails_closed_without_done(self):
        events = []
        response = {"content": [{"type": "unknown", "text": VALID}]}
        with self.assertRaisesRegex(RuntimeError, "Unsupported raw assistant content block type"):
            self.run_runtime("claude", [response], sink=events.append)
        self.assertEqual(events, [{"type": "reset", "content": ""}])

    def test_runtime_evidence_reproduces_strict_i2v_rejection(self):
        from naiba.super_agent.recipe_catalog import validate_output
        from tests.test_recipe_catalog import i2v
        valid = i2v()
        raw = "Unexpected explanation</think>" + valid
        events = []
        body = sse({"choices": [{"delta": {"content": raw}}]}) + b"data: [DONE]\n"
        result, _, calls = self.run_runtime("openai_chat", [body], stream=True, sink=events.append)
        self.assertEqual(result, valid)
        self.assertTrue(validate_output("minimax_h3_i2v", result, image_count=1, duration_seconds=5)["valid"])
        selected = validate_output("minimax_h3_i2v", events[-1]["content"], image_count=1, duration_seconds=5)
        self.assertFalse(selected["valid"])
        self.assertEqual(selected["raw"], raw)
        self.assertEqual(len(calls), 1)

    def test_claude_initial_text_block_is_captured_before_deltas(self):
        events = []
        body = sse({"type": "content_block_start", "content_block": {"type": "text", "text": "Unexpected explanation</think>"}},
                   {"type": "content_block_delta", "delta": {"type": "text_delta", "text": VALID}},
                   {"type": "message_stop"})
        self.run_runtime("claude", [body], stream=True, sink=events.append)
        self.assert_raw(events, "Unexpected explanation</think>" + VALID)

    def test_no_done_when_all_stream_attempts_are_cut(self):
        events = []
        body = sse({"choices": [{"delta": {"content": VALID}}]})
        with self.assertRaisesRegex(RuntimeError, "没有终止原因"):
            self.run_runtime("openai_chat", [body, body], stream=True, sink=events.append)
        self.assertNotIn("done", [e["type"] for e in events])

    def test_stream_sink_network_exception_never_retries_provider(self):
        seen = []
        def sink(event):
            seen.append(event)
            if event["type"] == "delta":
                raise OSError("consumer failed")
        body = sse({"choices": [{"delta": {"content": VALID}}]}) + b"data: [DONE]\n"
        with self.assertRaisesRegex(RuntimeError, "raw_content_sink callback failed"):
            self.run_runtime("openai_chat", [body], stream=True, sink=sink)
        self.assertEqual([e["type"] for e in seen], ["reset", "delta"])

    def test_no_sink_keeps_existing_cleaned_result(self):
        result, _, calls = self.run_runtime("openai_chat", [{"choices": [{"message": {"content": RAW}}]}])
        self.assertEqual(result, VALID)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
