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


def tool_chunk(*, cid: str = "", name: str = "", args: str = "") -> bytes:
    """openai_chat 风格的原生 tool_calls 增量事件（name 与 arguments 可拆包）。"""
    function = {}
    if name:
        function["name"] = name
    if args:
        function["arguments"] = args
    call = {"index": 0, "function": function}
    if cid:
        call["id"] = cid
    return sse({"choices": [{"delta": {"tool_calls": [call]}}]})


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

    # ---- 原生 tool_calls 与正文交错（2026-10-05：claude-opus-5.5 走 codecraftapi
    # 「每轮只吐几个字」的回归位）。旧代码在 tool_calls 分支里把 guard.detected 置位，
    # 守卫永久闭嘴：tool_call **之后**的正文不再外发，又被终态 action 顶掉 ⇒ 只显示半句。
    # 修复后：正文继续外发；同 chunk 共存的 text 也要补发（A 方案）。 ----

    def test_sse_native_tool_call_keeps_prose_after_call(self):
        """正文被 tool_call 劈成两段：两段都要外发、顺序拼接；返回 content 仍是 action。"""
        events = []
        response = [
            sse({"choices": [{"delta": {"content": "手机点双箭头却弹起键盘，这个交"}}]}),
            tool_chunk(cid="call_1", name="list_directory"),
            tool_chunk(args='{"path": "/"}'),
            sse({"choices": [{"delta": {"content": "互确实反直觉 — 先帮你把问题理清楚。"}}]}),
            sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        ]
        result = StreamMixins._read_sse_response(response, "openai_chat", events.append)
        deltas = "".join(str(e.get("content") or "") for e in events if e.get("type") == "delta")
        self.assertEqual(
            deltas, "手机点双箭头却弹起键盘，这个交互确实反直觉 — 先帮你把问题理清楚。",
            "tool_call 之后到达的正文必须继续作为 delta 外发（不能只剩前半句）",
        )
        self.assertNotIn("list_directory", deltas, "tool_call 载荷不得泄漏进正文")
        action = SkillAgent._parse_action(result["content"])
        self.assertEqual(action["tool"], "list_directory")
        self.assertEqual(action["arguments"], {"path": "/"})

    def test_sse_native_tool_call_prose_boundary_mid_word(self):
        """跨边界拼回完整词：「这个交」+「互」=「交互」——防有人改成只保留之后那段。"""
        events = []
        response = [
            sse({"choices": [{"delta": {"content": "这个交"}}]}),
            tool_chunk(cid="call_9", name="list_directory"),
            sse({"choices": [{"delta": {"content": "互确实反直觉。"}}]}),
            sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        ]
        StreamMixins._read_sse_response(response, "openai_chat", events.append)
        deltas = "".join(str(e.get("content") or "") for e in events if e.get("type") == "delta")
        self.assertEqual(deltas, "这个交互确实反直觉。")
        self.assertIn("交互", deltas, "词被劈开的两半都要保留，顺序不能反")

    def test_sse_same_chunk_text_and_tool_calls_both_kept(self):
        """A 方案：同一 SSE 事件同时带 content 与 tool_calls（部分中转会合批），
        该段 text 也要进 delta 与 full_content_parts，不能被 `continue` 吃掉。"""
        events = []
        response = [
            sse({"choices": [{"delta": {"content": "先看目录结构，"}}]}),
            sse({"choices": [{"delta": {
                "content": "再决定改哪里。",
                "tool_calls": [{"index": 0, "id": "call_5",
                                "function": {"name": "list_directory", "arguments": '{"path": "/"}'}}],
            }}]}),
            sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        ]
        result = StreamMixins._read_sse_response(response, "openai_chat", events.append)
        deltas = "".join(str(e.get("content") or "") for e in events if e.get("type") == "delta")
        self.assertEqual(deltas, "先看目录结构，再决定改哪里。",
                         "同 chunk 的 text 语义上先于 tool_calls，必须补发")
        action = SkillAgent._parse_action(result["content"])
        self.assertEqual(action["tool"], "list_directory")

    def test_sse_native_tool_call_then_text_protocol_still_hidden(self):
        """tool_call 之后若出现**文本协议**（围栏 JSON / <invoke> / Harmony），仍不得进 delta
        ——守住「判定为协议后剩余正文不外发」这条既有语义不被本次修复破坏。"""
        events = []
        response = [
            sse({"choices": [{"delta": {"content": "前半句正文。"}}]}),
            tool_chunk(cid="call_7", name="pwsh"),
            tool_chunk(args='{"command": "dir"}'),
            sse({"choices": [{"delta": {"content": '{"type": "tool", "tool": "pwsh"}'}}]}),
            sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        ]
        result = StreamMixins._read_sse_response(response, "openai_chat", events.append)
        deltas = "".join(str(e.get("content") or "") for e in events if e.get("type") == "delta")
        self.assertEqual(deltas, "前半句正文。",
                         "tool_call 之后的文本协议必须继续被守卫吞掉")
        action = SkillAgent._parse_action(result["content"])
        self.assertEqual(action["tool"], "pwsh")

    def test_sse_native_tool_call_only_after_call_no_leak(self):
        """正文**全部**在 tool_call 之后（上游把 text block 排在 tool_use 之后）也必须
        正常外发——这是旧代码「一个字都不显示、只剩工具卡片」的回归位。"""
        events = []
        response = [
            tool_chunk(cid="call_2", name="read_file"),
            tool_chunk(args='{"path": "a.txt"}'),
            sse({"choices": [{"delta": {"content": "读完了，结论如下。"}}]}),
            sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        ]
        result = StreamMixins._read_sse_response(response, "openai_chat", events.append)
        deltas = "".join(str(e.get("content") or "") for e in events if e.get("type") == "delta")
        self.assertEqual(deltas, "读完了，结论如下。",
                         "tool_call 之后到达的正文一个字都不能丢")
        action = SkillAgent._parse_action(result["content"])
        self.assertEqual(action["tool"], "read_file")


class EmptyStreamEvidenceTests(unittest.TestCase):
    """空流证据诊断（STREAM_DEBUG_ON / NAIBA_DEBUG_STREAM=1，默认关闭）。

    外部排查（2026-10 报告）：空流报错缺原始响应证据，分不清「上游没回」还是
    「解析丢失」。开启后空流必须留存脱敏分片证据；默认关闭时零副作用。
    """

    EMPTY_RESPONSE = [
        sse({"choices": [{"delta": {"role": "assistant"}}]}),
        sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        b"data: [DONE]",
    ]

    def tearDown(self):
        import naiba.core.diagnostics as diagnostics

        diagnostics.STREAM_DEBUG_ON = False

    def _collect(self, response, events):
        StreamMixins._read_sse_response(response, "openai_chat", events.append)

    def test_disabled_by_default_no_evidence(self):
        events = []
        self._collect(self.EMPTY_RESPONSE, events)
        self.assertEqual([e for e in events if e.get("type") == "debug_stream"], [],
                         "诊断默认关闭，不得产生任何事件")

    def test_enabled_empty_stream_emits_evidence(self):
        import naiba.core.diagnostics as diagnostics

        diagnostics.STREAM_DEBUG_ON = True
        events = []
        self._collect(self.EMPTY_RESPONSE, events)
        evidence = [e for e in events if e.get("type") == "debug_stream"]
        self.assertEqual(len(evidence), 1, "开启后空流必须留一份证据")
        lines = evidence[0]["lines"]
        self.assertTrue(any("chunks=2" in line for line in lines), f"证据应含分片数：{lines[:2]}")
        self.assertTrue(any("choices" in line for line in lines), "证据应含事件类型分布")

    def test_enabled_non_empty_stream_no_evidence(self):
        import naiba.core.diagnostics as diagnostics

        diagnostics.STREAM_DEBUG_ON = True
        events = []
        self._collect(
            [
                sse({"choices": [{"delta": {"content": "有正文"}}]}),
                b"data: [DONE]",
            ],
            events,
        )
        self.assertEqual([e for e in events if e.get("type") == "debug_stream"], [],
                         "有正文的流不留证据")

    def test_evidence_sanitizes_long_fields(self):
        import naiba.core.diagnostics as diagnostics

        diagnostics.STREAM_DEBUG_ON = True
        events = []
        blob = "A" * 5000
        self._collect(
            [
                sse({"choices": [{"delta": {"role": "assistant"}}]}),
                sse({"type": "response.done", "blob": blob}),
                b"data: [DONE]",
            ],
            events,
        )
        evidence = [e for e in events if e.get("type") == "debug_stream"]
        self.assertEqual(len(evidence), 1)
        text = "\n".join(evidence[0]["lines"])
        self.assertNotIn(blob, text, "超长字段必须压成占位符（防刷屏）")
        self.assertIn("<str:5000>", text)


if __name__ == "__main__":
    unittest.main()
