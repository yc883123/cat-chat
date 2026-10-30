// ============================================================
// 27-writer.js —— 工作台「织书台」功能页（原创写作，module="writer"）
// 新建/导入作品 → 三阶段（设定 → 大纲 → 正文）→ 导出。
//
// 边界（织书台实施计划 §9）：
// - 作品与正文以服务端为唯一事实源：列表/详情只拿索引，正文只有单章接口给。
// - 生成推进由服务端流水线负责（0.5s 轮询驱动）；前端只发命令、展示流水线与
//   任务事件，不持有「下一步必须执行」的状态——刷新/迟到响应都不会重复生成。
// - 版本不可变：AI 写回、人工保存、恢复旧版本都产生新版本并切换指针。
// - 只从 01-core / 22-studio 取公共能力，22 不反向 import 本文件，无循环依赖。
// ============================================================

import { $, api, applyModelSkinFor, state, toast } from "./01-core.js";
import { onStudioPageChange } from "./22-studio.js";

const WRITER_TASKS_URL = "/api/studio/tasks?module=writer";
const LAST_BOOK_KEY = "naibaWriterLastBookId";
const LIMITS_KEY = "naibaWriterLimits";
// 模板资源是 public 静态文件：不带口令也能取（与页面本身同口径），没有密钥。
const TEMPLATES_URL = "/studio/writer/templates.json";

const TASK_STATUS_LABELS = {
  queued: "排队中",
  running: "生成中",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};
const TERMINAL_TASK_STATUSES = new Set(["done", "failed", "cancelled", "interrupted"]);

const PIPELINE_STATUS_LABELS = {
  running: "进行中",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
  conflict: "版本冲突",
};

// 流水线类型的中文名（与服务端 PIPELINE_KINDS 对齐）。
const KIND_LABELS = {
  setup_all: "生成整套设定",
  setup_step: "单步设定",
  outline: "生成全书大纲",
  chapter: "整章正文",
  chapter_segs: "分段正文",
  chapter_continue: "续写正文",
  chapter_rewrite: "重写本章",
};

// 步骤中文名（与服务端 prompts.STEP_LABELS 对齐）。
const STEP_LABELS = {
  genre: "题材推断",
  title: "书名候选",
  blurb: "作品简介",
  world: "世界观设定",
  chars: "角色设定",
  outline: "章节大纲",
  segs: "段落要点",
  draft: "正文",
};

// 写作风格（与服务端 STYLE_PRESETS 对齐）。
const STYLE_OPTIONS = [
  ["web", "网文爽文（默认）"],
  ["jinyong", "历史正剧 · 金庸式"],
  ["strict", "严肃历史（严格考据）"],
  ["light", "轻松幽默"],
  ["sus", "悬疑烧脑"],
];

const VERSION_KIND_LABELS = {
  manual: "人工",
  generated: "AI 生成",
  imported: "导入",
  template: "模板",
};

const CHAR_FIELDS = [
  ["name", "名字"],
  ["role", "身份定位"],
  ["identity", "一句话简介"],
  ["goal", "目标动机"],
  ["traits", "性格特点"],
  ["speech", "说话风格"],
  ["arc", "成长弧线"],
  ["background", "背景故事"],
];

const writer = {
  initialized: false,
  // 功能页是否正被显示（切走时停轮询）。
  visible: false,
  busy: false,
  // 作品态：bookId 为空表示还没打开作品。
  bookId: "",
  book: null,
  setup: null,
  outline: null,
  chapters: [],
  broken: [],
  location: null,
  stage: "setup",
  pane: "editor",
  // 生成参数（滑条），本地保留。
  charCount: 4,
  chapterCount: 12,
  outlineChapterCount: 12,
  // 模型选择（与拆书匠同口径）：先选供应商，再按供应商拉完整模型目录。
  setupProviderKey: "", setupModelName: "",
  outlineProviderKey: "", outlineModelName: "",
  draftProviderKey: "", draftModelName: "",
  providerModelsCache: new Map(),
  providerModelInflight: new Map(),
  // 设定表单编辑态：套用模板 / 手改后为 true，保存或放弃后复位。
  setupDirty: false,
  // 当前打开章节与正文。
  chapterNo: 0,
  chapter: null,
  draftDirty: false,
  // 流水线与轮询。
  pipeline: null,
  pipelineTask: null,
  streamTaskId: "",
  streamText: "",
  taskCursor: 0,
  pollTimer: null,
  pollBusy: false,
  pollToken: 0,
  // 预检（右栏「输入来源」），与正式提交共用服务端 build 函数。
  budget: null,
  budgetToken: 0,
  // 迟到响应保护：切作品/切章节都换令牌，旧响应直接丢。
  bookToken: 0,
  chapterToken: 0,
  booksToken: 0,
  // 模板弹窗。
  templates: null,
  tplChannel: "",
  tplCat: "",
  tplQuery: "",
  tplSelectedId: "",
  // 导入预览。
  importId: "",
  importPreview: null,
  // 管理记录。
  manage: {
    tab: "setup",
    filter: "",
    selected: new Set(),
    token: 0,
    expandedChapter: 0,
  },
};

// ============================================================
// 小工具
// ============================================================

function fmtChars(count) {
  const value = Number(count) || 0;
  if (value >= 10000) return `${(value / 10000).toFixed(1)}万`;
  return String(value);
}

function fmtTime(ms) {
  const value = Number(ms);
  if (!Number.isFinite(value) || value <= 0) return "时间未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未记录";
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function setSaveState(text, noteText = "") {
  const stateEl = $("#writerSaveState");
  if (stateEl) stateEl.textContent = text;
  const noteEl = $("#writerSaveNote");
  if (noteEl && noteText) noteEl.textContent = noteText;
}

function fail(prefix, error) {
  const message = String(error?.message || error || "未知错误");
  toast(`${prefix}：${message}`);
}

function setBusy(busy) {
  writer.busy = busy;
  for (const id of ["writerSetupGenerate", "writerSetupSave", "writerOutlineGenerate", "writerOutlineSave",
    "writerSyncBtn", "writerDraftGenerate", "writerDraftSegs", "writerDraftContinue", "writerDraftRewrite",
    "writerDraftSave", "writerImportCommit", "writerExportBtn", "writerManageBtn",
    "writerImportBtn", "writerBooksBtn", "writerNewBookBtn"]) {
    const btn = $(`#${id}`);
    if (btn) btn.disabled = busy;
  }
}

function kindLabel(kind) {
  return KIND_LABELS[String(kind || "")] || "生成";
}

function stepLabel(step) {
  return STEP_LABELS[String(step || "")] || step || "—";
}

function versionKindLabel(kind) {
  return VERSION_KIND_LABELS[String(kind || "")] || (kind || "未知");
}

// ============================================================
// 供应商 / 模型下拉（与 25-novel.js 同一套口径）
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
  let models = writer.providerModelsCache.get(providerKey);
  if (!models) {
    const inflight = writer.providerModelInflight.get(providerKey);
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
      writer.providerModelInflight.set(providerKey, task);
      try {
        models = await task;
      } finally {
        writer.providerModelInflight.delete(providerKey);
      }
      writer.providerModelsCache.set(providerKey, models);
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
    const configured = String(
      (state.bootstrap?.model_profiles || []).find((row) => row.model_key === providerKey)?.model || ""
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

let providerChain = Promise.resolve();
function fillProviderModels() {
  providerChain = providerChain.then(async () => {
    const pairs = [
      ["writerSetupProvider", "writerSetupModel", "setupProviderKey", "setupModelName"],
      ["writerOutlineProvider", "writerOutlineModel", "outlineProviderKey", "outlineModelName"],
      ["writerDraftProvider", "writerDraftModel", "draftProviderKey", "draftModelName"],
    ];
    for (const [providerId, modelId, providerField, modelField] of pairs) {
      const providerSelect = $(`#${providerId}`);
      writer[providerField] = fillProviderSelect(providerSelect, writer[providerField]);
      writer[modelField] = await fillModelSelect($(`#${modelId}`), writer[providerField]);
    }
  }).catch(() => {});
  return providerChain;
}

function readModel(providerField, modelField) {
  const providerKey = writer[providerField] || "";
  const modelName = writer[modelField] || "";
  return { model_key: providerKey, model_name: modelName };
}

// ============================================================
// 渲染：页面头 / 左栏 / 阶段面板
// ============================================================

function renderPageState() {
  const el = $("#writerPageState");
  if (!el) return;
  if (!writer.bookId) {
    el.textContent = "未打开作品";
    return;
  }
  const title = `《${writer.book?.title || "未命名作品"}》`;
  const stageName = { setup: "设定", outline: "大纲", draft: "正文" }[writer.stage] || "设定";
  const running = writer.pipeline?.status === "running";
  el.textContent = running
    ? `${title} · ${kindLabel(writer.pipeline.kind)} · ${stageName}`
    : `${title} · ${stageName}`;
}

function renderBookSummary() {
  const summary = $("#writerBookSummary");
  if (!summary) return;
  const hasBook = Boolean(writer.bookId && writer.book);
  summary.hidden = !hasBook;
  if (!hasBook) return;
  const cover = $("#writerBookCover");
  if (cover) cover.textContent = (writer.book.title || "未")[0] || "未";
  const titleEl = $("#writerBookTitle");
  if (titleEl) titleEl.textContent = writer.book.title || "未命名作品";
  const setupTitle = String(writer.setup?.title || "").trim();
  const meta = $("#writerBookMeta");
  if (meta) {
    const bits = [];
    if (String(writer.setup?.genre?.cat || "").trim()) bits.push(String(writer.setup.genre.cat).trim());
    if (String(writer.setup?.genre?.tone || "").trim()) bits.push(String(writer.setup.genre.tone).trim());
    meta.textContent = bits.length ? bits.join(" · ") : "还没有设定";
  }
  const total = writer.chapters.length;
  const done = writer.chapters.filter((row) => Number(row.len) > 0).length;
  const chars = writer.chapters.reduce((sum, row) => sum + (Number(row.len) || 0), 0);
  const statChapters = $("#writerStatChapters");
  if (statChapters) statChapters.textContent = String(total);
  const statDone = $("#writerStatDone");
  if (statDone) statDone.textContent = String(done);
  const statChars = $("#writerStatChars");
  if (statChars) statChars.textContent = fmtChars(chars);
  const progressText = $("#writerProgressText");
  if (progressText) progressText.textContent = `${done} / ${total || 0}`;
  const progressBar = $("#writerProgressBar");
  if (progressBar) progressBar.style.width = total ? `${Math.round((done / total) * 100)}%` : "0%";
  const tocMeta = $("#writerTocMeta");
  if (tocMeta) tocMeta.textContent = total ? `${total} 章 · ${done} 章已成稿` : "还没有章节";
}

function chapterDot(row) {
  if (writer.pipeline?.status === "running"
    && Number(writer.pipeline?.request_snapshot?.chapter_no || 0) === Number(row.no)) return "run";
  if (Number(row.len) > 0) return "done";
  return "";
}

function chapterRow(row) {
  const item = document.createElement("div");
  item.className = "writer-chapter-row";
  item.classList.toggle("active", Number(row.no) === writer.chapterNo && writer.stage === "draft");
  item.dataset.writerChapter = String(row.no);
  const dot = document.createElement("i");
  dot.className = `writer-dot ${chapterDot(row)}`.trim();
  const copy = document.createElement("span");
  copy.className = "writer-chapter-copy";
  const strong = document.createElement("strong");
  strong.textContent = `第${row.no}章 ${row.title || ""}`;
  const small = document.createElement("small");
  small.textContent = Number(row.len) > 0 ? `${fmtChars(row.len)} 字 · 已成稿` : "待写 · 梗概已有";
  copy.append(strong, small);
  item.append(dot, copy);
  return item;
}

function renderChapters() {
  const list = $("#writerChapterList");
  if (!list) return;
  const empty = $("#writerChapterEmpty");
  if (!writer.bookId) {
    list.replaceChildren();
    if (empty) {
      empty.hidden = false;
      empty.textContent = "还没有打开作品。点右上「新建作品」或「作品」。";
    }
    return;
  }
  if (!writer.chapters.length) {
    list.replaceChildren();
    if (empty) {
      empty.hidden = false;
      empty.textContent = "还没有章节。先在「设定」完成创意并生成大纲（会自动同步章节），或在大纲页点「按当前大纲同步章节」。";
    }
    return;
  }
  if (empty) empty.hidden = true;
  list.replaceChildren(...writer.chapters.map(chapterRow));
}

function fillSetupForm() {
  const setup = writer.setup || {};
  const value = (id, text) => {
    const el = $(`#${id}`);
    if (el) el.value = String(text ?? "");
  };
  value("writerIdea", setup.idea);
  value("writerTitle", setup.title);
  value("writerBlurb", setup.blurb);
  value("writerWorldview", setup.worldview);
  value("writerGenreCat", setup.genre?.cat);
  value("writerGenreTone", setup.genre?.tone);
  value("writerVibe", setup.constraints?.vibe);
  value("writerAvoid", setup.constraints?.avoid);
  value("writerTags", (setup.genre_tags || []).join(", "));
  const style = $("#writerStyle");
  if (style) {
    style.replaceChildren(...STYLE_OPTIONS.map(([key, label]) => new Option(label, key)));
    style.value = String(setup.style || "web");
  }
  renderCharacters();
  renderCandidates("#writerTitleCandidates", "#writerTitleCandidateList", setup.title_candidates,
    (text) => {
      const input = $("#writerTitle");
      if (input) input.value = text;
      writer.setupDirty = true;
      toast(`书名已改为「${text}」，保存后生效`);
    });
  renderCandidates("#writerBlurbCandidates", "#writerBlurbCandidateList", setup.blurb_candidates,
    (text) => {
      const input = $("#writerBlurb");
      if (input) input.value = text;
      writer.setupDirty = true;
      toast("简介已替换为该候选，保存后生效");
    });
}

function renderCandidates(boxId, listId, candidates, onPick) {
  const box = $(boxId);
  if (!box) return;
  const rows = (Array.isArray(candidates) ? candidates : []).filter((row) => String(row || "").trim());
  box.hidden = !rows.length;
  if (!rows.length) return;
  const list = $(listId);
  if (!list) return;
  list.replaceChildren(...rows.map((text) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "writer-candidate";
    chip.title = "点一下采用这条候选";
    chip.textContent = String(text || "").length > 60
      ? `${String(text).slice(0, 60)}…`
      : String(text || "");
    chip.addEventListener("click", () => onPick(String(text || "")));
    return chip;
  }));
}

function renderCharacters() {
  const list = $("#writerCharList");
  if (!list) return;
  const chars = (writer.setup?.characters || []).filter((row) => row && typeof row === "object");
  if (!chars.length) {
    const empty = document.createElement("p");
    empty.className = "writer-empty";
    empty.textContent = "还没有角色。生成设定、从模板套用，或点「加一个角色」手写。";
    list.replaceChildren(empty);
    return;
  }
  list.replaceChildren(...chars.map((row, index) => {
    const card = document.createElement("div");
    card.className = "writer-char-card";
    card.dataset.writerCharIndex = String(index);
    const head = document.createElement("div");
    head.className = "writer-char-head";
    const strong = document.createElement("strong");
    strong.textContent = row.name || `角色 ${index + 1}`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "control-button tiny danger";
    remove.dataset.writerCharRemove = String(index);
    remove.textContent = "删除";
    head.append(strong, remove);
    const grid = document.createElement("div");
    grid.className = "writer-grid-2";
    for (const [key, label] of CHAR_FIELDS) {
      if (key === "background") continue;
      const field = document.createElement("label");
      field.className = "writer-field";
      field.textContent = label;
      const input = document.createElement("input");
      input.type = "text";
      input.dataset.writerCharField = key;
      input.value = String(row[key] || "");
      field.append(input);
      grid.append(field);
    }
    const backgroundField = document.createElement("label");
    backgroundField.className = "writer-field";
    backgroundField.textContent = "背景故事";
    const backgroundInput = document.createElement("textarea");
    backgroundInput.rows = 2;
    backgroundInput.dataset.writerCharField = "background";
    backgroundInput.value = String(row.background || "");
    backgroundField.append(backgroundInput);
    card.append(head, grid, backgroundField);
    return card;
  }));
}

function collectCharacters() {
  const list = $("#writerCharList");
  if (!list) return [];
  const rows = [];
  for (const card of list.querySelectorAll(".writer-char-card")) {
    const row = {};
    for (const [key] of CHAR_FIELDS) {
      const input = card.querySelector(`[data-writer-char-field="${key}"]`);
      row[key] = String(input?.value || "").trim();
    }
    if (row.name) rows.push(row);
  }
  return rows;
}

function collectSetup() {
  const splitTags = String($("#writerTags")?.value || "")
    .split(/[,，、]/).map((tag) => tag.trim()).filter(Boolean).slice(0, 5);
  return {
    idea: String($("#writerIdea")?.value || ""),
    genre: {
      channel: String(writer.setup?.genre?.channel || ""),
      cat: String($("#writerGenreCat")?.value || "").trim(),
      sub_cat: String(writer.setup?.genre?.sub_cat || ""),
      tone: String($("#writerGenreTone")?.value || "").trim(),
      hook: String(writer.setup?.genre?.hook || ""),
    },
    genre_tags: splitTags,
    title: String($("#writerTitle")?.value || "").trim(),
    title_candidates: Array.isArray(writer.setup?.title_candidates) ? writer.setup.title_candidates : [],
    blurb: String($("#writerBlurb")?.value || ""),
    blurb_candidates: Array.isArray(writer.setup?.blurb_candidates) ? writer.setup.blurb_candidates : [],
    worldview: String($("#writerWorldview")?.value || ""),
    characters: collectCharacters(),
    style: String($("#writerStyle")?.value || "web"),
    constraints: {
      vibe: String($("#writerVibe")?.value || "").trim(),
      avoid: String($("#writerAvoid")?.value || "").trim(),
    },
  };
}

function renderOutline() {
  const list = $("#writerOutlineList");
  if (!list) return;
  const rows = Array.isArray(writer.outline?.chapters) ? writer.outline.chapters : [];
  const meta = $("#writerOutlineMeta");
  if (meta) {
    meta.textContent = rows.length
      ? `${rows.length} 章 · 改完点「保存大纲修改」`
      : (writer.bookId ? "还没有大纲。点「生成全书大纲」。" : "打开作品后再生成大纲");
  }
  const save = $("#writerOutlineSave");
  if (save) save.disabled = !rows.length || writer.busy;
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "writer-empty";
    empty.textContent = writer.bookId
      ? "还没有大纲。点上面的「生成全书大纲」。"
      : "先打开或新建作品。";
    list.replaceChildren(empty);
    return;
  }
  list.replaceChildren(...rows.map((row) => {
    const item = document.createElement("label");
    item.className = "writer-outline-item";
    const head = document.createElement("div");
    head.className = "writer-outline-head";
    const no = document.createElement("span");
    no.textContent = `第${row.no}章`;
    const title = document.createElement("input");
    title.type = "text";
    title.dataset.writerOutlineTitle = String(row.no);
    title.value = String(row.title || "");
    title.setAttribute("aria-label", `第${row.no}章标题`);
    head.append(no, title);
    const beat = document.createElement("textarea");
    beat.rows = 2;
    beat.dataset.writerOutlineBeat = String(row.no);
    beat.value = String(row.beat || "");
    beat.placeholder = "这一章发生什么";
    beat.setAttribute("aria-label", `第${row.no}章梗概`);
    const hook = document.createElement("input");
    hook.type = "text";
    hook.dataset.writerOutlineHook = String(row.no);
    hook.value = String(row.hook || "");
    hook.placeholder = "章末钩子（可留空）";
    hook.setAttribute("aria-label", `第${row.no}章钩子`);
    item.append(head, beat, hook);
    return item;
  }));
}

function collectOutline() {
  const list = $("#writerOutlineList");
  if (!list) return [];
  const rows = [];
  for (const item of list.querySelectorAll(".writer-outline-item")) {
    const title = item.querySelector("[data-writer-outline-title]");
    const beat = item.querySelector("[data-writer-outline-beat]");
    const hook = item.querySelector("[data-writer-outline-hook]");
    const no = Number(title?.dataset.writerOutlineTitle || 0);
    if (!no) continue;
    rows.push({
      no,
      title: String(title?.value || "").trim() || `第 ${no} 章`,
      beat: String(beat?.value || "").trim(),
      hook: String(hook?.value || "").trim(),
    });
  }
  return rows;
}

function renderOutlineVersions() {
  const group = $("#writerOutlineVersionsGroup");
  const select = $("#writerOutlineVersion");
  if (!group || !select) return;
  const versions = Array.isArray(writer.book?.outline_versions) ? writer.book.outline_versions : [];
  group.hidden = versions.length < 1;
  const current = String(writer.book?.current_outline_version_id || "");
  select.replaceChildren(...versions.map((row) => new Option(
    `v${row.version} · ${versionKindLabel(row.kind)} · ${row.chapter_count} 章 · ${fmtTime(row.updated_at)}`,
    String(row.id || ""),
  )));
  if (versions.length) select.value = current || String(versions.at(-1).id || "");
}

function renderDraft() {
  const chapter = writer.chapter;
  const hasChapter = Boolean(chapter);
  const titleEl = $("#writerDraftTitle");
  if (titleEl) titleEl.textContent = hasChapter ? `第${chapter.no}章 ${chapter.title || ""}` : "还没有选择章节";
  const beatEl = $("#writerDraftBeat");
  if (beatEl) {
    beatEl.textContent = hasChapter && String(chapter.beat || "").trim() ? `梗概：${chapter.beat}` : "";
  }
  const content = $("#writerDraftContent");
  if (content && !writer.draftDirty) content.value = hasChapter ? String(chapter.content || "") : "";
  const meta = $("#writerDraftMeta");
  if (meta) {
    if (!hasChapter) meta.textContent = "点左侧目录选一章";
    else if (String(chapter.content || "").trim()) meta.textContent = `${fmtChars(String(chapter.content).length)} 字 · ${chapter.versions?.length || 0} 个版本`;
    else meta.textContent = "还没有正文";
  }
  const canWrite = hasChapter && Boolean(writer.book?.current_outline_version_id);
  for (const id of ["writerDraftGenerate", "writerDraftSegs", "writerDraftRewrite"]) {
    const btn = $(`#${id}`);
    if (btn) btn.disabled = !canWrite || writer.busy;
  }
  const continueBtn = $("#writerDraftContinue");
  if (continueBtn) continueBtn.disabled = !(hasChapter && String(chapter?.content || "").trim()) || writer.busy;
  const save = $("#writerDraftSave");
  if (save) save.disabled = !hasChapter || !writer.draftDirty || writer.busy;
  const reload = $("#writerDraftReload");
  if (reload) reload.disabled = !hasChapter || !writer.draftDirty;
  // 版本清单
  const group = $("#writerDraftVersionsGroup");
  const select = $("#writerDraftVersion");
  if (group && select) {
    const versions = hasChapter ? (chapter.versions || []) : [];
    group.hidden = versions.length < 1;
    select.replaceChildren(...versions.map((row) => new Option(
      `v${row.version} · ${versionKindLabel(row.kind)} · ${fmtChars(row.len)} 字 · ${fmtTime(row.updated_at)}`,
      String(row.id || ""),
    )));
    if (versions.length) select.value = String(chapter.current_version_id || versions.at(-1).id || "");
  }
}

function renderEditor() {
  const placeholder = $("#writerEditorPlaceholder");
  if (placeholder) placeholder.hidden = Boolean(writer.bookId);
  for (const panel of document.querySelectorAll("[data-writer-stage-panel]")) {
    panel.hidden = panel.dataset.writerStagePanel !== writer.stage || !writer.bookId;
  }
  const heads = {
    setup: ["设定", "先告诉它你要写一本什么书"],
    outline: ["全书大纲", "按设定包写出每一章的走向"],
    draft: ["正文", "写正文时自动带上设定摘要与前文衔接"],
  };
  const head = $("#writerEditorHead");
  if (head) head.textContent = heads[writer.stage][0];
  const hint = $("#writerEditorHint");
  if (hint) hint.textContent = heads[writer.stage][1];
  if (writer.stage === "setup") fillSetupForm();
  if (writer.stage === "outline") { renderOutline(); renderOutlineVersions(); }
  if (writer.stage === "draft") renderDraft();
}

function renderAll() {
  renderPageState();
  renderBookSummary();
  renderChapters();
  renderEditor();
  renderPipeline();
  refreshPlan();
  renderLocation();
  const exportBtn = $("#writerExportBtn");
  if (exportBtn) exportBtn.disabled = !writer.bookId || writer.busy;
  for (const id of ["writerManageBtn", "writerImportBtn", "writerBooksBtn", "writerNewBookBtn"]) {
    const btn = $(`#${id}`);
    if (btn) btn.disabled = writer.busy;
  }
}

// ============================================================
// 右栏：输入来源（预检）与生成进度
// ============================================================

function planLine(label, chars, extra = "") {
  return { label, chars, extra };
}

function renderPlan() {
  const body = $("#writerPlanBody");
  const stateEl = $("#writerPlanState");
  const meta = $("#writerBudgetMeta");
  if (!body) return;
  const budget = writer.budget;
  if (!budget) {
    body.replaceChildren(Object.assign(document.createElement("p"), {
      className: "writer-empty",
      textContent: PLAN_EMPTY_TEXT[writer.stage] || "打开作品后，这里显示本次生成会带上哪些内容。",
    }));
    if (stateEl) stateEl.textContent = "—";
    if (meta) meta.textContent = "";
    return;
  }
  const blocks = [];
  const included = Array.isArray(budget.included) ? budget.included : [];
  const omitted = Array.isArray(budget.omitted) ? budget.omitted : [];
  if (included.length) {
    const card = document.createElement("div");
    card.className = "writer-note-card";
    const title = document.createElement("strong");
    title.textContent = "本次会带上";
    const list = document.createElement("div");
    list.className = "writer-kv";
    for (const slot of included) {
      const span = document.createElement("span");
      span.textContent = String(slot.label || slot.key || "");
      const b = document.createElement("b");
      b.textContent = `${fmtChars(slot.chars)} 字`;
      list.append(span, b);
    }
    card.append(title, list);
    blocks.push(card);
  }
  if (omitted.length) {
    const card = document.createElement("div");
    card.className = "writer-note-card";
    const title = document.createElement("strong");
    title.textContent = "会被省略的";
    const p = document.createElement("p");
    p.textContent = omitted
      .map((slot) => `${slot.label || slot.key || ""}（${slot.reason || "未提供"}）`)
      .join("；");
    card.append(title, p);
    blocks.push(card);
  }
  const kv = document.createElement("div");
  kv.className = "writer-kv";
  const rows = [
    ["预计输入", `≈ ${fmtChars(budget.estimated_tokens)} tokens`],
    ["模型", readModel(...PLAN_MODEL_FIELDS[writer.stage] || ["setupProviderKey", "setupModelName"]).model_name || "未选择"],
  ];
  for (const [key, value] of rows) {
    const span = document.createElement("span");
    span.textContent = key;
    const b = document.createElement("b");
    b.textContent = value;
    kv.append(span, b);
  }
  blocks.push(kv);
  if (budget.fits === false) {
    const card = document.createElement("div");
    card.className = "writer-note-card writer-warn-card";
    const title = document.createElement("strong");
    title.textContent = "输入超过模型窗口";
    const p = document.createElement("p");
    p.textContent = (budget.suggestions || []).join(" ") || "请减少设定或前文引用后再生成";
    card.append(title, p);
    blocks.push(card);
  }
  body.replaceChildren(...blocks);
  if (stateEl) stateEl.textContent = budget.fits === false ? "超窗口，先减料" : "可以生成";
  if (meta) meta.textContent = `共 ${fmtChars(budget.chars)} 字`;
}

const PLAN_EMPTY_TEXT = {
  setup: "还没打开作品。生成前会先在这里列出六步链第一步（题材推断）的输入来源。",
  outline: "还没打开作品。生成前会先在这里列出大纲步的输入来源（当前设定 + 章节数）。",
  draft: "先打开作品并选一章。生成前会在这里列出实际带上的设定、摘要、上一章结尾与本章梗概。",
};
const PLAN_MODEL_FIELDS = {
  setup: ["setupProviderKey", "setupModelName"],
  outline: ["outlineProviderKey", "outlineModelName"],
  draft: ["draftProviderKey", "draftModelName"],
};

async function refreshPlan() {
  if (!writer.bookId) {
    writer.budget = null;
    renderPlan();
    return;
  }
  const kinds = {
    setup: "genre",
    outline: "outline",
    draft: "chapter",
  };
  const kind = kinds[writer.stage];
  if (!kind || (writer.stage === "draft" && !writer.chapterNo)) {
    writer.budget = null;
    renderPlan();
    return;
  }
  const model = readModel(...(PLAN_MODEL_FIELDS[writer.stage] || ["setupProviderKey", "setupModelName"]));
  const token = ++writer.budgetToken;
  try {
    const payload = await api("/api/writer/generate/preview", {
      method: "POST",
      body: {
        kind,
        book_id: writer.bookId,
        chapter_no: writer.stage === "draft" ? writer.chapterNo : undefined,
        model_key: model.model_key,
        char_count: writer.charCount,
        chapter_count: writer.stage === "setup" ? writer.chapterCount : writer.outlineChapterCount,
        extra_note: String(
          writer.stage === "setup" ? $("#writerSetupNote")?.value || ""
            : writer.stage === "outline" ? $("#writerOutlineNote")?.value || ""
              : $("#writerDraftNote")?.value || ""
        ),
      },
    });
    if (token !== writer.budgetToken || writer.stage !== stageAtRequest) return;
    writer.budget = payload.budget || null;
  } catch (_error) {
    if (token !== writer.budgetToken) return;
    writer.budget = null;
  }
  renderPlan();
}

// 预检请求发出时的阶段快照：响应回来时若已切阶段则丢弃。
let stageAtRequest = "setup";

function renderPipeline() {
  const block = $("#writerPipelineBlock");
  if (!block) return;
  const pipeline = writer.pipeline;
  block.hidden = !pipeline;
  if (!pipeline) return;
  const steps = Array.isArray(pipeline.steps) ? pipeline.steps : [];
  const stepIndex = Number(pipeline.step_index || 0);
  const total = steps.length;
  const current = pipeline.status === "running"
    ? (steps[stepIndex] || "")
    : "";
  const stepsLabel = $("#writerPipelineStepsLabel");
  if (stepsLabel) {
    stepsLabel.textContent = pipeline.status === "running"
      ? `${kindLabel(pipeline.kind)} · 第 ${Math.min(stepIndex + 1, total)} / ${total} 步 · ${stepLabel(current)}`
      : kindLabel(pipeline.kind);
  }
  const status = $("#writerPipelineStatus");
  if (status) {
    const label = PIPELINE_STATUS_LABELS[String(pipeline.status || "")] || pipeline.status;
    status.textContent = label;
    status.className = `writer-pill ${pipeline.status === "running" ? "run"
      : pipeline.status === "done" ? "ok"
        : pipeline.status === "conflict" ? "wait" : "bad"}`;
  }
  const model = $("#writerPipelineModel");
  if (model) {
    const snapshot = pipeline.request_snapshot || {};
    model.textContent = String(snapshot.model_name || "模型未记录");
  }
  const taskLine = $("#writerPipelineTask");
  if (taskLine) {
    const taskStatus = String(pipeline.task_status || writer.pipelineTask?.status || "");
    taskLine.textContent = pipeline.status === "running"
      ? `当前任务：${taskStatus ? TASK_STATUS_LABELS[taskStatus] || taskStatus : "查询中"}`
      : (pipeline.error || "—");
  }
  const stream = $("#writerPipelineStream");
  if (stream) {
    const text = writer.streamText || writer.pipelineTask?.result_partial || "";
    stream.hidden = !text;
    if (text) {
      // 只展示尾部：长正文从头滚没有意义，最新内容才是用户要盯的。
      stream.textContent = text.length > 2000 ? `……${text.slice(-2000)}` : text;
      stream.scrollTop = stream.scrollHeight;
    }
  }
  const error = $("#writerPipelineError");
  if (error) {
    const showError = pipeline.error && pipeline.status !== "running";
    error.hidden = !showError;
    if (showError) error.textContent = String(pipeline.error || "");
  }
  const cancel = $("#writerPipelineCancel");
  if (cancel) cancel.hidden = pipeline.status !== "running";
  const retry = $("#writerPipelineRetry");
  if (retry) {
    retry.hidden = !["failed", "cancelled", "interrupted", "conflict"].includes(String(pipeline.status || ""));
    retry.disabled = writer.busy;
    retry.textContent = pipeline.status === "conflict" ? "基于当前版本重新生成" : "重试本步";
  }
  renderPageState();
}

// ============================================================
// 流水线轮询
// ============================================================

function stopPipelinePolling() {
  if (writer.pollTimer) {
    clearTimeout(writer.pollTimer);
    writer.pollTimer = null;
  }
}

function schedulePipelinePolling() {
  stopPipelinePolling();
  if (!writer.visible || !writer.pipeline || writer.pipeline.status !== "running") return;
  writer.pollTimer = setTimeout(() => void pollPipelineTick(), 1200);
}

async function pollPipelineTick() {
  const bookId = writer.bookId;
  const token = writer.pollToken;
  if (writer.pollBusy || !bookId || !writer.visible) return;
  const pipeline = writer.pipeline;
  if (!pipeline || pipeline.status !== "running") return;
  writer.pollBusy = true;
  try {
    const payload = await api(`/api/writer/books/${encodeURIComponent(bookId)}/pipeline`);
    if (token !== writer.pollToken || writer.bookId !== bookId) return;
    const fresh = payload.pipeline;
    if (!fresh) return;
    // 换任务：清空流式缓冲（新任务的事件从零开始）。
    const taskId = String(fresh.current_task_id || "");
    if (taskId !== writer.streamTaskId) {
      writer.streamTaskId = taskId;
      writer.streamText = "";
      writer.taskCursor = 0;
      writer.pipelineTask = null;
    }
    if (taskId) {
      try {
        const { task } = await api(`/api/studio/tasks/${encodeURIComponent(taskId)}`);
        if (token !== writer.pollToken) return;
        writer.pipelineTask = task;
        const events = await api(
          `/api/studio/tasks/${encodeURIComponent(taskId)}/events?after=${writer.taskCursor}`,
        );
        if (token !== writer.pollToken) return;
        for (const row of events.events || []) {
          if (row.type === "delta" && row.content) writer.streamText += String(row.content);
          if (typeof row.seq === "number") writer.taskCursor = Math.max(writer.taskCursor, Number(row.seq));
        }
      } catch (_error) {
        // 事件流读不到时退回用任务记录里的 result_partial，不影响终态判断。
      }
    }
    const wasRunning = writer.pipeline?.status === "running";
    writer.pipeline = fresh;
    renderPipeline();
    if (wasRunning && fresh.status !== "running") {
      // 终态：流水线可能已写回新版本，重读作品刷新目录与编辑区（保留未保存的编辑）。
      await openBook(bookId, { keepEdits: true, keepStage: true });
      if (fresh.status === "done") {
        toast(`生成完成：${kindLabel(fresh.kind)}`);
        setSaveState("生成完成", "本轮生成的内容已写回作品（可在目录与各阶段里看到）。");
      } else {
        const label = PIPELINE_STATUS_LABELS[String(fresh.status || "")] || fresh.status;
        toast(`生成${label}：${fresh.error || "可重试"}`);
        setSaveState("生成未完成", String(fresh.error || ""));
      }
      renderAll();
    }
  } catch (_error) {
    // 网络抖动不终止轮询；下一轮继续。
  } finally {
    writer.pollBusy = false;
    schedulePipelinePolling();
  }
}

function beginPipelineWatch(pipeline) {
  writer.pipeline = pipeline || null;
  writer.streamTaskId = String(pipeline?.current_task_id || "");
  writer.streamText = "";
  writer.taskCursor = 0;
  writer.pipelineTask = null;
  writer.pollToken += 1;
  renderPipeline();
  schedulePipelinePolling();
  renderPageState();
}

// ============================================================
// 生成提交
// ============================================================

function gateByBudget(model) {
  const budget = writer.budget;
  if (!budget) return true;
  if (budget.context_known && budget.fits === false) {
    toast((budget.suggestions || []).join(" ") || "本次输入超过模型窗口，请减少设定或前文引用后再生成");
    return false;
  }
  if (!budget.context_known && Number(budget.estimated_tokens || 0) > 60000) {
    return window.confirm(
      `当前容量未知，本次输入估算约 ${budget.estimated_tokens} tokens，可能超出模型窗口。\n确认继续提交吗？`,
    );
  }
  return true;
}

async function submitPipeline(kind, { chapterNo = 0, allowDrop = false } = {}) {
  if (!writer.bookId) return;
  if (writer.pipeline?.status === "running") {
    toast("这部作品已有正在进行的生成，请先取消或等它结束");
    return;
  }
  const modelFields = PLAN_MODEL_FIELDS[writer.stage] || ["setupProviderKey", "setupModelName"];
  const model = readModel(...modelFields);
  if (!model.model_name) {
    toast("先在设置里检查模型，再回来选择");
    return;
  }
  if (!gateByBudget(model)) return;
  setBusy(true);
  try {
    const payload = await api(`/api/writer/books/${encodeURIComponent(writer.bookId)}/pipelines`, {
      method: "POST",
      body: {
        kind,
        chapter_no: chapterNo || undefined,
        model_key: model.model_key,
        model_name: model.model_name,
        char_count: writer.charCount,
        chapter_count: writer.stage === "setup" ? writer.chapterCount : writer.outlineChapterCount,
        extra_note: String(
          writer.stage === "setup" ? $("#writerSetupNote")?.value || ""
            : writer.stage === "outline" ? $("#writerOutlineNote")?.value || ""
              : $("#writerDraftNote")?.value || ""
        ),
        allow_drop: allowDrop || undefined,
        revision: writer.book?.revision,
        chapter_revision: chapterNo ? writer.chapter?.revision : undefined,
      },
    });
    beginPipelineWatch(payload.pipeline);
    setSaveState("已提交", `${kindLabel(kind)}已提交；每完成一步都会自动存一版。`);
    toast("已提交生成");
  } catch (error) {
    fail("提交生成失败", error);
  } finally {
    setBusy(false);
    renderAll();
  }
}

async function cancelPipeline() {
  if (!writer.bookId || writer.pipeline?.status !== "running") return;
  try {
    const payload = await api(`/api/writer/books/${encodeURIComponent(writer.bookId)}/pipeline/cancel`, {
      method: "POST", body: {},
    });
    writer.pipeline = payload.pipeline || writer.pipeline;
    toast("已取消；已完成的步骤保留");
    renderPipeline();
  } catch (error) {
    fail("取消失败", error);
  }
}

async function retryPipeline() {
  if (!writer.bookId || !writer.pipeline) return;
  const status = String(writer.pipeline.status || "");
  if (!["failed", "cancelled", "interrupted", "conflict"].includes(status)) return;
  let rebase = false;
  if (status === "conflict") {
    rebase = window.confirm(
      "作品或章节在生成期间被修改过（版本冲突）。\n要基于当前最新版本重新生成本步吗？",
    );
    if (!rebase) return;
  }
  try {
    const payload = await api(`/api/writer/books/${encodeURIComponent(writer.bookId)}/pipeline/retry`, {
      method: "POST", body: { rebase: rebase || undefined },
    });
    beginPipelineWatch(payload.pipeline);
    toast("已重试本步");
  } catch (error) {
    fail("重试失败", error);
  }
}

// ============================================================
// 作品：打开 / 新建 / 重命名 / 删除
// ============================================================

async function loadBooks() {
  try {
    const payload = await api("/api/writer/books");
    return payload?.books || [];
  } catch (error) {
    fail("读取作品列表失败", error);
    return [];
  }
}

async function loadLocation() {
  try {
    writer.location = await api("/api/writer/location");
  } catch (_error) {
    writer.location = null;
  }
  renderLocation();
}

function renderLocation() {
  const noteEl = $("#writerLocationNote");
  const pathEl = $("#writerLocationPath");
  const copy = $("#writerCopyPath");
  const open = $("#writerOpenFolder");
  if (!pathEl) return;
  const root = String(writer.location?.writer_root || "");
  if (!root) {
    if (noteEl) noteEl.textContent = "打开作品后，这里显示这台电脑上的实际保存目录。";
    pathEl.hidden = true;
    if (copy) copy.hidden = true;
    if (open) open.hidden = true;
    return;
  }
  if (noteEl) noteEl.textContent = "全部原创作品都保存在下面这个目录里，每本书一个子目录。";
  pathEl.hidden = false;
  pathEl.textContent = root;
  if (copy) {
    copy.hidden = false;
    copy.disabled = !writer.location?.can_open_folder && !root;
  }
  if (open) open.hidden = !writer.location?.can_open_folder;
}

async function openBook(bookId, { keepEdits = false, keepStage = false } = {}) {
  const token = ++writer.bookToken;
  try {
    const payload = await api(`/api/writer/books/${encodeURIComponent(bookId)}`);
    if (token !== writer.bookToken) return;
    writer.bookId = bookId;
    writer.book = payload.book;
    writer.setup = payload.setup;
    writer.outline = payload.outline;
    writer.chapters = payload.chapters || [];
    writer.broken = payload.broken || [];
    writer.pipeline = payload.book?.active_pipeline || null;
    if (!keepEdits) {
      writer.setupDirty = false;
      writer.draftDirty = false;
    }
    if (!keepStage) writer.stage = "setup";
    // 活动流水线的流式缓冲与新作品/新状态对齐。
    writer.streamTaskId = String(writer.pipeline?.current_task_id || "");
    writer.streamText = "";
    writer.taskCursor = 0;
    writer.pipelineTask = null;
    try { localStorage.setItem(LAST_BOOK_KEY, bookId); } catch (_error) { /* storage may be unavailable */ }
    setSaveState("已打开", `《${writer.book?.title || ""}》已打开（第 ${writer.book?.revision ?? "?"} 版）。`);
    renderAll();
    schedulePipelinePolling();
    // 打开的章节还在作品里就重新取一份（版本可能已更新）；不在了就清掉。
    if (writer.chapterNo && !writer.chapters.some((row) => Number(row.no) === writer.chapterNo)) {
      writer.chapter = null;
      writer.chapterNo = 0;
    }
    if (writer.chapterNo && writer.stage === "draft" && !writer.draftDirty) {
      await openChapter(writer.chapterNo, { silent: true });
    }
    void refreshPlan();
  } catch (error) {
    fail("打开作品失败", error);
  }
}

async function createBook() {
  const input = $("#writerNewTitle");
  const title = String(input?.value || "").trim();
  setBusy(true);
  try {
    const payload = await api("/api/writer/books", {
      method: "POST",
      body: { title: title || undefined },
    });
    const book = payload.book;
    if (input) input.value = "";
    closeDialog("#writerBooksDialog");
    toast(`已新建《${book.title}》`);
    await openBook(book.id);
    setStage("setup");
  } catch (error) {
    const errorEl = $("#writerBooksError");
    if (errorEl) errorEl.textContent = String(error?.message || error);
    fail("新建作品失败", error);
  } finally {
    setBusy(false);
  }
}

async function renameBook(bookId, oldTitle) {
  const title = window.prompt("改成什么书名？（最长 200 字）", oldTitle || "");
  if (title === null) return;
  const clean = String(title).trim();
  if (!clean) {
    toast("书名不能为空");
    return;
  }
  try {
    await api(`/api/writer/books/${encodeURIComponent(bookId)}`, {
      method: "PATCH",
      body: { title: clean, revision: writer.bookId === bookId ? writer.book?.revision : undefined },
    });
    toast("已重命名");
    if (writer.bookId === bookId) await openBook(bookId, { keepEdits: true, keepStage: true });
    else await renderBooksDialog();
  } catch (error) {
    fail("重命名失败", error);
  }
}

async function deleteBook(bookId, title) {
  if (!window.confirm(
    `要删除《${title || "未命名作品"}》吗？\n设定、大纲与全部章节正文都会一并删除，这个操作不能撤销。`,
  )) return;
  try {
    await api(`/api/writer/books/${encodeURIComponent(bookId)}`, { method: "DELETE" });
    toast("已删除作品");
    if (writer.bookId === bookId) {
      writer.bookId = "";
      writer.book = null;
      writer.setup = null;
      writer.outline = null;
      writer.chapters = [];
      writer.chapter = null;
      writer.chapterNo = 0;
      writer.pipeline = null;
      stopPipelinePolling();
      try { localStorage.removeItem(LAST_BOOK_KEY); } catch (_error) { /* storage may be unavailable */ }
      renderAll();
    }
    await renderBooksDialog();
  } catch (error) {
    fail("删除作品失败", error);
  }
}

async function renderBooksDialog() {
  const list = $("#writerBookList");
  const errorEl = $("#writerBooksError");
  if (!list) return;
  if (errorEl) errorEl.textContent = "";
  list.replaceChildren(Object.assign(document.createElement("p"), {
    className: "writer-empty",
    textContent: "正在读取…",
  }));
  const token = ++writer.booksToken;
  try {
    const books = await loadBooks();
    if (token !== writer.booksToken) return;
    if (!books.length) {
      list.replaceChildren(Object.assign(document.createElement("p"), {
        className: "writer-empty",
        textContent: "还没有原创作品。上面输入书名点「新建」，或点「导入存档」从启元存档导入。",
      }));
      return;
    }
    list.replaceChildren(...books.map((book) => {
      const row = document.createElement("div");
      row.className = "writer-book-row";
      row.classList.toggle("active", book.id === writer.bookId);
      const open = document.createElement("button");
      open.type = "button";
      open.className = "writer-book-open";
      open.dataset.writerOpen = book.id;
      const strong = document.createElement("strong");
      strong.textContent = `《${book.title}》`;
      const small = document.createElement("small");
      small.textContent = `${book.chapter_count} 章 · ${fmtTime(book.updated_at)}`;
      open.append(strong, small);
      const actions = document.createElement("span");
      actions.className = "writer-book-row-actions";
      const rename = document.createElement("button");
      rename.type = "button";
      rename.className = "control-button tiny";
      rename.dataset.writerRenameBook = book.id;
      rename.dataset.writerRenameTitle = book.title;
      rename.textContent = "重命名";
      const del = document.createElement("button");
      del.type = "button";
      del.className = "control-button tiny danger";
      del.dataset.writerDeleteBook = book.id;
      del.dataset.writerDeleteTitle = book.title;
      del.textContent = "删除";
      actions.append(rename, del);
      row.append(open, actions);
      return row;
    }));
  } catch (error) {
    if (token !== writer.booksToken) return;
    if (errorEl) errorEl.textContent = String(error?.message || error);
    list.replaceChildren();
  }
}

// ============================================================
// 章节（正文阶段）
// ============================================================

async function openChapter(chapterNo, { silent = false } = {}) {
  if (!writer.bookId) return;
  const token = ++writer.chapterToken;
  try {
    const payload = await api(
      `/api/writer/books/${encodeURIComponent(writer.bookId)}/chapters/${Number(chapterNo)}`,
    );
    if (token !== writer.chapterToken) return;
    writer.chapterNo = Number(chapterNo);
    writer.chapter = payload;
    writer.draftDirty = false;
    renderChapters();
    renderDraft();
    void refreshPlan();
  } catch (error) {
    if (!silent) fail("读取章节失败", error);
  }
}

async function saveChapterEdit() {
  if (!writer.bookId || !writer.chapter) return;
  const content = String($("#writerDraftContent")?.value || "");
  if (!content.trim()) {
    toast("正文是空的；要清空请删章节，不要保存空稿");
    return;
  }
  setBusy(true);
  try {
    const payload = await api(
      `/api/writer/books/${encodeURIComponent(writer.bookId)}/chapters/${writer.chapter.no}`,
      {
        method: "PATCH",
        body: { content, revision: writer.chapter.revision },
      },
    );
    writer.chapter = { ...writer.chapter, ...payload.chapter_no !== undefined ? payload : {} };
    writer.draftDirty = false;
    toast(payload.duplicate ? "与当前版本相同，未重复存版本" : "已保存为新版本");
    setSaveState("已保存", "正文按新版本保存；旧版本保留，可随时切回。");
    await openChapter(writer.chapter.no, { silent: true });
    await reloadBookIndex();
  } catch (error) {
    if (error?.status === 409) {
      if (window.confirm("这一章在别处被修改过（版本冲突）。\n要放弃你的修改、重新读取最新内容吗？")) {
        writer.draftDirty = false;
        await openChapter(writer.chapter.no, { silent: true });
      }
      return;
    }
    fail("保存失败", error);
  } finally {
    setBusy(false);
    renderDraft();
  }
}

async function selectChapterVersion(versionId) {
  if (!writer.bookId || !writer.chapter || !versionId) return;
  try {
    await api(`/api/writer/books/${encodeURIComponent(writer.bookId)}/versions/select`, {
      method: "POST",
      body: {
        kind: "chapter",
        chapter_no: writer.chapter.no,
        version_id: versionId,
        revision: writer.chapter.revision,
      },
    });
    writer.draftDirty = false;
    toast("已切换版本");
    await openChapter(writer.chapter.no, { silent: true });
    await reloadBookIndex();
  } catch (error) {
    if (error?.status === 409) {
      toast("这一章在别处被修改过；已自动重读最新内容");
      await openChapter(writer.chapter.no, { silent: true });
      return;
    }
    fail("切换版本失败", error);
  }
}

async function deleteChapter() {
  if (!writer.bookId || !writer.chapter) return;
  if (!window.confirm(
    `要删除第 ${writer.chapter.no} 章（含全部正文版本）吗？这个操作不能撤销。`,
  )) return;
  try {
    await api(
      `/api/writer/books/${encodeURIComponent(writer.bookId)}/chapters/${writer.chapter.no}`,
      { method: "DELETE" },
    );
    toast("已删除章节");
    writer.chapter = null;
    writer.chapterNo = 0;
    await reloadBookIndex();
    renderAll();
  } catch (error) {
    fail("删除章节失败", error);
  }
}

/** 只重读作品索引（目录、版本指针），不动当前打开章节与未保存编辑。 */
async function reloadBookIndex() {
  if (!writer.bookId) return;
  const token = writer.bookToken;
  try {
    const payload = await api(`/api/writer/books/${encodeURIComponent(writer.bookId)}`);
    if (token !== writer.bookToken) return;
    writer.book = payload.book;
    writer.setup = payload.setup;
    writer.outline = payload.outline;
    writer.chapters = payload.chapters || [];
    writer.pipeline = payload.book?.active_pipeline || writer.pipeline;
    renderBookSummary();
    renderChapters();
    if (writer.stage === "outline") { renderOutline(); renderOutlineVersions(); }
  } catch (_error) {
    // 索引刷新失败不阻断主流程；下一次操作会再读。
  }
}

// ============================================================
// 设定与大纲的人工保存
// ============================================================

async function saveSetupManual() {
  if (!writer.bookId) return;
  const setup = collectSetup();
  if (!String(setup.idea || "").trim() && !String(setup.title || "").trim()
    && !String(setup.worldview || "").trim() && !setup.characters.length) {
    toast("设定还是空的；先写点内容或点「生成整套设定」");
    return;
  }
  setBusy(true);
  try {
    const payload = await api(`/api/writer/books/${encodeURIComponent(writer.bookId)}`, {
      method: "PATCH",
      body: { setup, revision: writer.book?.revision },
    });
    writer.setup = payload.setup;
    writer.book = payload.book;
    writer.setupDirty = false;
    toast("设定已保存为新版本");
    setSaveState("已保存", "设定按新版本保存；旧版本保留，可在「管理记录」里切回。");
    renderAll();
    void refreshPlan();
  } catch (error) {
    if (error?.status === 409) {
      if (window.confirm("作品在别处被修改过（版本冲突）。\n要重新读取最新内容吗？（你的表单修改会丢失）")) {
        writer.setupDirty = false;
        await openBook(writer.bookId, { keepStage: true });
      }
      return;
    }
    fail("保存设定失败", error);
  } finally {
    setBusy(false);
  }
}

async function saveOutlineManual() {
  if (!writer.bookId) return;
  const chapters = collectOutline();
  if (!chapters.length) {
    toast("大纲是空的；先生成或填写章节");
    return;
  }
  await saveOutlineRows(chapters, false);
}

async function saveOutlineRows(chapters, allowDrop) {
  setBusy(true);
  try {
    const payload = await api(`/api/writer/books/${encodeURIComponent(writer.bookId)}`, {
      method: "PATCH",
      body: {
        outline: chapters,
        revision: writer.book?.revision,
        sync: true,
        allow_drop: allowDrop || undefined,
      },
    });
    writer.outline = { ...(writer.outline || {}), ...payload.outline_version_id ? {} : {}, chapters };
    writer.book = payload.book;
    toast("大纲已保存为新版本，章节目录已同步");
    setSaveState("已保存", "大纲按新版本保存，章节目录已按它重建（已有正文保留）。");
    await reloadBookIndex();
    renderOutline();
    void refreshPlan();
  } catch (error) {
    const message = String(error?.message || error || "");
    if (message.includes("已有正文，大纲里却没有它")) {
      if (window.confirm(`${message}\n\n确认删除这些章节（含正文）并继续保存吗？`)) {
        await saveOutlineRows(chapters, true);
        return;
      }
      return;
    }
    if (error?.status === 409) {
      if (window.confirm("作品在别处被修改过（版本冲突）。\n要重新读取最新内容吗？（你的大纲修改会丢失）")) {
        await openBook(writer.bookId, { keepStage: true });
      }
      return;
    }
    fail("保存大纲失败", error);
  } finally {
    setBusy(false);
  }
}

async function syncChapters() {
  if (!writer.bookId) return;
  await syncChaptersRequest(false);
}

async function syncChaptersRequest(allowDrop) {
  setBusy(true);
  try {
    const payload = await api(`/api/writer/books/${encodeURIComponent(writer.bookId)}/sync`, {
      method: "POST",
      body: { revision: writer.book?.revision, allow_drop: allowDrop || undefined },
    });
    writer.book = payload.book;
    toast("章节目录已按当前大纲同步");
    setSaveState("已同步", "章节目录已按当前大纲重建（已有正文保留）。");
    await reloadBookIndex();
    renderAll();
  } catch (error) {
    const message = String(error?.message || error || "");
    if (message.includes("已有正文，大纲里却没有它")) {
      if (window.confirm(`${message}\n\n确认删除这些章节（含正文）并继续吗？`)) {
        await syncChaptersRequest(true);
        return;
      }
      return;
    }
    if (error?.status === 409) {
      toast("作品在别处被修改过；已重新读取");
      await openBook(writer.bookId, { keepEdits: true, keepStage: true });
      return;
    }
    fail("同步章节失败", error);
  } finally {
    setBusy(false);
  }
}

async function selectSetupVersion(versionId) {
  if (!writer.bookId || !versionId) return;
  try {
    const payload = await api(`/api/writer/books/${encodeURIComponent(writer.bookId)}/versions/select`, {
      method: "POST",
      body: { kind: "setup", version_id: versionId, revision: writer.book?.revision },
    });
    writer.book = payload.book;
    writer.setupDirty = false;
    toast("已切换设定版本");
    await openBook(writer.bookId, { keepStage: true });
  } catch (error) {
    if (error?.status === 409) {
      toast("作品在别处被修改过；已重新读取");
      await openBook(writer.bookId, { keepEdits: true, keepStage: true });
      return;
    }
    fail("切换设定版本失败", error);
  }
}

async function selectOutlineVersion(versionId) {
  if (!writer.bookId || !versionId) return;
  try {
    const payload = await api(`/api/writer/books/${encodeURIComponent(writer.bookId)}/versions/select`, {
      method: "POST",
      body: { kind: "outline", version_id: versionId, revision: writer.book?.revision },
    });
    writer.book = payload.book;
    toast("已切换大纲版本；要更新章节目录请点「按当前大纲同步章节」");
    await openBook(writer.bookId, { keepStage: true });
  } catch (error) {
    if (error?.status === 409) {
      toast("作品在别处被修改过；已重新读取");
      await openBook(writer.bookId, { keepEdits: true, keepStage: true });
      return;
    }
    fail("切换大纲版本失败", error);
  }
}

// ============================================================
// 模板弹窗
// ============================================================

async function loadTemplates() {
  if (writer.templates) return writer.templates;
  try {
    const response = await fetch(TEMPLATES_URL);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    writer.templates = {
      channels: Array.isArray(payload?.channels) ? payload.channels : [],
      categories: Array.isArray(payload?.categories) ? payload.categories : [],
      templates: Array.isArray(payload?.templates) ? payload.templates : [],
    };
    return writer.templates;
  } catch (error) {
    fail("读取题材模板失败", error);
    return { channels: [], categories: [], templates: [] };
  }
}

function templateCategories() {
  const data = writer.templates || { categories: [], templates: [] };
  let cats = data.categories;
  if (writer.tplChannel) {
    cats = cats.filter((cat) => String(cat.channel || "") === writer.tplChannel);
  }
  return cats;
}

function filteredTemplates() {
  const data = writer.templates || { categories: [], templates: [] };
  const query = writer.tplQuery.trim().toLowerCase();
  const cat = writer.tplCat;
  let rows = data.templates;
  if (writer.tplChannel) {
    rows = rows.filter((row) => {
      const own = String(row.gender || "") === (writer.tplChannel === "male" ? "男频" : "女频");
      const viaCat = templateCategories().some((c) => c.key === row.cat);
      return own || viaCat;
    });
  }
  if (cat) rows = rows.filter((row) => row.cat === cat);
  if (query) {
    rows = rows.filter((row) => {
      const haystack = [row.name, row.desc, row.structure, row.hook, ...(row.tags || [])]
        .map((item) => String(item || "").toLowerCase());
      return haystack.some((text) => text.includes(query));
    });
  }
  return rows;
}

function renderTemplateDialog() {
  const catSelect = $("#writerTplCat");
  if (catSelect) {
    const cats = templateCategories();
    catSelect.replaceChildren(...[
      new Option("全部分类", ""),
      ...cats.map((cat) => new Option(String(cat.name || cat.key), String(cat.key))),
    ]);
    catSelect.value = cats.some((cat) => cat.key === writer.tplCat) ? writer.tplCat : "";
  }
  const count = $("#writerTplCount");
  const rows = filteredTemplates();
  if (count) count.textContent = `共 ${rows.length} 条`;
  renderTemplateList(rows, 12);
}

function renderTemplateList(rows, limit) {
  const list = $("#writerTplList");
  if (!list) return;
  if (!rows.length) {
    list.replaceChildren(Object.assign(document.createElement("p"), {
      className: "writer-empty",
      textContent: "这个筛选下没有模板；换个分类或搜索词试试。",
    }));
    return;
  }
  const shown = typeof limit === "number" ? rows.slice(0, limit) : rows;
  list.replaceChildren(...shown.map((row) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "writer-tpl-card";
    card.classList.toggle("active", row.id === writer.tplSelectedId);
    card.dataset.writerTplId = String(row.id || "");
    const strong = document.createElement("strong");
    strong.textContent = String(row.name || "未命名模板");
    const small = document.createElement("small");
    const bits = [String(row.gender || ""), String(row.sub || ""), String(row.structure || "")]
      .filter(Boolean);
    small.textContent = bits.join(" · ") || String(row.desc || "").slice(0, 40);
    const tags = document.createElement("span");
    tags.className = "writer-tpl-tags";
    tags.textContent = (row.tags || []).slice(0, 4).join(" / ");
    card.append(strong, small);
    if ((row.tags || []).length) card.append(tags);
    return card;
  }));
}

function renderTemplateDetail() {
  const detail = $("#writerTplDetail");
  if (!detail) return;
  const row = (writer.templates?.templates || []).find((item) => item.id === writer.tplSelectedId);
  detail.hidden = !row;
  if (!row) return;
  const name = $("#writerTplDetailName");
  if (name) name.textContent = String(row.name || "模板");
  const meta = $("#writerTplDetailMeta");
  if (meta) {
    meta.textContent = [String(row.gender || ""), String(row.sub || ""), String(row.structure || "")]
      .filter(Boolean).join(" · ");
  }
  const body = $("#writerTplDetailBody");
  if (body) {
    const sections = [];
    if (String(row.desc || "").trim()) sections.push(`【简介】\n${row.desc}`);
    if (String(row.prompt || "").trim()) sections.push(`【创意】\n${row.prompt}`);
    if (String(row.worldview || "").trim()) sections.push(`【世界观】\n${row.worldview}`);
    if (String(row.characters || "").trim()) sections.push(`【角色】\n${row.characters}`);
    if (String(row.outline || "").trim()) sections.push(`【大纲骨架】\n${row.outline}`);
    body.textContent = sections.join("\n\n") || "（模板没有内容）";
  }
}

/** 模板字符角色串 → 角色卡数组：「主角沈砚：隐忍坚毅…；引路人玄露长老：…」。
 *  只按分隔符与第一处冒号做机械切分，切不出来的整段丢弃（不猜名字）。 */
function parseTemplateCharacters(text) {
  const rows = [];
  for (const piece of String(text || "").split(/[；;]/)) {
    const clean = piece.trim();
    if (!clean) continue;
    const sepIndex = clean.search(/[：:]/);
    if (sepIndex <= 0) continue;
    const name = clean.slice(0, sepIndex).trim();
    const identity = clean.slice(sepIndex + 1).trim();
    if (!name || !identity) continue;
    rows.push({
      name: name.slice(0, 60),
      role: "",
      identity: identity.slice(0, 200),
      goal: "", traits: "", speech: "", arc: "", background: "",
    });
    if (rows.length >= 10) break;
  }
  return rows;
}

function applyTemplate() {
  const row = (writer.templates?.templates || []).find((item) => item.id === writer.tplSelectedId);
  if (!row) {
    toast("先在列表里选一个模板");
    return;
  }
  // 只改状态、不直接改输入框：setStage 里的 fillSetupForm 以 writer.setup 为唯一事实源，
  // 先写状态再重渲染，表单值才不会被旧状态盖回去。
  writer.setup = {
    ...(writer.setup || {}),
    idea: String(row.prompt || ""),
    title: String(row.title || "").replace(/^《|》$/g, ""),
    blurb: String(row.blurb || ""),
    worldview: String(row.worldview || ""),
    genre: { ...(writer.setup?.genre || {}), cat: String(row.sub || "") },
    genre_tags: (Array.isArray(row.tags) ? row.tags : []).map((tag) => String(tag || "").trim()).filter(Boolean).slice(0, 5),
    characters: parseTemplateCharacters(row.characters),
  };
  writer.setupDirty = true;
  closeDialog("#writerTemplateDialog");
  setStage("setup");
  toast(`已套用模板「${row.name}」；确认内容后点「保存修改为新版本」入库`);
}

// ============================================================
// 导入 / 导出
// ============================================================

async function importArchive(file) {
  if (!file) return;
  if (file.size > 20 * 1024 * 1024) {
    toast(`文件太大（${(file.size / 1024 / 1024).toFixed(1)} MB），上限 20 MB`);
    return;
  }
  setBusy(true);
  setSaveState("正在导入", `正在解析「${file.name}」，请稍候。`);
  try {
    const form = new FormData();
    form.append("file", file);
    form.append("filename", file.name);
    const payload = await api("/api/writer/import", { method: "POST", body: form });
    writer.importId = String(payload.import_id || "");
    writer.importPreview = payload;
    renderImportPreview();
    openDialog("#writerImportDialog");
  } catch (error) {
    fail("导入失败", error);
    setSaveState("导入失败", String(error?.message || error));
  } finally {
    setBusy(false);
    renderAll();
  }
}

function renderImportPreview() {
  const box = $("#writerImportPreview");
  if (!box) return;
  const preview = writer.importPreview;
  if (!preview) {
    box.replaceChildren();
    return;
  }
  const rows = [
    ["书名", preview.title || "未命名"],
    ["题材", preview.genre || "未识别"],
    ["创意字数", String(preview.idea_len || 0)],
    ["角色", `${preview.character_count || 0} 人`],
    ["大纲章节", `${preview.outline_count || 0} 章`],
    ["正文章节", `${preview.chapter_count || 0} 章`],
    ["只按梗概处理", `${preview.sketch_only_count || 0} 章（正文与梗概相同）`],
  ];
  const kv = document.createElement("div");
  kv.className = "writer-kv";
  for (const [key, value] of rows) {
    const span = document.createElement("span");
    span.textContent = key;
    const b = document.createElement("b");
    b.textContent = value;
    kv.append(span, b);
  }
  const parts = [kv];
  const warnings = Array.isArray(preview.warnings) ? preview.warnings : [];
  if (warnings.length) {
    const card = document.createElement("div");
    card.className = "writer-note-card writer-warn-card";
    const title = document.createElement("strong");
    title.textContent = "导入提示";
    const p = document.createElement("p");
    p.textContent = warnings.join("；");
    card.append(title, p);
    parts.push(card);
  }
  const noteEl = document.createElement("p");
  noteEl.className = "writer-hint";
  noteEl.textContent = "确认后会新建一部原创作品；原存档文件不会被修改，设置与密钥不会迁移。";
  parts.push(noteEl);
  box.replaceChildren(...parts);
}

async function commitImport() {
  if (!writer.importId) return;
  setBusy(true);
  try {
    const payload = await api(`/api/writer/import/${encodeURIComponent(writer.importId)}/commit`, {
      method: "POST", body: {},
    });
    writer.importId = "";
    writer.importPreview = null;
    closeDialog("#writerImportDialog");
    toast(`已导入《${payload.book?.title || ""}》`);
    await openBook(payload.book?.id);
  } catch (error) {
    const errorEl = $("#writerImportError");
    if (errorEl) errorEl.textContent = String(error?.message || error);
    fail("导入入库失败", error);
  } finally {
    setBusy(false);
  }
}

async function exportBook(fmt = "txt", withExtras = false) {
  if (!writer.bookId) return;
  // 字符类里的 `/` 要写成 `\/`：esm_graph_check 的正则扫描按「第一个未转义斜杠」收尾，
  // 类内裸斜杠会让它提前闭合、引号配对错位，后面的字符串被当成代码（误报 import 漏引）。
  const safeTitle = (writer.book?.title || "作品").replace(/[\\\/:*?"<>|]/g, "_");
  const name = `${safeTitle}.${fmt}`;
  try {
    const query = new URLSearchParams({ format: fmt });
    if (withExtras) query.set("extras", "1");
    const payload = await api(
      `/api/writer/books/${encodeURIComponent(writer.bookId)}/export?${query.toString()}`,
    );
    const filename = String(payload?.filename || name);
    const mime = fmt === "md" ? "text/markdown;charset=utf-8" : "text/plain;charset=utf-8";
    const missing = Array.isArray(payload?.missing_chapters) ? payload.missing_chapters : [];
    // triggerDownload 只接受 URL；这里落的是内存文本，直接造临时 blob 链接。
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
      `已交给浏览器下载「${filename}」（${fmt.toUpperCase()}${withExtras ? "，含设定与大纲" : "，全书"}）。`
      + (missing.length ? `注意：第 ${missing.slice(0, 8).join("、")} 章没有正文，未包含在文件里。` : ""));
  } catch (error) {
    fail("导出失败", error);
  }
}

// ============================================================
// 管理记录
// ============================================================

function manageRowLabel(row) {
  if (writer.manage.tab === "setup") {
    return `v${row.version} · ${versionKindLabel(row.kind)} · ${String(row.title || "无书名")}`;
  }
  if (writer.manage.tab === "outline") {
    return `v${row.version} · ${versionKindLabel(row.kind)} · ${row.chapter_count} 章`;
  }
  if (writer.manage.tab === "task") {
    return String(row.title || "未命名任务");
  }
  return String(row.title || `第 ${row.no} 章`);
}

function manageRowMeta(row) {
  const bits = [];
  if (writer.manage.tab === "setup") bits.push(`${fmtChars(row.len)} 字`);
  if (writer.manage.tab === "outline") bits.push(`${fmtChars(row.len)} 字`);
  if (writer.manage.tab === "task") {
    bits.push(TASK_STATUS_LABELS[String(row.status || "")] || row.status || "状态未知");
  }
  bits.push(fmtTime(row.updated_at || row.created_at));
  return bits.join(" · ");
}

function filteredManageRows() {
  const tab = writer.manage.tab;
  const query = writer.manage.filter.trim().toLowerCase();
  let rows = [];
  if (tab === "setup") rows = Array.isArray(writer.book?.setup_versions) ? writer.book.setup_versions : [];
  else if (tab === "outline") rows = Array.isArray(writer.book?.outline_versions) ? writer.book.outline_versions : [];
  else if (tab === "chapter") rows = writer.chapters.filter((row) => Number(row.version_count || 0) > 0);
  else rows = (writer.manage.records || []).filter((row) => {
    const owner = String(row.writer?.book_id || "");
    return !writer.bookId || owner === writer.bookId;
  });
  if (query) rows = rows.filter((row) => manageRowLabel(row).toLowerCase().includes(query));
  return rows;
}

function manageRowSelectable(row) {
  return writer.manage.tab === "task";
}

function renderManage() {
  const tabs = document.querySelectorAll("#writerManageTabs [data-writer-manage-tab]");
  tabs.forEach((btn) => {
    const active = btn.dataset.writerManageTab === writer.manage.tab;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
  const list = $("#writerManageList");
  const noteEl = $("#writerManageNote");
  if (!list) return;
  const scope = $("#writerManageScope");
  if (scope) scope.textContent = writer.bookId ? `《${writer.book?.title || ""}》` : "先打开作品";
  if (noteEl) {
    noteEl.textContent = {
      setup: "设定版本逐条保留；「设为当前」立即切换，内容不会丢失。",
      outline: "大纲版本逐条保留；切换后要点大纲页的「按当前大纲同步章节」才会更新章节目录。",
      chapter: "点一章展开它的正文版本；「设为当前」立即切换正文。",
      task: "织书台这一本作品的生成记录；删除只清任务记录，不删已保存的内容。",
    }[writer.manage.tab] || "";
  }
  if (!writer.bookId) {
    list.replaceChildren(Object.assign(document.createElement("p"), {
      className: "writer-empty",
      textContent: "先打开一部作品。",
    }));
    return;
  }
  const rows = filteredManageRows();
  if (!rows.length) {
    list.replaceChildren(Object.assign(document.createElement("p"), {
      className: "writer-empty",
      textContent: writer.manage.tab === "task"
        ? "这一本还没有生成记录。"
        : "还没有记录。",
    }));
  } else {
    list.replaceChildren(...rows.map((row) => {
      const key = writer.manage.tab === "task"
        ? `task:${row.id}`
        : writer.manage.tab === "chapter"
          ? `chapter:${row.no}`
          : `version:${row.id}`;
      const item = document.createElement("div");
      item.className = "writer-manage-row";
      if (writer.manage.tab === "chapter") item.classList.toggle("expanded", writer.manage.expandedChapter === Number(row.no));
      const check = document.createElement("input");
      check.type = "checkbox";
      check.dataset.writerManageCheck = key;
      check.checked = writer.manage.selected.has(key);
      check.disabled = !manageRowSelectable(row);
      check.hidden = !manageRowSelectable(row);
      const open = document.createElement("button");
      open.type = "button";
      open.className = "writer-manage-open";
      open.dataset.writerManageOpen = key;
      const strong = document.createElement("strong");
      strong.textContent = manageRowLabel(row);
      const small = document.createElement("small");
      small.textContent = manageRowMeta(row);
      open.append(strong, small);
      item.append(check, open);
      const currentId = writer.manage.tab === "setup"
        ? String(writer.book?.current_setup_version_id || "")
        : writer.manage.tab === "outline"
          ? String(writer.book?.current_outline_version_id || "")
          : "";
      if (currentId && row.id === currentId) {
        const badge = document.createElement("span");
        badge.className = "writer-pill ok";
        badge.textContent = "当前";
        item.append(badge);
      } else if (writer.manage.tab === "setup" || writer.manage.tab === "outline") {
        const use = document.createElement("button");
        use.type = "button";
        use.className = "control-button tiny";
        use.dataset.writerManageUse = key;
        use.textContent = "设为当前";
        item.append(use);
      } else if (writer.manage.tab === "chapter") {
        const count = document.createElement("span");
        count.className = "writer-kv-line";
        count.textContent = `${row.version_count} 个版本`;
        item.append(count);
      }
      return item;
    }));
  }
  // 展开章的版本子列表
  if (writer.manage.tab === "chapter" && writer.manage.expandedChapter) {
    void renderChapterVersionRows();
  }
  const selection = $("#writerManageSelection");
  if (selection) {
    selection.textContent = writer.manage.selected.size
      ? `已选 ${writer.manage.selected.size} 条生成记录` : "";
  }
  const del = $("#writerManageDelete");
  if (del) del.disabled = !writer.manage.selected.size;
}

async function renderChapterVersionRows() {
  const list = $("#writerManageList");
  if (!list) return;
  const no = writer.manage.expandedChapter;
  if (!no) return;
  try {
    const payload = await api(
      `/api/writer/books/${encodeURIComponent(writer.bookId)}/chapters/${no}`,
    );
    const current = String(payload.current_version_id || "");
    const holder = document.createElement("div");
    holder.className = "writer-manage-versions";
    holder.dataset.writerManageVersions = String(no);
    if (!(payload.versions || []).length) {
      holder.append(Object.assign(document.createElement("p"), {
        className: "writer-empty", textContent: "这一章还没有正文版本。",
      }));
    }
    for (const row of payload.versions || []) {
      const item = document.createElement("div");
      item.className = "writer-manage-row";
      const open = document.createElement("button");
      open.type = "button";
      open.className = "writer-manage-open";
      open.dataset.writerManageVersionOpen = String(row.id || "");
      open.dataset.writerManageVersionChapter = String(no);
      const strong = document.createElement("strong");
      strong.textContent = `v${row.version} · ${versionKindLabel(row.kind)}`;
      const small = document.createElement("small");
      small.textContent = `${fmtChars(row.len)} 字 · ${fmtTime(row.updated_at)}`;
      open.append(strong, small);
      item.append(open);
      if (row.id === current) {
        const badge = document.createElement("span");
        badge.className = "writer-pill ok";
        badge.textContent = "当前";
        item.append(badge);
      } else {
        const use = document.createElement("button");
        use.type = "button";
        use.className = "control-button tiny";
        use.dataset.writerManageUse = `chapterver:${no}:${row.id}`;
        use.textContent = "设为当前";
        item.append(use);
      }
      holder.append(item);
    }
    const anchor = list.querySelector(`[data-writer-manage-open="chapter:${no}"]`)?.parentElement;
    if (anchor?.nextElementSibling?.dataset?.writerManageVersions) {
      anchor.nextElementSibling.replaceWith(holder);
    } else if (anchor) {
      anchor.after(holder);
    } else {
      list.append(holder);
    }
  } catch (error) {
    fail("读取正文版本失败", error);
  }
}

async function loadManageTasks() {
  if (writer.manage.tab !== "task") return;
  const token = ++writer.manage.token;
  try {
    const payload = await api(WRITER_TASKS_URL);
    if (token !== writer.manage.token) return;
    writer.manage.records = Array.isArray(payload.tasks) ? payload.tasks : [];
  } catch (error) {
    if (token !== writer.manage.token) return;
    writer.manage.records = [];
    fail("读取生成记录失败", error);
  }
  renderManage();
}

async function openManageRecord(key) {
  const [type, ...rest] = key.split(":");
  const head = $("#writerManagePreviewHead");
  const meta = $("#writerManagePreviewMeta");
  const body = $("#writerManagePreviewBody");
  const box = $("#writerManagePreview");
  if (!box || !body) return;
  try {
    if (type === "task") {
      const taskId = rest.join(":");
      const { task } = await api(`/api/studio/tasks/${encodeURIComponent(taskId)}`);
      if (head) head.textContent = String(task.title || "生成记录");
      if (meta) meta.textContent = `${TASK_STATUS_LABELS[String(task.status || "")] || task.status} · ${fmtTime(task.created_at)}`;
      body.textContent = String(task.result || task.result_partial || task.error || "（没有内容）");
    } else if (type === "chapter") {
      writer.manage.expandedChapter = writer.manage.expandedChapter === Number(rest[0]) ? 0 : Number(rest[0]);
      renderManage();
      return;
    } else if (type === "chapterver") {
      const chapterNo = Number(rest[0]);
      const payload = await api(
        `/api/writer/books/${encodeURIComponent(writer.bookId)}/chapters/${chapterNo}`,
      );
      if (head) head.textContent = `第 ${chapterNo} 章 · 版本 v${payload.versions?.find((row) => row.id === rest[1])?.version || "?"}`;
      if (meta) meta.textContent = `${fmtChars(String(payload.content || "").length)} 字（当前版本内容）`;
      body.textContent = String(payload.content || "（这一版不是当前版本；设为当前后可在此查看全文）");
    } else {
      // setup / outline 版本：预览当前生效版本的完整内容（旧版本内容没有读取接口，
      // 切为当前后即可预览——版本内容本身不会丢）。
      if (type === "version" && rest[0] !== String(writer.book?.current_setup_version_id || "")
        && rest[0] !== String(writer.book?.current_outline_version_id || "")) {
        if (head) head.textContent = "版本内容";
        if (meta) meta.textContent = "";
        body.textContent = "（只有当前生效的版本能直接预览全文；点「设为当前」后即可查看，旧版本不会丢失。）";
        box.hidden = false;
        return;
      }
      if (writer.manage.tab === "setup") {
        if (head) head.textContent = "当前设定";
        if (meta) meta.textContent = `${fmtChars(JSON.stringify(writer.setup || {}).length)} 字`;
        body.textContent = renderSetupText(writer.setup || {});
      } else {
        const rows = Array.isArray(writer.outline?.chapters) ? writer.outline.chapters : [];
        if (head) head.textContent = "当前大纲";
        if (meta) meta.textContent = `${rows.length} 章`;
        body.textContent = rows
          .map((row) => `第${row.no}章 ${row.title}${row.beat ? ` —— ${row.beat}` : ""}`)
          .join("\n") || "（大纲是空的）";
      }
    }
    box.hidden = false;
  } catch (error) {
    fail("读取记录失败", error);
  }
}

function renderSetupText(setup) {
  const lines = [];
  if (String(setup.idea || "").trim()) lines.push(`创意：${setup.idea}`);
  const genre = setup.genre || {};
  const bits = [genre.cat, genre.sub_cat, genre.tone].filter((bit) => String(bit || "").trim());
  if (bits.length) lines.push(`题材：${bits.join(" · ")}`);
  if (String(setup.title || "").trim()) lines.push(`书名：${setup.title}`);
  if (String(setup.blurb || "").trim()) lines.push(`简介：${setup.blurb}`);
  if (String(setup.worldview || "").trim()) lines.push(`世界观：${setup.worldview}`);
  if ((setup.genre_tags || []).length) lines.push(`标签：${setup.genre_tags.join("、")}`);
  for (const row of setup.characters || []) {
    if (String(row?.name || "").trim()) {
      lines.push(`角色：${row.name} — ${String(row.identity || "").trim()}`);
    }
  }
  const constraints = setup.constraints || {};
  if (String(constraints.vibe || "").trim()) lines.push(`参考气质：${constraints.vibe}`);
  if (String(constraints.avoid || "").trim()) lines.push(`回避项：${constraints.avoid}`);
  return lines.join("\n\n") || "（设定是空的）";
}

async function deleteManageSelection() {
  const keys = [...writer.manage.selected];
  if (!keys.length) return;
  const taskIds = keys.filter((key) => key.startsWith("task:")).map((key) => key.slice(5));
  if (!taskIds.length) {
    toast("只有「生成记录」可以删除；版本请用切换而不是删除");
    return;
  }
  if (!window.confirm(
    `要删除 ${taskIds.length} 条生成记录吗？\n只删任务记录，不删除从它保存的设定、大纲或正文。`,
  )) return;
  const errorEl = $("#writerManageError");
  if (errorEl) errorEl.textContent = "";
  try {
    await api("/api/studio/tasks", { method: "DELETE", body: { ids: taskIds } });
    writer.manage.selected.clear();
    await loadManageTasks();
    toast("已删除选中的生成记录");
  } catch (error) {
    if (errorEl) errorEl.textContent = String(error?.message || error);
    fail("删除记录失败", error);
  }
}

// ============================================================
// 对话框 / 分档 / 阶段
// ============================================================

function openDialog(selector) {
  const dialog = $(selector);
  if (!dialog) return;
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeDialog(selector) {
  const dialog = $(selector);
  if (!dialog) return;
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

function setStage(stage) {
  const next = ["setup", "outline", "draft"].includes(stage) ? stage : "setup";
  writer.stage = next;
  stageAtRequest = next;
  document.querySelectorAll("#writerStages [data-writer-stage]").forEach((btn) => {
    const active = btn.dataset.writerStage === next;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
  renderEditor();
  renderChapters();
  renderPageState();
  void refreshPlan();
}

// 手机竖屏断点：与 styles.css 的 `@media (max-width: 760px)` 一致。
const PORTRAIT_QUERY = "(max-width: 760px)";

function isPaneLayout() {
  return typeof window.matchMedia === "function" && window.matchMedia(PORTRAIT_QUERY).matches;
}

function setPane(pane) {
  const next = ["book", "editor", "notes"].includes(pane) ? pane : "editor";
  writer.pane = next;
  const columns = $("#writerColumns");
  if (columns) columns.dataset.writerPane = next;
  document.querySelectorAll("#writerPanes [data-writer-pane]").forEach((btn) => {
    const active = btn.dataset.writerPane === next;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
  applyPaneAccessibility();
}

/** 分档布局下，隐藏栏里的控件必须对键盘/读屏不可达（inert），否则会「点不到却还在」。
 *  只切可见栏，不复制状态。 */
function applyPaneAccessibility() {
  const columns = $("#writerColumns");
  if (!columns) return;
  const paned = isPaneLayout();
  for (const panel of columns.querySelectorAll(".writer-panel")) {
    if (!paned) {
      panel.removeAttribute("inert");
      continue;
    }
    const key = panel.classList.contains("writer-book") ? "book"
      : panel.classList.contains("writer-editor") ? "editor" : "notes";
    if (key === writer.pane) panel.removeAttribute("inert");
    else panel.setAttribute("inert", "");
  }
}

function closeExportMenu() {
  const menu = $("#writerExportMenu");
  if (menu) menu.hidden = true;
  $("#writerExportBtn")?.setAttribute("aria-expanded", "false");
}

// ============================================================
// 事件接线
// ============================================================

export function bindWriterEvents() {
  if (writer.initialized) return;
  restoreLimits();
  writer.initialized = true;
  setPane(writer.pane);

  // ---- 页面头 ----
  $("#writerNewBookBtn")?.addEventListener("click", () => {
    $("#writerBooksError").textContent = "";
    void renderBooksDialog();
    openDialog("#writerBooksDialog");
    $("#writerNewTitle")?.focus();
  });
  $("#writerBooksBtn")?.addEventListener("click", () => {
    $("#writerBooksError").textContent = "";
    void renderBooksDialog();
    openDialog("#writerBooksDialog");
  });
  $("#writerCreateBtn")?.addEventListener("click", () => void createBook());
  $("#writerNewTitle")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      void createBook();
    }
  });
  $("#writerBooksClose")?.addEventListener("click", () => closeDialog("#writerBooksDialog"));
  $("#writerBookList")?.addEventListener("click", (event) => {
    const open = event.target.closest("[data-writer-open]");
    if (open) {
      closeDialog("#writerBooksDialog");
      void openBook(open.dataset.writerOpen);
      return;
    }
    const rename = event.target.closest("[data-writer-rename-book]");
    if (rename) {
      void renameBook(rename.dataset.writerRenameBook, rename.dataset.writerRenameTitle);
      return;
    }
    const del = event.target.closest("[data-writer-delete-book]");
    if (del) void deleteBook(del.dataset.writerDeleteBook, del.dataset.writerDeleteTitle);
  });
  $("#writerImportBtn")?.addEventListener("click", () => $("#writerImportFile")?.click());
  $("#writerImportFile")?.addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file) void importArchive(file);
  });
  $("#writerImportCommit")?.addEventListener("click", () => void commitImport());
  $("#writerImportCancel")?.addEventListener("click", () => {
    writer.importId = "";
    writer.importPreview = null;
    closeDialog("#writerImportDialog");
  });
  $("#writerExportBtn")?.addEventListener("click", () => {
    const menu = $("#writerExportMenu");
    if (!menu) return;
    const open = menu.hidden;
    if (open) renderExportNote();
    menu.hidden = !open;
    $("#writerExportBtn")?.setAttribute("aria-expanded", String(open));
  });
  $("#writerExportMenu")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-writer-export]");
    if (!btn) return;
    closeExportMenu();
    const kind = btn.dataset.writerExport;
    if (kind === "md-extras") void exportBook("md", true);
    else void exportBook(kind === "md" ? "md" : "txt", false);
  });
  document.addEventListener("click", (event) => {
    const menu = $("#writerExportMenu");
    if (!menu || menu.hidden) return;
    if (event.target.closest("#writerExportMenu, #writerExportBtn")) return;
    closeExportMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const menu = $("#writerExportMenu");
    if (menu && !menu.hidden) {
      closeExportMenu();
      $("#writerExportBtn")?.focus();
    }
  });
  $("#writerManageBtn")?.addEventListener("click", () => {
    writer.manage.tab = "setup";
    writer.manage.selected.clear();
    writer.manage.expandedChapter = 0;
    renderManage();
    openDialog("#writerManageDialog");
    if (writer.manage.tab === "task") void loadManageTasks();
  });
  $("#writerManageClose")?.addEventListener("click", () => closeDialog("#writerManageDialog"));

  // ---- 阶段与分档 ----
  $("#writerStages")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-writer-stage]");
    if (btn) setStage(btn.dataset.writerStage);
  });
  $("#writerPanes")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-writer-pane]");
    if (btn) setPane(btn.dataset.writerPane);
  });
  if (typeof window.matchMedia === "function") {
    const query = window.matchMedia(PORTRAIT_QUERY);
    const onChange = () => applyPaneAccessibility();
    if (typeof query.addEventListener === "function") query.addEventListener("change", onChange);
    else if (typeof query.addListener === "function") query.addListener(onChange);
  }

  // ---- 目录 ----
  $("#writerChapterList")?.addEventListener("click", (event) => {
    const row = event.target.closest("[data-writer-chapter]");
    if (!row) return;
    setStage("draft");
    void openChapter(Number(row.dataset.writerChapter));
  });

  // ---- 设定表单 ----
  for (const id of ["writerIdea", "writerTitle", "writerBlurb", "writerWorldview", "writerGenreCat",
    "writerGenreTone", "writerVibe", "writerAvoid", "writerTags"]) {
    $(`#${id}`)?.addEventListener("input", () => { writer.setupDirty = true; });
  }
  $("#writerStyle")?.addEventListener("change", (event) => {
    writer.setupDirty = true;
    void applyModelSkinFor(String(event.target.value || ""));
  });
  $("#writerCharAdd")?.addEventListener("click", () => {
    const chars = collectCharacters();
    if (chars.length >= 10) {
      toast("最多 10 个角色");
      return;
    }
    writer.setup = { ...(writer.setup || {}), characters: [...chars, {
      name: "", role: "", identity: "", goal: "", traits: "", speech: "", arc: "", background: "",
    }] };
    writer.setupDirty = true;
    renderCharacters();
  });
  $("#writerCharList")?.addEventListener("click", (event) => {
    const remove = event.target.closest("[data-writer-char-remove]");
    if (!remove) return;
    const index = Number(remove.dataset.writerCharRemove);
    const chars = collectCharacters();
    chars.splice(index, 1);
    writer.setup = { ...(writer.setup || {}), characters: chars };
    writer.setupDirty = true;
    renderCharacters();
  });
  $("#writerSetupClear")?.addEventListener("click", () => {
    if (writer.setupDirty && !window.confirm("设定表单里有未保存的修改，确定清空吗？")) return;
    writer.setup = null;
    writer.setupDirty = false;
    fillSetupForm();
    renderCharacters();
    toast("已清空设定表单（作品里已保存的版本不受影响）");
  });
  $("#writerSetupSave")?.addEventListener("click", () => void saveSetupManual());
  $("#writerSetupGenerate")?.addEventListener("click", () => void submitPipeline("setup_all"));
  for (const id of ["writerCharCount", "writerChapterCount"]) {
    $(`#${id}`)?.addEventListener("input", (event) => {
      const value = Number(event.target.value || 0);
      if (id === "writerCharCount") writer.charCount = value;
      else writer.chapterCount = value;
      const label = $(`#${id}Value`);
      if (label) label.textContent = String(value);
      saveLimits();
    });
  }
  $("#writerSetupNote")?.addEventListener("change", () => void refreshPlan());

  // ---- 模型选择 ----
  for (const [providerId, modelId, providerField, modelField] of [
    ["writerSetupProvider", "writerSetupModel", "setupProviderKey", "setupModelName"],
    ["writerOutlineProvider", "writerOutlineModel", "outlineProviderKey", "outlineModelName"],
    ["writerDraftProvider", "writerDraftModel", "draftProviderKey", "draftModelName"],
  ]) {
    $(`#${providerId}`)?.addEventListener("change", async (event) => {
      writer[providerField] = String(event.target.value || "");
      writer[modelField] = "";
      writer[modelField] = await fillModelSelect($(`#${modelId}`), writer[providerField]);
      void refreshPlan();
    });
    $(`#${modelId}`)?.addEventListener("change", (event) => {
      writer[modelField] = String(event.target.value || "");
      // 换模型也跟随皮肤（与主对话同一条规则：只在自动模式下生效）。
      void applyModelSkinFor(writer[modelField]);
      void refreshPlan();
    });
  }

  // ---- 模板弹窗 ----
  $("#writerTemplateBtn")?.addEventListener("click", async () => {
    await loadTemplates();
    renderTemplateDialog();
    renderTemplateDetail();
    openDialog("#writerTemplateDialog");
  });
  $("#writerTemplateClose")?.addEventListener("click", () => closeDialog("#writerTemplateDialog"));
  $("#writerTplChannels")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-writer-tpl-channel]");
    if (!btn) return;
    writer.tplChannel = String(btn.dataset.writerTplChannel || "");
    document.querySelectorAll("#writerTplChannels [data-writer-tpl-channel]").forEach((chip) => {
      const active = chip === btn;
      chip.classList.toggle("active", active);
      chip.setAttribute("aria-pressed", String(active));
    });
    writer.tplCat = "";
    renderTemplateDialog();
  });
  $("#writerTplCat")?.addEventListener("change", (event) => {
    writer.tplCat = String(event.target.value || "");
    renderTemplateDialog();
  });
  $("#writerTplSearch")?.addEventListener("input", (event) => {
    writer.tplQuery = String(event.target.value || "");
    renderTemplateDialog();
  });
  $("#writerTplReroll")?.addEventListener("click", () => {
    const rows = filteredTemplates();
    const shuffled = [...rows].sort(() => Math.random() - 0.5);
    renderTemplateList(shuffled, 12);
  });
  $("#writerTplList")?.addEventListener("click", (event) => {
    const card = event.target.closest("[data-writer-tpl-id]");
    if (!card) return;
    writer.tplSelectedId = String(card.dataset.writerTplId || "");
    renderTemplateDialog();
    renderTemplateDetail();
  });
  $("#writerTplApply")?.addEventListener("click", () => applyTemplate());

  // ---- 大纲 ----
  $("#writerOutlineGenerate")?.addEventListener("click", () => void submitPipeline("outline"));
  $("#writerSyncBtn")?.addEventListener("click", () => void syncChapters());
  $("#writerOutlineSave")?.addEventListener("click", () => void saveOutlineManual());
  $("#writerOutlineChapterCount")?.addEventListener("input", (event) => {
    writer.outlineChapterCount = Number(event.target.value || 0);
    const label = $("#writerOutlineChapterCountValue");
    if (label) label.textContent = String(writer.outlineChapterCount);
    saveLimits();
  });
  $("#writerOutlineNote")?.addEventListener("change", () => void refreshPlan());
  $("#writerOutlineVersion")?.addEventListener("change", (event) => {
    void selectOutlineVersion(String(event.target.value || ""));
  });

  // ---- 正文 ----
  $("#writerDraftGenerate")?.addEventListener("click", () => void submitPipeline("chapter", { chapterNo: writer.chapterNo }));
  $("#writerDraftSegs")?.addEventListener("click", () => void submitPipeline("chapter_segs", { chapterNo: writer.chapterNo }));
  $("#writerDraftContinue")?.addEventListener("click", () => void submitPipeline("chapter_continue", { chapterNo: writer.chapterNo }));
  $("#writerDraftRewrite")?.addEventListener("click", () => void submitPipeline("chapter_rewrite", { chapterNo: writer.chapterNo }));
  $("#writerDraftPrev")?.addEventListener("click", () => stepChapter(-1));
  $("#writerDraftNext")?.addEventListener("click", () => stepChapter(1));
  $("#writerDraftContent")?.addEventListener("input", () => {
    writer.draftDirty = true;
    renderDraft();
  });
  $("#writerDraftSave")?.addEventListener("click", () => void saveChapterEdit());
  $("#writerDraftReload")?.addEventListener("click", () => {
    writer.draftDirty = false;
    if (writer.chapter) void openChapter(writer.chapter.no, { silent: true });
    else renderDraft();
  });
  $("#writerDraftNote")?.addEventListener("change", () => void refreshPlan());
  $("#writerDraftVersion")?.addEventListener("change", (event) => {
    void selectChapterVersion(String(event.target.value || ""));
  });

  // ---- 流水线 ----
  $("#writerPipelineCancel")?.addEventListener("click", () => void cancelPipeline());
  $("#writerPipelineRetry")?.addEventListener("click", () => void retryPipeline());

  // ---- 管理记录 ----
  $("#writerManageTabs")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-writer-manage-tab]");
    if (!btn) return;
    writer.manage.tab = String(btn.dataset.writerManageTab || "setup");
    writer.manage.selected.clear();
    writer.manage.expandedChapter = 0;
    renderManage();
    if (writer.manage.tab === "task") void loadManageTasks();
  });
  $("#writerManageFilter")?.addEventListener("input", (event) => {
    writer.manage.filter = String(event.target.value || "");
    renderManage();
  });
  $("#writerManageSelectAll")?.addEventListener("click", () => {
    for (const row of filteredManageRows()) {
      if (manageRowSelectable(row)) writer.manage.selected.add(`task:${row.id}`);
    }
    renderManage();
  });
  $("#writerManageClear")?.addEventListener("click", () => {
    writer.manage.selected.clear();
    renderManage();
  });
  $("#writerManageDelete")?.addEventListener("click", () => void deleteManageSelection());
  $("#writerManageList")?.addEventListener("click", (event) => {
    const check = event.target.closest("[data-writer-manage-check]");
    if (check) {
      const key = check.dataset.writerManageCheck;
      if (check.checked) writer.manage.selected.add(key);
      else writer.manage.selected.delete(key);
      renderManage();
      return;
    }
    const use = event.target.closest("[data-writer-manage-use]");
    if (use) {
      const key = String(use.dataset.writerManageUse || "");
      const [type, ...rest] = key.split(":");
      if (type === "chapterver") {
        void selectChapterVersion(rest[1]);
      } else if (writer.manage.tab === "setup") {
        void selectSetupVersion(rest.join(":"));
      } else {
        void selectOutlineVersion(rest.join(":"));
      }
      return;
    }
    const open = event.target.closest("[data-writer-manage-open]");
    if (open) void openManageRecord(String(open.dataset.writerManageOpen || ""));
    const versionOpen = event.target.closest("[data-writer-manage-version-open]");
    if (versionOpen) {
      void openManageRecord(`chapterver:${versionOpen.dataset.writerManageVersionChapter}:${versionOpen.dataset.writerManageVersionOpen}`);
    }
  });

  // ---- 保存位置 ----
  $("#writerCopyPath")?.addEventListener("click", async () => {
    const path = String(writer.location?.writer_root || "");
    if (!path) return;
    try {
      await navigator.clipboard.writeText(path);
      toast("已复制路径");
    } catch (_error) {
      toast("复制失败；路径已显示在下方，可手动选择复制");
    }
  });
  $("#writerOpenFolder")?.addEventListener("click", async () => {
    try {
      await api("/api/writer/reveal", { method: "POST", body: {} });
      toast("已在电脑上打开文件夹");
    } catch (error) {
      fail("打开文件夹失败", error);
    }
  });

  // ---- 功能页切换 ----
  onStudioPageChange((next, detail) => {
    if (detail?.closed || next !== "writer") {
      // 切走就停轮询：页面隐藏后继续轮询只是白耗请求。
      writer.visible = false;
      stopPipelinePolling();
      return;
    }
    writer.visible = true;
    // 模型下拉依赖 bootstrap 的供应商配置：首屏时它可能还没到，进页面时重算一次。
    fillProviderModels();
    void loadLocation();
    if (writer.bookId) void openBook(writer.bookId);
    else {
      let remembered = "";
      try { remembered = localStorage.getItem(LAST_BOOK_KEY) || ""; } catch (_error) { /* storage may be unavailable */ }
      if (remembered) void openBook(remembered);
    }
  });

  renderAll();
}

function renderExportNote() {
  const noteEl = $("#writerExportNote");
  if (!noteEl) return;
  const title = (writer.book?.title || "作品").replace(/[\\\/:*?"<>|]/g, "_");
  const count = writer.chapters.length;
  const missing = writer.chapters.filter((row) => Number(row.len || 0) <= 0).length;
  noteEl.textContent = count
    ? `全书 ${count} 章${missing ? `（${missing} 章还没有正文，导出时会跳过）` : ""}。`
      + `文件名「${title}.txt」或「${title}.md」；下载交给浏览器处理，通常保存到浏览器默认下载目录。`
    : "导出由浏览器发起下载，通常保存到浏览器默认下载目录。";
}

function stepChapter(direction) {
  if (!writer.chapters.length) return;
  const index = writer.chapters.findIndex((row) => Number(row.no) === writer.chapterNo);
  const nextIndex = index < 0 ? 0 : Math.min(Math.max(index + direction, 0), writer.chapters.length - 1);
  const next = writer.chapters[nextIndex];
  if (next && Number(next.no) !== writer.chapterNo) void openChapter(Number(next.no));
}

function restoreLimits() {
  try {
    const raw = localStorage.getItem(LIMITS_KEY);
    if (!raw) return;
    const saved = JSON.parse(raw);
    if (Number.isFinite(Number(saved.char_count)) && Number(saved.char_count) >= 1 && Number(saved.char_count) <= 10) {
      writer.charCount = Number(saved.char_count);
    }
    if (Number.isFinite(Number(saved.chapter_count)) && Number(saved.chapter_count) >= 5 && Number(saved.chapter_count) <= 30) {
      writer.chapterCount = Number(saved.chapter_count);
      writer.outlineChapterCount = Number(saved.chapter_count);
    }
  } catch (_error) {
    // 存储不可用或值损坏：保持默认值。
  }
  const charRange = $("#writerCharCount");
  if (charRange) charRange.value = String(writer.charCount);
  const charLabel = $("#writerCharCountValue");
  if (charLabel) charLabel.textContent = String(writer.charCount);
  const chapterRange = $("#writerChapterCount");
  if (chapterRange) chapterRange.value = String(writer.chapterCount);
  const chapterLabel = $("#writerChapterCountValue");
  if (chapterLabel) chapterLabel.textContent = String(writer.chapterCount);
  const outlineRange = $("#writerOutlineChapterCount");
  if (outlineRange) outlineRange.value = String(writer.outlineChapterCount);
  const outlineLabel = $("#writerOutlineChapterCountValue");
  if (outlineLabel) outlineLabel.textContent = String(writer.outlineChapterCount);
}

function saveLimits() {
  try {
    localStorage.setItem(LIMITS_KEY, JSON.stringify({
      char_count: writer.charCount,
      chapter_count: writer.chapterCount,
    }));
  } catch (_error) {
    // 存储不可用：参数只在本次会话内生效。
  }
}
