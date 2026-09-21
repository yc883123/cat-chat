# -*- coding: utf-8 -*-
"""应用内「更换应用图标」的守门测试。

功能形态（2026-09-21 落地）：侧栏底部齿轮右侧入口 → 弹窗预览/选择图片/恢复默认 →
图标写入 `app_dir/custom-icon.png` + `custom-icon.ico` → **完全退出并重新启动**才生效
（pywebview 不支持运行时换窗口图标，只换托盘会留下"托盘变了窗口没变"的半截状态）。

覆盖：
1. 归一化 `normalize_app_icon`：**居中留白贴方形、不裁切内容**、超边缩放、太小/非图片/超限拒绝；
2. 存储 `store_app_icon` / `clear_app_icon` / `has_custom_app_icon` / `read_app_icon_png`：
   两文件成对生效、恢复默认只删自定义、**上传失败不动已有图标**、损坏/缺一份按没有处理；
3. app 层四个接口（设置/清除/状态/读取）的状态码与中文业务文案；
4. http 层路由与 multipart 分流（源码级，与既有路由守门同口径）；
5. launcher `_resolve_app_icon`：自定义 > 内置 icon.ico > 兜底绘制，层级间静默回退；
6. 前端入口/弹层/绑定/样式与 `.gitignore` 的静态断言。
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.storage.app_icon import (  # noqa: E402
    APP_ICON_ICO_SIZES,
    APP_ICON_MAX_BYTES,
    APP_ICON_MIN_EDGE,
    app_icon_paths,
    clear_app_icon,
    has_custom_app_icon,
    normalize_app_icon,
    read_app_icon_png,
    store_app_icon,
)


def _png(size: tuple[int, int] = (300, 300), color=(10, 120, 200)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _two_tone_png(size: tuple[int, int] = (400, 200)) -> bytes:
    """左半红、右半蓝：用来证明归一化**没有裁掉**任何一侧。"""
    from PIL import Image

    img = Image.new("RGB", size, (200, 0, 0))
    for x in range(size[0] // 2, size[0]):
        for y in range(size[1]):
            img.putpixel((x, y), (0, 0, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _open(data: bytes):
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def _ico_bytes(size: tuple[int, int] = (256, 256), color=(20, 20, 20)) -> bytes:
    """造一份"内置 icon.ico"（回退路径的输入）。"""
    _png_bytes, ico = normalize_app_icon(_png(size, color))
    return ico


class AppIconNormalizeTests(unittest.TestCase):
    def test_non_square_is_padded_to_square_without_cropping(self) -> None:
        png, _ico = normalize_app_icon(_two_tone_png((400, 200)))
        img = _open(png)
        self.assertEqual(img.size, (400, 400), "长边为边长贴成方形")
        self.assertEqual(img.mode, "RGBA")
        # 上下留白必须是透明的（不是白底/黑底），否则深色托盘上会出现一块白框。
        self.assertEqual(img.getpixel((5, 5))[3], 0, "留白区必须透明")
        self.assertEqual(img.getpixel((395, 395))[3], 0, "留白区必须透明")
        # 原图左半红、右半蓝：两侧都还在 ⇒ 没有裁切。
        left = img.getpixel((5, 200))
        right = img.getpixel((395, 200))
        self.assertEqual(left[:3], (200, 0, 0), "左半内容被裁掉了")
        self.assertEqual(right[:3], (0, 0, 200), "右半内容被裁掉了")

    def test_small_image_is_not_upscaled(self) -> None:
        png, _ico = normalize_app_icon(_png((100, 80)))
        self.assertEqual(_open(png).size, (100, 100))

    def test_oversized_image_is_scaled_to_max_edge(self) -> None:
        png, _ico = normalize_app_icon(_png((4000, 200)))
        img = _open(png)
        self.assertEqual(img.size, (1024, 1024))
        self.assertEqual(img.getpixel((0, 0))[3], 0)

    def test_ico_carries_all_expected_sizes(self) -> None:
        _png_bytes, ico = normalize_app_icon(_png((300, 300)))
        img = _open(ico)
        self.assertEqual(img.format, "ICO")
        got = {size[0] for size in img.ico.sizes()}
        self.assertEqual(got, set(APP_ICON_ICO_SIZES), "ICO 必须齐 7 档尺寸")
        self.assertEqual(max(got), 256)

    def test_rejects_bad_inputs(self) -> None:
        cases = {
            "空文件": b"",
            "不是图片": b"not an image at all",
            "超过 5MB": b"x" * (APP_ICON_MAX_BYTES + 1),
            "太小": _png((APP_ICON_MIN_EDGE - 1, 200)),
        }
        for label, raw in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError) as ctx:
                    normalize_app_icon(raw)
                self.assertTrue(str(ctx.exception).strip(), "必须给可读的中文文案")

    def test_size_error_names_the_limit(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            normalize_app_icon(_png((30, 30)))
        self.assertIn(str(APP_ICON_MIN_EDGE), str(ctx.exception))

    def test_exif_orientation_is_applied(self) -> None:
        """手机竖拍图带 EXIF 旋转：不摆正就会出现"传的是竖的、图标是横的"。"""
        from PIL import Image

        buf = io.BytesIO()
        img = Image.new("RGB", (200, 100), (30, 30, 30))
        exif = img.getexif()
        exif[274] = 6  # Orientation: 顺时针 90°
        img.save(buf, format="JPEG", exif=exif)
        png, _ico = normalize_app_icon(buf.getvalue())
        self.assertEqual(_open(png).size, (200, 200))


class AppIconStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="naiba_app_icon_")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_store_writes_both_files(self) -> None:
        store_app_icon(self.root, _png())
        png_path, ico_path = app_icon_paths(self.root)
        self.assertTrue(png_path.is_file())
        self.assertTrue(ico_path.is_file())
        self.assertTrue(has_custom_app_icon(self.root))
        self.assertTrue(png_path.read_bytes(), "png 不能是空文件")
        self.assertEqual(len(list(self.root.glob("*.part"))), 0, "不留临时文件")

    def test_clear_removes_both_and_is_idempotent(self) -> None:
        store_app_icon(self.root, _png())
        self.assertTrue(clear_app_icon(self.root))
        png_path, ico_path = app_icon_paths(self.root)
        self.assertFalse(png_path.exists())
        self.assertFalse(ico_path.exists())
        self.assertFalse(has_custom_app_icon(self.root))
        self.assertFalse(clear_app_icon(self.root), "已经没了就该回 False")

    def test_failed_upload_keeps_previous_icon(self) -> None:
        store_app_icon(self.root, _png(color=(10, 10, 10)))
        png_path, ico_path = app_icon_paths(self.root)
        before = (png_path.read_bytes(), ico_path.read_bytes())
        with self.assertRaises(ValueError):
            store_app_icon(self.root, b"broken bytes")
        self.assertEqual((png_path.read_bytes(), ico_path.read_bytes()), before, "失败必须原样保留")
        self.assertEqual(len(list(self.root.glob("*.part"))), 0)

    def test_one_file_alone_does_not_count_as_custom(self) -> None:
        png_path, _ico_path = app_icon_paths(self.root)
        png_path.write_bytes(_png())
        self.assertFalse(has_custom_app_icon(self.root), "缺 ico = 半截状态，按没有处理")

    def test_zero_byte_file_does_not_count_as_custom(self) -> None:
        png_path, ico_path = app_icon_paths(self.root)
        png_path.write_bytes(_png())
        ico_path.write_bytes(b"")
        self.assertFalse(has_custom_app_icon(self.root), "半途失败留下的空文件不算生效")

    def test_read_prefers_custom_then_builtin(self) -> None:
        resource = self.root / "res"
        resource.mkdir()
        (resource / "icon.ico").write_bytes(_ico_bytes((256, 256), (7, 7, 7)))
        app_dir = self.root / "app"
        app_dir.mkdir()

        builtin = read_app_icon_png(app_dir, resource)
        self.assertTrue(builtin, "没有自定义时必须回内置图标的 PNG")
        self.assertEqual(_open(builtin).size, (256, 256))

        store_app_icon(app_dir, _png((300, 300), (200, 0, 0)))
        custom = read_app_icon_png(app_dir, resource)
        self.assertEqual(custom, app_icon_paths(app_dir)[0].read_bytes(), "自定义优先，且原样返回")
        self.assertEqual(_open(custom).size, (300, 300))

    def test_read_returns_empty_when_nothing_is_available(self) -> None:
        self.assertEqual(read_app_icon_png(self.root / "nope", self.root / "no-res"), b"")

    def test_broken_custom_png_still_serves_builtin(self) -> None:
        """自定义文件被手工改坏：读取侧要给内置图，不能抛异常（弹窗不能因此炸掉）。"""
        resource = self.root / "res"
        resource.mkdir()
        (resource / "icon.ico").write_bytes(_ico_bytes())
        app_dir = self.root / "app"
        app_dir.mkdir()
        png_path, ico_path = app_icon_paths(app_dir)
        png_path.write_bytes(b"broken")
        ico_path.write_bytes(b"broken")
        data = read_app_icon_png(app_dir, resource)
        self.assertTrue(data)
        self.assertEqual(_open(data).size, (256, 256))


class AppIconApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from naiba.app import NaibaChatApp
        from naiba.paths import PathContext

        self._tmp = tempfile.TemporaryDirectory(prefix="naiba_app_icon_api_")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.paths = PathContext.local(self.root, self.root / "config.json")
        # 内置图标的"资源目录"在测试里指到临时目录：PathContext.local 会把它设成根目录，
        # 而根目录下没有 icon.ico（源码模式下它指向仓库根）。
        self.resource = self.root / "res"
        self.resource.mkdir()
        (self.resource / "icon.ico").write_bytes(_ico_bytes((256, 256)))
        self.paths.resource_dir = self.resource
        self.app = NaibaChatApp(paths=self.paths)
        self.addCleanup(self.app.stop)

    def test_set_status_clear_roundtrip(self) -> None:
        self.assertEqual(self.app.app_icon_status(), {"custom": False})
        payload, status = self.app.api_set_app_icon(_png((300, 600)))
        self.assertEqual(int(status), 200)
        self.assertEqual(payload, {"ok": True, "custom": True})
        self.assertEqual(self.app.app_icon_status(), {"custom": True})
        png_path, ico_path = app_icon_paths(self.root)
        self.assertTrue(png_path.is_file() and ico_path.is_file())

        payload, status = self.app.api_clear_app_icon()
        self.assertEqual(int(status), 200)
        self.assertFalse(payload["custom"])
        self.assertFalse(png_path.exists() or ico_path.exists())
        self.assertEqual(self.app.app_icon_status(), {"custom": False})

    def test_set_rejects_bad_image_with_chinese_message(self) -> None:
        payload, status = self.app.api_set_app_icon(b"nope")
        self.assertEqual(int(status), 400)
        self.assertIn("图片", payload.get("error", ""))
        _, ico_path = app_icon_paths(self.root)
        self.assertFalse(ico_path.exists(), "被拒的上传不能落盘")

    def test_read_icon_returns_png_bytes(self) -> None:
        data, status = self.app.api_read_app_icon()
        self.assertEqual(int(status), 200)
        self.assertEqual(_open(data).size, (256, 256), "默认态回内置图标的 PNG")
        self.app.api_set_app_icon(_png((300, 300)))
        data, status = self.app.api_read_app_icon()
        self.assertEqual(int(status), 200)
        self.assertEqual(_open(data).size, (300, 300))

    def test_read_icon_404_when_nothing_available(self) -> None:
        (self.resource / "icon.ico").unlink()
        payload, status = self.app.api_read_app_icon()
        self.assertEqual(int(status), 404)
        self.assertIn("error", payload)


class AppIconHttpRouteTests(unittest.TestCase):
    """源码级路由守门（与既有路由测试同口径：不真起 HTTP 服务）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.http = (ROOT / "naiba" / "http.py").read_text(encoding="utf-8")

    def test_routes_are_registered(self) -> None:
        self.assertIn('elif path == "/api/app-icon":', self.http)
        self.assertIn('elif path == "/api/app-icon/image":', self.http)
        self.assertIn("self._serve_app_icon()", self.http)
        self.assertIn("self._app_icon_upload()", self.http)

    def test_upload_is_split_before_json_read(self) -> None:
        """multipart 必须在 `_read_json` 之前分流，否则上传体会被当成 JSON 读坏。"""
        upload_at = self.http.index("self._app_icon_upload()")
        self.assertLess(upload_at, self.http.index("body = self._read_json(max_size=130 * 1024 * 1024)"))
        self.assertIn('path == "/api/app-icon" and self.headers.get("Content-Type", "").lower().startswith("multipart/form-data")', self.http)

    def test_upload_size_cap_and_auth(self) -> None:
        self.assertIn("APP_ICON_MAX_BYTES", self.http)
        self.assertIn("from naiba.storage.app_icon import APP_ICON_MAX_BYTES", self.http)
        block = self.http[self.http.index('path == "/api/app-icon" and self.headers.get'):]
        self.assertIn("_authorized(parsed)", block[:400], "上传必须校验访问口令")

    def test_delete_route(self) -> None:
        self.assertIn("self.app.api_clear_app_icon()", self.http)

    def test_get_status_route_returns_custom_flag(self) -> None:
        self.assertIn("self.app.app_icon_status()", self.http)

    def test_image_is_never_cached(self) -> None:
        block = self.http[self.http.index("def _serve_app_icon"):]
        block = block[: block.index("def _upload_request")]
        self.assertIn('"Content-Type", "image/png"', block)
        self.assertIn('"Cache-Control", "no-store"', block, "换/换回图标后刷新必须立刻看到新图")

    def test_json_post_guides_to_multipart(self) -> None:
        self.assertIn("应用图标请以 multipart 表单上传图片", self.http)


class AppIconAuthTests(unittest.TestCase):
    """图标接口与其它设置类接口同权限：非本机请求必须带访问口令。"""

    def _handler(self, client_ip: str, token: str, provided: str = ""):
        import urllib.parse

        from naiba.http import RequestHandler

        handler = RequestHandler.__new__(RequestHandler)
        handler.client_address = (client_ip, 51234)
        handler.headers = {"Authorization": f"Bearer {provided}"} if provided else {}
        from types import SimpleNamespace

        app = SimpleNamespace(config=SimpleNamespace(data={"access_token": token}))
        handler.server = SimpleNamespace(app=app)
        return handler, urllib.parse.urlparse("/api/app-icon")

    def test_local_request_needs_no_token(self) -> None:
        handler, parsed = self._handler("127.0.0.1", "secret")
        self.assertTrue(handler._authorized(parsed))

    def test_lan_request_without_token_is_rejected(self) -> None:
        handler, parsed = self._handler("192.168.5.9", "secret")
        self.assertFalse(handler._authorized(parsed))

    def test_lan_request_with_token_is_accepted(self) -> None:
        handler, parsed = self._handler("192.168.5.9", "secret", provided="secret")
        self.assertTrue(handler._authorized(parsed))


class LauncherIconResolutionTests(unittest.TestCase):
    """`_resolve_app_icon` 的真行为：自定义 > 内置 > 兜底，层间静默回退。"""

    def setUp(self) -> None:
        try:
            import launcher
        except ImportError as exc:  # 桌面依赖缺失时跳过（CI/无 GUI 环境）
            self.skipTest(f"launcher 不可导入：{exc}")
        self.launcher = launcher
        self._tmp = tempfile.TemporaryDirectory(prefix="naiba_launcher_icon_")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.app_dir = self.root / "app"
        self.app_dir.mkdir()
        self.resource = self.root / "res"
        self.resource.mkdir()
        self._prev_app = getattr(launcher.srv, "APP", None)
        self._prev_resource = launcher.srv.RESOURCE_DIR
        launcher.srv.RESOURCE_DIR = self.resource

        def restore() -> None:
            launcher.srv.RESOURCE_DIR = self._prev_resource
            if self._prev_app is None:
                if hasattr(launcher.srv, "APP"):
                    del launcher.srv.APP
            else:
                launcher.srv.APP = self._prev_app

        self.addCleanup(restore)

    def _point_at(self, app_dir: Path) -> None:
        from types import SimpleNamespace

        self.launcher.srv.APP = SimpleNamespace(paths=SimpleNamespace(app_dir=app_dir))

    def test_custom_icon_wins(self) -> None:
        (self.resource / "icon.ico").write_bytes(_ico_bytes((256, 256), (1, 2, 3)))
        store_app_icon(self.app_dir, _png((300, 300), (9, 9, 9)))
        self._point_at(self.app_dir)
        image, ico_path = self.launcher._resolve_app_icon()
        self.assertEqual(image.size, (300, 300))
        self.assertEqual(Path(ico_path), app_icon_paths(self.app_dir)[1], "窗口图标必须用自定义 .ico")

    def test_broken_custom_falls_back_to_builtin(self) -> None:
        (self.resource / "icon.ico").write_bytes(_ico_bytes((256, 256), (1, 2, 3)))
        png_path, ico_path = app_icon_paths(self.app_dir)
        png_path.write_bytes(b"broken")
        ico_path.write_bytes(b"broken")
        self._point_at(self.app_dir)
        image, resolved = self.launcher._resolve_app_icon()
        self.assertEqual(image.size, (256, 256))
        self.assertEqual(Path(resolved), self.resource / "icon.ico")

    def test_valid_png_with_broken_ico_falls_back(self) -> None:
        """只有 `.ico` 坏（PNG 完好）也必须**整体**回退。

        `.ico` 只交给 Windows/pywebview，坏掉不会抛，只会让窗口悄悄退回通用图标——
        那就是「托盘用自定义、窗口用默认」的半截状态，正是本功能要避免的。
        """
        (self.resource / "icon.ico").write_bytes(_ico_bytes((256, 256), (1, 2, 3)))
        store_app_icon(self.app_dir, _png((300, 300)))
        app_icon_paths(self.app_dir)[1].write_bytes(b"not an ico at all")
        self._point_at(self.app_dir)
        image, resolved = self.launcher._resolve_app_icon()
        self.assertEqual(image.size, (256, 256), "托盘也必须一起回退，不能只回退窗口")
        self.assertEqual(Path(resolved), self.resource / "icon.ico")

    def test_png_masquerading_as_ico_falls_back(self) -> None:
        """`.ico` 后缀里装 PNG 内容：Windows 大概率不认，按坏件整体回退。"""
        (self.resource / "icon.ico").write_bytes(_ico_bytes((256, 256), (1, 2, 3)))
        store_app_icon(self.app_dir, _png((300, 300)))
        app_icon_paths(self.app_dir)[1].write_bytes(_png((300, 300)))
        self._point_at(self.app_dir)
        image, resolved = self.launcher._resolve_app_icon()
        self.assertEqual(Path(resolved), self.resource / "icon.ico")
        self.assertEqual(image.size, (256, 256))

    def test_missing_ico_falls_back_to_builtin(self) -> None:
        (self.resource / "icon.ico").write_bytes(_ico_bytes((256, 256), (1, 2, 3)))
        store_app_icon(self.app_dir, _png((300, 300)))
        app_icon_paths(self.app_dir)[1].unlink()
        self._point_at(self.app_dir)
        image, resolved = self.launcher._resolve_app_icon()
        self.assertEqual(Path(resolved), self.resource / "icon.ico")

    def test_last_resort_draws_placeholder(self) -> None:
        self._point_at(self.app_dir)
        image, resolved = self.launcher._resolve_app_icon()
        self.assertIsNone(resolved, "没有可用 .ico 时窗口图标留空，不拦启动")
        self.assertEqual(image.size, (64, 64))

    def test_app_dir_falls_back_to_module_constant(self) -> None:
        """启动早期 `srv.APP` 还没装配：必须退回模块级 APP_DIR，而不是抛 AttributeError。"""
        if hasattr(self.launcher.srv, "APP"):
            del self.launcher.srv.APP
        prev_dir = self.launcher.srv.APP_DIR
        self.launcher.srv.APP_DIR = self.app_dir
        self.addCleanup(lambda: setattr(self.launcher.srv, "APP_DIR", prev_dir))
        store_app_icon(self.app_dir, _png((300, 300)))
        image, resolved = self.launcher._resolve_app_icon()
        self.assertEqual(Path(resolved).name, "custom-icon.ico")
        self.assertEqual(image.size, (300, 300))

    def test_tray_and_window_share_one_resolver(self) -> None:
        """托盘与窗口图标必须同源自 `_resolve_app_icon`（各写一份 = 半截状态）。"""
        source = (ROOT / "launcher.py").read_text(encoding="utf-8")
        self.assertIn("icon_image, icon_path = _resolve_app_icon()", source)
        self.assertIn("self.tray = self._build_tray(local_url, icon_image)", source)
        self.assertIn('start_kwargs["icon"] = icon_path', source)
        self.assertIn("image, _ico_path = _resolve_app_icon()", source, "_build_tray 自己调用时的兜底")
        self.assertEqual(
            source.count('"icon.ico"'), 1,
            "内置图标只在解析器里取一次，不得散落到托盘/窗口两处",
        )


class AppIconFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        cls.js = (ROOT / "public" / "js" / "15-bind-events.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")

    def test_entry_sits_right_of_settings_gear(self) -> None:
        self.assertIn('id="openAppIcon"', self.html)
        self.assertLess(
            self.html.index('id="openSettings"'),
            self.html.index('id="openAppIcon"'),
            "入口必须在齿轮之后（其右侧）",
        )

    def test_dialog_markup(self) -> None:
        for node in ('id="appIconDialog"', 'id="appIconPreview"', 'id="appIconStatus"',
                     'id="appIconPick"', 'id="appIconReset"', 'id="appIconFile"'):
            with self.subTest(node=node):
                self.assertIn(node, self.html)
        self.assertIn('data-close="appIconDialog"', self.html)

    def test_dialog_promises_restart_only(self) -> None:
        """文案只能承诺"重启后生效"：不得暗示 exe 文件图标 / 任务栏即时变化。"""
        self.assertIn("完全退出并重新启动", self.html)
        self.assertNotIn("exe 图标", self.html)
        self.assertNotIn("立即生效", self.html.split('id="appIconDialog"')[1].split("</dialog>")[0])

    def test_reset_button_starts_hidden(self) -> None:
        block = self.html.split('id="appIconDialog"')[1].split("</dialog>")[0]
        self.assertIn('id="appIconReset" type="button" hidden', block)

    def test_bindings_and_resolver_wiring(self) -> None:
        self.assertIn("bindAppIconControls();", self.js)
        self.assertIn("api('/api/app-icon', { method: 'POST', body: form })", self.js)
        self.assertIn("api('/api/app-icon', { method: 'DELETE' })", self.js)
        self.assertIn("api('/api/app-icon')", self.js)
        self.assertIn("token=${encodeURIComponent(state.token)}", self.js)
        self.assertIn("请完全退出并重新启动 Cat Chat 生效", self.js)

    def test_styles_exist(self) -> None:
        for rule in (".app-icon-dialog {", ".app-icon-preview {", ".app-icon-status {", ".app-icon-error[hidden]"):
            with self.subTest(rule=rule):
                self.assertIn(rule, self.css)

    def test_source_mode_files_are_gitignored(self) -> None:
        """源码模式 app_dir = 仓库根：用户换的图标会落在仓库里，绝不能进版本库。"""
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("custom-icon.png", ignore)
        self.assertIn("custom-icon.ico", ignore)


if __name__ == "__main__":
    unittest.main()
