# -*- coding: utf-8 -*-
"""视觉分批守门：vision_analyze 单批 ≤4 张、超限标注、不静默截断（教训 24）。

保护对象：
- _extract_step_image_batches：每个调用独立一批（≤4 张注入），loaded/shown/total_batches
  元数据完整——超限不再静默（旧 parts[:4] 让模型误以为后续批次不存在）；
- registry 双形态最大张数默认 4（装载/分析），schema 提示分多次调用。
"""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.skills.agent import (  # noqa: E402
    _extract_step_image_batches,
    _image_batch_label,
    _image_batch_message,
)
from naiba.run.chat import _user_turn_index  # noqa: E402
from naiba.tools.registry import (  # noqa: E402
    VISION_ANALYZE_DESCRIPTION,
    VISION_ANALYZE_LOAD_DESCRIPTION,
    VISION_ANALYZE_LOAD_PARAMETERS,
    VISION_ANALYZE_PARAMETERS,
)


def _png(path: Path) -> None:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "blue").save(buf, format="PNG")
    path.write_bytes(buf.getvalue())


class ExtractStepImageBatchesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba-vision-batch-"))
        self.images = []
        for index in range(5):
            p = self.tmp / f"img_{index}.png"
            _png(p)
            self.images.append({"name": p.name, "path": str(p), "thumb_path": ""})

    def _run(self, tool: str, images: list[dict]) -> dict:
        return {"tool": tool, "result": json.dumps({"note": "x", "images": images}, ensure_ascii=False), "success": True}

    def test_two_calls_yield_two_batches(self) -> None:
        runs = [self._run("vision_analyze", self.images), self._run("vision_analyze", self.images)]
        batches = _extract_step_image_batches(runs, inject=True)
        self.assertEqual(len(batches), 2, "每次调用应独立成批")
        for b in batches:
            self.assertEqual(b["loaded"], 5)
            self.assertEqual(b["shown"], 4, "单批最多注入 4 张")
            self.assertEqual(len(b["parts"]), 4)
        self.assertEqual([b["batch_index"] for b in batches], [1, 2])
        self.assertEqual(batches[0]["total_batches"], 2)
        self.assertEqual(batches[1]["total_batches"], 2)

    def test_within_limit_single_batch(self) -> None:
        batches = _extract_step_image_batches([self._run("vision_analyze", self.images[:3])], inject=True)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["loaded"], 3)
        self.assertEqual(batches[0]["shown"], 3)
        self.assertEqual(len(batches[0]["parts"]), 3)

    def test_text_brain_injects_nothing(self) -> None:
        batches = _extract_step_image_batches([self._run("vision_analyze", self.images)], inject=False)
        self.assertEqual(batches, [], "文本大脑不注入图片，不生成批次")

    def test_non_vision_runs_skipped(self) -> None:
        runs = [self._run("read_file", []), {"tool": "vision_analyze", "result": "not-json", "success": True}]
        batches = _extract_step_image_batches(runs, inject=True)
        self.assertEqual(batches, [])


class ImageBatchLabelTests(unittest.TestCase):
    """注入标签必须带**身份**：装载轮次 + 文件名（§九.150）。

    病历（2026-10-01 用户实测）：旧标签 `【图片批 1/1】` 三条字字相同、不带轮次也不带文件名，
    而它会随 trace 重放进后续每一轮 ⇒ 模型在第 4 轮盯着第 1 轮那张旧图，把上一轮自己写的
    提示词复述成"已检查完毕、肢体正常"。用户给的格式就是身份：`（历史·第N轮装载）：文件名`。
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba-vision-label-"))
        self.images = []
        for index in range(5):
            p = self.tmp / f"lumine_scene_{index}.png"
            _png(p)
            self.images.append({"name": p.name, "path": str(p), "thumb_path": ""})

    def _batch(self, images: list[dict]) -> dict:
        runs = [{"tool": "vision_analyze", "result": json.dumps({"note": "x", "images": images}), "success": True}]
        return _extract_step_image_batches(runs, inject=True)[0]

    def test_batch_carries_the_shown_file_names(self) -> None:
        batch = self._batch(self.images)
        self.assertEqual(batch["shown"], 4)
        self.assertEqual(batch["names"], [f"lumine_scene_{i}.png" for i in range(4)],
                         "只带**实际注入**那几张的名字（没注入的不许写进标签）")

    def test_label_carries_turn_and_file_name(self) -> None:
        batch = self._batch(self.images[:1])
        self.assertEqual(
            _image_batch_label(batch, 2),
            "【图片批 1/1（历史·第2轮装载）：lumine_scene_0.png】",
        )

    def test_label_lists_every_shown_name_and_marks_truncation(self) -> None:
        batch = self._batch(self.images)
        self.assertEqual(
            _image_batch_label(batch, 2),
            "【图片批 1/1（历史·第2轮装载）：lumine_scene_0.png、lumine_scene_1.png、"
            "lumine_scene_2.png、lumine_scene_3.png；本次读取 5 张，已展示前 4 张】",
        )

    def test_label_without_a_turn_index_does_not_invent_one(self) -> None:
        """子代理/计划执行没有"轮"的概念：退化成（历史·装载），不编"第0轮"。"""
        label = _image_batch_label(self._batch(self.images[:1]), 0)
        self.assertEqual(label, "【图片批 1/1（历史·装载）：lumine_scene_0.png】")
        self.assertNotIn("第0轮", label)

    def test_message_is_label_plus_images_only(self) -> None:
        """旧版那句「以上是工具刚读取的图片，请据此继续（点击即可查看大图）。」必须彻底消失。"""
        batch = self._batch(self.images[:2])
        message = _image_batch_message(batch, 3)
        self.assertEqual(message["role"], "user")
        parts = message["content"]
        self.assertEqual(len(parts), 3, "1 段标签 + 2 张图")
        self.assertEqual(parts[0]["text"], "【图片批 1/1（历史·第3轮装载）：lumine_scene_0.png、lumine_scene_1.png】")
        self.assertEqual([p.get("type") for p in parts[1:]], ["image", "image"])
        text = parts[0]["text"]
        for gone in ("请据此继续", "点击即可查看大图", "以上是工具刚读取的图片"):
            self.assertNotIn(gone, text, f"多余的说明必须去掉：{gone}")

    def test_str_only_images_still_get_labels(self) -> None:
        """兼容只给字符串路径的结果：不许因为取名炸掉整批注入。"""
        batch = self._batch([str(self.images[0]["path"])])
        self.assertEqual(batch["names"], ["lumine_scene_0.png"])
        self.assertEqual(_image_batch_label(batch, 1), "【图片批 1/1（历史·第1轮装载）：lumine_scene_0.png】")


class UserTurnIndexTests(unittest.TestCase):
    """轮次口径：插话不算一轮；注入的图片批消息根本不在库里所以不会被数进来。"""

    @staticmethod
    def _user(mid: str, interjection: bool = False) -> dict:
        metadata = {"interjection": True} if interjection else {}
        return {"id": mid, "role": "user", "content": "x", "metadata": metadata}

    @staticmethod
    def _assistant(mid: str) -> dict:
        return {"id": mid, "role": "assistant", "content": "y", "metadata": {}}

    def test_counts_user_turns_only(self) -> None:
        messages = [
            self._user("u1"), self._assistant("a1"),
            self._user("u2", interjection=True),      # 插话不构成一轮
            self._user("u2b"), self._assistant("a2"),
            self._user("u3"),
        ]
        self.assertEqual(_user_turn_index(messages, "u3"), 3)
        self.assertEqual(_user_turn_index(messages, "u2b"), 2)

    def test_falls_back_to_the_last_turn_when_id_is_unknown(self) -> None:
        messages = [self._user("u1"), self._assistant("a1"), self._user("u2")]
        self.assertEqual(_user_turn_index(messages, ""), 2, "会话消息已含本轮用户消息，数到最后即当前轮")
        self.assertEqual(_user_turn_index(messages, "不存在"), 2)

    def test_empty_history_is_turn_one(self) -> None:
        self.assertEqual(_user_turn_index([], "u1"), 1)


class VisionBatchSchemaTests(unittest.TestCase):
    def test_load_variant_default_is_four(self) -> None:
        prop = VISION_ANALYZE_LOAD_PARAMETERS["properties"]["max_images"]
        self.assertEqual(prop.get("default"), 4, "装载形态单批默认 4 张")
        self.assertIn("分多次", str(prop.get("description") or ""))

    def test_analyze_variant_default_is_four_and_hinted(self) -> None:
        prop = VISION_ANALYZE_PARAMETERS["properties"]["max_images"]
        self.assertEqual(prop.get("default"), 4, "分析形态单批默认 4 张")
        desc = str(VISION_ANALYZE_DESCRIPTION)
        self.assertIn("4 张", desc)

    def test_folder_claim_matches_schema(self) -> None:
        """描述里承诺「文件夹」就必须真有 folder 参数：装载形态支持目录，分析形态不支持。

        实现侧依据：装载走 `_cache_folder_images`（`p.is_dir()` 展开目录）；
        分析走 `_resolve_paths`（只认 `is_file()`，目录原样透传后交给视觉后端必然失败）。
        """
        load_desc = str(VISION_ANALYZE_LOAD_DESCRIPTION)
        analyze_desc = str(VISION_ANALYZE_DESCRIPTION)
        self.assertIn("文件夹", load_desc)
        self.assertIn("folder", VISION_ANALYZE_LOAD_PARAMETERS["properties"])
        self.assertNotIn("文件夹", analyze_desc, "分析形态只收图片文件路径，不得承诺目录")
        self.assertNotIn("folder", VISION_ANALYZE_PARAMETERS["properties"])


if __name__ == "__main__":
    unittest.main()
