# -*- coding: utf-8 -*-
"""内置背景图（`storage/backgrounds.py`）：生成、幂等、可被设为背景。

守门目标（全是"用户点一下才发现坏了"的面）：
- 清单里每一张都真的在盘上、且是**浏览器能解码**的位图（否则点一下就是"设置成功、背景空白"）；
- 生成是**幂等**的：已存在的主图不重写——用户手动替换/微调过的内置图不会被下次启动覆盖回去；
- 删掉的内置图下次访问要补回来（"内置"的含义）；
- 目录不可写等异常不能把设置面板打挂（降级为空清单）。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.config import CHAT_BACKGROUND_IMAGE_FORMATS  # noqa: E402
from naiba.storage import backgrounds  # noqa: E402


class BackgroundPresetsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "data"
        # 生成尺寸改小：这组图在测试里只验"能用"，不该为了断言花几秒画 1600×1000。
        self._original_size = backgrounds.PRESET_SIZE
        backgrounds.PRESET_SIZE = (320, 200)
        self.addCleanup(self._restore_size)

    def tearDown(self):
        # 显式清理（交给 GC 会报 ResourceWarning，`-W error` 下就是失败）。
        self.tmp.cleanup()

    def _restore_size(self):
        backgrounds.PRESET_SIZE = self._original_size

    def test_manifest_is_generated_and_usable(self):
        from PIL import Image

        presets = backgrounds.ensure_background_presets(self.data_dir)
        self.assertGreaterEqual(len(presets), 4, "内置背景至少要有几张可选")
        ids = [item["id"] for item in presets]
        self.assertEqual(len(ids), len(set(ids)), "id 不能重复")
        directory = backgrounds.backgrounds_dir(self.data_dir)
        for item in presets:
            with self.subTest(preset=item["id"]):
                main = Path(item["path"])
                self.assertTrue(main.is_file(), "清单里的图必须真的存在")
                self.assertIn(directory, main.parents, "必须落在 data/backgrounds 内")
                with Image.open(main) as image:
                    self.assertIn(str(image.format or "").upper(), CHAT_BACKGROUND_IMAGE_FORMATS)
                self.assertTrue(Path(item["thumb_path"]).is_file(), "缩略图缺失会让设置卡那一排变破图")
                self.assertTrue(item["name"], "缩略图上要有名字")
                self.assertTrue(item["description"], "要有用途说明")

    def test_generation_is_idempotent(self):
        first = backgrounds.ensure_background_presets(self.data_dir)
        stamps = {item["id"]: Path(item["path"]).stat().st_mtime_ns for item in first}
        second = backgrounds.ensure_background_presets(self.data_dir)
        self.assertEqual([item["id"] for item in first], [item["id"] for item in second])
        for item in second:
            with self.subTest(preset=item["id"]):
                self.assertEqual(
                    stamps[item["id"]], Path(item["path"]).stat().st_mtime_ns,
                    "已存在的主图不该被重写（用户可能自己换过）",
                )

    def test_missing_preset_is_regenerated(self):
        presets = backgrounds.ensure_background_presets(self.data_dir)
        Path(presets[0]["path"]).unlink()
        again = backgrounds.ensure_background_presets(self.data_dir)
        self.assertTrue(Path(again[0]["path"]).is_file(), "删掉的内置图下次访问要补回来")

    def test_unwritable_dir_degrades_to_empty_list(self):
        # data/backgrounds 的位置被一个同名文件占住：返回空清单，而不是把设置面板打挂。
        blocker = tempfile.TemporaryDirectory()
        self.addCleanup(blocker.cleanup)
        blocked = Path(blocker.name) / "data"
        blocked.mkdir(parents=True, exist_ok=True)
        (blocked / "backgrounds").write_text("busy", encoding="utf-8")
        self.assertEqual(backgrounds.ensure_background_presets(blocked), [])


if __name__ == "__main__":
    unittest.main()
