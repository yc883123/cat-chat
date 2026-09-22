# -*- coding: utf-8 -*-
"""护栏：naiba/llm/protocols 协议适配行为规格（收官线 ① 第二件）。

保护对象：各 request_format 的 wire 序列化/解析自 ModelRuntime 迁入 ProtocolMixins 时的
行为等价（MRO 委派）。全部用例直接以 ProtocolMixins 静态方法调用（不经网络）。
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.llm.protocols import NO_REASONING_PLACEHOLDER, ProtocolMixins as P  # noqa: E402


class LlmProtocolTests(unittest.TestCase):
    def test_openai_messages_basic(self):
        out = P._openai_messages([{"role": "user", "content": "你好"}, {"role": "assistant", "content": "好"}])
        self.assertEqual(out, [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "好"}])

    def test_openai_messages_tool_and_reasoning(self):
        out = P._openai_messages([
            {"role": "tool", "tool_call_id": "t1", "content": "结果"},
            {"role": "assistant", "content": "", "reasoning_content": "在想",
             "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {"path": "a"}}]},
        ], include_reasoning_content=True)
        self.assertEqual(out[0]["role"], "tool")
        self.assertEqual(out[0]["tool_call_id"], "t1")
        self.assertEqual(out[1]["reasoning_content"], "在想")
        self.assertEqual(out[1]["tool_calls"][0]["function"]["name"], "read_file")

    def test_openai_content_image_part(self):
        out = P._openai_content([{"type": "text", "text": "看图"},
                                 {"type": "image", "media_type": "image/png", "data": "QQ=="}])
        self.assertEqual(out[1]["type"], "image_url")
        self.assertTrue(out[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_responses_input(self):
        out = P._responses_input([
            {"role": "tool", "tool_call_id": "c1", "content": "out"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "name": "pwsh", "arguments": {"command": "dir"}}]},
        ])
        self.assertEqual(out[0]["type"], "function_call_output")
        self.assertEqual(out[0]["call_id"], "c1")
        # 无推理文本的工具轮也产出**非空占位** reasoning item（空文本服务端 400）
        self.assertEqual(out[1]["type"], "reasoning")
        self.assertEqual(out[1]["content"][0]["text"], NO_REASONING_PLACEHOLDER)
        self.assertTrue(NO_REASONING_PLACEHOLDER.strip())
        self.assertEqual(out[2]["role"], "assistant")
        self.assertEqual(out[3]["type"], "function_call")

    def test_responses_input_reasoning_passback(self):
        """回归：DeepSeek 思考模式（携带 tools）要求历史 reasoning 以独立 reasoning item
        回传（content 为 reasoning_text 内容块列表，官方 schema「以明文承载思维链内容」，
        传字符串会被 serde 拒 "expected a sequence"）；item 必须带唯一 id（无 id 多轮长链
        实测 400「must be passed back」）——优先用服务端真实 id，旧历史无 id 时合成 rs_h_ 兜底。"""
        # 纯对话 assistant 轮：block 数组 + 合成 id → assistant 消息
        out = P._responses_input([
            {"role": "assistant", "content": "回答", "reasoning_content": "思考中"},
        ])
        self.assertEqual(out[0]["type"], "reasoning")
        self.assertEqual(out[0]["content"], [{"type": "reasoning_text", "text": "思考中"}])
        self.assertTrue(str(out[0]["id"] or "").startswith("rs_h_"))
        self.assertEqual(out[1]["role"], "assistant")
        # 服务端真实 id 原样保留（流式解析捕获 / 非流式响应提取）
        out_id = P._responses_input([
            {"role": "assistant", "content": "回答", "reasoning_content": "思考中", "reasoning_id": "rs_real123"},
        ])
        self.assertEqual(out_id[0]["id"], "rs_real123")
        # tool_calls 轮：reasoning item → assistant 消息 → function_call（配对相邻）
        out2 = P._responses_input([
            {"role": "assistant", "content": "",
             "reasoning": "先调用工具",
             "tool_calls": [{"id": "c1", "name": "pwsh", "arguments": {"command": "dir"}}]},
        ])
        self.assertEqual(out2[0]["type"], "reasoning")
        self.assertEqual(out2[0]["content"][0]["text"], "先调用工具")
        self.assertEqual(out2[1]["role"], "assistant")
        self.assertEqual(out2[2]["type"], "function_call")
        self.assertEqual(out2[2]["call_id"], "c1")
        # 无 reasoning 的工具轮（服务端未返回 CoT）：仍必须产出 reasoning item，且 text 非空
        # （2026-09-10 真机矩阵：不回传 400 / text="" 400 / text=" " 或占位文本 200）
        out_empty = P._responses_input([
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c9", "name": "pwsh", "arguments": {"command": "ping"}}]},
        ])
        self.assertEqual(out_empty[0]["type"], "reasoning")
        self.assertEqual(out_empty[0]["content"],
                         [{"type": "reasoning_text", "text": NO_REASONING_PLACEHOLDER}])
        self.assertTrue(str(out_empty[0]["id"] or "").startswith("rs_h_"))
        self.assertEqual(out_empty[1]["role"], "assistant")
        self.assertEqual(out_empty[2]["type"], "function_call")
        # 多个空 CoT 轮不能撞 id（占位文本相同 → id 必须由工具调用签名派生）
        out_two = P._responses_input([
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c9", "name": "pwsh", "arguments": {"command": "ping"}}]},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c10", "name": "pwsh", "arguments": {"command": "pong"}}]},
        ])
        reasoning_ids = [item["id"] for item in out_two if item.get("type") == "reasoning"]
        self.assertEqual(len(reasoning_ids), 2)
        self.assertEqual(len(set(reasoning_ids)), 2, "空 CoT 轮的合成 id 必须逐轮唯一")
        # 无 reasoning 时：不产生 reasoning item（仅 assistant 消息）
        out3 = P._responses_input([{"role": "assistant", "content": "回答"}])
        self.assertEqual(out3[0].get("role"), "assistant")
        self.assertEqual(len(out3), 1)

    def test_tool_schemas_all_formats(self):
        rows = [{"name": "read_file", "description": "读", "parameters": {"type": "object", "properties": {}}}]
        self.assertEqual(P._tool_schemas(rows, "openai_chat")[0]["type"], "function")
        self.assertEqual(P._tool_schemas(rows, "claude")[0]["input_schema"]["type"], "object")
        self.assertEqual(P._tool_schemas(rows, "codex_responses")[0]["name"], "read_file")
        self.assertEqual(P._tool_schemas(rows, "gemini")[0]["name"], "read_file")

    def test_ollama_messages(self):
        out = P._ollama_messages([{"role": "user", "content": "hi"}])
        self.assertEqual(out[0]["role"], "user")

    def test_gemini_message(self):
        out = P._gemini_message({"role": "user", "content": "hi"})
        self.assertIn("role", out)
        self.assertIn("parts", out)

    def test_claude_message_and_cache_control(self):
        messages = [{"role": "user", "content": "最后一条"}]
        P._claude_apply_cache_control(messages)
        self.assertEqual(messages[0]["content"][-1].get("cache_control"), {"type": "ephemeral"})

    def test_reasoning_params(self):
        """预设表驱动：普通 openai_chat 发 reasoning_effort；DeepSeek 画像一个字段都不发。

        （后者是数据化之后才对的语义：DeepSeek 兼容端点拒收 reasoning_effort，
        旧实现里 ``deepseek`` 这个入参只在 codex_responses 分支被读到，openai_chat
        分支照样发字段——正是 Kimi K2 那个「调档必先 400」bug 的同源问题。）
        """
        params = P._reasoning_params("openai_chat", "high")
        self.assertIsInstance(params, dict)
        self.assertIn("reasoning_effort", params)
        self.assertEqual(P._reasoning_params("openai_chat", "high", deepseek=True), {})
        self.assertEqual(P._reasoning_params("openai_chat", "off"), {})
        self.assertEqual(P._reasoning_params("openai_chat", "auto"), {})

    def test_reasoning_params_kimi_k3(self):
        """Kimi K3 方言：reasoning_effort 只认 low/high/max（默认 max），思考关不掉。

        应用四档据此映射：off→low（最贴近「关」）、low→low、medium→high、high→max。
        K2.x 不接受 reasoning_effort（会 400），判定必须只命中 K3。
        """
        self.assertEqual(P._reasoning_params("openai_chat", "off", kimi_k3=True),
                         {"reasoning_effort": "low"})
        self.assertEqual(P._reasoning_params("openai_chat", "low", kimi_k3=True),
                         {"reasoning_effort": "low"})
        self.assertEqual(P._reasoning_params("openai_chat", "medium", kimi_k3=True),
                         {"reasoning_effort": "high"})
        self.assertEqual(P._reasoning_params("openai_chat", "high", kimi_k3=True),
                         {"reasoning_effort": "max"})
        # auto 不发任何参数（与其它协议一致）
        self.assertEqual(P._reasoning_params("openai_chat", "auto", kimi_k3=True), {})
        # 判定只看模型名、仅命中 K3（K2.x 用 thinking 参数，绝不能按 K3 方言发）
        self.assertTrue(P._is_kimi_k3_profile({"model": "kimi-k3"}))
        self.assertTrue(P._is_kimi_k3_profile({"model": "Kimi-K3-0905"}))
        self.assertFalse(P._is_kimi_k3_profile({"model": "kimi-k2.6"}))
        self.assertFalse(P._is_kimi_k3_profile({"model": "kimi-k2.7-code"}))
        self.assertFalse(P._is_kimi_k3_profile({"model": "deepseek-v4-flash"}))
        # codex_responses 不做 K3 特判：K3 无 Responses API，中继按 OpenAI 方言翻译
        self.assertEqual(P._reasoning_params("codex_responses", "medium", kimi_k3=True),
                         {"reasoning": {"effort": "medium"}})

    def test_with_endpoint_and_local_endpoint(self):
        self.assertEqual(P._with_endpoint("https://api.openai.com/v1", "/chat/completions"),
                         "https://api.openai.com/v1/chat/completions")
        # 本地端点：剥离余留 /v1 后再拼路径（ollama/lm_studio 基础 URL 习惯带 /v1）。
        self.assertEqual(P._local_endpoint("http://127.0.0.1:11434/v1", "/api/chat"),
                         "http://127.0.0.1:11434/api/chat")
        self.assertEqual(P._local_endpoint("http://127.0.0.1:1234", "/v1/chat/completions"),
                         "http://127.0.0.1:1234/v1/chat/completions")

    def test_with_endpoint_base_already_versioned(self):
        """base 自带版本段（方舟 /api/v3、/api/coding/v3，智谱 /api/paas/v4）⇒ 剥 /v1 前缀直接追加。"""
        ark_coding = "https://ark.cn-beijing.volces.com/api/coding/v3"
        self.assertEqual(P._with_endpoint(ark_coding, "/v1/chat/completions"),
                         "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions")
        self.assertEqual(P._with_endpoint(ark_coding, "/v1/models"),
                         "https://ark.cn-beijing.volces.com/api/coding/v3/models")
        self.assertEqual(P._with_endpoint("https://ark.cn-beijing.volces.com/api/v3", "/v1/chat/completions"),
                         "https://ark.cn-beijing.volces.com/api/v3/chat/completions")
        self.assertEqual(P._with_endpoint("https://open.bigmodel.cn/api/paas/v4", "/v1/chat/completions"),
                         "https://open.bigmodel.cn/api/paas/v4/chat/completions")
        # 回归：Gemini /v1beta 目标不走新规则；/v1 结尾与裸域名的既有拼写逐字不变。
        self.assertEqual(P._with_endpoint("https://generativelanguage.googleapis.com", "/v1beta/models"),
                         "https://generativelanguage.googleapis.com/v1beta/models")
        self.assertEqual(P._with_endpoint("https://api.openai.com/v1", "/v1/responses"),
                         "https://api.openai.com/v1/responses")
        self.assertEqual(P._with_endpoint("https://api.openai.com", "/v1/chat/completions"),
                         "https://api.openai.com/v1/chat/completions")
        self.assertEqual(P._with_endpoint("https://api.anthropic.com", "/v1/messages"),
                         "https://api.anthropic.com/v1/messages")

    def test_online_usage_variants(self):
        self.assertEqual(P._online_usage("openai_chat", {
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}),
            {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "cached_tokens": 0})
        claude = P._online_usage("claude", {"usage": {
            "input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 2}})
        self.assertEqual(claude["input_tokens"], 15)
        self.assertEqual(claude["total_tokens"], 20)
        self.assertEqual(P._online_usage("ollama", {"prompt_eval_count": 8, "eval_count": 4}),
                         {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12, "cached_tokens": 0})

    def test_stream_delta_formats(self):
        self.assertEqual(P._stream_delta("openai_chat", {"choices": [{"delta": {"content": "a"}}]}),
                         ("a", ""))
        self.assertEqual(P._stream_delta("claude", {"type": "content_block_delta",
                                                    "delta": {"text": "b", "thinking": "t"}}),
                         ("b", "t"))
        self.assertEqual(P._stream_delta("codex_responses", {"type": "response.output_text.delta",
                                                             "delta": "c"}), ("c", ""))

    def test_reasoning_action_validation(self):
        self.assertEqual(P._reasoning_action('```json\n{"type": "tool", "tool": "pwsh", "arguments": {}}\n```'),
                         '{"type": "tool", "tool": "pwsh", "arguments": {}}')
        self.assertEqual(P._reasoning_action("普通推理文本"), "")
        self.assertEqual(P._reasoning_action('{"type": "tool", "tool": "", "arguments": {}}'), "")

    def test_content_helpers(self):
        self.assertEqual(P._content_text([{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]), "A\nB")
        self.assertEqual(P._text_value("x"), "x")

    def test_codex_responses_aggregated_dedup_and_filter(self):
        """聚合提取：completed 优先、同 id 去重、incomplete 排除、function_call 成 action。"""
        items = [
            {"type": "message", "id": "m1", "content": [{"type": "output_text", "text": "正文"}]},
            {"type": "reasoning", "id": "r1", "content": [{"type": "reasoning_text", "text": "思考"}]},
            {"type": "function_call", "call_id": "c1", "name": "pwsh",
             "arguments": json.dumps({"command": "dir"})},
        ]
        chunks = [
            {"type": "response.output_item.done", "item": items[0]},
            {"type": "response.output_item.done", "item": items[1]},
            {"type": "response.completed", "response": {"output": items}},
            {"type": "response.incomplete", "response": {"output": [
                {"type": "message", "id": "m2",
                 "content": [{"type": "output_text", "text": "被截断"}]}]}},
        ]
        text, reasoning, reasoning_id, action = P._codex_responses_aggregated(chunks)
        self.assertEqual(text, "正文", "completed 优先且同 id 去重，正文只出现一次")
        self.assertEqual(reasoning, "思考")
        self.assertEqual(reasoning_id, "r1")
        payload = json.loads(action)
        self.assertEqual(payload["type"], "tool")
        self.assertEqual(payload["tool"], "pwsh")
        self.assertEqual(payload["arguments"], {"command": "dir"})
        self.assertNotIn("被截断", text, "incomplete 不得作为回填来源")

    def test_codex_responses_aggregated_done_only_fallback(self):
        """completed 缺失时回退 output_item.done。"""
        chunks = [
            {"type": "response.output_item.done", "item": {
                "type": "message", "id": "m1",
                "content": [{"type": "output_text", "text": "仅 done"}]}},
        ]
        text, reasoning, reasoning_id, action = P._codex_responses_aggregated(chunks)
        self.assertEqual(text, "仅 done")
        self.assertEqual((reasoning, reasoning_id, action), ("", "", ""))

    def test_codex_responses_aggregated_incomplete_blocks_done_fallback(self):
        """incomplete 即使在 done 之后到达，也不能把部分 item 当成功结果。"""
        chunks = [
            {"type": "response.output_item.done", "item": {
                "type": "message", "id": "m1",
                "content": [{"type": "output_text", "text": "截断前内容"}]}},
            {"type": "response.incomplete", "response": {"output": []}},
        ]
        self.assertEqual(
            P._codex_responses_aggregated(chunks), ("", "", "", ""),
        )


if __name__ == "__main__":
    unittest.main()
