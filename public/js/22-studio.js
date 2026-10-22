// ============================================================
// 22-studio.js —— 工作台（Studio，_studio_plan_v2.md）
// 独立生成域：目录检索（预设 559 条 + 自定义）→ 填变量/自由输入 → 生成（Studio 自有
// 异步任务，/api/studio/*）→ 结果留在工作台 → 「继续追问」经必选 Agent 框交接普通对话。
// 不直接编排会话创建/聊天 Run/事件流，仅调 handoff API（单一跨域口）。
// 应用级视图：入口在侧栏「对话 / 工作台」模式切换，不再是顶栏会话按钮。
// ============================================================

import { $, api, state, toast } from "./01-core.js";
import { populateComposerModels } from "./07-models-agents.js";
import { openConversation, renderSidebar } from "./08-conversations.js";
import { closeSidebar, openSidebar, setLeftSidebarCollapsed, sidebarDesktop } from "./14-file-panel.js";

// ---- 模块清单（目录条目 module -> 显示名；顺序即分类顺序） ----
const MODULE_TABS = [
  { key: "", label: "全部" },
  { key: "vs", label: "文生图" },
  { key: "h3", label: "视频" },
  { key: "vi", label: "反推" },
  { key: "ec", label: "电商" },
  { key: "global", label: "通用" },
  { key: "script", label: "剧本" },
  { key: "game", label: "游戏原画" },
  { key: "style", label: "风格" },
  { key: "custom", label: "我的" },
];

const TYPE_LABELS = { template: "模板", rule: "规则", lexicon: "词条", example: "示例" };

// 任务状态：界面只说中文，不直接露英文状态码。
const TASK_STATUS_LABELS = {
  queued: "排队中",
  running: "生成中",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};

// 结果栏的两种模式：任务结果 / 历史产出列表。两者互斥显示。
const RESULT_MODES = ["result", "history"];
// 手机三档：模板 / 参数 / 结果。
const PANES = ["list", "form", "result"];
// 标题区最近任务捷径固定最多 3 条；完整列表走侧栏「历史产出」。
const RECENT_TASK_LIMIT = 3;
// 列表接口自身最多返回 60 条；这是展示范围提示，不是分页参数。
const HISTORY_RANGE_HINT = "最近任务，最多显示 60 条";

const studio = {
  providerModelsCache: new Map(),
  open: false,
  summary: null,
  module: "",
  query: "",
  kind: "free", // free | builtin | custom
  itemId: "",
  customId: "",
  meta: null,
  body: "",
  varValues: {},
  providerKey: "",
  modelName: "",
  catalogLoaded: false,
  customLoaded: false,
  tasks: [],
  tasksLoaded: false,
  tasksError: "",
  resultMode: "result",
  historyState: "ready", // ready | loading | error
  pane: "form",
  activeTaskId: "",
  eventCursor: 0,
  pollBusy: false,
  pollTimer: null,
  agentPicked: "",
  streamText: "",
  reasoningText: "",
  // 迟到响应保护：列表 / 详情 / 任务各自一个令牌，响应回来时核对是否仍是当前请求。
  listToken: 0,
  detailToken: 0,
  taskToken: 0,
  historyToken: 0,
};

// ============================================================
// 视图开关（对话 / 工作台）
// ============================================================

export function isStudioOpen() {
  return studio.open;
}

function syncModeUi() {
  const open = studio.open;
  document.body.dataset.studio = open ? "on" : "off";
  const view = $("#studioView");
  if (view) view.hidden = !open;
  const studioBtn = $("#openStudio");
  if (studioBtn) {
    studioBtn.setAttribute("aria-pressed", String(open));
    studioBtn.classList.toggle("active", open);
  }
  const chatBtn = $("#openChatMode");
  if (chatBtn) {
    chatBtn.setAttribute("aria-pressed", String(!open));
    chatBtn.classList.toggle("active", !open);
  }
  const nav = $("#sidebarStudioNav");
  if (nav) nav.hidden = !open;
}

// 进入工作台：已在工作台时再点也不退出（退出由「对话」负责）。
export function openStudioView() {
  if (studio.open) return;
  studio.open = true;
  syncModeUi();
  setStudioPane(studio.pane);
  if (!sidebarDesktop()) closeSidebar();
  void refreshStudio();
  if (studio.pane === "form") focusStudioInput();
}

export function closeStudioView() {
  if (!studio.open) return;
  studio.open = false;
  syncModeUi();
  stopStudioPolling();
  // 会话树在隐藏期间尺寸为 0，恢复显示后必须按真实可用高度重算虚拟窗口。
  renderSidebar();
}

function focusStudioInput() {
  const input = $("#studioUserRequest");
  if (input && !input.closest("dialog") && !input.closest("[hidden]")) setTimeout(() => input.focus(), 60);
}

// 重进工作台时按服务端详情校准活动任务：离开期间可能已完成/失败/被取消，
// 不能凭上次渲染的快照决定是否继续轮询（否则会把已终结任务当运行中，或漏掉终态正文）。
async function reconcileActiveTask() {
  const taskId = studio.activeTaskId;
  if (!taskId) return;
  const token = ++studio.taskToken;
  try {
    const { task } = await api(`/api/studio/tasks/${encodeURIComponent(taskId)}`);
    if (token !== studio.taskToken || studio.activeTaskId !== taskId || !studio.open) return;
    renderResultPanel(task);
    if (task.status === "queued" || task.status === "running") {
      startStudioPolling();
    } else {
      stopStudioPolling();
    }
  } catch (error) {
    if (token !== studio.taskToken || studio.activeTaskId !== taskId || !studio.open) return;
    stopStudioPolling();
    if (error.status === 404) {
      // 任务记录已不存在：清掉活动任务，界面回到空结果态，不继续轮询。
      studio.activeTaskId = "";
      studio.lastResult = null;
      studio.streamText = "";
      updateResultStream("");
      return;
    }
    toast(`任务状态读取失败：${error.message}`);
  }
}

async function refreshStudio() {
  // 离开期间任务可能已经结束：重进时以服务端最新状态为准，不拿可能过期的快照决定订阅。
  await reconcileActiveTask();
  if (!studio.catalogLoaded) {
    try {
      const { catalog } = await api("/api/studio/catalog");
      studio.summary = catalog;
      studio.catalogLoaded = true;
    } catch (error) {
      toast(`工作台目录读取失败：${error.message}`);
    }
  }
  // 自定义条目计数要真实：先取一次（本地只读接口），不把未加载伪装成 0。
  await loadCustomItems();
  renderStudioCategories();
  // 只在还没有选中条目时重画一次默认态（「自由输入」）；已有选择时不重写，避免
  // 把正在编辑的自定义正文/变量草稿冲掉。
  if (!studio.meta) renderStudioDetail();
  await Promise.all([renderStudioList(), refreshTasks()]);
  syncStudioModel();
}

// ============================================================
// 手机三档
// ============================================================

function setStudioPane(pane) {
  const next = PANES.includes(pane) ? pane : "form";
  studio.pane = next;
  const view = $("#studioView");
  if (view) view.dataset.pane = next;
  document.querySelectorAll(".studio-pane-btn").forEach((btn) => {
    const active = btn.dataset.studioPane === next;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
}

// ============================================================
// 顶栏 / 分类 / 列表 / 详情
// ============================================================

function renderStudioCategories() {
  const wrap = $("#studioCats");
  if (!wrap) return;
  const counts = (studio.summary && studio.summary.modules) || {};
  const total = studio.summary ? Number(studio.summary.count || 0) : null;
  wrap.replaceChildren(...MODULE_TABS.map((tab) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "studio-cat";
    button.setAttribute("aria-pressed", String(tab.key === studio.module));
    button.dataset.studioModule = tab.key;
    let count = "";
    if (tab.key === "custom") count = studio.customLoaded ? String(customCount()) : "";
    else if (tab.key === "") count = total === null ? "" : String(total);
    else count = counts[tab.key] ?? "";
    const name = document.createElement("span");
    name.className = "studio-cat-name";
    name.textContent = tab.label;
    const num = document.createElement("em");
    num.className = "studio-cat-count";
    num.textContent = count;
    button.append(name, num);
    return button;
  }));
}

function customCount() {
  return studio.customItems?.length ?? "";
}

async function loadCustomItems() {
  if (studio.customLoaded) return;
  try {
    const { items, corrupt } = await api("/api/studio/custom");
    studio.customItems = items;
    studio.customCorrupt = corrupt || [];
    studio.customLoaded = true;
    if (corrupt?.length) {
      toast(`${corrupt.length} 个自定义条目文件损坏，已跳过（不影响其他条目）`);
    }
  } catch (error) {
    toast(`自定义条目读取失败：${error.message}`);
  }
}

async function selectStudioCategory(key) {
  studio.module = key;
  studio.kind = key === "custom" ? "custom" : "free";
  studio.itemId = "";
  if (key === "custom") studio.customId = "";
  // 换分类只换条目来源与详情展示；「我的输入」草稿、变量值和模型选择都保留，
  // 不因为导航动作清空用户已填内容。
  studio.meta = null;
  studio.body = "";
  studio.detailToken += 1;
  setResultMode("result");
  setStudioPane("list");
  renderStudioCategories();
  renderStudioDetail();
  await renderStudioList();
}

async function renderStudioList() {
  const list = $("#studioList");
  if (!list) return;
  list.replaceChildren(buildIndicator());
  const token = ++studio.listToken;
  if (studio.module === "custom") {
    await loadCustomItems();
    if (token !== studio.listToken) return;
    renderStudioCategories();
    const rows = [...(studio.customItems || [])].filter((row) => matchesQuery(row));
    if (!rows.length && !studio.query) {
      list.replaceChildren(emptyListPanel("还没有自定义条目", '在上面"我的输入"里写下提示词，点"生成"后可以先入口；也可以把已存的自定义导出/导入。'));
      return;
    }
    list.replaceChildren(...rows.map((row) => studioListRow({
      kind: "custom", id: row.id, title: row.title, summary: row.summary || row.body?.slice(0, 90) || "",
      tags: row.tags, extra: row.module === "style" ? "词条" : TYPE_LABELS[row.type] || "",
      genre: row.genre, chars: row.chars, active: studio.kind === "custom" && studio.customId === row.id,
    })));
    if (!rows.length) list.replaceChildren(emptyListPanel("无匹配自定义条目", "换个搜索词试试。"));
    return;
  }
  try {
    const params = new URLSearchParams();
    if (studio.module) params.set("module", studio.module);
    if (studio.query) params.set("q", studio.query);
    const { items } = await api(`/api/studio/catalog/items?${params}`);
    if (token !== studio.listToken) return; // 分类/搜索已变，丢弃这次结果
    if (studio.module && items.length === 0 && studio.query) {
      toast("本模块没有匹配条目");
    }
    if (!items.length) {
      list.replaceChildren(emptyListPanel("没有匹配条目", "换一个分类或搜索词。"));
      return;
    }
    const shown = items.slice(0, 150);
    list.replaceChildren(...shown.map((row) => studioListRow({
      kind: "builtin", id: row.id, title: row.title, summary: row.summary,
      tags: row.tags, extra: TYPE_LABELS[row.type] || "", genre: row.genre,
      chars: row.chars, hasVars: row.has_vars,
      active: studio.kind === "builtin" && studio.itemId === row.id,
    })));
    if (items.length > shown.length) {
      list.append(emptyListPanel(`已显示前 ${shown.length} 条（共 ${items.length}）`, "输入关键词缩小范围。"));
    }
  } catch (error) {
    if (token !== studio.listToken) return;
    list.replaceChildren(emptyListPanel("目录读取失败", String(error.message || error)));
  }
}

function buildIndicator() {
  const p = document.createElement("p");
  p.className = "studio-list-loading";
  p.textContent = "正在读取目录…";
  return p;
}

function emptyListPanel(title, hint) {
  const panel = document.createElement("div");
  panel.className = "studio-empty";
  const strong = document.createElement("strong");
  strong.textContent = title;
  const small = document.createElement("small");
  small.textContent = hint;
  panel.append(strong, document.createElement("br"), small);
  return panel;
}

function matchesQuery(row) {
  const needle = studio.query.trim().toLowerCase();
  if (!needle) return true;
  const hay = [row.title, row.summary, row.genre, ...(row.tags || [])].join(" ").toLowerCase();
  return hay.includes(needle);
}

function studioListRow({ kind, id, title, summary, tags, extra, genre, chars, hasVars, active }) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `studio-row${active ? " active" : ""}`;
  button.dataset.studioPick = id;
  button.dataset.studioKind = kind;
  const head = document.createElement("div");
  head.className = "studio-row-head";
  const titleEl = document.createElement("span");
  titleEl.className = "studio-row-title";
  titleEl.textContent = title || "（未命名）";
  head.appendChild(titleEl);
  const chips = document.createElement("span");
  chips.className = "studio-row-chips";
  [extra, genre, hasVars ? "含变量" : ""].filter(Boolean).forEach((chip) => {
    const chipEl = document.createElement("em");
    chipEl.textContent = chip;
    chips.appendChild(chipEl);
  });
  (tags || []).slice(0, 3).forEach((tag) => {
    const chipEl = document.createElement("em");
    chipEl.textContent = tag;
    chips.appendChild(chipEl);
  });
  head.appendChild(chips);
  const summaryEl = document.createElement("p");
  summaryEl.className = "studio-row-summary";
  summaryEl.textContent = (summary || "").slice(0, 110);
  button.append(head, summaryEl);
  return button;
}

async function selectStudioEntry(kind, id) {
  const token = ++studio.detailToken;
  studio.kind = kind;
  studio.meta = null;
  studio.body = "";
  studio.varValues = {};
  setResultMode("result");
  if (kind === "builtin") {
    studio.itemId = id;
    studio.customId = "";
    await loadCatalogEntry(token);
  } else if (kind === "custom") {
    studio.customId = id;
    studio.itemId = "";
    await loadCustomEntry(token);
  }
  if (token !== studio.detailToken) return;
  renderStudioDetail();
  setStudioPane("form");
  await renderStudioList();
}

async function loadCatalogEntry(token) {
  try {
    const { item, body } = await api(`/api/studio/catalog/${encodeURIComponent(studio.itemId)}`);
    if (token !== studio.detailToken) return;
    studio.meta = item;
    studio.body = body;
  } catch (error) {
    if (token !== studio.detailToken) return;
    toast(`条目读取失败：${error.message}`);
    studio.itemId = "";
    studio.kind = "free";
  }
}

async function loadCustomEntry(token) {
  // 自定义列表接口自带 body（custom.list 直读每条 JSON），不需要第二个接口。
  // 捕获发起时的 id：await 期间用户可能已切到别的条目/内置条目。
  const wantedId = studio.customId;
  if (!studio.customLoaded) await loadCustomItems();
  if (token !== studio.detailToken || studio.customId !== wantedId) return;
  const found = (studio.customItems || []).find((r) => r.id === wantedId);
  studio.meta = found || null;
  studio.body = found?.body || "";
}

function renderStudioDetail() {
  const wrap = $("#studioDetail");
  const titleEl = $("#studioDetailTitle");
  const summaryEl = $("#studioDetailSummary");
  const chipsEl = $("#studioDetailChips");
  const bodyEl = $("#studioBodyPreview");
  const varsEl = $("#studioVarsForm");
  const requestField = $("#studioUserRequest");
  if (!wrap || !titleEl) return;
  const selected = Boolean(studio.meta);
  wrap.hidden = false;
  if (selected) {
    const moduleLabel = MODULE_TABS.find((m) => m.key === studio.meta.module)?.label || studio.meta.module;
    titleEl.textContent = studio.meta.title;
    summaryEl.textContent = studio.meta.summary || "";
    chipsEl.replaceChildren();
    [moduleLabel, TYPE_LABELS[studio.meta.type] || studio.meta.type].filter(Boolean).forEach((chip) => {
      const el = document.createElement("span");
      el.className = "studio-chip";
      el.textContent = chip;
      chipsEl.append(el);
    });
    (studio.meta.tags || []).slice(0, 6).forEach((tag) => {
      const el = document.createElement("span");
      el.className = "studio-chip soft";
      el.textContent = tag;
      chipsEl.append(el);
    });
    if (studio.meta.source && studio.meta.origin === "builtin") {
      const src = document.createElement("span");
      src.className = "studio-chip soft";
      src.title = "原始来源文件";
      src.textContent = `来源 ${studio.meta.source}`;
      chipsEl.append(src);
    }
    bodyEl.textContent = studio.body;
    bodyEl.hidden = false;
    const editor = $("#studioBodyEditor");
    const editorHint = $("#studioBodyEditableHint");
    const saveBtn = $("#studioCustomSave");
    const deleteBtn = $("#studioCustomDelete");
    const exportBtn = $("#studioCustomExport");
    const isCustom = studio.kind === "custom" && studio.customId;
    if (editor) {
      editor.hidden = !isCustom;
      if (isCustom) editor.value = studio.body;
      editor.addEventListener("input", () => { studio.bodyEdited = true; }, { once: true });
    }
    if (editorHint) editorHint.hidden = !isCustom;
    if (saveBtn) saveBtn.hidden = !isCustom;
    if (deleteBtn) deleteBtn.hidden = !isCustom;
    if (exportBtn) exportBtn.hidden = !isCustom;
    studio.bodyEdited = false;
    ["#studioBodyPreviewWrap"].forEach((sel) => { const el = $(sel); if (el && !isCustom) el.removeAttribute("open"); });
    varsEl.replaceChildren();
    (studio.meta.vars || []).forEach((v) => {
      const row = document.createElement("label");
      row.className = "studio-var-field";
      const input = document.createElement("input");
      input.type = "text";
      input.value = studio.varValues[v.key] ?? v.default ?? "";
      input.placeholder = `填 ${v.label}`;
      input.dataset.studioVar = v.key;
      input.addEventListener("input", () => { studio.varValues[v.key] = input.value; });
      const name = document.createElement("span");
      name.textContent = v.label;
      row.append(name, input);
      varsEl.append(row);
    });
    varsEl.style.display = (studio.meta.vars || []).length ? "" : "none";
    if ((studio.meta.vars || []).length) {
      requestField.placeholder = "附加要求（可空）：对这条模板想再说明什么";
    } else {
      requestField.placeholder = "把你的要求写在这里（这条模板没有变量位）";
    }
  } else {
    titleEl.textContent = "自由输入";
    summaryEl.textContent = "不选模板，直接把需求写进来生成。";
    chipsEl.replaceChildren();
    bodyEl.hidden = true;
    bodyEl.textContent = "";
    ["#studioCustomSave", "#studioCustomDelete", "#studioCustomExport", "#studioBodyEditableHint"].forEach((sel) => { const el = $(sel); if (el) el.hidden = true; });
    const editorFree = $("#studioBodyEditor");
    if (editorFree) editorFree.hidden = true;
    varsEl.replaceChildren();
    varsEl.style.display = "none";
    requestField.placeholder = "把你的要求写在这里";
  }
}

// ============================================================
// 模型选择（与聊天同口径：供应商目录 + 已检测模型名）
// ============================================================

async function syncStudioModel() {
  const providerSelect = $("#studioProviderSelect");
  const modelSelect = $("#studioModelSelect");
  if (!providerSelect || !modelSelect) return;
  const profiles = state.bootstrap?.model_profiles || state.bootstrap?.providers || [];
  if (!providerSelect.options.length) {
    providerSelect.replaceChildren();
    profiles.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.model_key;
      opt.textContent = p.name || p.model_key;
      providerSelect.append(opt);
    });
    if (profiles.length) {
      providerSelect.value = $("#modelSelect")?.value || state.bootstrap?.default_model_key || profiles[0].model_key;
    }
  } else {
    providerSelect.value = $("#modelSelect")?.value || providerSelect.value;
  }
  if (studio.providerKey && profiles.some((p) => p.model_key === studio.providerKey)) {
    providerSelect.value = studio.providerKey;
  }
  const key = providerSelect.value;
  studio.providerKey = key;
  if (!key) {
    modelSelect.replaceChildren();
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "请先在设置中添加模型";
    modelSelect.append(opt);
    return;
  }
  if (studio.modelName && [...modelSelect.options].some((o) => o.value === studio.modelName)) {
    modelSelect.value = studio.modelName;
    return;
  }
  let models = studio.providerModelsCache.get(key);
  if (!models) {
    try {
      ({ models } = await api("/api/providers/models", { method: "POST", body: { model_key: key } }));
      studio.providerModelsCache.set(key, models || []);
    } catch (error) {
      studio.providerModelsCache.set(key, []);
    }
  }
  try {
    modelSelect.replaceChildren();
    (models || []).forEach((entry) => {
      const id = typeof entry === "object" ? (entry.id ?? entry.model ?? `${entry.name ?? ""}`) : String(entry);
      const clean = String(id || "").trim();
      if (!clean) return;
      const opt = document.createElement("option");
      opt.value = clean;
      opt.textContent = clean;
      modelSelect.append(opt);
    });
    if (studio.modelName && [...modelSelect.options].some((o) => o.value === studio.modelName)) {
      modelSelect.value = studio.modelName;
    } else if (modelSelect.options.length > 1) {
      modelSelect.value = modelSelect.options[1].value; // 第 0 项是「请重新检测」占位提示（07 同口径），跳过
    }
  } catch (error) {
    modelSelect.replaceChildren();
    const opt = document.createElement("option");
    opt.value = studio.modelName || "";
    opt.textContent = studio.modelName ? `已保存：${studio.modelName}` : "模型目录读取失败";
    modelSelect.append(opt);
  }
}


async function saveCustomEdit() {
  if (!studio.customId) return;
  const editor = $("#studioBodyEditor");
  const bodyText = String(editor?.value ?? "");
  if (!bodyText.trim()) { toast("正文不能为空"); return; }
  try {
    await api(`/api/studio/custom/${studio.customId}`, { method: "PATCH", body: { body: bodyText } });
    studio.customLoaded = false;
    const row = studio.customItems?.find((r) => r.id === studio.customId);
    if (row) row.body = bodyText;
    studio.body = bodyText;
    const preview = $("#studioBodyPreview");
    if (preview) preview.textContent = bodyText;
    toast("已保存修改");
    await loadCustomItems();
    renderStudioCategories();
  } catch (error) {
    toast(`保存失败：${error.message}`);
  }
}

async function deleteCustomEntry() {
  if (!studio.customId) return;
  const row = studio.customItems?.find((r) => r.id === studio.customId);
  const title = row?.title || "这条条目";
  if (!window.confirm(`删除「${title}」？这条操作不能撤回`)) return;
  try {
    await api(`/api/studio/custom/${studio.customId}`, { method: "DELETE" });
    studio.customLoaded = false;
    studio.kind = "custom";
    studio.customId = "";
    studio.meta = null;
    studio.body = "";
    await loadCustomItems();
    renderStudioCategories();
    renderStudioDetail();
    await renderStudioList();
    toast("已删除");
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

function exportCustomEntry() {
  if (!studio.customId) return;
  const row = studio.customItems?.find((r) => r.id === studio.customId);
  if (!row) return;
  const blob = new Blob([JSON.stringify(row, null, 1)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${row.id}.json`;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function pickCustomImportFile() {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".txt,.md,.json";
  input.style.display = "none";
  document.body.append(input);
  input.addEventListener("change", () => {
    void importCustomFile(input.files?.[0]);
    input.remove();
  });
  input.click();
}

async function importCustomFile(file) {
  if (!file) return;
  let text = "";
  try {
    text = await file.text();
  } catch (error) {
    toast(`读取文件失败：${error.message}`);
    return;
  }
  try {
    const { item } = await api("/api/studio/custom/import", {
      method: "POST",
      body: { filename: file.name, content: text },
    });
    studio.customLoaded = false;
    studio.kind = "custom";
    studio.customId = item.id;
    studio.meta = item;
    studio.body = item.body || "";
    await loadCustomItems();
    renderStudioCategories();
    renderStudioDetail();
    await renderStudioList();
    toast(`已导入「${item.title}」`);
  } catch (error) {
    toast(`导入失败：${error.message}`);
  }
}
// ============================================================
// 生成任务：提交 / 订阅 / 取消 / 重试
// ============================================================

async function submitGenerate() {
  const userRequest = String($("#studioUserRequest")?.value || "").trim();
  const vars = { ...studio.varValues };
  if (studio.kind === "builtin" && !studio.itemId && !userRequest) { toast("请选择条目或先写你的需求"); return; }
  const payload = {
    user_request: userRequest,
    vars,
    model_key: $("#studioProviderSelect")?.value || "",
    model_name: $("#studioModelSelect")?.value || "",
  };
  if (studio.kind === "builtin" && studio.itemId) payload.item_id = studio.itemId;
  if (studio.kind === "custom" && studio.customId) payload.custom_id = studio.customId;
  if (!userRequest) { toast("先写你的需求再生成"); focusStudioInput(); return; }
  if (studio.kind === "builtin") {
    const declared = (studio.meta?.vars || []).map((v) => v.key);
    const unfilled = declared.filter((k) => !String(vars[k] ?? "").trim());
    if (unfilled.length && !userRequest) {
      // 未填变量且没有附加要求：可生成（未识别占位符按后端口径原样保留），但先提醒一次。
      const okless = window.confirm(`模板里还有 ${unfilled.length} 个变量没填：${unfilled.join("、")}。\n留空继续生成吗？（占位符会原样保留并提示）`);
      if (!okless) return;
    }
  }
  try {
    // 创建新任务即作废其它任务详情请求：之前点开的历史任务 A 即使晚到也不得接管轮询。
    studio.taskToken += 1;
    const { task, unresolved_vars } = await api("/api/studio/generate", { method: "POST", body: payload });
    if (Array.isArray(unresolved_vars) && unresolved_vars.length) {
      toast(`有 ${unresolved_vars.length} 个占位符没法识别，已原样保留：${unresolved_vars.slice(0, 3).join("、")}`);
    }
    studio.activeTaskId = task.id;
    studio.eventCursor = 0;
    studio.streamText = "";
    studio.reasoningText = "";
    setResultMode("result");
    renderResultPanel(task);
    setStudioPane("result");
    startStudioPolling();
    await refreshTasks();
  } catch (error) {
    // 提交失败留在参数档，不丢输入。
    toast(`创建生成任务失败：${error.message}`);
    setStudioPane("form");
  }
}

async function cancelStudioTask() {
  if (!studio.activeTaskId) return;
  try {
    await api(`/api/studio/tasks/${studio.activeTaskId}/cancel`, { method: "POST", body: {} });
    toast("已请求取消");
  } catch (error) {
    toast(`取消失败：${error.message}`);
  }
}

async function retryStudioTask(taskId) {
  const target = taskId || studio.activeTaskId;
  if (!target) return;
  try {
    // 重试会生成新任务 id：同样立即作废旧详情请求，避免迟到的旧任务接管。
    studio.taskToken += 1;
    const { task } = await api(`/api/studio/tasks/${target}/retry`, { method: "POST", body: {} });
    studio.activeTaskId = task.id;
    studio.eventCursor = 0;
    studio.streamText = "";
    studio.reasoningText = "";
    setResultMode("result");
    renderResultPanel(task);
    setStudioPane("result");
    startStudioPolling();
    await refreshTasks();
    toast("已重新排队");
  } catch (error) {
    toast(`重试失败：${error.message}`);
  }
}

function stopStudioPolling() {
  if (studio.pollTimer) {
    clearInterval(studio.pollTimer);
    studio.pollTimer = null;
  }
}

function startStudioPolling() {
  stopStudioPolling();
  studio.pollTimer = setInterval(() => void pollStudioEvents(), 1200);
  void pollStudioEvents();
}

async function pollStudioEvents() {
  const taskId = studio.activeTaskId;
  if (!taskId || studio.pollBusy) return;
  studio.pollBusy = true;
  try {
    const { status } = await syncTaskEvents(taskId, studio.eventCursor);
    if (studio.activeTaskId !== taskId) return; // 已切到别的任务，迟到的 A 结果不写进 B
    const done = status === "done" || status === "cancelled" || status === "failed" || status === "interrupted";
    if (done) {
      stopStudioPolling();
      const snapshot = await api(`/api/studio/tasks/${taskId}`);
      if (studio.activeTaskId !== taskId) return;
      renderResultPanel(snapshot.task);
      await refreshTasks();
    }
  } catch (error) {
    toast(`生成状态读取失败：${error.message}`);
  } finally {
    studio.pollBusy = false;
  }
}

async function syncTaskEvents(taskId, after) {
  const payload = await api(`/api/studio/tasks/${taskId}/events?after=${after}`);
  if (studio.activeTaskId !== taskId) return { status: "" }; // 旧任务的响应，丢弃
  const nextCursor = payload.last_seq ?? after;
  for (const event of payload.events) {
    consumeStudioEvent(event);
  }
  studio.eventCursor = nextCursor;
  return { status: payload.status };
}

function consumeStudioEvent(event) {
  const kind = String(event.type || "");
  if (kind === "delta") {
    studio.streamText += String(event.content || "");
    updateResultStream(studio.streamText);
    return;
  }
  if (kind === "reasoning_delta") {
    studio.reasoningText += String(event.content || "");
    updateReasoningChip();
    return;
  }
  if (kind === "status") {
    const hint = $("#studioTaskHint");
    if (hint && String(event.message || "")) hint.textContent = String(event.message);
    return;
  }
  if (kind === "usage") {
    const meta = $("#studioResultMeta");
    if (meta) {
      const usage = event.usage || {};
      const tokens = [];
      if (usage.input_tokens) tokens.push(`输入 ${usage.input_tokens}`);
      if (usage.output_tokens) tokens.push(`输出 ${usage.output_tokens}`);
      if (usage.cost) tokens.push(`费用 ${usage.cost}`);
      meta.textContent = tokens.join(" · ");
    }
    return;
  }
  // 终态事件交给 renderResultPanel（任务详情快照）处理。
}

function updateResultStream(text) {
  const panel = $("#studioResultStream");
  if (!panel) return;
  panel.textContent = text;
  panel.scrollTop = panel.scrollHeight;
}

function updateReasoningChip() {
  const hint = $("#studioTaskHint");
  if (!hint) return;
  hint.textContent = studio.reasoningText ? `思考中…（${studio.reasoningText.length} 字）` : hint.textContent;
}

function renderResultPanel(task) {
  studio.lastResult = task;
  const panel = $("#studioResultStream");
  const hint = $("#studioTaskHint");
  if (!panel) return;
  const running = task.status === "running" || task.status === "queued";
  const text = task.result || task.result_partial || "";
  if (text) {
    // 终态任务也要显示 result_partial（取消/中断保留已产出部分），
    // 不能因为「不是 done」就把一个旧任务的正文继续留在面板里。
    updateResultStream(text);
    if (task.status === "done") studio.streamText = text;
  } else if (running) {
    updateResultStream(studio.streamText || "");
  } else if (task.status === "done") {
    updateResultStream("（本任务没有可显示的结果）");
    studio.streamText = "";
  } else {
    updateResultStream("");
    studio.streamText = "";
  }
  if (hint) {
    const map = {
      queued: "排队中…",
      running: "正在生成…",
      done: "生成完成",
      cancelled: "已取消（保留已生成的部分）",
      failed: `生成失败：${task.error || "原因未知"}`,
      interrupted: "已中断（服务重启前）",
    };
    hint.textContent = map[task.status] || "";
  }
  const cancelBtn = $("#studioCancelTask");
  const retryBtn = $("#studioRetryTask");
  const handoffBtn = $("#studioHandoff");
  if (cancelBtn) cancelBtn.hidden = !running;
  if (retryBtn) retryBtn.hidden = !["failed", "cancelled", "interrupted"].includes(task.status);
  if (handoffBtn) handoffBtn.hidden = task.status !== "done" || !task.result;
  const meta = $("#studioResultMeta");
  if (meta) {
    const usage = task.usage || {};
    const tokens = [];
    if (usage.input_tokens) tokens.push(`输入 ${usage.input_tokens}`);
    if (usage.output_tokens) tokens.push(`输出 ${usage.output_tokens}`);
    if (usage.cost) tokens.push(`费用 ${usage.cost}`);
    meta.textContent = tokens.join(" · ");
  }
  if (task.handoff && task.handoff.conversation_id) {
    const chip = $("#studioHandoffChip");
    if (chip) chip.textContent = "已交接";
  }
}

// ============================================================
// 任务历史：最近 3 条 chips + 侧栏「历史产出」完整列表（同一批数据）
// ============================================================

async function refreshTasks({ syncHistory = false } = {}) {
  try {
    const { tasks } = await api("/api/studio/tasks");
    studio.tasks = Array.isArray(tasks) ? tasks : [];
    studio.tasksLoaded = true;
    studio.tasksError = "";
    if (studio.historyState === "error") studio.historyState = "ready";
  } catch (error) {
    studio.tasksError = String(error.message || error);
  }
  renderRecentTasks();
  // 后台轮询/任务完成只更新标题区捷径与缓存，不覆盖用户正在浏览的历史列表；
  // 只有显式刷新（进入工作台、提交、重试）才同步列表。
  if (syncHistory && studio.resultMode === "history") renderHistoryList();
}

function taskTitle(task) {
  const title = String(task.title || "").trim();
  if (title) return title;
  const body = String(task.result || task.result_partial || "").trim();
  if (body) return body.slice(0, 30);
  return "未命名任务";
}

function taskStatusLabel(status) {
  return TASK_STATUS_LABELS[String(status || "")] || "状态未知";
}

function taskOrigin(task) {
  const label = String(task.origin_label || "").trim();
  if (label) return label;
  const module = String(task.module || "").trim();
  // 「全部」是分类导航的汇总项，不是任务的真实来源；module 缺失时不能回退到它。
  const hit = module ? MODULE_TABS.find((m) => m.key === module) : null;
  if (hit) return hit.label;
  return "来源未记录";
}

function formatTaskTime(value) {
  const ms = Number(value);
  if (!Number.isFinite(ms) || ms <= 0) return "时间未记录";
  const date = new Date(ms);
  if (Number.isNaN(date.getTime())) return "时间未记录";
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

// 结果栏标题区：最近任务捷径，固定最多 3 条；历史列表打开时隐藏，避免重复导航。
function renderRecentTasks() {
  const wrap = $("#studioTaskHistory");
  if (!wrap) return;
  if (studio.resultMode === "history") {
    wrap.replaceChildren();
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  const recent = (studio.tasks || []).slice(0, RECENT_TASK_LIMIT);
  wrap.replaceChildren(...recent.map((task) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "studio-task-row";
    button.dataset.studioTask = task.id;
    const label = document.createElement("span");
    label.textContent = taskTitle(task);
    const status = document.createElement("em");
    status.textContent = taskStatusLabel(task.status);
    button.append(label, status);
    return button;
  }));
}

function setResultMode(mode) {
  const next = RESULT_MODES.includes(mode) ? mode : "result";
  const changed = studio.resultMode !== next;
  studio.resultMode = next;
  const history = next === "history";
  const historyList = $("#studioHistoryList");
  const stream = $("#studioResultStream");
  const actions = document.querySelector(".studio-result-actions");
  const title = $("#studioResultTitle");
  const range = $("#studioHistoryRange");
  const meta = $("#studioResultMeta");
  if (historyList) historyList.hidden = !history;
  if (stream) stream.hidden = history;
  if (actions) actions.hidden = history;
  if (meta) meta.hidden = history;
  if (range) {
    range.hidden = !history;
    range.textContent = HISTORY_RANGE_HINT;
  }
  if (title) title.textContent = history ? "历史产出" : "生成结果";
  renderRecentTasks();
  if (history && changed) renderHistoryList();
}

function renderHistoryList() {
  const wrap = $("#studioHistoryList");
  if (!wrap) return;
  if (studio.historyState === "loading") {
    wrap.replaceChildren(historyNotice("正在读取历史产出…"));
    return;
  }
  const tasks = studio.tasks || [];
  const nodes = [];
  if (studio.historyState === "error") {
    nodes.push(historyErrorPanel());
    if (!tasks.length) {
      wrap.replaceChildren(...nodes);
      return;
    }
  }
  if (!tasks.length) {
    nodes.push(emptyListPanel("还没有历史产出", "回到参数档写下你的需求，生成结果会留在这里。"));
    wrap.replaceChildren(...nodes);
    return;
  }
  nodes.push(...tasks.map(historyRow));
  wrap.replaceChildren(...nodes);
}

function historyNotice(text) {
  const p = document.createElement("p");
  p.className = "studio-history-notice";
  p.textContent = text;
  return p;
}

function historyErrorPanel() {
  const panel = document.createElement("div");
  panel.className = "studio-history-error";
  const strong = document.createElement("strong");
  strong.textContent = "历史产出一时读取失败";
  const detail = document.createElement("small");
  detail.textContent = studio.tasks.length
    ? `${studio.tasksError || "请求失败"}。下面显示的是上次读取的结果，可能不是最新。`
    : (studio.tasksError || "请求失败。");
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "control-button";
  retry.dataset.studioHistoryRetry = "1";
  retry.textContent = "重新读取";
  panel.append(strong, document.createElement("br"), detail, document.createElement("br"), retry);
  return panel;
}

function historyRow(task) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "studio-history-row";
  button.dataset.studioTask = task.id;
  const title = document.createElement("span");
  title.className = "studio-history-title";
  title.textContent = taskTitle(task);
  title.title = taskTitle(task);
  const metaLine = document.createElement("span");
  metaLine.className = "studio-history-meta";
  [taskStatusLabel(task.status), formatTaskTime(task.created_at), taskOrigin(task)].forEach((part) => {
    const item = document.createElement("em");
    item.textContent = part;
    metaLine.append(item);
  });
  button.append(title, metaLine);
  return button;
}

async function openHistoryList() {
  openStudioView();
  setResultMode("history");
  setStudioPane("result");
  if (!sidebarDesktop()) closeSidebar();
  const token = ++studio.historyToken;
  // 已有缓存时不先清空列表（避免闪空）；只有首次读取才显示「正在读取」。
  studio.historyState = studio.tasksLoaded ? "ready" : "loading";
  renderHistoryList();
  try {
    const { tasks } = await api("/api/studio/tasks");
    if (token !== studio.historyToken) return;
    studio.tasks = Array.isArray(tasks) ? tasks : [];
    studio.tasksLoaded = true;
    studio.tasksError = "";
    studio.historyState = "ready";
  } catch (error) {
    if (token !== studio.historyToken) return;
    studio.tasksError = String(error.message || error);
    studio.historyState = "error";
  }
  renderRecentTasks();
  renderHistoryList();
}

async function pickStudioTask(taskId) {
  const token = ++studio.taskToken;
  try {
    const { task } = await api(`/api/studio/tasks/${taskId}`);
    if (token !== studio.taskToken) return; // 快速切 A→B：A 的迟到响应不覆盖 B
    studio.activeTaskId = taskId;
    studio.eventCursor = 0;
    studio.streamText = "";
    studio.reasoningText = "";
    setResultMode("result");
    renderResultPanel(task);
    setStudioPane("result");
    const running = task.status === "queued" || task.status === "running";
    if (running) {
      startStudioPolling();
    } else {
      stopStudioPolling();
    }
  } catch (error) {
    if (token !== studio.taskToken) return;
    toast(`任务读取失败：${error.message}`);
  }
}

// ============================================================
// 继续追问（必选 Agent 的交接框）
// ============================================================

async function openStudioAgentDialog() {
  if (!studio.activeTaskId) { toast("先生成一条结果再继续追问"); return; }
  const dialog = $("#studioAgentDialog");
  const list = $("#studioAgentList");
  const handoffNote = $("#studioHandoffNote");
  if (!dialog || !list) return;
  if (handoffNote) handoffNote.value = "";
  studio.agentPicked = "";
  const confirmBtn = $("#studioHandoffConfirm");
  if (confirmBtn) confirmBtn.disabled = true;
  list.replaceChildren();
  try {
    const { agents } = await api("/api/agents");
    (agents || []).forEach((agent) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "studio-agent-row";
      button.dataset.studioAgent = agent.id;
      const name = document.createElement("span");
      name.textContent = agent.name || agent.id;
      const hint = document.createElement("small");
      hint.textContent = agent.system_prompt ? String(agent.system_prompt).slice(0, 60) : "";
      button.append(name, hint);
      list.append(button);
    });
  } catch (error) {
    toast(`Agent 列表读取失败：${error.message}`);
  }
  if (typeof dialog.showModal === "function") dialog.showModal();
}

function pickStudioAgent(agentId, row) {
  studio.agentPicked = agentId;
  const list = $("#studioAgentList");
  if (list) list.querySelectorAll(".studio-agent-row").forEach((el) => el.classList.toggle("active", el === row));
  const confirmBtn = $("#studioHandoffConfirm");
  if (confirmBtn) confirmBtn.disabled = !agentId;
}

async function confirmStudioHandoff() {
  const agentId = studio.agentPicked;
  if (!agentId) { toast("先在列表里选中一个 Agent"); return; }
  const handoffNote = String($("#studioHandoffNote")?.value || "").trim();
  const confirmBtn = $("#studioHandoffConfirm");
  let conversationId = "";
  try {
    if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.textContent = "正在创建会话…"; }
    const { conversation_id } = await api("/api/studio/handoff", {
      method: "POST",
      body: { task_id: studio.activeTaskId, agent_id: agentId, note: handoffNote },
    });
    conversationId = conversation_id;
  } catch (error) {
    toast(`交接失败：${error.message}（生成结果仍保留在工作台）`);
    return;
  } finally {
    if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.textContent = "创建会话并继续"; }
  }
  $("#studioAgentDialog")?.close();
  toast("已创建普通会话，正在打开…");
  // 交接成功回对话：与「对话」按钮走同一条切换路径（恢复侧栏、停止前端轮询）。
  closeStudioView();
  void populateComposerModels();
  openConversation(conversationId);
}

// ============================================================
// 事件接线（组合根 bind-events 调用一次）
// ============================================================

export function bindStudioEvents() {
  // 侧栏模式切换：工作台只进不退，退出统一由「对话」负责。
  // 手机端每次点模式按钮都关闭抽屉，避免挡住刚打开的目标内容（已在工作台时同样关闭）。
  $("#openStudio")?.addEventListener("click", () => {
    openStudioView();
    if (!sidebarDesktop()) closeSidebar();
  });
  // 工作台自己的手机导航入口（对话顶栏在工作台态整体退场，汉堡按钮由本模块提供）。
  $("#studioOpenSidebar")?.addEventListener("click", () => openSidebar());
  // 桌面端侧栏收起后，对话顶栏的 #expandSidebar 已随顶栏退场；用本模块顶栏的同款按钮恢复。
  $("#studioExpandSidebar")?.addEventListener("click", () => setLeftSidebarCollapsed(false));
  $("#openChatMode")?.addEventListener("click", () => {
    closeStudioView();
    if (!sidebarDesktop()) closeSidebar();
  });
  $("#sidebarStudioNav")?.addEventListener("click", (event) => {
    const link = event.target.closest("[data-studio-nav]");
    if (!link) return;
    if (link.dataset.studioNav === "history") void openHistoryList();
  });
  $("#studioCats")?.addEventListener("click", (event) => {
    const cat = event.target.closest("[data-studio-module]");
    if (!cat) return;
    void selectStudioCategory(cat.dataset.studioModule);
  });
  $("#studioPanes")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-studio-pane]");
    if (!btn) return;
    setStudioPane(btn.dataset.studioPane);
  });
  $("#studioSearch")?.addEventListener("input", () => {
    studio.query = String($("#studioSearch")?.value || "");
    void renderStudioList();
  });
  const studioEl = $("#studioView");
  studioEl?.addEventListener("click", (event) => {
    const historyRetry = event.target.closest("[data-studio-history-retry]");
    if (historyRetry) { void openHistoryList(); return; }
    const pick = event.target.closest("[data-studio-pick]");
    if (pick && pick.dataset.studioKind) {
      void selectStudioEntry(pick.dataset.studioKind, pick.dataset.studioPick);
      return;
    }
    const taskPick = event.target.closest("[data-studio-task]");
    if (taskPick) {
      void pickStudioTask(taskPick.dataset.studioTask);
      return;
    }
    if (event.target.closest("#studioGenerate")) void submitGenerate();
    if (event.target.closest("#studioCancelTask")) void cancelStudioTask();
    if (event.target.closest("#studioRetryTask")) void retryStudioTask("");
    if (event.target.closest("#studioCustomSave")) void saveCustomEdit();
    if (event.target.closest("#studioCustomDelete")) void deleteCustomEntry();
    if (event.target.closest("#studioCustomExport")) exportCustomEntry();
    if (event.target.closest("#studioCustomImport")) void pickCustomImportFile();
    if (event.target.closest("#studioHandoff")) void openStudioAgentDialog();
    if (event.target.closest("#studioHandoffConfirm")) void confirmStudioHandoff();
  });
  // 追问框是 document 级 <dialog>（top layer），选择 Agent 的委托必须挂在它自己身上。
  $("#studioAgentList")?.addEventListener("click", (event) => {
    const agentRow = event.target.closest("[data-studio-agent]");
    if (agentRow) pickStudioAgent(agentRow.dataset.studioAgent, agentRow);
  });
  $("#studioProviderSelect")?.addEventListener("change", async () => {
    studio.providerKey = $("#studioProviderSelect")?.value || "";
    studio.modelName = "";
    $("#studioModelSelect") && ($("#studioModelSelect").value = "");
    await syncStudioModel();
  });
  $("#studioModelSelect")?.addEventListener("change", () => {
    studio.modelName = $("#studioModelSelect")?.value || "";
  });
  bindHandoffDialogEvents();
  window.addEventListener("beforeunload", stopStudioPolling);
}

function bindHandoffDialogEvents() {
  const dialog = $("#studioAgentDialog");
  if (!dialog) return;
  const handoffNote = $("#studioHandoffNote");
  handoffNote?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      void confirmStudioHandoff();
    }
  });
  dialog.addEventListener("close", () => {
    // 取消/关闭只回工作台，不创建会话（Agent 选择确认才调 handoff API）。
    studio.agentPicked = "";
  });
}

// 页面重载后的恢复：工作台不持久化选择状态，只恢复"是否开着"由调用方决定。
