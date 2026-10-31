# -*- coding: utf-8 -*-
"""流式回复渲染守门：增量到达时不能因全量 Markdown 重排把页面卡住。"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StreamingRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (ROOT / "public" / "js" / "04-messages.js").read_text(encoding="utf-8")

    @classmethod
    def _body(cls) -> str:
        start = cls.source.index("export function scheduleStreamingMarkdown(")
        rest = cls.source[start:]
        match = re.search(r"\n(?:export )?function ", rest[1:])
        return rest if match is None else rest[: match.start() + 1]

    def test_streaming_render_is_frame_coalesced(self) -> None:
        body = self._body()
        self.assertIn("requestAnimationFrame", body)
        self.assertIn("element.textContent = element.dataset.raw || ''", body)
        self.assertIn("if (!element.isConnected) return", body)

    def test_streaming_render_does_not_reparse_markdown(self) -> None:
        body = self._body()
        self.assertNotIn("markdown(", body)
        self.assertNotIn("element.innerHTML", body)
        self.assertNotIn("setTimeout", body)

    def test_final_message_path_still_owns_markdown_rendering(self) -> None:
        """流式阶段只负责跟手显示，最终消息仍由正常消息渲染器排版。"""
        self.assertIn("markdown(message.content", self.source)
        self.assertIn("renderMessages", self.source)


if __name__ == "__main__":
    unittest.main()
