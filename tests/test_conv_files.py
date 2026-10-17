# -*- coding: utf-8 -*-
"""护栏：会话文件访问边界 _conv_file_* 行为规格。

保护对象：阶段 1 将 _conv_file_allow/open/save 迁出 server.py 到 core/conv_files.py 时的
行为等价性。安全边界：文件面板只允许"本会话改动过 或 位于会话工作区内"的文件，
写回必须同时满足两者，越界路径（含 ../ 穿越、绝对路径、http:// 前缀）一律拒绝。

附录 A（文件预览策略跟进优化）：结构化预览错误码 + 文本编码选择——
GBK 文件必须返回 encoding_required 而不是静默 U+FFFD；指定编码重开正确解码；
非 UTF-8 打开的文本强制只读。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import _conv_file_allow, _conv_file_open, _conv_file_save  # noqa: E402
from naiba.core.conv_files import ConvFilePreviewError  # noqa: E402


class StubConfig:
    def __init__(self, base: Path):
        self.base = base

    def resolve_workspace_dir(self, raw=None):
        candidate = Path(str(raw or "").strip()) if str(raw or "").strip() else Path("ws")
        target = candidate if candidate.is_absolute() else self.base / candidate
        return target.resolve()


class ConversationFileBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.config = StubConfig(self.root)
        self.touched = str(self.ws / "a.txt")
        self.conv = {
            "workspace_dir": str(self.ws),
            "messages": [
                {"metadata": {"files": [{"path": self.touched}]}},
            ],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_touched_file_inside_workspace_allows_open_and_save(self):
        target = Path(self.touched)
        target.write_text("原始内容", encoding="utf-8")
        file_path, listed, within_root, root = _conv_file_allow(self.conv, self.config, self.touched)
        self.assertTrue(file_path)
        self.assertTrue(listed)
        self.assertTrue(within_root)
        opened = _conv_file_open(self.conv, self.config, self.touched)
        self.assertEqual(opened["kind"], "text")
        saved = _conv_file_save(self.conv, self.config, self.touched, "新的内容")
        self.assertEqual(saved["size"], len("新的内容".encode("utf-8")))
        self.assertEqual(target.read_text(encoding="utf-8"), "新的内容")
        self.assertFalse(any(Path(self.ws).glob(".*.naiba-tmp")), "临时文件残留")

    def test_inside_but_not_touched_cannot_save(self):
        other = self.ws / "b.txt"
        other.write_text("x", encoding="utf-8")
        _file_path, listed, within_root, _root = _conv_file_allow(self.conv, self.config, str(other))
        self.assertFalse(listed)
        self.assertTrue(within_root)
        # 只读可开，写回必须"改动过+在工作区内"同时成立。
        self.assertEqual(_conv_file_open(self.conv, self.config, str(other))["kind"], "text")
        with self.assertRaisesRegex(ValueError, "无权保存"):
            _conv_file_save(self.conv, self.config, str(other), "覆盖")

    def test_absolute_path_outside_workspace_rejected(self):
        outside = self.root / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        _file_path, listed, within_root, _root = _conv_file_allow(self.conv, self.config, str(outside))
        self.assertFalse(within_root)
        self.assertFalse(listed)
        with self.assertRaisesRegex(ValueError, "无权访问"):
            _conv_file_open(self.conv, self.config, str(outside))

    def test_traversal_out_of_workspace_rejected(self):
        traversal = str(self.ws / ".." / "escape.txt")
        _file_path, listed, within_root, _root = _conv_file_allow(self.conv, self.config, traversal)
        self.assertFalse(within_root)
        with self.assertRaisesRegex(ValueError, "无权访问"):
            _conv_file_open(self.conv, self.config, traversal)

    def test_http_prefix_rejected(self):
        _file_path, listed, within_root, _root = _conv_file_allow(
            self.conv, self.config, "https://example.com/a.txt"
        )
        self.assertIsNone(_file_path)
        self.assertFalse(within_root)

    def test_empty_conversation_uses_default_workspace(self):
        default_file = self.root / "ws" / "def.txt"
        default_file.write_text("d", encoding="utf-8")
        _file_path, listed, within_root, _root = _conv_file_allow(None, self.config, "def.txt")
        self.assertTrue(_file_path)
        self.assertTrue(within_root)
        self.assertFalse(listed)


class ConvFileEncodingTests(unittest.TestCase):
    """附录 A.1：文本编码选择——GBK 文件不静默 U+FFFD，非 UTF-8 强制只读。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.config = StubConfig(self.root)
        self.gbk_file = self.ws / "gbk.txt"
        self.gbk_file.write_bytes("中文内容测试".encode("gbk"))
        self.utf8_file = self.ws / "utf8.txt"
        self.utf8_file.write_text("UTF-8 内容", encoding="utf-8")
        self.conv = {"workspace_dir": str(self.ws), "messages": []}

    def tearDown(self):
        self.tmp.cleanup()

    def test_gbk_file_returns_encoding_required_not_replacement_chars(self):
        with self.assertRaises(ConvFilePreviewError) as ctx:
            _conv_file_open(self.conv, self.config, str(self.gbk_file))
        self.assertEqual(ctx.exception.code, "encoding_required")

    def test_explicit_gbk_encoding_decodes_and_marks_readonly(self):
        info = _conv_file_open(self.conv, self.config, str(self.gbk_file), encoding="gbk")
        self.assertEqual(info["kind"], "text")
        self.assertEqual(info["encoding"], "gbk")
        self.assertEqual(info["content"], "中文内容测试")
        self.assertTrue(info.get("readonly"))
        self.assertFalse(info["savable"])

    def test_utf8_file_still_savable(self):
        info = _conv_file_open(self.conv, self.config, str(self.utf8_file))
        self.assertEqual(info["encoding"], "utf-8")
        self.assertFalse(info.get("readonly"))
        # savable = listed AND within_root（既有口径）：本会话没改动过的文件不可保存，
        # 这里的断言点是「UTF-8 打开不额外降级只读」，savable 只受访问边界影响。
        self.assertFalse(info["savable"])
        self.assertEqual(info["content"], "UTF-8 内容")

    def test_utf8_touched_file_savable(self):
        conv = {
            "workspace_dir": str(self.ws),
            "messages": [{"metadata": {"files": [{"path": str(self.utf8_file)}]}}],
        }
        info = _conv_file_open(conv, self.config, str(self.utf8_file))
        self.assertTrue(info["savable"])
        self.assertFalse(info.get("readonly"))

    def test_unknown_encoding_rejected(self):
        with self.assertRaisesRegex(ValueError, "不支持的编码"):
            _conv_file_open(self.conv, self.config, str(self.utf8_file), encoding="utf-16")

    def test_wrong_encoding_still_fails_strict(self):
        # 指定编码后仍解码失败：继续抛 encoding_required（前端留在选择器）。
        with self.assertRaises(ConvFilePreviewError) as ctx:
            _conv_file_open(self.conv, self.config, str(self.gbk_file), encoding="big5")
        self.assertEqual(ctx.exception.code, "encoding_required")


class ConvFilePreviewErrorCodesTests(unittest.TestCase):
    """附录 A.2：结构化预览错误码各自触发路径。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.config = StubConfig(self.root)
        self.conv = {"workspace_dir": str(self.ws), "messages": []}

    def tearDown(self):
        self.tmp.cleanup()

    def test_not_found_code(self):
        with self.assertRaises(ConvFilePreviewError) as ctx:
            _conv_file_open(self.conv, self.config, str(self.ws / "missing.txt"))
        self.assertEqual(ctx.exception.code, "not_found")

    def test_forbidden_code(self):
        outside = self.root / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        with self.assertRaises(ConvFilePreviewError) as ctx:
            _conv_file_open(self.conv, self.config, str(outside))
        self.assertEqual(ctx.exception.code, "forbidden")

    def test_binary_unsupported_code(self):
        binary = self.ws / "blob.bin"
        binary.write_bytes(b"\x00\x01\x02binary")
        info = _conv_file_open(self.conv, self.config, str(binary))
        self.assertEqual(info["kind"], "binary")
        # unsupported 属于前端展示口径：kind=binary 的响应足够前端渲染
        # 「默认应用打开」卡片，不再额外抛错（二进制不报错，见计划 A.3 兜底口径）。

    def test_too_large_code(self):
        from naiba.core.conv_files import _CONV_FILE_READ_CAP
        big = self.ws / "big.txt"
        big.write_bytes(b"a" * (_CONV_FILE_READ_CAP + 1))
        info = _conv_file_open(self.conv, self.config, str(big))
        self.assertTrue(info["truncated"], "超大文本截断预览而非报错")
        self.assertEqual(len(info["content"]), _CONV_FILE_READ_CAP)


if __name__ == "__main__":
    unittest.main()
