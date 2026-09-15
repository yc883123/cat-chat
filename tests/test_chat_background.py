# -*- coding: utf-8 -*-
"""聊天背景图设置（config.json 的 chat_background）：默认值、取景归一化、路径与格式校验。

守门目标（都是"静默失败"的高危面）：
- 非法透明度/位置/缩放不能让 NaN 或越界值漏进公开设置（前端直接拿它写 CSS 变量）；
- 背景图路径必须落在 data/uploads 内（会被前端拼进 /api/file?path=…）；
- 按**图片内容**判定格式，TIFF/HEIC 这类浏览器解不出来的必须当场拒绝——
  否则是"设置保存成功但背景一片空白"，用户看不出原因。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.config import (  # noqa: E402
    CHAT_BACKGROUND_DEFAULT_OPACITY,
    CHAT_BACKGROUND_DEFAULT_POSITION,
    CHAT_BACKGROUND_DEFAULT_ZOOM,
    CHAT_BACKGROUND_MAX_ZOOM,
    CHAT_BACKGROUND_MIN_CROP,
    CHAT_BACKGROUND_MIN_ZOOM,
    ConfigStore,
    normalize_chat_background,
    normalize_chat_background_crop,
    normalize_chat_background_opacity,
    normalize_chat_background_position,
    normalize_chat_background_zoom,
)


def default_payload(**overrides) -> dict:
    """默认 chat_background + 覆盖项（断言整份对象时用它，避免漏键导致测试假绿）。"""
    payload = {
        "image": "",
        "opacity": CHAT_BACKGROUND_DEFAULT_OPACITY,
        "crop": None,     # None = 自动（按对话区比例取最大区域）
        "position_x": CHAT_BACKGROUND_DEFAULT_POSITION,
        "position_y": CHAT_BACKGROUND_DEFAULT_POSITION,
        "zoom": CHAT_BACKGROUND_DEFAULT_ZOOM,
    }
    payload.update(overrides)
    return payload


class ChatBackgroundConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # root 必须 resolve()：CI runner 的 TEMP 是 8.3 短路径（`...\RUNNER~1\...`），而产品侧
        # 把设置里的背景图路径 resolve() 之后再存——测试侧不 resolve 就会「本地全绿、CI 全红」
        # （§六 第 ③ 条；本地用 junction 建一个 `naiba~1` 目录即可复现）。
        self.root = Path(self.tmp.name).resolve()
        self.data_dir = self.root / "data"
        self.uploads = self.data_dir / "uploads" / "2026-09-15"
        self.uploads.mkdir(parents=True)
        self.config_path = self.root / "config.json"
        self.store = self._store()

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self, payload=None):
        base = {"data_dir": str(self.data_dir)}
        if payload:
            base.update(payload)
        self.config_path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
        return ConfigStore(self.config_path)

    def _image(self, name, fmt="PNG", size=(24, 16)) -> Path:
        from PIL import Image

        path = self.uploads / name
        Image.new("RGB", size, (10, 120, 200)).save(path, format=fmt)
        return path

    # ---- 默认值 / 迁移 ----

    def test_fresh_install_defaults(self):
        self.assertEqual(self.store.data["chat_background"], default_payload())

    def test_settings_payload_exposes_chat_background(self):
        # 前端只从 bootstrap.settings 取这份数据，漏掉它就等于功能不存在。
        self.assertIn("chat_background", self.store.public())

    def test_legacy_config_without_layout_keys_is_completed(self):
        # 旧配置（只有 image/opacity）必须补齐取景三键且不报错——升级不能把老用户的背景弄丢。
        image = self._image("bg.png")
        store = self._store({"chat_background": {"image": str(image), "opacity": 0.5}})
        self.assertEqual(
            store.data["chat_background"],
            default_payload(image=str(image), opacity=0.5),
        )

    def test_hand_edited_values_are_normalized_on_load(self):
        store = self._store({"chat_background": {"image": "  ", "opacity": 9}})
        self.assertEqual(store.data["chat_background"], default_payload(opacity=1.0))
        store = self._store({"chat_background": {"opacity": "abc"}})
        self.assertEqual(store.data["chat_background"]["opacity"], CHAT_BACKGROUND_DEFAULT_OPACITY)
        store = self._store({"chat_background": "broken"})
        self.assertEqual(store.data["chat_background"], default_payload())
        # 取景字段的手改脏值同样要收敛（这里是加载期，静默归位；写入口另有显式报错）。
        store = self._store({
            "chat_background": {"position_x": -20, "position_y": 900, "zoom": "zoom!"},
        })
        self.assertEqual(store.data["chat_background"]["position_x"], 0.0)
        self.assertEqual(store.data["chat_background"]["position_y"], 100.0)
        self.assertEqual(store.data["chat_background"]["zoom"], CHAT_BACKGROUND_DEFAULT_ZOOM)

    def test_crop_is_normalized_on_load(self):
        """裁剪区域是权威取景字段：越界收敛回图片内、结构不可用回落 None（自动）。"""
        store = self._store({"chat_background": {"crop": {"x": 0.9, "y": 0.9, "w": 0.5, "h": 0.5}}})
        self.assertEqual(store.data["chat_background"]["crop"], {"x": 0.5, "y": 0.5, "w": 0.5, "h": 0.5})
        # 宽高非正 / 字段缺失 / 不是对象 → None（自动），不报错（加载期要能起来）。
        for broken in ({"x": 0, "y": 0, "w": 0, "h": 0.5}, {"x": 0, "y": 0, "w": 0.5}, {"x": 0, "y": 0, "w": 0.5, "h": "z"}, "nope"):
            with self.subTest(broken=broken):
                self.assertIsNone(normalize_chat_background_crop(broken))
                self.assertIsNone(self._store({"chat_background": {"crop": broken}}).data["chat_background"]["crop"])
        # 极小区域 clamp 到下限（存储里的"一条缝"没有观看意义）。
        tiny = normalize_chat_background_crop({"x": 0, "y": 0, "w": 0.001, "h": 0.001})
        self.assertEqual(tiny["w"], CHAT_BACKGROUND_MIN_CROP)
        self.assertEqual(tiny["h"], CHAT_BACKGROUND_MIN_CROP)
        # NaN / inf 一律当"不可用"（NaN 参与比较恒为 False，会被 min/max 静默放过）。
        self.assertIsNone(normalize_chat_background_crop({"x": float("nan"), "y": 0, "w": 0.5, "h": 0.5}))
        self.assertIsNone(normalize_chat_background_crop({"x": 0, "y": 0, "w": float("inf"), "h": 0.5}))

    def test_opacity_normalization_bounds_and_nan(self):
        self.assertEqual(normalize_chat_background_opacity(0.01), 0.05)
        self.assertEqual(normalize_chat_background_opacity(2), 1.0)
        self.assertEqual(normalize_chat_background_opacity(None), CHAT_BACKGROUND_DEFAULT_OPACITY)
        self.assertEqual(normalize_chat_background_opacity(float("nan")), CHAT_BACKGROUND_DEFAULT_OPACITY)
        self.assertEqual(normalize_chat_background_opacity(0.6), 0.6)

    def test_layout_normalization_bounds_and_nan(self):
        self.assertEqual(normalize_chat_background_position(-1), 0.0)
        self.assertEqual(normalize_chat_background_position(101), 100.0)
        self.assertEqual(normalize_chat_background_position(float("nan")), CHAT_BACKGROUND_DEFAULT_POSITION)
        self.assertEqual(normalize_chat_background_position("33.3"), 33.3)
        self.assertEqual(normalize_chat_background_zoom(0.001), CHAT_BACKGROUND_MIN_ZOOM)
        self.assertEqual(normalize_chat_background_zoom(99), CHAT_BACKGROUND_MAX_ZOOM)
        self.assertEqual(normalize_chat_background_zoom(float("nan")), CHAT_BACKGROUND_DEFAULT_ZOOM)
        self.assertEqual(normalize_chat_background_zoom(0.5), 0.5)
        # 归一化入口对非对象输入必须给出完整默认值（加载期拿到的是"未知形状"）。
        self.assertEqual(normalize_chat_background("nope"), default_payload())
        self.assertEqual(normalize_chat_background(None), default_payload())

    # ---- update_settings 契约 ----

    def test_update_merges_and_persists(self):
        image = self._image("bg.png")
        result = self.store.update_settings({"chat_background": {"image": str(image)}})
        self.assertEqual(result["chat_background"]["image"], str(image))
        self.assertEqual(result["chat_background"]["opacity"], CHAT_BACKGROUND_DEFAULT_OPACITY)
        reloaded = ConfigStore(self.config_path).data["chat_background"]
        self.assertEqual(reloaded["image"], str(image))

        # 只改透明度：图片保留；越界 clamp。
        self.store.update_settings({"chat_background": {"opacity": 0.8}})
        self.assertEqual(self.store.data["chat_background"], default_payload(image=str(image), opacity=0.8))
        self.store.update_settings({"chat_background": {"opacity": -3}})
        self.assertEqual(self.store.data["chat_background"]["opacity"], 0.05)

    def test_layout_update_clamps_and_keeps_siblings(self):
        """编辑器写的就是这三个字段：越界 clamp、部分更新不丢兄弟字段、能持久化重载。"""
        image = self._image("bg.png")
        self.store.update_settings({"chat_background": {"image": str(image), "opacity": 0.6}})

        result = self.store.update_settings({
            "chat_background": {"position_x": 12.34, "position_y": -5, "zoom": 2.5},
        })
        self.assertEqual(
            result["chat_background"],
            default_payload(image=str(image), opacity=0.6, position_x=12.3, position_y=0.0, zoom=2.5),
        )
        reloaded = ConfigStore(self.config_path).data["chat_background"]
        self.assertEqual(reloaded, result["chat_background"])

        # 再只改 zoom：位置/图片不许被动过；同时越界要 clamp 到 [0.05, 4]。
        self.store.update_settings({"chat_background": {"zoom": 99}})
        self.assertEqual(self.store.data["chat_background"]["zoom"], CHAT_BACKGROUND_MAX_ZOOM)
        self.assertEqual(self.store.data["chat_background"]["position_x"], 12.3)
        self.assertEqual(self.store.data["chat_background"]["image"], str(image))
        self.store.update_settings({"chat_background": {"zoom": 0}})
        self.assertEqual(self.store.data["chat_background"]["zoom"], CHAT_BACKGROUND_MIN_ZOOM)

    def test_zoom_clamped_to_storage_floor_for_extreme_ratios(self):
        """存储下限 0.05 是刻意登记的下限：极端长宽比（>28:1）的图无法完全缩到"整张可见"。"""
        self.store.update_settings({"chat_background": {"zoom": 0.01}})
        self.assertEqual(self.store.data["chat_background"]["zoom"], CHAT_BACKGROUND_MIN_ZOOM)

    def test_crop_write_contract(self):
        """写入路径：结构必须齐全（缺一个就报错）、越界 clamp、crop: null = 恢复自动。

        这里是"用户拖了手柄却没反应/调整凭空消失"的第一道防线：结构错误必须显式报错，
        不能静默回落成"自动"（那样用户只会看到自己的调整不见了）。
        """
        image = self._image("bg.png")
        self.store.update_settings({"chat_background": {"image": str(image), "opacity": 0.6}})
        result = self.store.update_settings({
            "chat_background": {"crop": {"x": 0.1, "y": 0.2, "w": 0.6, "h": 0.5}},
        })
        self.assertEqual(result["chat_background"]["crop"], {"x": 0.1, "y": 0.2, "w": 0.6, "h": 0.5})
        # 只改取景不许把图片/透明度带跑（增量契约）。
        self.assertEqual(result["chat_background"]["image"], str(image))
        self.assertEqual(result["chat_background"]["opacity"], 0.6)
        # 越界：整体收敛回图片内（宽高优先，位置跟着平移）。
        result = self.store.update_settings({
            "chat_background": {"crop": {"x": 0.8, "y": -1, "w": 0.6, "h": 0.5}},
        })
        self.assertEqual(result["chat_background"]["crop"], {"x": 0.4, "y": 0.0, "w": 0.6, "h": 0.5})
        # null = 恢复自动（按对话区比例取最大区域）；旧字段保持原值，不再参与渲染。
        result = self.store.update_settings({"chat_background": {"crop": None}})
        self.assertIsNone(result["chat_background"]["crop"])
        self.assertEqual(result["chat_background"]["zoom"], CHAT_BACKGROUND_DEFAULT_ZOOM)
        for broken in (
            {"x": 0, "y": 0, "w": 0.5},                      # 缺 h
            {"x": 0, "y": 0, "w": 0, "h": 0.5},              # 宽度非正
            {"x": 0, "y": 0, "w": 0.5, "h": 0.5, "z": 1},    # 多余字段
            {"x": 0, "y": 0, "w": float("nan"), "h": 0.5},   # NaN
            "cover",                                          # 形状不对
        ):
            with self.subTest(broken=broken):
                with self.assertRaises(ValueError):
                    self.store.update_settings({"chat_background": {"crop": broken}})
        # 报错之后既有取景不能被改坏（失败要原子）。
        self.assertIsNone(self.store.data["chat_background"]["crop"])

    def test_retired_fit_field_is_rejected(self):
        """`fit` 没有进契约：取景形状全部由 crop 表达，老客户端若还发 fit 必须报错。"""
        with self.assertRaises(ValueError):
            self.store.update_settings({"chat_background": {"fit": "cover"}})

    def test_empty_image_clears_background(self):
        image = self._image("bg.png")
        self.store.update_settings({"chat_background": {"image": str(image)}})
        self.store.update_settings({"chat_background": {"image": ""}})
        self.assertEqual(self.store.data["chat_background"]["image"], "")

    def test_rejects_invalid_shape_and_unknown_fields(self):
        for payload in (None, "bg.png", [], 1):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.store.update_settings({"chat_background": payload})
        with self.assertRaises(ValueError):
            self.store.update_settings({"chat_background": {"image": "", "extra": 1}})

    def test_rejects_path_outside_uploads(self):
        source = self._image("bg.png")
        outside = self.root / "outside.png"
        outside.write_bytes(source.read_bytes())
        with self.assertRaises(ValueError):
            self.store.update_settings({"chat_background": {"image": str(outside)}})
        # data_dir 内但不在 uploads 下：同样拒绝（删除/回收通道只覆盖 uploads）。
        other = self.data_dir / "generated" / "x.png"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(source.read_bytes())
        with self.assertRaises(ValueError):
            self.store.update_settings({"chat_background": {"image": str(other)}})
        with self.assertRaises(ValueError):
            self.store.update_settings({"chat_background": {"image": "bg.png"}})

    def test_accepts_builtin_background_dir(self):
        """内置背景图目录（data/backgrounds）与 uploads 一样放行；别的 data 子目录仍拒绝。"""
        from naiba.storage import backgrounds

        original = backgrounds.PRESET_SIZE
        backgrounds.PRESET_SIZE = (160, 100)
        self.addCleanup(lambda: setattr(backgrounds, "PRESET_SIZE", original))

        presets = backgrounds.ensure_background_presets(self.data_dir)
        self.assertTrue(presets, "内置背景清单不该为空")
        builtin = Path(presets[0]["path"])

        result = self.store.update_settings({"chat_background": {"image": str(builtin)}})
        self.assertEqual(result["chat_background"]["image"], str(builtin))

        # 口径没有放宽成"data_dir 里随便什么文件都行"：第三个子目录照样拒绝。
        other = self.data_dir / "other" / "x.png"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(builtin.read_bytes())
        with self.assertRaises(ValueError):
            self.store.update_settings({"chat_background": {"image": str(other)}})

    def test_rejects_missing_and_broken_file(self):
        with self.assertRaises(ValueError):
            self.store.update_settings({"chat_background": {"image": str(self.uploads / "nope.png")}})
        broken = self.uploads / "broken.png"
        broken.write_bytes(b"not an image at all")
        with self.assertRaises(ValueError):
            self.store.update_settings({"chat_background": {"image": str(broken)}})

    def test_rejects_browser_undecodable_format(self):
        """TIFF 能上传、能落盘，但 WebView2 解不出来——必须在这里拦掉。"""
        tiff = self._image("bg.tiff", fmt="TIFF")
        with self.assertRaises(ValueError) as ctx:
            self.store.update_settings({"chat_background": {"image": str(tiff)}})
        self.assertIn("仅支持", str(ctx.exception))
        self.assertIn("TIFF", str(ctx.exception))

    def test_format_is_detected_by_content_not_extension(self):
        """把 TIFF 改名成 .png 不能蒙混过关（浏览器照样解不出来）。"""
        disguised = self._image("bg.tiff", fmt="TIFF").rename(self.uploads / "disguised.png")
        with self.assertRaises(ValueError):
            self.store.update_settings({"chat_background": {"image": str(disguised)}})

    def test_accepts_web_safe_bitmap_formats(self):
        cases = {"bg.png": "PNG", "bg.jpg": "JPEG", "bg.bmp": "BMP", "bg.gif": "GIF"}
        for name, fmt in cases.items():
            with self.subTest(name=name):
                path = self._image(name, fmt=fmt)
                result = self.store.update_settings({"chat_background": {"image": str(path)}})
                self.assertEqual(result["chat_background"]["image"], str(path))


if __name__ == "__main__":
    unittest.main()
