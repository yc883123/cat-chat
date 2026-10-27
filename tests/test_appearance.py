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
    APPEARANCE_COLOR_SKINS,
    APPEARANCE_MODEL_SKIN_FILES,
    APPEARANCE_SKINS,
    ConfigStore,
    normalize_appearance,
)

DEFAULTS = {
    "theme": "system",
    "skin": "violet",
    # 皮肤跟随模型：默认自动（见 APPEARANCE_SKIN_MODE_DEFAULT）。
    "skin_mode": "auto",
    "chat_font_size": 15,
    "chat_font_family": "system",
    "chat_font_family_custom": "",
    # 完成提示音 / 托盘完成卡片（与 config.py normalize_appearance 的默认值一一对应）。
    "done_sound": True,
    "done_sound_volume": 60,
    "tray_done_toast": True,
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

    # ---- 完成提示音 / 托盘完成卡片（done_sound / done_sound_volume / tray_done_toast） ----

    def test_done_notify_defaults_and_round_trip(self):
        """三个新键默认值正确，合法写入能落盘、能重载。"""
        store = self._store()
        self.assertEqual(store.data["appearance"]["done_sound"], True)
        self.assertEqual(store.data["appearance"]["done_sound_volume"], 60)
        self.assertEqual(store.data["appearance"]["tray_done_toast"], True)
        store.update_settings({
            "appearance": {"done_sound": False, "done_sound_volume": 30, "tray_done_toast": False}
        })
        reloaded = ConfigStore(self.path)
        self.assertEqual(reloaded.data["appearance"]["done_sound"], False)
        self.assertEqual(reloaded.data["appearance"]["done_sound_volume"], 30)
        self.assertEqual(reloaded.data["appearance"]["tray_done_toast"], False)

    def test_done_notify_rejects_invalid_values_on_update(self):
        """写入路径显式报错（不夹回）：布尔键必须真布尔，音量必须 0-100 整数。"""
        store = self._store()
        bad_payloads = [
            {"done_sound": "on"},
            {"done_sound": 1},
            {"done_sound": None},
            {"tray_done_toast": "yes"},
            {"done_sound_volume": True},   # bool 是 int 的子类，必须显式拦
            {"done_sound_volume": -1},
            {"done_sound_volume": 101},
            {"done_sound_volume": "loud"},
            {"done_sound_volume": None},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    store.update_settings({"appearance": payload})
        # 边界值必须放行（0 / 100 都在契约内）。
        self.assertEqual(store.update_settings({"appearance": {"done_sound_volume": 0}})["appearance"]["done_sound_volume"], 0)
        self.assertEqual(store.update_settings({"appearance": {"done_sound_volume": 100}})["appearance"]["done_sound_volume"], 100)

    def test_done_notify_invalid_values_clamped_on_load(self):
        """手改/旧配置里的非法值加载期回默认或夹回，绝不外泄给前端。"""
        store = self._store({
            "appearance": {"done_sound": "on", "done_sound_volume": 999, "tray_done_toast": 1}
        })
        self.assertEqual(store.data["appearance"]["done_sound"], True)
        self.assertEqual(store.data["appearance"]["done_sound_volume"], 100)
        self.assertEqual(store.data["appearance"]["tray_done_toast"], True)
        store = self._store({"appearance": {"done_sound_volume": "no"}})
        self.assertEqual(store.data["appearance"]["done_sound_volume"], 60)

    def test_done_notify_partial_update_keeps_other_keys(self):
        """单项提交不抹平其它键（前端音量滑条/开关各自单独 POST 的前提）。"""
        store = self._store()
        store.update_settings({"appearance": {"done_sound_volume": 20, "tray_done_toast": False}})
        store.update_settings({"appearance": {"done_sound": False}})
        self.assertEqual(store.data["appearance"]["done_sound"], False)
        self.assertEqual(store.data["appearance"]["done_sound_volume"], 20)
        self.assertEqual(store.data["appearance"]["tray_done_toast"], False)

    # ---- 模型皮肤与「皮肤跟随模型」（skin_mode） ----

    def test_model_skin_ids_are_accepted_and_round_trip(self):
        """18 个模型皮肤键都必须能存能读（它们与配色皮肤共用 appearance.skin）。"""
        self.assertEqual(18, len(APPEARANCE_MODEL_SKIN_FILES))
        self.assertEqual(
            set(APPEARANCE_COLOR_SKINS), {"violet", "ocean", "rose", "forest"}
        )
        # 配色皮肤与模型皮肤并集就是全部合法值，不多不少。
        self.assertEqual(
            set(APPEARANCE_SKINS),
            set(APPEARANCE_COLOR_SKINS) | set(APPEARANCE_MODEL_SKIN_FILES),
        )
        store = self._store()
        for skin in APPEARANCE_MODEL_SKIN_FILES:
            with self.subTest(skin=skin):
                saved = store.update_settings({"appearance": {"skin": skin}})
                self.assertEqual(saved["appearance"]["skin"], skin)
                self.assertEqual(
                    ConfigStore(self.path).data["appearance"]["skin"], skin
                )

    def test_model_skin_files_exist_in_public_skins(self):
        """每个皮肤键对应的 WebP 必须真的在 public/skins 里，否则用户选中就是一片空白。"""
        skins_dir = Path(__file__).resolve().parents[1] / "public" / "skins"
        self.assertTrue(skins_dir.is_dir(), "public/skins 目录不存在")
        for skin, file_stem in APPEARANCE_MODEL_SKIN_FILES.items():
            with self.subTest(skin=skin):
                target = skins_dir / f"{file_stem}.webp"
                self.assertTrue(target.is_file(), f"缺少皮肤文件：{target.name}")
                self.assertGreater(target.stat().st_size, 0)

    def test_skin_mode_default_and_round_trip(self):
        store = self._store()
        self.assertEqual(store.data["appearance"]["skin_mode"], "auto")
        saved = store.update_settings({"appearance": {"skin_mode": "manual"}})
        self.assertEqual(saved["appearance"]["skin_mode"], "manual")
        self.assertEqual(
            ConfigStore(self.path).data["appearance"]["skin_mode"], "manual"
        )

    def test_skin_mode_rejects_unknown_value(self):
        store = self._store()
        for payload in ("AUTO_ALWAYS", "off", "", 1, None):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    store.update_settings({"appearance": {"skin_mode": payload}})

    def test_unknown_skin_mode_is_normalized_on_load(self):
        """手改配置里的非法模式在加载期收回 auto，绝不外泄给前端。"""
        store = self._store({"appearance": {"skin_mode": "sometimes"}})
        self.assertEqual(store.data["appearance"]["skin_mode"], "auto")

    def test_skin_mode_update_keeps_other_appearance_keys(self):
        """切模式只改模式，不能顺手抹掉皮肤/字体等其它键。"""
        store = self._store()
        store.update_settings({
            "appearance": {"skin": "07_gpt_sol_astra", "chat_font_family": "serif"}
        })
        store.update_settings({"appearance": {"skin_mode": "manual"}})
        appearance = store.data["appearance"]
        self.assertEqual(appearance["skin"], "07_gpt_sol_astra")
        self.assertEqual(appearance["chat_font_family"], "serif")
        self.assertEqual(appearance["skin_mode"], "manual")

    def test_skin_error_message_lists_every_option(self):
        """报错文案由集合动态生成：加一款皮肤不能忘了同步文案。"""
        store = self._store()
        with self.assertRaises(ValueError) as ctx:
            store.update_settings({"appearance": {"skin": "no-such-skin"}})
        message = str(ctx.exception)
        for skin in APPEARANCE_SKINS:
            with self.subTest(skin=skin):
                self.assertIn(skin, message)


if __name__ == "__main__":
    unittest.main()
