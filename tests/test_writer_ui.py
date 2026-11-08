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
import shutil
import subprocess
import tempfile
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

    def test_setup_and_outline_renders_yield_to_unsaved_edits(self):
        """切阶段/切页丢内容的根因是渲染函数无条件重填：守卫必须写在渲染入口。"""
        js = (ROOT / "public" / "js" / "27-writer.js").read_text(encoding="utf-8")
        self.assertIn('if (writer.stage === "setup" && !writer.setupDirty) fillSetupForm();', js)
        self.assertIn("partsDirty", js, "分部大纲也要有 dirty 标记")
        self.assertIn("applyWriterDraft", js, "刷新/重启后能用按书存的草稿恢复")
        self.assertIn("keepEdits: unsaved", js, "进页时把没保存的修改带上")

    def test_four_stages_and_side_by_stage(self):
        """批次二：阶段从三个变四个（设定/大纲/章节/正文），左栏按阶段切换。"""
        js = (ROOT / "public" / "js" / "27-writer.js").read_text(encoding="utf-8")
        self.assertIn('["setup", "outline", "chapter", "draft"]', js, "四个阶段的白名单")
        self.assertIn("function renderSide()", js)
        for side in ("setup", "outline", "chapter", "draft"):
            self.assertIn(f'data-writer-side="{side}"', self.page, f"左栏 {side}")
        for panel in ("writerSetupPanel", "writerOutlinePanel", "writerChapterPanel", "writerDraftPanel"):
            self.assertIn(f'id="{panel}"', self.page)
        self.assertIn('data-writer-stage="chapter"', self.page)

    def test_outline_is_text_not_chapters(self):
        """大纲是分部文字；章节由拆章产出（页面不再有「大纲章节数」滑条）。"""
        js = (ROOT / "public" / "js" / "27-writer.js").read_text(encoding="utf-8")
        self.assertIn("function renderParts()", js)
        self.assertIn("outline_text", js, "整篇大纲走 outline_text 流水线")
        self.assertIn('submitPipeline("split"', js, "章节页提交拆章")
        self.assertIn('id="writerOutlineText"', self.page)
        self.assertNotIn('id="writerOutlineChapterCount"', self.page, "大纲章节数已删除")

    def test_export_goes_through_backend_and_native_save(self):
        """exe 里 <a download> 会被 pywebview 静默取消（ALLOW_DOWNLOADS 默认 False）：
        导出只能「后端落盘 → saveLocalFile 交给原生另存为」。"""
        js = (ROOT / "public" / "js" / "27-writer.js").read_text(encoding="utf-8")
        self.assertIn("/export-file", js)
        self.assertIn("saveLocalFile(", js)
        self.assertNotIn("URL.createObjectURL(", js, "不再造 blob 链接（桌面端点了等于没点）")

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
                      '"/api/writer/reveal"', '"/export-file"'):
            self.assertIn(piece, self.http, piece)

    def test_export_file_route_is_post_and_before_catch_all(self):
        """导出落盘有副作用 → 只能挂在 do_POST 里；GET 的 /export 仍在（老客户端与单测在用）。"""
        post_start = self.http.index("def do_POST(self)")
        put_start = self.http.index("def do_PUT(self)") if "def do_PUT(self)" in self.http else len(self.http)
        dispatch = self.http[post_start:put_start]
        self.assertIn("api_export_file", dispatch, "导出落盘走 POST 分支")
        self.assertIn('path.endswith("/export-file")', dispatch)
        get_part = self.http[:post_start]
        self.assertIn("api_export_book", get_part, "GET 纯文本导出保留")

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


class WriterRound3UiTests(unittest.TestCase):
    """第三次反馈（2026-10-31）的前端契约：复核梳理按钮、拆分字数联动、
    「拆哪一部」独立选中、增加一部表单同步。"""

    def setUp(self):
        self.index = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        start = self.index.index('id="studioPageWriter"')
        self.page = self.index[start: self.index.index('</section>', self.index.index('id="writerImportFile"'))]
        self.js = (ROOT / "public" / "js" / "27-writer.js").read_text(encoding="utf-8")

    def test_review_button_and_kind_wired(self):
        self.assertIn('id="writerOutlineReview"', self.page, "多部统一复核梳理按钮")
        self.assertIn('$("#writerOutlineReview")?.addEventListener("click", () => void generateOutlineReview())', self.js)
        self.assertIn('submitPipeline("outline_review"', self.js)
        self.assertIn("outline_review: \"多部统一复核梳理\"", self.js, "KIND_LABELS 有中文标签")
        self.assertIn("outline_review: \"复核梳理\"", self.js, "STEP_LABELS 有中文标签")

    def test_split_part_dropdown_is_independent(self):
        """「拆哪一部」不再镜像大纲页当前部：有独立状态 + change 监听（第三次反馈⑥）。"""
        self.assertIn("splitPartNo: 0", self.js, "状态里有拆分面板自己的部号")
        self.assertNotIn("select.value = String(writer.partNo || 1);", self.js,
                         "renderParts 不得再把拆哪一部拽回大纲页当前部")
        self.assertIn('$("#writerSplitPart")?.addEventListener("change"', self.js)
        self.assertIn("if (!Number(writer.splitPartNo || 0)) writer.splitPartNo = Number(writer.partNo || 1);",
                      self.js, "只在没选过时借一次当前部做初值")
        self.assertIn("if (!sameBook) writer.splitPartNo = 0;", self.js, "换书重置")
        self.assertIn("拆「第 ${partNo} 部」", self.js, "提交后的保存状态要写明拆的是哪一部")

    def test_part_mutations_sync_form(self):
        """加部/删部/按部数重排后表单必须跟着换部，防旧内容盖到新部头上（第三次反馈③）。"""
        self.assertIn("function syncPartFormToCurrent()", self.js)
        for fn in ("addPart", "removePart", "applyPartsTarget"):
            block = self.js[self.js.index(f"function {fn}("):]
            block = block[:block.index("\n}", 1) + 2]
            self.assertIn("syncPartFormToCurrent()", block, f"{fn} 之后要同步表单")

    def test_split_chars_formula_linked(self):
        self.assertIn('id="writerSplitTotalChars"', self.page, "本部目标总字数输入框")
        for option in ('<option value="8000">8000 字</option>', '<option value="9000">9000 字</option>'):
            self.assertIn(option, self.page, "单章字数新档（阅读平台常见 4000–9000）")
        self.assertIn("function syncSplitTotalChars()", self.js)
        self.assertIn("function applySplitTotalChars()", self.js)
        self.assertIn('$("#writerSplitCount")?.addEventListener("input", syncSplitTotalChars)', self.js)
        self.assertIn("SPLIT_STEP_CHAPTERS = 20", self.js, "与服务端分批常量对齐（提示文案用）")

    def test_split_dialog_is_actually_shown(self):
        """「接续/覆盖」弹窗必须真的弹出来：startSplit 只挂监听不 showModal 时，
        <dialog> 没有 open 属性不可见，下面的 Promise 等按钮/close 永远挂着，
        表现就是「开始拆分」点了毫无反应（2026-11-07 用户报障根因，自 9522d44 起天生如此）。"""
        block = self.js[self.js.index("async function startSplit("):]
        block = block[: block.index("\n}", 1) + 2]
        self.assertIn("if (!dialog.open) dialog.showModal();", block,
                      "弹窗分支里必须显式 showModal，漏了就是点了没反应")
        self.assertLess(block.index("dialog.showModal()"), block.index("await new Promise"),
                        "先 showModal 再开始等接续/覆盖选择，顺序反了同样永远等不到")

    def test_generate_skips_empty_catalog_presave(self):
        """一次性生成全部大纲不再被「大纲还是空的」挡住：纯目录不先存（第三次反馈①）。"""
        self.assertIn("async function ensurePartsSavedBeforeGenerate()", self.js)
        self.assertIn("if (!await ensurePartsSavedBeforeGenerate()) return;", self.js)
        self.assertIn("部目录已保存", self.js, "空目录也允许人工保存（只存部数与标题）")


class WriterContinuityUiTests(unittest.TestCase):
    """跨章连贯性（修复计划 §三）：只读检查的前端呈现 + 状态重建入口的接线。"""

    def setUp(self):
        self.index = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        self.js = (ROOT / "public" / "js" / "27-writer.js").read_text(encoding="utf-8")
        start = self.index.index('id="studioPageWriter"')
        self.page = self.index[start: self.index.index('</section>', self.index.index('id="writerImportFile"'))]

    def test_continuity_panel_and_button_wired(self):
        self.assertIn('id="writerContinuityRun"', self.page, "连贯性检查按钮")
        self.assertIn('id="writerContinuityReport"', self.page, "报告容器")
        self.assertIn('$("#writerContinuityRun")?.addEventListener("click", () => void runContinuityCheck())', self.js)
        self.assertIn("function renderContinuity()", self.js)
        self.assertIn("async function runContinuityCheck()", self.js)
        # 只读口径要写进界面文案：检查不改正文。
        self.assertIn("不会改动正文", self.page)
        # 过期展示：报告 stale 时提示重跑。
        self.assertIn("请重新运行", self.js)

    def test_state_rebuild_entry_wired(self):
        self.assertIn('id="writerDraftRebuildState"', self.page, "章末状态重建按钮")
        self.assertIn('submitPipeline("state_rebuild"', self.js)
        self.assertIn("state_rebuild: \"重建章末状态\"", self.js, "KIND_LABELS 有中文标签")
        self.assertIn("state: \"章末状态\"", self.js, "STEP_LABELS 有中文标签")

    def test_continuity_api_paths(self):
        """前端调用的两个只读接口路径与服务端路由一致。"""
        self.assertIn("/continuity/run", self.js)
        block = self.js[self.js.index("async function loadContinuityReport()"):]
        block = block[:block.index("\n}", 1) + 2]
        self.assertIn("/continuity`", block, "读取报告走 GET /continuity")


class WriterDraftSubmissionTests(unittest.TestCase):
    """执行真实前端函数（Node VM），另用真实服务检查续写读取刚保存的尾段。"""

    def test_offline_javascript_submission_order_and_guards(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required to execute the real writer JS")
        result = subprocess.run(
            [node, str(ROOT / "verify" / "writer_draft_offline_test.cjs")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("27 cases passed", result.stdout)
        self.assertIn("writer export offline: 8 cases passed", result.stdout)

    def test_continue_preview_reads_newly_saved_tail(self):
        from naiba.writer import prompts
        from naiba.writer.service import WriterService
        from naiba.writer.pipeline import WriterPipelineManager
        from naiba.writer.store import WriterStore

        with tempfile.TemporaryDirectory() as directory:
            service = object.__new__(WriterService)
            service.store = WriterStore(Path(directory))
            service.pipeline = WriterPipelineManager(None, service.store)
            service._context_window = lambda _key: 131072
            bid = service.store.create_book(title="正文顺序回归", setup={"idea": "收到来信"})["id"]
            service.store.save_outline(bid, sync=True, chapters=[{"no": 1, "title": "来信", "beat": "辨认来客"}])
            service.store.save_chapter_version(bid, 1, content="旧稿唯一标记")
            chapter = service.store.get_chapter(bid, 1)
            content = "手动新稿前缀不应进入尾段" + "雨" * (prompts.CONTINUE_TAIL_CHARS + 100) + "手动新稿尾段唯一标记"
            saved, code = service.api_save_chapter(bid, 1, {"content": content, "revision": chapter["revision"]})
            self.assertEqual(code, 201)
            self.assertGreater(saved["chapter_revision"], chapter["revision"])
            preview, code = service.api_preview({"book_id": bid, "kind": "chapter_continue", "chapter_no": 1})
            self.assertEqual(code, 200)
            self.assertEqual(preview["kind"], "chapter_continue")
            self.assertIn(content[-prompts.CONTINUE_TAIL_CHARS:], preview["content"])
            self.assertNotIn("旧稿唯一标记", preview["content"])
            self.assertNotIn("手动新稿前缀不应进入尾段", preview["content"])

    def test_fixture_is_offline_and_uses_real_markup_module(self):
        fixture = (ROOT / "verify" / "_serve_writer_draft_ui.py").read_text(encoding="utf-8")
        self.assertIn("ThreadingHTTPServer(('127.0.0.1'", fixture)
        self.assertIn("TemporaryDirectory(", fixture)
        self.assertIn("f.service.api_preview(body)", fixture)
        self.assertIn("f.service.api_save_chapter(bid, no, body)", fixture)
        self.assertNotIn("import playwright", fixture)
        self.assertNotIn("start_pipeline(", fixture)


if __name__ == "__main__":
    unittest.main()
