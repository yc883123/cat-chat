# -*- coding: utf-8 -*-
"""输入区右键「粘贴」的原生剪贴板载荷：`launcher.clipboard_payload` 的语义守门。

为什么要 Python 桥：桌面壳（WebView2）里 `navigator.clipboard.read()` 要 clipboard-read
权限，而同一模块的写方向已经实测被拒；桥没有权限与安全上下文要求。手机没有桥（也非安全
上下文）⇒ 这是桌面端能力（§九.148）。

本文件只测 `clipboard_payload` 的**纯逻辑**（喂假 reader），win32 边界的真实往返由
`verify/clipboard_paste_smoke.py` 在真剪贴板上证明——两条合起来才叫"改在生效路径上"：
假 reader 证明分支判据，真剪贴板证明 CF_DIB/CF_HDROP 的真实读写形态（`GetClipboardData(CF_HDROP)`
经 pywin32 解码后是 tuple，而写入方向必须自己合成 DROPFILES 结构——这不是纯逻辑能发现的）。

`ClipboardDibMatrixTests` 是**反着写**的一条：它钉住"裸 CF_DIB 的各种真实形态都要原样读通"。
当年曾加过补 BITMAPFILEHEADER 的代码，理由"PIL 打不开裸 DIB"来自一个**绕开了风险路径**的探针
（探针里直接用了那个补头函数，等于从没真正试过裸 DIB）；矩阵实测推翻它——裸读各形态全部正常，
补头既没修好也没弄坏任何形态（零收益），所以那段代码被删掉了。
"""
from __future__ import annotations

import base64
import io
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import launcher  # noqa: E402


def _dib_for(size=(19, 11), color=(7, 90, 200)) -> bytes:
    """按真实形态造一份 CF_DIB（BMP 去掉 14 字节 BITMAPFILEHEADER）。"""
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "BMP")
    return buf.getvalue()[14:]


def _png_for(size=(13, 17)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, (10, 20, 30, 255)).save(buf, "PNG")
    return buf.getvalue()


def _v5_dib(size=(5, 4), *, masks: bytes, header_size: int = 124, bpp: int = 32) -> bytes:
    """造一份 BITMAPV5HEADER / V4HEADER + BI_BITFIELDS 的 CF_DIB（Windows 32 位位图常见形态）。

    像素按 BGRA 写：B=30 / G=60 / R=200 ⇒ 读出来 RGB 应为 (200, 60, 30)（通道顺序错了就红）。
    """
    width, height = size
    stride = ((width * bpp + 31) // 32) * 4
    pixels = bytearray()
    for y in range(height):
        row = bytearray()
        for x in range(width):
            row += bytes((30, 60, 200, 255))
        row += b"\x00" * (stride - len(row))
        pixels += row
    header = struct.pack("<IiiHHIIiiII", header_size, width, height, 1, bpp, 3,
                         len(pixels), 2835, 2835, 0, 0)
    header += masks
    header += b"\x00" * (header_size - len(header))
    return header + bytes(pixels)


def _plain_rgb_dib(size=(7, 5)) -> bytes:
    """最常见的形态：PIL 写出的 BMP 去掉 14 字节文件头（40 字节头 + BI_RGB）。"""
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 60, 30)).save(buf, "BMP")
    return buf.getvalue()[14:]


_MASKS_FULL = struct.pack("<IIII", 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
_MASKS_THREE = struct.pack("<III", 0x00FF0000, 0x0000FF00, 0x000000FF) + b"\x00" * 4
_MASKS_TWO = struct.pack("<II", 0x00FF0000, 0x0000FF00) + b"\x00" * 8
_MASKS_ZERO = b"\x00" * 16


class ClipboardPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_clip_payload_")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _payload(self, **raw) -> dict:
        base = {"hdrop": [], "dib": None, "png": None, "text": ""}
        base.update(raw)
        return launcher.clipboard_payload(read=lambda: base)

    # ---- ① 文件优先（零拷贝，路径附件）----
    def test_files_take_priority_and_get_names_and_sizes(self) -> None:
        first = self.dir / "中文图片.png"
        second = self.dir / "第二个文件.txt"
        first.write_bytes(b"12345")
        second.write_text("hi", encoding="utf-8")
        payload = self._payload(hdrop=[str(first), str(second)], dib=_dib_for())
        self.assertEqual(payload["kind"], "files", "有磁盘路径就要走零拷贝，不该把位图搬过 IPC")
        self.assertEqual(payload["paths"], [str(first), str(second)])
        self.assertEqual(payload["names"], ["中文图片.png", "第二个文件.txt"])
        self.assertEqual(payload["sizes"], [5, 2])
        self.assertTrue(payload["ok"])

    def test_missing_paths_are_dropped_and_do_not_block_the_image(self) -> None:
        """幽灵路径（复制后文件被删/移动）不能把整条粘贴打成失败，要退回位图。"""
        payload = self._payload(hdrop=[str(self.dir / "不存在.png")], dib=_dib_for())
        self.assertEqual(payload["kind"], "image")
        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(payload["base64"]))).size, (19, 11))

    # ---- ② 位图（截图 / 网页复制图片）----
    def test_dib_becomes_a_real_png(self) -> None:
        payload = self._payload(dib=_dib_for((31, 23)))
        self.assertEqual(payload["kind"], "image")
        self.assertEqual(payload["mime"], "image/png")
        self.assertEqual(payload["name"], launcher.CLIPBOARD_IMAGE_NAME)
        image = Image.open(io.BytesIO(base64.b64decode(payload["base64"])))
        self.assertEqual(image.size, (31, 23))
        self.assertEqual(image.format, "PNG")

    def test_registered_png_format_is_used_when_there_is_no_dib(self) -> None:
        payload = self._payload(png=_png_for((29, 7)))
        self.assertEqual(payload["kind"], "image")
        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(payload["base64"]))).size, (29, 7))

    def test_oversized_image_reports_instead_of_truncating(self) -> None:
        oversized = b"\x89PNG\r\n\x1a\n" + b"0" * (launcher.CLIPBOARD_IMAGE_MAX_BYTES + 1)
        payload = self._payload(png=oversized)
        self.assertFalse(payload["ok"])
        self.assertIn("过大", payload["error"])
        self.assertIn("Ctrl+V", payload["error"], "错误要给出可行动作")

    # ---- ③ 文本 / 空 ----
    def test_text_only_is_reported_without_shipping_the_text(self) -> None:
        payload = self._payload(text="一段很长的文本" * 100)
        self.assertEqual(payload, {"ok": True, "kind": "text"}, "文本不进 IPC，由前端 readText 读")

    def test_empty_clipboard(self) -> None:
        self.assertEqual(self._payload(), {"ok": True, "kind": "empty"})

    # ---- ④ 失败要如实回给用户（不许吞异常成假成功）----
    def test_reader_failure_is_reported(self) -> None:
        def boom():
            raise RuntimeError("系统剪贴板被其它程序占用")

        payload = launcher.clipboard_payload(read=boom)
        self.assertFalse(payload["ok"])
        self.assertIn("剪贴板", payload["error"])
        self.assertIn("被其它程序占用", payload["error"])

    def test_corrupt_image_falls_back_to_text_but_reports_when_nothing_else(self) -> None:
        """坏图不该连累文本粘贴；没有可退的东西才报错。"""
        self.assertEqual(self._payload(dib=b"\x00\x01\x02", text="还能粘文本")["kind"], "text")
        broken = self._payload(dib=b"\x00\x01\x02")
        self.assertFalse(broken["ok"])
        self.assertIn("无法解析", broken["error"])

    # ---- ⑤ 裸 CF_DIB 形态矩阵（**不许补 BITMAPFILEHEADER**）----
    def test_every_realistic_dib_variant_reads_with_correct_pixels(self) -> None:
        """常见的裸 CF_DIB 形态都要能读，且**像素颜色正确**（通道顺序错了这里就红）。

        为什么不补 BITMAPFILEHEADER（矩阵实测的结论，别只看一半）：这些形态 Pillow 裸读
        全部正常；补头既没修好任何形态、也没弄坏任何形态（唯一读不动的"V5 + 只给 2 个掩码"
        补头同样读不动）⇒ 零收益，所以那段代码被删掉了。**注意**：正因为补头无害，把补头
        加回来这一组用例**不会**变红——这里守的是"各形态能读通 + 颜色对"，不是"没补头"。
        """
        width, height = 7, 5
        variants = {
            "40 字节头 BI_RGB（最常见）": _plain_rgb_dib((width, height)),
            "V5 头 + 完整 4 掩码": _v5_dib((width, height), masks=_MASKS_FULL),
            "V5 头 + 3 掩码（无 alpha）": _v5_dib((width, height), masks=_MASKS_THREE),
            "V5 头 + 掩码全 0": _v5_dib((width, height), masks=_MASKS_ZERO),
            "V4 头(108) + 3 掩码": _v5_dib((width, height), masks=_MASKS_THREE, header_size=108),
        }
        for name, dib in variants.items():
            with self.subTest(variant=name):
                payload = self._payload(dib=dib)
                self.assertEqual(payload["kind"], "image", name)
                image = Image.open(io.BytesIO(base64.b64decode(payload["base64"])))
                self.assertEqual(image.size, (width, height), name)
                self.assertEqual(image.convert("RGB").getpixel((0, 0)), (200, 60, 30), name)

    def test_two_mask_v5_dib_is_reported_instead_of_silently_dropped(self) -> None:
        """只给 2 个掩码的 V5 是 Pillow 唯一读不动的形态：要如实报错，不能静默变空。"""
        payload = self._payload(dib=_v5_dib((4, 4), masks=_MASKS_TWO))
        self.assertFalse(payload["ok"])
        self.assertIn("无法解析", payload["error"])

    # ---- ⑥ JsApi 接线 ----
    def test_jsapi_method_delegates(self) -> None:
        with mock.patch.object(launcher, "clipboard_payload", return_value={"ok": True, "kind": "empty"}) as spy:
            self.assertEqual(launcher.JsApi().naibaClipboardPayload(), {"ok": True, "kind": "empty"})
        spy.assert_called_once_with()

    def test_payload_is_json_serialisable(self) -> None:
        """桥要经 pywebview 的 JSON 序列化：bytes/Path 之类会当场炸，必须全是朴素类型。"""
        path = self.dir / "a.txt"
        path.write_text("x", encoding="utf-8")
        for payload in (self._payload(hdrop=[str(path)]), self._payload(dib=_dib_for()),
                        self._payload(text="t"), self._payload()):
            json.dumps(payload)   # 抛异常即失败


class ClipboardWritePathTests(unittest.TestCase):
    """写方向（copy_image_to_clipboard）不能被这次改动带坏：两条路共用同一套剪贴板语义。"""

    def test_write_path_still_reports_missing_dependencies_clearly(self) -> None:
        with mock.patch.dict(sys.modules, {"win32clipboard": None}):
            result = launcher.JsApi().copy_image_to_clipboard("")
        self.assertFalse(result["ok"])
        self.assertIn("依赖", result["error"])


if __name__ == "__main__":
    unittest.main()
