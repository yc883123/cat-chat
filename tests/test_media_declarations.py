# -*- coding: utf-8 -*-
"""媒体类型与采集声明的守门（前后端"同一份名单"的唯一防线）。

背景：后端曾有三份手写同源名单、前端又各写一份正则，漂移已造成真实事故
（`.bmp`/`.svg` 被前端从"修改文件"里滤掉、后端又不当媒体 → 产物在消息里彻底消失；
`.m4v` 漏 MIME 兜底 → 视频可能不播）。本测试冻结以下不变量：

1. 唯一定义（``core/media_types.py``）是各消费方的唯一来源：
   ``attachments.MEDIA_PRODUCT_EXTS``、``storage.media.IMAGE_SUFFIXES``、
   ``http._MEDIA_MIME_FALLBACK``、``/api/bootstrap.media_exts`` 全部由它派生；
2. 每个内置工具在装配期必须带合法的 ``metadata["media"]`` 声明（policy/extract 取值合法、
   声明表无未注册/退役名），未声明的 MCP/第三方工具走默认口径；
3. 前端不再各写扩展名正则：媒体判定一律经 ``mediaKind()`` 读 bootstrap 名单。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.core import attachments as attachments_mod  # noqa: E402
from naiba.core import media_types as media_mod  # noqa: E402
from naiba.storage import media as storage_media_mod  # noqa: E402
from naiba.tools import registry as registry_mod  # noqa: E402


class MediaTypeSingleSourceTests(unittest.TestCase):
    """唯一定义与各消费方必须逐项一致（新增扩展名只改一处即可全链路生效）。"""

    def test_attachments_uses_single_definition(self) -> None:
        self.assertEqual(set(attachments_mod.MEDIA_PRODUCT_EXTS), set(media_mod.MEDIA_EXTS))

    def test_upload_image_suffixes_derived(self) -> None:
        self.assertEqual(
            set(storage_media_mod.IMAGE_SUFFIXES), set(media_mod.IMAGE_PROCESS_EXTS)
        )
        # GIF 不参与压缩（保动画），但必须能生成首帧缩略图（否则前端推导 404 破图）。
        self.assertNotIn(".gif", storage_media_mod.IMAGE_SUFFIXES)
        self.assertIn(".gif", storage_media_mod.THUMB_SOURCE_SUFFIXES)

    def test_mime_fallback_covers_every_media_ext(self) -> None:
        from naiba import http as http_mod

        missing = sorted(set(media_mod.MEDIA_EXTS) - set(http_mod._MEDIA_MIME_FALLBACK))
        self.assertEqual(missing, [], f"媒体扩展名缺少 MIME 兜底：{missing}")
        # .m4v 曾在两处名单里被当作视频、却漏了 MIME 兜底（可能不播）。
        self.assertEqual(http_mod._MEDIA_MIME_FALLBACK.get(".m4v"), "video/x-m4v")

    def test_bootstrap_payload_shape(self) -> None:
        payload = media_mod.media_exts_payload()
        self.assertEqual(sorted(payload["exts"]), sorted(media_mod.MEDIA_KINDS))
        for kind, exts in media_mod.MEDIA_EXTS_BY_KIND.items():
            self.assertEqual(list(exts), payload["exts"][kind])
            self.assertEqual(payload["limits"][kind], media_mod.MEDIA_BUCKET_LIMITS[kind])
        # 分桶上限必须存在且为正（前端提示块与宿主预截断共用）。
        self.assertEqual(payload["limits"], {"image": 20, "video": 8, "audio": 8})

    def test_bootstrap_endpoint_exposes_media_exts(self) -> None:
        source = (ROOT / "naiba" / "app.py").read_text(encoding="utf-8")
        self.assertIn('"media_exts": media_exts_payload()', source, "bootstrap 未暴露媒体名单")

    def test_media_kind_of_sources(self) -> None:
        cases = {
            "C:\\work\\a.png": "image",
            "C:\\work\\a.PNG": "image",
            "/tmp/a.webp?size=2": "image",
            "https://example.com/a.mp4": "video",
            "http://127.0.0.1:8188/view?filename=a.gif&type=output": "image",
            "D:\\a.m4v": "video",
            "D:\\a.flac": "audio",
            "C:\\work\\a.txt": None,
            "C:\\work\\a.bmp": None,  # 非媒体卡（前端按文件 chip 显示，不再静默消失）
            "C:\\work\\a.svg": None,
            "": None,
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(media_mod.media_kind_of(source), expected)


class MediaDeclarationTests(unittest.TestCase):
    """工具媒体声明：内置工具必须显式声明，取值合法，声明表与注册表对账。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = registry_mod.build_tool_registry()

    def test_every_builtin_tool_declares_media(self) -> None:
        for name in self.registry.names():
            with self.subTest(tool=name):
                spec = self.registry.get(name)
                self.assertIsNotNone(spec)
                declaration = (spec.metadata or {}).get("media")
                self.assertIsNotNone(declaration, f"工具缺少 media 声明：{name}")
                self.assertIn(declaration["policy"], media_mod.MEDIA_POLICIES)
                self.assertIn(declaration["extract"], media_mod.MEDIA_EXTRACTORS)

    def test_declaration_table_matches_registry(self) -> None:
        declared = set(registry_mod.MEDIA_DECLARATIONS)
        registered = set(self.registry.names())
        self.assertEqual(
            sorted(registered - declared), [], "内置工具未登记媒体声明（装配期会报错）"
        )
        self.assertEqual(sorted(declared - registered), [], "媒体声明表存在未注册/退役工具名")

    def test_declaration_values_normalize(self) -> None:
        for name, declaration in registry_mod.MEDIA_DECLARATIONS.items():
            with self.subTest(tool=name):
                normalized = media_mod.normalize_media_declaration(declaration)
                self.assertEqual(normalized, {"policy": declaration["policy"], "extract": declaration["extract"]})

    def test_unknown_tool_falls_back_to_default(self) -> None:
        # MCP/第三方动态工具没有声明：走默认口径而不是报错。
        self.assertEqual(
            registry_mod.media_declaration_for("mcp__demo__render"),
            media_mod.DEFAULT_MEDIA_DECLARATION,
        )
        self.assertEqual(
            media_mod.normalize_media_declaration(None), media_mod.DEFAULT_MEDIA_DECLARATION
        )

    def test_invalid_declaration_rejected(self) -> None:
        for bad in ({"policy": "always", "extract": "scan"}, {"policy": "inline", "extract": "regex"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    media_mod.normalize_media_declaration(bad)

    def test_declare_media_rejects_unknown_tool(self) -> None:
        registry = registry_mod.ToolRegistry()
        with self.assertRaises(KeyError):
            registry.declare_media("no_such_tool", {"policy": "inline", "extract": "scan"})

    def test_media_metadata_not_exposed_to_model(self) -> None:
        for row in self.registry.schemas():
            with self.subTest(tool=row["name"]):
                self.assertNotIn("metadata", row, "metadata（含 media 声明）不得进入模型可见 schema")

    def test_enumeration_gate_is_declared_not_hardcoded(self) -> None:
        """枚举类工具的媒体门禁由声明承担（不再有 ENUMERATION_TOOLS 硬编码集合）。"""
        gated = {
            name
            for name, declaration in registry_mod.MEDIA_DECLARATIONS.items()
            if declaration["policy"] == "intent_gated"
        }
        self.assertEqual(gated, {"list_directory", "search_files", "grep"})
        self.assertTrue(gated <= set(self.registry.names()), "门禁集合里有未声明的工具名")

    def test_registry_exposes_media_declaration_with_alias_normalization(self) -> None:
        # 别名（grep→search_files）必须归一到同一份声明。
        self.assertEqual(
            self.registry.media_declaration("grep"),
            self.registry.media_declaration("search_files"),
        )
        self.assertEqual(self.registry.media_declaration("write_file")["policy"], "inline")
        self.assertEqual(self.registry.media_declaration("web_search")["extract"], "none")

    def test_startup_registration_order_preserves_declarations(self) -> None:
        """真实启动顺序（build → 各域 Provider 覆盖注册）后声明必须原样存活。

        真实事故 2026-10（外部用户 3.1.8-beta 排查报告）：CoreToolProvider 用
        全新 spec 覆盖注册，装配期 declare_media 写入的声明整体丢失，
        list_directory/search_files/read_file 回落默认 inline/scan——目录枚举
        结果里的历史旧图被自动采集附图。只对账名单的守门拦不住这类覆盖，
        必须按真实启动顺序注册后再逐名对账**取值**。
        """
        from naiba.tools.providers.core import CoreToolProvider, ToolContext
        from naiba.tools.providers.documents import DocumentToolProvider
        from naiba.tools.providers.video import VideoToolProvider

        registry = registry_mod.build_tool_registry()
        ctx = ToolContext(
            workspace=ROOT,
            python_executable=sys.executable,
            command_timeout=120,
            mcp_registry=None,
        )
        registry.register_provider(CoreToolProvider(ctx))
        registry.register_provider(DocumentToolProvider(ctx, lambda: ROOT))
        registry.register_provider(VideoToolProvider(ctx, lambda: ROOT))

        # Provider 覆盖后名单不得增减（防 Provider 又引入未声明的新工具名）。
        self.assertEqual(sorted(registry.names()), sorted(registry_mod.MEDIA_DECLARATIONS))
        for name, declaration in registry_mod.MEDIA_DECLARATIONS.items():
            with self.subTest(tool=name):
                self.assertEqual(
                    registry.media_declaration(name),
                    media_mod.normalize_media_declaration(declaration),
                )
        # 本次事故最危险形态：读文本/列目录工具不得回落 inline（自动附图）。
        self.assertEqual(
            registry.media_declaration("read_file"), {"policy": "never", "extract": "none"}
        )
        self.assertEqual(
            registry.media_declaration("read"), {"policy": "never", "extract": "none"}
        )
        self.assertEqual(
            registry.media_declaration("list_directory")["policy"], "intent_gated"
        )
        self.assertEqual(
            registry.media_declaration("search_files")["policy"], "intent_gated"
        )


class FrontendMediaListTests(unittest.TestCase):
    """前端不再各写扩展名名单：判定统一经 mediaKind() 读 /api/bootstrap.media_exts。"""

    JS_FILES = ("03-media.js", "04-messages.js", "10-upload.js")
    # 旧实现里的硬编码扩展名正则片段（出现即说明又写了一份名单）。
    BANNED_SNIPPETS = ("png|jpe?g", "mp4|webm|mov", "wav|mp3|m4a", "FILE_CHIP_MEDIA_EXTS")

    def _read(self, name: str) -> str:
        return (ROOT / "public" / "js" / name).read_text(encoding="utf-8")

    def test_media_kind_is_defined_and_exported(self) -> None:
        source = self._read("03-media.js")
        self.assertIn("export function mediaKind(", source)
        self.assertIn("media_exts", source, "mediaKind 必须读后端名单")

    def test_no_hardcoded_extension_lists(self) -> None:
        for name in self.JS_FILES:
            source = self._read(name)
            for snippet in self.BANNED_SNIPPETS:
                with self.subTest(file=name, snippet=snippet):
                    self.assertNotIn(snippet, source, "前端又写了一份媒体扩展名名单")

    def test_consumers_use_media_kind(self) -> None:
        for name in ("04-messages.js", "10-upload.js"):
            with self.subTest(file=name):
                self.assertIn("mediaKind(", self._read(name))

    def test_media_markup_uses_media_kind(self) -> None:
        source = self._read("03-media.js")
        self.assertIn("mediaKind(source, attachment.name)", source)


class FrontendInlineMediaTests(unittest.TestCase):
    """媒体就地内嵌（P3）的接线守门：源码级断言。

    本沙箱无法跑浏览器冒烟（Edge spawn 被拦），因此把"流式插入/末尾去重/截断提示"
    这几处关键接线钉在源码上；渲染结果由 `verify/media_markup_check.mjs` 真执行校验。
    """

    def _read(self, name: str) -> str:
        return (ROOT / "public" / "js" / name).read_text(encoding="utf-8")

    def test_tool_block_renders_media_inline(self) -> None:
        source = self._read("03-media.js")
        self.assertIn("export function toolMediaMarkup(", source)
        self.assertIn("${toolMediaMarkup(run)}", source, "toolRunMarkup 必须复用媒体块")

    def test_streaming_inserts_media_block_after_tool_block(self) -> None:
        source = self._read("12-chat-input.js")
        self.assertIn("toolMediaMarkup(event)", source)
        self.assertIn("classList.contains('tool-media')", source)
        self.assertIn("insertAdjacentHTML('afterend', markup)", source)

    def test_bottom_grid_dedupes_inline_media(self) -> None:
        source = self._read("04-messages.js")
        self.assertIn("remainingAttachments(metadata)", source)

    def test_truncation_notice_is_rendered(self) -> None:
        self.assertIn("media-truncated", self._read("03-media.js"))
        self.assertIn(".media-truncated", (ROOT / "public" / "styles.css").read_text(encoding="utf-8"))

    def test_inline_media_block_has_styles(self) -> None:
        css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".tool-media", css)
        self.assertIn(".message-body > .tool-media", css)


class FileChangeChipAlignmentTests(unittest.TestCase):
    """`.bmp`/`.svg` 产物曾经两头都不显示：现在必须落回"修改文件"chip（可见）。"""

    def test_non_media_paths_stay_file_chips(self) -> None:
        source = (ROOT / "public" / "js" / "03-media.js").read_text(encoding="utf-8")
        self.assertIn("mediaKind(raw, f.name) !== 'other'", source)

    def test_backend_keeps_non_media_out_of_media_set(self) -> None:
        for ext in (".bmp", ".svg", ".avif"):
            with self.subTest(ext=ext):
                self.assertFalse(media_mod.is_media_path(f"a{ext}"))
        self.assertTrue(media_mod.is_media_path("a.png"))

    def test_backend_file_changes_keeps_media_excluded(self) -> None:
        from naiba.core.file_changes import file_changes_from_runs

        runs = [
            {"tool": "write_file", "success": True, "arguments": {"path": "C:\\a.png"}},
            {"tool": "write_file", "success": True, "arguments": {"path": "C:\\a.bmp"}},
            {"tool": "write_file", "success": True, "arguments": {"path": "C:\\a.txt"}},
        ]
        paths = [row["path"] for row in file_changes_from_runs(runs)]
        self.assertEqual(paths, ["C:\\a.bmp", "C:\\a.txt"], "媒体产物不进'修改文件'，非媒体必须保留")


if __name__ == "__main__":
    unittest.main()
