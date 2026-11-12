"""工作台生成偏好：部分更新、重载与非法输入的离线行为守门。"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from naiba.config import ConfigStore, normalize_studio_prefs


class StudioGenerationPreferencesTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "config.json"
        self.store = ConfigStore(self.path)

    def test_old_configuration_defaults_do_not_select_skills(self):
        self.assertEqual(self.store.data["studio"]["system_prompt"], "")
        self.assertEqual(self.store.data["studio"]["prompt_source"], "template")
        self.assertEqual(self.store.data["studio"]["skill_ids"], [])
        self.assertEqual(normalize_studio_prefs(None)["prompt_source"], "template")

    def test_partial_updates_preserve_other_generation_and_library_preferences(self):
        selection = {"kind": "builtin", "id": "h3-007-h3-i2va", "module": "h3", "page": "library"}
        library = {"favorites": [{"kind": "builtin", "id": "h3-007-h3-i2va"}],
                   "titles": [{"kind": "builtin", "id": "h3-007-h3-i2va", "title": "常用写法"}],
                   "last_selection": selection, "page": "library", "view": "studio"}
        self.store.update_settings({"studio": library})
        prompt = "  我的系统提示词\n保留空格与换行\n"
        self.store.update_settings({"studio": {"system_prompt": prompt}})
        self.store.update_settings({"studio": {"skill_ids": ["two", " one ", "two"]}})
        self.store.update_settings({"studio": {"prompt_source": "skill"}})
        saved = ConfigStore(self.path).data["studio"]
        self.assertEqual(saved["system_prompt"], prompt)
        self.assertEqual(saved["skill_ids"], ["two", "one"])
        self.assertEqual(saved["prompt_source"], "skill")
        for key, value in library.items():
            self.assertEqual(saved[key], value)
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))["studio"]
        self.assertEqual(on_disk, saved)
        # Old navigation/favorite writes may arrive after the generation edits.
        self.store.update_settings({"studio": {"page": "novel", "favorites": []}})
        saved = ConfigStore(self.path).data["studio"]
        self.assertEqual(saved["system_prompt"], prompt)
        self.assertEqual(saved["skill_ids"], ["two", "one"])
        self.assertEqual(saved["prompt_source"], "skill")

    def test_clear_fields_independently_and_empty_skill_draft_is_allowed(self):
        self.store.update_settings({"studio": {"system_prompt": "规则", "prompt_source": "skill", "skill_ids": ["one"]}})
        self.store.update_settings({"studio": {"system_prompt": ""}})
        self.assertEqual(self.store.data["studio"]["skill_ids"], ["one"])
        self.store.update_settings({"studio": {"skill_ids": []}})
        saved = ConfigStore(self.path).data["studio"]
        self.assertEqual(saved["system_prompt"], "")
        self.assertEqual(saved["skill_ids"], [])
        self.assertEqual(saved["prompt_source"], "skill")
        self.store.update_settings({"studio": {"skill_ids": ["one"], "system_prompt": "新规则"}})
        self.store.update_settings({"studio": {"prompt_source": "template"}})
        saved = ConfigStore(self.path).data["studio"]
        self.assertEqual(saved["skill_ids"], ["one"])
        self.assertEqual(saved["system_prompt"], "新规则")

    def test_invalid_updates_leave_memory_and_persisted_preferences_unchanged(self):
        self.store.update_settings({"studio": {"system_prompt": "保留", "prompt_source": "skill", "skill_ids": ["keep"]}})
        invalid = [{"system_prompt": value} for value in (None, 123, True, [], {}, "长" * 100001)]
        invalid += [{"prompt_source": value} for value in (None, "", "SKILL", "other", [], {})]
        invalid += [{"skill_ids": value} for value in (None, "one", {}, [""], ["  "], [1], [False], ["one", None])]
        invalid += [{"unexpected": True}, {"system_prompt": "不应生效", "skill_ids": [""]}]
        for patch in invalid:
            with self.subTest(field=next(iter(patch)), value_type=type(next(iter(patch.values()))).__name__):
                before = copy.deepcopy(self.store.data["studio"])
                serialized = self.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.store.update_settings({"studio": patch})
                self.assertEqual(self.store.data["studio"], before)
                self.assertEqual(self.path.read_bytes(), serialized)

    def test_maximum_prompt_roundtrips_without_truncation(self):
        prompt = "字" * 100000
        saved = self.store.update_settings({"studio": {"system_prompt": prompt}})
        self.assertEqual(saved["studio"]["system_prompt"], prompt)
        self.assertEqual(ConfigStore(self.path).data["studio"]["system_prompt"], prompt)

    def test_load_normalizes_invalid_mode_and_skill_list_without_reordering(self):
        parsed = normalize_studio_prefs({"system_prompt": "规则", "prompt_source": "invalid",
                                        "skill_ids": ["two", None, " one ", "two", "", 17]})
        self.assertEqual(parsed["prompt_source"], "template")
        self.assertEqual(parsed["skill_ids"], ["two", "one"])
        self.assertEqual(parsed["system_prompt"], "规则")


if __name__ == "__main__":
    unittest.main()
