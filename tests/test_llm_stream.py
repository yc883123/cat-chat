# -*- coding: utf-8 -*-
"""护栏：naiba/llm/stream 流解析与推理流行为规格（收官线 ① 第三件）。

保护对象：SSE/Ollama/LM Studio 流解析、<think> 推理流、Agent 工具协议守卫与
缓冲收尾自 ModelRuntime 迁入 StreamMixins 时的行为等价（MRO 委派，纯解析不触网）。
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.llm.stream import (  # noqa: E402
    StreamMixins,
    _InlineReasoningParser,
    _ReasoningStreamer,
)
from naiba.skills.agent import SkillAgent  # noqa: E402


def sse(chunk: dict) -> bytes:
    """把单个 Responses SSE 事件序列化成一行 bytes。"""
    return ("data: " + json.dumps(chunk, ensure_ascii=False)).encode("utf-8")


class StreamGuardTests(unittest.TestCase):
    def test_classify_text_vs_tool(self):
        self.assertEqual(StreamMixins._classify_agent_output("普通正文"), "text")
        self.assertEqual(StreamMixins._classify_agent_output('{"type": "tool", "tool": "pwsh"}'), "tool")
        self.assertEqual(StreamMixins._classify_agent_output('<tool name="pwsh">x</tool>'), "tool")

    def test_tool_protocol_offset(self):
        self.assertEqual(StreamMixins._tool_protocol_offset('<tool name="x">'), 0)
        self.assertEqual(StreamMixins._tool_protocol_offset('<|open|>tools<|sep|>'), 0)
        self.assertIsNone(StreamMixins._tool_protocol_offset("纯文本内容"))

    def test_possible_protocol_suffix_length(self):
        self.assertEqual(StreamMixins._possible_protocol_suffix_length(""), 0)
        self.assertEqual(StreamMixins._possible_protocol_suffix_length("<to"), 3)
        self.assertEqual(StreamMixins._possible_protocol_suffix_length("<tool"), 5)
        self.assertEqual(StreamMixins._possible_protocol_suffix_length("<|open|>to"), 10)
        # <think 由 _InlineReasoningParser 处理，不属于工具协议守卫 token。
        self.assertEqual(StreamMixins._possible_protocol_suffix_length("<thi"), 0)

    def test_kimi_harmony_single_tool_action(self):
        raw = (
            '收到，开始排查：<|open|>tools<|sep|>'
            '<|open|>call tool="pwsh" index="1"<|sep|>'
            '<|open|>argument key="command" type="string"<|sep|>'
            'Get-Process | Select-Object Id,Name'
            '<|close|>argument<|sep|><|close|>call<|sep|>'
            '<|close|>tools<|sep|><|close|>message<|sep|>'
        )
        action = SkillAgent._parse_action(raw)
        self.assertEqual(action["type"], "tool")
        self.assertEqual(action["tool"], "pwsh")
        self.assertEqual(
            action["arguments"], {"command": "Get-Process | Select-Object Id,Name"}
        )

    def test_kimi_harmony_parallel_actions_and_json_types(self):
        raw = (
            '<|open|>tools<|sep|>'
            '<|open|>call tool="read_file" index="1"<|sep|>'
            '<|open|>argument key="path" type="string"<|sep|>D:\\\\work\\\\a.txt'
            '<|close|>argument<|sep|>'
            '<|open|>argument key="max_lines" type="integer"<|sep|>12'
            '<|close|>argument<|sep|><|close|>call<|sep|>'
            '<|open|>call tool="list_directory" index="2"<|sep|>'
            '<|open|>argument key="path" type="string"<|sep|>D:\\\\work'
            '<|close|>argument<|sep|>'
            '<|open|>argument key="recursive" type="boolean"<|sep|>true'
            '<|close|>argument<|sep|><|close|>call<|sep|>'
            '<|close|>tools<|sep|>'
        )
        action = SkillAgent._parse_action(raw)
        self.assertEqual(action["type"], "tools")
        self.assertEqual([call["tool"] for call in action["calls"]],
                         ["read_file", "list_directory"])
        self.assertEqual(action["calls"][0]["arguments"]["max_lines"], 12)
        self.assertIs(action["calls"][1]["arguments"]["recursive"], True)

    def test_kimi_harmony_truncated_call_is_parse_error(self):
        raw = (
            '<|open|>tools<|sep|><|open|>call tool="pwsh" index="1"<|sep|>'
            '<|open|>argument key="command" type="string"<|sep|>Get-Process'
        )
        self.assertEqual(SkillAgent._parse_action(raw), {"type": "parse_error"})

    def test_clean_content_strips_think_blocks(self):
        # 语义：保留最后一个 </think> 之后的可见文本，其余 think 块剔除。
        self.assertEqual(StreamMixins._clean_content("前文<think>推理</think>答案"), "答案")
        self.assertEqual(StreamMixins._clean_content("  普通文本  \n"), "普通文本")


class InlineReasoningTests(unittest.TestCase):
    def test_think_split_across_feeds(self):
        parser = _InlineReasoningParser()
        self.assertEqual(parser.feed("<think>推理中"), ("", "推理中"))
        self.assertEqual(parser.feed("</think>正文"), ("正文", ""))

    def test_plain_text_passthrough(self):
        parser = _InlineReasoningParser()
        self.assertEqual(parser.feed("你好"), ("你好", ""))

    def test_final_flushes_reasoning(self):
        parser = _InlineReasoningParser()
        self.assertEqual(parser.feed("<think>未完", final=True), ("", "未完"))


class ReasoningStreamerTests(unittest.TestCase):
    def test_live_stream_events(self):
        events = []
        parts = []
        streamer = _ReasoningStreamer(events.append, parts)
        streamer.feed("第一段")
        streamer.feed("第二段")
        streamer.finish()
        self.assertEqual([e.get("type") for e in events],
                         ["reasoning_start", "reasoning_delta", "reasoning_delta", "reasoning_end"])

    def test_fallback_oneshot_for_precollected_parts(self):
        events = []
        streamer = _ReasoningStreamer(events.append, ["兜底思考"])
        streamer.finish()
        self.assertEqual([e.get("type") for e in events],
                         ["reasoning_start", "reasoning_delta", "reasoning_end"])
        self.assertEqual(events[1]["content"], "兜底思考")


class StreamReaderTests(unittest.TestCase):
    def test_read_ollama_stream_smoke(self):
        events = []
        response = [
            '{"message": {"content": "你好"}}'.encode("utf-8"),
            '{"message": {"thinking": "想一下"}, "prompt_eval_count": 3, "eval_count": 2}'.encode("utf-8"),
        ]
        result = StreamMixins._read_ollama_stream(response, events.append)
        self.assertEqual(result["content"], "你好")
        self.assertEqual(result["reasoning"], "想一下")
        self.assertEqual(result["usage"], {"input_tokens": 3, "output_tokens": 2,
                                           "total_tokens": 5, "cached_tokens": 0})

    def test_read_sse_response_smoke(self):
        events = []
        response = [
            'data: {"choices": [{"delta": {"content": "回答"}}]}'.encode("utf-8"),
            b"data: [DONE]",
        ]
        result = StreamMixins._read_sse_response(response, "openai_chat", events.append)
        self.assertEqual(result["content"], "回答")
        self.assertEqual(result["reasoning"], "")
        self.assertTrue(any(e.get("type") == "delta" for e in events))

    def test_read_sse_hides_split_kimi_harmony_protocol(self):
        events = []
        pieces = [
            '<|open|>to',
            'ols<|sep|><|open|>call tool="pwsh" index="1"<|sep|>',
            '<|open|>argument key="command" type="string"<|sep|>Get-Date',
            '<|close|>argument<|sep|><|close|>call<|sep|><|close|>tools<|sep|>',
        ]
        response = [
            sse({"choices": [{"delta": {"content": piece}}]}) for piece in pieces
        ]
        result = StreamMixins._read_sse_response(response, "openai_chat", events.append)
        self.assertFalse(any("<|open|>" in str(e.get("content") or "") for e in events))
        action = SkillAgent._parse_action(result["content"])
        self.assertEqual(action["tool"], "pwsh")
        self.assertEqual(action["arguments"], {"command": "Get-Date"})

    def test_read_sse_captures_reasoning_item_id(self):
        """DeepSeek Responses 思考回传必需 reasoning item id：从 output_item.added
        捕获；delta 事件的 item_id 兜底。"""
        events = []
        response = [
            'data: {"type": "response.output_item.added", "output_index": 0, '
            '"item": {"type": "reasoning", "id": "rs_abc123", "status": "in_progress"}}'.encode("utf-8"),
            'data: {"type": "response.reasoning_text.delta", "item_id": "rs_abc123", "delta": "思考"}'.encode("utf-8"),
            'data: {"type": "response.output_text.delta", "delta": "回答"}'.encode("utf-8"),
        ]
        result = StreamMixins._read_sse_response(response, "codex_responses", events.append)
        self.assertEqual(result["reasoning"], "思考")
        self.assertEqual(result["reasoning_id"], "rs_abc123")
        self.assertEqual(result["content"], "回答")

    def test_read_sse_reasoning_id_fallback_from_delta(self):
        """output_item.added 未携带 id 时，delta 事件的 item_id 兜底收集。"""
        result = StreamMixins._read_sse_response([
            'data: {"type": "response.reasoning_text.delta", "item_id": "rs_delta9", "delta": "想"}'.encode("utf-8"),
            'data: {"type": "response.reasoning_text.delta", "item_id": "rs_delta9", "delta": "一下"}'.encode("utf-8"),
        ], "codex_responses", None)
        self.assertEqual(result["reasoning_id"], "rs_delta9")

    def test_read_sse_backfills_aggregated_message_once(self):
        """无 output_text.delta、仅聚合事件时回填正文；done 与 completed 含同一
        message 时正文只能下发一次。"""
        message = {"type": "message", "id": "msg_1",
                   "content": [{"type": "output_text", "text": "聚合正文"}]}
        response = [
            sse({"type": "response.output_item.done", "item": message}),
            sse({"type": "response.completed", "response": {"output": [message]}}),
        ]
        events = []
        result = StreamMixins._read_sse_response(response, "codex_responses", events.append)
        self.assertEqual(result["content"], "聚合正文")
        self.assertEqual(
            sum(1 for e in events if e.get("type") == "delta" and e.get("content") == "聚合正文"),
            1,
            "同一 message 同时出现在 done 与 completed 时正文只能下发一次",
        )

    def test_read_sse_backfills_aggregated_reasoning_and_id(self):
        """仅聚合事件时回填思考文本与 reasoning item id（供下一轮回传）。"""
        reasoning_item = {"type": "reasoning", "id": "rs_agg1",
                          "content": [{"type": "reasoning_text", "text": "聚合思考"}]}
        response = [
            sse({"type": "response.output_item.done", "item": reasoning_item}),
            sse({"type": "response.completed", "response": {"output": [
                reasoning_item,
                {"type": "message", "id": "msg_2",
                 "content": [{"type": "output_text", "text": "答复"}]},
            ]}}),
        ]
        result = StreamMixins._read_sse_response(response, "codex_responses", None)
        self.assertEqual(result["content"], "答复")
        self.assertEqual(result["reasoning"], "聚合思考")
        self.assertEqual(result["reasoning_id"], "rs_agg1")

    def test_read_sse_backfills_aggregated_function_call(self):
        """仅聚合事件的 function_call 也要组装为 Agent action，不能被当空流。"""
        response = [
            sse({"type": "response.completed", "response": {"output": [
                {"type": "function_call", "call_id": "call_1", "name": "pwsh",
                 "arguments": json.dumps({"command": "dir"})},
            ]}}),
        ]
        result = StreamMixins._read_sse_response(response, "codex_responses", None)
        payload = json.loads(result["content"])
        self.assertEqual(payload["type"], "tool")
        self.assertEqual(payload["tool"], "pwsh")
        self.assertEqual(payload["arguments"], {"command": "dir"})

    def test_read_sse_incomplete_not_backfilled(self):
        """response.incomplete 属被截断的回答，不得作为成功正文回填。"""
        response = [
            sse({"type": "response.incomplete", "response": {"output": [
                {"type": "message", "id": "msg_x",
                 "content": [{"type": "output_text", "text": "被截断"}]},
            ]}}),
        ]
        result = StreamMixins._read_sse_response(response, "codex_responses", None)
        self.assertEqual(result["content"], "")

    def test_read_sse_incomplete_blocks_prior_done_backfill(self):
        """done 在 incomplete 之前到达时，截断部分也不得进入成功正文。"""
        response = [
            sse({"type": "response.output_item.done", "item": {
                "type": "message", "id": "msg_partial",
                "content": [{"type": "output_text", "text": "截断前内容"}]}}),
            sse({"type": "response.incomplete", "response": {"output": []}}),
        ]
        result = StreamMixins._read_sse_response(response, "codex_responses", None)
        self.assertEqual(result["content"], "")


if __name__ == "__main__":
    unittest.main()
