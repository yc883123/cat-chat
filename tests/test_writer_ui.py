# -*- coding: utf-8 -*-
"""护栏：织书台（原创写作）前端接入 —— 命名守门与结构契约。

覆盖（实施计划 §12.1「命名」与「兼容」条目）：
- 第四个模块只显示「拆书匠」，第五个只显示「织书台」；`novel` / `writer`
  内部标识、API 路径与数据目录不变；
- 历史任务里的旧名「创作场」由展示层按 module 映射兼容（记录本身不重写）；
- 模块库任务过滤改用 `exclude_modules=novel,writer`（旧 `exclude_module` 保留）；
- 织书台页面结构：三阶段 + 三栏 + 四个弹窗；无内联 onclick；
- 新任务来源标签：拆书匠新任务 origin_label=拆书匠、织书台 origin_label=织书台。
"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class NamingGateTests(unittest.TestCase):
    """显示名守门：内部标识不动，显示层只认新名。"""

    def setUp(self):
        self.index = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        self.studio_js = (ROOT / "public" / "js" / "22-studio.js").read_text(encoding="utf-8")
        self.novel_js = (ROOT / "public" / "js" / "25-novel.js").read_text(encoding="utf-8")
        self.writer_js = (ROOT / "public" / "js" / "27-writer.js").read_text(encoding="utf-8")
        self.bind_js = (ROOT / "public" / "js" / "15-bind-events.js").read_text(encoding="utf-8")

    def test_nav_shows_new_names_only(self):
        sidebar = self.index[self.index.index('id="studioPages"'):]
        sidebar = sidebar[: sidebar.index("</nav>")]
        self.assertIn(">拆书匠</span>", sidebar, "第四页导航显示「拆书匠」")
        self.assertIn(">织书台</span>", sidebar, "第五页导航显示「织书台」")
        self.assertNotIn("创作场", sidebar, "侧栏不应再出现旧名「创作场」")
        self.assertIn('data-studio-page="novel"', sidebar, "内部标识 novel 不变")
        self.assertIn('data-studio-page="writer"', sidebar, "内部标识 writer 不变")

    def test_novel_page_title_and_api_unchanged(self):
        page = self.index[self.index.index('id="studioPageNovel"'):]
        page = page[: page.index('id="studioPageWriter"')]
        self.assertIn("<h2>拆书匠</h2>", page)
        self.assertNotIn("创作场", page, "拆书匠页面不应再出现旧名")

    def test_writer_page_is_registered(self):
        self.assertIn('id="studioPageWriter"', self.index)
        self.assertIn('data-studio-page-view="writer"', self.index)
        self.assertIn('const STUDIO_PAGES = ["library", "labeler", "comfyui", "novel", "writer"];',
                      self.studio_js, "22-studio 要登记第五页")
        from naiba.config import STUDIO_PAGE_VIEWS
        self.assertIn("writer", STUDIO_PAGE_VIEWS, "后端 STUDIO_PAGE_VIEWS 含 writer")

    def test_library_task_filter_excludes_both_modules(self):
        self.assertIn('"/api/studio/tasks?exclude_modules=novel,writer"', self.studio_js,
                      "模块库任务过滤排除 novel 与 writer")
        self.assertNotIn("exclude_module=novel\"", self.studio_js, "旧单数过滤已替换")

    def test_task_origin_maps_by_module(self):
        origin = self.studio_js[self.studio_js.index("function taskOrigin("):]
        origin = origin[: origin.index("\n}")]
        self.assertIn('if (module === "novel") return "拆书匠";', origin,
                      "novel 任务来源显示「拆书匠」（历史记录不重写，展示层兼容）")
        self.assertIn('if (module === "writer") return "织书台";', origin,
                      "writer 任务来源显示「织书台」")

    def test_backend_origin_labels(self):
        novel_service = (ROOT / "naiba" / "novel" / "service.py").read_text(encoding="utf-8")
        self.assertIn('"origin_label": "拆书匠"', novel_service,
                      "拆书匠新任务的来源标签")
        pipeline = (ROOT / "naiba" / "writer" / "pipeline.py").read_text(encoding="utf-8")
        self.assertIn('"origin_label": "织书台"', pipeline, "织书台任务的来源标签")
        self.assertIn('"module": "writer"', pipeline, "任务 module=writer")

    def test_novel_visible_strings_renamed(self):
        self.assertIn('fail("读取拆书匠任务失败", error);', self.novel_js)
        self.assertNotIn('fail("读取创作场任务失败"', self.novel_js)


class WriterPageStructureTests(unittest.TestCase):
    """织书台页面结构：三阶段、三栏、弹窗与事件接线。"""

    def setUp(self):
        self.index = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        page = self.index[self.index.index('id="studioPageWriter"'):]
        self.page = page[: page.index('</section>', page.index('id="writerImportFile"'))]

    def test_page_has_no_breadcrumb_and_three_stages(self):
        head = self.page[: self.page.index('class="writer-stagebar"')]
        self.assertNotIn("breadcrumb", head, "页面头不放面包屑（与拆书匠同一拍板）")
        for stage in ("setup", "outline", "draft"):
            self.assertIn(f'data-writer-stage="{stage}"', self.page, f"阶段按钮 {stage}")
        for panel in ("writerSetupPanel", "writerOutlinePanel", "writerDraftPanel"):
            self.assertIn(f'id="{panel}"', self.page)

    def test_three_columns_and_dialogs(self):
        for ident in ("writerColumns", "writerChapterList", "writerPlanBody", "writerPipelineBlock",
                      "writerBooksDialog", "writerTemplateDialog", "writerImportDialog",
                      "writerManageDialog", "writerImportFile", "writerExportMenu"):
            self.assertIn(f'id="{ident}"', self.page, ident)

    def test_no_inline_handlers(self):
        self.assertNotIn("onclick=", self.page, "不写内联 onclick（事件在 27-writer.js 接线）")
        self.assertNotIn("onchange=", self.page)

    def test_writer_js_binds_from_composition_root(self):
        bind = (ROOT / "public" / "js" / "15-bind-events.js").read_text(encoding="utf-8")
        self.assertIn('import { bindWriterEvents } from "./27-writer.js";', bind)
        self.assertIn("bindWriterEvents();", bind)
        js = (ROOT / "public" / "js" / "27-writer.js").read_text(encoding="utf-8")
        self.assertIn("export function bindWriterEvents()", js)
        self.assertIn('from "./01-core.js"', js)
        self.assertIn('from "./22-studio.js"', js)
        self.assertNotIn('from "./25-novel.js"', js, "27 不 import 25（无依赖链）")

    def test_writer_js_only_polls_server_pipeline(self):
        """前端不持有「下一步」状态：只读流水线与任务事件（实施计划 §4.2）。"""
        js = (ROOT / "public" / "js" / "27-writer.js").read_text(encoding="utf-8")
        self.assertIn("/pipeline`", js.replace('`/api/writer/books/${encodeURIComponent(bookId)}/pipeline`', "/pipeline`"),
                      "轮询读服务端流水线")
        self.assertIn("setTimeout(() => void pollPipelineTick()", js)

    def test_css_has_writer_section_and_mobile_panes(self):
        css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
        for piece in (".writer-columns {", "248px minmax(0, 1fr) 280px",
                      ".writer-stage.active {", "#writerColumns[data-writer-pane=\"book\"] .writer-book"):
            self.assertIn(piece, css, piece)
        self.assertIn(".writer-page-actions {\n  position: relative", css,
                      "导出菜单以页面头操作区为定位基准")
        # 窄屏页头与拆书匠同一套：只留 4 个按钮、阶段说明收起。
        # 缺任一条，5 个按钮（382）+ 说明（176）会把整页最小宽度顶到 406px，
        # 360 宽手机上右边被裁 46px（2026-10-30 实测）。
        for piece in ("#writerNewBookBtn { display: none; }",
                      ".writer-stagebar-note { display: none; }"):
            self.assertIn(piece, css, piece)
        self.assertIn(".writer-book-summary[hidden], .writer-candidates[hidden], .writer-block[hidden]", css,
                      "自带 display: grid 的容器必须补 [hidden] 守卫，否则空卡/假进度块仍占位")


class WriterTemplateAssetTests(unittest.TestCase):
    """模板资源：随 public 打包，结构与启元运行时一致。"""

    def test_templates_json_shape(self):
        data = json.loads((ROOT / "public" / "studio" / "writer" / "templates.json").read_text(encoding="utf-8"))
        self.assertEqual(data.get("version"), 2)
        self.assertGreaterEqual(len(data.get("templates") or []), 50)
        self.assertGreaterEqual(len(data.get("categories") or []), 20)
        keys = {str(ch.get("key")) for ch in data.get("channels") or []}
        self.assertIn("male", keys)
        self.assertIn("female", keys)
        for row in data["templates"]:
            self.assertIn("id", row)
            self.assertIn("prompt", row, "模板必须带创意提示词")


class WriterBackendRoutesTests(unittest.TestCase):
    """织书台路由接线：两段式契约 + 前端依赖的两个补充口。"""

    def setUp(self):
        self.http = (ROOT / "naiba" / "http.py").read_text(encoding="utf-8")

    def test_core_routes_wired(self):
        for piece in ('"/api/writer/books"', '"/api/writer/import"', "/commit\"",
                      '"/pipelines"', '"/pipeline/cancel"', '"/pipeline/retry"',
                      '"/versions/select"', '"/sync"', '"/api/writer/location"',
                      '"/api/writer/reveal"'):
            self.assertIn(piece, self.http, piece)

    def test_import_is_multipart_gated(self):
        self.assertIn("_writer_import_upload", self.http)
        self.assertIn("MAX_IMPORT_BYTES", self.http, "导入上限与 writer 域同源")

    def test_reveal_is_local_only(self):
        start = self.http.index('elif path == "/api/writer/reveal"')
        reveal = self.http[start + len('elif path == "/api/writer/reveal"'):]
        reveal = reveal[: reveal.index("elif path ==")]
        self.assertIn("_is_local_request", reveal, "打开文件夹只允许本机触发")

    def test_patch_book_supports_manual_outline(self):
        service = (ROOT / "naiba" / "writer" / "service.py").read_text(encoding="utf-8")
        patch = service[service.index("def api_patch_book"):]
        patch = patch[: patch.index("def api_delete_book")]
        self.assertIn('"outline" in body', patch, "人工大纲编辑走 PATCH book")
        self.assertIn('kind="manual"', patch)


if __name__ == "__main__":
    unittest.main()
