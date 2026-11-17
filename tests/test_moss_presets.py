# -*- coding: utf-8 -*-
"""MOSS-Image 提示词套件预设的离线守门。

原件按字节复核并锁定指纹；转换只读仓库，不依赖网络、模型或用户下载目录。
CLI 只操作 TemporaryDirectory 的副本，不重建或改写真实 public/studio。
"""

import hashlib
import json
from collections import Counter
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.studio.catalog import StudioCatalog  # noqa: E402
from verify import studio_catalog_build as build  # noqa: E402

KIT = ROOT / "presets" / "moss-prompt-kit"
PUBLIC = ROOT / "public" / "studio"
COLLECTION = "MOSS-Image 提示词套件"
# 5 份原件按 path:sha256 排序拼接后的独立指纹；与可变 manifest 分开锁定。
ASSET_DIGEST = "7bb347d810fb08004f960430788cd85c5d0b16d4c60a5a8176c241f7129891b9"
FILE_COUNT = 5
TOTAL_BYTES = 128629
EXPECTED = {
    "vs-221-moss-t2i-rules": ("vs", "MOSS · 文生图提示词扩写规则"),
    "vs-222-fisher-moss-expander": ("vs", "fisher · MOSS 提示词扩写器（完整版）"),
    "vs-223-moss-official-enhancer": ("vs", "MOSS-Image 官方 · 提示词增强器（Caption / Layout）"),
    "vi-121-moss-reverse-rules": ("vi", "MOSS · 图像反推规则"),
}
IDS = set(EXPECTED)


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _snapshot(directory):
    return {p.relative_to(directory).as_posix(): p.read_bytes()
            for p in directory.rglob("*") if p.is_file()}


def _family(source_dir=None):
    return build.moss_family(source_dir) if source_dir is not None else build.moss_family()


class MossOriginalTests(unittest.TestCase):
    def test_manifest_and_every_original_byte_is_locked(self):
        manifest = _json(KIT / "source.json")
        self.assertEqual(manifest["collection"], COLLECTION)
        self.assertIn("Apache-2.0", manifest["license"])
        rows = manifest["files"]
        self.assertEqual(len(rows), FILE_COUNT)
        self.assertEqual(len({row["path"] for row in rows}), FILE_COUNT)
        self.assertEqual(sum(int(row["bytes"]) for row in rows), TOTAL_BYTES)
        digest_rows = []
        for row in sorted(rows, key=lambda r: r["path"]):
            path = KIT / row["path"]
            self.assertTrue(path.is_file(), row["path"])
            data = path.read_bytes()
            self.assertEqual(len(data), row["bytes"], row["path"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), row["sha256"], row["path"])
            digest_rows.append(f'{row["path"]}:{row["sha256"]}')
        self.assertEqual(hashlib.sha256("\n".join(digest_rows).encode("utf-8")).hexdigest(), ASSET_DIGEST)
        actual = {p.relative_to(KIT).as_posix() for p in KIT.rglob("*") if p.is_file()} - {"source.json"}
        self.assertEqual(actual, {row["path"] for row in rows}, "目录里不能有清单外文件")
        self.assertFalse((KIT / "official" / "moss-image-prompt-enhancer" / "scripts").exists(),
                         "Python 脚本不入库")
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("presets/moss-prompt-kit/** -text", attributes.splitlines(),
                      "原字节校验资源不能被 Windows core.autocrlf 改写")

    def test_no_download_absolute_paths_leak_into_sources(self):
        for path in KIT.rglob("*"):
            if not path.is_file() or path.name == "source.json":
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?i)[A-Z]:[\\/](?:Users|迅雷下载)[\\/]", path.name)

    def test_excluded_entries_are_explained(self):
        manifest = _json(KIT / "source.json")
        rows = manifest["excluded"]
        self.assertTrue(any("compile_prompt.py" in str(row.get("file") or "") for row in rows))
        portrait = [row for row in rows if "人像提示词模板" in str(row.get("file") or "")]
        self.assertEqual(len(portrait), 1, "人像模板与既有条目重复，必须在清单里写明理由")
        self.assertIn("vs-206", str(portrait[0].get("reason") or ""))


class MossStudioTests(unittest.TestCase):
    def setUp(self):
        self.items, self.bodies, self.rejects, self.stats, self.descriptive = _family()
        self.by_id = {row["id"]: row for row in self.items}

    def test_exact_family_metadata_and_reproducible_conversion(self):
        self.assertEqual(len(self.items), len(IDS))
        self.assertEqual(set(self.by_id), IDS)
        self.assertEqual(set(self.bodies), IDS)
        self.assertEqual(self.rejects, [])
        self.assertEqual((self.items, self.bodies, self.rejects, self.stats, self.descriptive), _family(KIT))
        self.assertEqual(build.cross_check(self.items, self.bodies), {})
        self.assertEqual(Counter(row["module"] for row in self.items), Counter({"vs": 3, "vi": 1}))
        for item_id, (module, title) in EXPECTED.items():
            with self.subTest(item_id=item_id):
                row = self.by_id[item_id]
                self.assertEqual((row["module"], row["type"], row["origin"]), (module, "template", "builtin"))
                self.assertEqual(row["title"], title)
                self.assertIn(COLLECTION, row["source"])
                self.assertIn("MOSS", row["tags"])
                self.assertIn(row["genre"], {"提示词规则", "提示词引擎", "图像反推"})
                self.assertEqual(row["chars"], len(self.bodies[item_id]))
                self.assertEqual(row["source_sha1"], hashlib.sha1(self.bodies[item_id].encode("utf-8")).hexdigest())

    def test_bodies_are_single_file_and_references_are_closed(self):
        preset_by_id = {preset["id"]: preset for preset in build.MOSS_PRESETS}
        for item_id, preset in preset_by_id.items():
            with self.subTest(item_id=item_id):
                body = self.bodies[item_id]
                self.assertFalse(body.lstrip().startswith("---"), "frontmatter 必须剥掉")
                self.assertNotIn("\r", body)
                if preset["mode"] == "skill":
                    self.assertTrue(body.startswith("> 单文件版："))
                    self.assertNotRegex(body, r"(?m)^(?:name|description):\s")
                    for path in preset["files"][1:]:
                        self.assertIn(f"## 附：{path}", body, "每个参考文档都要有可达的末尾章节")
                else:
                    raw = (KIT / preset["files"][0]).read_text(encoding="utf-8")
                    self.assertTrue(body.startswith(raw.strip()[:120]), "规则类正文必须保留原文开头")
                refs = set(re.findall(r"(?<![\w./-])((?:[\w-]+/)?references/[^\s)\]`\"'>]+?\.md)", body))
                appendix = set(re.findall(r"(?m)^## (?:附：)?(.+)$", body))
                dangling = sorted(ref for ref in refs
                                  if not any(ref == path or path.endswith("/" + ref) for path in appendix))
                self.assertEqual(dangling, [], "正文引用的参考文档必须合并到末尾")
                hits, _handled = build.match_sensitive(body, item_id)
                self.assertEqual(hits, [], "敏感硬标记必须零命中")

    def test_module_routing_matches_the_library_tabs(self):
        self.assertTrue(self.bodies["vs-221-moss-t2i-rules"].startswith("你是 MOSS-Image 的中文提示词扩写助手"))
        self.assertTrue(self.bodies["vi-121-moss-reverse-rules"].startswith("你是 MOSS-Image 的图像反推提示词助手"))
        self.assertIn("Layout", self.bodies["vs-223-moss-official-enhancer"])
        self.assertIn("compile_prompt.py", self.bodies["vs-223-moss-official-enhancer"], "脚本未内置必须写明")
        self.assertIn("## references/mode-decisions.md", self.bodies["vs-222-fisher-moss-expander"])

    def test_committed_catalog_rows_and_bodies_match_family(self):
        catalog = StudioCatalog(PUBLIC)
        rows = [row for row in catalog.items() if row["id"] in IDS]
        self.assertEqual({row["id"] for row in rows}, IDS)
        for row in rows:
            with self.subTest(item_id=row["id"]):
                self.assertEqual({k: v for k, v in row.items() if k != "order"},
                                 {k: v for k, v in self.by_id[row["id"]].items() if k != "order"})
                self.assertEqual(catalog.get_body(row["id"]), self.bodies[row["id"]])
        report = _json(PUBLIC / "conversion_report.json")
        self.assertEqual(report["sources"][build.MOSS_SOURCE]["output"], len(IDS))
        self.assertEqual(report["moss_conversion"]["output"], len(IDS))


class MossAppendCliTests(unittest.TestCase):
    def _baseline(self, out):
        shutil.copytree(PUBLIC, out)
        index = _json(out / "catalog_index.json")
        index["items"] = [row for row in index["items"] if row["id"] not in IDS]
        index["count"] = len(index["items"])
        index["chars"] = sum(row["chars"] for row in index["items"])
        index["modules"] = dict(Counter(row["module"] for row in index["items"]))
        index["types"] = dict(Counter(row["type"] for row in index["items"]))
        index["sources"] = {k: v for k, v in index.get("sources", {}).items() if k != build.MOSS_SOURCE}
        _write_json(out / "catalog_index.json", index)
        prompt = _json(out / "bodies_prompt.json")
        _write_json(out / "bodies_prompt.json", {k: v for k, v in prompt.items() if k not in IDS})
        report = _json(out / "conversion_report.json")
        report.get("sources", {}).pop(build.MOSS_SOURCE, None)
        report.pop("moss_conversion", None)
        report["descriptive_mentions"] = [row for row in report.get("descriptive_mentions", [])
                                          if row.get("id") not in IDS]
        _write_json(out / "conversion_report.json", report)
        return index

    def _run(self, out, *, source=None):
        command = [sys.executable, "-I", str(ROOT / "verify" / "studio_catalog_build.py"),
                   "--append-moss", "--out-dir", str(out)]
        if source is not None:
            command += ["--moss-dir", str(source)]
        return subprocess.run(command, cwd=out.parent, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)

    def test_append_is_byte_idempotent_and_keeps_old_rows_and_other_shards(self):
        with tempfile.TemporaryDirectory(prefix="moss-cli-") as tmp:
            out = Path(tmp) / "studio"
            before_index = self._baseline(out)
            before = _snapshot(out)
            old_prompt = _json(out / "bodies_prompt.json")
            first = self._run(out)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            once = _snapshot(out)
            index = _json(out / "catalog_index.json")
            self.assertEqual(index["count"], before_index["count"] + len(IDS))
            self.assertEqual([row for row in index["items"] if row["id"] not in IDS], before_index["items"])
            self.assertEqual({row["id"] for row in index["items"] if row["id"] in IDS}, IDS)
            bodies = _json(out / "bodies_prompt.json")
            self.assertEqual({k: v for k, v in bodies.items() if k not in IDS}, old_prompt)
            for name, data in before.items():
                if name not in {"catalog_index.json", "bodies_prompt.json", "conversion_report.json"}:
                    self.assertEqual(once[name], data, f"不应改动其他旧分片：{name}")
            old_report = json.loads(before["conversion_report.json"])
            report = _json(out / "conversion_report.json")
            for key, value in old_report["sources"].items():
                self.assertEqual(report["sources"][key], value)
            for entry in old_report.get("descriptive_mentions", []):
                self.assertIn(entry, report["descriptive_mentions"])
            catalog = StudioCatalog(out)
            for row in index["items"]:
                self.assertTrue(catalog.get_body(row["id"]))
            second = self._run(out, source=KIT)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(_snapshot(out), once, "重复增补必须逐字节幂等")

    def test_bad_source_fails_closed_before_writing_any_catalog_file(self):
        for damage in ("missing_reference", "changed_bytes", "missing_manifest", "wrong_collection",
                       "extra_file", "wrong_hash", "missing_row", "traversal_path", "absolute_path"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory(prefix="moss-invalid-") as tmp:
                tmp = Path(tmp)
                out = tmp / "studio"
                self._baseline(out)
                source = tmp / "originals"
                shutil.copytree(KIT, source)
                manifest_path = source / "source.json"
                manifest = _json(manifest_path)
                if damage == "missing_reference":
                    (source / manifest["files"][0]["path"]).unlink()
                elif damage == "changed_bytes":
                    path = source / manifest["files"][0]["path"]
                    path.write_bytes(path.read_bytes() + b"\n")
                elif damage == "missing_manifest":
                    manifest_path.unlink()
                elif damage == "extra_file":
                    (source / "stray.md").write_text("stray\n", encoding="utf-8")
                elif damage == "wrong_collection":
                    manifest["collection"] = "另一个合集"
                elif damage == "wrong_hash":
                    manifest["files"][0]["sha256"] = "0" * 64
                elif damage == "missing_row":
                    manifest["files"] = manifest["files"][:-1]
                else:
                    outside = tmp / "outside.md"
                    shutil.copyfile(source / manifest["files"][0]["path"], outside)
                    manifest["files"][0]["path"] = (str(outside.resolve())
                                                    if damage == "absolute_path" else "../outside.md")
                if manifest_path.exists():
                    _write_json(manifest_path, manifest)
                before = _snapshot(out)
                result = self._run(out, source=source)
                self.assertNotEqual(result.returncode, 0, damage)
                self.assertEqual(_snapshot(out), before, "校验必须先于任何写入")


if __name__ == "__main__":
    unittest.main()
