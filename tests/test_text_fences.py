# -*- coding: utf-8 -*-
"""围栏识别守门：naiba/core/text_fences（流式层与终态层共用的唯一口径）。

保护对象（都对应一条线上事故，缺一即回归）：

1. **偏移保持**——掩码后长度逐字符不变、换行位置不变，终态层才能
   「在掩码文本上定位 `{` → 到原文同一偏移取内容」；
2. **围栏内一律掩掉**（含围栏行本身），围栏外一律不动 ⇒ 协议标记只可能在掩码文本上命中；
3. **未闭合围栏掩到结尾**（模型被切断时没有闭合行，不能反过来把后续内容当协议）；
4. **行内代码不是围栏**（`x = ` + 反引号内容），否则一行行内代码会吞掉其后整段正文；
5. **整文剥围栏**只认「整条回答就是一块围栏」，正文中间的围栏不算。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core import text_fences  # noqa: E402


class MaskFencedCodeTests(unittest.TestCase):
    PROSE_WITH_JSON = (
        "先看这个示例：\n"
        "```json\n"
        '{"type": "tool", "tool": "pwsh", "arguments": {"command": "dir"}}\n'
        "```\n"
        "上面是写法示例，实际不需要现在执行。\n"
    )

    def test_offsets_are_preserved(self) -> None:
        masked = text_fences.mask_fenced_code(self.PROSE_WITH_JSON)
        self.assertEqual(len(masked), len(self.PROSE_WITH_JSON), "掩码必须等长（偏移保持）")
        self.assertEqual(masked.count("\n"), self.PROSE_WITH_JSON.count("\n"), "换行位置不得移动")
        for index, char in enumerate(self.PROSE_WITH_JSON):
            if char == "\n":
                self.assertEqual(masked[index], "\n")

    def test_fenced_body_is_masked_and_prose_survives(self) -> None:
        masked = text_fences.mask_fenced_code(self.PROSE_WITH_JSON)
        self.assertNotIn('"type"', masked, "围栏内的协议键必须被掩掉")
        self.assertNotIn("```", masked, "围栏行本身也要掩掉")
        self.assertIn("先看这个示例", masked)
        self.assertIn("上面是写法示例", masked)

    def test_marker_outside_fence_still_visible(self) -> None:
        text = "好的，现在执行：\n{\"type\": \"tool\", \"tool\": \"pwsh\"}\n"
        masked = text_fences.mask_fenced_code(text)
        self.assertIn('{"type"', masked, "围栏外的协议不得被掩掉")

    def test_tilde_fence_and_longer_close(self) -> None:
        text = "正文一\n~~~\n{\"type\": \"tool\"}\n~~~~\n正文二\n"
        masked = text_fences.mask_fenced_code(text)
        self.assertNotIn('"type"', masked)
        self.assertIn("正文一", masked)
        self.assertIn("正文二", masked)

    def test_unterminated_fence_masks_to_end(self) -> None:
        text = "正文一\n```python\nx = 1\n没闭合就到结尾"
        masked = text_fences.mask_fenced_code(text)
        self.assertIn("正文一", masked)
        self.assertNotIn("x = 1", masked)
        self.assertEqual(len(masked), len(text))

    def test_inline_code_is_not_a_fence(self) -> None:
        # 两个反引号不构成围栏；三个反引号但信息串里还有反引号也不构成围栏。
        marker_line = '{"type": "tool"}'
        for line in ("``x = 1``", "```x = `y```"):
            with self.subTest(line=line):
                text = line + "\n" + "后面的正文必须还在" + "\n" + marker_line + "\n"
                masked = text_fences.mask_fenced_code(text)
                self.assertIn("后面的正文必须还在", masked, "行内代码被当围栏 ⇒ 其后整段正文被吞")
                self.assertIn('{"type"', masked, "行内代码之后的协议仍应可见")

    def test_indented_fence_beyond_four_spaces_is_not_a_fence(self) -> None:
        text = "    ```\n{\"type\": \"tool\"}\n    ```\n正文\n"
        masked = text_fences.mask_fenced_code(text)
        self.assertIn('{"type"', masked, "缩进 4 空格是缩进代码块，不是围栏")


class FenceMarkerTests(unittest.TestCase):
    def test_open_rejects_backtick_in_info_string(self) -> None:
        self.assertIsNone(text_fences.fence_open("```x = `y``"))
        self.assertEqual(text_fences.fence_open("```json"), ("`", 3))
        self.assertEqual(text_fences.fence_open("~~~~"), ("~", 4))

    def test_close_requires_same_char_and_at_least_open_length(self) -> None:
        self.assertTrue(text_fences.fence_close("```", "`", 3))
        self.assertTrue(text_fences.fence_close("````", "`", 3), "闭合长度 ≥ 开启即合法")
        self.assertFalse(text_fences.fence_close("``", "`", 3))
        self.assertFalse(text_fences.fence_close("~~~", "`", 3))
        self.assertFalse(text_fences.fence_close("``` python", "`", 3), "闭合行只能有空白")


class UnwrapWholeResponseFenceTests(unittest.TestCase):
    """统一后的契约：整文围栏 ⇒ ``(inner, char)``；否则 ``None``（见维护说明「围栏 API 统一」）。"""

    def test_whole_response_fence_is_unwrapped(self) -> None:
        raw = '```json\n{"type": "tool", "tool": "pwsh", "arguments": {}}\n```'
        inner, char = text_fences.unwrap_whole_response_fence(raw)
        self.assertEqual(char, "`")
        self.assertIn('{"type"', inner)
        self.assertNotIn("```", inner)

    def test_whole_response_xml_fence_is_unwrapped(self) -> None:
        raw = '```xml\n<invoke name="pwsh"><parameter name="command">dir</parameter></invoke>\n```'
        inner, char = text_fences.unwrap_whole_response_fence(raw)
        self.assertEqual(char, "`")
        self.assertIn("<invoke", inner)

    def test_tilde_fence_reports_its_char(self) -> None:
        inner, char = text_fences.unwrap_whole_response_fence("~~~json\n{}\n~~~")
        self.assertEqual((inner, char), ("{}", "~"))

    def test_fence_in_the_middle_is_not_unwrapped(self) -> None:
        raw = '讲解：\n```json\n{"type": "tool", "tool": "pwsh"}\n```\n就这些。'
        self.assertIsNone(text_fences.unwrap_whole_response_fence(raw))

    def test_lone_fence_line_is_not_a_whole_response_fence(self) -> None:
        self.assertIsNone(text_fences.unwrap_whole_response_fence("```json"))
        self.assertIsNone(text_fences.unwrap_whole_response_fence("没有围栏"))


class TailAnchorTests(unittest.TestCase):
    """``only_fence_tail`` 收的是**解码结束位置**（``raw_decode`` 的 end），不是起始位置。"""

    def test_only_fence_tail_accepts_whitespace_and_closing_fence(self) -> None:
        text = '{"type":"tool"}   \n```\n   '
        self.assertTrue(text_fences.only_fence_tail(text, text.index("}") + 1))
        self.assertTrue(text_fences.only_fence_tail('{"a":1}\n', len('{"a":1}')))

    def test_only_fence_tail_rejects_trailing_prose(self) -> None:
        text = '{"type":"tool"}\n然后我说几句收尾。'
        self.assertFalse(text_fences.only_fence_tail(text, text.index("}") + 1))

    def test_only_fence_tail_closers_option(self) -> None:
        """``closers=True`` 放行协议**自身**的收尾标签（DeepSeek 包装方言要用）。

        ``</invoke>`` / ``</tool>`` 是协议的一部分，不是"后面还接了正文"；不放行会让
        已上线的包装方言整条退化成 parse_error。真正的正文仍然要拒。
        """
        text = '<tool name="x"><parameter name="p">1</parameter></tool>\n</invoke>'
        end = text.index("</tool>") + len("</tool>")
        self.assertTrue(text_fences.only_fence_tail(text, end, closers=True))
        self.assertFalse(text_fences.only_fence_tail(text, end), "默认口径不放行任何收尾标签")
        prose = text + "\n以上就是全部。"
        self.assertFalse(
            text_fences.only_fence_tail(prose, end, closers=True), "收尾标签之后有正文仍要拒"
        )

    def test_only_fence_tail_must_be_given_raw_text(self) -> None:
        """判据必须喂**原文**：掩码文本里一个孤立 ``` 会开假围栏、把尾部正文掩成空格。"""
        text = '{"type":"tool"}\n```\n这条先不执行。'
        end = text.index("}") + 1
        masked = text_fences.mask_fenced_code(text)
        self.assertFalse(text_fences.only_fence_tail(text, end))
        self.assertTrue(
            text_fences.only_fence_tail(masked, end),
            "这条断言是反向钉桩：说明为什么调用方不能传掩码文本",
        )

    def test_in_final_block(self) -> None:
        self.assertTrue(text_fences.in_final_block('正文\n{"type": "tool", "too', 3))
        self.assertFalse(
            text_fences.in_final_block('正文\n{"type": "tool"}\n\n后面还有一段话\n', 3),
            "空行之后又有内容 ⇒ 标记不在最后一段",
        )


class FenceScanStateTests(unittest.TestCase):
    """跨 chunk 续扫：流式层必须能把「这一片处于围栏内吗」作为状态带过去。"""

    def test_state_survives_across_chunks(self) -> None:
        first, state = text_fences.fence_scan("正文一\n```js\n{\"type\": \"tool\"}")
        self.assertTrue(state[0], "第一片结束时应仍处在围栏内")
        self.assertEqual(state[1:], ("`", 3))
        self.assertNotIn('"type"', first)
        second, state_after = text_fences.fence_scan("\n```\n正文二", in_fence=state[0], fence_char=state[1], fence_length=state[2])
        self.assertFalse(state_after[0], "闭合行把状态带回围栏外")
        self.assertIn("正文二", second)
        self.assertNotIn("```", second, "闭合围栏行本身也要掩掉")

    def test_default_state_is_outside_fence(self) -> None:
        masked, state = text_fences.fence_scan("{\"type\": \"tool\"}")
        self.assertEqual(state, (False, "", 0))
        self.assertIn('{"type"', masked)

    def test_offsets_stay_aligned_with_original_text(self) -> None:
        text = "a\n```\n{\"type\": \"tool\"}\n```\nb"
        masked, _state = text_fences.fence_scan(text)
        self.assertEqual(len(masked), len(text))
        self.assertEqual([i for i, c in enumerate(text) if c == "\n"], [i for i, c in enumerate(masked) if c == "\n"])


if __name__ == "__main__":
    unittest.main()
