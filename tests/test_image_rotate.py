# -*- coding: utf-8 -*-
"""图片旋转（`storage.media.rotate_uploaded_image`）：把竖图真的转成横图。

守门目标：
- 旋转是**顺时针**、且长宽真的对调（方向写错会让用户点两下才发现图是倒的）；
- 输出落在 uploads 分日目录内、带缩略图（前端马上要用它当背景）；
- 越界/坏图要报错而不是产出半个文件；同一个源图重复旋转要命中内容去重（不堆文件）。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.storage.media import is_uploads_path, rotate_uploaded_image  # noqa: E402


class RotateUploadedImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "data"
        self.uploads = self.data_dir / "uploads" / "2026-09-15"
        self.uploads.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _image(self, name: str, width: int, height: int, left=(255, 0, 0), right=(0, 0, 255)) -> Path:
        """左半/右半异色的小图：旋转后能按像素判断方向对不对。"""
        from PIL import Image, ImageDraw

        path = self.uploads / name
        image = Image.new("RGB", (width, height), left)
        ImageDraw.Draw(image).rectangle([width // 2, 0, width - 1, height - 1], fill=right)
        image.save(path, format="PNG")
        return path

    def test_rotates_clockwise_and_swaps_sides(self):
        from PIL import Image

        source = self._image("portrait.png", 40, 20, left=(255, 0, 0), right=(0, 0, 255))
        result = rotate_uploaded_image(source, self.data_dir)
        self.assertTrue(is_uploads_path(self.data_dir, result["path"]), "结果必须落在 uploads 内（可回收/可清理）")
        self.assertTrue(result["thumb_path"])
        with Image.open(result["path"]) as rotated:
            self.assertEqual((rotated.width, rotated.height), (20, 40), "长宽必须对调")
            # 顺时针 90°：原来的"左半边(红)"转到上半边，"右半边(蓝)"转到下半边。
            self.assertEqual(rotated.convert("RGB").getpixel((10, 4)), (255, 0, 0))
            self.assertEqual(rotated.convert("RGB").getpixel((10, 35)), (0, 0, 255))

    def test_three_turns_is_counter_clockwise(self):
        from PIL import Image

        source = self._image("portrait.png", 40, 20, left=(255, 0, 0), right=(0, 0, 255))
        result = rotate_uploaded_image(source, self.data_dir, turns=3)
        with Image.open(result["path"]) as rotated:
            self.assertEqual((rotated.width, rotated.height), (20, 40))
            self.assertEqual(rotated.convert("RGB").getpixel((10, 4)), (0, 0, 255), "转三次 = 逆时针 90°")

    def test_repeated_rotation_hits_content_dedup(self):
        source = self._image("portrait.png", 40, 20)
        first = rotate_uploaded_image(source, self.data_dir)
        second = rotate_uploaded_image(source, self.data_dir)
        self.assertEqual(first["path"], second["path"], "同样内容不该堆出第二份文件")
        self.assertTrue(second.get("deduped"))

    def test_missing_and_broken_source_raise(self):
        with self.assertRaises(ValueError):
            rotate_uploaded_image(self.uploads / "nope.png", self.data_dir)
        broken = self.uploads / "broken.png"
        broken.write_bytes(b"not an image")
        with self.assertRaises(ValueError):
            rotate_uploaded_image(broken, self.data_dir)


if __name__ == "__main__":
    unittest.main()
