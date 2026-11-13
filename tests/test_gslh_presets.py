# -*- coding: utf-8 -*-
"""古手梨花 Skill 合集预设的离线守门。

原件按字节复核并锁定指纹；转换只读仓库，不依赖网络、模型或用户下载目录。
CLI 只操作 TemporaryDirectory 的副本，不重建或改写真实 public/studio。
"""

import hashlib
import io
import json
from collections import Counter
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.studio.catalog import StudioCatalog  # noqa: E402
from verify import studio_catalog_build as build  # noqa: E402

SKILLS = ROOT / "presets" / "gushoulihua-skills"
PUBLIC = ROOT / "public" / "studio"
COLLECTION = "是古手梨花sama-提示词Skill 合集"
AUTHOR = "B站-是古手梨花sama"
# 50 份原件按 path:sha256 排序拼接后的独立指纹；与可变 manifest 分开锁定。
ASSET_DIGEST = "e0a5fa8d22821da176e05288721bf05bd0791cb17139d7b4e663e91851b81e94"
FILE_COUNT = 50
TOTAL_BYTES = 867724
EXPECTED = {
    "vs-201-gslh-k2-anime": ("vs", "K2 · 二次元插画引擎（SFW）"),
    "vs-202-gslh-k2-portrait": ("vs", "K2 · 人像摄影引擎（SFW 基础版）"),
    "vs-203-gslh-k2-contrast": ("vs", "K2 · 人像反差特化（SFW）"),
    "vs-204-gslh-k2-palace": ("vs", "K2 · 仙宫场景引擎（SFW）"),
    "vs-205-gslh-moss-anime": ("vs", "MOSS · 二次元提示词模板（SFW）"),
    "vs-206-gslh-moss-portrait": ("vs", "MOSS · 人像摄影模板（SFW v3.0）"),
    "vs-207-gslh-yaoguang": ("vs", "瑶光 · Z-image 人像提示词引擎（v5.0）"),
    "vs-211-gslh-img-sb-fight": ("vs", "图片分镜 · 动漫战斗"),
    "vs-212-gslh-img-sb-narrative": ("vs", "图片分镜 · 叙事文戏"),
    "h3-201-gslh-edge-i2v": ("h3", "H3 · 单图首帧擦边短剧（I2VA）"),
    "h3-202-gslh-pv-anime": ("h3", "H3 · 动漫 PV"),
    "h3-203-gslh-sb-manga": ("h3", "H3 · 漫画视频分镜（Ref2VA）"),
    "h3-204-gslh-sb-fight": ("h3", "H3 · 动漫战斗分镜（Ref2VA）"),
    "h3-205-gslh-sb-live-manga": ("h3", "H3 · 真人漫画分镜（Ref2VA）"),
    "h3-206-gslh-full-fight": ("h3", "H3 · 完整动漫打戏"),
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
    return build.gslh_family(source_dir) if source_dir is not None else build.gslh_family()


class GslhOriginalTests(unittest.TestCase):
    def test_manifest_and_every_original_byte_is_locked(self):
        manifest = _json(SKILLS / "source.json")
        self.assertEqual(manifest["collection"], COLLECTION)
        self.assertEqual(manifest["author"], AUTHOR)
        self.assertIn("未附许可证", manifest["license"])
        rows = manifest["files"]
        self.assertEqual(len(rows), FILE_COUNT)
        self.assertEqual(len({row["path"] for row in rows}), FILE_COUNT)
        self.assertEqual(sum(int(row["bytes"]) for row in rows), TOTAL_BYTES)
        digest_rows = []
        for row in sorted(rows, key=lambda r: r["path"]):
            path = SKILLS / row["path"]
            self.assertTrue(path.is_file(), row["path"])
            data = path.read_bytes()
            self.assertEqual(len(data), row["bytes"], row["path"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), row["sha256"], row["path"])
            digest_rows.append(f'{row["path"]}:{row["sha256"]}')
        self.assertEqual(hashlib.sha256("\n".join(digest_rows).encode("utf-8")).hexdigest(), ASSET_DIGEST)
        actual = {p.relative_to(SKILLS).as_posix() for p in SKILLS.rglob("*") if p.is_file()} - {"source.json"}
        self.assertEqual(actual, {row["path"] for row in rows}, "目录里不能有清单外文件")
        self.assertFalse((SKILLS / "anima-prompt-engine").exists(), "ANIMA 触碰 NSFW 红线，不纳入")
        self.assertFalse((SKILLS / "h3-prompt-writing").exists(), "通用 H3 与仓库内置重复，不纳入")
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("presets/gushoulihua-skills/** -text", attributes.splitlines(),
                      "原字节校验资源不能被 Windows core.autocrlf 改写")

    def test_no_download_absolute_paths_leak_into_sources(self):
        for path in SKILLS.rglob("*"):
            if not path.is_file() or path.name == "source.json":
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?i)[A-Z]:[\\/](?:Users|迅雷下载)[\\/]", path.name)


class GslhStudioTests(unittest.TestCase):
    def setUp(self):
        self.items, self.bodies, self.rejects, self.stats, self.descriptive = _family()
        self.by_id = {row["id"]: row for row in self.items}

    def test_exact_family_metadata_and_reproducible_conversion(self):
        self.assertEqual(len(self.items), len(IDS))
        self.assertEqual(set(self.by_id), IDS)
        self.assertEqual(set(self.bodies), IDS)
        self.assertEqual(self.rejects, [])
        self.assertEqual((self.items, self.bodies, self.rejects, self.stats, self.descriptive), _family(SKILLS))
        self.assertEqual(build.cross_check(self.items, self.bodies), {})
        self.assertEqual(Counter(row["module"] for row in self.items), Counter({"vs": 9, "h3": 6}))
        for item_id, (module, title) in EXPECTED.items():
            with self.subTest(item_id=item_id):
                row = self.by_id[item_id]
                self.assertEqual((row["module"], row["type"], row["origin"]), (module, "template", "builtin"))
                self.assertEqual(row["title"], title)
                self.assertIn(COLLECTION, row["source"])
                self.assertIn("古手梨花", row["tags"])
                self.assertIn(row["genre"], {"提示词引擎", "视频提示词", "视频分镜", "图片分镜"})
                self.assertEqual(row["vars"], [])
                self.assertFalse(row["has_vars"])
                self.assertEqual(row["chars"], len(self.bodies[item_id]))
                self.assertEqual(row["source_sha1"], hashlib.sha1(self.bodies[item_id].encode("utf-8")).hexdigest())

    def test_bodies_are_single_file_and_references_are_closed(self):
        preset_by_id = {preset["id"]: preset for preset in build.GSLH_PRESETS}
        for item_id, preset in preset_by_id.items():
            with self.subTest(item_id=item_id):
                body = self.bodies[item_id]
                self.assertFalse(body.lstrip().startswith("---"), "frontmatter 必须剥掉")
                self.assertNotIn("\r", body)
                self.assertNotRegex(body, r"(?m)^(?:name|description):\s")
                if preset["mode"] == "skill":
                    self.assertTrue(body.startswith("> 单文件版："))
                    for path in preset["files"][1:]:
                        self.assertIn(f"## 附：{path}", body, "每个参考文档都要有可达的末尾章节")
                else:
                    raw = (SKILLS / preset["files"][0]).read_text(encoding="utf-8")
                    self.assertTrue(body.startswith(raw.strip()[:120]), "引擎类正文必须保留原文开头")
                refs = set(re.findall(r"(?<![\w./-])((?:[\w-]+/)?references/[^\s)\]`\"'>]+?\.md)", body))
                appendix = set(re.findall(r"(?m)^## 附：(.+)$", body))
                dangling = sorted(ref for ref in refs
                                  if not any(ref == path or path.endswith("/" + ref) for path in appendix))
                self.assertEqual(dangling, [], "正文引用的参考文档必须合并到末尾")
                hits, _handled = build.match_sensitive(body, item_id)
                self.assertEqual(hits, [], "敏感硬标记必须在人工豁免清单内全部有理由")

    def test_per_skill_tool_and_missing_template_cleanup(self):
        full_fight = self.bodies["h3-206-gslh-full-fight"]
        self.assertNotIn("Codex", full_fight)
        self.assertNotIn("imagegen", full_fight)
        self.assertNotIn("references/cases/", full_fight)
        self.assertIn("本预设只交付可复制的图片提示词", full_fight)
        edge = self.bodies["h3-201-gslh-edge-i2v"]
        self.assertIn("本合集未附原「MiniMax H3 擦擦导演台图生视频提示词模板.md」", edge)
        self.assertNotIn("references/MiniMax-H3-擦擦导演台图生视频提示词模板.md", edge)
        self.assertIn("## 附：h3-edge-i2v/references/template-rules.md", edge)
        yaoguang = self.bodies["vs-207-gslh-yaoguang"]
        self.assertNotIn("](references/", yaoguang)
        self.assertNotIn("`references/瑶光提示词模板_v5.0.md`", yaoguang)
        self.assertIn("文末《瑶光提示词模板 v5.0》", yaoguang)
        fight = self.bodies["h3-204-gslh-sb-fight"]
        self.assertIn("## 附：img-storyboard-fight/references/handoff.md", fight)
        self.assertNotIn("../img-storyboard-fight", fight)
        manuscript = self.bodies["h3-203-gslh-sb-manga"]
        self.assertIn("## 附：h3-storyboard-manga/references/output-contract.md", manuscript)
        k2_anime = self.bodies["vs-201-gslh-k2-anime"]
        self.assertIn("K2 二次元提示词引擎", k2_anime)

    def test_committed_catalog_rows_and_bodies_match_family(self):
        catalog = StudioCatalog(PUBLIC)
        rows = [row for row in catalog.items() if "-gslh-" in row["id"]]
        self.assertEqual(len(rows), len(IDS))
        self.assertEqual({row["id"] for row in rows}, IDS)
        for row in rows:
            with self.subTest(item_id=row["id"]):
                self.assertEqual({k: v for k, v in row.items() if k != "order"},
                                 {k: v for k, v in self.by_id[row["id"]].items() if k != "order"})
                self.assertEqual(catalog.get_body(row["id"]), self.bodies[row["id"]])
        all_rows = catalog.items()
        self.assertEqual(len({row["id"] for row in all_rows}), len(all_rows))
        for row in all_rows:
            self.assertTrue(catalog.get_body(row["id"]).strip(), row["id"])
        report = _json(PUBLIC / "conversion_report.json")
        self.assertEqual(report["sources"][build.GSLH_SOURCE]["output"], len(IDS))
        self.assertEqual(report["gslh_conversion"]["output"], len(IDS))


class GslhAppendCliTests(unittest.TestCase):
    def _baseline(self, out):
        shutil.copytree(PUBLIC, out)
        index = _json(out / "catalog_index.json")
        index["items"] = [row for row in index["items"] if row["id"] not in IDS]
        index["count"] = len(index["items"])
        index["chars"] = sum(row["chars"] for row in index["items"])
        index["modules"] = dict(Counter(row["module"] for row in index["items"]))
        index["types"] = dict(Counter(row["type"] for row in index["items"]))
        index["sources"] = {k: v for k, v in index.get("sources", {}).items() if k != build.GSLH_SOURCE}
        _write_json(out / "catalog_index.json", index)
        prompt = _json(out / "bodies_prompt.json")
        _write_json(out / "bodies_prompt.json", {k: v for k, v in prompt.items() if k not in IDS})
        report = _json(out / "conversion_report.json")
        report.get("sources", {}).pop(build.GSLH_SOURCE, None)
        report.pop("gslh_conversion", None)
        report["descriptive_mentions"] = [row for row in report.get("descriptive_mentions", [])
                                          if "-gslh-" not in str(row.get("id") or "")]
        _write_json(out / "conversion_report.json", report)
        return index

    def _run(self, out, *, source=None):
        command = [sys.executable, "-I", str(ROOT / "verify" / "studio_catalog_build.py"),
                   "--append-gslh", "--out-dir", str(out)]
        if source is not None:
            command += ["--gslh-dir", str(source)]
        return subprocess.run(command, cwd=out.parent, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)

    def test_append_is_byte_idempotent_and_keeps_old_rows_and_other_shards(self):
        with tempfile.TemporaryDirectory(prefix="gslh-cli-") as tmp:
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
            for key, value in old_report["anomalies"].items():
                self.assertEqual(report["anomalies"][key], value)
            for tag in ("script", "game", "style"):
                name = f"bodies_{tag}.json"
                self.assertEqual(report["products"][name], old_report["products"][name])
            self.assertEqual(index["count"], len(index["items"]))
            self.assertEqual(sum(index["modules"].values()), index["count"])
            self.assertEqual(sum(index["types"].values()), index["count"])
            self.assertEqual(index["chars"], sum(row["chars"] for row in index["items"]))
            catalog = StudioCatalog(out)
            for row in index["items"]:
                self.assertTrue(catalog.get_body(row["id"]))
            second = self._run(out, source=SKILLS)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(_snapshot(out), once, "重复增补必须逐字节幂等")

    def test_bad_source_fails_closed_before_writing_any_catalog_file(self):
        for damage in ("missing_reference", "changed_bytes", "missing_manifest", "wrong_collection",
                       "extra_file", "wrong_hash", "missing_row", "traversal_path", "absolute_path"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory(prefix="gslh-invalid-") as tmp:
                tmp = Path(tmp)
                out = tmp / "studio"
                self._baseline(out)
                source = tmp / "originals"
                shutil.copytree(SKILLS, source)
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
                self.assertNotEqual(result.returncode, 0, "损坏或漂移原件不能被静默接收")
                self.assertTrue((result.stdout + result.stderr).strip(), "失败必须提供诊断")
                self.assertEqual(_snapshot(out), before, "校验失败不能部分改写目录")


class GslhCliEncodingTests(unittest.TestCase):
    def test_cli_configures_utf8_for_legacy_windows_console(self):
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        self.addCleanup(stdout.close)
        self.addCleanup(stderr.close)
        with mock.patch.object(build.sys, "stdout", stdout), mock.patch.object(build.sys, "stderr", stderr), \
                mock.patch.object(build.sys, "argv", ["studio_catalog_build.py", "--help"]):
            with self.assertRaises(SystemExit) as exit_info:
                build.main()
            self.assertEqual(exit_info.exception.code, 0)
            self.assertEqual(stdout.encoding, "utf-8")
            self.assertEqual(stderr.encoding, "utf-8")
            stdout.flush()
            self.assertIn("工作台", stdout.buffer.getvalue().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
