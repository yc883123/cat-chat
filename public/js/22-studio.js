// ============================================================
// 22-studio.js —— 工作台（Studio，_studio_plan_v2.md）
// 独立生成域：目录检索（预设 559 条 + 自定义）→ 填变量/自由输入 → 生成（Studio 自有
// 异步任务，/api/studio/*）→ 结果留在工作台 → 「继续追问」经必选 Agent 框交接普通对话。
// 不直接编排会话创建/聊天 Run/事件流，仅调 handoff API（单一跨域口）。
// ============================================================

import { $, api, state, toast } from "./01-core.js";
import { populateComposerModels } from "./07-models-agents.js";
import { openConversation } from "./08-conversations.js";

// ---- 模块清单（目录条目 module -> 显示名；顺序即 tab 顺序） ----
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
  activeTaskId: "",
  eventCursor: 0,
  pollBusy: false,
  pollTimer: null,
  agentPicked: "",
  streamText: "",
  reasoningText: "",
};

// ============================================================
// 视图开关
// ============================================================

export function isStudioOpen() {
  return studio.open;
}

export function openStudioView() {
  studio.open = true;
  document.body.dataset.studio = "on";
  const view = $("#studioView");
  if (view) view.hidden = false;
  $("#openStudio")?.setAttribute("aria-pressed", "true");
  void refreshStudio();
  focusStudioInput();
}

export function closeStudioView() {
  studio.open = false;
  document.body.dataset.studio = "off";
  const view = $("#studioView");
  if (view) view.hidden = true;
  $("#openStudio")?.setAttribute("aria-pressed", "false");
  stopStudioPolling();
}

function focusStudioInput() {
  const input = $("#studioUserRequest");
  if (input && !input.closest('dialog')) setTimeout(() => input.focus(), 60);
}

async function refreshStudio() {
  // 之前留下的进行中任务：重新开视图时恢复订阅（历史选择状态不持久化，只接住活任务）。
  if (studio.lastResult && ["queued", "running"].includes(studio.lastResult.status) && studio.activeTaskId) {
    void pollStudioEvents();
    startStudioPolling();
  }
  if (!studio.catalogLoaded) {
    try {
      const { catalog } = await api("/api/studio/catalog");
      studio.summary = catalog;
      studio.catalogLoaded = true;
      renderStudioTabs();
    } catch (error) {
      toast(`工作台目录读取失败：${error.message}`);
    }
  }
  await Promise.all([renderStudioList(), renderStudioTasks(), syncStudioModel()]);
}

// ============================================================
// 顶栏 / 列表 / 详情
// ============================================================

function renderStudioTabs() {
  const wrap = $("#studioTabs");
  if (!wrap) return;
  const counts = (studio.summary && studio.summary.modules) || {};
  wrap.replaceChildren(...MODULE_TABS.map((tab) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "studio-tab";
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", String(tab.key === studio.module));
    button.dataset.studioModule = tab.key;
    const count = tab.key === "custom" ? (studio.customLoaded ? customCount() : "") : (tab.key ? (counts[tab.key] ?? "") : "");
    button.textContent = count === "" ? tab.label : `${tab.label} ${count}`;
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

async function renderStudioList() {
  const list = $("#studioList");
  if (!list) return;
  list.replaceChildren(buildIndicator());
  if (studio.module === "custom") {
    await loadCustomItems();
    renderStudioTabs();
    const rows = (studio.customItems || []).filter((row) => matchesQuery(row));
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
  studio.kind = kind;
  studio.meta = null;
  studio.body = "";
  studio.varValues = {};
  if (kind === "builtin") {
    studio.itemId = id;
    studio.customId = "";
    await loadCatalogEntry();
  } else if (kind === "custom") {
    studio.customId = id;
    studio.itemId = "";
    await loadCustomEntry();
  }
  renderStudioDetail();
  await renderStudioList();
}

async function loadCatalogEntry() {
  try {
    const { item, body } = await api(`/api/studio/catalog/${encodeURIComponent(studio.itemId)}`);
    studio.meta = item;
    studio.body = body;
  } catch (error) {
    toast(`条目读取失败：${error.message}`);
    studio.itemId = "";
    studio.kind = "free";
  }
}

async function loadCustomEntry() {
  // 自定义列表接口自带 body（custom.list 直读每条 JSON），不需要第二个接口。
  if (!studio.customLoaded) await loadCustomItems();
  const found = (studio.customItems || []).find((r) => r.id === studio.customId);
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
    const { task, unresolved_vars } = await api("/api/studio/generate", { method: "POST", body: payload });
    if (Array.isArray(unresolved_vars) && unresolved_vars.length) {
      toast(`有 ${unresolved_vars.length} 个占位符没法识别，已原样保留：${unresolved_vars.slice(0, 3).join("、")}`);
    }
    studio.activeTaskId = task.id;
    studio.eventCursor = 0;
    studio.streamText = "";
    studio.reasoningText = "";
    renderResultPanel(task);
    startStudioPolling();
    await renderStudioTasks();
  } catch (error) {
    toast(`创建生成任务失败：${error.message}`);
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
    const { task } = await api(`/api/studio/tasks/${target}/retry`, { method: "POST", body: {} });
    studio.activeTaskId = task.id;
    studio.eventCursor = 0;
    studio.streamText = "";
    studio.reasoningText = "";
    renderResultPanel(task);
    startStudioPolling();
    await renderStudioTasks();
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
  if (!studio.activeTaskId || studio.pollBusy) return;
  studio.pollBusy = true;
  try {
    const { status } = await syncTaskEvents(studio.activeTaskId, studio.eventCursor);
    const done = status === "done" || status === "cancelled" || status === "failed" || status === "interrupted";
    if (done) {
      stopStudioPolling();
      const task = await api(`/api/studio/tasks/${studio.activeTaskId}`);
      renderResultPanel(task.task);
      await renderStudioTasks();
    }
  } catch (error) {
    toast(`生成状态读取失败：${error.message}`);
  } finally {
    studio.pollBusy = false;
  }
}

async function syncTaskEvents(taskId, after) {
  const payload = await api(`/api/studio/tasks/${taskId}/events?after=${after}`);
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
  if (text && task.status === "done") {
    updateResultStream(text);
    studio.streamText = text;
  } else if (running && studio.streamText) {
    updateResultStream(studio.streamText);
  } else if (task.status === "done") {
    updateResultStream(task.result || "（本任务没有可显示的结果）");
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
// 任务历史（工作台结果留存的可见出口）
// ============================================================

async function renderStudioTasks() {
  try {
    const { tasks } = await api("/api/studio/tasks");
    studio.tasks = tasks || [];
  } catch (_) { /* 历史读取失败不打断主流程 */ }
  const wrap = $("#studioTaskHistory");
  if (!wrap) return;
  wrap.replaceChildren();
  studio.tasks.slice(0, 12).forEach((task) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "studio-task-row";
    button.dataset.studioTask = task.id;
    const label = document.createElement("span");
    label.textContent = task.title || task.result?.slice(0, 30) || task.result_partial?.slice(0, 30) || "（任务）";
    const status = document.createElement("em");
    status.textContent = task.status;
    button.append(label, status);
    wrap.append(button);
  });
}

async function pickStudioTask(taskId) {
  try {
    const { task } = await api(`/api/studio/tasks/${taskId}`);
    studio.activeTaskId = taskId;
    studio.eventCursor = 0;
    studio.streamText = "";
    studio.reasoningText = "";
    renderResultPanel(task);
    const running = task.task.status === "queued" || task.task.status === "running";
    if (running) {
      startStudioPolling();
      await syncTaskEvents(taskId, 0);
    }
  } catch (error) {
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
  closeStudioView();
  void populateComposerModels();
  openConversation(conversationId);
}

// ============================================================
// 事件接线（组合根 bind-events 调用一次）
// ============================================================

export function bindStudioEvents() {
  $("#openStudio")?.addEventListener("click", () => {
    if (studio.open) closeStudioView();
    else openStudioView();
  });
  $("#studioTabs")?.addEventListener("click", (event) => {
    const tab = event.target.closest("[data-studio-module]");
    if (!tab) return;
    studio.module = tab.dataset.studioModule;
    studio.kind = studio.module === "custom" ? "custom" : "free";
    if (studio.kind === "custom") studio.customId = "";
    renderStudioTabs();
    void renderStudioList();
    if (["custom"].includes(studio.module)) renderStudioDetail();
  });
  $("#studioSearch")?.addEventListener("input", () => {
    studio.query = String($("#studioSearch")?.value || "");
    void renderStudioList();
  });
  const studioEl = $("#studioView");
  studioEl?.addEventListener("click", (event) => {
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
