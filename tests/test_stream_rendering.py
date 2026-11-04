# -*- coding: utf-8 -*-
"""流式回复渲染守门：块级增量渲染（2026-11-04 起替代 09b3de0d 的「终态才渲染」取舍）。

契约：
- 流式阶段按「空行 + 围栏闭合」切块，稳定块只渲染一次（只做 DOM 追加，不整体替换）；
- 每帧只重渲染最后一个未完结块（尾块），且尾块代码不做语法高亮（块稳定后补一次）；
- 扫描器（02b-markdown-stream.js）是零依赖纯函数，node 可直接加载做行为单测
  （见 test_markdown_stream_blocks.py）；
- 终态消息仍由 renderMessages 按 markdown() 全量排版。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StreamingRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (ROOT / "public" / "js" / "04-messages.js").read_text(encoding="utf-8")
        cls.scanner = (ROOT / "public" / "js" / "02b-markdown-stream.js").read_text(encoding="utf-8")
        cls.markdown_js = (ROOT / "public" / "js" / "02-markdown.js").read_text(encoding="utf-8")

    @classmethod
    def _body(cls) -> str:
        """流式渲染的源码区间：块级增量注释块起到下一个领域函数（getStreamingProseSegment）为止。"""
        start = cls.source.index("// ---- 流式正文的块级增量渲染 ----")
        end = cls.source.index("export function getStreamingProseSegment(")
        return cls.source[start:end]

    def test_streaming_render_is_frame_coalesced(self) -> None:
        body = self._body()
        self.assertIn("requestAnimationFrame", body)
        self.assertIn("if (!element.isConnected) return", body)

    def test_streaming_render_is_block_incremental(self) -> None:
        body = self._body()
        # 块边界来自零依赖扫描器，渲染状态按元素存 WeakMap。
        self.assertIn("scanMarkdownBlocks(", body)
        self.assertIn("streamingRenderStates = new WeakMap()", body)
        # 旧全量替换（每个节流周期 innerHTML = markdown(累计全文)）不得回归。
        self.assertNotIn("element.innerHTML = markdown(", body)
        # 稳定块只追加：节点插到尾块之前，已渲染内容不参与后续重排版。
        self.assertIn("insertBefore", body)

    def test_streaming_tail_skips_code_highlight(self) -> None:
        body = self._body()
        self.assertIn("highlight: false", body)
        # markdown() 必须真的支持停高亮，尾块才能省掉高亮正则。
        self.assertIn("opts.highlight === false", self.markdown_js)

    def test_external_overwrite_resets_stream_state(self) -> None:
        """moveBottomProseInline 整体重写 innerHTML，必须作废块级渲染状态。"""
        start = self.source.index("export function moveBottomProseInline(")
        end = self.source.index("export function", start + 10)
        body = self.source[start:end]
        self.assertIn("streamingRenderStates.delete(", body)

    def test_block_scanner_stays_zero_dependency(self) -> None:
        """扫描器保持零 import、零 DOM 引用，node 才能直接加载做行为单测。"""
        self.assertNotRegex(self.scanner, r"(?m)^\s*import\s")
        self.assertNotIn("document.", self.scanner)
        self.assertNotIn("window.", self.scanner)

    def test_final_message_path_still_owns_markdown_rendering(self) -> None:
        """流式块级渲染只是过程显示，最终消息仍由正常消息渲染器排版。"""
        self.assertIn("markdown(message.content", self.source)
        self.assertIn("renderMessages", self.source)


if __name__ == "__main__":
    unittest.main()
