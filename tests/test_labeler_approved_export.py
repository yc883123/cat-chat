"""已采纳 TXT 正文的数据库读取与导出一致性；只使用本地样本。"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from naiba.labeler.db import LabelerDB
from naiba.labeler.export import caption_text, render
from naiba.labeler.originals import archive_original
import zipfile


class LabelerApprovedExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = LabelerDB(self.root)
        original = archive_original(self.root, b"local image fixture")
        self.annotation = {
            "item_id": "image-1", "path": str(self.root / "a.png"),
            "path_key": "fixture-a", "content_version": "1:1",
            "relative_path": "dataset/a.png", "name": "a.png",
            "original_sha256": original["sha256"], "tags": ["metadata-tag"],
            "confidences": [0.9], "description": "已写完。assistant footer",
            "output_mode": "both", "model_key": "stub", "model_name": "stub",
        }
        self.db.add_items("task-1", [self.annotation], "fixture-config")

    def stored(self, body):
        self.db.complete_item("task-1", "image-1", annotation={**self.annotation, "approved_txt": body},
                              usage={}, config_fingerprint="fixture-config")
        return self.db.annotations(task_id="task-1")[0]

    def test_db_body_overrides_metadata_and_footer_verbatim(self):
        for body in ("  第一行\r\n\r\n第二行\n第三行\r末行  \n", "", " \n\t "):
            with self.subTest(body=body):
                row = self.stored(body)
                self.assertEqual(row["approved_txt"], body)
                self.assertEqual(caption_text(row), body)
                self.assertEqual(row["tags"], ["metadata-tag"])
                self.assertNotIn("assistant footer", caption_text(row))

    def test_caption_zip_and_lora_preserve_db_body_bytes(self):
        body = "  第一段\r\n\r\n第二段\n末行  \n"
        row = self.stored(body)
        for fmt, member in (("captions", "dataset/a.txt"), ("lora", "000001.txt")):
            with self.subTest(fmt=fmt):
                with zipfile.ZipFile(io.BytesIO(render([row], fmt, data_dir=self.root))) as archive:
                    self.assertEqual(archive.read(member), body.encode("utf-8"))
                    if fmt == "lora":
                        manifest = json.loads(archive.read("manifest.json"))
                        self.assertEqual(manifest["entries"][0]["caption_source"], "approved_txt")

    def test_summary_alone_flattens_newlines(self):
        row = self.stored("第一段\r\n\r\n第二段\n末行\r结束")
        self.assertEqual(render([row], "txt").decode("utf-8"),
                         "dataset/a.png, 第一段 第二段 末行 结束\n")
        self.assertEqual(caption_text(row), row["approved_txt"])

    def test_null_legacy_keeps_old_caption_modes_and_trigger_deduplication(self):
        row = self.stored(None)
        cases = [
            ({"tags": ["trigger", "cat"], "description": "trigger，一只猫\n坐在窗台", "output_mode": "both"},
             "trigger, cat, 一只猫 坐在窗台"),
            ({"tags": ["cat"], "description": "旧描述", "output_mode": "keywords"}, "cat"),
            ({"tags": [], "description": "旧描述\n第二行", "output_mode": "description"}, "旧描述 第二行"),
        ]
        self.assertIsNone(row["approved_txt"])
        for fields, expected in cases:
            with self.subTest(fields=fields):
                legacy = {**row, **fields}
                self.assertEqual(caption_text(legacy), expected)
                missing = dict(legacy)
                missing.pop("approved_txt")
                self.assertEqual(caption_text(missing), expected)
                for fmt, member in (("captions", "dataset/a.txt"), ("lora", "000001.txt")):
                    with zipfile.ZipFile(io.BytesIO(render([legacy], fmt, data_dir=self.root))) as archive:
                        self.assertEqual(archive.read(member).decode("utf-8"), expected)
