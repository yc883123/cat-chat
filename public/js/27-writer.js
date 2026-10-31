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

import { $, api, applyModelSkinFor, localFileUrl, state, toast } from "./01-core.js";
import { isPywebview, saveLocalFile } from "./03-media.js";
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
  outline_text: "生成本部大纲",
  outline_all: "一次生成全部大纲",
  outline_review: "多部统一复核梳理",
  split: "拆分章节",
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
  outline_text: "全篇大纲",
  outline_review: "复核梳理",
  split: "拆章",
  segs: "段落要点",
  draft: "正文",
};

// 拆章自动分批：与服务端 prompts.SPLIT_STEP_CHAPTERS 对齐（提示文案用）。
const SPLIT_STEP_CHAPTERS = 20;

// 写作风格（与服务端 STYLE_PRESETS 对齐）。
const STYLE_OPTIONS = [
  ["web", "网文爽文（默认）"],
  ["webnovel", "网络爽文"],
  ["hardboiled", "硬汉派（冷硬派）"],
  ["thriller", "悬疑惊悚"],
  ["realism", "现实主义"],
  ["romance", "浪漫主义"],
  ["magic_real", "魔幻现实主义"],
  ["stream", "意识流"],
  ["black_humor", "黑色幽默"],
  ["gothic", "哥特式"],
  ["minimal", "极简主义"],
  ["baroque", "巴洛克式"],
  ["existential", "存在主义"],
  ["satire", "讽刺主义"],
  ["naturalism", "自然主义"],
  ["epic", "史诗风格"],
  ["pastoral", "田园牧歌"],
  ["cyberpunk", "赛博朋克"],
  ["steampunk", "蒸汽朋克"],
  ["wuxia", "武侠风格"],
  ["xianxia", "仙侠风格"],
  ["lightnovel", "轻小说风格"],
  ["epistolary", "书信体 / 日记体"],
  ["metafiction", "元小说"],
  ["postmodern", "后现代主义"],
  ["new_sensation", "新感觉派"],
  ["local", "乡土文学"],
  ["hard_sf", "科幻硬核"],
  ["space_opera", "太空歌剧"],
  ["aesthetic", "唯美主义"],
  ["absurd", "荒诞派"],
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
  ["name", "全名"],
  ["alias", "别称"],
  ["age", "年龄"],
  ["origin", "出身"],
  ["identity", "身份"],
  ["costume", "服装"],
  ["personality", "性格"],
  ["voice", "声音"],
  ["relations", "关系网"],
  ["intro", "人物介绍"],
  ["goal", "目标动机"],
  ["arc", "成长弧线"],
];
// 卡片默认折叠时只露这几个（其余点「展开全部字段」）。
const CHAR_BRIEF_FIELDS = new Set(["name", "identity", "personality", "voice"]);
const LEAD_MAX = 9;
const SIDE_MAX = 99;
const TAGS_MAX = 20;
const REFS_MAX = 8;
const REF_DIMS = ["故事背景", "世界观", "叙事节奏", "人物关系", "语言风格"];

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
  // 大纲当前编辑的部（多部作品切部编辑；单部作品永远是 1）。
  partNo: 1,
  // 拆分面板「拆哪一部」自己的选择：不跟大纲页当前部走（第三次反馈⑥）。
  // 0 = 还没选过（打开作品时按当时的当前部初始化一次）。
  splitPartNo: 0,
  // 部标题/大纲文字的编辑态：DOM 是最新值，渲染函数要让路（与 setupDirty 同一套规则）。
  partsDirty: false,
  // 生成参数（滑条），本地保留。设定链只出设定（第二次反馈），
  // 「大纲章数」滑条已删——大纲没有章节概念，章节由拆章产出。
  charCount: 2,
  sideCount: 4,
  // 模型选择（与拆书匠同口径）：先选供应商，再按供应商拉完整模型目录。
  setupProviderKey: "", setupModelName: "",
  outlineProviderKey: "", outlineModelName: "",
  chapterProviderKey: "", chapterModelName: "",
  draftProviderKey: "", draftModelName: "",
  providerModelsCache: new Map(),
  providerModelInflight: new Map(),
  // 设定表单编辑态：套用模板 / 手改后为 true，保存或放弃后复位。
  setupDirty: false,
  // 批次三：题材树 / 标签多选 / 参考作品 / 角色分区。
  charTab: "lead",
  tags: [],
  refs: [],
  genrePick: { channel: "", cat: "", sub_cat: "" },
  genres: null,
  tagPool: null,
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

/** 导出文件大小：只用于提示「存下来的东西不是 0 字节」。 */
function fmtBytes(count) {
  const value = Number(count) || 0;
  if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} 字节`;
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
    "writerOutlineGenerateAll", "writerPartsApply", "writerImportSettingsBtn",
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
      ["writerChapterProvider", "writerChapterModel", "chapterProviderKey", "chapterModelName"],
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
  const stageName = { setup: "设定", outline: "大纲", chapter: "章节", draft: "正文" }[writer.stage] || "设定";
  const running = writer.pipeline?.status === "running";
  el.textContent = running
    ? `${title} · ${kindLabel(writer.pipeline.kind)} · ${stageName}`
    : `${title} · ${stageName}`;
  // 表单/大纲里有还没写进作品的改动：页头必须能看出来，否则用户会以为已经存了。
  if (writer.setupDirty || writer.partsDirty) el.textContent += " · 有未保存修改";
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
      empty.textContent = "还没有打开作品。点右上「作品」新建或打开。";
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

// ---- 左栏：按阶段切换（设定=作品与版本 · 大纲=设定汇总 · 章节=大纲汇总 · 正文=目录） ----

function renderSide() {
  const stage = writer.stage;
  for (const side of document.querySelectorAll("[data-writer-side]")) {
    side.hidden = side.dataset.writerSide !== stage;
  }
  if (stage === "setup") renderSetupVersions();
  if (stage === "outline") renderSetupSummary();
  if (stage === "chapter") renderChapterOutlineSummary();
}

function renderSetupVersions() {
  const list = $("#writerSetupVersionList");
  const empty = $("#writerSetupVersionEmpty");
  if (!list) return;
  const rows = Array.isArray(writer.book?.setup_versions) ? writer.book.setup_versions : [];
  if (empty) empty.hidden = rows.length > 0;
  const current = String(writer.book?.current_setup_version_id || "");
  list.replaceChildren(...rows.slice().reverse().map((row) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = `writer-version-row${String(row.id) === current ? " active" : ""}`;
    item.dataset.writerSetupVersion = String(row.id || "");
    const title = document.createElement("strong");
    title.textContent = `v${row.version} · ${VERSION_KIND_LABELS[row.kind] || row.kind || "版本"}`;
    const meta = document.createElement("small");
    meta.textContent = `${fmtTime(row.updated_at)} · ${fmtChars(row.len)} 字`;
    item.append(title, meta);
    return item;
  }));
}

function summaryLine(label, value) {
  const text = String(value || "").trim();
  if (!text) return null;
  const row = document.createElement("p");
  row.className = "writer-summary-row";
  const key = document.createElement("span");
  key.textContent = label;
  const val = document.createElement("b");
  val.textContent = text;
  row.append(key, val);
  return row;
}

function renderSetupSummary() {
  const box = $("#writerSetupSummary");
  if (!box) return;
  const setup = writer.setup || {};
  const genre = setup.genre || {};
  const chars = (setup.characters || []).filter((row) => row && String(row.name || "").trim());
  // 创意/世界观不再截断（第二次反馈 §四：预览要能看全）——容器本身可滚动。
  const rows = [
    summaryLine("创意", String(setup.idea || "")),
    summaryLine("书名", setup.title),
    summaryLine("题材", [genre.channel === "female" ? "女生类" : genre.channel === "male" ? "男生类" : "",
      genre.cat, genre.sub_cat].filter(Boolean).join(" › ")),
    summaryLine("基调", genre.tone),
    summaryLine("标签", (setup.genre_tags || []).join(" · ")),
    summaryLine("风格", styleLabelOf(setup.style)),
    summaryLine("雷区", String(setup.constraints?.avoid || "")),
    summaryLine("角色", chars.length ? chars.map((row) => String(row.name)).join("、") : ""),
    summaryLine("世界观", String(setup.worldview || "")),
  ].filter(Boolean);
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "writer-empty";
    empty.textContent = "还没有设定内容。回「设定」页写一句创意，或从模板套用。";
    box.replaceChildren(empty);
    return;
  }
  box.replaceChildren(...rows);
}

function renderChapterOutlineSummary() {
  const box = $("#writerChapterOutlineText");
  const meta = $("#writerChapterOutlineMeta");
  if (!box) return;
  const parts = outlineParts();
  const legacy = hasLegacyOutline() ? writer.outline.chapters : [];
  if (meta) {
    meta.textContent = parts.length
      ? `${parts.length} 部 · ${fmtChars(parts.reduce((n, row) => n + String(row.text || "").length, 0))} 字`
      : (legacy.length ? "逐章大纲（旧版）" : "还没有大纲");
  }
  // 章节页也要能切大纲版本（第二次反馈）：切了版本，下面的汇总与拆章就跟着换。
  const versionGroup = $("#writerChapterOutlineVersionGroup");
  const versionSelect = $("#writerChapterOutlineVersion");
  const versions = Array.isArray(writer.book?.outline_versions) ? writer.book.outline_versions : [];
  if (versionGroup) versionGroup.hidden = versions.length < 2;
  if (versionSelect) {
    const current = String(writer.book?.current_outline_version_id || "");
    versionSelect.replaceChildren(...versions.map((row) => new Option(
      `v${row.version} · ${versionKindLabel(row.kind)} · ${row.parts_count ? `${row.parts_count} 部` : `${row.chapter_count} 章`} · ${fmtTime(row.updated_at)}`,
      String(row.id || ""),
    )));
    if (versions.length) versionSelect.value = current || String(versions.at(-1).id || "");
  }
  if (parts.length) {
    box.replaceChildren(...parts.map((row) => {
      const block = document.createElement("section");
      block.className = "writer-outline-part";
      const head = document.createElement("strong");
      head.textContent = `第 ${row.no} 部 · ${row.title || "未命名"}`;
      const body = document.createElement("div");
      body.className = "writer-outline-part-text";
      body.textContent = String(row.text || "").trim() || "（这一部还没有内容）";
      block.append(head, body);
      return block;
    }));
    return;
  }
  if (legacy.length) {
    box.replaceChildren(...legacy.map((row) => {
      const p = document.createElement("p");
      p.className = "writer-outline-legacy";
      p.textContent = `第${row.no}章 ${row.title || ""}：${String(row.beat || "").slice(0, 80)}`;
      return p;
    }));
    return;
  }
  const empty = document.createElement("p");
  empty.className = "writer-empty";
  empty.textContent = "还没有大纲。回「大纲」页写，或直接生成。";
  box.replaceChildren(empty);
}

function chapterPlan() {
  const plan = writer.book?.chapter_plan || {};
  return { chars: Number(plan.chars || 0), ending: String(plan.ending || "suspense") };
}

const ENDING_LABELS = { suspense: "留悬念", natural: "自然收", hook: "留钩子 + 预告" };

function renderTocLimits() {
  const box = $("#writerTocLimits");
  const label = $("#writerTocLimit");
  const plan = chapterPlan();
  const count = writer.chapters.length;
  const done = writer.chapters.filter((row) => Number(row.len) > 0).length;
  if (label) label.textContent = `${count} 章 · ${done} 章已成稿`;
  if (!box) return;
  box.textContent = count
    ? `${count} 章${plan.chars ? ` · 单章 ≤ ${fmtChars(plan.chars)} 字` : " · 单章不限"} · ${ENDING_LABELS[plan.ending] || plan.ending}`
    : "还没有章节。去「章节」页按大纲拆分。";
}

// ---- 章节页：拆分 + 简述 ----

function renderChapterStage() {
  const plan = chapterPlan();
  const charsEl = $("#writerChapterChars");
  if (charsEl) charsEl.value = String(plan.chars);
  const endingEl = $("#writerChapterEnding");
  if (endingEl) endingEl.value = plan.ending;
  renderParts();
  syncSplitTotalChars();
  renderBriefList();
}

function renderBriefList() {
  const list = $("#writerBriefList");
  const meta = $("#writerBriefMeta");
  if (!list) return;
  const rows = writer.chapters;
  if (meta) {
    meta.textContent = rows.length
      ? `${rows.length} 章 · 点行可改标题与简述`
      : "还没有章节";
  }
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "writer-empty";
    empty.textContent = "先在上面按大纲拆分，章节目录会出现在这里；已有正文的章节不会被覆盖。";
    list.replaceChildren(empty);
    return;
  }
  list.replaceChildren(...rows.map((row) => {
    const item = document.createElement("div");
    item.className = "writer-brief-row";
    item.dataset.writerBrief = String(row.no);
    const head = document.createElement("div");
    head.className = "writer-brief-head";
    const no = document.createElement("button");
    no.type = "button";
    no.className = "writer-brief-no";
    no.dataset.writerBriefOpen = String(row.no);
    no.title = "去正文页写这一章";
    no.textContent = `第${row.no}章`;
    const title = document.createElement("input");
    title.type = "text";
    title.dataset.writerBriefTitle = String(row.no);
    title.value = String(row.title || "");
    title.setAttribute("aria-label", `第${row.no}章标题`);
    head.append(no, title);
    const beat = document.createElement("textarea");
    beat.rows = 2;
    beat.dataset.writerBriefBeat = String(row.no);
    beat.value = String(row.beat || "");
    beat.placeholder = "这一章发生什么（80—150 字）";
    beat.setAttribute("aria-label", `第${row.no}章简述`);
    item.append(head, beat);
    return item;
  }));
}

// ---- 批次三：题材树 / 标签 / 参考作品 ----

async function loadGenres() {
  if (writer.genres) return writer.genres;
  try {
    const res = await fetch(`${TEMPLATES_URL.replace("templates.json", "genres.json")}`);
    writer.genres = res.ok ? await res.json() : { channels: [] };
  } catch (_error) {
    writer.genres = { channels: [] };
  }
  return writer.genres;
}

async function loadTagPool() {
  if (writer.tagPool) return writer.tagPool;
  try {
    const res = await fetch(`${TEMPLATES_URL.replace("templates.json", "tags.json")}`);
    const data = res.ok ? await res.json() : { tags: [] };
    writer.tagPool = Array.isArray(data.tags) ? data.tags : [];
  } catch (_error) {
    writer.tagPool = [];
  }
  return writer.tagPool;
}

function genreChip(label, active, data) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = `writer-genre-cat${active ? " active" : ""}`;
  Object.assign(btn.dataset, data);
  btn.textContent = label;
  return btn;
}

function renderGenrePicker() {
  const box = $("#writerGenrePicker");
  if (!box) return;
  const data = writer.genres;
  if (!data) {
    box.textContent = "正在读取题材树…";
    return;
  }
  const pick = writer.genrePick;
  const channel = pick.channel || "male";
  const channels = Array.isArray(data.channels) ? data.channels : [];
  const blocks = channels.map((ch) => {
    const wrap = document.createElement("div");
    wrap.className = `writer-genre-col${ch.key === channel ? " active" : ""}`;
    const head = document.createElement("button");
    head.type = "button";
    head.className = "writer-genre-channel";
    head.dataset.writerGenreChannel = String(ch.key);
    head.textContent = String(ch.label || "");
    const cats = document.createElement("div");
    cats.className = "writer-genre-cats";
    for (const cat of ch.cats || []) {
      cats.append(genreChip(String(cat.label || ""),
        ch.key === channel && pick.cat === String(cat.label || ""),
        { writerGenreCat: String(cat.label || ""), writerGenreChannel: String(ch.key) }));
    }
    wrap.append(head, cats);
    return wrap;
  });
  const subs = document.createElement("div");
  subs.className = "writer-genre-subs";
  const activeCh = channels.find((ch) => ch.key === channel);
  const activeCat = (activeCh?.cats || []).find((cat) => String(cat.label) === pick.cat);
  for (const sub of activeCat?.subs || []) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `writer-genre-sub${pick.sub_cat === sub ? " active" : ""}`;
    btn.dataset.writerGenreSub = String(sub);
    btn.textContent = String(sub);
    subs.append(btn);
  }
  if (activeCat) subs.append(genreChip("不限二级", !pick.sub_cat, { writerGenreSub: "" }));
  box.replaceChildren(...blocks, subs);
  const catEl = $("#writerGenreCat");
  if (catEl) catEl.value = pick.cat || "";
  const subEl = $("#writerGenreSubCat");
  if (subEl) subEl.value = pick.sub_cat || "";
  const chEl = $("#writerGenreChannel");
  if (chEl) chEl.value = pick.channel || "";
}

function tagChip(label, selected) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = `writer-tag${selected ? " on" : ""}`;
  btn.dataset.writerTag = String(label);
  btn.setAttribute("aria-pressed", String(selected));
  btn.textContent = selected ? `${label} ×` : String(label);
  return btn;
}

function renderTags() {
  const box = $("#writerTagBox");
  if (!box) return;
  const query = String($("#writerTagSearch")?.value || "").trim();
  const selected = writer.tags.slice();
  const pool = (writer.tagPool || []).filter((tag) => !selected.includes(tag)
    && (!query || String(tag).includes(query)));
  const chips = [
    ...selected.map((tag) => tagChip(tag, true)),
    ...pool.slice(0, query ? 80 : 36).map((tag) => tagChip(tag, false)),
  ];
  if (!chips.length) {
    const empty = document.createElement("p");
    empty.className = "writer-hint";
    empty.textContent = writer.tagPool ? "没有匹配的标签；可以直接用上面的自定义题材描述。" : "正在读取标签库…";
    box.replaceChildren(empty);
  } else {
    box.replaceChildren(...chips);
  }
  const stat = $("#writerTagStat");
  if (stat) stat.textContent = `已选 ${selected.length} / ${TAGS_MAX}`;
  const hidden = $("#writerTags");
  if (hidden) hidden.value = selected.join(", ");
}

function renderRefs() {
  const box = $("#writerRefList");
  if (!box) return;
  if (!writer.refs.length) {
    const empty = document.createElement("p");
    empty.className = "writer-hint";
    empty.textContent = "还没有参考作品。点下面「+ 再加一本参考」写一本（可留空不填）。";
    box.replaceChildren(empty);
    return;
  }
  box.replaceChildren(...writer.refs.map((ref, index) => {
    const card = document.createElement("div");
    card.className = "writer-ref";
    card.dataset.writerRef = String(index);
    const head = document.createElement("div");
    head.className = "writer-ref-head";
    const title = document.createElement("input");
    title.type = "text";
    title.value = String(ref.title || "");
    title.placeholder = "作品名，例：《长夜难明》";
    title.dataset.writerRefTitle = String(index);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "control-button tiny";
    remove.dataset.writerRefRemove = String(index);
    remove.textContent = "删除";
    head.append(title, remove);
    const dims = document.createElement("div");
    dims.className = "writer-chips";
    for (const dim of REF_DIMS) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `writer-chip${(ref.dims || []).includes(dim) ? " on" : ""}`;
      btn.dataset.writerRefDim = dim;
      btn.dataset.writerRefIndex = String(index);
      btn.textContent = dim;
      dims.append(btn);
    }
    const note = document.createElement("input");
    note.type = "text";
    note.value = String(ref.note || "");
    note.placeholder = "怎么参考，例：学它「查案推进 + 每章一个旧账」的结构";
    note.dataset.writerRefNote = String(index);
    const same = document.createElement("label");
    same.className = "writer-check";
    const sameBox = document.createElement("input");
    sameBox.type = "checkbox";
    sameBox.checked = Boolean(ref.same_fanfic);
    sameBox.dataset.writerRefSame = String(index);
    const sameText = document.createElement("span");
    sameText.textContent = "本作品同人（改写自己已有的书）";
    same.append(sameBox, sameText);
    card.append(head, dims, note, same);
    return card;
  }));
}

// ---- 设定表单：填充 / 收集 ----

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
  value("writerGenreTone", setup.genre?.tone);
  value("writerGenreCustom", setup.genre?.cat && setup.genre?.custom ? setup.genre.cat : "");
  value("writerVibe", setup.constraints?.vibe);
  value("writerAvoid", setup.constraints?.avoid);
  // 题材树 / 标签 / 参考作品 / 角色分区
  writer.genrePick = {
    channel: String(setup.genre?.channel || ""),
    cat: String(setup.genre?.cat || ""),
    sub_cat: String(setup.genre?.sub_cat || ""),
  };
  writer.tags = Array.isArray(setup.genre_tags) ? setup.genre_tags.slice(0, TAGS_MAX) : [];
  writer.refs = (Array.isArray(setup.references) ? setup.references : []).map((row) => ({
    title: String(row?.title || ""),
    dims: Array.isArray(row?.dims) ? row.dims.slice() : [],
    note: String(row?.note || ""),
    same_fanfic: Boolean(row?.same_fanfic),
  }));
  (async () => {
    await Promise.all([loadGenres(), loadTagPool()]);
    renderGenrePicker();
    renderTags();
  })();
  renderRefs();
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
      scheduleDraftSave();
      toast(`书名已改为「${text}」，保存后生效`);
    });
  renderCandidates("#writerBlurbCandidates", "#writerBlurbCandidateList", setup.blurb_candidates,
    (text) => {
      const input = $("#writerBlurb");
      if (input) input.value = text;
      writer.setupDirty = true;
      scheduleDraftSave();
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
      ? `${String(text || "").slice(0, 60)}…`
      : String(text || "");
    chip.addEventListener("click", () => onPick(String(text || "")));
    return chip;
  }));
}

function renderCharacters() {
  const list = $("#writerCharList");
  if (!list) return;
  const chars = (writer.setup?.characters || []).filter((row) => row && typeof row === "object");
  const leads = chars.filter((row) => (row.role || "lead") === "lead").length;
  const sides = chars.length - leads;
  const leadEl = $("#writerLeadTabCount");
  if (leadEl) leadEl.textContent = `${leads} / ${LEAD_MAX}`;
  const sideEl = $("#writerSideTabCount");
  if (sideEl) sideEl.textContent = `${sides} / ${SIDE_MAX}`;
  document.querySelectorAll("#writerCharTabs [data-writer-char-tab]").forEach((btn) => {
    const active = btn.dataset.writerCharTab === writer.charTab;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
  if (!chars.length) {
    const empty = document.createElement("p");
    empty.className = "writer-empty";
    empty.textContent = "还没有角色。生成设定、从模板套用，或点「+ 加一个角色」手写。";
    list.replaceChildren(empty);
    return;
  }
  list.replaceChildren(...chars.map((row, index) => {
    const role = (row.role || "lead") === "lead" ? "lead" : "side";
    const card = document.createElement("div");
    card.className = "writer-char-card";
    card.dataset.writerCharIndex = String(index);
    card.dataset.writerCharRole = role;
    card.hidden = role !== writer.charTab;
    const head = document.createElement("div");
    head.className = "writer-char-head";
    const badge = document.createElement("span");
    badge.className = `writer-char-badge ${role}`;
    badge.textContent = role === "lead" ? "主角" : "配角";
    const strong = document.createElement("strong");
    strong.textContent = row.name || `角色 ${index + 1}`;
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "control-button tiny";
    toggle.dataset.writerCharToggle = String(index);
    toggle.textContent = "展开全部字段";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "control-button tiny danger";
    remove.dataset.writerCharRemove = String(index);
    remove.textContent = "删除";
    head.append(badge, strong, toggle, remove);
    const grid = document.createElement("div");
    grid.className = "writer-grid-2";
    for (const [key, label] of CHAR_FIELDS) {
      if (!CHAR_BRIEF_FIELDS.has(key)) continue;
      grid.append(charField(row, key, label, false));
    }
    const more = document.createElement("div");
    more.className = "writer-grid-2 writer-char-more";
    more.hidden = true;
    for (const [key, label] of CHAR_FIELDS) {
      if (CHAR_BRIEF_FIELDS.has(key)) continue;
      const isLong = key === "intro" || key === "relations";
      more.append(charField(row, key, label, isLong));
    }
    card.append(head, grid, more);
    return card;
  }));
}

function charField(row, key, label, longText) {
  const field = document.createElement("label");
  field.className = "writer-field";
  field.textContent = label;
  const input = document.createElement(longText ? "textarea" : "input");
  if (longText) input.rows = 2;
  else input.type = "text";
  input.dataset.writerCharField = key;
  input.value = String(row[key] || "");
  field.append(input);
  return field;
}

function collectCharacters() {
  const list = $("#writerCharList");
  if (!list) return [];
  const rows = [];
  for (const card of list.querySelectorAll(".writer-char-card")) {
    const row = { role: card.dataset.writerCharRole === "side" ? "side" : "lead" };
    for (const [key] of CHAR_FIELDS) {
      const input = card.querySelector(`[data-writer-char-field="${key}"]`);
      row[key] = String(input?.value || "").trim();
    }
    if (row.name) rows.push(row);
  }
  return rows;
}

function collectSetup() {
  return {
    idea: String($("#writerIdea")?.value || ""),
    genre: {
      channel: String(writer.genrePick.channel || ""),
      cat: String(writer.genrePick.cat || "").trim(),
      sub_cat: String(writer.genrePick.sub_cat || "").trim(),
      tone: String($("#writerGenreTone")?.value || "").trim(),
      hook: String(writer.setup?.genre?.hook || ""),
    },
    genre_tags: writer.tags.slice(0, TAGS_MAX),
    title: String($("#writerTitle")?.value || "").trim(),
    title_candidates: Array.isArray(writer.setup?.title_candidates) ? writer.setup.title_candidates : [],
    blurb: String($("#writerBlurb")?.value || ""),
    blurb_candidates: Array.isArray(writer.setup?.blurb_candidates) ? writer.setup.blurb_candidates : [],
    worldview: String($("#writerWorldview")?.value || ""),
    characters: collectCharacters(),
    references: writer.refs
      .filter((ref) => String(ref.title || "").trim())
      .slice(0, REFS_MAX)
      .map((ref) => ({
        title: String(ref.title).trim(),
        dims: (ref.dims || []).filter((dim) => REF_DIMS.includes(dim)),
        note: String(ref.note || "").trim(),
        same_fanfic: Boolean(ref.same_fanfic),
      })),
    style: String($("#writerStyle")?.value || "web"),
    constraints: {
      vibe: String($("#writerVibe")?.value || "").trim(),
      avoid: String($("#writerAvoid")?.value || "").trim(),
    },
  };
}

// ---- 大纲：分部文字（3.9.13 起大纲不再等于章节表） ----

/** 当前大纲的分部；老作品没有 parts 时返回空数组。 */
function outlineParts() {
  const parts = Array.isArray(writer.outline?.parts) ? writer.outline.parts : [];
  return parts.filter((row) => row && typeof row === "object");
}

/** 老作品：只有逐章大纲、没有分部文字。 */
function hasLegacyOutline() {
  return !outlineParts().length
    && Array.isArray(writer.outline?.chapters) && writer.outline.chapters.length > 0;
}

function currentPart() {
  const parts = outlineParts();
  return parts.find((row) => Number(row.no) === Number(writer.partNo)) || parts[0] || null;
}

function renderParts() {
  const box = $("#writerOutlineParts");
  const parts = outlineParts();
  // 部数计划输入框跟着当前作品走（正在输入时别打扰）。
  const targetInput = $("#writerPartsTarget");
  if (targetInput && document.activeElement !== targetInput) {
    targetInput.value = String(parts.length || 1);
  }
  if (box) {
    box.hidden = !parts.length;
    box.replaceChildren(...parts.map((row) => {
      const btn = document.createElement("button");
      btn.type = "button";
      const active = Number(row.no) === Number(writer.partNo);
      btn.className = `writer-part${active ? " active" : ""}`;
      btn.dataset.writerPart = String(row.no);
      btn.setAttribute("aria-pressed", String(active));
      const chars = String(row.text || "").length;
      btn.textContent = `第 ${row.no} 部 · ${row.title || "未命名"}` + (chars ? ` · ${fmtChars(chars)} 字` : " · 空");
      return btn;
    }));
  }
  const select = $("#writerSplitPart");
  if (select) {
    const list = parts.length ? parts : [{ no: 1, title: "第 1 部" }];
    select.replaceChildren(...list.map((row) => new Option(`第 ${row.no} 部 · ${row.title || "未命名"}`, String(row.no))));
    // 「拆哪一部」是拆分面板自己的选择，不跟大纲页正在看的部走——第三次反馈⑥：
    // 任何一次重渲染都把它拽回 writer.partNo，用户选好的第 1 部会被悄悄换成别的部。
    // 只在还没选过（刚打开作品）时借一次当前部做初值，之后记住用户的选择；
    // 选中的部在这版大纲里不存在了（切了版本/删了部）才回落到第一部。
    if (!Number(writer.splitPartNo || 0)) writer.splitPartNo = Number(writer.partNo || 1);
    if (!list.some((row) => Number(row.no) === Number(writer.splitPartNo))) {
      writer.splitPartNo = Number(list[0]?.no || 1);
    }
    select.value = String(writer.splitPartNo);
  }
}

/** 把当前部的值写回表单；partsDirty 时让路（DOM 才是最新值）。 */
function fillPartForm() {
  if (writer.partsDirty) return;
  const part = currentPart();
  const noEl = $("#writerPartNo");
  if (noEl) noEl.value = String(part?.no || writer.partNo || 1);
  const titleEl = $("#writerPartTitle");
  if (titleEl) titleEl.value = String(part?.title || "");
  const textEl = $("#writerOutlineText");
  if (textEl) textEl.value = String(part?.text || "");
  renderOutlineStat();
}

function renderOutlineStat() {
  const el = $("#writerOutlineStat");
  if (!el) return;
  const text = String($("#writerOutlineText")?.value || "");
  const total = outlineParts().reduce((sum, row) => sum + String(row.text || "").length, 0);
  const count = outlineParts().length || 1;
  el.textContent = `${count} 部 · 本部 ${fmtChars(text.length)} 字 · 合计 ${fmtChars(total)} 字`
    + (writer.partsDirty ? " · 有未保存修改" : "");
}

/** 收当前编辑态：把表单里的部标题/正文覆盖进分部数组。 */
function collectParts() {
  const parts = outlineParts().map((row) => ({
    no: Number(row.no) || 0,
    title: String(row.title || ""),
    text: String(row.text || ""),
  }));
  const no = Number(writer.partNo || 1);
  const title = String($("#writerPartTitle")?.value || "").trim() || `第 ${no} 部`;
  const text = String($("#writerOutlineText")?.value || "");
  const idx = parts.findIndex((row) => row.no === no);
  if (idx >= 0) parts[idx] = { no, title, text };
  else parts.push({ no, title, text });
  return parts.sort((a, b) => a.no - b.no);
}

function renderOutline() {
  renderParts();
  fillPartForm();
  const legacyRows = hasLegacyOutline() ? writer.outline.chapters : [];
  const group = $("#writerLegacyOutlineGroup");
  if (group) group.hidden = !legacyRows.length;
  const list = $("#writerLegacyOutlineList");
  if (list) {
    if (!legacyRows.length) list.replaceChildren();
    else {
      list.replaceChildren(...legacyRows.map((row) => {
        const item = document.createElement("div");
        item.className = "writer-outline-item readonly";
        const head = document.createElement("div");
        head.className = "writer-outline-head";
        const no = document.createElement("span");
        no.textContent = `第${row.no}章`;
        const title = document.createElement("strong");
        title.textContent = String(row.title || "");
        head.append(no, title);
        const beat = document.createElement("p");
        beat.className = "writer-hint";
        beat.textContent = String(row.beat || "");
        item.append(head, beat);
        return item;
      }));
    }
  }
  const textEl = $("#writerOutlineText");
  if (textEl) textEl.disabled = false;
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
    outline: ["全书大纲", "只写故事怎么走，不写章节"],
    chapter: ["章节", "按大纲拆出章节目录，逐章可改简述"],
    draft: ["正文", "单章编写：自动带上设定摘要与前文衔接"],
  };
  const head = $("#writerEditorHead");
  if (head) head.textContent = heads[writer.stage][0];
  const hint = $("#writerEditorHint");
  if (hint) hint.textContent = heads[writer.stage][1];
  if (writer.stage === "setup" && !writer.setupDirty) fillSetupForm();
  if (writer.stage === "outline") { renderOutline(); renderOutlineVersions(); }
  if (writer.stage === "chapter") renderChapterStage();
  if (writer.stage === "draft") renderDraft();
}

function renderAll() {
  renderPageState();
  renderBookSummary();
  renderSide();
  renderChapters();
  renderTocLimits();
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
  setup: "还没打开作品。生成前会先在这里列出设定链第一步（题材推断）的输入来源。",
  outline: "还没打开作品。生成前会先在这里列出大纲步的输入来源（当前设定 + 分部）。",
  chapter: "还没打开作品。拆章前会先在这里列出实际带上的大纲与设定。",
  draft: "先打开作品并选一章。生成前会在这里列出实际带上的设定、摘要、上一章结尾与本章梗概。",
};
const PLAN_MODEL_FIELDS = {
  setup: ["setupProviderKey", "setupModelName"],
  outline: ["outlineProviderKey", "outlineModelName"],
  chapter: ["chapterProviderKey", "chapterModelName"],
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
    outline: "outline_text",
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
        side_count: writer.sideCount,
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
    // 失败态只留红色错误行（writerPipelineError）：这里重复一遍 error 原文，
    // 用户会在三处看到同一段话（第二次反馈）。白色行只回答「当前任务在干嘛」。
    taskLine.textContent = pipeline.status === "running"
      ? `当前任务：${taskStatus ? TASK_STATUS_LABELS[taskStatus] || taskStatus : "查询中"}`
      : "—";
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
    if (showError) {
      // 后端原文只是一句「已有正文，大纲里却没有它」；不告诉用户开关在哪，等于让人卡死。
      const raw = String(pipeline.error || "");
      error.textContent = raw.includes("已有正文")
        ? `${raw}　要删这些章节（含正文），勾选「允许删除已被移出大纲的章节」后重试；不想删就把它们写回大纲。`
        : raw;
    }
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
      // 终态：流水线可能已写回新版本，重读作品刷新目录与编辑区。
      // 成功完成的阶段，其「未保存修改」标记要让位——否则生成前留下的脏标记
      // 会挡住表单回填，看起来就是「生成完了，内容却不显示」（第二次反馈实锤）。
      if (fresh.status === "done") {
        if (fresh.kind === "setup_all" || fresh.kind === "setup_step") writer.setupDirty = false;
        if (fresh.kind === "outline_text" || fresh.kind === "outline_all"
          || fresh.kind === "outline_review") writer.partsDirty = false;
        persistDraftAfterSave();
      }
      await openBook(bookId, { keepEdits: true, keepStage: true });
      if (fresh.status === "done") {
        toast(`生成完成：${kindLabel(fresh.kind)}`);
        setSaveState("生成完成", "本轮生成的内容已写回作品（可在目录与各阶段里看到）。");
      } else {
        const label = PIPELINE_STATUS_LABELS[String(fresh.status || "")] || fresh.status;
        toast(`生成${label}：可重试`);
        // 失败原因只写在右栏红色错误行（writerPipelineError），这里不再重复原文。
        setSaveState("生成未完成", `详情见右栏「生成进度」里的红色错误行。`);
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

/** 生成前把表单里未保存的设定改动先存成一版（人工）。
 *
 * 为什么必须有这一步：流水线读的是**服务端已保存的设定版本**，不是表单。
 * 套用模板或手改后直接点生成，如果不先存，用户眼前的创意会被静默忽略——
 * 模型拿空设定开场，只会回「未提供创意 / 未指定」这类占位结果（真实踩过）。
 * 返回 true = 可以继续提交；false = 已提示原因，调用方应中止。
 */
async function saveSetupBeforeGenerate() {
  const setup = collectSetup();
  const hasContent = String(setup.idea || "").trim() || String(setup.title || "").trim()
    || String(setup.worldview || "").trim() || setup.characters.length;
  if (!hasContent) {
    toast("先在「故事创意」里写一句话，或从模板套用，再点生成");
    return false;
  }
  try {
    const payload = await api(`/api/writer/books/${encodeURIComponent(writer.bookId)}`, {
      method: "PATCH",
      body: { setup, revision: writer.book?.revision },
    });
    writer.setup = payload.setup;
    writer.book = payload.book;
    writer.setupDirty = false;
    persistDraftAfterSave();
    toast("已自动把表单里的设定存为新版本，接着开始生成");
    return true;
  } catch (error) {
    if (error?.status === 409) {
      toast("作品在别处被修改过（版本冲突）；已重新读取，请再看一眼表单后重试");
      writer.setupDirty = false;
      await openBook(writer.bookId, { keepStage: true });
    } else {
      fail("先保存设定失败", error);
    }
    return false;
  }
}

async function submitPipeline(kind, opts = {}) {
  const {
    chapterNo = 0, allowDrop = false, partNo = 0, partTitle = "",
    targetChars = 0, wantCount = 0, fromNo = 0, extraNote = "", model: preset = null,
    partPlan = null,
  } = opts;
  if (!writer.bookId) return;
  if (writer.pipeline?.status === "running") {
    toast("这部作品已有正在进行的生成，请先取消或等它结束");
    return;
  }
  const modelFields = PLAN_MODEL_FIELDS[writer.stage] || ["setupProviderKey", "setupModelName"];
  const model = preset && preset.model_name ? preset : readModel(...modelFields);
  if (!model.model_name) {
    toast("先在设置里检查模型，再回来选择");
    return;
  }
  if (!gateByBudget(model)) return;
  setBusy(true);
  try {
    // 表单里有未保存的设定改动（例：刚套用模板）：先自动存一版再提交，
    // 否则流水线仍用旧的已保存设定。
    if (writer.setupDirty) {
      const saved = await saveSetupBeforeGenerate();
      if (!saved) return;
    }
    const fallbackNote = writer.stage === "setup" ? $("#writerSetupNote")?.value || ""
      : writer.stage === "outline" ? $("#writerOutlineNote")?.value || ""
        : writer.stage === "chapter" ? $("#writerChapterNote")?.value || ""
          : $("#writerDraftNote")?.value || "";
    const payload = await api(`/api/writer/books/${encodeURIComponent(writer.bookId)}/pipelines`, {
      method: "POST",
      body: {
        kind,
        chapter_no: chapterNo || undefined,
        model_key: model.model_key,
        model_name: model.model_name,
        char_count: writer.charCount,
        side_count: writer.sideCount,
        part_no: partNo || undefined,
        part_title: partTitle || undefined,
        target_chars: targetChars || undefined,
        want_count: wantCount || undefined,
        from_no: fromNo || undefined,
        part_plan: Array.isArray(partPlan) && partPlan.length ? partPlan : undefined,
        extra_note: extraNote || fallbackNote,
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
// 草稿暂存（切阶段 / 切页 / 刷新都不丢）
// ============================================================
//
// 三层事实源，顺序固定：DOM（用户正在打字） > localStorage 草稿 > 服务端已保存版本。
// dirty 标记就是「DOM 更新」的旗子：置真后渲染函数必须让路，否则重建整列/重填表单
// 会把用户刚输入的内容抹掉（这就是「填了一半切走再回来归零」的根因）。

const DRAFT_KEY_PREFIX = "naibaWriterDraft:";
const DRAFT_FLUSH_MS = 800;
let draftTimer = 0;

function writerDraftKey(bookId) {
  return `${DRAFT_KEY_PREFIX}${bookId}`;
}

function readWriterDraft(bookId) {
  if (!bookId) return null;
  try {
    const raw = localStorage.getItem(writerDraftKey(bookId));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch (_error) {
    return null;
  }
}

function clearWriterDraft(bookId) {
  if (!bookId) return;
  if (draftTimer) {
    clearTimeout(draftTimer);
    draftTimer = 0;
  }
  try { localStorage.removeItem(writerDraftKey(bookId)); } catch (_error) { /* storage may be unavailable */ }
}

/** 把草稿写进 localStorage。bookId 由调用方锁定：延时写入期间换了书也不能串稿。 */
function writeWriterDraft(bookId) {
  if (!bookId || bookId !== writer.bookId) return;
  if (!writer.setupDirty && !writer.partsDirty) return;
  const payload = { revision: writer.book?.revision ?? null, saved_at: Date.now() };
  if (writer.setupDirty) payload.setup = collectSetup();
  if (writer.partsDirty) {
    payload.parts = collectParts();
    payload.part_no = Number(writer.partNo || 1);
  }
  try { localStorage.setItem(writerDraftKey(bookId), JSON.stringify(payload)); } catch (_error) { /* 存不下就不存 */ }
}

function scheduleDraftSave() {
  if (!writer.bookId || (!writer.setupDirty && !writer.partsDirty)) return;
  const bookId = writer.bookId;
  if (draftTimer) clearTimeout(draftTimer);
  draftTimer = setTimeout(() => {
    draftTimer = 0;
    writeWriterDraft(bookId);
  }, DRAFT_FLUSH_MS);
  renderPageState();
}

/** 保存成功后收拾草稿：还有别的脏数据就重写成「只剩它」的一份（版本号此时已更新），
 *  全干净了才整条删掉。这样保存设定不会顺带把大纲草稿一起删掉。 */
function persistDraftAfterSave() {
  if (writer.setupDirty || writer.partsDirty) writeWriterDraft(writer.bookId);
  else clearWriterDraft(writer.bookId);
}

/** 打开作品时尝试恢复草稿：只认版本号对得上的那份，其余一律当过期丢掉。 */
function applyWriterDraft(bookId, book) {
  const draft = readWriterDraft(bookId);
  if (!draft) return { setup: false, outline: false };
  const serverRevision = book?.revision ?? null;
  if (draft.revision === undefined || String(draft.revision) !== String(serverRevision)) {
    clearWriterDraft(bookId);
    return { setup: false, outline: false };
  }
  let setup = false;
  let outline = false;
  if (draft.setup && typeof draft.setup === "object") {
    writer.setup = draft.setup;
    setup = true;
  }
  if (Array.isArray(draft.parts) && draft.parts.length) {
    writer.outline = { ...(writer.outline || {}), schema: 2, parts: draft.parts, chapters: [] };
    writer.partNo = Number(draft.part_no || 1);
    outline = true;
  }
  return { setup, outline };
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
    const sameBook = writer.bookId === bookId;
    writer.bookId = bookId;
    writer.book = payload.book;
    // 换书时「拆哪一部」的选择也跟着换：上一部作品的部号对这一本没有意义。
    if (!sameBook) writer.splitPartNo = 0;
    writer.setup = payload.setup;
    writer.outline = payload.outline;
    writer.chapters = payload.chapters || [];
    writer.broken = payload.broken || [];
    writer.pipeline = payload.book?.active_pipeline || null;
    // 换书一律重填：上一本的 dirty 标记留着会让表单/大纲停在上一本的内容上（串稿）。
    const keep = sameBook && keepEdits;
    if (!keep) {
      writer.setupDirty = false;
      writer.draftDirty = false;
      writer.partsDirty = false;
    }
    // 刷新/重启后恢复没保存的草稿；版本对不上（已保存过或在别处改过）就丢弃，避免复活旧内容。
    const restored = keep
      ? { setup: false, outline: false }
      : applyWriterDraft(bookId, payload.book);
    if (!keepStage) writer.stage = "setup";
    // 草稿数据已经写进 writer.setup / writer.outline：先清标记让 renderAll 按它重建 DOM
    // （守卫只在 dirty 时让路），重建完再把标记置回。
    if (restored.setup) writer.setupDirty = false;
    if (restored.outline) writer.partsDirty = false;
    // 活动流水线的流式缓冲与新作品/新状态对齐。
    writer.streamTaskId = String(writer.pipeline?.current_task_id || "");
    writer.streamText = "";
    writer.taskCursor = 0;
    writer.pipelineTask = null;
    try { localStorage.setItem(LAST_BOOK_KEY, bookId); } catch (_error) { /* storage may be unavailable */ }
    setSaveState("已打开", `《${writer.book?.title || ""}》已打开（第 ${writer.book?.revision ?? "?"} 版）。`);
    renderAll();
    if (restored.setup) writer.setupDirty = true;
    if (restored.outline) writer.partsDirty = true;
    if (restored.setup || restored.outline) {
      renderPageState();
      renderOutline();
      setSaveState("有未保存修改", "已恢复上次没保存的草稿；点「保存」才会写进作品。");
    }
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
      clearWriterDraft(bookId);
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
    persistDraftAfterSave();
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

/** 风格键 → 中文标签（右栏预检与设定汇总共用）。 */
function styleLabelOf(key) {
  const row = STYLE_OPTIONS.find(([value]) => String(value) === String(key || ""));
  return row ? row[1] : "";
}

// ---- 大纲：分部文字的保存与生成 ----

async function savePartsManual() {
  if (!writer.bookId) return;
  const parts = collectParts();
  const hasText = parts.some((row) => String(row.text || "").trim());
  if (!hasText) {
    // 只排了部目录（部数与标题）也算内容：第三次反馈②之前「目录没正文就存不了」，
    // 用户只能靠逐部生成硬凑目录。单部默认名且没字才真的没东西可存。
    const meaningful = parts.length > 1
      || (parts[0] && String(parts[0].title || "").trim()
        && String(parts[0].title || "").trim() !== `第 ${parts[0].no} 部`);
    if (!meaningful) {
      toast("大纲还是空的；先写点内容或点「生成这一部大纲」");
      return;
    }
  }
  setBusy(true);
  try {
    const payload = await api(`/api/writer/books/${encodeURIComponent(writer.bookId)}`, {
      method: "PATCH",
      body: { parts, revision: writer.book?.revision },
    });
    writer.book = payload.book;
    writer.outline = { ...(writer.outline || {}), schema: 2, parts, chapters: [] };
    writer.partsDirty = false;
    persistDraftAfterSave();
    toast(hasText ? "大纲已保存为新版本" : "部目录已保存；各部正文可以之后再生成");
    setSaveState("已保存", hasText
      ? "大纲按版本保存；拆章只改章节目录，不动大纲本身。"
      : "部目录已保存；生成大纲时按这些部逐部写。");
    renderAll();
    void refreshPlan();
  } catch (error) {
    if (error?.status === 409) {
      if (window.confirm("作品在别处被修改过（版本冲突）。\n要重新读取最新内容吗？（你的大纲修改会丢失）")) {
        writer.partsDirty = false;
        await openBook(writer.bookId, { keepStage: true });
      }
      return;
    }
    fail("保存大纲失败", error);
  } finally {
    setBusy(false);
  }
}

/** 生成前把大纲目录先落库：有正文才值得存一版；纯目录（还没写）不用存，
 *  流水线每步写回时会带上部清单把目录重建出来（第三次反馈①②）。
 *  返回 true = 可以继续提交；false = 保存失败（如版本冲突），调用方应中止。 */
async function ensurePartsSavedBeforeGenerate() {
  if (!writer.partsDirty) return true;
  const parts = collectParts();
  if (!parts.some((row) => String(row.text || "").trim())) return true;
  await savePartsManual();
  return !writer.partsDirty;
}

/** 生成当前这一部的整篇大纲（outline_text 流水线）。 */
async function generatePartText() {
  if (!writer.bookId) return;
  const model = readModel("outlineProviderKey", "outlineModelName");
  if (!model.model_name) {
    toast("先在设置里检查模型，再回来选择");
    return;
  }
  if (!await ensurePartsSavedBeforeGenerate()) return;
  try {
    await submitPipeline("outline_text", {
      partNo: Number(writer.partNo || 1),
      partTitle: String($("#writerPartTitle")?.value || "").trim(),
      targetChars: 8000,
      model,
    });
  } catch (_error) {
    // submitPipeline 自己会 toast；这里只防未捕获。
  }
}

/** 程序性改部（增/删/按部数重排/升级）后把表单强制对齐当前部。
 *  fillPartForm 的 dirty 守卫是给「用户正在打字」让路的；这里刚换了个部，
 *  表单必须跟着换——否则旧标题/旧正文还留在输入框里，下一次 collectParts
 *  会把它们盖到当前部头上（第三次反馈③「第4部·第3部」就是这么来的）。 */
function syncPartFormToCurrent() {
  const part = currentPart();
  const noEl = $("#writerPartNo");
  if (noEl) noEl.value = String(part?.no || writer.partNo || 1);
  const titleEl = $("#writerPartTitle");
  if (titleEl) titleEl.value = String(part?.title || "");
  const textEl = $("#writerOutlineText");
  if (textEl) textEl.value = String(part?.text || "");
  renderOutlineStat();
}

/** 按填的部数重建分部目录：多退少补，已有内容的部原样保留。 */
function applyPartsTarget() {
  const target = Math.min(20, Math.max(1, Number($("#writerPartsTarget")?.value || 1)));
  const parts = collectParts();
  if (target === parts.length) {
    toast(`已经是 ${target} 部了`);
    return;
  }
  const next = [];
  for (let no = 1; no <= target; no += 1) {
    next.push(parts[no - 1] || { no, title: `第 ${no} 部`, text: "" });
  }
  writer.outline = { ...(writer.outline || {}), schema: 2, parts: next, chapters: [] };
  writer.partNo = Math.min(Number(writer.partNo || 1), target);
  writer.partsDirty = true;
  renderOutline();
  syncPartFormToCurrent();
  toast(`已按 ${target} 部重排目录${target > parts.length ? "；新增的部是空的，点「一次性生成全部大纲」或逐部填写" : ""}；保存后生效`);
}

/** 一次性生成全部部的大纲（outline_all）：每步一部，后面的部接住前面的线。
 *  再点一次就是重新生成（抽卡）：已写过内容的部会被替换，旧版本留在历史里。 */
async function generateAllParts() {
  if (!writer.bookId) return;
  const model = readModel("outlineProviderKey", "outlineModelName");
  if (!model.model_name) {
    toast("先在设置里检查模型，再回来选择");
    return;
  }
  if (!await ensurePartsSavedBeforeGenerate()) return;
  const plan = collectParts().map((row) => ({ no: row.no, title: row.title }));
  if (!plan.length) {
    toast("先填好各部的部标题（或点「按部数生成目录」）");
    return;
  }
  const withText = collectParts().filter((row) => String(row.text || "").trim()).length;
  if (withText && !window.confirm(
    `要按 ${plan.length} 部一次性重新生成全部大纲吗？\n已写过内容的部会被新生成的内容替换（旧版本保留在历史里）；后面的部会衔接前面的部，保证故事连贯。`,
  )) return;
  try {
    await submitPipeline("outline_all", { partPlan: plan, targetChars: 8000, model });
  } catch (_error) {
    // submitPipeline 自己会 toast。
  }
}

/** 多部统一复核梳理（outline_review）：把已写好的各部放在一起互相核对，
 *  逐部修订衔接、矛盾与重复；每部存一版新大纲，旧版本可切回。 */
async function generateOutlineReview() {
  if (!writer.bookId) return;
  const model = readModel("outlineProviderKey", "outlineModelName");
  if (!model.model_name) {
    toast("先在设置里检查模型，再回来选择");
    return;
  }
  if (!await ensurePartsSavedBeforeGenerate()) return;
  const parts = collectParts().filter((row) => String(row.text || "").trim());
  if (parts.length < 2) {
    toast("多部复核梳理至少要两部已写好的大纲；先逐部生成或填写");
    return;
  }
  if (!window.confirm(
    `要对照其余各部，逐部复核梳理这 ${parts.length} 部大纲吗？\n` +
    "它会修掉部与部之间接不上、对不齐、重复的桥段，只做必要修改；\n" +
    "每部修订后都按新版本保存，旧版本留在历史里，不满意可随时切回。",
  )) return;
  try {
    await submitPipeline("outline_review", {
      partPlan: parts.map((row) => ({ no: row.no, title: row.title })),
      model,
    });
  } catch (_error) {
    // submitPipeline 自己会 toast。
  }
}

function addPart() {
  const parts = collectParts();
  if (parts.length >= 20) {
    toast("最多 20 部");
    return;
  }
  const nextNo = parts.length + 1;
  parts.push({ no: nextNo, title: `第 ${nextNo} 部`, text: "" });
  writer.partNo = nextNo;
  writer.partsDirty = true;
  writer.outline = { ...(writer.outline || {}), schema: 2, parts, chapters: [] };
  renderOutline();
  syncPartFormToCurrent();
  toast(`已加第 ${nextNo} 部，部标题已自动编好号；写完点「保存大纲修改」`);
}

function removePart() {
  const parts = collectParts();
  if (parts.length <= 1) {
    toast("至少要留一部");
    return;
  }
  const no = Number(writer.partNo || 1);
  const rest = parts.filter((row) => Number(row.no) !== no)
    .map((row, index) => ({ ...row, no: index + 1 }));
  writer.outline = { ...(writer.outline || {}), schema: 2, parts: rest, chapters: [] };
  writer.partNo = Math.max(1, no - 1);
  writer.partsDirty = true;
  renderOutline();
  syncPartFormToCurrent();
  toast(`已删除第 ${no} 部，后面的部已重新编号；保存后生效`);
}

/** 老作品：把逐章大纲拼成一段文字，升级为分部大纲（不改动原版本）。 */
async function upgradeLegacyOutline() {
  const rows = hasLegacyOutline() ? writer.outline.chapters : [];
  if (!rows.length) {
    toast("没有可升级的逐章大纲");
    return;
  }
  if (!window.confirm(
    `要把这 ${rows.length} 章的逐章大纲合并成一段大纲文字吗？\n合并后可以逐字修改，再按新大纲拆章；老版本仍保留在历史里。`,
  )) return;
  const text = rows.map((row) => `第${row.no}章 ${row.title || ""}：${String(row.beat || "").trim()}`)
    .join("\n");
  writer.partNo = 1;
  writer.outline = {
    ...(writer.outline || {}), schema: 2,
    parts: [{ no: 1, title: "第 1 部", text }], chapters: [],
  };
  writer.partsDirty = true;
  renderOutline();
  // 表单里还留着升级前那一部的旧内容；不先对齐到第 1 部，collectParts 会把
  // 输入框里的旧文盖掉刚拼好的大纲。
  syncPartFormToCurrent();
  await savePartsManual();
}

// ---- 章节：拆分 ----

async function saveChapterPlan() {
  if (!writer.bookId) return;
  const chars = Number($("#writerChapterChars")?.value || 0);
  const ending = String($("#writerChapterEnding")?.value || "suspense");
  const plan = chapterPlan();
  if (plan.chars === chars && plan.ending === ending) return;
  try {
    const payload = await api(`/api/writer/books/${encodeURIComponent(writer.bookId)}`, {
      method: "PATCH",
      body: { chapter_plan: { chars, ending }, revision: writer.book?.revision },
    });
    writer.book = payload.book;
    renderTocLimits();
    setSaveState("已保存", `单章字数${chars ? ` ≤ ${chars} 字` : "不限"} · 结尾${ENDING_LABELS[ending] || ending}`);
  } catch (error) {
    fail("保存章节设置失败", error);
  }
}

const SPLIT_BATCH_MAX = 999;

// ---- 拆分字数联动：拆成多少章 × 单章字数 = 本部目标总字数（第三次反馈⑤） ----

function splitChapterChars() {
  return Number($("#writerChapterChars")?.value || 0);
}

/** 由「章数 × 单章」刷出总字数框；单章不限时公式不成立，框留空。 */
function syncSplitTotalChars() {
  const totalEl = $("#writerSplitTotalChars");
  if (!totalEl || document.activeElement === totalEl) return;
  const chars = splitChapterChars();
  const count = Number($("#writerSplitCount")?.value || 0);
  totalEl.value = chars && count ? String(count * chars) : "";
}

/** 由总字数反推章数（四舍五入，至少 1 章），再把总字数框对齐成整数结果。 */
function applySplitTotalChars() {
  const totalEl = $("#writerSplitTotalChars");
  if (!totalEl) return;
  const total = Number(totalEl.value || 0);
  if (!total || total < 1) return;
  const chars = splitChapterChars();
  if (!chars) {
    toast("单章字数现在是「不限」，没法由总字数反推章数；先选一个单章字数");
    syncSplitTotalChars();
    return;
  }
  const count = Math.min(9999, Math.max(1, Math.round(total / chars)));
  const countEl = $("#writerSplitCount");
  if (countEl) countEl.value = String(count);
  totalEl.value = String(count * chars);
}

/** 拆一批：按 wantCount 与 999 上限切批；接续拆分会带上已拆章节的衔接上下文（服务端）。 */
async function startSplit() {
  if (!writer.bookId) return;
  const want = Number($("#writerSplitCount")?.value || 0);
  if (!want || want < 1) {
    toast("先填要拆成多少章（1–9999）");
    return;
  }
  await saveChapterPlan();
  const model = readModel("chapterProviderKey", "chapterModelName");
  if (!model.model_name) {
    toast("先在设置里检查模型，再回来选择");
    return;
  }
  if (!writer.chapters.length) {
    await submitSplitBatch({ fromNo: 1, partNo: splitPartNo(), model });
    return;
  }
  // 已有章节：让用户选「接续」（从下一章继续）或「覆盖重拆」（清空后从第 1 章开始）。
  const existing = writer.chapters.length;
  const hasBody = writer.chapters.some((row) => Number(row.len || 0) > 0);
  const note = $("#writerSplitDialogNote");
  if (note) {
    note.textContent = `现在已有 ${existing} 章${hasBody ? "（部分已有正文）" : ""}。`
      + "接续＝保留现有章节，从它们的下一章继续拆；覆盖重拆＝清空现有章节目录（含全部正文，不能恢复），从第 1 章重新拆。";
  }
  const dialog = $("#writerSplitDialog");
  if (!dialog || typeof dialog.showModal !== "function") {
    // 弹窗不可用（老内核）：退回 window.confirm 的接续/取消二选一。
    if (window.confirm(`现在已有 ${existing} 章；接续拆分会从第 ${existing + 1} 章继续（已有章节不会被覆盖）。继续吗？`)) {
      await submitSplitBatch({ fromNo: existing + 1, partNo: splitPartNo(), model });
    }
    return;
  }
  const continueBtn = $("#writerSplitContinueChoice");
  const overwriteBtn = $("#writerSplitOverwriteChoice");
  const cancelBtn = $("#writerSplitCancelChoice");
  // signal：弹窗一结束就把本轮监听全部摘掉；once 残留下轮会串选择。
  const signal = new AbortController().signal;
  const done = await new Promise((resolve) => {
    const settle = dialog.addEventListener.bind(dialog);
    settle("close", () => resolve("cancel"), { once: true, signal });
    continueBtn?.addEventListener("click", () => resolve("continue"), { once: true, signal });
    overwriteBtn?.addEventListener("click", () => resolve("overwrite"), { once: true, signal });
    cancelBtn?.addEventListener("click", () => resolve("cancel"), { once: true, signal });
  });
  dialog.close();
  if (done === "continue") {
    await submitSplitBatch({ fromNo: existing + 1, partNo: splitPartNo(), model });
    return;
  }
  if (done === "overwrite") {
    if (!window.confirm(
      `要清空现有 ${existing} 章吗？\n章节目录与全部正文版本都会删除，不能恢复；之后从第 1 章重新拆。`,
    )) return;
    try {
      await api(`/api/writer/books/${encodeURIComponent(writer.bookId)}/chapters/clear`, {
        method: "POST",
        body: { revision: writer.book?.revision },
      });
      writer.chapters = [];
      writer.chapter = null;
      writer.chapterNo = 0;
      toast("已清空章节目录；开始覆盖重拆");
      await submitSplitBatch({ fromNo: 1, partNo: splitPartNo(), model });
    } catch (error) {
      fail("清空章节失败", error);
    }
  }
}

function splitPartNo() {
  // 状态优先（renderParts 会把 DOM 对齐到它），DOM 只是兜底。
  return Number(writer.splitPartNo || $("#writerSplitPart")?.value || writer.partNo || 1);
}

/** 继续拆：还剩多少就再拆一批（每批 ≤999）。 */
async function continueSplit() {
  const target = Number($("#writerSplitCount")?.value || 0);
  if (!target) return;
  const fromNo = writer.chapters.reduce((max, row) => Math.max(max, Number(row.no) || 0), 0) + 1;
  if (fromNo > target) {
    toast("已经拆到目标章数了");
    return;
  }
  const model = readModel("chapterProviderKey", "chapterModelName");
  if (!model.model_name) {
    toast("先在设置里检查模型，再回来选择");
    return;
  }
  await submitSplitBatch({
    fromNo, partNo: splitPartNo(), model,
  });
}

async function submitSplitBatch({ fromNo, partNo, model }) {
  const target = Number($("#writerSplitCount")?.value || 0);
  const remaining = Math.max(1, target - fromNo + 1);
  const wantCount = Math.min(SPLIT_BATCH_MAX, remaining);
  try {
    await submitPipeline("split", {
      partNo,
      wantCount,
      fromNo,
      model,
      extraNote: String($("#writerChapterNote")?.value || ""),
    });
    const btn = $("#writerSplitContinue");
    if (btn) btn.hidden = fromNo + wantCount > target;
    const batches = wantCount > SPLIT_STEP_CHAPTERS
      ? `，会自动按每批 ${SPLIT_STEP_CHAPTERS} 章连拆` : "";
    setSaveState("已提交拆分",
      `拆「第 ${partNo} 部」：第 ${fromNo} 章起拆 ${wantCount} 章（目标 ${target} 章）${batches}；完成后章节目录会自动刷新。`);
  } catch (_error) {
    // submitPipeline 已提示
  }
}

/** 改章节标题/简述（失焦保存）。 */
async function updateChapterMeta(chapterNo, patch) {
  if (!writer.bookId || !chapterNo) return;
  try {
    const payload = await api(
      `/api/writer/books/${encodeURIComponent(writer.bookId)}/chapters/${chapterNo}/meta`,
      { method: "PATCH", body: { ...patch, revision: writer.book?.revision } },
    );
    writer.book = payload.book;
    const row = writer.chapters.find((item) => Number(item.no) === Number(chapterNo));
    if (row && payload.chapter) {
      row.title = payload.chapter.title;
      row.beat = payload.chapter.beat;
    }
    renderChapters();
    setSaveState("已保存", `第 ${chapterNo} 章的标题/简述已更新。`);
  } catch (error) {
    fail("保存章节简述失败", error);
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
    toast("已切换大纲版本；大纲汇总与拆分会跟着这一版走");
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
  // 先收下表单里的现值，再把模板字段覆盖上去：整栏会重渲染一次（人物卡等内容要一起更新），
  // 这一步保证用户手填的其它字段（参考气质、雷区、风格等）不会被这次刷新吞掉。
  const current = collectSetup();
  // 模板的角色串是散文，机械切分可能切不出条目；切不出时保留用户已有的角色卡，不用空数组盖掉。
  const templateChars = parseTemplateCharacters(row.characters);
  writer.setup = {
    ...(writer.setup || {}),
    ...current,
    idea: String(row.prompt || ""),
    title: String(row.title || "").replace(/^《|》$/g, ""),
    blurb: String(row.blurb || ""),
    worldview: String(row.worldview || ""),
    genre: { ...(current.genre || {}), cat: String(row.sub || "") },
    genre_tags: (Array.isArray(row.tags) ? row.tags : []).map((tag) => String(tag || "").trim()).filter(Boolean).slice(0, 5),
    characters: templateChars.length ? templateChars : current.characters,
  };
  writer.setupDirty = true;
  closeDialog("#writerTemplateDialog");
  // 程序化改 writer.setup 之后必须自己重填一次：renderEditor 的 dirty 守卫只给「用户正在打字」让路，
  // 否则套用模板只会改内存，表单还停在旧内容上（切阶段才丢内容的同一个坑的另一面）。
  fillSetupForm();
  setStage("setup");
  toast(`已套用模板「${row.name}」；点「保存修改为新版本」入库，或直接点生成（会自动先保存）`);
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

/** 设定集文件夹导入（第二次反馈 §一/§六）：整个文件夹的 .md 一次读入并解析。 */
async function importSettingsFolder(fileList) {
  if (!writer.bookId) {
    toast("先打开（或新建）一部作品，再导入设定集");
    return;
  }
  const files = [...(fileList || [])].filter((file) => /\.(md|markdown|txt)$/i.test(file.name));
  if (!files.length) {
    toast("文件夹里没有 .md / .txt 文件；请选择设定集文件夹");
    return;
  }
  files.sort((a, b) => String(a.name).localeCompare(String(b.name), "zh-Hans-CN"));
  const totalBytes = files.reduce((sum, file) => sum + Number(file.size || 0), 0);
  if (totalBytes > 12 * 1024 * 1024) {
    toast(`设定集太大（${fmtBytes(totalBytes)}），上限 12 MB；请删减后重试`);
    return;
  }
  setBusy(true);
  setSaveState("正在导入设定集", `正在读取 ${files.length} 个文件，请稍候。`);
  try {
    const contents = await Promise.all(files.map(async (file) => ({
      name: String(file.webkitRelativePath || file.name || "").split("/").pop() || file.name,
      content: await file.text(),
    })));
    const payload = await api(
      `/api/writer/books/${encodeURIComponent(writer.bookId)}/import-settings`,
      { method: "POST", body: { files: contents, revision: writer.book?.revision } },
    );
    writer.book = payload.book;
    writer.importPreview = null;
    await openBook(writer.bookId, { keepStage: true });
    const preview = payload.preview || {};
    const bits = [
      `识别角色 ${preview.character_count || 0} 人`,
      `分部大纲 ${preview.parts_count || 0} 部`,
      `章节框架 ${preview.chapter_count || 0} 章`,
    ];
    if (preview.plan_chars) bits.push(`单章约 ${preview.plan_chars} 字`);
    const warnings = Array.isArray(preview.warnings) ? preview.warnings : [];
    setSaveState("设定集已导入", bits.join(" · ")
      + (warnings.length ? `。注意：${warnings.join("；")}` : ""));
    toast(`设定集导入完成：${bits.join("，")}`);
    // 识别到什么就停在哪一步：有章节框架直接到章节页，有分部大纲到大纲页。
    if ((preview.appended_chapters || []).length) setStage("chapter");
    else if (preview.parts_count) setStage("outline");
    else setStage("setup");
    renderAll();
  } catch (error) {
    fail("导入设定集失败", error);
    setSaveState("设定集导入失败", String(error?.message || error));
  } finally {
    setBusy(false);
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

/** 导出：后端先把整本落成一个真实文件，再由这里决定怎么交给用户。
 *
 * 不再用 `URL.createObjectURL` + `<a download>`：exe 里 pywebview 的 ALLOW_DOWNLOADS
 * 默认 False，WebView2 收到 DownloadStarting 直接 Cancel——点了等于没点（用户实测）。
 * 桌面端只能把「已存在的文件路径」交给 pywebview 的原生另存为；浏览器/手机照旧下载。
 */
async function exportBook(fmt = "txt", withExtras = false) {
  if (!writer.bookId) return;
  const label = `${fmt.toUpperCase()}${withExtras ? "（含设定与大纲）" : "（全书）"}`;
  try {
    const payload = await api(
      `/api/writer/books/${encodeURIComponent(writer.bookId)}/export-file`,
      { method: "POST", body: { format: fmt, extras: withExtras ? "1" : "" } },
    );
    const missing = Array.isArray(payload?.missing_chapters) ? payload.missing_chapters : [];
    const bytes = Number(payload?.bytes || 0);
    // 没有正文：不弹保存框。弹了也只是让人存一个空壳，正是「导出又坏了」的来源。
    if (!bytes || payload?.has_content === false) {
      setSaveState("没有正文可导出",
        `《${writer.book?.title || "作品"}》还没有任何一章写成正文，这样导出只会得到一个空文件。`
        + "改选「导出 Markdown（含设定与大纲）」可以先把设定和大纲带走。");
      toast("还没有正文；可改选「导出 Markdown（含设定与大纲）」");
      return;
    }
    const filename = String(payload?.suggested_name || payload?.filename || "");
    const mode = saveLocalFile(localFileUrl(payload.path), filename);
    const missingNote = missing.length
      ? `注意：第 ${missing.slice(0, 8).join("、")} 章没有正文，未包含在文件里。`
      : "";
    if (mode === "saved") {
      setSaveState("已导出",
        `已弹出「另存为」：${filename}（${label}，${fmtBytes(bytes)}）。`
        + `无论存到哪，导出目录里都留了一份：${payload.dir || "导出目录"}。${missingNote}`);
    } else if (mode === "download") {
      setSaveState("已导出", `已交给浏览器下载「${filename}」（${label}，${fmtBytes(bytes)}）。${missingNote}`);
    } else {
      setSaveState("导出失败",
        `没能把文件交给你；但它已经写好（${fmtBytes(bytes)}），可以从「保存位置」那个目录里自己取。`);
    }
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
  // 设定 / 大纲版本与生成记录都可勾选删除；章节行删的是整章（走章节自己的删除入口）。
  return writer.manage.tab !== "chapter";
}

function manageTabNote() {
  return {
    setup: "设定版本逐条保留；「设为当前」立即切换。勾选后可删除不要的版本（删除不能恢复；删的是版本文件本身）。",
    outline: "大纲版本逐条保留；「设为当前」立即切换。勾选后可删除不要的版本（删除不能恢复）。",
    chapter: "点一章展开它的正文版本；「设为当前」立即切换正文。展开后可以勾选删除单个正文版本。",
    task: "织书台这一本作品的生成记录；删除只清任务记录，不删已保存的内容。",
  }[writer.manage.tab] || "";
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
  if (noteEl) noteEl.textContent = manageTabNote();
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
    const nouns = { setup: "个设定版本", outline: "个大纲版本", chapter: "章", task: "条生成记录" };
    selection.textContent = writer.manage.selected.size
      ? `已选 ${writer.manage.selected.size} ${nouns[writer.manage.tab] || "条"}（删除不能恢复）` : "";
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
      // 正文版本也能勾选删除（第二次反馈：管理记录要能管理正文版本）。
      const check = document.createElement("input");
      check.type = "checkbox";
      check.dataset.writerManageCheck = `chapterver:${no}:${row.id}`;
      check.checked = writer.manage.selected.has(`chapterver:${no}:${row.id}`);
      item.append(check);
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
  const versionKeys = keys.filter((key) => key.startsWith("version:"));
  const chapterVerKeys = keys.filter((key) => key.startsWith("chapterver:"));
  if (!taskIds.length && !versionKeys.length && !chapterVerKeys.length) {
    toast("先勾选要删除的记录");
    return;
  }
  const errorEl = $("#writerManageError");
  if (errorEl) errorEl.textContent = "";
  // 任务记录：只清记录不删内容。
  if (taskIds.length) {
    if (versionKeys.length || chapterVerKeys.length) {
      toast("任务记录和版本要分开删：先删一种，再删另一种");
      return;
    }
    if (!window.confirm(
      `要删除 ${taskIds.length} 条生成记录吗？\n只删任务记录，不删除从它保存的设定、大纲或正文。`,
    )) return;
    try {
      await api("/api/studio/tasks", { method: "DELETE", body: { ids: taskIds } });
      writer.manage.selected.clear();
      await loadManageTasks();
      toast("已删除选中的生成记录");
    } catch (error) {
      if (errorEl) errorEl.textContent = String(error?.message || error);
      fail("删除记录失败", error);
    }
    return;
  }
  // 正文版本：一章一接口，按章分组。
  if (chapterVerKeys.length) {
    const byChapter = new Map();
    for (const key of chapterVerKeys) {
      const [, no, vid] = key.split(":");
      const list = byChapter.get(Number(no)) || [];
      list.push(vid);
      byChapter.set(Number(no), list);
    }
    if (!window.confirm(
      `要删除 ${chapterVerKeys.length} 个正文版本吗？\n正文版本文件会一并删除，不能恢复；删到当前版本时指针会落到最新剩余版本。`,
    )) return;
    try {
      for (const [no, ids] of byChapter) {
        // 正文版本走章节自己的 revision（与「设为当前」同一口径）。
        const chapterRow = writer.chapters.find((row) => Number(row.no) === no);
        await api(
          `/api/writer/books/${encodeURIComponent(writer.bookId)}/chapters/${no}/versions/delete`,
          { method: "POST", body: { version_ids: ids, revision: chapterRow?.revision } },
        );
      }
      writer.manage.selected.clear();
      await openBook(writer.bookId, { keepStage: true });
      renderManage();
      toast(`已删除 ${chapterVerKeys.length} 个正文版本`);
    } catch (error) {
      if (errorEl) errorEl.textContent = String(error?.message || error);
      fail("删除正文版本失败", error);
    }
    return;
  }
  // 设定 / 大纲版本。
  const kind = writer.manage.tab === "outline" ? "outline" : "setup";
  const ids = versionKeys.map((key) => key.slice("version:".length));
  const currentId = kind === "setup"
    ? String(writer.book?.current_setup_version_id || "")
    : String(writer.book?.current_outline_version_id || "");
  if (!window.confirm(
    `要删除 ${ids.length} 个${kind === "setup" ? "设定" : "大纲"}版本吗？\n`
    + `版本文件会一并删除，不能恢复${ids.includes(currentId) ? "；当前版本被删时会自动切到最新剩余版本" : ""}。`,
  )) return;
  try {
    await api(`/api/writer/books/${encodeURIComponent(writer.bookId)}/versions/delete`, {
      method: "POST",
      body: { kind, version_ids: ids, revision: writer.book?.revision },
    });
    writer.manage.selected.clear();
    await openBook(writer.bookId, { keepStage: true });
    renderManage();
    toast(`已删除 ${ids.length} 个${kind === "setup" ? "设定" : "大纲"}版本`);
  } catch (error) {
    if (errorEl) errorEl.textContent = String(error?.message || error);
    fail("删除版本失败", error);
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
  const next = ["setup", "outline", "chapter", "draft"].includes(stage) ? stage : "setup";
  writer.stage = next;
  stageAtRequest = next;
  document.querySelectorAll("#writerStages [data-writer-stage]").forEach((btn) => {
    const active = btn.dataset.writerStage === next;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
  renderEditor();
  renderSide();
  renderChapters();
  renderTocLimits();
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
  // 设定集文件夹导入：webkitdirectory 选择整个文件夹，读取其中全部 .md。
  $("#writerImportSettingsBtn")?.addEventListener("click", () => {
    if (!writer.bookId) {
      toast("先打开（或新建）一部作品，再导入设定集");
      return;
    }
    $("#writerImportSettingsInput")?.click();
  });
  $("#writerImportSettingsInput")?.addEventListener("change", (event) => {
    const files = event.target?.files;
    void importSettingsFolder(files);
    if (event.target) event.target.value = "";
  });
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
  for (const id of ["writerIdea", "writerTitle", "writerBlurb", "writerWorldview",
    "writerGenreTone", "writerGenreCustom", "writerVibe", "writerAvoid"]) {
    $(`#${id}`)?.addEventListener("input", () => {
      writer.setupDirty = true;
      scheduleDraftSave();
    });
  }
  $("#writerStyle")?.addEventListener("change", (event) => {
    writer.setupDirty = true;
    scheduleDraftSave();
    void applyModelSkinFor(String(event.target.value || ""));
  });

  // 题材树：点频道 / 分类 / 二级
  $("#writerGenrePicker")?.addEventListener("click", (event) => {
    const channelBtn = event.target.closest("[data-writer-genre-channel]");
    const catBtn = event.target.closest("[data-writer-genre-cat]");
    const subBtn = event.target.closest("[data-writer-genre-sub]");
    if (channelBtn && !catBtn) {
      writer.genrePick = { channel: String(channelBtn.dataset.writerGenreChannel || "male"), cat: "", sub_cat: "" };
    } else if (catBtn) {
      writer.genrePick = {
        channel: String(catBtn.dataset.writerGenreChannel || writer.genrePick.channel || "male"),
        cat: String(catBtn.dataset.writerGenreCat || ""),
        sub_cat: "",
      };
    } else if (subBtn) {
      writer.genrePick = { ...writer.genrePick, sub_cat: String(subBtn.dataset.writerGenreSub || "") };
    } else {
      return;
    }
    writer.setupDirty = true;
    renderGenrePicker();
    scheduleDraftSave();
  });

  // 标签：搜索 + 多选（上限 20）
  $("#writerTagSearch")?.addEventListener("input", () => renderTags());
  $("#writerTagBox")?.addEventListener("click", (event) => {
    const chip = event.target.closest("[data-writer-tag]");
    if (!chip) return;
    const tag = String(chip.dataset.writerTag || "");
    if (!tag) return;
    const index = writer.tags.indexOf(tag);
    if (index >= 0) {
      writer.tags.splice(index, 1);
    } else {
      if (writer.tags.length >= TAGS_MAX) {
        toast(`最多选 ${TAGS_MAX} 个标签`);
        return;
      }
      writer.tags.push(tag);
    }
    writer.setupDirty = true;
    renderTags();
    scheduleDraftSave();
  });

  // 参考作品
  $("#writerRefAdd")?.addEventListener("click", () => {
    if (writer.refs.length >= REFS_MAX) {
      toast(`最多 ${REFS_MAX} 本参考`);
      return;
    }
    writer.refs.push({ title: "", dims: [], note: "", same_fanfic: false });
    writer.setupDirty = true;
    renderRefs();
    scheduleDraftSave();
  });
  $("#writerRefList")?.addEventListener("input", (event) => {
    const titleEl = event.target.closest("[data-writer-ref-title]");
    const noteEl = event.target.closest("[data-writer-ref-note]");
    const sameEl = event.target.closest("[data-writer-ref-same]");
    if (titleEl) writer.refs[Number(titleEl.dataset.writerRefTitle)] && (writer.refs[Number(titleEl.dataset.writerRefTitle)].title = String(titleEl.value || ""));
    else if (noteEl) writer.refs[Number(noteEl.dataset.writerRefNote)] && (writer.refs[Number(noteEl.dataset.writerRefNote)].note = String(noteEl.value || ""));
    else if (sameEl) writer.refs[Number(sameEl.dataset.writerRefSame)] && (writer.refs[Number(sameEl.dataset.writerRefSame)].same_fanfic = Boolean(sameEl.checked));
    else return;
    writer.setupDirty = true;
    scheduleDraftSave();
  });
  $("#writerRefList")?.addEventListener("click", (event) => {
    const remove = event.target.closest("[data-writer-ref-remove]");
    if (remove) {
      writer.refs.splice(Number(remove.dataset.writerRefRemove), 1);
      writer.setupDirty = true;
      renderRefs();
      scheduleDraftSave();
      return;
    }
    const dim = event.target.closest("[data-writer-ref-dim]");
    if (!dim) return;
    const index = Number(dim.dataset.writerRefIndex);
    const ref = writer.refs[index];
    if (!ref) return;
    const value = String(dim.dataset.writerRefDim || "");
    ref.dims = ref.dims || [];
    const at = ref.dims.indexOf(value);
    if (at >= 0) ref.dims.splice(at, 1);
    else ref.dims.push(value);
    writer.setupDirty = true;
    renderRefs();
    scheduleDraftSave();
  });

  // 角色分区：主角 / 配角
  $("#writerCharTabs")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-writer-char-tab]");
    if (!btn) return;
    writer.charTab = String(btn.dataset.writerCharTab || "lead");
    renderCharacters();
  });
  $("#writerCharAdd")?.addEventListener("click", () => {
    const chars = collectCharacters();
    const tab = writer.charTab === "side" ? "side" : "lead";
    const used = chars.filter((row) => row.role === tab).length;
    if (tab === "lead" && used >= LEAD_MAX) {
      toast(`主角最多 ${LEAD_MAX} 人`);
      return;
    }
    if (tab === "side" && used >= SIDE_MAX) {
      toast(`配角最多 ${SIDE_MAX} 人`);
      return;
    }
    chars.push({ name: "", role: tab });
    writer.setup = { ...(writer.setup || {}), characters: chars };
    writer.setupDirty = true;
    scheduleDraftSave();
    renderCharacters();
  });
  // 角色卡的字段是动态生成的，不在上面那批 id 里：单独委托，否则改角色同样算「未保存」漏掉。
  $("#writerCharList")?.addEventListener("input", (event) => {
    if (!event.target.closest("[data-writer-char-field]")) return;
    writer.setupDirty = true;
    scheduleDraftSave();
  });
  $("#writerCharList")?.addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-writer-char-toggle]");
    if (toggle) {
      const card = toggle.closest(".writer-char-card");
      const more = card?.querySelector(".writer-char-more");
      if (more) {
        more.hidden = !more.hidden;
        toggle.textContent = more.hidden ? "展开全部字段" : "收起字段";
      }
      return;
    }
    const remove = event.target.closest("[data-writer-char-remove]");
    if (!remove) return;
    const index = Number(remove.dataset.writerCharRemove);
    const chars = collectCharacters();
    chars.splice(index, 1);
    writer.setup = { ...(writer.setup || {}), characters: chars };
    writer.setupDirty = true;
    scheduleDraftSave();
    renderCharacters();
  });
  $("#writerSetupClear")?.addEventListener("click", () => {
    if (writer.setupDirty && !window.confirm("设定表单里有未保存的修改，确定清空吗？")) return;
    writer.setup = null;
    writer.setupDirty = false;
    clearWriterDraft(writer.bookId);
    fillSetupForm();
    renderCharacters();
    renderPageState();
    toast("已清空设定表单（作品里已保存的版本不受影响）");
  });
  $("#writerSetupSave")?.addEventListener("click", () => void saveSetupManual());
  $("#writerSetupGenerate")?.addEventListener("click", () => void submitPipeline("setup_all"));
  for (const id of ["writerCharCount", "writerSideCount"]) {
    $(`#${id}`)?.addEventListener("input", (event) => {
      const value = Number(event.target.value || 0);
      if (id === "writerCharCount") writer.charCount = value;
      else writer.sideCount = value;
      const label = $(`#${id}Value`);
      if (label) label.textContent = String(value);
      saveLimits();
      void refreshPlan();
    });
  }
  $("#writerSetupNote")?.addEventListener("change", () => void refreshPlan());
  // 左栏「设定版本」：点一版切回。这一列此前漏绑了点击事件（点了一直没反应）；
  // 表单有未保存修改时先确认，别让切换默默丢稿。
  $("#writerSetupVersionList")?.addEventListener("click", (event) => {
    const row = event.target.closest("[data-writer-setup-version]");
    if (!row) return;
    const versionId = String(row.dataset.writerSetupVersion || "");
    if (!versionId || versionId === String(writer.book?.current_setup_version_id || "")) return;
    if (writer.setupDirty && !window.confirm("设定表单有还没保存的修改，切版本会丢掉这些修改。继续吗？")) return;
    void selectSetupVersion(versionId);
  });

  // ---- 模型选择 ----
  for (const [providerId, modelId, providerField, modelField] of [
    ["writerSetupProvider", "writerSetupModel", "setupProviderKey", "setupModelName"],
    ["writerOutlineProvider", "writerOutlineModel", "outlineProviderKey", "outlineModelName"],
    ["writerChapterProvider", "writerChapterModel", "chapterProviderKey", "chapterModelName"],
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

  // ---- 大纲（分部文字） ----
  $("#writerOutlineParts")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-writer-part]");
    if (!btn) return;
    if (writer.partsDirty && !window.confirm("这一部有未保存的修改，切部会丢掉。继续吗？")) return;
    writer.partNo = Number(btn.dataset.writerPart || 1);
    writer.partsDirty = false;
    renderOutline();
  });
  // 部标题/大纲正文改动即置脏：渲染让路，避免切阶段把输入抹掉。
  for (const id of ["writerPartTitle", "writerOutlineText"]) {
    $(`#${id}`)?.addEventListener("input", () => {
      writer.partsDirty = true;
      renderOutlineStat();
      scheduleDraftSave();
    });
  }
  $("#writerOutlineGenerate")?.addEventListener("click", () => void generatePartText());
  $("#writerOutlineGenerateAll")?.addEventListener("click", () => void generateAllParts());
  $("#writerOutlineReview")?.addEventListener("click", () => void generateOutlineReview());
  $("#writerPartsApply")?.addEventListener("click", () => applyPartsTarget());
  $("#writerOutlineSave")?.addEventListener("click", () => void savePartsManual());
  $("#writerPartAdd")?.addEventListener("click", () => addPart());
  $("#writerPartRemove")?.addEventListener("click", () => removePart());
  $("#writerOutlineUpgrade")?.addEventListener("click", () => void upgradeLegacyOutline());
  $("#writerGotoChapter")?.addEventListener("click", () => setStage("chapter"));
  $("#writerOutlineNote")?.addEventListener("change", () => void refreshPlan());
  $("#writerOutlineVersion")?.addEventListener("change", (event) => {
    void selectOutlineVersion(String(event.target.value || ""));
  });

  // ---- 章节（拆分 + 简述） ----
  $("#writerSplitStart")?.addEventListener("click", () => void startSplit());
  $("#writerSplitContinue")?.addEventListener("click", () => void continueSplit());
  // 「拆哪一部」是独立选择：记住它，别让任何重渲染把它拽回大纲页当前部（第三次反馈⑥）。
  $("#writerSplitPart")?.addEventListener("change", (event) => {
    writer.splitPartNo = Number(event.target.value || 1);
  });
  // 字数联动：改章数或单章字数 → 总字数跟着变；改总字数 → 反推章数（第三次反馈⑤）。
  $("#writerSplitCount")?.addEventListener("input", syncSplitTotalChars);
  $("#writerSplitTotalChars")?.addEventListener("input", syncSplitTotalChars);
  $("#writerSplitTotalChars")?.addEventListener("change", applySplitTotalChars);
  for (const id of ["writerChapterChars", "writerChapterEnding"]) {
    $(`#${id}`)?.addEventListener("change", () => {
      void saveChapterPlan();
      syncSplitTotalChars();
    });
  }
  // 简述/标题失焦保存（input 不落盘：改一个字就发一次请求既慢又容易撞 revision）。
  $("#writerBriefList")?.addEventListener("change", (event) => {
    const titleEl = event.target.closest("[data-writer-brief-title]");
    const beatEl = event.target.closest("[data-writer-brief-beat]");
    if (titleEl) {
      const no = Number(titleEl.dataset.writerBriefTitle || 0);
      void updateChapterMeta(no, { title: String(titleEl.value || "") });
      return;
    }
    if (beatEl) {
      const no = Number(beatEl.dataset.writerBriefBeat || 0);
      void updateChapterMeta(no, { beat: String(beatEl.value || "") });
    }
  });
  $("#writerBriefList")?.addEventListener("click", (event) => {
    const open = event.target.closest("[data-writer-brief-open]");
    if (!open) return;
    setStage("draft");
    void openChapter(Number(open.dataset.writerBriefOpen || 0));
  });
  // 章节页左栏切大纲版本：切换后汇总与「拆哪一部」都会跟着新版本走。
  $("#writerChapterOutlineVersion")?.addEventListener("change", (event) => {
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
    // 当前页签里所有可勾选的行（设定/大纲版本页=版本，任务页=生成记录）。
    const prefix = writer.manage.tab === "task" ? "task:"
      : writer.manage.tab === "chapter" ? "chapter:" : "version:";
    for (const row of filteredManageRows()) {
      if (manageRowSelectable(row)) writer.manage.selected.add(`${prefix}${row.id}`);
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
      // 切走时立刻把草稿落一次：延时写入的定时器活不过这一次切换。
      if (draftTimer) {
        clearTimeout(draftTimer);
        draftTimer = 0;
        writeWriterDraft(writer.bookId);
      }
      return;
    }
    writer.visible = true;
    // 模型下拉依赖 bootstrap 的供应商配置：首屏时它可能还没到，进页面时重算一次。
    fillProviderModels();
    void loadLocation();
    // 进页不重填：草稿/未保存修改一律优先于服务端已保存版本（keepStage 同理，
    // 否则在正文页切走再回来会被踢回设定页）。
    const unsaved = writer.setupDirty || writer.draftDirty || writer.partsDirty;
    if (writer.bookId) void openBook(writer.bookId, { keepEdits: unsaved, keepStage: true });
    else {
      let remembered = "";
      try { remembered = localStorage.getItem(LAST_BOOK_KEY) || ""; } catch (_error) { /* storage may be unavailable */ }
      if (remembered) void openBook(remembered);
    }
  });

  // 页面被刷新/关窗时把草稿落一次（定时器来不及跑）。
  window.addEventListener("pagehide", () => {
    if (draftTimer) {
      clearTimeout(draftTimer);
      draftTimer = 0;
    }
    writeWriterDraft(writer.bookId);
  });

  renderAll();
}

function renderExportNote() {
  const noteEl = $("#writerExportNote");
  if (!noteEl) return;
  // 字符类里的 `/` 要写成 `\/`：esm_graph_check 的正则扫描按「第一个未转义斜杠」收尾，
  // 类内裸斜杠会让它提前闭合、引号配对错位，后面的字符串被当成代码（误报 import 漏引）。
  const title = (writer.book?.title || "作品").replace(/[\\\/:*?"<>|]/g, "_");
  const count = writer.chapters.length;
  const missing = writer.chapters.filter((row) => Number(row.len || 0) <= 0).length;
  const parts = [];
  if (count) parts.push(`全书 ${count} 章${missing ? `（${missing} 章还没有正文，导出时会跳过）` : ""}。`);
  if (count && missing === count) {
    parts.push("整本还没有正文，纯正文导出会是空文件；要带走设定与大纲请选「导出 Markdown（含设定与大纲）」。");
  }
  // 端不同，落点就不同：桌面是「另存为」，手机/浏览器是下载。写死了只会误导一半用户。
  parts.push(isPywebview()
    ? `导出会先在保存目录的 _exports 里生成文件（默认名「${title}.txt」/「${title}.md」），随后弹出系统「另存为」。`
    : `导出由浏览器下载，文件名「${title}.txt」或「${title}.md」，通常保存到浏览器默认下载目录。`);
  noteEl.textContent = parts.join("");
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
    if (Number.isFinite(Number(saved.char_count)) && Number(saved.char_count) >= 1 && Number(saved.char_count) <= LEAD_MAX) {
      writer.charCount = Number(saved.char_count);
    }
    if (Number.isFinite(Number(saved.side_count)) && Number(saved.side_count) >= 0 && Number(saved.side_count) <= 20) {
      writer.sideCount = Number(saved.side_count);
    }
  } catch (_error) {
    // 存储不可用或值损坏：保持默认值。
  }
  const charRange = $("#writerCharCount");
  if (charRange) charRange.value = String(writer.charCount);
  const charLabel = $("#writerCharCountValue");
  if (charLabel) charLabel.textContent = String(writer.charCount);
  const sideRange = $("#writerSideCount");
  if (sideRange) sideRange.value = String(writer.sideCount);
  const sideLabel = $("#writerSideCountValue");
  if (sideLabel) sideLabel.textContent = String(writer.sideCount);
}

function saveLimits() {
  try {
    localStorage.setItem(LIMITS_KEY, JSON.stringify({
      char_count: writer.charCount,
      side_count: writer.sideCount,
    }));
  } catch (_error) {
    // 存储不可用：参数只在本次会话内生效。
  }
}
