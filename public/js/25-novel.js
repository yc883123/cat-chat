// ============================================================
// 25-novel.js —— 工作台「创作场」功能页（小说改编）
// 导入 TXT → 自动拆章（零 AI）→ 勾选章节 → 保存为作品 → 原文/改编稿管理 → 生成要素/大纲/剧本 → 导出。
//
// 边界（《小说改编工作台总计划》§5.1、§5.6）：
// - 两段式：列表与详情只拿索引；单章正文与单份稿件正文都按需请求。
// - 阶段一是文件处理，不创建 Studio 任务；要素/大纲/剧本与单章 AI 改编走 Studio 队列。
// - 与聊天域零耦合：只调 /api/novel/*。
// - 只从 01-core / 22-studio 取公共能力，22 不反向 import 本文件，无循环依赖。
//
// 原文与改编稿（《创作场原文改编与记录管理整改计划》§3）：
// - 保存编辑结果时**新增一份改编稿**，原文与其它稿件逐字保留。
// - 四类点击状态彼此独立：选中用于生成 / 批量管理勾选 / 展开阅读 / 进入编辑。
// - 卡片展开状态按稿件 ID 记录，轮询或切阶段返回都不会把它强行撑开。
// ============================================================

import { $, api, applyModelSkinFor, state, toast } from "./01-core.js";
import { onStudioPageChange, setStudioPage } from "./22-studio.js";

// 与服务端一致的上限（前端只做提前提示，服务端仍逐项校验）。
const MAX_FILE_BYTES = 60 * 1024 * 1024;

const LEVEL_LABELS = {
  heading: "按章节标题识别",
  sep: "按符号分节（没有识别到章节标题）",
  single: "只识别到 1 个标题，仍按它切分",
  len: "没有识别到任何标题，按字数等分",
};

// 稿件类型的中文名（服务端也下发 kind_label，这里只用于前端兜底）。
const MANUSCRIPT_KIND_LABELS = {
  original: "原文",
  legacy: "升级前保存的正文",
  manual: "人工改编稿",
  ai: "AI 改编稿",
  broken: "稿件损坏",
};

const novel = {
  initialized: false,
  // 作品态：bookId 为空表示还没打开作品。
  bookId: "",
  book: null,
  chapters: [],
  broken: [],
  // 数据位置（保存目录 / 可复制路径 / 能否打开文件夹），来自 /api/novel/location。
  location: null,
  // 当前打开的章节：索引 + 稿件清单；正文字段只放基础稿件，其它稿件按需取。
  chapterNo: 0,
  chapter: null,
  // 稿件正文缓存：`<稿件ID>` → 正文（点开卡片或编辑时才拉）。
  manuscriptBodies: new Map(),
  // 各章稿件清单缓存：`<章号>` → 稿件清单（逐章选稿下拉要看到全部稿件）。
  chapterRosters: new Map(),
  chapterRostersInflight: new Set(),
  // 展开状态按稿件 ID 记录；收起的卡片不会被轮询重新撑开。
  expanded: new Set(),
  // 用户亲手收过展开状态的章节号：这些章不再自动展开基础稿件。
  expandedTouched: new Set(),
  // 编辑态：`0` 未编辑；否则是「基于哪份稿件」的稿件 ID。
  editingManuscriptId: "",
  editDraft: "",
  aiTargetId: "",
  // 导入态：暂存里等勾选的章节。
  staged: null,
  picked: new Set(),
  filter: "",
  pane: "editor",
  busy: false,
  // 阶段（拆章 / 要素 / 大纲 / 剧本）。
  stage: "split",
  // 结果区折叠态：键是 `<阶段>:task|saved`。轮询只重画内容，不能把收起的区块撑开。
  collapsed: new Set(),
  // 生成态：任务只取创作场分区（module=novel），与模块库互不串台。
  tasks: [],
  // 最近任务只显示 3 条紧凑行（提词库同口径）；完整历史在「管理记录」。
  recentTaskLimit: 3,
  taskPollTimer: null,
  taskPollBusy: false,
  // 每个阶段各自保存当前任务和流式结果。
  stageStates: {
    elements: { activeTaskId: "", taskCursor: 0, streamText: "", warnings: [], busy: false, submittedAt: 0, firstDeltaAt: 0, recentDeltas: [] },
    outline: { activeTaskId: "", taskCursor: 0, streamText: "", warnings: [], busy: false, submittedAt: 0, firstDeltaAt: 0, recentDeltas: [] },
    script: { activeTaskId: "", taskCursor: 0, streamText: "", warnings: [], busy: false, submittedAt: 0, firstDeltaAt: 0, recentDeltas: [] },
    // 分镜生图（阶段四）：多存逐场解析结果与勾选集（解析一次，轮询不重算）。
    storyboard: { activeTaskId: "", taskCursor: 0, streamText: "", warnings: [], busy: false, submittedAt: 0, firstDeltaAt: 0, recentDeltas: [], progressNote: "", rows: [], parsed: false, checked: new Set() },
  },
  // 单章 AI 改编写在拆章阶段，但有自己的一条任务槽。
  adaptTask: { activeTaskId: "", taskCursor: 0, streamText: "", warnings: [], busy: false, submittedAt: 0, progressNote: "" },
  // 模型选择（与提词库同口径）：先选供应商，再按供应商拉完整模型目录。
  stageProviderKey: "",
  stageModelName: "",
  stageModelsCache: new Map(),
  stageModelInflight: new Map(),
  aiProviderKey: "",
  aiModelName: "",
  // 生成参数（用户可调，按用户保存）。
  bodyChars: 3000,
  maxChapters: 6,
  limits: { min: 3000, max: 15000, step: 500, chaptersMin: 1, chaptersMax: 20, chaptersDefault: 6 },
  // 逐章稿件选择：`<章号>` → 稿件 ID；空值表示该章用基础稿件（原文）。
  manuscriptChoice: new Map(),
  // 剧本阶段：当前正在编辑的场次号（0 = 未在编辑）。
  sceneEditing: 0,
  sceneBusy: false,
  // 迟到响应保护：每次切换作品/章节都换令牌，旧响应回来直接丢掉。
  bookToken: 0,
  chapterToken: 0,
  booksToken: 0,
  taskToken: 0,
  selectedVersions: { elements: 0, outline: 0, script: 0 },
  // 预览数据（服务端预检结果），右栏说明直接用它，不在前端重算。
  budget: null,
  budgetToken: 0,
  // 管理记录面板状态。
  manage: {
    open: false,
    tab: "manuscript",
    filter: "",
    records: null,
    selected: new Set(),
    loading: false,
    token: 0,
  },
};

const STAGE_TASKS_URL = "/api/studio/tasks?module=novel";
const LAST_BOOK_KEY = "naibaNovelLastBookId";
const LIMITS_KEY = "naibaNovelLimits";
const TASK_STATUS_LABELS = {
  queued: "排队中",
  running: "生成中",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};
const TERMINAL_TASK_STATUSES = new Set(["done", "failed", "cancelled", "interrupted"]);
const STAGE_COPY = {
  elements: {
    title: "要素",
    hint: "从选定章节稿件整理人物、场景和道具",
    savedLabel: "作品里已保存的要素",
    empty: "还没有生成要素。勾选章节后点「生成」。",
  },
  outline: {
    title: "改编大纲",
    hint: "把选定章节稿件整理成三幕结构",
    savedLabel: "作品里已保存的大纲",
    empty: "还没有生成大纲。勾选章节后点「生成」。",
  },
  script: {
    title: "剧本",
    hint: "把选定章节稿件改编成可编辑的剧本场次",
    savedLabel: "已保存的场次",
    empty: "还没有生成剧本。勾选章节后点「生成」。",
  },
  storyboard: {
    title: "分镜生图",
    hint: "把已保存的剧本场次逐场转成出图提示词，发给造梦机出图",
    savedLabel: "",
    empty: "还没有生成分镜提示词。点「生成分镜提示词」。",
  },
};

// 各阶段的右栏说明：随阶段、选章、选稿与保存状态变化（计划 §7.2）。
const STAGE_PLAN_COPY = {
  split: "原文只读。手动编辑与 AI 润色都会新增一份改编稿，原文逐字保留；原文与改编稿都可以分别作为后续素材。",
  elements: "根据本次选择的章节稿件整理人物、场景和道具，不要求事先有大纲。",
  outline: "可以直接根据章节稿件生成三幕大纲，不强制先生成要素。",
  script: "没有保存要素或大纲也能直接生成剧本；有保存内容时按这里明确选择的版本加入人物设定和改编大纲。",
  storyboard: "把剧本里已保存的最新场次逐场转成出图提示词，每场一条；生成后勾选需要的场次，发送到造梦机逐条出图。分镜提示词不写回作品。",
};

// ============================================================
// 小工具
// ============================================================

function fmtChars(count) {
  const value = Number(count) || 0;
  if (value >= 10000) return `${(value / 10000).toFixed(1)} 万`;
  return String(value);
}

function pad2(value) {
  return String(value).padStart(2, "0");
}

function fmtTime(ms) {
  const value = Number(ms) || 0;
  if (!value) return "时间未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未记录";
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

function setSaveState(text, note = "") {
  const stateEl = $("#novelSaveState");
  if (stateEl) stateEl.textContent = text;
  const noteEl = $("#novelSaveNote");
  if (noteEl && note) noteEl.textContent = note;
}

function closeExportMenu() {
  const menu = $("#novelExportMenu");
  if (menu) menu.hidden = true;
  $("#novelExportBtn")?.setAttribute("aria-expanded", "false");
}

function setBusy(busy) {
  novel.busy = busy;
  const btn = $("#novelSaveBook");
  if (btn) btn.disabled = busy || !novel.staged;
}

/** 统一错误提示：错误来自服务端的用户可读中文，直接展示，不吞不猜。 */
function fail(prefix, error) {
  const message = String(error?.message || error || "未知错误");
  toast(`${prefix}：${message}`);
  return message;
}

function stageState(stage = novel.stage) {
  return novel.stageStates[stage] || novel.stageStates.elements;
}

/** 阶段中文名（拆章与单章改编不在 STAGE_COPY 里，取值必须兜底）。 */
function stageLabel(stage = novel.stage) {
  if (stage === "split") return "拆章";
  return String(STAGE_COPY[stage]?.title || "生成");
}

function activeTaskSlot() {
  return novel.stage === "split" ? novel.adaptTask : stageState();
}

const STAGE_PANEL_CONTROL_IDS = [
  "novelStageMeta", "novelStageNote", "novelStageProvider", "novelStageModel",
  "novelStageGenerate", "novelStageWarning", "novelStageTask", "novelStageTaskState",
  "novelStageCancel", "novelStageRetry", "novelStageApply", "novelStageTaskNote",
  "novelStageResult", "novelStageSaved", "novelStageSavedAt", "novelStageSavedBody", "novelStageVersion",
  "novelTaskHistory", "novelPickManuscripts", "novelBodyChars", "novelBodyCharsValue",
  "novelMaxChapters", "novelMaxChaptersValue",
  "novelSceneBlock", "novelSceneCount", "novelSceneNote", "novelSceneList", "novelSceneEdit",
  "novelSceneHead", "novelSceneBody", "novelSceneSave", "novelSceneCancel", "novelScriptVersion",
  "novelStoryboardNote", "novelStoryboardList", "novelStoryboardAll", "novelStoryboardSend",
];

function activateStagePanel(stage) {
  const host = document.querySelector("#novelEditorBody");
  if (!host) return;
  const panels = [...host.querySelectorAll("[data-stage-panel]")];
  const active = panels.find((panel) => panel.dataset.stagePanel === stage) || panels[0];
  for (const panel of panels) {
    panel.hidden = panel !== active;
    panel.removeAttribute("id");
    for (const id of STAGE_PANEL_CONTROL_IDS) {
      panel.querySelectorAll(`[data-novel-control="${id}"]`).forEach((el) => el.removeAttribute("id"));
    }
  }
  if (!active) return;
  active.id = "novelStagePanel";
  for (const id of STAGE_PANEL_CONTROL_IDS) {
    const el = active.querySelector(`[data-novel-control="${id}"]`);
    if (el) el.id = id;
  }
}

function prepareStagePanels() {
  const first = $("#novelStagePanel");
  if (!first || first.dataset.stagePanelsReady) return;
  const host = first.parentElement;
  const ids = ["novelStagePanel", ...STAGE_PANEL_CONTROL_IDS];
  const markControls = (panel) => {
    for (const id of ids) {
      const el = id === "novelStagePanel" ? panel : panel.querySelector(`#${id}`);
      if (el) el.dataset.novelControl = id;
    }
  };
  first.dataset.stagePanel = "elements";
  markControls(first);
  for (const stage of ["outline", "script", "storyboard"]) {
    const clone = first.cloneNode(true);
    clone.removeAttribute("id");
    clone.dataset.stagePanel = stage;
    markControls(clone);
    clone.querySelectorAll("[id]").forEach((el) => el.removeAttribute("id"));
    host.append(clone);
  }
  first.dataset.stagePanelsReady = "true";
}

// ============================================================
// 渲染：章节列表
// ============================================================

function visibleChapters() {
  const keyword = novel.filter.trim().toLowerCase();
  if (!keyword) return novel.chapters;
  return novel.chapters.filter((row) => String(row.title || "").toLowerCase().includes(keyword));
}

function renderChapters() {
  const list = $("#novelChapterList");
  if (!list) return;
  const empty = $("#novelChapterEmpty");
  // 导入暂存态：列表显示待勾选的章节（复选框可点）；作品态：显示已保存章节。
  if (novel.staged) {
    const rows = novel.staged.chapters || [];
    const keyword = novel.filter.trim().toLowerCase();
    const shown = keyword
      ? rows.filter((row) => String(row.title || "").toLowerCase().includes(keyword))
      : rows;
    list.replaceChildren(...shown.map(stagedRow));
    if (empty) {
      empty.hidden = shown.length > 0;
      empty.textContent = rows.length
        ? "没有匹配的章节。换个关键词试试。"
        : "这本书里没有拆出章节。";
    }
    updatePickedCount();
    return;
  }
  const rows = visibleChapters();
  list.replaceChildren(...rows.map(chapterRow));
  if (empty) {
    empty.hidden = rows.length > 0;
    empty.textContent = novel.chapters.length
      ? "没有匹配的章节。换个关键词试试。"
      : "还没有章节。点上面的「导入 TXT」选一本小说。";
  }
  updatePickedCount();
}

function stagedRow(row) {
  const label = document.createElement("label");
  label.className = "novel-chapter-row";
  const checked = novel.picked.has(row.i);
  label.classList.toggle("picked", checked);
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = checked;
  box.dataset.novelPick = String(row.i);
  box.setAttribute("aria-label", `选择 ${row.title}`);
  const copy = document.createElement("span");
  copy.className = "novel-chapter-copy";
  const title = document.createElement("strong");
  title.textContent = row.title;
  const meta = document.createElement("small");
  meta.textContent = `${fmtChars(row.len)} 字`;
  copy.append(title, meta);
  const no = document.createElement("span");
  no.className = "novel-chapter-no";
  no.textContent = String(row.i).padStart(2, "0");
  label.append(box, copy, no);
  return label;
}

function chapterRow(row) {
  // 拆章阶段：点一行 = 打开这一章（看原文与改编稿）。
  // 要素/大纲/剧本阶段：同一行要能勾选当素材，所以换成带复选框的 label。
  if (novel.stage !== "split") {
    const label = stagedRow(row);
    label.dataset.novelChapter = String(row.i);
    label.classList.toggle("active", row.i === novel.chapterNo);
    const adapted = Number(row.adapted_count) || 0;
    if (adapted > 0) {
      const tail = label.querySelector(".novel-chapter-copy small");
      if (tail) tail.textContent = `${fmtChars(row.len)} 字 · 另有 ${adapted} 份改编稿`;
    }
    return label;
  }
  const button = document.createElement("button");
  button.type = "button";
  button.className = "novel-chapter-row";
  button.dataset.novelChapter = String(row.i);
  button.classList.toggle("active", row.i === novel.chapterNo);
  const copy = document.createElement("span");
  copy.className = "novel-chapter-copy";
  const title = document.createElement("strong");
  title.textContent = row.title;
  const meta = document.createElement("small");
  const bits = [`${fmtChars(row.len)} 字`];
  const count = Number(row.manuscript_count) || 1;
  if (count > 1) bits.push(`${count} 份稿件`);
  if (row.base_kind === "legacy") bits.push("原文缺失");
  meta.textContent = bits.join(" · ");
  copy.append(title, meta);
  const no = document.createElement("span");
  no.className = "novel-chapter-no";
  no.textContent = String(row.i).padStart(2, "0");
  button.append(copy, no);
  return button;
}

function updatePickedCount() {
  const el = $("#novelSelectedCount");
  if (!el) return;
  el.textContent = novel.staged
    ? `已选 ${novel.picked.size} / ${novel.staged.chapters.length} 章`
    : `共 ${novel.chapters.length} 章`;
}

function renderBookSummary() {
  const book = novel.book;
  const staged = novel.staged;
  const titleEl = $("#novelBookTitle");
  const metaEl = $("#novelBookMeta");
  const cover = $("#novelBookCover");
  if (staged) {
    // 书名去掉扩展名：文件叫「长夜灯.txt」，作品名应该是「长夜灯」。
    const bookName = String(staged.name || "").replace(/\.[^.]+$/, "") || staged.name;
    if (titleEl) titleEl.textContent = `《${bookName}》`;
    if (metaEl) {
      metaEl.textContent =
        `${staged.total} 章 · 编码 ${staged.encoding} · ${LEVEL_LABELS[staged.split_level] || staged.split_level}`;
    }
    if (cover) cover.textContent = "待";
    const stats = [$("#novelStatChapters"), $("#novelStatChars"), $("#novelStatState")];
    if (stats[0]) stats[0].textContent = String(staged.total);
    if (stats[1]) stats[1].textContent = fmtChars(staged.total_len);
    if (stats[2]) stats[2].textContent = "待保存";
    return;
  }
  if (book) {
    if (titleEl) titleEl.textContent = `《${book.title}》`;
    if (metaEl) {
      const source = book.source || {};
      const parts = [source.encoding ? `编码 ${source.encoding}` : ""];
      if (source.split_level) parts.push(LEVEL_LABELS[source.split_level] || source.split_level);
      metaEl.textContent = parts.filter(Boolean).join(" · ") || "已保存作品";
    }
    if (cover) cover.textContent = "创";
    const stats = [$("#novelStatChapters"), $("#novelStatChars"), $("#novelStatState")];
    if (stats[0]) stats[0].textContent = String(book.chapter_count);
    if (stats[1]) stats[1].textContent = fmtChars(book.source?.total_len || 0);
    if (stats[2]) stats[2].textContent = novel.broken.length ? "数据异常" : "已保存";
    return;
  }
  if (titleEl) titleEl.textContent = "还没有打开作品";
  if (metaEl) metaEl.textContent = "导入一本 TXT 即可开始";
  if (cover) cover.textContent = "创";
  const stats = [$("#novelStatChapters"), $("#novelStatChars"), $("#novelStatState")];
  if (stats[0]) stats[0].textContent = "0";
  if (stats[1]) stats[1].textContent = "0";
  if (stats[2]) stats[2].textContent = "未导入";
}

function renderImportBlock() {
  const block = $("#novelImportBlock");
  if (!block) return;
  block.hidden = !novel.staged;
  const save = $("#novelSaveBook");
  if (save) save.disabled = !novel.staged || novel.busy;
  if (!novel.staged) return;
  const staged = novel.staged;
  const stateEl = $("#novelImportState");
  if (stateEl) stateEl.textContent = "待保存";
  const note = $("#novelImportNote");
  if (note) {
    note.textContent =
      `共 ${staged.total} 章、约 ${fmtChars(staged.total_len)} 字。` +
      `拆分层级：${LEVEL_LABELS[staged.split_level] || staged.split_level}。` +
      `编码 ${staged.encoding} 是实际选中的解码方式，不代表源文件就是这个编码。` +
      "勾选要保留的章节，再点「保存为作品」。";
  }
}

// ============================================================
// 渲染：原文与改编稿卡片
// ============================================================

function manuscriptRows() {
  return Array.isArray(novel.chapter?.manuscripts) ? novel.chapter.manuscripts : [];
}

function manuscriptLabel(row) {
  return String(row?.label || MANUSCRIPT_KIND_LABELS[row?.kind] || "稿件");
}

function manuscriptKindLabel(row) {
  return String(row?.kind_label || MANUSCRIPT_KIND_LABELS[row?.kind] || "");
}

/** 稿件正文：优先用缓存，基础稿件的正文随单章接口一起回来。 */
function manuscriptBody(row) {
  if (!row) return "";
  const cached = novel.manuscriptBodies.get(row.id);
  if (typeof cached === "string") return cached;
  if (row.id === novel.chapter?.base_manuscript_id) return String(novel.chapter?.content || "");
  return "";
}

async function ensureManuscriptBody(row) {
  if (!row || !novel.bookId) return "";
  const cached = novel.manuscriptBodies.get(row.id);
  if (typeof cached === "string") return cached;
  if (row.id === novel.chapter?.base_manuscript_id && novel.chapter?.content) return novel.chapter.content;
  try {
    const payload = await api(
      `/api/novel/books/${encodeURIComponent(novel.bookId)}/chapters/${row.chapter_no || novel.chapterNo}/manuscripts/${encodeURIComponent(row.id)}`
    );
    const body = String(payload?.content ?? "");
    novel.manuscriptBodies.set(row.id, body);
    return body;
  } catch (error) {
    fail("读取稿件失败", error);
    return "";
  }
}

function isExpanded(manuscriptId) {
  return novel.expanded.has(String(manuscriptId));
}

function toggleExpanded(manuscriptId) {
  const id = String(manuscriptId);
  if (novel.expanded.has(id)) novel.expanded.delete(id);
  else novel.expanded.add(id);
  // 记住「这一章用户亲手收过」，之后重新渲染 / 轮询都不再自动撑开。
  if (novel.chapterNo) novel.expandedTouched.add(novel.chapterNo);
}

function manuscriptCard(row) {
  const card = document.createElement("article");
  card.className = "novel-manuscript-card";
  card.dataset.novelManuscriptCard = row.id;
  card.classList.toggle("base", row.id === novel.chapter?.base_manuscript_id);
  card.classList.toggle("adapted", row.kind === "manual" || row.kind === "ai");
  card.classList.toggle("legacy", row.kind === "legacy");
  card.classList.toggle("broken", Boolean(row.broken));
  const open = isExpanded(row.id);

  const head = document.createElement("div");
  head.className = "novel-manuscript-head";
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "novel-manuscript-toggle";
  toggle.dataset.novelManuscriptToggle = row.id;
  toggle.setAttribute("aria-expanded", String(open));
  toggle.title = open ? "收起正文" : "展开正文";
  const caret = document.createElement("span");
  caret.className = "novel-manuscript-caret";
  caret.setAttribute("aria-hidden", "true");
  caret.textContent = open ? "▾" : "▸";
  const label = document.createElement("strong");
  label.textContent = manuscriptLabel(row);
  toggle.append(caret, label);

  const meta = document.createElement("small");
  meta.className = "novel-manuscript-meta";
  const bits = [manuscriptKindLabel(row), `${fmtChars(row.len)} 字`];
  if (row.kind === "manual" || row.kind === "ai") bits.push(fmtTime(row.created_at));
  if (row.source_manuscript_id) {
    const source = manuscriptRows().find((item) => item.id === row.source_manuscript_id);
    bits.push(source ? `来源：${manuscriptLabel(source)}` : "来源稿件已删除");
  }
  if (row.broken) bits.push("文件损坏，需要重新导入或删除");
  meta.textContent = bits.filter(Boolean).join(" · ");

  head.append(toggle, meta);
  card.append(head);

  if (row.broken) return card;

  const actions = document.createElement("div");
  actions.className = "novel-manuscript-actions";
  const mk = (text, action, title, extra = "") => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `control-button tiny${extra}`;
    btn.dataset.novelManuscriptAction = action;
    btn.dataset.novelManuscriptId = row.id;
    btn.title = title;
    btn.textContent = text;
    return btn;
  };
  actions.append(
    mk("编辑", "edit", "基于这份稿件编辑；保存时新增一份改编稿，原文与这份稿件都会保留"),
    mk("AI 润色", "ai", "按你的要求让 AI 改写这一份稿件，结果保存后新增一张卡片"),
  );
  const used = novel.manuscriptChoice.get(novel.chapterNo) === row.id;
  const isBase = row.id === novel.chapter?.base_manuscript_id;
  if (!used) actions.append(mk("用于后续生成", "use", "让后续阶段的这一章默认使用这份稿件"));
  const inUse = document.createElement("span");
  inUse.className = "novel-manuscript-inuse";
  inUse.textContent = used ? "本次生成使用中" : (isBase ? "默认素材" : "");
  if (inUse.textContent) actions.append(inUse);
  card.append(actions);

  if (novel.editingManuscriptId === row.id) {
    const editing = document.createElement("p");
    editing.className = "novel-note";
    editing.textContent = "正在编辑这份稿件；保存会新增一份改编稿。";
    card.append(editing);
  }
  if (open) {
    const body = document.createElement("pre");
    body.className = "novel-manuscript-body";
    body.textContent = manuscriptBody(row) || "正在读取正文…";
    card.append(body);
    // 正文还没取到（非基础稿件）：这一轮先占位，取回后重画。
    if (!novel.manuscriptBodies.has(row.id) && row.id !== novel.chapter?.base_manuscript_id) {
      void ensureManuscriptBody(row).then((text) => {
        if (novel.manuscriptBodies.has(row.id) && text) {
          body.textContent = text;
        }
      });
    }
  }
  return card;
}

function renderManuscripts() {
  const host = $("#novelManuscripts");
  if (!host) return;
  const rows = manuscriptRows();
  if (!rows.length) {
    host.replaceChildren();
    return;
  }
  host.replaceChildren(...rows.map(manuscriptCard));
  const originMissing = rows.every((row) => row.kind !== "original")
    && rows.some((row) => row.kind === "legacy");
  if (originMissing) {
    const note = document.createElement("p");
    note.className = "novel-note novel-origin-missing";
    const reason = String(novel.book?.origin_recovery?.reason || "");
    note.textContent = "这一章的原文在升级前就没有单独保存过，现有正文是升级前保存的内容"
      + (reason ? `（${reason}）` : "") + "。后续生成可以直接使用这份稿件。";
    host.append(note);
  }
}

function renderManuscriptEditor() {
  const box = $("#novelManuscriptEdit");
  if (!box) return;
  const editing = Boolean(novel.editingManuscriptId) && novel.stage === "split";
  box.hidden = !editing;
  const aiBox = $("#novelAiEdit");
  if (aiBox) aiBox.hidden = !(novel.aiTargetId && novel.stage === "split");
  if (!editing) return;
  const source = manuscriptRows().find((row) => row.id === novel.editingManuscriptId);
  const head = $("#novelManuscriptEditHead");
  if (head) {
    head.textContent = `编辑${source ? manuscriptLabel(source) : "稿件"}`;
  }
  const note = $("#novelManuscriptEditNote");
  if (note) note.textContent = source ? `基于「${manuscriptLabel(source)}」` : "";
  const body = $("#novelChapterBody");
  if (body && document.activeElement !== body) body.value = novel.editDraft;
  const hint = $("#novelManuscriptEditNoteBody");
  if (hint) {
    hint.textContent = source
      ? `保存后会新增一张改编稿卡片，「${manuscriptLabel(source)}」逐字保留。内容没改动时不会重复建卡。`
      : "保存后会新增一张改编稿卡片。";
  }
  const save = $("#novelSaveChapter");
  if (save) save.disabled = novel.busy || !String(novel.editDraft || "").trim();
  const del = $("#novelDeleteChapter");
  if (del) del.disabled = novel.busy || novel.chapters.length <= 1;
}

function renderAiEditor() {
  const box = $("#novelAiEdit");
  if (!box || box.hidden) return;
  const source = manuscriptRows().find((row) => row.id === novel.aiTargetId);
  const head = $("#novelAiEditHead");
  if (head) head.textContent = source ? `AI 润色「${manuscriptLabel(source)}」` : "AI 润色 / 改编";
  const note = $("#novelAiNote");
  const result = $("#novelAiResult");
  const warning = $("#novelAiWarning");
  const slot = novel.adaptTask;
  const task = novel.tasks.find((row) => row.id === slot.activeTaskId);
  const status = String(task?.status || "");
  // 生成过程与终稿都就地显示：先看到稿子，再决定是否「保存改编稿」进作品。
  const draft = String(slot.streamText || task?.result || "");
  if (result) {
    result.textContent = draft;
    result.hidden = !draft.trim();
  }
  const bits = [];
  if (source) bits.push("AI 会根据这份稿件和你的要求生成新稿，原文会保留。");
  if (slot.progressNote) bits.push(slot.progressNote);
  if (task) {
    bits.push(`任务${TASK_STATUS_LABELS[status] || "状态未知"}。`);
    if (status === "done" && draft.trim()) bits.push("结果还没保存；保存后才会新增稿件卡片。");
  }
  if (warning) {
    const lines = [...bits];
    for (const noteRow of slot.warnings || []) lines.push(noteRow);
    warning.textContent = lines.join(" ");
  }
  if (note) {
    note.textContent = draft.trim()
      ? "上面是生成结果，还没进作品；点「保存改编稿」才会新增一张稿件卡片。"
      : (status === "queued" || status === "running"
        ? "正在生成，结果会实时显示在这里。"
        : "生成中、失败或取消只产生任务记录；成功的结果会先显示在这里，点「保存改编稿」才进作品。");
  }
  const submit = $("#novelAiSubmit");
  if (submit) {
    const ready = status === "done" && draft.trim();
    submit.textContent = ready ? "保存改编稿" : "开始改编";
    submit.disabled = novel.busy || (!ready && (!novel.aiModelName || !String($("#novelAiRequest")?.value || "").trim()));
  }
}

function renderEditor() {
  const placeholder = $("#novelEditorPlaceholder");
  const fields = $("#novelEditorFields");
  const stagePanel = $("#novelStagePanel");
  const head = $("#novelEditorHead");
  const hint = $("#novelEditorHint");
  const chapter = novel.chapter;
  const stageMode = novel.stage !== "split";
  // 阶段二：编辑区换成生成面板，章节稿件卡片整体让位。
  if (stagePanel) stagePanel.hidden = !stageMode;
  if (fields) fields.hidden = stageMode || !chapter;
  if (placeholder) placeholder.hidden = stageMode || Boolean(chapter);
  // 拆章也要同步滑条：润色面板里的那份用的是同一份状态。
  renderLimits();
  if (stageMode) {
    activateStagePanel(novel.stage);
    const copy = STAGE_COPY[novel.stage];
    if (head) head.textContent = copy?.title || stageLabel(novel.stage);
    if (hint) hint.textContent = copy?.hint || "";
    renderStagePanel();
    return;
  }
  if (!chapter) {
    if (head) head.textContent = "章节与稿件";
    if (hint) hint.textContent = novel.staged ? "勾选章节后保存为作品，再打开单章" : "选一章开始";
    return;
  }
  if (head) head.textContent = "章节与稿件";
  const rows = manuscriptRows();
  if (hint) {
    hint.textContent = `第 ${chapter.i} 章 · ${rows.length} 份稿件`
      + (chapter.base_kind === "legacy" ? " · 原文缺失" : "");
  }
  const titleInput = $("#novelChapterTitle");
  if (titleInput && document.activeElement !== titleInput) titleInput.value = chapter.title;
  const meta = $("#novelEditorMeta");
  if (meta) {
    meta.replaceChildren(
      metaItem("字数", String(chapter.len)),
      metaItem("稿件", String(rows.length)),
      metaItem("版本", `第 ${chapter.revision} 版`),
      metaItem("状态", novel.editingManuscriptId ? "正在编辑稿件" : "已保存"),
    );
  }
  renderManuscripts();
  renderManuscriptEditor();
  renderAiEditor();
}

function metaItem(label, value) {
  const wrap = document.createElement("div");
  wrap.className = "novel-meta-item";
  const span = document.createElement("span");
  span.textContent = label;
  const b = document.createElement("b");
  b.textContent = value;
  wrap.append(span, b);
  return wrap;
}

// ============================================================
// 阶段二：要素 / 大纲 / 剧本生成
// ============================================================

function versionStamp(row) {
  const ms = Number(row?.updated_at || 0);
  return fmtTime(ms);
}

function versionLabel(row) {
  const chapters = Array.isArray(row?.chapter_ids) ? row.chapter_ids : [];
  return `第 ${row.version} 版 · ${versionStamp(row)}${chapters.length ? ` · 第 ${chapters.join("、")} 章` : " · 来源未记录"}`;
}

function selectedStageVersion(stage) {
  const versions = stage === "elements" ? novel.book?.elements_versions : novel.book?.outline_versions;
  const selectedNumber = novel.selectedVersions[stage];
  return versions?.find((row) => Number(row.version) === selectedNumber) || versions?.at(-1) || null;
}

function stageSavedText(stage) {
  if (!novel.book) return "";
  if (stage === "elements") {
    const version = selectedStageVersion(stage);
    if (version?.raw) return String(version.raw);
    const elements = novel.book.elements || {};
    if (elements.raw) return String(elements.raw);
    const lines = [];
    const push = (label, rows) => {
      if (!rows || !rows.length) return;
      lines.push(`## ${label}`);
      for (const row of rows) lines.push(`· ${row}`);
    };
    push("人物", elements.characters);
    push("场景", elements.scenes);
    push("道具", elements.props);
    return lines.join("\n");
  }
  const version = selectedStageVersion(stage);
  if (version?.text) return String(version.text);
  return String(novel.book.outline || "");
}

function stageSavedAt(stage) {
  const selected = selectedStageVersion(stage);
  return selected ? versionLabel(selected) : "";
}

/** 逐章稿件选择：每章一个下拉，默认原文。 */
/** 逐章稿件选择：每章一个下拉，默认原文。
 *
 * 每章的稿件清单要单独取（单章接口才带清单）；这里按需拉一次并缓存，
 * 否则其它章节的下拉只能显示「原文」，用户看不到该章的改编稿。
 */
function renderPickManuscripts() {
  const host = $("#novelPickManuscripts");
  if (!host) return;
  const picked = [...novel.picked].sort((a, b) => a - b);
  // 拆章与分镜生图不按章节选稿：分镜吃的是已保存剧本场次，没有逐章素材。
  if (novel.stage === "split" || novel.stage === "storyboard" || !picked.length) {
    host.replaceChildren();
    host.hidden = true;
    return;
  }
  host.hidden = false;
  const rows = picked.map((chapterNo) => {
    const wrap = document.createElement("label");
    wrap.className = "novel-pick-manuscript";
    const title = document.createElement("span");
    const chapter = novel.chapters.find((row) => row.i === chapterNo);
    title.textContent = `第 ${chapterNo} 章${chapter?.title ? `《${chapter.title}》` : ""}`;
    const select = document.createElement("select");
    select.dataset.novelPickManuscript = String(chapterNo);
    select.setAttribute("aria-label", `第 ${chapterNo} 章使用哪份稿件`);
    const known = chapterNo === novel.chapterNo ? manuscriptRows() : novel.chapterRosters.get(chapterNo);
    if (!known) {
      // 还没取到这一章的稿件清单：先占位，取回后重画（不假装只有原文）。
      const pending = document.createElement("option");
      pending.value = "";
      pending.textContent = "正在读取稿件…";
      select.append(pending);
      select.disabled = true;
      void loadChapterRoster(chapterNo);
    } else {
      const options = known.length ? known : [{ id: "", label: "原文", kind: "original", len: chapter?.len || 0 }];
      for (const row of options) {
        const option = document.createElement("option");
        // 基础稿件（原文 / 升级前正文）用空值表示「默认稿件」：服务端把空值解析成
        // 该章的基础稿件，两边口径一致；用稿件 ID 当值会让默认选项选不中而显示空白。
        const isBase = row.id === (chapterNo === novel.chapterNo
          ? novel.chapter?.base_manuscript_id
          : novel.chapters.find((item) => item.i === chapterNo)?.base_manuscript_id);
        option.value = (isBase || !row.id) ? "" : row.id;
        const suffix = row.kind === "original" ? "" : `（${fmtChars(row.len)} 字）`;
        option.textContent = `${manuscriptLabel(row)}${suffix}`;
        select.append(option);
      }
      const choice = novel.manuscriptChoice.get(chapterNo) || "";
      if (choice && !options.some((row) => row.id === choice)) {
        const option = document.createElement("option");
        option.value = choice;
        option.textContent = "已选稿件（已不在这一章）";
        select.append(option);
      }
      select.value = choice;
    }
    wrap.append(title, select);
    return wrap;
  });
  host.replaceChildren(...rows);
}

/** 取某一章的稿件清单（只缓存清单，不带正文）。 */
async function loadChapterRoster(chapterNo) {
  if (!novel.bookId || novel.chapterRosters.has(chapterNo) || novel.chapterRostersInflight.has(chapterNo)) return;
  novel.chapterRostersInflight.add(chapterNo);
  try {
    const payload = await api(
      `/api/novel/books/${encodeURIComponent(novel.bookId)}/chapters/${chapterNo}`
    );
    novel.chapterRosters.set(chapterNo, Array.isArray(payload?.manuscripts) ? payload.manuscripts : []);
    if (payload?.content_manuscript_id) {
      novel.manuscriptBodies.set(payload.content_manuscript_id, String(payload.content || ""));
    }
  } catch (_error) {
    // 读不到就保持占位；用户仍可用当前打开的这一章，不因此挡住生成。
  } finally {
    novel.chapterRostersInflight.delete(chapterNo);
    renderPickManuscripts();
  }
}

function renderLimits() {
  const limits = novel.limits;
  const body = $("#novelBodyChars");
  const bodyValue = $("#novelBodyCharsValue");
  const maxChapters = $("#novelMaxChapters");
  const maxValue = $("#novelMaxChaptersValue");
  // 拆章阶段用润色面板里的那份滑条（阶段面板整块隐藏）；两份滑条共享 novel.bodyChars，
  // 这里同时同步它们的取值与范围，避免一处改了另一处显示旧值。
  const aiBody = $("#novelAiBodyChars");
  const aiValue = $("#novelAiBodyCharsValue");
  if (body) {
    body.min = String(limits.min);
    body.max = String(limits.max);
    body.step = String(limits.step);
    if (document.activeElement !== body) body.value = String(novel.bodyChars);
  }
  if (bodyValue) bodyValue.textContent = String(novel.bodyChars);
  if (maxChapters) {
    maxChapters.min = String(limits.chaptersMin);
    maxChapters.max = String(limits.chaptersMax);
    if (document.activeElement !== maxChapters) maxChapters.value = String(novel.maxChapters);
  }
  if (maxValue) maxValue.textContent = String(novel.maxChapters);
  if (aiBody) {
    aiBody.min = String(limits.min);
    aiBody.max = String(limits.max);
    aiBody.step = String(limits.step);
    if (document.activeElement !== aiBody) aiBody.value = String(novel.bodyChars);
  }
  if (aiValue) aiValue.textContent = String(novel.bodyChars);
}

function renderStagePanel() {
  const stage = novel.stage;
  const stageData = stageState(stage);
  const isStoryboard = stage === "storyboard";
  renderLimits();
  renderPickManuscripts();
  // 分镜生图不吃章节素材：正文上限滑条对它没有意义，整块让位。
  document.querySelector("#novelStagePanel .novel-limits")?.toggleAttribute("hidden", isStoryboard);

  // 素材勾选状态：用左栏已勾的章节；导入暂存态不允许生成。
  // 分镜生图看的是剧本场次数，不是勾选章节。
  const picked = [...novel.picked].sort((a, b) => a - b);
  const sceneCount = Array.isArray(novel.book?.script) ? novel.book.script.length : 0;
  const overLimit = picked.length > novel.maxChapters;
  const ready = isStoryboard
    ? Boolean(novel.bookId) && sceneCount > 0 && !novel.staged
    : Boolean(novel.bookId) && picked.length > 0 && !novel.staged && !overLimit;
  const meta = $("#novelStageMeta");
  if (meta) {
    meta.replaceChildren(
      metaItem("作品", novel.book ? `《${novel.book.title}》` : "未打开"),
      ...(isStoryboard
        ? [metaItem("剧本场次", sceneCount ? `${sceneCount} 场` : "还没有场次")]
        : [metaItem("素材章节", picked.length ? `${picked.length} 章` : "未选择"),
           metaItem("上限", `最多 ${novel.maxChapters} 章`)]),
    );
  }
  document.querySelectorAll("#novelStagePanel [data-novel-context]").forEach((section) => {
    section.hidden = section.dataset.novelContext !== stage || !novel.book;
    const elements = novel.book?.elements_versions || [];
    const outlines = novel.book?.outline_versions || [];
    const legacyElements = novel.book?.elements?.raw ? [{ version: novel.book.elements.version || 1 }] : [];
    const legacyOutline = novel.book?.outline ? [{ version: novel.book.outline_version || 1 }] : [];
    const elementRows = elements.length ? elements : legacyElements;
    const outlineRows = outlines.length ? outlines : legacyOutline;
    const elementSelect = section.querySelector("[data-context-elements]");
    const outlineSelect = section.querySelector("[data-context-outline]");
    if (elementSelect) {
      elementSelect.replaceChildren(...elementRows.map((row) => new Option(versionLabel(row), String(row.version))));
      if (!elementRows.length) elementSelect.append(new Option("尚无已保存要素", ""));
      elementSelect.disabled = !elementRows.length;
      elementSelect.value = String(novel.selectedVersions.elements || elementRows.at(-1)?.version || "");
    }
    if (outlineSelect) {
      outlineSelect.replaceChildren(...outlineRows.map((row) => new Option(versionLabel(row), String(row.version))));
      if (!outlineRows.length) outlineSelect.append(new Option("尚无已保存大纲", ""));
      outlineSelect.disabled = !outlineRows.length;
      outlineSelect.value = String(novel.selectedVersions.outline || outlineRows.at(-1)?.version || "");
    }
  });
  const generate = $("#novelStageGenerate");
  if (generate) {
    generate.disabled = !ready || stageData.busy;
    generate.textContent = stage === "script" ? "生成剧本"
      : (isStoryboard ? "生成分镜提示词" : `生成${stageLabel(stage)}`);
    generate.title = "每次点击都会新建一条任务；可以在结果确认后再次生成，不会锁死本阶段。";
  }
  const warning = $("#novelStageWarning");
  if (warning) {
    const lines = [];
    if (novel.staged) lines.push("先把导入的章节保存为作品，再生成。");
    else if (!novel.bookId) lines.push("先打开或保存一个作品。");
    else if (isStoryboard && !sceneCount) lines.push("这个作品还没有剧本场次；先到「剧本」阶段生成并保存场次。");
    else if (!isStoryboard && !picked.length) lines.push("在左侧勾选至少一章作为素材。");
    else if (!novel.stageModelName) lines.push("还没有可用模型，请到设置里检查模型。");
    else if (!isStoryboard && overLimit) {
      lines.push(`一次最多带 ${novel.maxChapters} 章（已选 ${picked.length} 章，多了 ${picked.length - novel.maxChapters} 章），请分批生成或调高上限。`);
    }
    for (const note of stageData.warnings) lines.push(note);
    warning.textContent = lines.join(" ");
  }

  // 任务区：进行中显示进度与取消；完成显示结果与写回；失败显示原因与重试。
  const taskBox = $("#novelStageTask");
  const savedBox = $("#novelStageSaved");
  const task = novel.tasks.find((row) => row.id === stageData.activeTaskId) || null;
  if (taskBox) taskBox.hidden = !task;
  if (task) {
    const stateEl = $("#novelStageTaskState");
    const status = String(task.status || "");
    if (stateEl) {
      stateEl.textContent = TASK_STATUS_LABELS[status] || "状态未知";
    }
    const note = $("#novelStageTaskNote");
    if (note) {
      const bits = [];
      const elapsed = stageData.submittedAt ? Math.max(0, Math.floor((Date.now() - stageData.submittedAt) / 1000)) : 0;
      if (status === "queued") bits.push(`生成任务在后台排队${elapsed >= 5 ? `，已等待 ${elapsed} 秒` : ""}。作品页面不会被锁住。`);
      if (status === "running") {
        if (!bodyHasText(stageData, task)) {
          bits.push(elapsed >= 8
            ? `模型仍在处理，已等待 ${elapsed} 秒，尚未返回正文；中转较慢时页面会继续等待，你可以取消任务。`
            : "正在连接模型并等待首段回复，可以切到别的阶段继续操作。");
        } else {
          const samples = stageData.recentDeltas;
          const span = samples.length > 1 ? (samples[samples.length - 1].at - samples[0].at) / 1000 : 0;
          const chars = samples.reduce((sum, row) => sum + row.chars, 0);
          const speed = span > 0 ? Math.max(1, Math.round(chars / span)) : 0;
          bits.push(`已生成 ${stageData.streamText.length || String(task.result_partial || "").length} 字${speed ? `，最近约 ${speed} 字/秒` : ""}。`);
        }
      }
      if (status === "failed" && task.error) bits.push(String(task.error));
      if (status === "cancelled") bits.push("任务已取消。");
      if (status === "interrupted") bits.push("服务重启让任务中断了，可以重试。");
      note.textContent = bits.join(" ");
    }
    const body = String(stageData.streamText || task.result || task.result_partial || "");
    const result = $("#novelStageResult");
    if (result) result.textContent = body;
    const cancel = $("#novelStageCancel");
    if (cancel) cancel.hidden = TERMINAL_TASK_STATUSES.has(status);
    const retry = $("#novelStageRetry");
    if (retry) retry.hidden = !["failed", "cancelled", "interrupted"].includes(status);
    const apply = $("#novelStageApply");
    // 只有「已完成且结果非空」才能写回：写回是覆盖动作，不能拿空结果覆盖已有成果。
    // 分镜提示词不写回作品（结果直接发给造梦机），所以这一阶段不出现写回按钮。
    if (apply) {
      apply.hidden = isStoryboard || !(status === "done" && body.trim());
      apply.textContent = stage === "script" ? "保存为新剧本版本" : `保存为新${stageLabel(stage)}版本`;
      apply.title = "保存会新增一个版本并保留旧版；后续阶段可选择使用哪个版本。";
    }
  }
  renderStageTaskHistory(stage, stageData.activeTaskId);

  // 「作品里已保存的结果」只给要素/大纲用；剧本阶段由场次列表承担；
  // 分镜生图不写回作品，没有已保存版本。
  const saved = stage === "script" || isStoryboard ? "" : stageSavedText(stage);
  if (savedBox) savedBox.hidden = !saved;
  if (saved) {
    const versionSelect = $("#novelStageVersion");
    const versions = stage === "elements" ? (novel.book?.elements_versions || []) : (novel.book?.outline_versions || []);
    if (versionSelect) {
      const currentNumber = stage === "elements" ? novel.book?.elements?.version : novel.book?.outline_version;
      const rows = versions.length ? versions : [{ version: currentNumber || 1 }];
      versionSelect.replaceChildren(...rows.map((row) => new Option(versionLabel(row), String(row.version))));
      versionSelect.value = String(novel.selectedVersions[stage] || rows.at(-1).version);
      versionSelect.hidden = rows.length < 2;
    }
    const savedBody = $("#novelStageSavedBody");
    if (savedBody) savedBody.textContent = saved;
    const savedAt = $("#novelStageSavedAt");
    if (savedAt) savedAt.textContent = stageSavedAt(stage);
  }
  renderSceneBlock(stage === "script");
  renderStoryboard(stageData, novel.tasks.find((row) => row.id === stageData.activeTaskId) || null);
  applyStageCollapse();
}

// 结果/已保存区块的折叠态：每个阶段各自记，键 `阶段:task|saved`。
// 只隐藏正文（结果 pre / 说明 / 已保存正文），标题与操作按钮始终在。
function applyStageCollapse() {
  const kinds = { task: "#novelStageTask", saved: "#novelStageSaved" };
  for (const [kind, panelSelector] of Object.entries(kinds)) {
    const collapsed = novel.collapsed.has(`${novel.stage}:${kind}`);
    const toggle = document.querySelector(`#novelStagePanel [data-novel-collapse="${kind}"]`);
    if (toggle) toggle.setAttribute("aria-expanded", String(!collapsed));
    const panel = $(panelSelector);
    if (panel) panel.classList.toggle("is-collapsed", collapsed);
    if (kind === "task") {
      const body = $("#novelStageResult");
      if (body) body.hidden = collapsed;
      const note = $("#novelStageTaskNote");
      if (note) note.hidden = collapsed;
    } else {
      const body = $("#novelStageSavedBody");
      if (body) body.hidden = collapsed;
    }
  }
}

/** 最近任务：紧凑行，最多 3 条；完整历史在「管理记录」的生成记录页签。 */
function renderStageTaskHistory(stage, activeTaskId) {
  const list = $("#novelTaskHistory");
  if (!list) return;
  const rows = novel.tasks
    .filter((row) => row.module === "novel"
      && row.novel?.book_id === novel.bookId
      && row.novel?.stage === stage)
    .sort((a, b) => Number(b.created_at || 0) - Number(a.created_at || 0));
  if (!rows.length) {
    list.replaceChildren();
    list.hidden = true;
    return;
  }
  list.hidden = false;
  const head = document.createElement("div");
  head.className = "novel-task-history-head";
  const label = document.createElement("span");
  label.textContent = "最近任务";
  const all = document.createElement("button");
  all.type = "button";
  all.className = "novel-link-button";
  all.dataset.novelManageTab = "task";
  all.textContent = "全部记录";
  all.title = "在「管理记录」里查看全部生成记录";
  head.append(label, all);
  const shown = rows.slice(0, novel.recentTaskLimit).map((task) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "novel-task-row";
    button.dataset.novelTask = task.id;
    button.classList.toggle("active", task.id === activeTaskId);
    const title = document.createElement("b");
    title.textContent = `${task.title || stageLabel(stage)}`;
    const meta = document.createElement("small");
    const status = document.createElement("em");
    status.textContent = TASK_STATUS_LABELS[task.status] || "状态未知";
    const time = document.createElement("span");
    time.textContent = fmtTime(task.updated_at || task.created_at);
    meta.append(status, time);
    const open = document.createElement("span");
    open.className = "novel-task-open";
    open.textContent = "查看";
    button.append(title, meta, open);
    return button;
  });
  list.replaceChildren(head, ...shown);
}

function bodyHasText(stageData, task) {
  return Boolean(String(stageData.streamText || task.result || task.result_partial || "").trim());
}

// ============================================================
// 右栏：本次会做什么（随阶段、选章、选稿与保存状态变化）
// ============================================================

function renderPlan() {
  const host = $("#novelPlanBody");
  if (!host) return;
  const stageEl = $("#novelPreviewStage");
  if (stageEl) {
    stageEl.textContent = stageLabel(novel.stage);
  }
  const stateEl = $("#novelPlanState");
  if (stateEl) {
    stateEl.textContent = novel.staged ? "待保存作品" : (novel.bookId ? "已保存为作品" : "未打开作品");
  }
  const blocks = [];
  const push = (title, lines) => {
    const rows = (lines || []).filter((line) => String(line || "").trim());
    if (!rows.length) return;
    blocks.push({ title, lines: rows.map(String) });
  };

  push("这一步做什么", [STAGE_PLAN_COPY[novel.stage] || ""]);

  if (!novel.bookId) {
    push("还差什么", ["先导入一本 TXT 并保存为作品。"]);
  } else {
    const picked = [...novel.picked].sort((a, b) => a - b);
    const model = novel.stage === "split"
      ? { name: novel.aiModelName, key: novel.aiProviderKey }
      : { name: novel.stageModelName, key: novel.stageProviderKey };
    push("作品与模型", [
      `作品《${novel.book?.title || ""}》已保存。`,
      model.name ? `使用的模型：${model.name}` : "还没有选择模型。",
    ]);
    if (novel.stage === "split") {
      const chapter = novel.chapter;
      const row = chapter ? manuscriptRows().find((item) => item.id === (novel.aiTargetId || novel.editingManuscriptId)) : null;
      push("这一章怎么用", [
        chapter ? `当前第 ${chapter.i} 章，共 ${manuscriptRows().length} 份稿件。` : "还没有打开章节。",
        row ? `AI 润色基于「${manuscriptLabel(row)}」（${fmtChars(row.len)} 字）。` : "",
        "AI 润色固定只改这一份稿件，不会转成整本改编。",
      ]);
    } else if (picked.length) {
      push("本次素材（逐章）", picked.map((chapterNo) => {
        const chapter = novel.chapters.find((item) => item.i === chapterNo);
        const choiceId = novel.manuscriptChoice.get(chapterNo);
        const known = novel.chapterNo === chapterNo ? manuscriptRows().find((row) => row.id === choiceId) : null;
        const label = known ? manuscriptLabel(known) : (choiceId ? "已选稿件" : "原文");
        const length = known ? known.len : (chapter?.len || 0);
        return `第 ${chapterNo} 章《${chapter?.title || ""}》用「${label}」，${fmtChars(length)} 字`;
      }));
    } else {
      push("本次素材", ["还没有勾选章节。"]);
    }
    if (novel.stage === "script") {
      const sources = novel.budget?.sources || {};
      push("引用详情", [
        sources.uses_characters ? `人物设定：${sources.elements_label || "已保存的版本"}` : "人物设定：未使用。",
        sources.uses_outline ? `改编大纲：${sources.outline_label || "已保存的版本"}` : "改编大纲：未使用，直接根据章节改编。",
        sources.has_summaries ? "会引用已保存的前文摘要（只在本批之前的章节）。" : "没有可用的前文摘要。",
      ]);
    }
  }

  // 预算与实际输入：数值全部来自服务端预检，不在前端重算。
  const budget = novel.budget;
  if (novel.stage === "storyboard") {
    // 分镜生图没有「每章上限」概念，说明按场次口径来写。
    const scenes = Array.isArray(novel.book?.script) ? novel.book.script : [];
    const kgCount = Number(budget?.kg_entities || 0);
    const lines = [
      `按剧本里已保存的 ${scenes.length} 场生成，每场一条出图提示词。`,
      budget ? `这次发送的场次正文与说明约 ${fmtChars(budget.chars)} 字。` : "",
      kgCount
        ? `已加入已保存要素里的 ${kgCount} 条设定参考，帮助人物与场景在这组图里保持一致。`
        : "还没有已保存要素；提示词只按场次内容来写。",
      budget?.context_known
        ? `估算输入约 ${budget.estimated_tokens} tokens，模型可用约 ${budget.available_tokens} tokens。`
        : (budget ? "模型上下文容量未知：无法确认能否容纳本次输入。" : ""),
    ].filter(Boolean);
    push("实际输入与限制", lines.length ? lines : ["勾选章节后这里会显示实际输入。"]);
  } else if (budget) {
    const lines = [
      `每章上限 ${budget.body_limit} 字，一次最多 ${budget.max_chapters} 章。`,
      `这次发送正文约 ${fmtChars(budget.used_chars)} 字（原文共 ${fmtChars(budget.total_source_chars)} 字）。`,
    ];
    if (budget.dropped_chars > 0) {
      lines.push(`有 ${fmtChars(budget.dropped_chars)} 字因每章上限被省略；生成前就已经知道，不会等排队后才说。`);
    }
    if (Number(budget.kg_entities) > 0) {
      lines.push(`已加入已保存要素里的 ${budget.kg_entities} 条设定参考，帮助人物与设定保持一致。`);
    }
    lines.push(budget.context_known
      ? `估算输入约 ${budget.estimated_tokens} tokens，模型可用约 ${budget.available_tokens} tokens。`
      : "模型上下文容量未知：无法确认能否容纳本次素材。");
    if (budget.fits === false) lines.push("按估算已经超出模型可用空间，提交会被拦下。");
    for (const suggestion of budget.suggestions || []) lines.push(suggestion);
    push("实际输入与限制", lines);
  } else if (novel.stage !== "split" && novel.bookId) {
    push("实际输入与限制", [
      `每章上限 ${novel.bodyChars} 字，一次最多 ${novel.maxChapters} 章。`,
      "勾选章节后这里会按真实稿件算出实际引用字数与估算 token。",
    ]);
  }
  for (const note of novel.stageWarnings || []) {
    push("提示", [note]);
  }

  push("保存后会怎样", [
    novel.stage === "split"
      ? "手动编辑与 AI 润色的结果都要点「保存」才会成为新的改编稿卡片。"
      : "仅生成、尚未保存到作品的内容，不会自动被后续步骤引用。",
    "保存状态只是状态信息，不是按钮。",
  ]);

  host.replaceChildren(...blocks.map((block) => {
    const section = document.createElement("div");
    section.className = "novel-plan-block";
    const title = document.createElement("strong");
    title.textContent = block.title;
    const list = document.createElement("ul");
    for (const line of block.lines) {
      const item = document.createElement("li");
      item.textContent = line;
      list.append(item);
    }
    section.append(title, list);
    return section;
  }));
}

function renderLocation() {
  const note = $("#novelLocationNote");
  const pathEl = $("#novelLocationPath");
  const copyBtn = $("#novelCopyPath");
  const openBtn = $("#novelOpenFolder");
  const location = novel.location;
  const available = Boolean(location?.novel_root);
  if (copyBtn) copyBtn.hidden = !available;
  if (openBtn) {
    // 手机网页不能打开这台电脑的文件夹：只有桌面版（pywebview 桥）才给按钮。
    const desktop = Boolean(window.pywebview?.api);
    openBtn.hidden = !available || !location?.can_open_folder || !desktop;
  }
  if (pathEl) {
    pathEl.hidden = !available;
    pathEl.textContent = available ? String(location.novel_root) : "";
  }
  if (note) {
    note.textContent = available
      ? `作品保存在这台电脑的数据目录下（studio/novel/<作品ID>）。导出由浏览器发起下载，通常进入浏览器默认下载目录；如果系统弹出保存位置选择，会按你选的位置保存。`
      : "打开作品后，这里显示这台电脑上的实际保存目录。";
  }
}

function refreshPreview() {
  renderPlan();
  renderLocation();
}

// ============================================================
// 剧本场次（阶段三）
// ============================================================

function renderSceneBlock(visible) {
  const block = $("#novelSceneBlock");
  if (!block) return;
  block.hidden = !visible;
  if (!visible) return;
  const versions = Array.isArray(novel.book?.script_versions) ? novel.book.script_versions : [];
  const latestVersion = Number(novel.book?.script_version || versions.at(-1)?.version || 0);
  const selectedVersion = novel.selectedVersions.script || latestVersion;
  const historical = selectedVersion !== latestVersion;
  const versionRow = versions.find((row) => Number(row.version) === selectedVersion);
  const rows = historical ? (versionRow?.script || []) : (Array.isArray(novel.book?.script) ? novel.book.script : []);
  const versionSelect = $("#novelScriptVersion");
  if (versionSelect) {
    const options = versions.length ? versions : (rows.length ? [{ version: latestVersion || 1 }] : []);
    versionSelect.replaceChildren(...options.map((row) => new Option(versionLabel(row), String(row.version))));
    versionSelect.hidden = options.length < 2;
    if (options.length) versionSelect.value = String(selectedVersion || options.at(-1).version);
  }
  if (novel.sceneEditing && historical) novel.sceneEditing = 0;
  const list = $("#novelSceneList");
  const count = $("#novelSceneCount");
  if (count) count.textContent = versionRow ? versionLabel(versionRow) : (rows.length ? `共 ${rows.length} 场` : "");
  const note = $("#novelSceneNote");
  if (note) {
    if (!rows.length) {
      note.textContent = STAGE_COPY.script.empty;
    } else {
      note.textContent = historical
        ? `正在查看第 ${selectedVersion} 版（只读）；切回最新版本后可编辑。`
        : (novel.book?.script_parsed === false
          ? "上次生成的剧本没能识别出场次结构，已按整段保存；可以手动编辑成 INT./EXT. 场景行。"
          : "点一场可以编辑；↑↓ 调整顺序，改动会立刻保存。");
    }
  }
  if (!list) return;
  list.replaceChildren(...rows.map(sceneRow));
  list.querySelectorAll("[data-novel-scene-action], [data-novel-scene-open]").forEach((button) => { button.disabled = historical; });
  renderSceneEdit();
}

function sceneRow(row) {
  const no = Number(row.i) || 0;
  const item = document.createElement("div");
  item.className = "novel-scene-row";
  item.classList.toggle("active", no === novel.sceneEditing);
  item.dataset.novelScene = String(no);
  const head = document.createElement("button");
  head.type = "button";
  head.className = "novel-scene-open";
  head.dataset.novelSceneOpen = String(no);
  head.setAttribute("aria-expanded", String(no === novel.sceneEditing));
  const strong = document.createElement("strong");
  strong.textContent = String(row.scene || "（没有场景行）");
  const small = document.createElement("small");
  const chapter = Number(row.from_chapter) || 0;
  const bodyLen = String(row.content || "").length;
  small.textContent = `${chapter ? `第 ${chapter} 章` : "来源未记录"} · ${bodyLen} 字`;
  head.append(strong, small);
  const actions = document.createElement("span");
  actions.className = "novel-scene-actions";
  const mk = (label, action, title) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "control-button tiny";
    btn.dataset.novelSceneAction = action;
    btn.dataset.novelSceneNo = String(no);
    btn.title = title;
    btn.textContent = label;
    return btn;
  };
  actions.append(
    mk("↑", "up", "与上一场交换位置"),
    mk("↓", "down", "与下一场交换位置"),
    mk("删除", "delete", "删除这一场"),
  );
  item.append(head, actions);
  return item;
}

function renderSceneEdit() {
  const box = $("#novelSceneEdit");
  if (!box) return;
  const latestVersion = Number(novel.book?.script_version || novel.book?.script_versions?.at(-1)?.version || 0);
  const historical = Number(novel.selectedVersions.script || latestVersion) !== latestVersion;
  const rows = historical
    ? (novel.book?.script_versions?.find((item) => Number(item.version) === novel.selectedVersions.script)?.script || [])
    : (Array.isArray(novel.book?.script) ? novel.book.script : []);
  const row = rows.find((item) => Number(item.i) === novel.sceneEditing);
  box.hidden = historical || !row;
  if (!row) return;
  const headInput = $("#novelSceneHead");
  if (headInput && document.activeElement !== headInput) headInput.value = String(row.scene || "");
  const bodyInput = $("#novelSceneBody");
  if (bodyInput && document.activeElement !== bodyInput) bodyInput.value = String(row.content || "");
}

async function saveScene() {
  const latestVersion = Number(novel.book?.script_version || novel.book?.script_versions?.at(-1)?.version || 0);
  if (!novel.bookId || !novel.sceneEditing || Number(novel.selectedVersions.script || latestVersion) !== latestVersion) return;
  const scene = String($("#novelSceneHead")?.value || "").trim();
  const content = String($("#novelSceneBody")?.value ?? "");
  novel.sceneBusy = true;
  try {
    const payload = await api(
      `/api/novel/books/${encodeURIComponent(novel.bookId)}/script/${novel.sceneEditing}`,
      { method: "PATCH", body: { scene, content, revision: novel.book?.revision } }
    );
    const rows = Array.isArray(novel.book?.script) ? [...novel.book.script] : [];
    const index = rows.findIndex((item) => Number(item.i) === novel.sceneEditing);
    if (index >= 0) rows[index] = { ...rows[index], ...payload.scene };
    novel.book = { ...novel.book, script: rows, revision: payload.revision };
    setSaveState("已保存", `第 ${novel.sceneEditing} 场已保存。`);
    renderAll();
    toast("场次已保存");
  } catch (error) {
    fail("保存场次失败", error);
  } finally {
    novel.sceneBusy = false;
  }
}

async function deleteScene(sceneNo) {
  if (!novel.bookId) return;
  if (!window.confirm(`要删除第 ${sceneNo} 场吗？删除后不能撤销。`)) return;
  try {
    await api(
      `/api/novel/books/${encodeURIComponent(novel.bookId)}/script/${sceneNo}`,
      { method: "DELETE" }
    );
    if (novel.sceneEditing === sceneNo) novel.sceneEditing = 0;
    // 编号会重排，必须从服务端重新读一次，不能只在前端删一条。
    const payload = await api(`/api/novel/books/${encodeURIComponent(novel.bookId)}/script`);
    novel.book = { ...novel.book, script: payload.script, revision: payload.revision };
    renderAll();
    toast(`已删除第 ${sceneNo} 场`);
  } catch (error) {
    fail("删除场次失败", error);
  }
}

async function moveScene(sceneNo, direction) {
  if (!novel.bookId) return;
  const rows = Array.isArray(novel.book?.script) ? novel.book.script : [];
  const index = rows.findIndex((item) => Number(item.i) === sceneNo);
  const target = direction === "up" ? index - 1 : index + 1;
  if (index < 0 || target < 0 || target >= rows.length) return;
  const order = rows.map((item) => Number(item.i));
  [order[index], order[target]] = [order[target], order[index]];
  try {
    const payload = await api(
      `/api/novel/books/${encodeURIComponent(novel.bookId)}/script/reorder`,
      { method: "POST", body: { order } }
    );
    novel.book = { ...novel.book, script: payload.script, revision: payload.revision };
    novel.sceneEditing = target + 1;
    renderAll();
  } catch (error) {
    fail("调整场次顺序失败", error);
  }
}

// ============================================================
// 分镜生图（阶段四）：逐场解析与发送到造梦机
// ============================================================

/** 逐场行在勾选集里的键：场次号 + 序号，模型漏号/重号也不会互相顶掉。 */
function storyboardRowKey(row, index) {
  return `${Number(row?.scene_no) || 0}:${index}`;
}

/** 与后端 parse_storyboard 同口径的逐场拆分；识别不到结构就 parsed=false 整段显示。 */
function parseStoryboardClient(text) {
  const raw = String(text || "");
  if (!raw.trim()) return { rows: [], parsed: false };
  const rows = [];
  let current = null;
  let buffer = [];
  const flush = () => {
    if (!current) return;
    rows.push({ scene_no: current.scene_no, scene_head: current.scene_head, prompt: buffer.join("\n").trim() });
    current = null;
    buffer = [];
  };
  for (const line of raw.split(/\r?\n/)) {
    const match = line.match(/^[ \t　]*【\s*第\s*(\d+)\s*场\s*】\s*(.*)$/);
    if (match) {
      flush();
      current = { scene_no: Number(match[1]), scene_head: match[2].trim() };
      continue;
    }
    if (current) buffer.push(line);
  }
  flush();
  if (!rows.some((row) => row.scene_no)) return { rows: [], parsed: false };
  return { rows, parsed: true };
}

/** 任务完成后把结果解析成逐场行（每个任务只解析一次，轮询重画不重算）。 */
function syncStoryboardRows(stageData, task) {
  if (!task || String(task.status || "") !== "done") return;
  if (stageData.rowsForTaskId === task.id) return;
  const text = String(stageData.streamText || task.result || task.result_partial || "");
  const { rows, parsed } = parseStoryboardClient(text);
  stageData.rows = rows;
  stageData.parsed = parsed;
  stageData.checked = new Set(rows.map((row, index) => storyboardRowKey(row, index)));
  stageData.rowsForTaskId = task.id;
}

function storyboardRow(row, index, stageData) {
  const key = storyboardRowKey(row, index);
  const wrap = document.createElement("label");
  wrap.className = "novel-storyboard-row";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.dataset.novelStoryboardRow = key;
  box.checked = stageData.checked.has(key);
  box.setAttribute("aria-label", `发送第 ${row.scene_no} 场提示词`);
  const copy = document.createElement("span");
  copy.className = "novel-storyboard-copy";
  const head = document.createElement("strong");
  head.textContent = row.scene_head || `第 ${row.scene_no} 场`;
  const body = document.createElement("small");
  body.textContent = row.prompt;
  copy.append(head, body);
  wrap.append(box, copy);
  return wrap;
}

function renderStoryboard(stageData, task) {
  const note = $("#novelStoryboardNote");
  const list = $("#novelStoryboardList");
  const send = $("#novelStoryboardSend");
  const all = $("#novelStoryboardAll");
  if (!note || !list || !send || !all) return;
  const scenes = Array.isArray(novel.book?.script) ? novel.book.script : [];
  const status = String(task?.status || "");
  const setNoteOnly = (text) => {
    note.textContent = text;
    list.replaceChildren();
    send.hidden = true;
    all.hidden = true;
  };
  if (!scenes.length) {
    setNoteOnly("这个作品还没有剧本场次。先到「剧本」阶段生成并保存场次，再回来生成分镜。");
    return;
  }
  if (!task || status !== "done") {
    setNoteOnly(!task
      ? `共 ${scenes.length} 场。点「生成分镜提示词」，模型会把每一场转成一条出图提示词。`
      : "生成完成后，这里会按场列出提示词。");
    return;
  }
  syncStoryboardRows(stageData, task);
  if (!stageData.parsed) {
    setNoteOnly("没能识别出「【第 N 场】」的场次结构，上面是模型原文，没有强行拆分。可以点「重试」再生成一次。");
    return;
  }
  const rows = stageData.rows;
  list.replaceChildren(...rows.map((row, index) => storyboardRow(row, index, stageData)));
  const checkedCount = stageData.checked.size;
  send.hidden = false;
  send.textContent = checkedCount ? `发送到造梦机（${checkedCount} 条）` : "发送到造梦机";
  send.disabled = !checkedCount;
  all.hidden = rows.length < 2;
  all.textContent = checkedCount === rows.length ? "清空勾选" : "全选";
  note.textContent = `共 ${rows.length} 场提示词。勾选后发送到造梦机，在造梦机里逐条「填入」正向提示词出图；提示词不写回作品。`;
}

/** 把勾选的分镜提示词批量带到造梦机（跨页交接，不做剪贴板中转）。 */
function sendStoryboardToComfyui() {
  const stageData = stageState();
  const task = novel.tasks.find((row) => row.id === stageData.activeTaskId) || null;
  syncStoryboardRows(stageData, task);
  if (!stageData.parsed || !stageData.rows.length) {
    toast("还没有可发送的分镜提示词");
    return;
  }
  const items = stageData.rows
    .map((row, index) => ({ row, key: storyboardRowKey(row, index) }))
    .filter((item) => stageData.checked.has(item.key))
    .map((item) => ({ no: item.row.scene_no, head: item.row.scene_head || "", prompt: item.row.prompt }));
  if (!items.length) {
    toast("先勾选要发送的场次");
    return;
  }
  document.dispatchEvent(new CustomEvent("naiba:storyboard-handoff", { detail: { items } }));
  setStudioPage("comfyui");
  toast(`已把 ${items.length} 条分镜提示词带到造梦机`);
}

function toggleStoryboardAll() {
  const stageData = stageState();
  if (stageData.checked.size >= stageData.rows.length) stageData.checked.clear();
  else {
    stageData.rows.forEach((row, index) => stageData.checked.add(storyboardRowKey(row, index)));
  }
  const task = novel.tasks.find((row) => row.id === stageData.activeTaskId) || null;
  renderStoryboard(stageData, task);
}

// ============================================================
// 模型选择（与提词库同口径）
// ============================================================

function providerRows() {
  const profiles = Array.isArray(state.bootstrap?.model_profiles)
    ? state.bootstrap.model_profiles
    : (Array.isArray(state.bootstrap?.providers) ? state.bootstrap.providers : []);
  return profiles
    .map((row) => ({
      key: String(row?.model_key || `online:${row?.id || ""}`),
      label: String(row?.name || row?.model_key || row?.id || ""),
    }))
    .filter((row) => row.key && row.label);
}

function fillProviderSelect(select, previousKey) {
  if (!select) return "";
  const rows = providerRows();
  select.replaceChildren(...rows.map((row) => {
    const option = document.createElement("option");
    option.value = row.key;
    option.textContent = row.label;
    return option;
  }));
  if (!rows.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "请先在设置中添加模型";
    select.append(option);
    return "";
  }
  const preferred = [
    previousKey,
    $("#modelSelect")?.value || "",
    state.bootstrap?.default_model_key || "",
  ].find((key) => key && rows.some((row) => row.key === key));
  select.value = preferred || rows[0].key;
  return select.value;
}

/** 按当前供应商拉完整模型目录，填进模型下拉。 */
async function fillModelSelect(select, providerKey) {
  if (!select) return "";
  if (!providerKey) {
    select.replaceChildren(new Option("请先在设置中添加模型", ""));
    return "";
  }
  let models = novel.stageModelsCache.get(providerKey);
  if (!models) {
    const inflight = novel.stageModelInflight.get(providerKey);
    if (inflight) {
      models = await inflight;
    } else {
      const task = (async () => {
        try {
          const payload = await api("/api/providers/models", {
            method: "POST",
            body: { model_key: providerKey },
          });
          return Array.isArray(payload?.models) ? payload.models : [];
        } catch (error) {
          return [];
        }
      })();
      novel.stageModelInflight.set(providerKey, task);
      try {
        models = await task;
      } finally {
        novel.stageModelInflight.delete(providerKey);
      }
      novel.stageModelsCache.set(providerKey, models);
    }
  }
  const names = [];
  for (const entry of models) {
    const id = typeof entry === "object"
      ? String(entry?.id ?? entry?.model ?? entry?.name ?? "")
      : String(entry ?? "");
    const clean = id.trim();
    if (clean && !names.includes(clean)) names.push(clean);
  }
  if (!names.length) {
    // 目录读不到：回退到这张卡片上配置的模型名（用户自己填的，能直接用）。
    const profile = providerRows().find((row) => row.key === providerKey);
    const configured = String(
      (state.bootstrap?.model_profiles || []).find((row) => row.model_key === providerKey)?.model
      || profile?.label || ""
    ).trim();
    if (configured) names.push(configured);
  }
  const previous = select.value;
  select.replaceChildren(...names.map((name) => new Option(name, name)));
  if (!names.length) {
    select.replaceChildren(new Option("模型目录读取失败，请到设置里检查", ""));
    return "";
  }
  select.value = names.includes(previous) ? previous : names[0];
  return select.value;
}

let stageModelsChain = Promise.resolve();
function fillStageModels() {
  stageModelsChain = stageModelsChain.then(async () => {
    const stageSelect = $("#novelStageProvider");
    novel.stageProviderKey = fillProviderSelect(stageSelect, novel.stageProviderKey);
    novel.stageModelName = await fillModelSelect($("#novelStageModel"), novel.stageProviderKey);
    const aiSelect = $("#novelAiProvider");
    novel.aiProviderKey = fillProviderSelect(aiSelect, novel.aiProviderKey || novel.stageProviderKey);
    novel.aiModelName = await fillModelSelect($("#novelAiModel"), novel.aiProviderKey);
  }).catch(() => {});
  return stageModelsChain;
}

function readStageModel() {
  return {
    model_key: novel.stageProviderKey || String($("#novelStageProvider")?.value || ""),
    model_name: novel.stageModelName || String($("#novelStageModel")?.value || ""),
  };
}

function readAiModel() {
  return {
    model_key: novel.aiProviderKey || String($("#novelAiProvider")?.value || ""),
    model_name: novel.aiModelName || String($("#novelAiModel")?.value || ""),
  };
}

// ============================================================
// 任务轮询
// ============================================================

function stopTaskPolling() {
  if (novel.taskPollTimer) {
    clearTimeout(novel.taskPollTimer);
    novel.taskPollTimer = null;
  }
}

async function refreshStageTasks() {
  try {
    const { tasks } = await api(STAGE_TASKS_URL);
    novel.tasks = Array.isArray(tasks) ? tasks : [];
  } catch (error) {
    // 读不到任务列表不该挡住生成面板的其它操作，只如实提示。
    fail("读取创作场任务失败", error);
    return;
  }
  renderStagePanel();
  scheduleTaskPolling();
}

function scheduleTaskPolling() {
  stopTaskPolling();
  const slot = activeTaskSlot();
  const task = novel.tasks.find((row) => row.id === slot.activeTaskId);
  if (!task || TERMINAL_TASK_STATUSES.has(String(task.status || ""))) return;
  novel.taskPollTimer = setTimeout(() => void pollActiveTask(), 1200);
}

async function pollActiveTask() {
  const slot = activeTaskSlot();
  if (novel.taskPollBusy || !slot.activeTaskId) return;
  novel.taskPollBusy = true;
  const token = novel.taskToken;
  try {
    const { task } = await api(`/api/studio/tasks/${encodeURIComponent(slot.activeTaskId)}`);
    if (token !== novel.taskToken) return;
    const index = novel.tasks.findIndex((row) => row.id === task.id);
    if (index >= 0) novel.tasks[index] = { ...novel.tasks[index], ...task };
    else novel.tasks.unshift(task);
    // 增量事件只取正文片段，不重复拉全量结果。
    try {
      const events = await api(
        `/api/studio/tasks/${encodeURIComponent(slot.activeTaskId)}/events?after=${slot.taskCursor}`
      );
      for (const row of events.events || []) {
        if (row.type === "delta" && row.content) {
          const chunk = String(row.content);
          slot.streamText += chunk;
          if (!slot.firstDeltaAt) slot.firstDeltaAt = Date.now();
          if (Array.isArray(slot.recentDeltas)) {
            slot.recentDeltas.push({ at: Date.now(), chars: chunk.length });
            slot.recentDeltas = slot.recentDeltas.filter((sample) => Date.now() - sample.at <= 10000);
          }
        }
        // 分块续写（4A）的进度事件：「正在生成第 i/N 块」。
        if (row.type === "status" && row.message) slot.progressNote = String(row.message);
        if (typeof row.seq === "number") slot.taskCursor = Math.max(slot.taskCursor, row.seq);
      }
    } catch (_error) {
      // 事件流读不到时退回用任务记录里的 result_partial，不影响终态判断。
    }
    if (novel.stage === "split") renderAiEditor();
    else renderStagePanel();
  } catch (error) {
    if (token === novel.taskToken) fail("读取任务状态失败", error);
  } finally {
    novel.taskPollBusy = false;
    scheduleTaskPolling();
  }
}

// ============================================================
// 生成动作
// ============================================================

/** 提交前预检：把右栏说明刷新成真实输入（不建任务、不计费）。 */
async function refreshPreviewBudget() {
  const stage = novel.stage;
  if (!novel.bookId || stage === "split") {
    novel.budget = null;
    refreshPreview();
    return;
  }
  // 分镜生图吃剧本场次，不要求勾选章节。
  const picked = [...novel.picked].sort((a, b) => a - b);
  if (stage !== "storyboard" && !picked.length) {
    novel.budget = null;
    refreshPreview();
    return;
  }
  const token = ++novel.budgetToken;
  const model = readStageModel();
  try {
    const payload = await api("/api/novel/stage/preview", {
      method: "POST",
      body: {
        book_id: novel.bookId,
        stage,
        ...(stage === "storyboard"
          ? { scene_ids: [] }
          : {
              chapter_ids: picked,
              manuscripts: manuscriptsPayload(),
              body_chars: novel.bodyChars,
              max_chapters: novel.maxChapters,
              elements_version: novel.selectedVersions.elements || undefined,
              outline_version: novel.selectedVersions.outline || undefined,
            }),
        extra_note: String($("#novelStageNote")?.value || ""),
        model_key: model.model_key,
      },
    });
    if (token !== novel.budgetToken) return;
    novel.budget = payload.budget || null;
    novel.stageWarnings = Array.isArray(payload.warnings) ? payload.warnings : [];
  } catch (error) {
    if (token !== novel.budgetToken) return;
    novel.budget = null;
    novel.stageWarnings = [String(error?.message || error)];
  }
  refreshPreview();
}

/** 逐章稿件选择：只提交有明确选择的章，未选的章由服务端用基础稿件。 */
function manuscriptsPayload() {
  const out = {};
  for (const [chapterNo, manuscriptId] of novel.manuscriptChoice.entries()) {
    if (manuscriptId) out[String(chapterNo)] = manuscriptId;
  }
  return out;
}

async function generateStage({ confirmLarge = false } = {}) {
  if (!novel.bookId) return;
  const stage = novel.stage;
  const stageData = stageState(stage);
  const isStoryboard = stage === "storyboard";
  // 分镜生图的素材是已保存剧本场次，不勾选章节。
  const picked = [...novel.picked].sort((a, b) => a - b);
  if (!isStoryboard) {
    if (!picked.length) {
      toast("先勾选至少一章作为素材");
      return;
    }
    if (picked.length > novel.maxChapters) {
      toast(`一次最多带 ${novel.maxChapters} 章，请分批生成或调高上限`);
      return;
    }
  }
  const model = readStageModel();
  if (!model.model_name) {
    toast("先在设置里检查模型，再回来选择");
    return;
  }
  stageData.busy = true;
  stageData.warnings = [];
  renderStagePanel();
  try {
    const payload = await api("/api/novel/stage", {
      method: "POST",
      body: {
        book_id: novel.bookId,
        stage,
        ...(isStoryboard
          ? { scene_ids: [] }
          : {
              chapter_ids: picked,
              manuscripts: manuscriptsPayload(),
              body_chars: novel.bodyChars,
              max_chapters: novel.maxChapters,
              elements_version: novel.selectedVersions.elements || undefined,
              outline_version: novel.selectedVersions.outline || undefined,
            }),
        extra_note: String($("#novelStageNote")?.value || ""),
        model_key: model.model_key,
        model_name: model.model_name,
        confirm_large: confirmLarge ? 1 : undefined,
      },
    });
    stageData.activeTaskId = payload.task?.id || "";
    stageData.streamText = "";
    stageData.taskCursor = 0;
    stageData.warnings = Array.isArray(payload.warnings) ? payload.warnings : [];
    stageData.submittedAt = Date.now();
    stageData.firstDeltaAt = 0;
    stageData.recentDeltas = [];
    stageData.progressNote = "";
    stageData.rows = [];
    stageData.parsed = false;
    stageData.checked = new Set();
    stageData.rowsForTaskId = "";
    // 新任务提交后展开结果区：上一轮用户收起来过，也不该挡住这次的新结果。
    novel.collapsed.delete(`${stage}:task`);
    novel.budget = payload.budget || novel.budget;
    if (novel.book && Number.isInteger(Number(payload.book_revision))) {
      novel.book.revision = Number(payload.book_revision);
    }
    if (payload.task) novel.tasks.unshift(payload.task);
    renderStagePanel();
    refreshPreview();
    scheduleTaskPolling();
  } catch (error) {
    // 容量未知且输入很大：服务端要求确认，问过用户再提交。
    const message = String(error?.message || error || "");
    if (String(error?.code || "") === "needs_confirm" || message.includes("请确认后再提交")) {
      if (window.confirm(`${message}\n\n确认继续提交吗？`)) {
        stageData.busy = false;
        await generateStage({ confirmLarge: true });
        return;
      }
    }
    fail("提交生成失败", error);
  } finally {
    stageData.busy = false;
    renderStagePanel();
  }
}

async function cancelStageTask() {
  const slot = activeTaskSlot();
  if (!slot.activeTaskId) return;
  try {
    await api(`/api/studio/tasks/${encodeURIComponent(slot.activeTaskId)}/cancel`, {
      method: "POST", body: {},
    });
    toast("已取消");
    await refreshStageTasks();
  } catch (error) {
    fail("取消失败", error);
  }
}

async function retryStageTask() {
  const slot = activeTaskSlot();
  if (!slot.activeTaskId) return;
  try {
    // 重发的是当时素材快照：改了章节再重试不会带新素材（与工作台其它任务同口径）。
    const { task } = await api(`/api/studio/tasks/${encodeURIComponent(slot.activeTaskId)}/retry`, {
      method: "POST", body: {},
    });
    slot.activeTaskId = task?.id || "";
    slot.streamText = "";
    slot.taskCursor = 0;
    slot.progressNote = "";
    if (slot.rows) {
      slot.rows = [];
      slot.parsed = false;
      slot.checked = new Set();
      slot.rowsForTaskId = "";
    }
    novel.collapsed.delete(`${novel.stage}:task`);
    await refreshStageTasks();
    toast("已重新排队");
  } catch (error) {
    fail("重试失败", error);
  }
}

async function applyStageResult() {
  const stage = novel.stage;
  if (stage === "storyboard") {
    toast("分镜提示词不写回作品；勾选后发送到造梦机出图");
    return;
  }
  const stageData = stageState(stage);
  if (!stageData.activeTaskId || !novel.bookId) return;
  const task = novel.tasks.find((row) => row.id === stageData.activeTaskId);
  const text = String(stageData.streamText || task?.result || task?.result_partial || "");
  if (!text.trim()) {
    toast("这个任务没有结果可写回");
    return;
  }
  const label = stageLabel(stage);
  const destination = stage === "script" ? "场次列表" : "作品资料";
  if (!window.confirm(`把这次的${label}保存为${destination}的新版本吗？已有版本会保留。`)) return;
  try {
    const payload = await api(`/api/novel/books/${encodeURIComponent(novel.bookId)}/apply`, {
      method: "POST",
      body: {
        stage,
        task_id: stageData.activeTaskId,
        text,
        revision: novel.book?.revision,
      },
    });
    novel.book = payload.book;
    stageData.activeTaskId = "";
    stageData.streamText = "";
    stageData.taskCursor = 0;
    novel.sceneEditing = 0;
    // 刚保存的新版本要让用户看见：如果之前收起了「已保存版本」，这里重新展开。
    novel.collapsed.delete(`${stage}:saved`);
    setSaveState("已保存", `${label}已写回作品。`);
    renderAll();
    toast(`${label}已保存到作品`);
  } catch (error) {
    // 409 = 任务/阶段不符或作品已被改过；如实提示，不覆盖。
    fail(`写回${label}失败`, error);
  }
}

// ---- 单章 AI 改编 ----

function openAiEditor(manuscriptId) {
  novel.aiTargetId = String(manuscriptId || "");
  novel.editingManuscriptId = "";
  renderEditor();
}

async function submitAiAdapt() {
  if (!novel.bookId || !novel.chapter || !novel.aiTargetId) return;
  const slot = novel.adaptTask;
  const task = novel.tasks.find((row) => row.id === slot.activeTaskId);
  const ready = String(task?.status || "") === "done"
    && String(slot.streamText || task?.result || "").trim();
  const model = readAiModel();
  if (ready) {
    await saveAiResult();
    return;
  }
  const request = String($("#novelAiRequest")?.value || "").trim();
  if (!model.model_name) {
    toast("先在设置里检查模型，再回来选择");
    return;
  }
  slot.busy = true;
  slot.warnings = [];
  renderAiEditor();
  try {
    const payload = await api("/api/novel/stage", {
      method: "POST",
      body: {
        book_id: novel.bookId,
        stage: "adapt",
        chapter_ids: [novel.chapter.i],
        manuscripts: manuscriptsPayload(),
        body_chars: novel.bodyChars,
        max_chapters: novel.maxChapters,
        extra_note: request,
        model_key: model.model_key,
        model_name: model.model_name,
      },
    });
    slot.activeTaskId = payload.task?.id || "";
    slot.streamText = "";
    slot.taskCursor = 0;
    slot.warnings = Array.isArray(payload.warnings) ? payload.warnings : [];
    const kgCount = Number(payload.sources?.kg_entities || 0);
    if (kgCount > 0) {
      slot.warnings.push(`已加入已保存要素里的 ${kgCount} 条设定参考，帮助人物与设定保持一致。`);
    }
    slot.submittedAt = Date.now();
    slot.progressNote = "";
    if (novel.book && Number.isInteger(Number(payload.book_revision))) {
      novel.book.revision = Number(payload.book_revision);
    }
    if (payload.task) novel.tasks.unshift(payload.task);
    renderAiEditor();
    scheduleTaskPolling();
    toast("已提交改编任务，结果会显示在下方");
  } catch (error) {
    fail("提交改编失败", error);
  } finally {
    slot.busy = false;
    renderAiEditor();
  }
}

async function saveAiResult() {
  const slot = novel.adaptTask;
  if (!slot.activeTaskId || !novel.bookId || !novel.chapter) return;
  const task = novel.tasks.find((row) => row.id === slot.activeTaskId);
  const text = String(slot.streamText || task?.result || task?.result_partial || "");
  if (!text.trim()) {
    toast("这个任务没有结果可保存");
    return;
  }
  try {
    const payload = await api(
      `/api/novel/books/${encodeURIComponent(novel.bookId)}/chapters/${novel.chapter.i}/manuscripts`,
      {
        method: "POST",
        body: {
          kind: "ai",
          content: text,
          source_manuscript_id: novel.aiTargetId,
          task_id: slot.activeTaskId,
          user_request: String($("#novelAiRequest")?.value || ""),
          revision: novel.chapter.revision,
        },
      }
    );
    const manuscript = payload.manuscript;
    novel.manuscriptBodies.set(manuscript.id, String(manuscript.content || ""));
    slot.activeTaskId = "";
    slot.streamText = "";
    novel.aiTargetId = "";
    setSaveState("已保存", `${manuscript.label} 已保存到作品。`);
    await openChapter(novel.chapterNo);
    toast(payload.duplicate ? "这份内容已经保存过，没有重复建卡" : `${manuscript.label} 已保存`);
  } catch (error) {
    fail("保存改编稿失败", error);
  }
}

// ============================================================
// 管理记录
// ============================================================

function manageTabRows() {
  const records = novel.manage.records;
  if (!records) return [];
  if (novel.manage.tab === "manuscript") return records.manuscripts || [];
  if (novel.manage.tab === "elements") return records.elements || [];
  if (novel.manage.tab === "outline") return records.outlines || [];
  if (novel.manage.tab === "script") return records.scripts || [];
  // 生成记录：创作场这一本作品的任务。
  return novel.tasks
    .filter((row) => row.module === "novel" && row.novel?.book_id === novel.bookId)
    .sort((a, b) => Number(b.created_at || 0) - Number(a.created_at || 0));
}

function manageRowLabel(row) {
  if (row.type === "task") return String(row.title || "生成任务");
  if (row.kind === "manual" || row.kind === "ai" || row.kind === "original" || row.kind === "legacy") {
    return manuscriptLabel(row);
  }
  if (row.kind === "elements") return `要素 第 ${row.version} 版`;
  if (row.kind === "outline") return `大纲 第 ${row.version} 版`;
  if (row.kind === "script") return `剧本 第 ${row.version} 版`;
  return "记录";
}

// 生成记录的任务来源阶段（row.novel.stage）；老任务没有 novel 字段时回空串。
function taskStageLabel(row) {
  const stage = String(row.novel?.stage || "");
  if (stage === "adapt") return "单章改编";
  if (stage === "split") return "拆章";
  return String(STAGE_COPY[stage]?.title || "");
}

function manageRowMeta(row) {
  if (row.type === "task") {
    const bits = [TASK_STATUS_LABELS[row.status] || "状态未知", fmtTime(row.updated_at || row.created_at)];
    // 素材章节让同阶段任务也能互相区分（如「第 3、4 章」）。
    const chapters = Array.isArray(row.novel?.chapter_ids) ? row.novel.chapter_ids : [];
    if (chapters.length) bits.push(`第 ${chapters.join("、")} 章`);
    const stage = taskStageLabel(row);
    if (stage) bits.push(stage);
    const saved = (novel.manage.records?.elements || []).some((item) => item.task_id === row.id)
      || (novel.manage.records?.outlines || []).some((item) => item.task_id === row.id)
      || (novel.manage.records?.scripts || []).some((item) => item.task_id === row.id)
      || (novel.manage.records?.manuscripts || []).some((item) => item.task_id === row.id);
    bits.push(saved ? "已保存为作品内容" : "未保存到作品");
    return bits.join(" · ");
  }
  const bits = [];
  if (row.chapter_no) bits.push(`第 ${row.chapter_no} 章`);
  if (row.kind) bits.push(MANUSCRIPT_KIND_LABELS[row.kind] || row.kind);
  if (row.len) bits.push(`${fmtChars(row.len)} 字`);
  if (row.scene_count !== undefined) bits.push(`${row.scene_count} 场`);
  if (row.updated_at) bits.push(fmtTime(row.updated_at));
  else if (row.created_at) bits.push(fmtTime(row.created_at));
  if (row.manuscripts?.length) {
    bits.push(`来源：第 ${row.manuscripts.map((item) => item.chapter_no).join("、")} 章选定稿件`);
  } else if (row.chapter_ids?.length) {
    bits.push(`来源：第 ${row.chapter_ids.join("、")} 章`);
  }
  return bits.filter(Boolean).join(" · ");
}

function manageRowKey(row) {
  if (row.type === "task") return `task:${row.id}`;
  if (row.chapter_no) return `manuscript:${row.chapter_no}:${row.id}`;
  return `${row.kind}:${row.id}`;
}

// 生成记录的内容预览：结果开头一小段，让「这条任务是出了什么」一眼可辨。
// 长结果只截开头（完整内容在对应阶段的「查看」里）；没结果的任务不显示这行。
function taskPreviewText(row) {
  const text = String(row.result || row.result_partial || "").trim();
  if (!text) return "";
  const line = text.split("\n").find((part) => part.trim()) || "";
  return line.length > 80 ? `${line.slice(0, 80)}…` : line;
}

function manageRowSelectable(row) {
  if (row.type === "task") {
    return !["queued", "running"].includes(String(row.status || ""));
  }
  return Boolean(row.id);
}

function filteredManageRows() {
  const keyword = novel.manage.filter.trim().toLowerCase();
  const rows = manageTabRows().map((row) => (novel.manage.tab === "task"
    ? { ...row, type: "task" }
    : { ...row, type: row.type || novel.manage.tab }));
  if (!keyword) return rows;
  return rows.filter((row) => {
    const haystack = [
      manageRowLabel(row), manageRowMeta(row), row.chapter_title, row.title,
    ].map((value) => String(value || "").toLowerCase()).join(" ");
    return haystack.includes(keyword);
  });
}

function renderManage() {
  const list = $("#novelManageList");
  if (!list) return;
  const scope = $("#novelManageScope");
  if (scope) {
    scope.textContent = novel.bookId ? `《${novel.book?.title || "作品"}》` : "先打开作品";
  }
  const note = $("#novelManageNote");
  if (note) {
    note.textContent = novel.bookId
      ? "只管理当前作品。点一行可以就地查看内容；勾选框用于批量删除。删除改编稿不会删除原文；删除生成记录不会删除已保存的内容。全选只作用于当前筛选范围。"
      : "还没有打开作品。先关闭这里，在左上角打开或保存一个作品。";
  }
  document.querySelectorAll("#novelManageTabs [data-novel-manage-tab]").forEach((btn) => {
    const active = btn.dataset.novelManageTab === novel.manage.tab;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
  const rows = filteredManageRows();
  if (!novel.bookId) {
    list.replaceChildren(Object.assign(document.createElement("p"), {
      className: "novel-empty", textContent: "还没有打开作品。",
    }));
  } else if (novel.manage.loading) {
    list.replaceChildren(Object.assign(document.createElement("p"), {
      className: "novel-empty", textContent: "正在读取记录…",
    }));
  } else if (!rows.length) {
    list.replaceChildren(Object.assign(document.createElement("p"), {
      className: "novel-empty", textContent: "这个分类下还没有记录。",
    }));
  } else {
    list.replaceChildren(...rows.map((row) => {
      const wrap = document.createElement("div");
      wrap.className = "novel-manage-row";
      const key = manageRowKey(row);
      const selectable = manageRowSelectable(row);
      const box = document.createElement("input");
      box.type = "checkbox";
      box.dataset.novelManageRow = key;
      box.checked = selectable && novel.manage.selected.has(key);
      box.disabled = !selectable;
      box.setAttribute("aria-label", `选择 ${manageRowLabel(row)}`);
      // 勾选框只管批量删除；点这一行本身就地查看内容。
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "novel-manage-open";
      copy.dataset.novelManageOpen = key;
      copy.title = "查看这条记录的内容";
      const strong = document.createElement("strong");
      strong.textContent = manageRowLabel(row);
      const small = document.createElement("small");
      small.textContent = manageRowMeta(row);
      copy.append(strong, small);
      // 生成记录带结果开头一行做预览；作品内容版本行 small 里已经有来源，不重复。
      if (row.type === "task") {
        const preview = taskPreviewText(row);
        if (preview) {
          const snippet = document.createElement("small");
          snippet.className = "novel-manage-preview";
          snippet.textContent = preview;
          copy.append(snippet);
        }
      }
      if (!selectable) {
        const tag = document.createElement("em");
        tag.className = "novel-manage-locked";
        tag.textContent = "执行中";
        copy.append(tag);
      }
      wrap.append(box, copy);
      return wrap;
    }));
  }
  const selection = $("#novelManageSelection");
  const count = novel.manage.selected.size;
  if (selection) {
    const total = rows.filter((row) => manageRowSelectable(row)).length;
    selection.textContent = count
      ? `已选 ${count} 条（当前筛选范围内共 ${total} 条可删除）`
      : `当前筛选范围内 ${total} 条可删除。`;
  }
  const del = $("#novelManageDelete");
  if (del) del.disabled = count === 0 || !novel.bookId;
}

// ---- 管理记录：点一行就地看内容 ----
// 记录清单按设计不带正文（`list_records` 只给索引），所以按类型各走各的读法：
// 稿件读单稿正文、要素/大纲/剧本读作品里对应版本、生成记录读任务结果。
function manageVersionText(row) {
  const versions = row.kind === "elements" ? novel.book?.elements_versions
    : row.kind === "outline" ? novel.book?.outline_versions
      : row.kind === "script" ? novel.book?.script_versions
        : null;
  const rows = Array.isArray(versions) ? versions : [];
  const hit = rows.find((item) => String(item.id || "") === String(row.id || ""))
    || rows.find((item) => Number(item.version) === Number(row.version));
  if (!hit) return "";
  if (row.kind === "elements") return String(hit.raw || "");
  if (row.kind === "outline") return String(hit.text || "");
  const scenes = Array.isArray(hit.script) ? hit.script : [];
  return scenes
    .map((scene, index) => `${index + 1}. ${String(scene.scene || "（没有场景行）")}\n${String(scene.content || "")}`)
    .join("\n\n");
}

async function openManageRecord(row) {
  const box = $("#novelManagePreview");
  const body = $("#novelManagePreviewBody");
  if (!box || !body || !row) return;
  const head = $("#novelManagePreviewHead");
  const meta = $("#novelManagePreviewMeta");
  if (head) head.textContent = manageRowLabel(row);
  if (meta) meta.textContent = manageRowMeta(row);
  box.hidden = false;
  body.textContent = "正在读取…";
  let text = "";
  if (row.type === "task") {
    text = String(row.result || row.result_partial || "").trim();
    if (!text) {
      const status = String(row.status || "");
      text = status === "queued" || status === "running"
        ? "任务还在执行，完成后这里会显示结果。"
        : "这条任务没有留下结果正文。";
    }
  } else if (row.chapter_no) {
    text = await ensureManuscriptBody(row);
  } else {
    text = manageVersionText(row);
  }
  body.textContent = String(text || "").trim() || "这条记录没有可显示的内容。";
}

async function loadManageRecords() {  if (!novel.bookId) {
    novel.manage.records = null;
    renderManage();
    return;
  }
  const token = ++novel.manage.token;
  novel.manage.loading = true;
  renderManage();
  try {
    const payload = await api(`/api/novel/books/${encodeURIComponent(novel.bookId)}/records`);
    if (token !== novel.manage.token) return;
    novel.manage.records = payload;
  } catch (error) {
    if (token !== novel.manage.token) return;
    novel.manage.records = null;
    const errorEl = $("#novelManageError");
    if (errorEl) errorEl.textContent = String(error?.message || error);
  } finally {
    if (token === novel.manage.token) {
      novel.manage.loading = false;
      renderManage();
    }
  }
}

function openManage(tab = "") {
  if (tab) novel.manage.tab = tab;
  novel.manage.selected.clear();
  const dialog = $("#novelManageDialog");
  if (!dialog) return;
  novel.manage.open = true;
  const errorEl = $("#novelManageError");
  if (errorEl) errorEl.textContent = "";
  const preview = $("#novelManagePreview");
  if (preview) preview.hidden = true;
  renderManage();
  // 记录清单与任务列表各拉各的：任一方失败都不该把另一方拖成空面板。
  void loadManageRecords();
  void refreshStageTasks().then(() => renderManage());
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeManage() {
  const dialog = $("#novelManageDialog");
  novel.manage.open = false;
  if (!dialog) return;
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

async function deleteManageSelection() {
  const keys = [...novel.manage.selected];
  if (!keys.length || !novel.bookId) return;
  const items = [];
  const taskIds = [];
  for (const key of keys) {
    const [type, ...rest] = key.split(":");
    if (type === "task") {
      taskIds.push(rest.join(":"));
    } else if (type === "manuscript") {
      const [chapterNo, manuscriptId] = rest;
      items.push({ type: "manuscript", chapter_no: Number(chapterNo), id: manuscriptId });
    } else {
      items.push({ type, id: rest.join(":") });
    }
  }
  const parts = [];
  if (items.length) parts.push(`${items.length} 条作品内容`);
  if (taskIds.length) parts.push(`${taskIds.length} 条生成记录`);
  const confirmText = [
    `要删除${parts.join("和")}吗？这个操作不能撤销。`,
    items.length ? "删除改编稿不会删除原文或已保存的要素、大纲、剧本。" : "",
    taskIds.length ? "删除生成记录只清任务记录，不删除从它保存的内容。" : "",
    items.length && items.some((row) => row.type === "manuscript")
      ? "如果删掉某章最后一份稿件，这一章会从作品里移除。" : "",
  ].filter(Boolean).join("\n");
  if (!window.confirm(confirmText)) return;
  const errorEl = $("#novelManageError");
  if (errorEl) errorEl.textContent = "";
  try {
    if (items.length) {
      const payload = await api("/api/novel/records/delete", {
        method: "POST",
        body: { book_id: novel.bookId, items, revision: novel.book?.revision },
      });
      novel.book = payload.book;
    }
    if (taskIds.length) {
      await api("/api/novel/tasks/delete", {
        method: "POST",
        body: { book_id: novel.bookId, task_ids: taskIds },
      });
    }
    novel.manage.selected.clear();
    await loadManageRecords();
    await refreshStageTasks();
    // 记录删了，当前章与选择器都要跟着更新，不能留着指向已删稿件的选择。
    for (const [chapterNo, manuscriptId] of [...novel.manuscriptChoice.entries()]) {
      if (manuscriptId && !(novel.manage.records?.manuscripts || []).some((row) => row.id === manuscriptId)) {
        novel.manuscriptChoice.delete(chapterNo);
        toast(`第 ${chapterNo} 章选用的稿件已被删除，已改回原文；请确认后再生成`);
      }
    }
    novel.expanded.clear();
    novel.expandedTouched.clear();
    novel.manuscriptBodies.clear();
    if (novel.chapterNo) await openChapter(novel.chapterNo);
    else renderAll();
    toast("已删除选中的记录");
  } catch (error) {
    if (errorEl) errorEl.textContent = String(error?.message || error);
    fail("删除记录失败", error);
  }
}

// ============================================================
// 数据操作
// ============================================================

function setStage(stage) {
  const next = ["split", "elements", "outline", "script", "storyboard"].includes(stage) ? stage : "split";
  novel.stage = next;
  stageState(next).warnings = [];
  novel.stageWarnings = [];
  novel.budget = null;
  document.querySelectorAll("#novelStages [data-novel-stage]").forEach((btn) => {
    const active = btn.dataset.novelStage === next;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
  if (next !== "split") {
    // 进阶段页：接上这个作品当前阶段的任务，避免「切回来什么也看不到」。
    void refreshStageTasks().then(() => refreshPreviewBudget());
    void fillStageModels();
  } else {
    stopTaskPolling();
  }
  renderAll();
}

function renderAll() {
  renderBookSummary();
  renderChapters();
  renderImportBlock();
  renderEditor();
  refreshPreview();
  const exportBtn = $("#novelExportBtn");
  if (exportBtn) exportBtn.disabled = !novel.bookId || novel.busy;
  const importBtn = $("#novelImportBtn");
  if (importBtn) importBtn.disabled = novel.busy;
  const manageBtn = $("#novelManageBtn");
  if (manageBtn) manageBtn.disabled = false;
}

async function loadBooks() {
  try {
    const payload = await api("/api/novel/books");
    return payload?.books || [];
  } catch (error) {
    fail("读取作品列表失败", error);
    return [];
  }
}

async function loadLocation() {
  try {
    novel.location = await api("/api/novel/location");
  } catch (_error) {
    novel.location = null;
  }
  renderLocation();
}

/** 只重读章节目录（标题、稿数），不动当前打开章节与稿件缓存。 */
async function refreshChapterIndex() {
  if (!novel.bookId) return;
  try {
    const payload = await api(`/api/novel/books/${encodeURIComponent(novel.bookId)}`);
    novel.chapters = payload.chapters || [];
    novel.broken = payload.broken || [];
    if (payload.book) novel.book = payload.book;
  } catch (error) {
    fail("刷新章节目录失败", error);
    return;
  }
  renderChapters();
  renderBookSummary();
}

async function openBook(bookId) {
  const token = ++novel.bookToken;
  novel.chapterToken += 1;
  novel.taskToken += 1;
  stopTaskPolling();
  try {
    const payload = await api(`/api/novel/books/${encodeURIComponent(bookId)}`);
    if (token !== novel.bookToken) return;
    novel.bookId = bookId;
    novel.selectedVersions = { elements: 0, outline: 0, script: 0 };
    try { localStorage.setItem(LAST_BOOK_KEY, bookId); } catch (_error) { /* storage may be unavailable */ }
    novel.book = payload.book;
    novel.chapters = payload.chapters || [];
    novel.broken = payload.broken || [];
    novel.staged = null;
    novel.picked.clear();
    novel.chapter = null;
    novel.chapterNo = 0;
    novel.manuscriptBodies.clear();
    novel.expanded.clear();
    novel.expandedTouched.clear();
    novel.manuscriptChoice.clear();
    novel.chapterRosters.clear();
    novel.chapterRostersInflight.clear();
    novel.editingManuscriptId = "";
    novel.aiTargetId = "";
    for (const data of Object.values(novel.stageStates)) {
      data.activeTaskId = "";
      data.streamText = "";
      data.taskCursor = 0;
      data.warnings = [];
      data.busy = false;
      data.submittedAt = 0;
      data.firstDeltaAt = 0;
      data.recentDeltas = [];
      data.progressNote = "";
      if (data.rows) {
        data.rows = [];
        data.parsed = false;
        data.checked = new Set();
        data.rowsForTaskId = "";
      }
    }
    novel.adaptTask = {
      activeTaskId: "", taskCursor: 0, streamText: "", warnings: [],
      busy: false, submittedAt: 0, progressNote: "",
    };
    const recovery = novel.book?.origin_recovery || {};
    const legacy = Array.isArray(recovery.legacy) ? recovery.legacy : [];
    setSaveState("已读取", `作品《${novel.book.title}》共 ${novel.chapters.length} 章。` +
      (novel.broken.length ? `有 ${novel.broken.length} 章读取失败（数据异常），需要重新导入或删除。` : "") +
      (legacy.length ? `其中 ${legacy.length} 章的原文在升级前没有单独保存，已标为「升级前保存的正文」。` : ""));
    renderAll();
    if (novel.chapters.length) await openChapter(novel.chapters[0].i);
    if (novel.stage !== "split") void refreshStageTasks().then(() => refreshPreviewBudget());
  } catch (error) {
    if (token !== novel.bookToken) return;
    fail("打开作品失败", error);
    setSaveState("读取失败", String(error?.message || error));
  }
}

async function openChapter(chapterNo) {
  if (!novel.bookId) return;
  if (novel.editingManuscriptId && novel.editDraft !== manuscriptBody(
    manuscriptRows().find((row) => row.id === novel.editingManuscriptId)
  )) {
    if (!window.confirm("这一章有还没保存的修改，切换后修改会丢失。要继续吗？")) return;
  }
  const token = ++novel.chapterToken;
  try {
    const payload = await api(
      `/api/novel/books/${encodeURIComponent(novel.bookId)}/chapters/${chapterNo}`
    );
    if (token !== novel.chapterToken) return;
    novel.chapterNo = chapterNo;
    novel.chapter = payload;
    // 基础稿件的正文随单章接口回来，直接写进缓存。
    if (payload.content_manuscript_id) {
      novel.manuscriptBodies.set(payload.content_manuscript_id, String(payload.content || ""));
    }
    // 打开过的章顺手记下稿件清单：切到要素阶段时逐章下拉能直接列出该章的改编稿。
    novel.chapterRosters.set(chapterNo, Array.isArray(payload.manuscripts) ? payload.manuscripts : []);
    // 首次打开一章：默认展开基础稿件（原文），让正文一眼可见。
    // 用户手动收过一次之后就不再自动撑开，避免「关不回去」。
    const baseId = String(payload.base_manuscript_id || payload.content_manuscript_id || "");
    if (baseId && !novel.expandedTouched.has(chapterNo)) {
      novel.expanded.add(baseId);
    }
    novel.editingManuscriptId = "";
    novel.editDraft = "";
    novel.aiTargetId = "";
    setSaveState("已读取", `已打开第 ${chapterNo} 章（${payload.len} 字，${(payload.manuscripts || []).length} 份稿件）。`);
    renderAll();
    if (novel.pane === "chapters") setPane("editor");
    if (novel.stage !== "split") void refreshPreviewBudget();
  } catch (error) {
    if (token !== novel.chapterToken) return;
    fail("读取章节失败", error);
  }
}

async function importFile(file) {
  if (!file) return;
  if (file.size > MAX_FILE_BYTES) {
    toast(`文件太大（${(file.size / 1024 / 1024).toFixed(1)} MB），上限 60 MB`);
    return;
  }
  setBusy(true);
  setSaveState("正在导入", `正在拆分《${file.name}》，请稍候。`);
  try {
    const form = new FormData();
    form.append("file", file);
    // 文件名单独给一份：multipart 头里的 filename 在服务端要按 latin-1 还原，
    // 这里显式传 UTF-8 字段更稳（中文书名不会变成问号）。
    form.append("filename", file.name);
    const payload = await api("/api/novel/import", { method: "POST", body: form });
    novel.staged = payload;
    novel.picked = new Set((payload.chapters || []).map((row) => row.i));
    novel.bookId = "";
    novel.book = null;
    novel.chapters = [];
    novel.broken = [];
    novel.chapter = null;
    novel.chapterNo = 0;
    novel.manuscriptChoice.clear();
    novel.chapterRosters.clear();
    novel.chapterRostersInflight.clear();
    setSaveState("待保存", `拆出 ${payload.total} 章。勾选要保留的章节，再点「保存为作品」。`);
    renderAll();
  } catch (error) {
    fail("导入失败", error);
    setSaveState("导入失败", String(error?.message || error));
  } finally {
    setBusy(false);
    renderAll();
  }
}

async function saveBook() {
  if (!novel.staged) return;
  const picked = [...novel.picked].sort((a, b) => a - b);
  if (!picked.length) {
    toast("至少选择一章");
    return;
  }
  setBusy(true);
  setSaveState("正在保存", `正在保存 ${picked.length} 章…`);
  try {
    const payload = await api("/api/novel/books", {
      method: "POST",
      body: { import_id: novel.staged.import_id, picked, title: novel.staged.name.replace(/\.[^.]+$/, "") },
    });
    novel.staged = null;
    novel.picked.clear();
    await openBook(payload.book.id);
    toast(`已保存作品《${payload.book.title}》，共 ${payload.book.chapter_count} 章`);
  } catch (error) {
    fail("保存作品失败", error);
    setSaveState("保存失败", String(error?.message || error));
  } finally {
    setBusy(false);
    renderAll();
  }
}

/** 保存编辑结果：新增一份改编稿，原文与来源稿件逐字保留。 */
async function saveManuscriptEdit() {
  if (!novel.bookId || !novel.chapter || !novel.editingManuscriptId) return;
  const content = String($("#novelChapterBody")?.value ?? novel.editDraft ?? "");
  if (!content.trim()) {
    toast("正文不能为空");
    return;
  }
  const titleInput = $("#novelChapterTitle");
  const title = String(titleInput?.value || "").trim();
  setBusy(true);
  setSaveState("正在保存", `正在保存第 ${novel.chapter.i} 章的改编稿…`);
  try {
    const payload = await api(
      `/api/novel/books/${encodeURIComponent(novel.bookId)}/chapters/${novel.chapter.i}`,
      {
        method: "PATCH",
        body: {
          title,
          content,
          revision: novel.chapter.revision,
          base_manuscript_id: novel.editingManuscriptId,
        },
      }
    );
    const chapter = payload.chapter;
    const chapterNo = novel.chapter.i;
    // 索引里的标题/字数要跟着更新，否则左侧列表还是旧值。
    const row = novel.chapters.find((item) => item.i === chapterNo);
    if (row) {
      row.title = chapter.title;
      row.len = chapter.len;
      row.manuscript_count = (chapter.manuscripts || []).length;
      row.adapted_count = (chapter.manuscripts || []).filter((item) => item.kind === "manual" || item.kind === "ai").length;
    }
    if (novel.book) novel.book.revision = (novel.book.revision || 0) + 1;
    const created = chapter.created_manuscript;
    const savedNote = created
      ? `${created.label} 已保存到作品（${created.len} 字）。原文与其它稿件都没有改动。`
      : "内容没有改动，没有新增稿件卡片。";
    novel.editingManuscriptId = "";
    novel.editDraft = "";
    // 重新读一次章节（稿件清单与正文以服务端为准），再把「已保存」写回去：
    // openChapter 会把状态改成「已读取」，不能让它盖掉保存结果。
    await openChapter(chapterNo);
    // 左栏的章节索引单独来自作品详情：标题改了要重读一次，否则列表还显示旧标题。
    await refreshChapterIndex();
    setSaveState("已保存", savedNote);
    toast(created ? `${created.label} 已保存` : "内容没有改动，没有新增稿件");
  } catch (error) {
    // 409 = 别处已经改过（乐观并发），提示重新读取，不覆盖别人的写回。
    fail("保存改编稿失败", error);
    setSaveState("保存失败", String(error?.message || error));
  } finally {
    setBusy(false);
    renderAll();
  }
}

async function deleteChapter() {
  if (!novel.bookId || !novel.chapter) return;
  const no = novel.chapter.i;
  const count = manuscriptRows().length;
  if (!window.confirm(
    `要从作品里移除第 ${no} 章吗？这一章的 ${count} 份稿件会一起删除，不能撤销。`
  )) return;
  setBusy(true);
  try {
    await api(`/api/novel/books/${encodeURIComponent(novel.bookId)}/chapters/${no}`, { method: "DELETE" });
    novel.chapter = null;
    novel.chapterNo = 0;
    novel.manuscriptChoice.delete(no);
    await openBook(novel.bookId);
    toast(`已移除第 ${no} 章`);
  } catch (error) {
    fail("删除章节失败", error);
  } finally {
    setBusy(false);
    renderAll();
  }
}

async function deleteBook(bookId, title) {
  if (!window.confirm(`要删除作品《${title}》吗？整本作品的章节与稿件都会删除，不能撤销。`)) return;
  try {
    await api(`/api/novel/books/${encodeURIComponent(bookId)}`, { method: "DELETE" });
    if (novel.bookId === bookId) {
      novel.bookId = "";
      try { localStorage.removeItem(LAST_BOOK_KEY); } catch (_error) { /* storage may be unavailable */ }
      novel.book = null;
      novel.chapters = [];
      novel.broken = [];
      novel.chapter = null;
      novel.chapterNo = 0;
      novel.manuscriptBodies.clear();
      novel.expanded.clear();
      novel.expandedTouched.clear();
      novel.manuscriptChoice.clear();
      novel.chapterRosters.clear();
      novel.chapterRostersInflight.clear();
      setSaveState("—", "作品已删除。");
      renderAll();
    }
    toast("作品已删除");
    void renderBookDialog();
  } catch (error) {
    fail("删除作品失败", error);
  }
}

/**
 * 导出全书。
 * `fmt` = txt | md；`withExtras` 为真时附要素与大纲附录（阶段三）。
 * 导出前显示格式与文件名；整本拼装在服务端完成，前端只落 Blob。
 */
/**
 * 导出全书。
 * `fmt` = txt | md；`withExtras` 为真时附要素与大纲附录（阶段三）。
 *
 * 导出前要显示格式、文件名与包含范围：这些在导出菜单里就先写清楚
 * （`renderExportNote`），点菜单项即按显示的范围下载，不再弹第二次确认。
 */
async function exportBook(fmt = "txt", withExtras = false) {
  if (!novel.bookId) return;
  const name = `${(novel.book?.title || "作品").replace(/[\\/:*?"<>|]/g, "_")}.${fmt}`;
  try {
    const query = new URLSearchParams({ format: fmt });
    if (withExtras) query.set("with_extras", "1");
    // 逐章稿件选择沿用当前选择，导出范围与界面显示一致。
    const wanted = manuscriptsPayload();
    if (Object.keys(wanted).length) query.set("manuscripts", JSON.stringify(wanted));
    const payload = await api(
      `/api/novel/books/${encodeURIComponent(novel.bookId)}/export?${query.toString()}`
    );
    const filename = String(payload?.filename || name);
    const mime = fmt === "md" ? "text/markdown;charset=utf-8" : "text/plain;charset=utf-8";
    // triggerDownload 只接受 URL；这里要落的是内存里的文本，直接造一个临时 blob 链接。
    const url = URL.createObjectURL(new Blob([payload.text || ""], { type: mime }));
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.rel = "noopener";
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    setSaveState("已导出",
      `已交给浏览器下载「${filename}」（${fmt.toUpperCase()}${withExtras ? "，含要素与大纲" : "，全书"}）。`
      + "可以在浏览器的下载列表里查看；若系统弹出保存位置，按你选的位置保存。");
  } catch (error) {
    fail("导出失败", error);
  }
}

/** 导出菜单里的范围说明：格式、文件名、包含哪些章。 */
function renderExportNote() {
  const note = $("#novelExportNote");
  if (!note) return;
  const title = (novel.book?.title || "作品").replace(/[\\/:*?"<>|]/g, "_");
  const count = novel.chapters.length;
  note.textContent = count
    ? `全书 ${count} 章。文件名「${title}.txt」或「${title}.md」；`
      + "下载交给浏览器处理，通常保存到浏览器默认下载目录。"
    : "导出由浏览器发起下载，通常保存到浏览器默认下载目的地。";
}

// ============================================================
// 作品列表弹窗
// ============================================================

async function renderBookDialog() {
  const list = $("#novelBookList");
  const errorEl = $("#novelBooksError");
  if (!list) return;
  if (errorEl) errorEl.textContent = "";
  list.replaceChildren(...[Object.assign(document.createElement("p"), {
    className: "novel-empty",
    textContent: "正在读取…",
  })]);
  const token = ++novel.booksToken;
  try {
    const books = await loadBooks();
    if (token !== novel.booksToken) return;
    if (!books.length) {
      list.replaceChildren(Object.assign(document.createElement("p"), {
        className: "novel-empty",
        textContent: "还没有保存过作品。先导入一本 TXT 并保存。",
      }));
      return;
    }
    list.replaceChildren(...books.map((book) => {
      const row = document.createElement("div");
      row.className = "novel-book-row";
      row.classList.toggle("active", book.id === novel.bookId);
      const open = document.createElement("button");
      open.type = "button";
      open.className = "novel-book-open";
      open.dataset.novelOpen = book.id;
      const strong = document.createElement("strong");
      strong.textContent = `《${book.title}》`;
      const small = document.createElement("small");
      small.textContent = `${book.chapter_count} 章 · ${fmtChars(book.source?.total_len || 0)} 字`;
      open.append(strong, small);
      const del = document.createElement("button");
      del.type = "button";
      del.className = "control-button tiny danger";
      del.dataset.novelDeleteBook = book.id;
      del.dataset.novelDeleteTitle = book.title;
      del.textContent = "删除";
      row.append(open, del);
      return row;
    }));
  } catch (error) {
    if (token !== novel.booksToken) return;
    if (errorEl) errorEl.textContent = String(error?.message || error);
    list.replaceChildren();
  }
}

// 手机竖屏断点：与 styles.css 的 `@media (max-width: 760px)` 一致。
const PORTRAIT_QUERY = "(max-width: 760px)";

function isPaneLayout() {
  return typeof window.matchMedia === "function" && window.matchMedia(PORTRAIT_QUERY).matches;
}

function setPane(pane) {
  const next = ["chapters", "editor", "preview"].includes(pane) ? pane : "editor";
  novel.pane = next;
  const columns = $("#novelColumns");
  if (columns) columns.dataset.novelPane = next;
  document.querySelectorAll("#studioPageNovel [data-novel-pane]").forEach((btn) => {
    if (!btn.classList.contains("novel-pane-btn")) return;
    const active = btn.dataset.novelPane === next;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
  applyPaneAccessibility();
}

/**
 * 隐藏的面板不可被键盘或辅助技术访问。
 * 只在竖屏分档布局下生效——桌面/横屏三栏全部可见，给其中一栏设 inert 会让它彻底点不动。
 */
function applyPaneAccessibility() {
  const paneLayout = isPaneLayout();
  const map = { chapters: ".novel-chapters", editor: ".novel-editor", preview: ".novel-preview" };
  for (const [key, selector] of Object.entries(map)) {
    const el = document.querySelector(`#studioPageNovel ${selector}`);
    if (!el) continue;
    const hidden = paneLayout && key !== novel.pane;
    el.setAttribute("aria-hidden", String(hidden));
    el.inert = hidden;
  }
}

// ---- 编辑态与草稿保护 ----

function markDirty(event) {
  if (!novel.editingManuscriptId) return;
  // 草稿保留在内存里，收起阅读区或切稿都不会丢；保存时以草稿为准。
  novel.editDraft = String(event?.target?.value ?? novel.editDraft ?? "");
  renderManuscriptEditor();
}

function beginEdit(manuscriptId) {
  const source = manuscriptRows().find((row) => row.id === manuscriptId);
  if (!source) return;
  novel.editingManuscriptId = String(manuscriptId);
  novel.aiTargetId = "";
  novel.editDraft = manuscriptBody(source);
  novel.expanded.add(String(manuscriptId));
  renderEditor();
  const body = $("#novelChapterBody");
  if (body) {
    body.value = novel.editDraft;
    body.focus();
  }
}

async function cancelEdit() {
  if (!novel.editingManuscriptId) return;
  const changed = novel.editDraft !== manuscriptBody(
    manuscriptRows().find((row) => row.id === novel.editingManuscriptId)
  );
  if (changed && !window.confirm("这次修改还没保存，取消后不会创建新稿件。要继续吗？")) return;
  novel.editingManuscriptId = "";
  novel.editDraft = "";
  renderEditor();
}

async function useForGeneration(manuscriptId) {
  if (!novel.chapterNo) return;
  novel.manuscriptChoice.set(novel.chapterNo, String(manuscriptId));
  renderEditor();
  const row = manuscriptRows().find((item) => item.id === manuscriptId);
  toast(`第 ${novel.chapterNo} 章的后续生成会使用「${row ? manuscriptLabel(row) : "这份稿件"}」`);
  if (novel.stage !== "split") void refreshPreviewBudget();
}

// ============================================================
// 接线
// ============================================================

function saveLimits() {
  try {
    localStorage.setItem(LIMITS_KEY, JSON.stringify({
      bodyChars: novel.bodyChars, maxChapters: novel.maxChapters,
    }));
  } catch (_error) { /* storage may be unavailable */ }
}

function restoreLimits() {
  try {
    const raw = localStorage.getItem(LIMITS_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    const bodyChars = Number(parsed?.bodyChars);
    const maxChapters = Number(parsed?.maxChapters);
    if (Number.isFinite(bodyChars)) {
      novel.bodyChars = Math.max(novel.limits.min,
        Math.min(novel.limits.max, Math.round(bodyChars / novel.limits.step) * novel.limits.step));
    }
    if (Number.isFinite(maxChapters)) {
      novel.maxChapters = Math.max(novel.limits.chaptersMin,
        Math.min(novel.limits.chaptersMax, Math.round(maxChapters)));
    }
  } catch (_error) { /* storage may be unavailable */ }
}

export function bindNovelEvents() {
  if (novel.initialized) return;
  prepareStagePanels();
  restoreLimits();
  novel.initialized = true;

  $("#novelImportBtn")?.addEventListener("click", () => $("#novelFileInput")?.click());
  $("#novelFileInput")?.addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file) void importFile(file);
  });
  $("#novelSaveBook")?.addEventListener("click", () => void saveBook());
  $("#novelCancelImport")?.addEventListener("click", () => {
    novel.staged = null;
    novel.picked.clear();
    setSaveState("—", "已放弃这次导入，没有保存任何内容。");
    renderAll();
  });
  $("#novelSaveChapter")?.addEventListener("click", () => void saveManuscriptEdit());
  $("#novelRevertChapter")?.addEventListener("click", () => void cancelEdit());
  $("#novelDeleteChapter")?.addEventListener("click", () => void deleteChapter());
  $("#novelAiCancel")?.addEventListener("click", () => {
    novel.aiTargetId = "";
    renderEditor();
  });
  $("#novelAiSubmit")?.addEventListener("click", () => void submitAiAdapt());
  $("#novelAiProvider")?.addEventListener("change", async (event) => {
    novel.aiProviderKey = String(event.target.value || "");
    novel.aiModelName = "";
    novel.aiModelName = await fillModelSelect($("#novelAiModel"), novel.aiProviderKey);
    renderAiEditor();
  });
  $("#novelAiModel")?.addEventListener("change", (event) => {
    novel.aiModelName = String(event.target.value || "");
    // 创作场换模型也跟随皮肤（与主对话同一条规则：只在自动模式下生效，
    // 识别不出家族时保持当前皮肤）。下同。
    void applyModelSkinFor(novel.aiModelName);
    renderAiEditor();
  });
  $("#novelAiRequest")?.addEventListener("input", () => renderAiEditor());

  // 稿件卡片：展开/收起、编辑、AI 润色、用于后续生成。
  $("#novelManuscripts")?.addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-novel-manuscript-toggle]");
    if (toggle) {
      toggleExpanded(toggle.dataset.novelManuscriptToggle);
      renderManuscripts();
      return;
    }
    const action = event.target.closest("[data-novel-manuscript-action]");
    if (!action) return;
    const id = action.dataset.novelManuscriptId;
    const kind = action.dataset.novelManuscriptAction;
    if (kind === "edit") beginEdit(id);
    else if (kind === "ai") openAiEditor(id);
    else if (kind === "use") void useForGeneration(id);
  });

  // 导出：点按钮开菜单，菜单项决定格式与是否附要素/大纲。
  $("#novelExportBtn")?.addEventListener("click", () => {
    const menu = $("#novelExportMenu");
    if (!menu) return;
    const open = menu.hidden;
    if (open) renderExportNote();
    menu.hidden = !open;
    $("#novelExportBtn")?.setAttribute("aria-expanded", String(open));
  });
  $("#novelExportMenu")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-novel-export]");
    if (!btn) return;
    closeExportMenu();
    const kind = btn.dataset.novelExport;
    if (kind === "md-extras") void exportBook("md", true);
    else void exportBook(kind === "md" ? "md" : "txt", false);
  });
  document.addEventListener("click", (event) => {
    const menu = $("#novelExportMenu");
    if (!menu || menu.hidden) return;
    if (event.target.closest("#novelExportMenu, #novelExportBtn")) return;
    closeExportMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const menu = $("#novelExportMenu");
    if (menu && !menu.hidden) {
      closeExportMenu();
      $("#novelExportBtn")?.focus();
    }
  });
  $("#novelBooksBtn")?.addEventListener("click", () => {
    const dialog = $("#novelBooksDialog");
    if (!dialog) return;
    void renderBookDialog();
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  });
  $("#novelBooksClose")?.addEventListener("click", () => {
    const dialog = $("#novelBooksDialog");
    if (!dialog) return;
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  });

  // ---- 保存位置 ----
  $("#novelCopyPath")?.addEventListener("click", async () => {
    const path = String(novel.location?.novel_root || "");
    if (!path) return;
    try {
      await navigator.clipboard.writeText(path);
      toast("保存目录已复制");
    } catch (_error) {
      // 手机或非 HTTPS 环境没有剪贴板权限：提示手动选中复制，不谎称成功。
      toast("无法自动复制，请长按上面的路径手动复制");
    }
  });
  $("#novelOpenFolder")?.addEventListener("click", async () => {
    try {
      await api("/api/novel/reveal", { method: "POST", body: {} });
    } catch (error) {
      fail("打开保存位置失败", error);
    }
  });

  $("#novelChapterFilter")?.addEventListener("input", (event) => {
    novel.filter = String(event.target.value || "");
    renderChapters();
  });

  $("#novelChapterList")?.addEventListener("change", (event) => {
    const box = event.target.closest("[data-novel-pick]");
    if (!box) return;
    const no = Number(box.dataset.novelPick);
    if (box.checked) novel.picked.add(no);
    else {
      novel.picked.delete(no);
      novel.manuscriptChoice.delete(no);
    }
    box.closest(".novel-chapter-row")?.classList.toggle("picked", box.checked);
    updatePickedCount();
    // 阶段二的素材就绪状态跟着勾选变，必须重渲染生成按钮。
    if (novel.stage !== "split") {
      renderStagePanel();
      void refreshPreviewBudget();
    }
  });

  $("#novelChapterList")?.addEventListener("click", (event) => {
    // 阶段二下章节行是 label（点行=勾选），不再切章节；拆章阶段才是打开单章。
    if (novel.stage !== "split") return;
    const btn = event.target.closest("[data-novel-chapter]");
    if (!btn) return;
    // 再次点同一章就收起编辑区（不重新打开覆盖状态）。
    if (novel.editingManuscriptId && Number(btn.dataset.novelChapter) === novel.chapterNo) {
      void cancelEdit();
      return;
    }
    void openChapter(Number(btn.dataset.novelChapter));
  });

  $("#novelEditorBody")?.addEventListener("click", (event) => {
    // 分镜生图：发送到造梦机 / 全选（按钮跟着活动面板搬 id，必须用容器委托）。
    if (event.target.closest("#novelStoryboardSend")) {
      sendStoryboardToComfyui();
      return;
    }
    if (event.target.closest("#novelStoryboardAll")) {
      toggleStoryboardAll();
      return;
    }
    const card = event.target.closest("[data-novel-task]");
    if (!card) return;
    const task = novel.tasks.find((row) => row.id === card.dataset.novelTask);
    if (!task || task.novel?.stage !== novel.stage) return;
    const data = stageState();
    data.activeTaskId = task.id;
    data.streamText = String(task.result || task.result_partial || "");
    data.taskCursor = 0;
    novel.taskToken += 1;
    renderStagePanel();
    scheduleTaskPolling();
  });
  $("#novelEditorBody")?.addEventListener("change", (event) => {
    const target = event.target;
    if (target.matches("[data-context-elements]")) novel.selectedVersions.elements = Number(target.value) || 0;
    else if (target.matches("[data-context-outline]")) novel.selectedVersions.outline = Number(target.value) || 0;
    else if (target.id === "novelStageVersion") novel.selectedVersions[novel.stage] = Number(target.value) || 0;
    else if (target.id === "novelScriptVersion") novel.selectedVersions.script = Number(target.value) || 0;
    else if (target.matches("[data-novel-pick-manuscript]")) {
      const chapterNo = Number(target.dataset.novelPickManuscript);
      const value = String(target.value || "");
      if (value) novel.manuscriptChoice.set(chapterNo, value);
      else novel.manuscriptChoice.delete(chapterNo);
      void refreshPreviewBudget();
      return;
    } else if (target.matches("[data-novel-storyboard-row]")) {
      // 分镜逐场勾选：只改发送范围，不触发预检。
      const stageData = stageState();
      const key = String(target.dataset.novelStoryboardRow || "");
      if (target.checked) stageData.checked.add(key);
      else stageData.checked.delete(key);
      renderStoryboard(stageData, novel.tasks.find((row) => row.id === stageData.activeTaskId) || null);
      return;
    } else return;
    renderStagePanel();
    void refreshPreviewBudget();
  });

  // ---- 双滑条：值变化立即反馈，并保存为后续默认值 ----
  const onLimits = (event) => {
    // 拆章阶段的润色面板和阶段面板各有一份「正文上限」滑条，两份都写同一个状态。
    const source = event?.target?.id === "novelAiBodyChars" ? $("#novelAiBodyChars") : $("#novelBodyChars");
    novel.bodyChars = Number(source?.value || novel.bodyChars);
    novel.maxChapters = Number($("#novelMaxChapters")?.value || novel.maxChapters);
    saveLimits();
    renderLimits();
    // 拆章阶段滑条挂在 AI 润色面板上，阶段面板是隐藏的，不必（也不该）重画它。
    if (novel.stage !== "split") renderStagePanel();
    void refreshPreviewBudget();
  };
  $("#novelBodyChars")?.addEventListener("input", onLimits);
  $("#novelAiBodyChars")?.addEventListener("input", onLimits);
  $("#novelMaxChapters")?.addEventListener("input", onLimits);

  // ---- 阶段切换与生成 ----
  $("#novelStages")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-novel-stage]");
    if (!btn || btn.disabled) return;
    setStage(btn.dataset.novelStage);
  });
  $("#novelManageBtn")?.addEventListener("click", () => openManage());
  $("#novelManageClose")?.addEventListener("click", () => closeManage());
  $("#novelManageDialog")?.addEventListener("close", () => { novel.manage.open = false; });
  $("#novelManageTabs")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-novel-manage-tab]");
    if (!btn) return;
    novel.manage.tab = btn.dataset.novelManageTab;
    novel.manage.selected.clear();
    const preview = $("#novelManagePreview");
    if (preview) preview.hidden = true;
    renderManage();
  });
  $("#novelManageFilter")?.addEventListener("input", (event) => {
    novel.manage.filter = String(event.target.value || "");
    renderManage();
  });
  $("#novelManageList")?.addEventListener("click", (event) => {
    const open = event.target.closest("[data-novel-manage-open]");
    if (!open) return;
    const key = open.dataset.novelManageOpen || "";
    const row = filteredManageRows().find((item) => manageRowKey(item) === key);
    if (row) void openManageRecord(row);
  });
  $("#novelManageList")?.addEventListener("change", (event) => {
    const box = event.target.closest("[data-novel-manage-row]");
    if (!box) return;
    if (box.checked) novel.manage.selected.add(box.dataset.novelManageRow);
    else novel.manage.selected.delete(box.dataset.novelManageRow);
    renderManage();
  });
  $("#novelManageSelectAll")?.addEventListener("click", () => {
    for (const row of filteredManageRows()) {
      if (manageRowSelectable(row)) novel.manage.selected.add(manageRowKey(row));
    }
    renderManage();
  });
  $("#novelManageClear")?.addEventListener("click", () => {
    novel.manage.selected.clear();
    renderManage();
  });
  $("#novelManageDelete")?.addEventListener("click", () => void deleteManageSelection());
  document.querySelector("#novelTaskHistory")?.addEventListener("click", (event) => {
    const all = event.target.closest("[data-novel-manage-tab]");
    if (all) {
      openManage(all.dataset.novelManageTab || "task");
      return;
    }
  });

  // 结果/已保存区块的折叠开关。面板是按阶段克隆的，所以委托在稳定的编辑区容器上，
  // 而不是绑某个阶段的 DOM 节点。
  $("#novelEditorBody")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-novel-collapse]");
    if (!btn || novel.stage === "split") return;
    const key = `${novel.stage}:${btn.dataset.novelCollapse}`;
    if (novel.collapsed.has(key)) novel.collapsed.delete(key);
    else novel.collapsed.add(key);
    renderStagePanel();
  });

  document.querySelectorAll("#novelEditorBody [data-stage-panel]").forEach((panel) => {
    panel.querySelector('[data-novel-control="novelStageGenerate"]')?.addEventListener("click", () => void generateStage());
    panel.querySelector('[data-novel-control="novelStageCancel"]')?.addEventListener("click", () => void cancelStageTask());
    panel.querySelector('[data-novel-control="novelStageRetry"]')?.addEventListener("click", () => void retryStageTask());
    panel.querySelector('[data-novel-control="novelStageApply"]')?.addEventListener("click", () => void applyStageResult());
    panel.querySelector('[data-novel-control="novelStageProvider"]')?.addEventListener("change", async (event) => {
      novel.stageProviderKey = String(event.target.value || "");
      novel.stageModelName = "";
      novel.stageModelName = await fillModelSelect($("#novelStageModel"), novel.stageProviderKey);
      renderStagePanel();
      void refreshPreviewBudget();
    });
    panel.querySelector('[data-novel-control="novelStageModel"]')?.addEventListener("change", (event) => {
      novel.stageModelName = String(event.target.value || "");
      void applyModelSkinFor(novel.stageModelName);
      renderStagePanel();
      void refreshPreviewBudget();
    });
    panel.querySelector('[data-novel-control="novelStageNote"]')?.addEventListener("change", () => void refreshPreviewBudget());
    panel.querySelector('[data-novel-control="novelSceneList"]')?.addEventListener("click", (event) => {
      const action = event.target.closest("[data-novel-scene-action]");
      if (action) {
        if (novel.selectedVersions.script && novel.selectedVersions.script !== Number(novel.book?.script_version || 0)) return;
        const no = Number(action.dataset.novelSceneNo);
        const kind = action.dataset.novelSceneAction;
        if (kind === "delete") void deleteScene(no);
        else void moveScene(no, kind);
        return;
      }
      const open = event.target.closest("[data-novel-scene-open]");
      if (!open) return;
      if (novel.selectedVersions.script && novel.selectedVersions.script !== Number(novel.book?.script_version || 0)) return;
      const no = Number(open.dataset.novelSceneOpen);
      // 再点同一场就收起编辑区（不重复展开）。
      novel.sceneEditing = novel.sceneEditing === no ? 0 : no;
      renderSceneBlock(true);
    });
    panel.querySelector('[data-novel-control="novelSceneSave"]')?.addEventListener("click", () => void saveScene());
    panel.querySelector('[data-novel-control="novelSceneCancel"]')?.addEventListener("click", () => {
      novel.sceneEditing = 0;
      renderSceneBlock(true);
    });
  });

  $("#novelBookList")?.addEventListener("click", (event) => {
    const del = event.target.closest("[data-novel-delete-book]");
    if (del) {
      void deleteBook(del.dataset.novelDeleteBook, del.dataset.novelDeleteTitle || "");
      return;
    }
    const open = event.target.closest("[data-novel-open]");
    if (!open) return;
    $("#novelBooksDialog")?.close?.();
    void openBook(open.dataset.novelOpen);
  });

  $("#novelPanes")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-novel-pane]");
    if (!btn) return;
    setPane(btn.dataset.novelPane);
  });

  $("#novelChapterBody")?.addEventListener("input", markDirty);

  // 横竖屏切换会改变三栏/分档布局，inert 必须跟着重算，否则转屏后整栏点不动。
  if (typeof window.matchMedia === "function") {
    const query = window.matchMedia(PORTRAIT_QUERY);
    const onChange = () => applyPaneAccessibility();
    if (typeof query.addEventListener === "function") query.addEventListener("change", onChange);
    else if (typeof query.addListener === "function") query.addListener(onChange);
  }

  // 功能页切换：进入本页时按服务端校准一次（页面刷新后也能恢复上次作品）。
  onStudioPageChange((next, detail) => {
    if (detail?.closed || next !== "novel") {
      // 切走就停轮询：页面隐藏后继续轮询只是白耗请求。
      stopTaskPolling();
      return;
    }
    // 模型下拉依赖 bootstrap 的供应商配置：首屏时它可能还没到，进页面时重算一次。
    fillStageModels();
    void loadLocation();
    if (novel.bookId) void openBook(novel.bookId);
    else {
      let remembered = "";
      try { remembered = localStorage.getItem(LAST_BOOK_KEY) || ""; } catch (_error) { /* storage may be unavailable */ }
      if (remembered) void openBook(remembered);
      else if (novel.stage !== "split") void refreshStageTasks();
    }
  });

  setPane(novel.pane);
  fillStageModels();
  void loadLocation();
  renderAll();
}
