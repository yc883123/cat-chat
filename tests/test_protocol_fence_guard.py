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

    def test_whitespace_only_delta_does_not_crash(self) -> None:
        """纯空格 delta（缩进 / 两空格硬换行被拆包）不得打死整条流。

        旧写法在 ``_possible_fence_suffix_length`` 里对空行做 ``body[0]``：整行只有 1–3 个
        空格时越界抛 IndexError。IndexError 不在 runtime 的重试 except 列表里、Agent 也只捕
        RuntimeError ⇒ 用户看到「请求失败：string index out of range」，整轮回答作废且不重试。
        """
        for pieces in (
            ["让我看看：\n", "  ", "接下来是第二行。\n"],
            ["甲\n", " ", "乙"],
            ["缩进：\n", "   ", "粗体"],
        ):
            content, emitted, _events = self._read(pieces)
            self.assertEqual(emitted, "".join(pieces), f"纯空格拆包不得丢正文：{pieces!r}")
            # content 走 _clean_content（末尾空白会 strip），这里只关心"一个字都没丢"。
            self.assertEqual(content, "".join(pieces).strip())

    def test_closing_fence_split_across_chunks(self) -> None:
        """闭合围栏被 SSE 拆成两包（`` ` `` + `` `` ``）也必须认出来。

        半截闭合行漏留 ⇒ 被当正文发出去、``_advance`` 又按整行扫 ⇒ 闭合认不出、
        ``_in_fence`` 永久停在围栏内，之后真正的协议会被当成围栏里的代码正文整套放行。
        """
        protocol = '{"type": "tool", "tool": "pwsh", "arguments": {"command": "dir"}}'
        for middle in (["``", "`\n"], ["`", "``\n"]):
            pieces = ["说明：\n```json\n{\"a\": 1}\n", *middle, protocol]
            content, emitted, _events = self._read(pieces)
            self.assertNotIn('"command"', emitted, f"协议本体不得进 delta：{middle!r}")
            self.assertTrue(emitted.endswith("```\n"), f"协议前半截不得外发：{emitted!r}")
            self.assertEqual(SkillAgent._parse_action(content)["tool"], "pwsh")

    def test_closing_fence_and_protocol_prefix_in_one_chunk(self) -> None:
        """「闭合围栏 + 协议前几个字符」落在同一块：keep 必须按本块**结束**状态判。

        用进入本块时的状态会因为「块内刚闭合围栏」而漏保 —— 协议前缀一旦流出，
        下一块里就没有 ``{`` 可锚，协议其余部分全部按正文外发。
        """
        pieces = [
            "说明：\n```json\n{\"a\": 1}\n",
            "```\n{\"ty",
            'pe": "tool", "tool": "pwsh", "arguments": {"command": "dir"}}',
        ]
        content, emitted, _events = self._read(pieces)
        self.assertNotIn('"tool"', emitted, f"协议前缀不得外发：{emitted!r}")
        self.assertEqual(SkillAgent._parse_action(content)["tool"], "pwsh")

    def test_guard_state_does_not_depend_on_status_callback(self) -> None:
        """status=None（视觉识别 / 子代理路径）时围栏状态同样必须推进。

        状态推进挂在 ``if status`` 上 ⇒ 同一个输入有没有 UI 回调会走出两种状态机。
        """
        from naiba.llm.stream import _ProtocolStreamGuard

        pieces = ["前言：\n```json\n", '{"type": "tool", "tool": "pwsh"}\n', "```\n后面还有正文"]
        detected: list[bool] = []
        for with_callback in (True, False):
            guard = _ProtocolStreamGuard()
            sink = (lambda _event: None) if with_callback else None
            for piece in pieces:
                guard.feed(piece, sink)
            guard.finish(sink)
            detected.append(guard.detected)
        self.assertEqual(detected[0], detected[1], "有没有 UI 回调不能改变守卫的状态机")

    def test_tab_indented_fence_split_across_chunks(self) -> None:
        """Tab 缩进的围栏拆包也不能泄漏协议（计划 §2.1）。

        ``text_fences`` 允许围栏行用最多 3 个空格**或制表符**缩进，但旧写法
        ``_possible_fence_suffix_length`` 只 ``lstrip(" ")``：带 Tab 缩进的半个闭合行
        ``"\\t``"`` 不被保留 ⇒ 半截当正文外发、``_advance`` 按整行扫认不出闭合 ⇒
        ``_in_fence`` 永久停在围栏内，之后的真实协议被当成围栏里的代码正文整套放行。
        """
        protocol = '{"type": "tool", "tool": "pwsh", "arguments": {"command": "dir"}}'
        pieces = ["\t```json\n", '{"a": 1}\n', "\t``", "`\n", protocol]
        content, emitted, _events = self._read(pieces)
        # 围栏内的示例 JSON 属于可见正文；闭合围栏行之后不得再出现协议。
        self.assertTrue(emitted.endswith("\t```\n"), f"协议前半截不得外发：{emitted!r}")
        self.assertNotIn('"command"', emitted, f"围栏后的真实协议不得进 delta：{emitted!r}")
        self.assertNotIn('"tool"', emitted, "只有围栏里的示例可以出现，真协议必须被吞")
        self.assertEqual(emitted.count('"a": 1'), 1, "围栏里的示例必须原样可见")
        self.assertEqual(SkillAgent._parse_action(content)["tool"], "pwsh")

    def test_tab_indented_opening_fence_split_across_chunks(self) -> None:
        """Tab 缩进的**开启**围栏被拆包：正文照常外发，围栏内的示例不判协议。"""
        pieces = ["说明：\n\t`", "``json\n", '{"a": 1}\n', "\t```\n", "后面还有正文。"]
        content, emitted, _events = self._read(pieces)
        self.assertEqual(emitted, "".join(pieces), "带缩进的开启围栏不得让正文停在半行")
        self.assertIn("后面还有正文", emitted)
        self.assertEqual(SkillAgent._parse_action(content)["type"], "final")

    def test_tab_indented_closing_fence_then_real_protocol(self) -> None:
        """Tab 缩进的闭合围栏之后紧接真实 JSON / XML / Harmony 协议，一律不得进 delta。"""
        json_protocol = '{"type": "tool", "tool": "pwsh", "arguments": {"command": "dir"}}'
        xml_protocol = '<invoke name="pwsh"><parameter name="command">dir</parameter></invoke>'
        harmony_protocol = (
            '<|open|>tools<|sep|><|open|>call tool="pwsh" index="1"<|sep|>'
            '<|open|>argument key="command" type="string"<|sep|>Get-Process'
            '<|close|>argument<|sep|><|close|>call<|sep|>'
            '<|close|>tools<|sep|><|end_of_text|>'
        )
        cases = [
            (json_protocol, '"command"', '{"a": 1}'),
            (xml_protocol, "<invoke", "<x/>"),
            (harmony_protocol, "<|open|>", "随便写点"),
        ]
        for protocol, leak_marker, example_body in cases:
            with self.subTest(protocol=leak_marker):
                pieces = [
                    f"示例：\n\t```\n{example_body}\n",
                    "\t``",
                    "`\n",
                    protocol,
                ]
                content, emitted, _events = self._read(pieces)
                self.assertNotIn(leak_marker, emitted, f"协议不得进 delta：{emitted!r}")
                self.assertTrue(emitted.endswith("\t```\n"), f"协议前半截不得外发：{emitted!r}")
                self.assertEqual(SkillAgent._parse_action(content)["tool"], "pwsh")

    def test_tab_indented_fence_status_none_matches_callback(self) -> None:
        """Tab 缩进围栏拆包时，``status=None`` 与有回调两条路径的状态机必须一致。

        ``status=None`` 是真实路径（视觉识别 / 子代理），此时没有 delta 事件可记录，
        所以比的是守卫的**状态**（detected / pending / 围栏状态），不是事件流。
        """
        from naiba.llm.stream import _ProtocolStreamGuard

        pieces = [
            "前言：\n\t```json\n",
            '{"a": 1}\n',
            "\t``",
            "`\n",
            '{"type": "tool", "tool": "pwsh"}',
        ]
        states: list[tuple] = []
        emitted_with_callback = ""
        for with_callback in (True, False):
            guard = _ProtocolStreamGuard()
            events: list[dict] = []
            sink = events.append if with_callback else None
            for piece in pieces:
                guard.feed(piece, sink)
            guard.finish(sink)
            states.append(
                (
                    guard.detected,
                    guard.pending,
                    guard._in_fence,
                    guard._fence_char,
                    guard._fence_length,
                )
            )
            if with_callback:
                emitted_with_callback = forwarded(events)
        self.assertEqual(states[0], states[1], "有没有 UI 回调不能改变守卫的状态机")
        self.assertTrue(states[0][0], "Tab 缩进围栏拆包后必须识别出真实协议")
        self.assertNotIn('"type": "tool"', emitted_with_callback)
        self.assertTrue(emitted_with_callback.endswith("\t```\n"))


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

    # ---- 「形状完整」与「被切断」必须分开（否则已上线方言退化成三次重试）----

    DEEPSEEK_WRAPPED = (
        '<tool type="tool">\n'
        '<tool name="read_file">\n'
        '<parameter name="path">D:\\素材\\单元03.md</parameter>\n'
        '<parameter name="max_chars">30000</parameter>\n'
        '</tool>\n'
        '</invoke>'
    )

    def test_deepseek_wrapped_named_tool_still_executes(self) -> None:
        """回归：已上线的 DeepSeek 包装方言（外层 ``<tool type="tool">`` + ``</invoke>`` 收尾）。

        取到内层 ``<tool name=…>…</tool>`` 之后尾部落的是外层收尾标签；尾锚定若只放行
        「空白 / 围栏行」，整条方言会被判成"协议后面还接正文"，最终连续三次解析失败、
        工具不再执行（该支持由 001bfdb 引入，原测试文件已删除，这里是唯一覆盖）。
        """
        action = SkillAgent._parse_action(self.DEEPSEEK_WRAPPED)
        self.assertEqual(action["type"], "tool", "包装方言不得退化成 parse_error")
        self.assertEqual(action["tool"], "read_file")
        self.assertEqual(action["arguments"], {"path": "D:\\素材\\单元03.md", "max_chars": 30000})

    def test_named_tool_with_outer_closer_still_executes(self) -> None:
        """同名收尾标签（``</tool>``）同样属于协议自身，不算"后面还接正文"。"""
        text = '<tool name="pwsh"><parameter name="command">dir</parameter></tool></tool>'
        self.assertEqual(SkillAgent._parse_action(text)["tool"], "pwsh")

    def test_named_tool_with_unknown_closer_is_not_executed(self) -> None:
        """未知关闭标签（``</evil>``）是动作**之后**的正文 ⇒ 不执行，整体按正文展示（计划 §2.2）。

        旧写法 ``_CLOSING_TAG`` 放行任意 ``</name>``：``</evil>`` 被当成"协议自身的收尾"，
        尾锚定误判通过 ⇒ 动作被执行、真正跟在动作后面的正文消失。
        """
        text = (
            '<tool name="read_file"><parameter name="path">x</parameter></tool>\n'
            "</evil>"
        )
        action = SkillAgent._parse_action(text)
        self.assertEqual(action["type"], "final", "未知关闭标签不得让动作绕过尾锚定")
        self.assertIn("</evil>", action["content"], "未知标签必须原样留在正文里")

    def test_named_tool_with_unknown_closer_and_prose_is_not_executed(self) -> None:
        """``</evil>`` 之后再接正文同样不执行（双重否决）。"""
        text = (
            '<tool name="read_file"><parameter name="path">x</parameter></tool>\n'
            "</evil>\n这段先不执行。"
        )
        action = SkillAgent._parse_action(text)
        self.assertEqual(action["type"], "final")
        self.assertIn("这段先不执行", action["content"])

    def test_invoke_with_whitelisted_outer_closer_still_executes(self) -> None:
        """白名单收尾标签（``</invoke>`` / ``</tool_calls>``）仍属协议自身，保持兼容。"""
        text = ('<tool_calls><invoke name="pwsh">'
                '<parameter name="command">dir</parameter></invoke></tool_calls>')
        action = SkillAgent._parse_action(text)
        self.assertEqual(action["type"], "tool")
        self.assertEqual(action["tool"], "pwsh")
        self.assertEqual(action["arguments"], {"command": "dir"})

    def test_complete_named_tool_with_trailing_prose_is_final(self) -> None:
        """形状**完整**的动作后面接正文 ⇒ 按正文展示，不进 parse_error 重试。

        旧写法 XML 分支无条件 True，「完整动作 + 正文」被误判成"想发却发不出来"，
        白白重试三次再把原文标成「未完成」——与 JSON 分支口径相反。
        """
        text = '<tool name="pwsh"><parameter name="command">dir</parameter></tool>\n这就是全部。'
        action = SkillAgent._parse_action(text)
        self.assertEqual(action["type"], "final")
        self.assertIn("这就是全部", action["content"])

    def test_xml_invoke_with_trailing_prose_is_final(self) -> None:
        """``<invoke>`` 方言与命名工具同口径：完整动作 + 正文 ⇒ final（不是 parse_error）。"""
        text = ('<invoke name="pwsh"><parameter name="command">dir</parameter></invoke>'
                '\n\n先不执行。')
        self.assertEqual(SkillAgent._parse_action(text)["type"], "final")

    def test_tool_calls_multi_invoke_keeps_parse_error(self) -> None:
        """多 call 的 ``<tool_calls>`` 包装块本实现不执行 ⇒ 必须保住有界重试。

        若把它算成"完整协议按正文展示"，模型就再也没机会改成单 call。
        """
        text = (
            '<tool_calls>\n'
            '<invoke name="pwsh"><parameter name="command">dir</parameter></invoke>\n'
            '<invoke name="pwsh"><parameter name="command">pwd</parameter></invoke>\n'
            '</tool_calls>'
        )
        self.assertEqual(SkillAgent._parse_action(text), {"type": "parse_error"})

    def test_fenced_xml_example_plus_real_trailing_invoke_executes(self) -> None:
        """围栏里的示例不参与定位，但**尾部**的真协议仍要能执行。

        旧写法对整段文本做 ``ET.fromstring``：前面有围栏示例就整体解析失败 ⇒ 尾部真协议
        被判成"被切断"；更早的版本则相反，会把围栏里的示例执行掉。
        """
        text = (
            '示例：\n```xml\n<tool name="x"><parameter name="p">1</parameter></tool>\n```\n'
            '现在执行：\n<invoke name="pwsh"><parameter name="command">dir</parameter></invoke>'
        )
        action = SkillAgent._parse_action(text)
        self.assertEqual(action["type"], "tool")
        self.assertEqual(action["tool"], "pwsh")
        self.assertEqual(action["arguments"], {"command": "dir"})

    def test_pretty_printed_truncated_json_is_parse_error(self) -> None:
        """pretty-print 且被切断的动作中间带空行 ⇒ 仍要进有界重试。

        ``in_final_block``（标记之后没有空行）看的是排版信号，会被动作自己内部的空行骗过；
        改用结构信号（括号没配平）后，被切断的动作不会静默当正文落库。
        """
        text = '我来执行：\n{"type": "tool",\n\n  "tool": "pwsh", "arguments": {'
        self.assertEqual(SkillAgent._parse_action(text), {"type": "parse_error"})

    def test_protocol_then_lone_fence_then_prose_is_final(self) -> None:
        """尾锚定必须看**原文**：动作后面一个孤立的 ``` 不得把尾部正文掩掉。

        判据传掩码文本时，那个 ``` 会开启假围栏、把它后面的正文一起掩成空格 ⇒
        锚定误判通过 ⇒ 动作被执行、尾部正文从界面消失（正是要消灭的"静默执行 + 吞正文"）。
        """
        text = ('{"type": "tool", "tool": "pwsh", "arguments": {"command": "dir"}}\n'
                '```\n这条命令我先不执行，等你确认。')
        action = SkillAgent._parse_action(text)
        self.assertEqual(action["type"], "final")
        self.assertIn("等你确认", action["content"])

    def test_harmony_end_token_tail_still_executes(self) -> None:
        """收尾 token 是一族：relay 未必剥掉的 ``<|end_of_text|>`` / ``<|eot_id|>`` 也要认。

        少认一个就等于把**完整**的动作判成"被切断"，连续三次解析失败后工具不再执行。
        """
        body = (
            '<|open|>tools<|sep|><|open|>call tool="pwsh" index="1"<|sep|>'
            '<|open|>argument key="command" type="string"<|sep|>Get-Process'
            '<|close|>argument<|sep|><|close|>call<|sep|>'
        )
        for tail in ('<|close|>tools<|sep|><|end_of_text|>', '<|close|>tools<|sep|><|eot_id|>'):
            action = SkillAgent._parse_action(body + tail)
            self.assertEqual(action["type"], "tool", f"收尾 token 未被识别：{tail!r}")
            self.assertEqual(action["tool"], "pwsh")


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
