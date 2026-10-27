# -*- coding: utf-8 -*-
"""守门：静态资源目录失效时的降级页（手机白屏修复，2026-10-17 用户截图）。

背景：onefile 打包的 ``%TEMP%\\_MEIxxxx`` 资源目录在自动更新重启窗口 / Temp 清理后
会被删除，而服务进程仍占着端口——桌面端资源已在内存照常可用，手机端请求 ``/`` 却
FileNotFoundError→500 JSON 白屏。修复后：
- public_dir 可读：行为与旧版逐字节一致（守门见旧有冒烟与 hash 测试）；
- public_dir 不可读：返回 503 + 纯内嵌 HTML 兜底页（零文件依赖、自动重试、恢复指引），
  **不是** 500 JSON。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


class _StubHandler:
    """只覆盖 _serve_static 判定链的最小 handler（不发真 socket）。"""

    from naiba.http import RequestHandler

    _serve_static = RequestHandler._serve_static
    _serve_static_degraded = RequestHandler._serve_static_degraded

    def __init__(self, public_dir: Path):
        self.app = SimpleNamespace(paths=SimpleNamespace(public_dir=public_dir))
        self.headers: list[tuple[int, str]] = []
        self._status = None
        self.body = b""
        self.errored: tuple[int, str] | None = None

    # ---- BaseHTTPRequestHandler 最小面 ----
    def send_response(self, status):
        self._status = status

    def send_header(self, name, value):
        self.headers.append((self._status, f"{name}: {value}"))

    def end_headers(self):
        pass

    def send_error(self, status, message=""):
        self.errored = (status, message)

    @property
    def wfile(self):
        return self

    def write(self, data):
        self.body = data

    def flush(self):
        pass


def _drive(handler: _StubHandler, requested: str) -> dict:
    handler._status = None
    handler.body = b""
    handler.headers = []
    handler._serve_static(requested)
    return {"status": handler._status, "body": handler.body, "headers": handler.headers, "errored": handler.errored}


class StaticDegradedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_normal_serving_unchanged(self):
        public = self.root / "public"
        (public / "js").mkdir(parents=True)
        (public / "index.html").write_bytes(b"<html>__ASSET_VERSION__</html>")
        (public / "js" / "main.js").write_bytes(b"// ok")  # static_asset_version 会扫 js/*.js
        (public / "styles.css").write_bytes(b"body{}")  # 同上：版本哈希也读 styles.css
        handler = _StubHandler(public)
        result = _drive(handler, "/")
        self.assertEqual(result["status"], HTTPStatus.OK)
        # 正常路径行为逐字节保持：占位符被静态版本哈希替换（哈希来自 test 里写的 js/css）
        self.assertNotIn(b"__ASSET_VERSION__", result["body"])
        self.assertRegex(result["body"].decode("ascii"), r"^<html>[0-9a-f]{12}</html>$")

    def test_missing_public_dir_serves_degraded_page(self):
        # 目录从未存在（模拟 _MEI 被清）：不再是未捕获异常 / 500 JSON
        handler = _StubHandler(self.root / "gone")
        result = _drive(handler, "/")
        self.assertEqual(result["status"], HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertIn("Cat Chat 正在恢复".encode("utf-8"), result["body"])
        self.assertIn(b"location.reload", result["body"])
        self.assertTrue(any(h[1].startswith("Content-Type: text/html") for h in result["headers"]))
        self.assertTrue(any(h[1] == "Cache-Control: no-store" for h in result["headers"]))

    def test_degraded_page_has_no_external_resources(self):
        # 兜底页此刻拿不到 js/css：不得引用任何外部文件
        handler = _StubHandler(self.root / "gone")
        result = _drive(handler, "/")
        body = result["body"].decode("utf-8")
        self.assertNotIn(".js", body)
        self.assertNotIn(".css", body)
        self.assertNotIn("src=", body)
        self.assertNotIn("href=", body)

    def test_subpath_also_degrades(self):
        handler = _StubHandler(self.root / "gone")
        result = _drive(handler, "js/main.js")
        self.assertEqual(result["status"], HTTPStatus.SERVICE_UNAVAILABLE)

    def test_traversal_still_forbidden(self):
        public = self.root / "public"
        public.mkdir()
        handler = _StubHandler(public)
        # 逃出 public_dir 的路径必须维持 send_error(403)（降级逻辑不放宽边界）
        result = _drive(handler, "../escape.html")
        self.assertEqual(result["errored"][0], HTTPStatus.FORBIDDEN)
        self.assertIsNone(result["status"])

    def test_missing_asset_returns_404_instead_of_index_html(self):
        """带扩展名的资源缺失时不能回落成首页。

        回落成 index.html 的表现是「一张 HTML 被当图片/脚本」，浏览器侧静默破图或被
        nosniff 拒掉——排查时几乎看不出是文件不存在（模型皮肤正是踩这个坑的场景：
        皮肤键写错只会让图空着，不报任何错）。
        """
        public = self.root / "public"
        (public / "js").mkdir(parents=True)
        (public / "index.html").write_bytes(b"<html>__ASSET_VERSION__</html>")
        (public / "js" / "main.js").write_bytes(b"// ok")
        (public / "styles.css").write_bytes(b"body{}")
        handler = _StubHandler(public)
        for requested in ("/skins/99_missing.webp", "/js/nope.js", "/styles-nope.css"):
            with self.subTest(requested=requested):
                result = _drive(handler, requested)
                self.assertEqual(HTTPStatus.NOT_FOUND, result["errored"][0])
                self.assertIsNone(result["status"])

    def test_missing_route_without_extension_still_serves_index(self):
        """无扩展名的路径仍走 SPA 兜底（刷新子页 / 前端路由不能变成 404）。"""
        public = self.root / "public"
        (public / "js").mkdir(parents=True)
        (public / "index.html").write_bytes(b"<html>__ASSET_VERSION__</html>")
        (public / "js" / "main.js").write_bytes(b"// ok")
        (public / "styles.css").write_bytes(b"body{}")
        handler = _StubHandler(public)
        result = _drive(handler, "/some/route")
        self.assertEqual(HTTPStatus.OK, result["status"])
        self.assertIn(b"<html>", result["body"])


if __name__ == "__main__":
    unittest.main()
