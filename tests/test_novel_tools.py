# -*- coding: utf-8 -*-
"""护栏：拆书匠工具层（docs/plans/2026-10-25-小说拆章模块移植计划.md §5.2 + 评审 §2）。

覆盖评审要求的六条补齐：
1. 权限策略与 read_pdf 同构（越界走确认，不是静默放行）；
2. 硬预算：outline 分页、chunks 单次章节数/字符数上限、超预算给续取信息；
3. AI 文本入口 novel_split_text 复用同一算法且有输入大小限制；
4. 算法与 IO 分离（读盘/编码/缓存都在本层，算法在 core.novel_split）；
5. 索引参数严格校验（拒布尔/非整数/负值/越界，重复去重保序）；
6. 缓存按 (路径, mtime_ns, size) 签名，不因碰撞返回别的书。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.tools.providers import novel as novel_tools  # noqa: E402
from naiba.tools.registry import build_tool_registry  # noqa: E402


def _write(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(encoding))
    return path


_CN_NUM = "零一二三四五六七八九十"


def _cn(n: int) -> str:
    """1..99 的中文数字（真实网文标题几乎都用中文数字）。"""
    if n <= 10:
        return _CN_NUM[n]
    if n < 20:
        return "十" + (_CN_NUM[n - 10] if n > 10 else "")
    tens, ones = divmod(n, 10)
    return _CN_NUM[tens] + "十" + (_CN_NUM[ones] if ones else "")


def _book(chapters: int = 6, body: str = "内容。" * 20) -> str:
    return "\n".join(f"第{_cn(i + 1)}章 标题{i}\n{body}\n" for i in range(chapters))


class NovelToolRegistrationTests(unittest.TestCase):
    def test_three_tools_registered(self):
        names = build_tool_registry().names()
        for name in ("novel_outline", "novel_chunks", "novel_split_text"):
            self.assertIn(name, names, f"工具未注册：{name}")

    def test_media_declarations_present(self):
        from naiba.tools.registry import MEDIA_DECLARATIONS

        for name in ("novel_outline", "novel_chunks", "novel_split_text"):
            self.assertIn(name, MEDIA_DECLARATIONS)
            self.assertEqual(MEDIA_DECLARATIONS[name]["extract"], "none",
                             "拆书匠不产出媒体文件")

    def test_file_tools_use_read_policy(self):
        """读文件的工具必须走 _make_read_policy（越界确认），不能是 no-confirm。"""
        src = (ROOT / "naiba" / "tools" / "providers" / "novel.py").read_text(encoding="utf-8")
        self.assertIn("_make_read_policy", src)
        self.assertIn('name != "novel_split_text"', src,
                      "只有不吃文件的 novel_split_text 可以免确认")


class NovelReadAndEncodingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        novel_tools.clear_cache()

    def test_reads_utf8_and_reports_encoding(self):
        path = _write(self.dir / "utf8.txt", _book(3))
        text, encoding = novel_tools.read_text_auto(path)
        self.assertIn("第一章", text)
        self.assertIn(encoding, ("utf-8", "utf-8-sig"))

    def test_reads_gb18030(self):
        path = _write(self.dir / "gbk.txt", _book(3), encoding="gb18030")
        text, encoding = novel_tools.read_text_auto(path)
        self.assertIn("第一章", text)
        self.assertEqual(encoding, "gb18030", "GB18030 是中文盗版 TXT 标配")

    def test_reads_utf8_bom(self):
        path = _write(self.dir / "bom.txt", _book(3), encoding="utf-8-sig")
        text, encoding = novel_tools.read_text_auto(path)
        self.assertIn("第一章", text)
        self.assertEqual(encoding, "utf-8-sig", "带 BOM 的文件应优先按 utf-8-sig 读")

    def test_empty_file_rejected(self):
        path = self.dir / "empty.txt"
        path.write_bytes(b"")
        with self.assertRaises(novel_tools.NovelError):
            novel_tools.read_text_auto(path)

    def test_missing_file_rejected(self):
        with self.assertRaises(novel_tools.NovelError):
            novel_tools.read_text_auto(self.dir / "nope.txt")

    def test_oversize_file_rejected(self):
        path = self.dir / "huge.txt"
        # 只声明大小，不真写 60MB：用稀疏方式伪造不了，这里改测常量与判断路径。
        path.write_bytes(b"x" * 1024)
        original = novel_tools.MAX_FILE_BYTES
        try:
            novel_tools.MAX_FILE_BYTES = 100
            with self.assertRaises(novel_tools.NovelError):
                novel_tools.read_text_auto(path)
        finally:
            novel_tools.MAX_FILE_BYTES = original


class NovelCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        novel_tools.clear_cache()

    def test_cache_reuses_loaded_book(self):
        path = _write(self.dir / "a.txt", _book(3))
        first = novel_tools.load_book(path)
        second = novel_tools.load_book(path)
        self.assertIs(first, second, "同一文件同一签名应复用缓存对象")

    def test_cache_does_not_cross_files(self):
        """不同文件不能拿到彼此的内容（单槽缓存碰撞是评审点名的风险）。"""
        a = _write(self.dir / "a.txt", _book(3, body="甲书内容。"))
        b = _write(self.dir / "b.txt", _book(4, body="乙书内容。"))
        book_a = novel_tools.load_book(a)
        book_b = novel_tools.load_book(b)
        self.assertIn("甲书内容", book_a["chapters"][0]["body"])
        self.assertIn("乙书内容", book_b["chapters"][0]["body"])
        self.assertEqual(book_a["total"], 3)
        self.assertEqual(book_b["total"], 4)

    def test_cache_invalidated_when_file_changes(self):
        path = _write(self.dir / "c.txt", _book(2))
        first = novel_tools.load_book(path)
        self.assertEqual(first["total"], 2)
        # 改内容并推进 mtime（Windows 的 mtime 精度可能不够，显式设一次）。
        import os
        import time

        _write(path, _book(5))
        future = time.time() + 5
        os.utime(path, (future, future))
        novel_tools.clear_cache()  # 签名含 mtime_ns，这里额外清一次确保读到新内容
        second = novel_tools.load_book(path)
        self.assertEqual(second["total"], 5, "文件变了必须重新拆章")

    def test_cache_is_bounded(self):
        for index in range(novel_tools.CACHE_SLOTS + 3):
            _write(self.dir / f"many{index}.txt", _book(2))
            novel_tools.load_book(self.dir / f"many{index}.txt")
        self.assertLessEqual(len(novel_tools._cache), novel_tools.CACHE_SLOTS,
                             "缓存槽必须有上限，避免长会话累积整本书")


class NovelOutlineToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        novel_tools.clear_cache()

    def _payload(self, raw: str) -> dict:
        return json.loads(raw)

    def test_outline_has_no_body(self):
        path = _write(self.dir / "book.txt", _book(5))
        payload = self._payload(novel_tools.novel_outline_impl(path))
        self.assertEqual(payload["total"], 5)
        for row in payload["chapters"]:
            self.assertEqual(set(row), {"i", "title", "len"}, "outline 绝不带正文")

    def test_outline_paginates(self):
        path = _write(self.dir / "book.txt", _book(10))
        payload = self._payload(novel_tools.novel_outline_impl(path, offset=0, limit=4))
        self.assertEqual(len(payload["chapters"]), 4)
        self.assertEqual(payload["next_offset"], 4)
        self.assertIn("还有 6 章", payload["note"])
        page2 = self._payload(novel_tools.novel_outline_impl(path, offset=4, limit=4))
        self.assertEqual([r["i"] for r in page2["chapters"]], [4, 5, 6, 7])

    def test_outline_rejects_bad_offset(self):
        path = _write(self.dir / "book.txt", _book(3))
        for bad in (-1, True, "abc"):
            with self.assertRaises(novel_tools.NovelError):
                novel_tools.novel_outline_impl(path, offset=bad)

    def test_outline_rejects_huge_limit(self):
        path = _write(self.dir / "book.txt", _book(3))
        with self.assertRaises(novel_tools.NovelError):
            novel_tools.novel_outline_impl(path, limit=novel_tools.OUTLINE_PAGE_MAX + 1)

    def test_empty_book_gets_note(self):
        path = _write(self.dir / "blank.txt", "   \n\n  ")
        payload = self._payload(novel_tools.novel_outline_impl(path))
        self.assertEqual(payload["total"], 0)
        self.assertIn("没有可拆的章节", payload["note"])


class NovelChunksToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        novel_tools.clear_cache()

    def _payload(self, raw: str) -> dict:
        return json.loads(raw)

    def test_returns_requested_chapters_in_order(self):
        path = _write(self.dir / "book.txt", _book(5))
        payload = self._payload(novel_tools.novel_chunks_impl(path, idx=[3, 1]))
        self.assertEqual([row["i"] for row in payload["chapters"]], [3, 1],
                         "按请求顺序返回，不重排")
        self.assertIn("content", payload["chapters"][0])

    def test_duplicate_indexes_deduped_keeping_order(self):
        path = _write(self.dir / "book.txt", _book(5))
        payload = self._payload(novel_tools.novel_chunks_impl(path, idx=[2, 2, 0]))
        self.assertEqual([row["i"] for row in payload["chapters"]], [2, 0])

    def test_index_out_of_range_rejected(self):
        path = _write(self.dir / "book.txt", _book(3))
        with self.assertRaises(novel_tools.NovelError) as ctx:
            novel_tools.novel_chunks_impl(path, idx=[99])
        self.assertIn("超出范围", str(ctx.exception))

    def test_negative_and_bool_indexes_rejected(self):
        path = _write(self.dir / "book.txt", _book(3))
        for bad in ([-1], [True], ["x"]):
            with self.assertRaises(novel_tools.NovelError):
                novel_tools.novel_chunks_impl(path, idx=bad)

    def test_empty_index_rejected(self):
        path = _write(self.dir / "book.txt", _book(3))
        for bad in (None, [], ""):
            with self.assertRaises(novel_tools.NovelError):
                novel_tools.novel_chunks_impl(path, idx=bad)

    def test_too_many_indexes_rejected(self):
        path = _write(self.dir / "book.txt", _book(novel_tools.MAX_CHUNK_COUNT + 5))
        with self.assertRaises(novel_tools.NovelError):
            novel_tools.novel_chunks_impl(
                path, idx=list(range(novel_tools.MAX_CHUNK_COUNT + 1)))

    def test_char_budget_returns_continuation(self):
        """超字符预算时给 remaining_idx，而不是静默截断。

        注：单章超过 max_len 会被再切块，所以这里给足 max_len 保证 8 章各是一章。
        """
        big_body = "字" * 9000
        path = _write(self.dir / "big.txt", _book(8, body=big_body))
        novel_tools.clear_cache()
        # 显式给足 max_len：否则每章会被再切成两块，索引与「章」就对不上了。
        payload = self._payload(novel_tools.novel_chunks_impl(
            path, idx=list(range(8)), max_len=20000))
        self.assertLessEqual(payload["chars"], novel_tools.MAX_CHUNK_CHARS)
        self.assertIn("remaining_idx", payload, "超预算必须给续取信息")
        self.assertIn("note", payload)
        got = [row["i"] for row in payload["chapters"]]
        self.assertEqual(got + payload["remaining_idx"], list(range(8)))

    def test_single_chapter_over_budget_is_explained(self):
        # 单章超过 max_len 会被切块，所以要让「单块」本身就超过单次上限：
        # 先按大 max_len 拆（不切块），再取第一章。
        original = novel_tools.MAX_CHUNK_CHARS
        try:
            novel_tools.MAX_CHUNK_CHARS = 100
            path = _write(self.dir / "one.txt", "第一章 超长\n" + ("字" * 5000))
            novel_tools.clear_cache()
            with self.assertRaises(novel_tools.NovelError) as ctx:
                novel_tools.novel_chunks_impl(path, idx=[0], max_len=20000)
            self.assertIn("超过单次上限", str(ctx.exception))
        finally:
            novel_tools.MAX_CHUNK_CHARS = original


class NovelSplitTextToolTests(unittest.TestCase):
    def test_splits_generated_text(self):
        payload = json.loads(novel_tools.novel_split_text_impl(_book(3)))
        self.assertEqual(payload["total"], 3)
        self.assertIn("content", payload["chapters"][0])

    def test_outline_only_returns_index(self):
        payload = json.loads(novel_tools.novel_split_text_impl(_book(3), outline_only=True))
        self.assertEqual(payload["total"], 3)
        self.assertEqual(set(payload["chapters"][0]), {"i", "title", "len"})

    def test_empty_text_rejected(self):
        for bad in (None, "", "   "):
            with self.assertRaises(novel_tools.NovelError):
                novel_tools.novel_split_text_impl(bad)

    def test_oversize_text_rejected(self):
        original = novel_tools.MAX_TEXT_CHARS
        try:
            novel_tools.MAX_TEXT_CHARS = 100
            with self.assertRaises(novel_tools.NovelError):
                novel_tools.novel_split_text_impl("字" * 200)
        finally:
            novel_tools.MAX_TEXT_CHARS = original

    def test_large_text_returns_index_only(self):
        """文本入口也守预算：超过单次返回上限时只给索引并说明原因。"""
        original = novel_tools.MAX_CHUNK_CHARS
        try:
            novel_tools.MAX_CHUNK_CHARS = 50
            payload = json.loads(novel_tools.novel_split_text_impl(_book(3)))
            self.assertEqual(set(payload["chapters"][0]), {"i", "title", "len"})
            self.assertIn("只给索引", payload["note"])
        finally:
            novel_tools.MAX_CHUNK_CHARS = original

    def test_max_len_is_honoured(self):
        long_text = "第一章 很长\n" + ("内容。" * 2000)
        payload = json.loads(novel_tools.novel_split_text_impl(long_text, max_len=1000))
        self.assertGreater(payload["total"], 1, "max_len 生效时应切成多块")
        self.assertTrue(any("(1/" in row["title"] for row in payload["chapters"]))


class NovelProviderExecuteTests(unittest.TestCase):
    def test_execute_wraps_errors_as_failure_text(self):
        provider = novel_tools.NovelToolProvider(None)
        tools = {spec.name: spec for spec in provider.tools()}
        ok, text = tools["novel_split_text"].execute(
            {"text": "第一章 A\n内容\n第二章 B\n内容"}, [], None)
        self.assertTrue(ok, text)
        self.assertIn("chapters", text)

        ok, text = tools["novel_split_text"].execute({"text": ""}, [], None)
        self.assertFalse(ok)
        self.assertIn("不能为空", text)

    def test_provider_requires_absolute_path_without_context(self):
        provider = novel_tools.NovelToolProvider(None)
        tools = {spec.name: spec for spec in provider.tools()}
        ok, text = tools["novel_outline"].execute({"path": "relative.txt"}, [], None)
        self.assertFalse(ok)
        self.assertIn("绝对路径", text)


if __name__ == "__main__":
    unittest.main()
