# -*- coding: utf-8 -*-
"""Appearance settings defaults, migration and validation."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.config import (  # noqa: E402
    APPEARANCE_CHAT_FONT_FAMILIES,
    APPEARANCE_CHAT_FONT_PRESETS,
    ConfigStore,
    normalize_appearance,
)

DEFAULTS = {
    "theme": "system",
    "skin": "violet",
    "chat_font_size": 15,
    "chat_font_family": "system",
    "chat_font_family_custom": "",
}


class AppearanceConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self, payload=None):
        if payload is not None:
            self.path.write_text(json.dumps(payload), encoding="utf-8")
        return ConfigStore(self.path)

    def test_fresh_install_defaults(self):
        store = self._store()
        self.assertEqual(store.data["appearance"], DEFAULTS)

    def test_partial_and_invalid_legacy_values_are_completed(self):
        store = self._store({"appearance": {"theme": "DARK", "skin": "unknown"}})
        self.assertEqual(store.data["appearance"], {**DEFAULTS, "theme": "dark"})

        store = self._store({"appearance": {"skin": "ocean"}})
        self.assertEqual(store.data["appearance"], {**DEFAULTS, "skin": "ocean"})

    def test_update_merges_and_persists_valid_values(self):
        store = self._store()
        result = store.update_settings({"appearance": {"theme": "dark", "skin": "rose"}})
        self.assertEqual(result["appearance"], {**DEFAULTS, "theme": "dark", "skin": "rose"})
        reloaded = ConfigStore(self.path)
        self.assertEqual(reloaded.data["appearance"], {**DEFAULTS, "theme": "dark", "skin": "rose"})

        # Updating one field preserves the other.
        store.update_settings({"appearance": {"theme": "light"}})
        self.assertEqual(store.data["appearance"], {**DEFAULTS, "theme": "light", "skin": "rose"})

    def test_update_rejects_invalid_shape_and_enum(self):
        store = self._store()
        for payload in (None, "dark", [], 1):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    store.update_settings({"appearance": payload})
        for payload in ({"theme": "blue"}, {"skin": "purple"}, {"theme": "dark", "extra": True}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    store.update_settings({"appearance": payload})

    # ---- 会话区字体（chat_font_size / chat_font_family） ----

    def test_chat_font_update_persists_and_survives_reload(self):
        store = self._store()
        store.update_settings({
            "appearance": {
                "chat_font_size": 17,
                "chat_font_family": "serif",
            }
        })
        self.assertEqual(store.data["appearance"]["chat_font_size"], 17)
        self.assertEqual(store.data["appearance"]["chat_font_family"], "serif")
        reloaded = ConfigStore(self.path)
        self.assertEqual(reloaded.data["appearance"]["chat_font_size"], 17)
        self.assertEqual(reloaded.data["appearance"]["chat_font_family"], "serif")

    def test_custom_font_family_round_trips_and_is_trimmed(self):
        store = self._store()
        saved = store.update_settings({
            "appearance": {
                "chat_font_family": "custom",
                "chat_font_family_custom": '  "LXGW WenKai", 楷体  ',
            }
        })
        self.assertEqual(saved["appearance"]["chat_font_family"], "custom")
        self.assertEqual(saved["appearance"]["chat_font_family_custom"], '"LXGW WenKai", 楷体')
        reloaded = ConfigStore(self.path)
        self.assertEqual(reloaded.data["appearance"]["chat_font_family_custom"], '"LXGW WenKai", 楷体')

    def test_custom_font_family_is_truncated_not_rejected(self):
        store = self._store()
        store.update_settings({
            "appearance": {"chat_font_family": "custom", "chat_font_family_custom": "x" * 400}
        })
        self.assertEqual(len(store.data["appearance"]["chat_font_family_custom"]), 100)

    def test_chat_font_family_switching_keeps_custom_text(self):
        """切回非 custom 时保留用户打过的串，避免"改一下又切回来"要重打。"""
        store = self._store()
        store.update_settings({
            "appearance": {"chat_font_family": "custom", "chat_font_family_custom": "Georgia"}
        })
        store.update_settings({"appearance": {"chat_font_family": "rounded"}})
        self.assertEqual(store.data["appearance"]["chat_font_family"], "rounded")
        self.assertEqual(store.data["appearance"]["chat_font_family_custom"], "Georgia")

    def test_update_rejects_out_of_range_or_non_numeric_font_size(self):
        store = self._store()
        for payload in (12, 19, 0, -5, "big", None, True):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    store.update_settings({"appearance": {"chat_font_size": payload}})
        # 边界值必须放行（13 / 18 都在契约内）。
        self.assertEqual(store.update_settings({"appearance": {"chat_font_size": 13}})["appearance"]["chat_font_size"], 13)
        self.assertEqual(store.update_settings({"appearance": {"chat_font_size": 18}})["appearance"]["chat_font_size"], 18)

    def test_update_rejects_invalid_font_family_and_non_string_custom(self):
        store = self._store()
        with self.assertRaises(ValueError):
            store.update_settings({"appearance": {"chat_font_family": "comic"}})
        with self.assertRaises(ValueError):
            store.update_settings({"appearance": {"chat_font_family_custom": 123}})

    def test_preset_chat_fonts_round_trip(self):
        """「指定字体」列表里的每一款都必须能存能读（它们只是枚举键，不是自由文本）。"""
        self.assertEqual(
            set(APPEARANCE_CHAT_FONT_PRESETS),
            {"yahei", "pingfang", "simsun", "kaiti", "simhei", "fangsong",
             "noto_sans", "noto_serif", "lxgw", "smiley", "harmonyos"},
        )
        store = self._store()
        for key in APPEARANCE_CHAT_FONT_PRESETS:
            with self.subTest(key=key):
                saved = store.update_settings({"appearance": {"chat_font_family": key}})
                self.assertEqual(saved["appearance"]["chat_font_family"], key)
                self.assertEqual(
                    ConfigStore(self.path).data["appearance"]["chat_font_family"], key
                )

    def test_unknown_font_key_is_normalized_on_load(self):
        """手改/旧版配置里的未知键在加载期收回 system（绝不外泄给前端）。"""
        store = self._store({"appearance": {"chat_font_family": "comic"}})
        self.assertEqual(store.data["appearance"]["chat_font_family"], "system")
        store = self._store({"appearance": {"chat_font_family": "LXGW"}})
        self.assertEqual(store.data["appearance"]["chat_font_family"], "lxgw")

    def test_font_family_error_message_lists_every_option(self):
        """报错文案由集合动态生成：加一款字体不能忘了同步文案（用户看到的就是这一句）。"""
        store = self._store()
        with self.assertRaises(ValueError) as ctx:
            store.update_settings({"appearance": {"chat_font_family": "comic-sans"}})
        message = str(ctx.exception)
        for key in APPEARANCE_CHAT_FONT_FAMILIES:
            with self.subTest(key=key):
                self.assertIn(key, message)

    def test_chat_font_size_is_normalized_on_load(self):
        """手改配置的越界/非法字号在加载期夹回，绝不漏进公开 settings 载荷。"""
        store = self._store({
            "appearance": {
                "chat_font_size": 99,
                "chat_font_family": "Comic Sans",
                "chat_font_family_custom": "y" * 500,
            }
        })
        self.assertEqual(store.data["appearance"]["chat_font_size"], 18)
        self.assertEqual(store.data["appearance"]["chat_font_family"], "system")
        self.assertEqual(len(store.data["appearance"]["chat_font_family_custom"]), 100)

        store = self._store({"appearance": {"chat_font_size": "not-a-number"}})
        self.assertEqual(store.data["appearance"]["chat_font_size"], 15)

        store = self._store({"appearance": {"chat_font_size": 2}})
        self.assertEqual(store.data["appearance"]["chat_font_size"], 13)

    def test_normalize_appearance_fills_missing_keys(self):
        self.assertEqual(
            normalize_appearance({"theme": "dark", "chat_font_size": 16}),
            {**DEFAULTS, "theme": "dark", "chat_font_size": 16},
        )
        self.assertEqual(normalize_appearance(None), DEFAULTS)


if __name__ == "__main__":
    unittest.main()
