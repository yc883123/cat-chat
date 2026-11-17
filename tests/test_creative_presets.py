"""审核后的创作模板：来源覆盖、逐条归类、重建和失败不写入。"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from naiba.studio.catalog import StudioCatalog
from verify import studio_catalog_build as build
from verify import studio_creative_presets as creative

KIT = ROOT / "presets" / creative.SOURCE
PUBLIC = ROOT / "public" / "studio"


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class CreativePresetTests(unittest.TestCase):
    def test_prose_category_is_consistent_across_layers(self):
        from naiba.core.contracts import STUDIO_CATALOG_MODULES
        from naiba.config import STUDIO_PREF_MODULES
        from naiba.studio import catalog, custom
        self.assertIn("prose", STUDIO_CATALOG_MODULES)
        self.assertEqual(catalog.MODULES_ALLOWED, custom.MODULES_ALLOWED)
        self.assertEqual(set(build.MODULES_ALLOWED), STUDIO_CATALOG_MODULES)
        self.assertIn("prose", STUDIO_PREF_MODULES)
        self.assertEqual(catalog.bodies_tag_for_module("prose"), build.BODIES_TAG_BY_MODULE["prose"])
        frontend = (ROOT / "public" / "js" / "22-studio.js").read_text(encoding="utf-8")
        self.assertIn('{ key: "prose", label: "小说写作" }', frontend)
        self.assertTrue(StudioCatalog(PUBLIC).items(module="prose"))

    def test_prose_selection_survives_preferences_reload(self):
        from naiba.config import ConfigStore
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            store = ConfigStore(path)
            selection = {"kind": "builtin", "id": "prose-creative-demo", "module": "prose", "page": "library"}
            store.update_settings({"studio": {"last_selection": selection}})
            self.assertEqual(ConfigStore(path).data["studio"]["last_selection"], selection)

    def test_each_input_is_accounted_and_bodies_are_reproducible(self):
        manifest = build.read_json(KIT / "source.json")
        self.assertEqual(len(manifest["sources"]), 41)
        items, bodies, rejects, stats, _ = creative.family(build)
        self.assertFalse(rejects)
        self.assertEqual(build.cross_check(items, bodies), {})
        self.assertEqual(stats["input"], 41)
        self.assertEqual(stats["output"], len(items))
        self.assertEqual(creative.family(build), creative.family(build, KIT))
        self.assertGreaterEqual(len({row["module"] for row in items}), 4)
        self.assertFalse(any("MOSS" in row["title"] for row in items))
        self.assertEqual(len(bodies), len(set(bodies.values())), "完全重复的正文不能多次入库")
        for source in manifest["sources"]:
            self.assertEqual(len(source["sha256"]), 64)
            self.assertTrue(source["reason"])
            if source["decision"] == "included":
                self.assertTrue(source["entry_ids"])
        for item in items:
            body = bodies[item["id"]]
            self.assertGreater(len(body), 100)
            self.assertNotIn("```python", body, "导出脚本不进入模板正文")
            self.assertNotRegex(body, r"(?i)[A-Z]:[\\/](?:Users|naiba-chat)[\\/]")
            self.assertEqual(item["source_sha1"], hashlib.sha1(body.encode()).hexdigest())

    def test_existing_catalog_contains_exact_reviewed_templates(self):
        items, bodies, _, _, _ = creative.family(build)
        catalog = StudioCatalog(PUBLIC)
        for row in items:
            stored = catalog.item(row["id"])
            self.assertEqual({k: v for k, v in stored.items() if k != "order"},
                             {k: v for k, v in row.items() if k != "order"})
            self.assertEqual(catalog.get_body(row["id"]), bodies[row["id"]])
        for row in catalog.items():
            self.assertTrue(catalog.get_body(row["id"]))

    def test_append_is_idempotent_preserves_others_and_routes_shards(self):
        items, bodies, _, _, _ = creative.family(build)
        ids = set(bodies)
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "studio"
            shutil.copytree(PUBLIC, out)
            before = build.read_json(out / "catalog_index.json")
            old_items = [row for row in before["items"] if row["id"] not in ids]
            old_shards = {tag: build.read_json(out / f"bodies_{tag}.json")
                          for tag in ("prompt", "script", "game", "style")}
            creative.append(build, out)
            first = snapshot(out)
            index = build.read_json(out / "catalog_index.json")
            self.assertEqual([row for row in index["items"] if row["id"] not in ids], old_items)
            for tag, old in old_shards.items():
                shard = build.read_json(out / f"bodies_{tag}.json")
                self.assertEqual({key: val for key, val in shard.items() if key not in ids},
                                 {key: val for key, val in old.items() if key not in ids})
                expected = {row["id"] for row in items if build.BODIES_TAG_BY_MODULE[row["module"]] == tag}
                self.assertEqual(set(shard) & ids, expected)
            creative.append(build, out)
            self.assertEqual(snapshot(out), first)

    def test_cli_runs_with_isolated_python_and_is_byte_stable(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "studio"
            shutil.copytree(PUBLIC, out)
            command = [sys.executable, "-I", str(ROOT / "verify" / "studio_catalog_build.py"),
                       "--append-creative", "--out-dir", str(out)]
            first = subprocess.run(command, cwd=temp, capture_output=True, timeout=30)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            before = snapshot(out)
            second = subprocess.run(command, cwd=temp, capture_output=True, timeout=30)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(snapshot(out), before)

    def test_source_damage_fails_before_any_catalog_write(self):
        for damage in ("body", "missing", "path", "sources"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as temp:
                temp = Path(temp)
                source, out = temp / "kit", temp / "studio"
                shutil.copytree(KIT, source)
                shutil.copytree(PUBLIC, out)
                manifest = build.read_json(source / "source.json")
                first = source / manifest["entries"][0]["path"]
                if damage == "body":
                    first.write_bytes(first.read_bytes() + b"changed")
                elif damage == "missing":
                    first.unlink()
                elif damage == "path":
                    manifest["entries"][0]["path"] = "bodies/../outside.md"
                else:
                    manifest["sources"].pop()
                build.write_json(source / "source.json", manifest)
                before = snapshot(out)
                with self.assertRaises(SystemExit):
                    creative.append(build, out, source)
                self.assertEqual(before, snapshot(out))


if __name__ == "__main__":
    unittest.main()
