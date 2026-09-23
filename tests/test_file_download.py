# -*- coding: utf-8 -*-
"""护栏：手机/桌面「把文件保存到本地」的响应头（§九.135 第 3 项）。

缺陷形态：`/api/file` 与 `/api/conversations/<id>/file/raw` 都**只发 Content-Type**，
手机浏览器因此只能内联预览——点开 .md / .py 就是把文本显示在页面里，没有任何"保存"入口；
而前端全局也没有下载按钮。手机上「下载已生成/修改的文件」这条路整条不通。

修法：两个端点都支持 `?download=1`，命中时补一个
`Content-Disposition: attachment; filename*=UTF-8''<百分号编码>`（RFC 6266/5987）。
**默认行为一字不动**（内联预览照旧），所以这里既正面断言 `download=1` 的头，
也负向断言「不带参数时没有这个头」「Range 语义与 Cache-Control 不受影响」
（视频 seek 走同一个函数，动它们就会波及播放）。

真机形态另有 `verify/file_download_smoke.cjs`（真后端 + 真点击，看浏览器是否真的走下载）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.http import AppHTTPServer, RequestHandler, content_disposition_attachment  # noqa: E402


class ContentDispositionTests(unittest.TestCase):
    """头本身的形态：中文走 RFC 5987，同时留一个纯 ASCII 兜底。"""

    def test_chinese_name_uses_rfc5987(self):
        header = content_disposition_attachment("测试文件.md")
        encoded = "%E6%B5%8B%E8%AF%95%E6%96%87%E4%BB%B6.md"
        self.assertEqual(header, f"attachment; filename=\"____.md\"; filename*=UTF-8''{encoded}")
        # 头里不许出现任何非 latin-1 字符（HTTP 头只能 latin-1，塞进去就会乱码或丢字）。
        header.encode("latin-1")

    def test_ascii_name_kept_in_both_forms(self):
        header = content_disposition_attachment("report-final.txt")
        self.assertIn('filename="report-final.txt"', header)
        self.assertIn("filename*=UTF-8''report-final.txt", header)

    def test_path_separators_and_controls_are_stripped(self):
        header = content_disposition_attachment('..\\dir/sub\r\n"evil".txt')
        self.assertNotIn("\r", header)
        self.assertNotIn("\n", header)
        self.assertNotIn("dir/", header)
        self.assertNotIn("\\", header)
        self.assertTrue(header.startswith("attachment; "))

    def test_empty_name_falls_back(self):
        self.assertIn('filename="download"', content_disposition_attachment(""))
        self.assertIn('filename="download"', content_disposition_attachment("   "))


class _StubApp:
    """`_serve_local_file` / `/file/raw` 会碰到的东西只有这三样。"""

    def __init__(self, data_dir: Path, workspace: Path, conversation=None) -> None:
        self.paths = SimpleNamespace(data_dir=Path(data_dir))
        # `_conv_workspace_root` 会带会话调用它（`resolve_workspace_dir(conversation)`），
        # 所以替身要吃得下位置参数——写成零参 lambda 会以 500 的形式炸在路由里。
        self.config = SimpleNamespace(
            data={"access_token": ""},
            resolve_workspace_dir=lambda *args, **kwargs: Path(workspace),
        )
        self._conversation = conversation
        self.storage = SimpleNamespace(get_conversation=lambda _cid: self._conversation)


class DownloadHeaderRouteTests(unittest.TestCase):
    """HTTP 层：带 / 不带 `download=1` 两个口径都要钉住。"""

    def setUp(self) -> None:
        self.data_dir = Path(tempfile.mkdtemp(prefix="download_hdr_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.data_dir, ignore_errors=True))
        self.workspace = self.data_dir / "ws"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.cn_file = self.workspace / "生成结果.md"
        self.cn_file.write_text("# 你好\n正文\n", encoding="utf-8")
        self.video = self.workspace / "clip.mp4"
        self.video.write_bytes(bytes(range(256)) * 8)

    def _serve(self, conversation=None) -> str:
        app = _StubApp(self.data_dir, self.workspace, conversation)
        server = AppHTTPServer(("127.0.0.1", 0), RequestHandler, app)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()

        def _stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(_stop)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def _get(self, url: str, headers: dict | None = None):
        request = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    # ---- /api/file ----

    def test_api_file_default_still_inline(self):
        """回归：不带参数时**不得**出现 Content-Disposition（内联预览的设备/灯箱零影响）。"""
        base = self._serve()
        url = f"{base}/api/file?path={urllib.parse.quote(str(self.cn_file))}"
        status, headers, body = self._get(url)
        self.assertEqual(status, 200)
        self.assertNotIn("Content-Disposition", headers)
        self.assertIn("text/markdown", headers.get("Content-Type", ""))
        self.assertEqual(body, self.cn_file.read_bytes())   # 别写死换行符：Windows 上 write_text 会写成 \r\n

    def test_api_file_download_sets_attachment(self):
        base = self._serve()
        url = f"{base}/api/file?path={urllib.parse.quote(str(self.cn_file))}&download=1"
        status, headers, body = self._get(url)
        self.assertEqual(status, 200)
        self.assertEqual(
            headers.get("Content-Disposition"),
            "attachment; filename=\"____.md\"; filename*=UTF-8''%E7%94%9F%E6%88%90%E7%BB%93%E6%9E%9C.md",
        )
        self.assertEqual(body, self.cn_file.read_bytes())   # 别写死换行符：Windows 上 write_text 会写成 \r\n

    def test_download_keeps_cache_and_range_headers(self):
        """加了附件头也不许动 Cache-Control / Accept-Ranges（视频 seek 走同一个函数）。"""
        base = self._serve()
        url = f"{base}/api/file?path={urllib.parse.quote(str(self.video))}&download=1"
        status, headers, _ = self._get(url, {"Range": "bytes=0-99"})
        self.assertEqual(status, 206)
        self.assertEqual(headers.get("Content-Range"), f"bytes 0-99/{self.video.stat().st_size}")
        self.assertEqual(headers.get("Accept-Ranges"), "bytes")
        self.assertEqual(headers.get("Cache-Control"), "private, max-age=3600")
        self.assertEqual(headers.get("Content-Length"), "100")
        self.assertTrue(headers.get("Content-Disposition", "").startswith("attachment; "))

    def test_download_only_on_exact_flag_value(self):
        """只有 `download=1` 才算；`download=0` / 空值必须维持内联（别把布尔判断写松）。"""
        base = self._serve()
        for flag in ("0", "", "true", "yes"):
            with self.subTest(flag=flag):
                url = f"{base}/api/file?path={urllib.parse.quote(str(self.cn_file))}&download={flag}"
                status, headers, _ = self._get(url)
                self.assertEqual(status, 200)
                self.assertNotIn("Content-Disposition", headers)

    # ---- /api/conversations/<id>/file/raw ----

    def _raw_url(self, base: str, conversation_id: str, path: Path, flag: str = "") -> str:
        url = (
            f"{base}/api/conversations/{conversation_id}/file/raw"
            f"?path={urllib.parse.quote(str(path))}"
        )
        return f"{url}&download={flag}" if flag else url

    def test_file_raw_download_sets_attachment(self):
        conversation = {"id": "conv-a", "workspace": str(self.workspace)}
        base = self._serve(conversation)
        status, headers, body = self._get(self._raw_url(base, "conv-a", self.cn_file, "1"))
        self.assertEqual(status, 200)
        self.assertEqual(
            headers.get("Content-Disposition"),
            "attachment; filename=\"____.md\"; filename*=UTF-8''%E7%94%9F%E6%88%90%E7%BB%93%E6%9E%9C.md",
        )
        self.assertEqual(body, self.cn_file.read_bytes())   # 别写死换行符：Windows 上 write_text 会写成 \r\n

    def test_file_raw_default_still_inline(self):
        conversation = {"id": "conv-a", "workspace": str(self.workspace)}
        base = self._serve(conversation)
        status, headers, _ = self._get(self._raw_url(base, "conv-a", self.cn_file))
        self.assertEqual(status, 200)
        self.assertNotIn("Content-Disposition", headers)

    def test_file_raw_still_rejects_outside_files(self):
        """安全口径不因为多了一个参数而松动：不在会话改动记录、也不在工作区内的文件照样 403/400。"""
        conversation = {"id": "conv-a", "workspace": str(self.workspace)}
        base = self._serve(conversation)
        outside = self.data_dir / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        status, _, body = self._get(self._raw_url(base, "conv-a", outside, "1"))
        self.assertIn(status, (400, 403))
        self.assertIn("error", json.loads(body.decode("utf-8")))


class LocalSaveRoutingTests(unittest.TestCase):
    """「把文件存到本地」在桌面与手机必须走两条不同的路（§九.135 补，用户实测反馈）。

    背景：桌面是 WebView2，`<a download>` + `Content-Disposition: attachment` 的导航**不会**
    弹保存框（要宿主处理 DownloadStarting，本项目没接）⇒ 用户点灯箱那颗 ↓ 没有任何反应，
    原话「电脑上点下载这张图没用」。修法是把「下载」收敛到 `saveLocalFile` 一个入口：
    有 pywebview 桥就走原生「另存为」JsApi，没有才走 `<a download>`。

    这几条是源码级护栏（真实行为由 `verify/file_download_smoke.cjs` 的 D 段覆盖）：
    单点实现、两条调用链都接上、同源门与「取消不算失败」都不能被改掉。
    """

    @classmethod
    def setUpClass(cls):
        cls.media = (ROOT / "public/js/03-media.js").read_text(encoding="utf-8")
        cls.bind = (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")

    def _body(self, source: str, header: str) -> str:
        at = source.index(header)
        return source[at : source.index("\n}", at)]

    @staticmethod
    def _code_only(source: str) -> str:
        """剔掉整行注释。

        负向断言必须先剔注释：本文件里就有「后端据 `download=1` 发 attachment」这种
        说明性注释，扫描时会把注释里的字样当成实现（§九.134 的同类教训）。
        """
        return "\n".join(line for line in source.split("\n") if not line.strip().startswith("//"))

    def test_save_local_file_single_entry(self) -> None:
        """分流逻辑只许有一份：`saveLocalFile` 同时被灯箱与面板调用。"""
        body = self._body(self.media, "export function saveLocalFile(")
        self.assertIn("naibaSaveFileAs", body, "桌面分支必须调原生「另存为」")
        self.assertIn("triggerDownload", body, "浏览器/手机分支必须仍走 <a download>")
        self.assertIn("localPathFromUrl", body, "取路径要走同源门，别直接信 URL")
        # 两条调用链都必须用它；写死 triggerDownload 就等于桌面又点了没反应。
        self.assertIn("saveLocalFile(url", self.media, "灯箱下载必须走 saveLocalFile")
        self.assertIn("saveLocalFile(convFileRawUrl(", self.bind, "文件面板「下载」必须走 saveLocalFile")
        self.assertNotIn(
            "triggerDownload(convFileRawUrl(", self._code_only(self.bind),
            "面板还留着旧写法，桌面会失效",
        )

    def test_local_path_only_same_origin(self) -> None:
        """`localPathFromUrl` 只认同源 URL 的 `path`——跨源地址绝不能拿它的 path 去碰本机文件。"""
        body = self._body(self.media, "export function localPathFromUrl(")
        self.assertIn("parsed.origin !== location.origin", body)
        self.assertIn("'path'", body)

    def test_cancel_is_not_a_failure(self) -> None:
        """用户在原生保存框按「取消」不是失败：不许弹「另存为失败」吓人。"""
        body = self._body(self.media, "export function saveLocalFile(")
        self.assertIn("cancelled", body)
        self.assertIn("result.ok === false", body)

    def test_call_sites_pass_bare_url(self) -> None:
        """调用点只给裸 URL：`download=1` 一律由单点补，避免两处各写一遍慢慢漂移。"""
        for source, name in ((self.media, "03-media.js"), (self.bind, "15-bind-events.js")):
            with self.subTest(file=name):
                code = self._code_only(source)
                self.assertNotIn("saveLocalFile(fileUrl(", code, "别在调用点先拼一遍 URL")
                self.assertNotIn("download=1", code, "`download=1` 只许在 01-core.triggerDownload 里出现")


class AttachAcceptTests(unittest.TestCase):
    """附件「文件」入口的 accept 必须带 MIME（§九.135 补，用户实测「点了文件还是跳相册」）。

    第一版只给扩展名清单，在部分 Android ROM/浏览器上会被当成"没给约束"而回落 `*/*`，
    弹出来的第一个建议还是相册。判据两条：①给了 `application/*` 或 `text/*`；
    ②**不含 `image/*` 与 `video/*`**——含了就等于把相册请回来。真实行为由
    `verify/attach_picker_smoke.cjs` 的 ⑩⑪⑪-1⑪-2 覆盖。
    """

    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")

    def _accept(self, key: str) -> str:
        at = self.source.index("const ATTACH_ACCEPT = {")
        block = self.source[at : self.source.index("\n};", at)]
        line = next(text for text in block.split("\n") if text.strip().startswith(f"{key}:"))
        return line

    def test_file_entry_carries_mime_types(self) -> None:
        line = self._accept("file")
        self.assertIn("FILE_ACCEPT_MIMES", line, "「文件」入口必须由 MIME 清单驱动")
        mimes = self.source[
            self.source.index("const FILE_ACCEPT_MIMES = [") : self.source.index("const FILE_ACCEPT_EXTS = [")
        ]
        self.assertIn("'application/pdf'", mimes)
        self.assertIn("'text/plain'", mimes)
        self.assertIn("'text/markdown'", mimes, "传个 .md 给 AI 看是最常见的用法")

    def test_file_entry_excludes_media(self) -> None:
        """多媒体 MIME 一个都不许出现在「文件」入口里——否则系统又把相册摆到第一个。"""
        mimes = self.source[
            self.source.index("const FILE_ACCEPT_MIMES = [") : self.source.index("const FILE_ACCEPT_EXTS = [")
        ]
        self.assertNotIn("image/", mimes)
        self.assertNotIn("video/", mimes)
        self.assertNotIn("audio/", mimes)

    def test_media_entry_unchanged(self) -> None:
        self.assertIn("'image/*,video/*'", self._accept("media"))

    def test_only_one_file_input(self) -> None:
        """上传链路只能一条：切的是同一个 input 的 `accept`，不新建第二个 input。

        范围限定在**输入区表单**里——页面上还有技能导入 / 头像 / 背景图等各自独立的
        file input（全页共 7 个），它们与「往对话里传文件」不是同一条链路。
        """
        html = (ROOT / "public/index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count('id="fileInput"'), 1)
        at = html.index('id="composerForm"')
        form = html[at : html.index("</form>", at)]
        self.assertEqual(form.count('type="file"'), 1, "输入区里只能有一个 file input")
        self.assertIn('<input id="fileInput" type="file" multiple hidden>', form)
        self.assertEqual(self.source.count("input.accept ="), 1, "accept 只许有一个写入点")


if __name__ == "__main__":
    unittest.main()
