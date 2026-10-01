# -*- coding: utf-8 -*-
"""护栏：VisionRouter.prepare_history 行为规格（自动路由移除后）。

保护对象：视觉调用统一由模型驱动——
- 纯文本模型（无视觉能力）发图：图片只改写为安全文本占位（路径引用 + vision_analyze 调用提示），
  绝不后台识图注入描述；
- 多模态模型（有视觉能力）：原样保留图片，不做占位改写。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.vision.runtime import VisionRouter  # noqa: E402


class _ConfigStub:
    def __init__(self, vision: dict):
        self.data = {"vision": vision}


class _AppStub:
    def __init__(self, vision: dict):
        self.config = _ConfigStub(vision)


def _image_part(name: str = "demo.png") -> dict:
    return {"type": "image", "media_type": "image/jpeg", "data": "YWJj", "name": name}


def _history_with_image() -> list[dict]:
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": "看看这张图"},
            _image_part("demo.png"),
        ],
    }]


class VisionPrepareHistoryTests(unittest.TestCase):
    def setUp(self):
        self.router = VisionRouter(_AppStub({
            "provider_model_key": "",
            "timeout_ms": 180000,
            "max_images": 4,
            "cache": True,
            "cache_ttl_seconds": 3600,
            "cache_max_entries": 200,
        }))

    def test_text_model_images_become_safe_placeholder(self):
        history = _history_with_image()
        new_history, note, demotions = self.router.prepare_history(
            history, {"model": "deepseek-v4-flash", "request_format": "openai_chat"}
        )
        self.assertTrue(note.startswith("已移除"), f"note 应说明图片清洗：{note}")
        self.assertEqual(demotions, [], "纯文本模型的占位改写不是本地图片总量降级，不落旗标")
        content = new_history[0]["content"]
        self.assertEqual(
            [part for part in content if part.get("type") == "image"], [],
            "纯文本模型绝不能收到原始 image 部件",
        )
        text = "".join(
            str(part.get("text") or "")
            for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
        self.assertIn("demo.png", text)
        self.assertIn("vision_analyze", text)
        self.assertNotIn("自动识别结果", text, "后台识图注入已移除")

    def test_multimodal_model_keeps_original_images(self):
        history = _history_with_image()
        new_history, note, demotions = self.router.prepare_history(
            history, {"model": "gpt-4o", "request_format": "openai_chat"}
        )
        self.assertEqual(note, "")
        self.assertEqual(demotions, [], "在线多模态大脑不做图片降级（旗标只在 kind=local 读写）")
        self.assertEqual(len(new_history[0]["content"]), 2)
        self.assertEqual(new_history[0]["content"][1]["type"], "image")


if __name__ == "__main__":
    unittest.main()
