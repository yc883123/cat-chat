# -*- coding: utf-8 -*-
"""守门：OneBot v11 协议层（NapCatQQ 反向 WebSocket）。

覆盖计划 §七 中属于协议层的几条：
- 事件字段校验（非法结构不进入任何业务分支）；
- 消息段白名单：文字/媒体/可忽略/必须回告的「不支持」；
- CQ 码字符串格式的兜底解析；
- 长回复切分：不超限、优先段落、代码围栏闭合与重开。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.channels import onebot  # noqa: E402


def private_event(message, **overrides):
    event = {
        "post_type": "message",
        "message_type": "private",
        "self_id": 10000,
        "message_id": 501,
        "user_id": 20000,
        "message": message,
    }
    event.update(overrides)
    return event


class ParseMessageEventTests(unittest.TestCase):
    def test_text_segments_join_into_body(self):
        parsed = onebot.parse_message_event(private_event([
            {"type": "text", "data": {"text": "第一段"}},
            {"type": "text", "data": {"text": "第二段"}},
        ]))
        self.assertEqual(parsed.text, "第一段\n第二段")
        self.assertEqual(parsed.account_id, "10000")
        self.assertEqual(parsed.qq_user_id, "20000")
        self.assertEqual(parsed.message_id, "501")
        self.assertEqual(parsed.scope, "private")

    def test_media_segments_are_classified(self):
        parsed = onebot.parse_message_event(private_event([
            {"type": "text", "data": {"text": "看图"}},
            {"type": "image", "data": {"file": "a.png", "url": "http://x/a.png"}},
            {"type": "record", "data": {"file": "b.amr", "url": "http://x/b.amr"}},
            {"type": "video", "data": {"file": "c.mp4", "url": "http://x/c.mp4"}},
            {"type": "file", "data": {"file": "d.pdf", "name": "d.pdf", "url": "http://x/d.pdf"}},
        ]))
        kinds = [item["kind"] for item in parsed.media]
        self.assertEqual(kinds, ["image", "audio", "video", "file"])
        self.assertEqual(parsed.unsupported, [])

    def test_unsupported_segment_is_reported_not_dropped(self):
        parsed = onebot.parse_message_event(private_event([
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "forward", "data": {"id": "x"}},
        ]))
        # 不静默丢弃：必须进 unsupported，由上层回告用户。
        self.assertEqual(parsed.unsupported, ["forward"])
        self.assertEqual(parsed.text, "看这个")

    def test_ignorable_segments_do_not_count_as_unsupported(self):
        parsed = onebot.parse_message_event(private_event([
            {"type": "at", "data": {"qq": "10000"}},
            {"type": "text", "data": {"text": "hi"}},
            {"type": "reply", "data": {"id": "9"}},
        ]))
        self.assertEqual(parsed.ignored, ["at", "reply"])
        self.assertEqual(parsed.unsupported, [])

    def test_non_message_events_are_rejected(self):
        with self.assertRaises(onebot.OneBotProtocolError):
            onebot.parse_message_event({"post_type": "meta_event", "meta_event_type": "heartbeat"})
        with self.assertRaises(onebot.OneBotProtocolError):
            onebot.parse_message_event({"post_type": "message", "message_type": "private"})

    def test_missing_required_fields_are_rejected(self):
        for missing in ("self_id", "message_id", "user_id"):
            event = private_event([{"type": "text", "data": {"text": "x"}}])
            event.pop(missing)
            with self.assertRaises(onebot.OneBotProtocolError):
                onebot.parse_message_event(event)

    def test_group_message_requires_group_id(self):
        event = private_event([{"type": "text", "data": {"text": "x"}}], message_type="group")
        with self.assertRaises(onebot.OneBotProtocolError):
            onebot.parse_message_event(event)
        event["group_id"] = 777
        parsed = onebot.parse_message_event(event)
        self.assertEqual(parsed.scope, "group")
        self.assertEqual(parsed.qq_group_id, "777")

    def test_cq_string_format_is_parsed(self):
        parsed = onebot.parse_message_event(private_event(
            "你好[CQ:image,file=a.png,url=http://x/a.png]世界"
        ))
        self.assertEqual(parsed.text, "你好\n世界")
        self.assertEqual(len(parsed.media), 1)
        self.assertEqual(parsed.media[0]["url"], "http://x/a.png")

    def test_cq_escapes_are_restored(self):
        segments = onebot.parse_cq_string("a&#91;b&#93;&#44;c&amp;d")
        self.assertEqual(segments[0]["data"]["text"], "a[b],c&d")

    def test_oversize_text_is_truncated_not_rejected(self):
        parsed = onebot.parse_message_event(private_event([
            {"type": "text", "data": {"text": "字" * (onebot.MAX_TEXT_CHARS + 50)}},
        ]))
        self.assertEqual(len(parsed.text), onebot.MAX_TEXT_CHARS)

    def test_private_message_is_always_triggered(self):
        parsed = onebot.parse_message_event(private_event([
            {"type": "text", "data": {"text": "你好"}},
        ]))
        self.assertTrue(parsed.triggered)

    def test_group_message_requires_mention_or_prefix(self):
        base = {"message_type": "group", "group_id": 777}
        silent = onebot.parse_message_event(private_event(
            [{"type": "text", "data": {"text": "大家好啊"}}], **base
        ))
        self.assertFalse(silent.triggered)
        mentioned = onebot.parse_message_event(private_event(
            [{"type": "at", "data": {"qq": "10000"}},
             {"type": "text", "data": {"text": "在吗"}}],
            **base,
        ))
        self.assertTrue(mentioned.triggered)
        self.assertEqual(mentioned.mentioned_ids, ["10000"])
        # at 别人不算触发。
        other = onebot.parse_message_event(private_event(
            [{"type": "at", "data": {"qq": "99999"}},
             {"type": "text", "data": {"text": "在吗"}}],
            **base,
        ))
        self.assertFalse(other.triggered)
        prefixed = onebot.parse_message_event(private_event(
            [{"type": "text", "data": {"text": "/cat 帮我看看"}}], **base
        ))
        self.assertTrue(prefixed.triggered)
        self.assertEqual(prefixed.text, "帮我看看")


class SplitMessageTests(unittest.TestCase):
    def test_short_text_is_single_segment(self):
        self.assertEqual(onebot.split_message("你好"), ["你好"])
        self.assertEqual(onebot.split_message("   "), [])

    def test_every_segment_respects_limit(self):
        text = "x" * 7000
        chunks = onebot.split_message(text, limit=1000)
        self.assertEqual(len(chunks), 7)
        self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))
        self.assertEqual("".join(chunks), text)

    def test_paragraph_boundary_preferred(self):
        text = ("段落" * 200 + "\n\n") * 4
        chunks = onebot.split_message(text, limit=500)
        self.assertTrue(all(len(chunk) <= 500 for chunk in chunks))
        # 段落边界优先：允许尾部余量，但每段都应是完整段落，而不是在句子中间断。
        for chunk in chunks:
            self.assertTrue(chunk.endswith("\n\n") or len(chunk) >= 250, repr(chunk[-20:]))

    def test_code_fence_is_closed_and_reopened(self):
        body = "\n".join(f"line{i}" for i in range(600))
        text = f"开头\n\n```python\n{body}\n```\n结束"
        chunks = onebot.split_message(text, limit=600)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 600 for chunk in chunks))
        # 每一段里出现的围栏行必须成对（不把半段代码当正文流出去）。
        for chunk in chunks:
            fences = [line for line in chunk.splitlines() if onebot._fence_marker(line)]
            self.assertEqual(len(fences) % 2, 0, chunk[:80])
        # 除首尾外的中间段都应该以重开的围栏开头。
        for chunk in chunks[1:-1]:
            self.assertTrue(chunk.startswith("```"), chunk[:40])
        self.assertIn("line0", chunks[0])
        self.assertIn("line599", chunks[-1])

    def test_single_overlong_line_is_hard_split(self):
        chunks = onebot.split_message("y" * 5000, limit=900)
        self.assertTrue(all(len(chunk) <= 900 for chunk in chunks))
        self.assertEqual("".join(chunks), "y" * 5000)


if __name__ == "__main__":
    unittest.main()
