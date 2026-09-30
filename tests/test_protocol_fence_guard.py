# -*- coding: utf-8 -*-
"""守门：正文里的「代码块示例」不是工具协议（流式层与终态层必须同一口径）。

病历（两处同源事故）：模型在回答里贴一段协议写法示例——
```` ```json {"type": "tool", "tool": "pwsh", ...} ``` ````——

1. **流式层**在示例处「吐出前文 + 判定协议」，该次响应剩余正文**一条 delta 都不再发**，
   界面正好停在围栏行（用户看到「说到 ``` 就没了」）；
2. **终态层**更严重：``_extract_json`` 对**任意位置**的 ``{`` 做 ``raw_decode``，
   把示例里的动作**直接执行**（示例命令真跑了一遍）。

两侧共用 ``naiba/core/text_fences`` 的口径（见 tests/test_text_fences.py），本文件钉死
四条边界，任何一侧改回「任意位置扫标记」都会红：

* 围栏内的示例不吞正文、不执行、不判 parse_error；
* **整条回答就是围栏包裹的协议**（既有支持）仍要执行；
* 真协议仍要执行（前言 + 协议是常见形态，不能被围栏改动误伤）；
* 协议后面还接正文 ⇒ **不执行**，整体按正文展示（「协议必须顶到回答尾」的取舍）；
* 三次解析失败不再只回固定文案，模型原文必须保留并标注「未完成」。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.llm.stream import StreamMixins  # noqa: E402
from naiba.skills.agent import SkillAgent  # noqa: E402


def sse(chunk: dict) -> bytes:
    return ("data: " + json.dumps(chunk, ensure_ascii=False)).encode("utf-8")


def sse_pieces(pieces: list[str]) -> list[bytes]:
    return [sse({"choices": [{"delta": {"content": piece}}]}) for piece in pieces]


def forwarded(events: list[dict]) -> str:
    return "".join(str(e.get("content") or "") for e in events if e.get("type") == "delta")


class _Catalog:
    def scan(self) -> list:
        return []


class StreamingFenceTests(unittest.TestCase):
    """流式层：围栏不能把正文截断。"""

    def _read(self, pieces: list[str]) -> tuple[str, str, list[dict]]:
        events: list[dict] = []
        result = StreamMixins._read_sse_response(sse_pieces(pieces), "openai_chat", events.append)
        return result["content"], forwarded(events), events

    def test_json_fence_example_keeps_streaming(self) -> None:
        pieces = [
            "先看这个示例：\n```json\n",
            '{"type": "tool", "tool": "pwsh", "arguments": {"command": "dir"}}\n',
            "```\n上面只是写法示例，现在不需要执行。\n",
        ]
        content, emitted, _events = self._read(pieces)
        self.assertEqual(emitted, "".join(pieces), "围栏之后的正文必须照常下发，一段都不能少")
        self.assertIn("上面只是写法示例", emitted, "旧行为：界面正好停在 ``` 那一行")
        self.assertEqual(SkillAgent._parse_action(content)["type"], "final")

    def test_xml_fence_example_keeps_streaming(self) -> None:
        pieces = [
            "XML 方言长这样：\n```xml\n<tool name=\"pwsh\">",
            "<parameter name=\"command\">dir</parameter>\n```\n说明到此为止。",
        ]
        content, emitted, _events = self._read(pieces)
        self.assertEqual(emitted, "".join(pieces))
        self.assertEqual(SkillAgent._parse_action(content)["type"], "final")

    def test_fence_marker_split_across_chunks(self) -> None:
        """围栏标记本身被 SSE 拆成两包（`` + ``）也不能被当协议起点。"""
        pieces = ["说明：\n`", "``\n{\"type\": \"tool\"}\n", "```\n后面还有正文。"]
        content, emitted, _events = self._read(pieces)
        self.assertEqual(emitted, "".join(pieces), "拆包的围栏不能让正文停在半行")
        self.assertIn("后面还有正文", emitted)
        self.assertEqual(SkillAgent._parse_action(content)["type"], "final")

    def test_unclosed_fence_flushes_everything_at_stream_end(self) -> None:
        """回答被切断、围栏没闭合 ⇒ 围栏后面的内容一律按正文，不得判协议。"""
        pieces = ["例子：\n```json\n", "{\"type\": \"tool\", \"tool\": \"pwsh\"}\n"]
        content, emitted, _events = self._read(pieces)
        self.assertEqual(emitted, "".join(pieces))
        action = SkillAgent._parse_action(content)
        self.assertEqual(action["type"], "final")
        self.assertIn('"tool": "pwsh"', action["content"], "未闭合围栏里的示例必须原样留在正文")

    def test_real_protocol_after_preface_is_still_hidden(self) -> None:
        """回归：前言 + 真协议（无围栏）仍是协议——前言发出、协议不外发。"""
        pieces = [
            "好的，现在读取文件：\n",
            '{"type": "tool", "tool": "read_file", "arguments": {"path": "D:\\\\work\\\\a.txt"}}',
        ]
        content, emitted, events = self._read(pieces)
        self.assertIn("好的，现在读取文件：", emitted)
        self.assertNotIn('"type": "tool"', emitted, "协议本体绝不能进 delta")
        self.assertFalse(any('"type": "tool"' in str(e.get("content") or "") for e in events))
        action = SkillAgent._parse_action(content)
        self.assertEqual(action["type"], "tool")
        self.assertEqual(action["tool"], "read_file")


class TerminalFenceTests(unittest.TestCase):
    """终态层 ``_parse_action``：围栏内外、尾锚定的完整判定矩阵。"""

    def test_json_fence_example_is_not_executed(self) -> None:
        text = (
            "要调工具可以这样写：\n```json\n"
            '{"type": "tool", "tool": "pwsh", "arguments": {"command": "Remove-Item * -Recurse"}}\n'
            "```\n以上是协议形状，我不会现在执行。\n"
        )
        action = SkillAgent._parse_action(text)
        self.assertEqual(action["type"], "final", "围栏里的示例被当动作执行 = 直接跑危险命令")
        self.assertIn("Remove-Item", action["content"], "示例内容必须原样留在正文里")

    def test_tool_key_fence_example_is_not_executed(self) -> None:
        text = (
            "字段说明如下：\n```json\n{\"tool\": \"pwsh\", \"arguments\": {}}\n```\n"
            "注意 tool 键的值是工具名。"
        )
        self.assertEqual(SkillAgent._parse_action(text)["type"], "final")

    def test_xml_fence_example_is_not_executed(self) -> None:
        text = (
            "DeepSeek 方言：\n```xml\n"
            '<tool name="pwsh"><parameter name="command">dir</parameter></tool>\n'
            "```\n这只是示例。"
        )
        self.assertEqual(SkillAgent._parse_action(text)["type"], "final")

    def test_harmony_fence_example_is_not_protocol(self) -> None:
        text = (
            "Kimi 的保留 token 形如：\n```\n"
            '<|open|>tools<|sep|><|open|>call tool="pwsh" index="1"\n'
            "```\n我不会执行它。"
        )
        self.assertEqual(SkillAgent._parse_action(text)["type"], "final")

    def test_whole_response_json_fence_still_executes(self) -> None:
        """既有支持：整条回答就是 ```json 包裹的协议 ⇒ 必须仍然执行（不能被围栏口径堵死）。"""
        raw = '```json\n{"type": "tool", "tool": "read_file", "arguments": {"path": "a.txt"}}\n```'
        action = SkillAgent._parse_action(raw)
        self.assertEqual(action["type"], "tool")
        self.assertEqual(action["tool"], "read_file")
        self.assertEqual(action["arguments"], {"path": "a.txt"})

    def test_whole_response_xml_fence_still_executes(self) -> None:
        raw = '```xml\n<invoke name="pwsh"><parameter name="command">dir</parameter></invoke>\n```'
        action = SkillAgent._parse_action(raw)
        self.assertEqual(action["type"], "tool")
        self.assertEqual(action["tool"], "pwsh")
        self.assertEqual(action["arguments"], {"command": "dir"})

    def test_preface_plus_real_action_executes(self) -> None:
        text = '好的，现在读取文件：\n{"type": "tool", "tool": "read_file", "arguments": {"path": "a.txt"}}'
        action = SkillAgent._parse_action(text)
        self.assertEqual(action["type"], "tool")
        self.assertEqual(action["arguments"], {"path": "a.txt"})

    def test_action_followed_by_prose_is_not_executed(self) -> None:
        """「协议必须顶到回答尾」的取舍：动作后还接正文 ⇒ 不执行，正文完整展示。"""
        text = (
            '{"type": "tool", "tool": "pwsh", "arguments": {"command": "dir"}}\n\n'
            "这条命令我先不执行，等你确认后再跑。"
        )
        action = SkillAgent._parse_action(text)
        self.assertEqual(action["type"], "final", "静默执行动作并丢掉正文的旧行为不能回来")
        self.assertIn('"command": "dir"', action["content"])
        self.assertIn("等你确认", action["content"])

    def test_bare_json_in_the_middle_is_prose(self) -> None:
        text = '先说结论：配置项 {"type": "tool", "tool": "pwsh"} 只是这个动作的形状，后面我还要解释两句。'
        self.assertEqual(SkillAgent._parse_action(text)["type"], "final")

    def test_truncated_json_action_is_still_parse_error(self) -> None:
        text = '我来执行：\n{"type": "tool", "tool": "pwsh", "arguments": {"comm'
        self.assertEqual(SkillAgent._parse_action(text), {"type": "parse_error"})

    def test_truncated_xml_action_is_still_parse_error(self) -> None:
        text = '我来执行：\n<tool name="pwsh"><parameter name="comm'
        self.assertEqual(SkillAgent._parse_action(text), {"type": "parse_error"})

    def test_plain_json_answer_is_not_protocol(self) -> None:
        self.assertEqual(SkillAgent._parse_action('{"answer": "就是这两个数"}')["type"], "final")


class ParseErrorPreservesProseTests(unittest.TestCase):
    """三次解析失败：模型原文必须保留（旧行为只回固定文案，正文整体丢失）。"""

    def _run(self, outputs: list[str]) -> tuple[str, dict]:
        events: list[dict] = []
        call_index = {"n": 0}

        def complete(profile, messages, options, event):
            index = call_index["n"]
            call_index["n"] += 1
            return outputs[index % len(outputs)]

        context: dict = {"run_id": "run-parse-error"}
        worker = SkillAgent(_Catalog(), None, complete, None)
        response, _runs, _reasonings, _usage = worker.run(
            "跑一下目录列表",
            [],
            {"kind": "online", "model": "m", "context_window": 128000},
            {"max_steps": 8, "stream": False},
            {"mode": "auto", "skill_ids": []},
            [],
            "",
            [],
            lambda payload: events.append(payload),
            None,
            8,
            None,
            context,
        )
        self.assertTrue(events, "至少要有事件")
        self.assertTrue(context.get("partial"), "必须经 run_context['partial'] 落「未完成」标记")
        return response, context

    def test_raw_output_survives_three_parse_failures(self) -> None:
        broken = '好的，我来列目录：\n{"type": "tool", "tool": "pwsh", "arguments": {"comm'
        response, context = self._run([broken])
        self.assertIn("我来列目录", response, "模型这一轮的正文不能被丢掉")
        self.assertIn(broken, response, "原文必须原样保留")
        self.assertIn("连续三次重试失败", response, "必须另起一段说明为什么停止")
        self.assertEqual(context["partial"]["reason"], "tool_protocol_parse_error")

    def test_empty_output_still_explains_itself(self) -> None:
        response, _context = self._run(['{"type": "tool", "tool": "pwsh", "arg'])
        self.assertIn("连续三次重试失败", response)


if __name__ == "__main__":
    unittest.main()
