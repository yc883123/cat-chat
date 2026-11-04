# -*- coding: utf-8 -*-
"""块边界扫描器（public/js/02b-markdown-stream.js）的行为单测：用 node 真实加载执行。

test_stream_rendering.py 只 grep 源码；这里补上真正的行为验证——分块结果、
围栏内空行不切、闭合围栏立即成块、增量分批扫描与一次性扫描等价。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "public" / "js" / "02b-markdown-stream.js"

# 在 node 里把 raw 按 chunks 分批喂给扫描器，收集稳定块与尾块（与 04-messages.js 同构）。
NODE_SCRIPT = r"""
import { pathToFileURL } from 'node:url';
const { scanMarkdownBlocks } = await import(pathToFileURL(process.env.MODULE_PATH).href);
function scanChunks(chunks) {
  const st = { pos: 0, fenceOpen: false, segStart: 0 };
  let raw = '';
  const out = [];
  for (const c of chunks) {
    raw += c;
    for (const b of scanMarkdownBlocks(raw, st)) out.push(raw.slice(b[0], b[1]));
  }
  out.push(raw.slice(st.segStart));
  return out;
}
const cases = JSON.parse(process.env.CASES);
console.log(JSON.stringify(cases.map((chunks) => scanChunks(chunks))));
"""


def scan_chunks(cases: list) -> list:
  env = dict(os.environ)
  env.update({
    "MODULE_PATH": str(MODULE),
    "CASES": json.dumps(cases, ensure_ascii=False),
    "PYTHONIOENCODING": "utf-8",
  })
  proc = subprocess.run(  # noqa: S603 - 固定 argv
    ["node", "--input-type=module", "-e", NODE_SCRIPT],
    capture_output=True, text=True, encoding="utf-8", env=env, timeout=60, check=True,
  )
  return json.loads(proc.stdout)


@unittest.skipIf(shutil.which("node") is None, "node 不可用")
class MarkdownStreamBlocksTests(unittest.TestCase):
  def assert_chunks(self, chunks: list, expected: list) -> None:
    self.assertEqual(scan_chunks([chunks])[0], expected)

  def test_paragraphs_split_on_blank_line(self) -> None:
    # 区间是 slice 语义：段落自带行尾换行（markdown() 对此不敏感）。
    self.assert_chunks(["第一段", "\n\n第二段"], ["第一段\n", "第二段"])

  def test_partial_line_stays_tail(self) -> None:
    # 没有换行就没有边界：半个词也要留在尾块里。
    self.assert_chunks(["一段很长的", "正文"], ["一段很长的正文"])

  def test_blank_lines_inside_fence_do_not_split(self) -> None:
    self.assert_chunks(
      ["```json\n{\n\n", '"a": 1}\n```', "\n\n后文"],
      ["```json\n{\n\n\"a\": 1}\n```\n", "后文"],
    )

  def test_closed_fence_is_immediate_boundary(self) -> None:
    # 闭合行一到代码块就完整：不必等后面的空行（与 markdown() 的占位符块一致）。
    self.assert_chunks(["```py\nx=1\n```", "\n正文"], ["```py\nx=1\n```\n", "正文"])

  def test_consecutive_blank_lines_produce_no_empty_block(self) -> None:
    self.assert_chunks(["甲\n", "\n\n\n乙"], ["甲\n", "乙"])

  def test_incremental_chunks_match_one_shot(self) -> None:
    doc = (
      "# 标题\n\n先说结论：**加粗**与`代码`。\n\n```json\n{\"a\": 1}\n\n```\n\n"
      "| 列 A | 列 B |\n| --- | --- |\n| 1 | 2 |\n\n- 甲\n- 乙\n- 丙\n\n（完）"
    )
    one_shot = scan_chunks([[doc]])[0]
    # 逐字分批（最坏的流式节奏）与一次性扫描必须得到同样的块序列。
    by_char = scan_chunks([list(doc)])[0]
    self.assertEqual(one_shot, by_char)
    # 空行只当边界吃掉，不进任何块；其余行必须原样保序。
    self.assertEqual(
      [line for line in "".join(one_shot).split("\n") if line],
      [line for line in doc.split("\n") if line],
    )


if __name__ == "__main__":
  unittest.main()
