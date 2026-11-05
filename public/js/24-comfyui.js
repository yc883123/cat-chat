// ============================================================
// 24-comfyui.js —— 造梦机（工作台第三个功能页）
// 只 import 01-core / 03-media / 22-studio；轮询经 onStudioPageChange 暂停/恢复
// （离开页面就停，回来由服务端状态校准，与打标器同一套约定）。
// 与聊天域零耦合：只调 /api/comfyui/*。
// ============================================================

import { $, api, localFileUrl, state, toast } from "./01-core.js";
import { attachmentThumbUrl, isPywebview, mediaKind, saveLocalFile } from "./03-media.js";
import { currentStudioPage, onStudioPageChange, setStudioPage } from "./22-studio.js";
const POLL_MS = 1200;
const REFRESH_MS = 5000;

const comfyui = {
  workflows: [],
  selectedId: "",
  detail: null,
  tasks: [],
  activeTaskId: "",
  eventSeq: 0,
  pollTimer: null,
  refreshTimer: null,
  unsubscribePage: null,
  importing: false,
  submitting: false,
  url: "",
  modelCandidates: null,   // 模型候选缓存 {checkpoints, loras}；null = 还没拉过
  deleting: new Set(),     // 正在「取消并删除」的任务 id，防重复提交
  mediaSelection: new Map(), // 媒体槽已选文件 {slotKey: {path, name, thumb_path}}；切工作流清空
  drafts: new Map(),       // 主表单用户已填的值 {工作流id|node_id|field_name: value}；重绘与刷新都保留，点「默认」才清
  uploadingSlot: "",       // 正在上传的媒体槽 key（防并发点选；批量拖拽时是各 key 拼接，仍为真值即锁住）
  mediaGroupsCollapsed: {}, // 媒体分组卡折叠状态 {kind: true}；只存内存，不落盘、不进 visible_params
  storyboardQueue: [],    // 拆书匠「分镜生图」发来的提示词；只存内存，刷新即清
};

// ============================================================
// 入口 / 生命周期
// ============================================================

// Composition root owns the destination; batch generations stay in this module.
let importReturn = null;
export function openComfyuiWorkflowImport({ onSave = null, onCancel = null } = {}) {
  importReturn = { onSave, onCancel };
  openImportDialog();
}
let workflowSaveBusy = false;

// ---- 另存核对 ----
// 另存只固化「枚举 / 严格布尔 / 有权威上下界的数值」；提示词、媒体、自由文本和
// 路径由任务输入或受管上传处理。核对清单必须列出**全部已扫描参数**：隐藏槽位
// 没有控件，不能算「已改变」，但要让人看见它们没有进入副本，而不是悄悄消失。
const SAVE_AS_PROMPT_ROLES = new Set(["positive_prompt", "negative_prompt"]);

function saveAsValueFor(slot) {
  // 草稿只在用户真的动过控件时才有记录；没记录 = 沿用工作流原值。
  const draft = savedDraft(slotKey(slot));
  if (draft === undefined) return undefined;
  const input = document.querySelector(`[data-comfyui-slot="${CSS.escape(slotKey(slot))}"]`);
  const text = String(input ? (input.value ?? "") : draft).trim();
  return slot.field_type === "bool" ? text === "true" : text;
}

function saveAsSameValue(left, right) {
  if (left === undefined) return true;
  if (left === right) return true;
  return typeof left === typeof right ? false : String(left) === String(right);
}

// 返回值是「不能另存的原因」；空串表示可以写进副本。
// 服务端仍会用实时 object_info 复核枚举成员与数值上下界，这里只挡明显不支持的改动。
function saveAsProblem(slot, value) {
  if (slot.control === "media" || MEDIA_FIELD_TYPES.has(slot.field_type)) {
    return "受管媒体入口：文件不进副本，另存后仍按任务选择的素材执行";
  }
  if (SAVE_AS_PROMPT_ROLES.has(slot.role)) {
    return "提示词按任务输入处理，不写进副本；另存后请在任务总管或主表单里填";
  }
  if (slot.field_type === "bool") return typeof value === "boolean" ? "" : "布尔参数只接受「开 / 关」";
  if (slot.field_type === "number" || slot.field_type === "seed") {
    const text = String(value).trim();
    const number = Number(text);
    if (!text || !Number.isFinite(number)) return "数值参数要填有限数字";
    if (slot.field_type !== "seed") return "";
    return Number.isInteger(number) && number >= 0
      ? "" : "种子要填 0 或更大的整数；想每次随机就留空";
  }
  if (slot.control === "combo") {
    const options = slot.candidates || [];
    return options.length && !options.includes(value)
      ? `「${value}」不在这个参数的候选里` : "";
  }
  return "自由文本 / 路径 / 代码不写进副本";
}

// 一行 = 一个已扫描槽位。changed 为假的隐藏槽位也要出现，但不会被提交。
function saveAsRows() {
  const visible = visibleKeySet();
  return (comfyui.detail?.params || []).map((slot) => {
    const key = slotKey(slot);
    const original = comfyui.detail?.prompt?.[slot.node_id]?.inputs?.[slot.field_name];
    const current = saveAsValueFor(slot);
    const changed = !saveAsSameValue(current, original);
    return {
      slot, key, original, current, changed,
      visible: visible.has(key),
      problem: changed ? saveAsProblem(slot, current) : "",
    };
  });
}

function saveAsText(value) {
  if (value === undefined) return "（没有）";
  if (typeof value === "boolean") return value ? "开" : "关";
  if (typeof value === "string" && !value) return "（空）";
  return String(value);
}

function saveAsRowMarkup(row, { changed }) {
  const { slot } = row;
  const name = slot.label || `${slot.node_id} · ${slot.field_name}`;
  const tags = [];
  if (!row.visible) tags.push('<span class="comfyui-saveas-tag">未在主表单显示</span>');
  if (changed && row.problem) tags.push('<span class="comfyui-saveas-tag is-blocked">不能另存</span>');
  const action = !row.visible
    ? `<button class="control-button tiny" type="button" data-comfyui-add-slot="${CSS.escape(row.key)}">加到主表单</button>`
    : "";
  const diff = changed
    ? `<span class="comfyui-saveas-diff">${escapeText(saveAsText(row.original))} → <b>${escapeText(saveAsText(row.current))}</b></span>`
    : `<span class="comfyui-saveas-diff is-same">${escapeText(saveAsText(row.original))}（未改）</span>`;
  const reason = row.problem ? `<small class="comfyui-saveas-reason">${escapeText(row.problem)}</small>` : "";
  return `<div class="comfyui-saveas-row${row.problem ? " is-blocked" : ""}"><span class="comfyui-saveas-name">${escapeText(name)}${tags.join("")}</span>${diff}${reason}${action}</div>`;
}

function renderSaveAsReview() {
  const list = $("#comfyuiSaveAsList");
  if (!list) return;
  const rows = saveAsRows();
  const changed = rows.filter((row) => row.changed);
  const blocked = changed.filter((row) => row.problem);
  const saveable = changed.filter((row) => !row.problem);
  const untouched = rows.filter((row) => !row.changed);
  const summary = $("#comfyuiSaveAsSummary");
  if (summary) {
    summary.textContent = changed.length
      ? `共 ${rows.length} 个参数 · ${changed.length} 项已改动（${saveable.length} 项写进副本${blocked.length ? `，${blocked.length} 项不能另存` : ""}）· ${untouched.length} 项未改`
      : `共 ${rows.length} 个参数，当前全部是工作流原值；直接另存会得到一份原样副本。`;
  }
  const parts = [];
  if (changed.length) {
    parts.push(`<div class="comfyui-saveas-group"><p class="comfyui-saveas-group-title">本次要写入副本的改动</p>${changed.map((row) => saveAsRowMarkup(row, { changed: true })).join("")}</div>`);
  }
  if (untouched.length) {
    parts.push(`<details class="comfyui-saveas-group"><summary>未改动的 ${untouched.length} 个参数（不会写入副本）</summary>${untouched.map((row) => saveAsRowMarkup(row, { changed: false })).join("")}</details>`);
  }
  list.innerHTML = parts.join("") || '<p class="comfyui-empty">这份工作流没有扫描到可调参数。</p>';
  const discardRow = $("#comfyuiSaveAsDiscardRow");
  if (discardRow) {
    discardRow.hidden = !blocked.length;
    if (!blocked.length) { const box = $("#comfyuiSaveAsDiscard"); if (box) box.checked = false; }
  }
  const confirm = $("#comfyuiSaveAsConfirm");
  if (confirm) confirm.disabled = Boolean(blocked.length) && !$("#comfyuiSaveAsDiscard")?.checked;
}

// 隐藏槽位没法编辑，也就无从改动；把它加进主表单后才能核对和另存。
async function addSlotToMainForm(key) {
  if (!comfyui.detail) return;
  const visible = visibleKeySet();
  visible.add(key);
  const order = orderedSlots(comfyui.detail.params || [])
    .map((slot) => ({ node_id: slot.node_id, field_name: slot.field_name }));
  try {
    const payload = await api(
      `/api/comfyui/workflows/${encodeURIComponent(comfyui.detail.id)}/params`,
      { method: "POST", body: { visible: [...visible].map(parseSlotKey), order } });
    comfyui.detail = {
      ...comfyui.detail,
      visible_params: payload.visible_params || [...visible].map(parseSlotKey),
      param_order: payload.param_order || order,
    };
    markVisibleSetKnowsMedia();
    renderParamForm();
    renderSaveAsReview();
  } catch (error) {
    const box = $("#comfyuiSaveAsError");
    if (box) box.textContent = `加入主表单失败：${error.message}`;
  }
}

function parseSlotKey(key) {
  const [nodeId, ...rest] = String(key).split("|");
  return { node_id: nodeId, field_name: rest.join("|") };
}

function escapeText(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

function openSaveAsDialog() {
  if (!comfyui.detail || workflowSaveBusy) return;
  const dialog = $("#comfyuiSaveAsDialog");
  if (!dialog) return;
  const nameInput = $("#comfyuiSaveAsName");
  if (nameInput) nameInput.value = `${comfyui.detail.name || "工作流"} · 副本`;
  const box = $("#comfyuiSaveAsDiscard");
  if (box) box.checked = false;
  const error = $("#comfyuiSaveAsError");
  if (error) error.textContent = "";
  renderSaveAsReview();
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeSaveAsDialog() {
  const dialog = $("#comfyuiSaveAsDialog");
  if (!dialog) return;
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

async function confirmSaveAs() {
  if (!comfyui.detail || workflowSaveBusy) return;
  const errorBox = $("#comfyuiSaveAsError");
  if (errorBox) errorBox.textContent = "";
  const name = String($("#comfyuiSaveAsName")?.value || "").trim();
  if (!name) { if (errorBox) errorBox.textContent = "请填写副本名称"; return; }
  const rows = saveAsRows();
  const changed = rows.filter((row) => row.changed);
  const blocked = changed.filter((row) => row.problem);
  if (blocked.length && !$("#comfyuiSaveAsDiscard")?.checked) {
    if (errorBox) errorBox.textContent = "还有不能另存的改动：请先还原它们，或勾选确认放弃。";
    return;
  }
  const source = comfyui.detail;
  // 只提交核对清单里「已改动且可以另存」的项；被放弃的改动在这里显式排除。
  const overrides = changed.filter((row) => !row.problem).map((row) => ({
    node_id: row.slot.node_id,
    field_name: row.slot.field_name,
    value: row.slot.field_type === "bool" ? row.current === true
      : ["number", "seed"].includes(row.slot.field_type) ? Number(row.current)
      : String(row.current),
  }));
  workflowSaveBusy = true;
  const confirm = $("#comfyuiSaveAsConfirm");
  if (confirm) confirm.disabled = true;
  try {
    const result = await api(`/api/comfyui/workflows/${encodeURIComponent(source.id)}/save-as`, { method: "POST", body: { name, workflow_fingerprint: source.workflow_fingerprint || source.fingerprint, overrides } });
    if (!result.workflow?.id) throw new Error("保存未返回工作流编号，请刷新列表后核对。");
    await refreshWorkflows();
    // Preserve media selections and every drafted value (including hidden slots that
    // carry a draft from when they were visible) when selecting the saved copy.
    const media = new Map(comfyui.mediaSelection);
    await selectWorkflow(result.workflow.id);
    if (comfyui.detail?.id !== result.workflow.id) throw new Error("工作流已保存但未切换成功，请在列表中选择新副本。");
    comfyui.mediaSelection = media;
    for (const row of rows) {
      if (row.current !== undefined) rememberDraft(row.key, typeof row.current === "boolean" ? String(row.current) : row.current);
    }
    renderParamForm();
    closeSaveAsDialog();
    const skipped = blocked.length ? `；${blocked.length} 项不能另存的改动没有写入` : "";
    toast(`已另存副本（写入 ${overrides.length} 项改动${skipped}），可继续交给任务总管；没有开始执行。`);
  } catch (error) {
    if (errorBox) errorBox.textContent = `另存失败：${error.message}`;
  } finally {
    workflowSaveBusy = false;
    if (confirm) confirm.disabled = false;
  }
}

export function bindComfyuiEvents() {
  $("#comfyuiSaveCurrentAs")?.addEventListener("click", openSaveAsDialog);
  $("#comfyuiSaveAsCancel")?.addEventListener("click", closeSaveAsDialog);
  $("#comfyuiSaveAsConfirm")?.addEventListener("click", () => void confirmSaveAs());
  $("#comfyuiSaveAsDiscard")?.addEventListener("change", () => {
    const confirm = $("#comfyuiSaveAsConfirm");
    if (confirm) confirm.disabled = !$("#comfyuiSaveAsDiscard")?.checked;
    const error = $("#comfyuiSaveAsError");
    if (error) error.textContent = "";
  });
  $("#comfyuiSaveAsName")?.addEventListener("input", () => {
    const error = $("#comfyuiSaveAsError");
    if (error) error.textContent = "";
  });
  $("#comfyuiSaveAsList")?.addEventListener("click", (event) => {
    const key = event.target.closest?.("[data-comfyui-add-slot]")?.dataset.comfyuiAddSlot;
    if (key) void addSlotToMainForm(key);
  });
  $("#comfyuiImportDialog")?.addEventListener("cancel", () => { const callback = importReturn?.onCancel; importReturn = null; callback?.(); });
  $("#comfyuiOpenBatch")?.addEventListener("click", () => setComfyuiPane("batch"));
  for (const type of ["input", "change"]) $("#comfyuiParamForm")?.addEventListener(type, () => updateDraftHint());
  if (comfyui.unsubscribePage) comfyui.unsubscribePage();
  comfyui.unsubscribePage = onStudioPageChange((page) => {
    if (page === "comfyui") {
      void enterPage();
    } else {
      stopPolling();
    }
  });

  $("#comfyuiTestConn")?.addEventListener("click", () => void refreshHealth());
  $("#comfyuiEditUrl")?.addEventListener("click", openUrlDialog);
  $("#comfyuiUrlCancel")?.addEventListener("click", closeUrlDialog);
  $("#comfyuiUrlSave")?.addEventListener("click", () => void saveUrl());
  $("#comfyuiRefreshWorkflows")?.addEventListener("click", () => void refreshWorkflows());
  $("#comfyuiImportBtn")?.addEventListener("click", openImportDialog);
  $("#comfyuiImportCancel")?.addEventListener("click", () => { const callback = importReturn?.onCancel; importReturn = null; closeImportDialog(); callback?.(); });
  $("#comfyuiImportConfirm")?.addEventListener("click", () => void confirmImport());
  $("#comfyuiImportFile")?.addEventListener("change", onImportFile);
  $("#comfyuiSubmit")?.addEventListener("click", () => void submitTask());
  $("#comfyuiClearTasks")?.addEventListener("click", () => void clearFinishedTasks());
  $("#comfyuiFreeModels")?.addEventListener("click", () => void freeModels());
  $("#comfyuiOpenOutputs")?.addEventListener("click", () => void openOutputsFolder());
  $("#comfyuiParamsSave")?.addEventListener("click", () => void saveParamsConfig());
  $("#comfyuiParamsCancel")?.addEventListener("click", closeParamsDialog);
  // 参数清单搜索：边输边滤（行是每次打开重建的，过滤只藏不删）。
  $("#comfyuiParamsSearch")?.addEventListener("input", filterParamsPicker);
  $("#comfyuiParamsPicker")?.addEventListener("change", () => {
    const error = $("#comfyuiParamsError");
    if (error) error.textContent = "";
  });
  // 上下移按钮走 click 委托（行是每次打开重建的）；拖拽在 bindParamsPickerDrag 里。
  $("#comfyuiParamsPicker")?.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-move]");
    if (!button) return;
    event.preventDefault();
    movePickerRow(button.closest(".comfyui-params-item"), button.dataset.move);
  });
  bindParamsPickerDrag();
  $("#comfyuiWorkflowList")?.addEventListener("click", onWorkflowListClick);
  $("#comfyuiParamForm")?.addEventListener("click", (event) => {
    if (event.target.closest("[data-comfyui-edit-params]")) openParamsDialog();
    else if (event.target.closest("[data-comfyui-reset-params]")) resetParamValues();
  });
  // 用户边填边记草稿：重绘（切页回来、传完媒体、保存显示集）时按草稿复原。
  for (const type of ["input", "change"]) {
    $("#comfyuiParamForm")?.addEventListener(type, (event) => {
      const input = event.target.closest?.("[data-comfyui-slot]");
      if (!input) return;
      rememberDraft(String(input.dataset.comfyuiSlot || ""), input.value);
    });
  }
  $("#comfyuiTaskList")?.addEventListener("click", onTaskListClick);
  $("#comfyuiOutputGrid")?.addEventListener("click", onOutputGridClick);
  $("#comfyuiPanes")?.addEventListener("click", onPaneClick);
  // 批量生成档：填单与跑批都渲染进 #comfyuiBatchBody，全部走容器级委托。
  const batchBody = $("#comfyuiBatchBody");
  batchBody?.addEventListener("click", onBatchClick);
  batchBody?.addEventListener("change", onBatchChange);
  batchBody?.addEventListener("input", onBatchInput);
  batchBody?.addEventListener("dragover", onBatchDragOver);
  batchBody?.addEventListener("dragleave", onBatchDragLeave);
  batchBody?.addEventListener("drop", onBatchDrop);
  // 分镜队列：拆书匠跨页交接 + 逐条填入正向提示词。
  document.addEventListener("naiba:storyboard-handoff", onStoryboardHandoff);
  $("#comfyuiStoryboardClear")?.addEventListener("click", () => {
    comfyui.storyboardQueue = [];
    renderStoryboardQueue();
  });
  $("#comfyuiStoryboardList")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-comfyui-storyboard-fill]");
    if (!button) return;
    fillStoryboardPrompt(Number(button.dataset.comfyuiStoryboardFill));
  });
}

async function enterPage() {
  void refreshHealth();
  void refreshWorkflows();
  void refreshTasks();
  void loadModelCandidates();
  watchComfyuiWidth();
  renderParamForm();
  renderStoryboardQueue();
  scheduleBackgroundRefresh();
}

function onPaneClick(event) {
  const btn = event.target.closest("[data-comfyui-pane]");
  if (!btn) return;
  setComfyuiPane(btn.dataset.comfyuiPane);
  if (btn.dataset.comfyuiPane === "batch") enterBatchPane();
  else stopBatchPolling();
}

// 三栏放不下时改回三档（工作流 / 提交 / 结果），中栏才拿得到整页宽度。
// 判档口径是**这一页自己的宽度**而不是视口：侧栏拖宽、文件面板打开都会让页面变窄
// 而视口不变（与顶栏 watchTopbarWidth 同一教训）。阈值来自实测：页面 1000px 以下
// 中栏就只剩 250px 上下——状态栏虽然换行兜住不再溢出，参数表单也已经没法用。
const NARROW_PAGE_WIDTH = 1000;
let comfyuiWidthObserver = null;

function watchComfyuiWidth() {
  const root = $("#studioPageComfyui");
  if (!root) return;
  const apply = (width) => {
    const narrow = width > 0 && width < NARROW_PAGE_WIDTH;
    if (narrow) root.dataset.comfyuiNarrow = "1";
    else delete root.dataset.comfyuiNarrow;
  };
  apply(root.getBoundingClientRect().width);
  if (typeof ResizeObserver !== "function") {
    window.addEventListener("resize", () => apply(root.getBoundingClientRect().width));
    return;
  }
  comfyuiWidthObserver?.disconnect();
  comfyuiWidthObserver = new ResizeObserver((entries) => {
    const box = entries[entries.length - 1]?.contentRect;
    apply(box ? Math.round(box.width) : root.getBoundingClientRect().width);
  });
  comfyuiWidthObserver.observe(root);
}

function narrowPanes() {
  return $("#studioPageComfyui")?.dataset.comfyuiNarrow === "1";
}

function setComfyuiPane(pane) {
  const root = $("#studioPageComfyui");
  if (!root) return;
  root.dataset.comfyuiPane = pane;
  document.querySelectorAll("#comfyuiPanes [data-comfyui-pane]").forEach((node) => {
    const active = node.dataset.comfyuiPane === pane;
    node.classList.toggle("active", active);
    node.setAttribute("aria-pressed", String(active));
  });
}

function scheduleBackgroundRefresh() {
  stopBackgroundRefresh();
  comfyui.refreshTimer = setInterval(() => {
    if (currentStudioPage() !== "comfyui") return;
    // 轻量保活：任务列表轮询走 pollActiveTask；这里只补工作流/连接的偶发变化。
    void refreshTasks(true);
  }, REFRESH_MS);
}

function stopBackgroundRefresh() {
  if (comfyui.refreshTimer) {
    clearInterval(comfyui.refreshTimer);
    comfyui.refreshTimer = null;
  }
}

// ============================================================
// 连接状态
// ============================================================

async function refreshHealth() {
  const dot = $("#comfyuiStatusDot");
  const text = $("#comfyuiStatusText");
  try {
    const payload = await api("/api/comfyui/health");
    if (payload.url) comfyui.url = String(payload.url);
    if (payload.ok) {
      dot?.classList.add("ok");
      dot?.classList.remove("error");
      const device = (payload.devices || []).map((item) => item.name).filter(Boolean)[0] || "";
      const version = payload.version ? ` · ${payload.version}` : "";
      if (text) text.textContent = `已连接 ${payload.url}${version}${device ? ` · ${device}` : ""}`;
    } else {
      markUnreachable(payload.error || "ComfyUI 未连接");
    }
  } catch (error) {
    markUnreachable(`ComfyUI 未连接：${error.message}`);
  }
}

// ---- 改地址 ----

function openUrlDialog() {
  const dialog = $("#comfyuiUrlDialog");
  if (!dialog) return;
  $("#comfyuiUrlInput").value = comfyui.url || "";
  $("#comfyuiUrlError").textContent = "";
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeUrlDialog() {
  const dialog = $("#comfyuiUrlDialog");
  if (!dialog) return;
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

async function saveUrl() {
  const raw = String($("#comfyuiUrlInput")?.value || "").trim();
  const error = $("#comfyuiUrlError");
  if (!raw) {
    if (error) error.textContent = "请填写 ComfyUI 地址，例如 http://127.0.0.1:8188。";
    return;
  }
  const button = $("#comfyuiUrlSave");
  if (button) button.disabled = true;
  try {
    const payload = await api("/api/comfyui/url", { method: "POST", body: { url: raw } });
    comfyui.url = String(payload.url || raw);
    closeUrlDialog();
    await refreshHealth();
    if (payload.reachable) toast(`地址已保存并连通：${payload.url}`);
    else toast(`地址已保存，但现在连不上 ComfyUI：${payload.error || "请确认它已启动"}`);
  } catch (caught) {
    if (error) error.textContent = String(caught.message || "保存失败");
  } finally {
    if (button) button.disabled = false;
  }
}

function markUnreachable(message) {
  const dot = $("#comfyuiStatusDot");
  const text = $("#comfyuiStatusText");
  dot?.classList.add("error");
  dot?.classList.remove("ok");
  if (text) text.textContent = message;
}

// ============================================================
// 工作流库
// ============================================================

async function refreshWorkflows() {
  try {
    const payload = await api("/api/comfyui/workflows");
    comfyui.workflows = payload.workflows || [];
    if (comfyui.selectedId && !comfyui.workflows.some((row) => row.id === comfyui.selectedId)) {
      comfyui.selectedId = "";
      comfyui.detail = null;
      renderParamForm();
    }
    renderWorkflowList();
  } catch (error) {
    toast(`读取工作流列表失败：${error.message}`);
  }
  // 「刷新」是用户在 ComfyUI 侧动过之后的显式动作：模型目录清单一并重拉，
  // 参数栏的模型/LoRA 下拉下一次展开就是新目录（下拉是展开时现读缓存）。
  void loadModelCandidates(true);
}

function renderWorkflowList() {
  const list = $("#comfyuiWorkflowList");
  if (!list) return;
  list.textContent = "";
  if (!comfyui.workflows.length) {
    const empty = document.createElement("p");
    empty.className = "comfyui-empty";
    empty.textContent = "还没有工作流。点「导入」把你在 ComfyUI 里跑通的工作流（API 格式 JSON）加进来。";
    list.append(empty);
    return;
  }
  for (const row of comfyui.workflows) {
    const card = document.createElement("div");
    card.className = "comfyui-workflow-card" + (row.id === comfyui.selectedId ? " active" : "");
    card.dataset.workflowId = row.id;
    const title = document.createElement("button");
    title.type = "button";
    title.className = "comfyui-workflow-title";
    title.textContent = row.name || "未命名工作流";
    const meta = document.createElement("span");
    meta.className = "comfyui-workflow-meta";
    meta.textContent = `${row.node_count} 个节点 · ${row.param_count} 个可调参数`;
    const del = document.createElement("button");
    del.type = "button";
    del.className = "comfyui-workflow-delete";
    del.dataset.comfyuiDeleteWorkflow = row.id;
    del.title = "删除这份工作流";
    del.textContent = "删除";
    card.append(title, meta, del);
    list.append(card);
  }
}

async function onWorkflowListClick(event) {
  const del = event.target.closest("[data-comfyui-delete-workflow]");
  if (del) {
    const id = del.dataset.comfyuiDeleteWorkflow;
    const row = comfyui.workflows.find((item) => item.id === id);
    if (!window.confirm(`删除工作流「${row?.name || id}」？已生成的历史任务不受影响。`)) return;
    try {
      await api(`/api/comfyui/workflows/${encodeURIComponent(id)}`, { method: "DELETE" });
      if (comfyui.selectedId === id) {
        comfyui.selectedId = "";
        comfyui.detail = null;
        renderParamForm();
      }
      await refreshWorkflows();
    } catch (error) {
      toast(`删除失败：${error.message}`);
    }
    return;
  }
  const title = event.target.closest(".comfyui-workflow-title");
  if (title) {
    const id = title.closest("[data-workflow-id]")?.dataset.workflowId || "";
    await selectWorkflow(id);
    if (narrowPanes()) setComfyuiPane("run");
  }
}

async function selectWorkflow(id) {
  comfyui.selectedId = id;
  comfyui.mediaSelection.clear();
  renderWorkflowList();
  try {
    const payload = await api(`/api/comfyui/workflows/${encodeURIComponent(id)}`);
    comfyui.detail = payload.workflow || null;
  } catch (error) {
    comfyui.detail = null;
    toast(`读取工作流详情失败：${error.message}`);
  }
  renderParamForm();
}

// 槽位渲染顺序：正向提示词 → 负向提示词 → 模型 → 媒体输入 → 其余（保持导入扫描顺序）。
const ROLE_ORDER = {
  positive_prompt: 0, negative_prompt: 1, checkpoint: 2, lora: 3,
  unet: 3, clip: 3, vae: 3, controlnet: 3, image: 4,
};
const ROLE_LABELS = {
  positive_prompt: "正向提示词",
  negative_prompt: "负向提示词",
  sampler: "采样器",
  scheduler: "调度器",
  checkpoint: "模型（Checkpoint）",
  lora: "LoRA 模型",
  unet: "模型（UNET）",
  clip: "模型（CLIP）",
  vae: "模型（VAE）",
  controlnet: "模型（ControlNet）",
  resolution: "分辨率",
};

// 媒体输入槽位（后端显式映射的加载节点）：field_type 即媒体类型。
const MEDIA_FIELD_TYPES = new Set(["image", "audio", "video"]);
const MEDIA_ACCEPT = {
  image: ".png,.jpg,.jpeg,.webp,.gif",
  audio: ".wav,.mp3,.m4a,.ogg,.flac",
  video: ".mp4,.webm,.mov,.m4v,.ogv",
};

function slotRank(slot) {
  if (ROLE_ORDER[slot.role] !== undefined) return ROLE_ORDER[slot.role];
  // 媒体输入排在无角色参数之前（计划 §4：媒体参数放在用户容易找到的位置）。
  if (MEDIA_FIELD_TYPES.has(slot.field_type)) return 4;
  return 5;
}

function orderedSlots(slots) {
  // 用户拖过顺序的工作流按保存的顺序排；没拖过的仍按角色优先级 + 导入顺序。
  // 保存过顺序之后新扫出来的槽（换了工作流版本）排在已排序的后面，不会被藏掉。
  const saved = savedOrderIndex();
  return slots
    .map((slot, index) => ({ slot, index }))
    .sort((a, b) => {
      if (saved.size) {
        const rankA = saved.get(slotKey(a.slot));
        const rankB = saved.get(slotKey(b.slot));
        if (rankA !== undefined || rankB !== undefined) {
          if (rankA === undefined) return 1;
          if (rankB === undefined) return -1;
          if (rankA !== rankB) return rankA - rankB;
          return a.index - b.index;
        }
      }
      const rankA = slotRank(a.slot);
      const rankB = slotRank(b.slot);
      return rankA !== rankB ? rankA - rankB : a.index - b.index;
    })
    .map((item) => item.slot);
}

function slotKey(slot) {
  return `${slot.node_id}|${slot.field_name}`;
}

// 「编辑参数」里拖出来的先后顺序（按工作流保存）：参数键 → 序号。
function savedOrderIndex() {
  const rows = comfyui.detail?.param_order;
  const map = new Map();
  if (!Array.isArray(rows)) return map;
  for (const item of rows) {
    if (!item) continue;
    map.set(`${item.node_id}|${item.field_name}`, map.size);
  }
  return map;
}

// 主表单默认显示的常用参数：提示词、种子、步数、采样器、调度器、模型、LoRA、分辨率。
// 其余已识别的参数留在数据里，用户可从「编辑参数」加进主表单。
const DEFAULT_VISIBLE_ROLES = new Set([
  "positive_prompt", "negative_prompt", "sampler", "scheduler", "checkpoint", "lora",
  "resolution",
]);

function defaultVisibleKeys(slots) {
  const set = new Set();
  for (const slot of slots) {
    if (DEFAULT_VISIBLE_ROLES.has(slot.role) || slot.field_type === "seed"
        || MEDIA_FIELD_TYPES.has(slot.field_type) || slot.field_name === "steps") {
      set.add(slotKey(slot));
    }
  }
  return set;
}

// 主表单可见参数：按工作流保存过就用保存的；没保存过用常用参数默认集。
// 媒体槽合并只对「媒体支持之前保存的旧显示集」生效：saveParamsConfig 保存成功后
// 会打 v2 标记（localStorage，按工作流 id），带标记的显示集按用户实际勾选执行，
// 「编辑参数」里取消勾选的媒体槽不再被强制合并回来（2026-10-26 计划 §4.5）。
const VISIBLE_MEDIA_V2_PREFIX = "comfyui.visibleMediaV2:";

function visibleSetKnowsMedia() {
  const id = String(comfyui.detail?.id || "");
  try {
    return localStorage.getItem(VISIBLE_MEDIA_V2_PREFIX + id) === "1";
  } catch (_) {
    return false;
  }
}

function markVisibleSetKnowsMedia() {
  const id = String(comfyui.detail?.id || "");
  try {
    localStorage.setItem(VISIBLE_MEDIA_V2_PREFIX + id, "1");
  } catch (_) { /* 隐私模式存不进：退回旧合并行为，不影响功能 */ }
}

function visibleKeySet() {
  const slots = comfyui.detail?.params || [];
  const stored = comfyui.detail?.visible_params;
  if (Array.isArray(stored)) {
    const set = new Set(stored.map((item) => `${item.node_id}|${item.field_name}`));
    if (!visibleSetKnowsMedia()) {
      // 媒体输入是后加能力：旧工作流保存过的显示集里不会有它们，合并进来保证
      // 「图片/音频/视频输入」始终可见。
      for (const slot of slots) {
        if (MEDIA_FIELD_TYPES.has(slot.field_type)) set.add(slotKey(slot));
      }
    }
    return set;
  }
  return defaultVisibleKeys(slots);
}

function renderParamForm() {
  const form = $("#comfyuiParamForm");
  if (!form) return;
  form.textContent = "";
  const submit = $("#comfyuiSubmit");
  if (submit) submit.disabled = !comfyui.detail;
  if (!comfyui.detail) {
    const empty = document.createElement("p");
    empty.className = "comfyui-empty";
    empty.textContent = "先选择一份工作流，这里会列出它可调的参数。";
    form.append(empty);
    return;
  }
  const head = document.createElement("div");
  head.className = "comfyui-param-heading-row";
  const heading = document.createElement("p");
  heading.className = "comfyui-param-heading";
  heading.textContent = comfyui.detail.name || "未命名工作流";
  const actions = document.createElement("span");
  actions.className = "comfyui-param-heading-actions";
  // 「默认」只把已填的内容还原成工作流里原本的值；「编辑」保存的是显示哪些参数、
  // 按什么顺序排，两者互不覆盖（用户口径 2026-10-29）。
  const reset = document.createElement("button");
  reset.type = "button";
  reset.className = "control-button tiny";
  reset.dataset.comfyuiResetParams = "1";
  reset.textContent = "默认";
  reset.title = "把填过的参数还原成工作流里原本的值；「编辑」保存的显示项与顺序不受影响";
  const edit = document.createElement("button");
  edit.type = "button";
  edit.className = "control-button tiny";
  edit.dataset.comfyuiEditParams = "1";
  edit.textContent = "编辑";
  edit.title = "选择要在主表单显示的参数，并调整它们的先后顺序";
  actions.append(reset, edit);
  head.append(heading, actions);
  form.append(head);
  const visible = visibleKeySet();
  const slots = orderedSlots(comfyui.detail.params || []).filter(
    (slot) => visible.has(slotKey(slot)));
  if (!slots.length) {
    const note = document.createElement("p");
    note.className = "comfyui-empty";
    note.textContent = "这份工作流没有可调的参数，直接点「开始生成」即可。";
    form.append(note);
  }
  // 媒体槽不逐槽渲染，按 field_type 归堆成「图片 / 音频 / 视频」分组卡（§4.1）：
  // orderedSlots() 里全部媒体槽同属 rank 4，卡片插在第一个媒体槽的位置一次性放好；
  // 非媒体槽照旧逐槽渲染，相对顺序与现在完全一致。归堆只认 field_type，
  // 与节点号、节点名、端口名解耦——工作流节点怎么命名都不影响分组。
  // 同一角色出现多个槽位（如两个 LoRA 加载器）时，完整标签本身带节点号与节点名，
  // 不再额外拼一次节点信息。
  const mediaGroups = new Map();
  const rowItems = [];
  let groupsPlaced = false;
  for (const slot of slots) {
    const kind = mediaKindOfSlot(slot);
    if (kind) {
      if (!groupsPlaced) {
        rowItems.push({ groups: true });
        groupsPlaced = true;
      }
      if (!mediaGroups.has(kind)) mediaGroups.set(kind, []);
      mediaGroups.get(kind).push(slot);
      continue;
    }
    rowItems.push({ slot });
  }
  for (const item of rowItems) {
    if (item.slot) {
      form.append(renderSlot(item.slot));
    } else {
      // 没有该类型节点就不生成该类型的卡（§3.1）。
      for (const kind of MEDIA_GROUP_ORDER) {
        const groupSlots = mediaGroups.get(kind) || [];
        if (groupSlots.length) form.append(renderMediaGroup(kind, groupSlots));
      }
    }
  }
  updateDraftHint();
}

// 未保存调整的台面提示：只数「主表单可见控件当前值 ≠ 工作流保存值」的项，
// 不打断调参、不弹窗；交接按工作流已保存快照出片，调整要么另存要么进总管批次填。
function updateDraftHint() {
  const hint = $("#comfyuiDraftHint");
  if (!hint) return;
  const graph = comfyui.detail?.prompt || {};
  let changed = 0;
  for (const slot of comfyui.detail?.params || []) {
    if ((slot.control || "") === "media") continue;
    const input = document.querySelector(`[data-comfyui-slot="${CSS.escape(slotKey(slot))}"]`);
    if (!input) continue;
    const original = ((graph[slot.node_id] || {}).inputs || {})[slot.field_name];
    const text = String(input.value ?? "").trim();
    if (slot.field_type === "seed") {
      if (text) changed += 1;
    } else if (slot.field_type === "bool") {
      if ((text === "true") !== Boolean(original)) changed += 1;
    } else if (slot.field_type === "number") {
      const value = Number(text);
      if (text !== "" && Number.isFinite(value) && value !== Number(original)) changed += 1;
    } else if (text !== String(original ?? "").trim()) {
      changed += 1;
    }
  }
  hint.hidden = changed === 0;
  hint.textContent = changed
    ? `当前有 ${changed} 项未保存调整；交给任务总管按工作流已保存的快照出片，这些调整不会自动带入——要带上就先另存进工作流，或在总管批次里逐项填。`
    : "";
}

// ---- 编辑参数 ----

function openParamsDialog() {
  if (!comfyui.detail) return;
  const dialog = $("#comfyuiParamsDialog");
  if (!dialog) return;
  renderParamsPicker();
  const search = $("#comfyuiParamsSearch");
  if (search) {
    search.value = "";
    filterParamsPicker();
  }
  const error = $("#comfyuiParamsError");
  if (error) error.textContent = "";
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeParamsDialog() {
  const dialog = $("#comfyuiParamsDialog");
  if (!dialog) return;
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

function renderParamsPicker() {
  const picker = $("#comfyuiParamsPicker");
  if (!picker) return;
  picker.textContent = "";
  const checked = visibleKeySet();
  for (const slot of orderedSlots(comfyui.detail?.params || [])) {
    const row = document.createElement("label");
    row.className = "comfyui-params-item";
    row.draggable = true;
    row.dataset.paramKey = slotKey(slot);
    const handle = document.createElement("span");
    handle.className = "comfyui-params-handle";
    handle.textContent = "⠿";
    handle.setAttribute("aria-hidden", "true");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.dataset.paramKey = slotKey(slot);
    box.checked = checked.has(slotKey(slot));
    const text = document.createElement("span");
    text.className = "comfyui-params-text";
    const roleLabel = ROLE_LABELS[slot.role];
    text.textContent = roleLabel ? `${roleLabel} · ${slot.label}` : slot.label;
    row.append(handle, box, text, moveButtons(row));
    picker.append(row);
  }
}

// 搜索过滤：按行文本（节点号 · 节点名 · 字段 → 端口）匹配，不动的行只是暂时藏起来，
// 勾选与顺序都还在 DOM 里，保存时全量照收。隐藏用 style.display 而不是 hidden 属性——
// .comfyui-params-item 的 display:flex 会盖掉 [hidden]。
function filterParamsPicker() {
  const picker = $("#comfyuiParamsPicker");
  if (!picker) return;
  const query = String($("#comfyuiParamsSearch")?.value || "").trim().toLowerCase();
  let shown = 0;
  for (const row of picker.querySelectorAll(".comfyui-params-item")) {
    const hit = !query || String(row.textContent || "").toLowerCase().includes(query);
    row.style.display = hit ? "" : "none";
    if (hit) shown += 1;
  }
  const empty = $("#comfyuiParamsSearchEmpty");
  if (empty) empty.hidden = shown > 0;
}

// 触屏和键盘没法拖：给每行配一对上下移按钮，落序与拖拽走同一份 DOM 顺序。
function moveButtons(row) {
  const wrap = document.createElement("span");
  wrap.className = "comfyui-params-move";
  for (const [dir, label, title] of [["up", "↑", "上移"], ["down", "↓", "下移"]]) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "comfyui-params-move-btn";
    button.dataset.move = dir;
    button.textContent = label;
    button.title = title;
    button.setAttribute("aria-label", title);
    wrap.append(button);
  }
  return wrap;
}

function movePickerRow(row, direction) {
  const picker = $("#comfyuiParamsPicker");
  if (!picker || !row) return;
  const sibling = direction === "up" ? row.previousElementSibling : row.nextElementSibling;
  if (!sibling) return;
  if (direction === "up") picker.insertBefore(row, sibling);
  else picker.insertBefore(sibling, row);
  clearParamsError();
  row.querySelector("input[type=checkbox]")?.focus();
}

function clearParamsError() {
  const error = $("#comfyuiParamsError");
  if (error) error.textContent = "";
}

// 「编辑参数」清单的拖拽落序：行是每次打开重建的，事件委托在容器上。
function bindParamsPickerDrag() {
  const picker = $("#comfyuiParamsPicker");
  if (!picker) return;
  let dragging = null;
  picker.addEventListener("dragstart", (event) => {
    const row = event.target.closest?.(".comfyui-params-item");
    if (!row) return;
    dragging = row;
    row.classList.add("is-dragging");
    event.dataTransfer.effectAllowed = "move";
    try { event.dataTransfer.setData("text/plain", row.dataset.paramKey || ""); } catch (_) { /* 老浏览器不认 */ }
  });
  picker.addEventListener("dragend", () => {
    picker.querySelectorAll(".is-dragging, .drop-target")
      .forEach((el) => el.classList.remove("is-dragging", "drop-target"));
    dragging = null;
  });
  picker.addEventListener("dragover", (event) => {
    if (!dragging) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    const over = event.target.closest?.(".comfyui-params-item");
    picker.querySelectorAll(".drop-target").forEach((el) => el.classList.remove("drop-target"));
    if (!over || over === dragging) return;
    over.classList.add("drop-target");
  });
  picker.addEventListener("drop", (event) => {
    if (!dragging) return;
    event.preventDefault();
    const over = event.target.closest?.(".comfyui-params-item");
    picker.querySelectorAll(".drop-target").forEach((el) => el.classList.remove("drop-target"));
    if (!over || over === dragging) return;
    // 落在目标行的下半部分就插到它后面，和肉眼看到的插入位置一致。
    const rect = over.getBoundingClientRect();
    const after = event.clientY > rect.top + rect.height / 2;
    picker.insertBefore(dragging, after ? over.nextElementSibling : over);
    clearParamsError();
  });
}

async function saveParamsConfig() {
  if (!comfyui.detail) return;
  const picker = $("#comfyuiParamsPicker");
  if (!picker) return;
  const visible = [];
  const order = [];
  // DOM 顺序就是用户拖出来的顺序：勾选决定显示哪些，顺序决定主表单里谁在上面。
  for (const box of picker.querySelectorAll("input[type=checkbox]")) {
    if (!box.dataset.paramKey) continue;
    const [nodeId, ...rest] = String(box.dataset.paramKey).split("|");
    const row = { node_id: nodeId, field_name: rest.join("|") };
    order.push(row);
    if (box.checked) visible.push(row);
  }
  const button = $("#comfyuiParamsSave");
  if (button) button.disabled = true;
  try {
    const payload = await api(
      `/api/comfyui/workflows/${encodeURIComponent(comfyui.detail.id)}/params`,
      { method: "POST", body: { visible, order } });
    comfyui.detail = {
      ...comfyui.detail,
      visible_params: payload.visible_params || visible,
      param_order: payload.param_order || order,
    };
    // 从此这份工作流的显示集就是「知道媒体槽」的新格式：按实际勾选执行。
    markVisibleSetKnowsMedia();
    closeParamsDialog();
    renderParamForm();
    toast("参数显示与顺序已保存，这份工作流下次打开也照此显示");
  } catch (error) {
    const line = $("#comfyuiParamsError");
    if (line) line.textContent = String(error.message || "保存失败");
  } finally {
    if (button) button.disabled = false;
  }
}

// ---- 模型候选（Checkpoint / LoRA）----

async function loadModelCandidates(force = false) {
  if (!force && comfyui.modelCandidates !== null) return;
  try {
    const payload = await api("/api/comfyui/models");
    comfyui.modelCandidates = {
      checkpoints: Array.isArray(payload.models) ? payload.models : [],
      loras: Array.isArray(payload.loras) ? payload.loras : [],
      unets: Array.isArray(payload.unets) ? payload.unets : [],
      clips: Array.isArray(payload.clips) ? payload.clips : [],
      vaes: Array.isArray(payload.vaes) ? payload.vaes : [],
      controlnets: Array.isArray(payload.controlnets) ? payload.controlnets : [],
    };
  } catch (_) {
    // 候选拿不到就只留自由输入，不阻断编辑
    comfyui.modelCandidates = { checkpoints: [], loras: [], unets: [], clips: [], vaes: [], controlnets: [] };
  }
}

// 各模型加载角色对应的后端候选清单键；不在表里的角色没有离线兜底。
const ROLE_CANDIDATE_KEYS = {
  checkpoint: "checkpoints",
  lora: "loras",
  unet: "unets",
  clip: "clips",
  vae: "vaes",
  controlnet: "controlnets",
};

function modelCandidatesFor(role) {
  const cache = comfyui.modelCandidates;
  if (!cache) return [];
  const key = ROLE_CANDIDATE_KEYS[role];
  return key ? (cache[key] || []) : [];
}

// 槽位候选：节点声明里带来的枚举优先（schema 增强）；模型加载槽离线时回退
// /api/comfyui/models 的对应目录清单；都没有就自由输入。
function slotCandidates(slot) {
  const declared = Array.isArray(slot.candidates) ? slot.candidates : [];
  if (declared.length) return declared;
  if (ROLE_CANDIDATE_KEYS[slot.role]) return modelCandidatesFor(slot.role);
  return [];
}

// 给输入框挂可搜索候选下拉；候选不可用时静默，自由输入不受影响。
function attachCandidateCombo(input, wrap, candidatesFn) {
  const list = document.createElement("div");
  list.className = "comfyui-combo-list";
  list.hidden = true;
  const positionList = () => {
    if (list.hidden || !list.isConnected) return;
    const rect = input.getBoundingClientRect();
    const maxHeight = Math.min(190, window.innerHeight * 0.4);
    const below = window.innerHeight - rect.bottom - 8;
    const above = rect.top - 8;
    const openAbove = below < Math.min(maxHeight, 120) && above > below;
    const available = Math.max(80, Math.min(maxHeight, openAbove ? above : below));
    list.style.left = `${rect.left}px`;
    list.style.width = `${rect.width}px`;
    list.style.maxHeight = `${available}px`;
    list.style.top = openAbove
      ? `${Math.max(8, rect.top - available - 2)}px`
      : `${rect.bottom + 2}px`;
  };
  const trackPosition = () => {
    window.addEventListener("scroll", positionList, true);
    window.addEventListener("resize", positionList);
  };
  const stopTracking = () => {
    window.removeEventListener("scroll", positionList, true);
    window.removeEventListener("resize", positionList);
  };
  const showList = () => {
    list.hidden = false;
    if (!list.isConnected) {
      document.body.append(list);
      trackPosition();
    }
    positionList();
  };
  const hideList = () => {
    list.hidden = true;
    list.remove();
    stopTracking();
  };
  const renderMatches = (showAll = false) => {
    const text = String(input.value || "").trim().toLowerCase();
    const all = candidatesFn();
    const matched = showAll || !text
      ? all
      : all.filter((name) => name.toLowerCase().includes(text));
    const matches = matched.slice(0, 30);
    list.textContent = "";
    list.style.height = "auto";
    list.style.maxHeight = "";
    if (!matches.length) {
      const empty = document.createElement("div");
      empty.className = "comfyui-combo-option";
      empty.textContent = "没有匹配的候选；直接填文件名也可以";
      list.append(empty);
      showList();
      return;
    }
    for (const name of matches) {
      const option = document.createElement("button");
      option.type = "button";
      option.className = "comfyui-combo-option";
      option.textContent = name;
      option.addEventListener("mousedown", (event) => {
        event.preventDefault();
        input.value = name;
        hideList();
      });
      list.append(option);
    }
    showList();
  };
  input.addEventListener("focus", () => renderMatches(true));
  input.addEventListener("input", () => renderMatches(false));
  input.addEventListener("blur", hideList);
}

// 主表单已填内容的来源：有草稿用草稿，否则用工作流里原本的值。
// 表单每次重绘（切页、传完媒体、改完「编辑」）都会重建控件，只认草稿才不会把
// 用户刚填的提示词打回工作流默认值（用户口径 2026-10-29）。
// 草稿键带工作流 id：不同工作流常有同名节点端口（如 3|seed），不隔离会互相串值。
function draftKey(key) {
  return `${comfyui.detail?.id || ""}|${key}`;
}

function savedDraft(key) {
  return comfyui.drafts.get(draftKey(key));
}

function initialFieldValue(slot, key) {
  const draft = savedDraft(key);
  return draft !== undefined ? draft : (slot.default ?? "");
}

// 用户改一个槽就记一条：重绘后仍按这里复原，刷新触发再也不会把提示词打回原值。
function rememberDraft(key, value) {
  comfyui.drafts.set(draftKey(key), String(value ?? ""));
}

// 「默认」：只把参数值还原成工作流原本的值，清空这份工作流的草稿后重绘；
// 「编辑」保存的显示项与顺序在 comfyui.detail 里，不受这条路径影响。
function resetParamValues() {
  if (!comfyui.detail) return;
  const prefix = `${comfyui.detail.id || ""}|`;
  for (const key of [...comfyui.drafts.keys()]) {
    if (key.startsWith(prefix)) comfyui.drafts.delete(key);
  }
  renderParamForm();
  toast("已还原成工作流里原本的参数值");
}

function renderSlot(slot) {
  const type = slot.field_type;
  // 媒体槽内部有自己的「选择文件」label：<label> 不能嵌 <label>，外层用 div。
  const isMedia = slot.control === "media" || MEDIA_FIELD_TYPES.has(type);
  const wrap = document.createElement(isMedia ? "div" : "label");
  wrap.className = "comfyui-param" + (isMedia ? " comfyui-param-media" : "");
  const title = document.createElement("span");
  title.className = "comfyui-param-label";
  // 宽/高两个槽位同属「分辨率」角色：主标签要分得清，否则表单里两行都叫「分辨率」。
  const roleLabel = slot.role === "resolution"
    ? (slot.field_name === "height" ? "分辨率（高）" : "分辨率（宽）")
    : ROLE_LABELS[slot.role];
  const fullLabel = slot.label || `${slot.node_id} · ${slot.field_name}`;
  // 有用途角色的槽位也要显示完整来源与端口映射；角色名只做前缀，不覆盖映射。
  title.textContent = roleLabel ? `${roleLabel} · ${fullLabel}` : fullLabel;
  title.title = fullLabel;
  wrap.append(title);
  const key = `${slot.node_id}|${slot.field_name}`;
  wrap.dataset.paramKey = key;
  if (isMedia) {
    wrap.append(renderMediaPicker(slot, key));
    return wrap;
  }
  if (type === "bool") {
    const select = document.createElement("select");
    select.dataset.comfyuiSlot = key;
    for (const [value, text] of [["true", "开"], ["false", "关"]]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      select.append(option);
    }
    const draft = savedDraft(key);
    select.value = draft === "true" || draft === "false"
      ? draft
      : (slot.default ? "true" : "false");
    wrap.append(select);
    return wrap;
  }
  if (slot.role === "positive_prompt" || slot.role === "negative_prompt") {
    // 主提示词是多行文本：单行 input 没法正常编辑长提示词；正向框给足高度，
    // 用户还能拖动下缘继续调。其他有角色的槽位（采样器/调度器/模型）走单行。
    const area = document.createElement("textarea");
    area.rows = slot.role === "positive_prompt" ? 9 : 5;
    area.dataset.comfyuiSlot = key;
    area.value = initialFieldValue(slot, key);
    area.spellcheck = false;
    wrap.append(area);
    return wrap;
  }
  if (slot.control === "multiline") {
    // 节点声明为多行字符串（如自定义多行文本节点）：同样给多行框。
    const area = document.createElement("textarea");
    area.rows = 4;
    area.dataset.comfyuiSlot = key;
    area.value = initialFieldValue(slot, key);
    area.spellcheck = false;
    wrap.append(area);
    return wrap;
  }
  if (type === "image") {
    // 声明拿不到时的兜底：老数据里的 image 槽按普通字符串处理。
    const input = document.createElement("input");
    input.type = "text";
    input.dataset.comfyuiSlot = key;
    input.value = initialFieldValue(slot, key);
    input.placeholder = "已在 ComfyUI 输入目录里的图片文件名";
    wrap.append(input);
    return wrap;
  }
  const input = document.createElement("input");
  input.type = type === "number" ? "number" : "text";
  input.step = "any";
  input.dataset.comfyuiSlot = key;
  if (type === "seed") {
    input.placeholder = "留空 = 每次随机；填数字 = 固定种子";
    input.value = savedDraft(key) ?? "";
  } else {
    input.value = initialFieldValue(slot, key);
  }
  const candidates = slotCandidates(slot);
  if (slot.control === "combo" || ROLE_CANDIDATE_KEYS[slot.role]) {
    // 枚举/模型槽：可搜索下拉；候选不可用或当前值不在候选里也照常编辑提交，
    // 不静默替换（计划 §4）。
    input.autocomplete = "off";
    input.spellcheck = false;
    if (slot.role === "lora") {
      input.placeholder = "从 ComfyUI LoRA 里选，或直接输入文件名";
    } else if (ROLE_CANDIDATE_KEYS[slot.role]) {
      input.placeholder = "从 ComfyUI 模型里选，或直接输入文件名";
    }
    attachCandidateCombo(input, wrap, () => slotCandidates(slot));
  }
  wrap.append(input);
  return wrap;
}

// ---- 媒体输入槽（图片/音频/视频）：按类型分组卡片 + 槽位块 + 拖拽/点选上传 ----

const MEDIA_KIND_LABELS = { image: "图片", audio: "音频", video: "视频" };
// 三张卡的先后顺序：图片 → 音频 → 视频（沿用 ROLE_ORDER 的媒体顺序）。
const MEDIA_GROUP_ORDER = ["image", "audio", "video"];
// MEDIA_ACCEPT 是 ".png,.jpg" 形态；拖拽校验要的是扩展名集合。
const MEDIA_EXT_SETS = {};
for (const [kind, accept] of Object.entries(MEDIA_ACCEPT)) {
  MEDIA_EXT_SETS[kind] = new Set(accept.split(",").map((item) => item.trim().toLowerCase()));
}

const MEDIA_GROUP_ICONS = {
  image: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>',
  audio: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>',
  video: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="2" y="4" width="14" height="16" rx="2"/><path d="M16 10l6-3v10l-6-3z"/></svg>',
};
const MEDIA_PLAY_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';

// 判定口径与 renderSlot 的 isMedia 完全一致：control 声明或 field_type 命中即媒体槽；
// kind 取 slot.media（后端显式映射）优先。类型不在三集合里的边缘数据返回 ""，
// 继续走 renderSlot → renderMediaPicker 的老行渲染，不至于丢失控件。
function mediaKindOfSlot(slot) {
  if (slot.control !== "media" && !MEDIA_FIELD_TYPES.has(slot.field_type)) return "";
  const kind = slot.media || slot.field_type;
  return MEDIA_GROUP_ORDER.includes(kind) ? kind : "";
}

// 四态口径唯一实现（分组块与兜底行共用）：判定与文案一字不改（§3.2）。
// full 用于块 title/兜底行的整句；badge 是块上角标的短文案；
// clearTitle 是已选块「清除」按钮的三种说明（与旧版一致）。
function mediaSlotStatus(slot, picked) {
  const emptyInWorkflow = !String(slot.default ?? "").trim() || String(slot.default).trim() === "None";
  const skipSafe = slot.media_optional === true;
  if (picked) {
    return {
      badge: "已选",
      cls: "ok",
      full: `已选：${picked.name}`,
      clearTitle: skipSafe
        ? "清除选择：生成时跳过这个输入"
        : emptyInWorkflow
          ? "清除选择：回到工作流里的空值"
          : "清除选择：生成时按工作流原本的文件执行",
    };
  }
  if (skipSafe) return { badge: "未选即跳过", cls: "", full: "未选即跳过" };
  if (emptyInWorkflow) return { badge: "请选择文件", cls: "", full: "请选择文件" };
  return { badge: "沿用原文件", cls: "keep", full: "未选沿用原文件" };
}

// 块底端口小字：节点号 · 节点名 → 目标端口（截断省略；完整映射在块 title 与「编辑参数」里）。
function mediaPortText(slot) {
  const ports = Array.isArray(slot.connections)
    ? slot.connections.map((item) => String(item?.target_field || "")).filter(Boolean)
    : [];
  const target = ports.length ? ports.join("、") : String(slot.field_name || "");
  const head = String(slot.node_title || slot.class_type || "");
  const base = head ? `${slot.node_id} · ${head}` : String(slot.node_id);
  return target ? `${base} → ${target}` : base;
}

function renderMediaGroup(kind, slots) {
  const group = document.createElement("section");
  group.className = "comfyui-media-group";
  group.dataset.comfyuiMediaGroup = kind;
  const collapsed = comfyui.mediaGroupsCollapsed[kind] === true;
  if (collapsed) group.classList.add("collapsed");
  const pickedCount = slots.filter(
    (slot) => comfyui.mediaSelection.get(slotKey(slot))?.path).length;
  const head = document.createElement("button");
  head.type = "button";
  head.className = "comfyui-media-group-head";
  head.setAttribute("aria-expanded", String(!collapsed));
  const icon = document.createElement("span");
  icon.className = "g-icon";
  icon.innerHTML = MEDIA_GROUP_ICONS[kind];
  const name = document.createElement("span");
  name.className = "g-name";
  name.textContent = MEDIA_KIND_LABELS[kind] || "媒体";
  const count = document.createElement("span");
  count.className = "g-count";
  count.textContent = `${slots.length} 个输入`;
  // 0/N 灰、部分选橙、全选绿：一眼看出这组还差几个（§3.1）。
  const fill = document.createElement("span");
  fill.className = "g-fill"
    + (pickedCount === 0 ? " idle" : pickedCount < slots.length ? " warn" : " ok");
  fill.textContent = `${pickedCount}/${slots.length} 已选`;
  const caret = document.createElement("span");
  caret.className = "caret";
  caret.textContent = "▾";
  head.append(icon, name, count, fill, caret);
  head.addEventListener("click", () => {
    comfyui.mediaGroupsCollapsed[kind] = !collapsed;
    rerenderParamForm();
  });
  const tiles = document.createElement("div");
  tiles.className = "comfyui-media-tiles";
  for (const slot of slots) tiles.append(renderMediaTile(slot, slotKey(slot), kind));
  const hint = document.createElement("p");
  hint.className = "comfyui-media-drop-hint";
  hint.textContent = "把文件拖到卡片任意处，会按槽位顺序依次填进空位";
  group.append(head, tiles, hint);
  wireMediaGroupDrop(group, tiles, slots, kind);
  return group;
}

function renderMediaTile(slot, key, kind) {
  const picked = comfyui.mediaSelection.get(key) || null;
  const status = mediaSlotStatus(slot, picked);
  // 「沿用原文件」的槽按实块渲染（占位图标 + 原文件名），不是虚线空块（§3.4）。
  const keepOriginal = !picked && status.cls === "keep";
  const tile = document.createElement("div");
  tile.className = "comfyui-media-tile"
    + (kind === "audio" ? " audio" : kind === "video" ? " video" : "");
  if (!picked && !keepOriginal) tile.classList.add("empty");
  tile.dataset.comfyuiMediaTile = key;
  tile.title = `${slot.label || ""}\n${status.full}`;
  const file = document.createElement("input");
  file.type = "file";
  file.accept = MEDIA_ACCEPT[kind] || "";
  file.hidden = true;
  file.addEventListener("change", () => void onMediaPicked(slot, key, file));

  const badge = document.createElement("span");
  badge.className = "t-status" + (status.cls ? ` ${status.cls}` : "");
  badge.textContent = status.badge;

  if (picked || keepOriginal) {
    const thumb = document.createElement("div");
    thumb.className = "thumb";
    const actions = document.createElement("div");
    actions.className = "t-actions";
    const replace = document.createElement("button");
    replace.type = "button";
    replace.textContent = "⇄";
    replace.title = picked ? "替换这个文件" : `选择文件替换（当前沿用 ${slot.default}）`;
    replace.setAttribute("aria-label", replace.title);
    replace.addEventListener("click", (event) => {
      event.stopPropagation();
      file.click();
    });
    actions.append(replace);
    if (picked) {
      const clear = document.createElement("button");
      clear.type = "button";
      clear.textContent = "✕";
      clear.title = status.clearTitle;
      clear.setAttribute("aria-label", clear.title);
      clear.addEventListener("click", (event) => {
        event.stopPropagation();
        comfyui.mediaSelection.delete(key);
        rerenderParamForm();
      });
      actions.append(clear);
    }
    if (picked) {
      const record = { path: picked.path, thumb_path: picked.thumb_path || "", name: picked.name || "" };
      if (kind === "image") {
        const img = document.createElement("img");
        img.src = attachmentThumbUrl(record);
        img.alt = record.name;
        img.loading = "lazy";
        // 放大走 03-media 的 document 级灯箱委托（与产物卡同一入口）。
        img.dataset.largeUrl = localFileUrl(record.path);
        thumb.append(img);
      } else if (kind === "video") {
        // 块只有 ~96px 宽，内嵌控件没法用：preload=metadata 的首帧当海报 +
        // 播放按钮，点块走放大播放浮层（§4.2）。
        const video = document.createElement("video");
        video.src = localFileUrl(record.path);
        video.preload = "metadata";
        video.muted = true;
        video.playsInline = true;
        thumb.append(video);
        const play = document.createElement("span");
        play.className = "play";
        play.innerHTML = MEDIA_PLAY_ICON;
        thumb.append(play);
        const duration = document.createElement("span");
        duration.className = "t-duration";
        video.addEventListener("loadedmetadata", () => {
          duration.textContent = formatMediaDuration(video.duration);
        });
        thumb.append(duration);
        tile.addEventListener("click", (event) => {
          if (event.target.closest(".t-actions")) return;
          openMediaPreviewOverlay(localFileUrl(record.path), "video");
        });
      } else {
        const icon = document.createElement("span");
        icon.className = "ph-icon";
        icon.innerHTML = MEDIA_PLAY_ICON;
        thumb.append(icon);
        tile.addEventListener("click", (event) => {
          if (event.target.closest(".t-actions")) return;
          openMediaPreviewOverlay(localFileUrl(record.path), "audio");
        });
      }
    } else {
      const icon = document.createElement("span");
      icon.className = "ph-icon";
      icon.innerHTML = MEDIA_GROUP_ICONS[kind];
      thumb.append(icon);
      tile.addEventListener("click", (event) => {
        if (event.target.closest(".t-actions")) return;
        file.click();
      });
    }
    const name = document.createElement("div");
    name.className = "t-name";
    name.textContent = picked ? picked.name : String(slot.default ?? "");
    name.title = name.textContent;
    const port = document.createElement("div");
    port.className = "t-port";
    port.textContent = mediaPortText(slot);
    tile.append(thumb, badge, actions, name, port);
  } else {
    // 空块：虚线格，点它即唤起系统选择器；音频是扁平小条，端口并进标签行。
    const plus = document.createElement("span");
    plus.className = "plus";
    plus.textContent = "＋";
    const label = document.createElement("span");
    label.className = "plus-label";
    label.textContent = kind === "audio"
      ? `选择${MEDIA_KIND_LABELS[kind]} · ${mediaPortText(slot)}`
      : `选择${MEDIA_KIND_LABELS[kind] || "文件"}`;
    const dropLabel = document.createElement("span");
    dropLabel.className = "drop-label";
    dropLabel.textContent = "松开即上传";
    const wrap = document.createElement("div");
    wrap.className = "plus-wrap";
    wrap.append(plus, label, dropLabel);
    tile.append(badge, wrap);
    if (kind !== "audio") {
      const port = document.createElement("div");
      port.className = "t-port";
      port.textContent = mediaPortText(slot);
      tile.append(port);
    }
    tile.addEventListener("click", () => file.click());
  }
  tile.append(file);
  wireMediaTileDrop(tile, key, kind);
  return tile;
}

function formatMediaDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

// 视频/音频放大播放浮层：动态建节点（本模块不改 index.html），点遮罩或 Esc 关闭。
function openMediaPreviewOverlay(url, kind) {
  document.querySelector(".comfyui-media-preview-overlay")?.remove();
  const overlay = document.createElement("div");
  overlay.className = "comfyui-media-preview-overlay";
  const media = document.createElement(kind === "video" ? "video" : "audio");
  media.src = url;
  media.controls = true;
  media.autoplay = true;
  if (kind === "video") media.playsInline = true;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "comfyui-media-preview-close";
  close.textContent = "✕";
  close.title = "关闭";
  close.setAttribute("aria-label", "关闭预览");
  const shutdown = () => {
    overlay.remove();
    document.removeEventListener("keydown", onKey);
  };
  const onKey = (event) => {
    if (event.key === "Escape") shutdown();
  };
  close.addEventListener("click", shutdown);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) shutdown();
  });
  document.addEventListener("keydown", onKey);
  overlay.append(media, close);
  document.body.append(overlay);
}

// ---- 拖拽（桌面增强；触屏不派发 HTML5 drag 事件，点选才是主路径）----

function dragHasFiles(event) {
  return Boolean(event.dataTransfer?.types?.includes("Files"));
}

// 目录拖放一律拒绝：媒体槽只收单文件，不进文件夹索引那套链路（§4.3）。
function dropHasDirectory(event) {
  for (const item of event.dataTransfer?.items || []) {
    const entry = typeof item.webkitGetAsEntry === "function" ? item.webkitGetAsEntry() : null;
    if (entry?.isDirectory) return true;
  }
  return false;
}

function fileMatchesMediaKind(file, kind) {
  const name = String(file?.name || "").toLowerCase();
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot) : "";
  return (MEDIA_EXT_SETS[kind] || new Set()).has(ext);
}

function fileKindOf(file) {
  for (const kind of MEDIA_GROUP_ORDER) {
    if (fileMatchesMediaKind(file, kind)) return kind;
  }
  return "";
}

// 类型不符的统一提示：指明拖的是什么、该去哪张卡（§3.5）——目标卡是文件自己的类型卡，
// 不是当前拖到的这张（音频拖到图片卡要说「请拖到音频卡」）。
function toastMediaKindMismatch(file, kind) {
  const actual = fileKindOf(file);
  toast(actual
    ? `这是${MEDIA_KIND_LABELS[actual]}，请拖到${MEDIA_KIND_LABELS[actual]}卡`
    : `这个文件不是${MEDIA_KIND_LABELS[kind]}，请换${MEDIA_KIND_LABELS[kind]}文件`);
}

function wireMediaTileDrop(tile, key, kind) {
  tile.addEventListener("dragover", (event) => {
    if (!dragHasFiles(event)) return;
    // dragover 也要 preventDefault：不声明「这里可以放」，drop 根本不会派发。
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    tile.classList.add("dragover");
  });
  tile.addEventListener("dragleave", (event) => {
    if (event.relatedTarget && tile.contains(event.relatedTarget)) return;
    tile.classList.remove("dragover");
  });
  tile.addEventListener("drop", (event) => {
    if (!dragHasFiles(event)) return;
    event.preventDefault();
    // 块自己收下，不再冒泡给卡片按空位填。
    event.stopPropagation();
    tile.classList.remove("dragover");
    void handleTileMediaDrop(event, key, kind);
  });
}

async function handleTileMediaDrop(event, key, kind) {
  const files = Array.from(event.dataTransfer?.files || []);
  if (!files.length) return;
  if (dropHasDirectory(event)) {
    toast("媒体输入只收单个文件，不支持拖文件夹");
    return;
  }
  const bad = files.find((file) => !fileMatchesMediaKind(file, kind));
  if (bad) {
    toastMediaKindMismatch(bad, kind);
    return;
  }
  if (files.length > 1) {
    toast(`一次拖了 ${files.length} 个文件，只有第一个进入这个输入；要批量请拖到卡片空白处`);
  }
  await uploadMediaFiles([{ key, file: files[0] }]);
}

function wireMediaGroupDrop(group, tiles, slots, kind) {
  const setCardDrop = (on) => {
    tiles.classList.toggle("card-drop", on);
    group.classList.toggle("card-drop", on);
  };
  group.addEventListener("dragover", (event) => {
    if (!dragHasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    setCardDrop(true);
  });
  group.addEventListener("dragleave", (event) => {
    if (event.relatedTarget && group.contains(event.relatedTarget)) return;
    setCardDrop(false);
  });
  group.addEventListener("drop", (event) => {
    if (!dragHasFiles(event)) return;
    event.preventDefault();
    setCardDrop(false);
    void handleGroupMediaDrop(event, slots, kind);
  });
}

async function handleGroupMediaDrop(event, slots, kind) {
  const files = Array.from(event.dataTransfer?.files || []);
  if (!files.length) return;
  if (dropHasDirectory(event)) {
    toast("媒体输入只收单个文件，不支持拖文件夹");
    return;
  }
  const bad = files.find((file) => !fileMatchesMediaKind(file, kind));
  if (bad) {
    toastMediaKindMismatch(bad, kind);
    return;
  }
  // 「沿用原文件」但本次没选的槽也算空位：拖进来的文件优先用上。
  const empties = slots.filter((slot) => !comfyui.mediaSelection.get(slotKey(slot))?.path);
  if (!empties.length) {
    toast("这张卡的输入都已选好，请拖到具体块上替换");
    return;
  }
  const assignCount = Math.min(files.length, empties.length);
  if (files.length > empties.length) {
    toast(`空位不够：先填 ${empties.length} 个空位，多出的 ${files.length - empties.length} 个请拖到具体块上替换`);
  }
  await uploadMediaFiles(files.slice(0, assignCount).map((file, index) => ({
    key: slotKey(empties[index]),
    file,
  })));
}

// 串行上传 + 单槽锁：批量拖拽逐个 await，全部完成后只重绘一次（§4.3），
// 避免逐槽重绘造成闪烁。失败逐个 toast，成功的照常写入 mediaSelection。
async function uploadMediaFiles(items) {
  if (comfyui.uploadingSlot) {
    toast("上一个文件还在上传，请稍候");
    return 0;
  }
  comfyui.uploadingSlot = items.map((item) => item.key).join("|");
  let okCount = 0;
  try {
    for (const { key, file } of items) {
      const form = new FormData();
      form.append("file", file, file.name);
      try {
        const payload = await api("/api/comfyui/media", { method: "POST", body: form });
        comfyui.mediaSelection.set(key, {
          path: String(payload.path || ""),
          name: String(payload.name || file.name),
          thumb_path: String(payload.thumb_path || ""),
        });
        okCount += 1;
      } catch (error) {
        toast(`上传失败：${error.message}`);
      }
    }
  } finally {
    comfyui.uploadingSlot = "";
    rerenderParamForm();
  }
  return okCount;
}

async function onMediaPicked(slot, key, fileInput) {
  const file = fileInput.files?.[0];
  fileInput.value = "";
  if (!file) return;
  await uploadMediaFiles([{ key, file }]);
}

// 兜底渲染：control=media 但类型不在图片/音频/视频里（手改存档等边缘数据）仍按
// 老的三件套行渲染，不至于丢控件；正常媒体槽都走 renderMediaGroup/renderMediaTile。
function renderMediaPicker(slot, key) {
  const kind = slot.media || slot.field_type;
  const wrap = document.createElement("div");
  wrap.className = "comfyui-media";
  wrap.dataset.comfyuiMediaSlot = key;
  const picked = comfyui.mediaSelection.get(key) || null;
  const preview = document.createElement("div");
  preview.className = "comfyui-media-preview";
  if (picked) {
    const record = { path: picked.path, thumb_path: picked.thumb_path || "", name: picked.name || "" };
    if (mediaKind(record.path, record.name) === "image") {
      const img = document.createElement("img");
      img.src = attachmentThumbUrl(record);
      img.alt = record.name;
      img.loading = "lazy";
      preview.append(img);
    } else if (mediaKind(record.path, record.name) === "video") {
      const video = document.createElement("video");
      video.src = localFileUrl(record.path);
      video.controls = true;
      video.preload = "metadata";
      preview.append(video);
    } else {
      const audio = document.createElement("audio");
      audio.src = localFileUrl(record.path);
      audio.controls = true;
      audio.preload = "metadata";
      preview.append(audio);
    }
  }
  const note = document.createElement("span");
  note.className = "comfyui-media-note";
  const status = mediaSlotStatus(slot, picked);
  note.textContent = status.full;
  note.title = status.full;
  const actions = document.createElement("div");
  actions.className = "comfyui-media-actions";
  const pickLabel = document.createElement("label");
  pickLabel.className = "control-button tiny comfyui-media-pick";
  pickLabel.textContent = picked ? "替换" : `选择${MEDIA_KIND_LABELS[kind] || "文件"}`;
  const file = document.createElement("input");
  file.type = "file";
  file.accept = MEDIA_ACCEPT[kind] || "";
  file.hidden = true;
  file.addEventListener("change", () => void onMediaPicked(slot, key, file));
  pickLabel.append(file);
  actions.append(pickLabel);
  if (picked) {
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "control-button tiny";
    clear.textContent = "清除";
    clear.title = status.clearTitle;
    clear.addEventListener("click", () => {
      comfyui.mediaSelection.delete(key);
      rerenderParamForm();
    });
    actions.append(clear);
  }
  wrap.append(preview, note, actions);
  return wrap;
}

function rerenderParamForm() {
  // 只重画表单：不动 comfyui.detail / mediaSelection（选择状态由调用方维护）。
  renderParamForm();
}

// ---- 导入 ----

function openImportDialog() {
  const dialog = $("#comfyuiImportDialog");
  if (!dialog) return;
  $("#comfyuiImportName").value = "";
  $("#comfyuiImportText").value = "";
  $("#comfyuiImportFile").value = "";
  $("#comfyuiImportError").textContent = "";
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeImportDialog() {
  const dialog = $("#comfyuiImportDialog");
  if (!dialog) return;
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

function onImportFile(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  file.text().then((text) => {
    // 选文件视为换内容：无条件覆盖粘贴框与名称。只填空框的话，第一次导入
    // 失败后残留的旧稿会顶掉重选的文件，让人误以为换文件也没用。
    const paste = $("#comfyuiImportText");
    if (paste) paste.value = text;
    const name = $("#comfyuiImportName");
    if (name) name.value = file.name.replace(/\.json$/i, "");
    const error = $("#comfyuiImportError");
    if (error) error.textContent = "";
  }).catch((caught) => {
    const error = $("#comfyuiImportError");
    if (error) error.textContent = `读取文件失败：${caught?.message || caught}`;
  });
}

async function confirmImport() {
  if (comfyui.importing) return;
  const raw = String($("#comfyuiImportText")?.value || "").trim();
  const name = String($("#comfyuiImportName")?.value || "").trim();
  const error = $("#comfyuiImportError");
  if (!raw) {
    if (error) error.textContent = "请先选择文件或粘贴工作流 JSON 内容。";
    return;
  }
  comfyui.importing = true;
  const button = $("#comfyuiImportConfirm");
  if (button) button.disabled = true;
  try {
    const payload = await api("/api/comfyui/workflows", {
      method: "POST",
      body: { name, workflow: raw },
    });
    closeImportDialog();
    const callback = importReturn?.onSave; importReturn = null;
    if (callback) { await callback(payload.workflow); return; }
    await refreshWorkflows();
    await selectWorkflow(String(payload.workflow?.id || ""));
    toast(`已导入「${payload.workflow?.name || name}」，共 ${payload.workflow?.params?.length || 0} 个可调参数`);
  } catch (caught) {
    if (error) error.textContent = String(caught.message || "导入失败");
  } finally {
    comfyui.importing = false;
    if (button) button.disabled = false;
  }
}

// ============================================================
// 任务：提交 / 轮询 / 取消 / 重试 / 产物
// ============================================================

function collectOverrides() {
  const overrides = [];
  for (const slot of comfyui.detail?.params || []) {
    const key = `${slot.node_id}|${slot.field_name}`;
    // 只收主表单里渲染出来的输入：隐藏参数没有输入框，自然不会提交覆盖值，
    // 生成时按工作流里原本的值执行。
    const input = document.querySelector(`[data-comfyui-slot="${CSS.escape(key)}"]`);
    if (!input) continue;
    const text = String(input.value ?? "").trim();
    if (slot.field_type === "seed") {
      if (text) overrides.push({ node_id: slot.node_id, field_name: slot.field_name, value: text });
      continue;
    }
    if (slot.field_type === "bool") {
      overrides.push({ node_id: slot.node_id, field_name: slot.field_name, value: text === "true" });
      continue;
    }
    overrides.push({ node_id: slot.node_id, field_name: slot.field_name, value: text });
  }
  return overrides;
}

// 媒体槽已选的受管文件（提交时随任务走受管引用，执行前由服务端转传 ComfyUI）。
// 未选的槽不提交：声明为可选的连接由执行端移除，其余按工作流原本的值执行。
function collectMediaInputs() {
  const items = [];
  for (const slot of comfyui.detail?.params || []) {
    if ((slot.control || "") !== "media" && !MEDIA_FIELD_TYPES.has(slot.field_type)) continue;
    const picked = comfyui.mediaSelection.get(slotKey(slot));
    if (picked?.path) {
      items.push({ node_id: slot.node_id, field_name: slot.field_name, path: picked.path });
    }
  }
  return items;
}

async function submitTask() {
  if (comfyui.submitting || !comfyui.detail) return;
  const shotsInput = $("#comfyuiShots");
  let shots = 1;
  try {
    shots = Math.max(1, Math.min(200, Math.floor(Number(shotsInput?.value || 1))));
  } catch (_) { /* 回落默认 */ }
  comfyui.submitting = true;
  const button = $("#comfyuiSubmit");
  if (button) button.disabled = true;
  try {
    const payload = await api("/api/comfyui/tasks", {
      method: "POST",
      body: {
        workflow_id: comfyui.detail.id,
        overrides: collectOverrides(),
        media_inputs: collectMediaInputs(),
        shots,
      },
    });
    comfyui.activeTaskId = String(payload.task?.id || "");
    comfyui.eventSeq = 0;
    comfyui.tasks = [];
    renderTasks();
    await refreshTasks();
    startPolling();
    if (narrowPanes()) setComfyuiPane("tasks");
    toast("已提交，正在排队执行");
  } catch (error) {
    toast(`提交失败：${error.message}`);
  } finally {
    comfyui.submitting = false;
    if (button) button.disabled = !comfyui.detail;
  }
}

async function refreshTasks(silent = false) {
  try {
    const payload = await api("/api/comfyui/tasks");
    const previous = comfyui.tasks.find((row) => row.id === comfyui.activeTaskId);
    comfyui.tasks = payload.tasks || [];
    const active = comfyui.tasks.find((row) => isTaskActive(row));
    if (active) {
      comfyui.activeTaskId = String(active.id);
      if (String(previous?.status || "") !== String(active.status)) comfyui.eventSeq = 0;
      startPolling();
    } else if (!isTaskActiveById(comfyui.activeTaskId)) {
      stopPolling();
    }
    renderTasks();
    await renderLatestOutputs();
  } catch (error) {
    if (!silent) toast(`读取任务列表失败：${error.message}`);
  }
}

function isTaskActive(row) {
  return row.status === "queued" || row.status === "running";
}

function isTaskActiveById(id) {
  if (!id) return false;
  const row = comfyui.tasks.find((item) => item.id === id);
  return row ? isTaskActive(row) : false;
}

function startPolling() {
  if (comfyui.pollTimer) return;
  if (currentStudioPage() !== "comfyui") return;
  comfyui.pollTimer = setInterval(() => { void pollActiveTask(); }, POLL_MS);
}

function stopPolling() {
  if (comfyui.pollTimer) {
    clearInterval(comfyui.pollTimer);
    comfyui.pollTimer = null;
  }
}

async function pollActiveTask() {
  if (currentStudioPage() !== "comfyui") {
    stopPolling();
    return;
  }
  if (!isTaskActiveById(comfyui.activeTaskId)) {
    stopPolling();
    return;
  }
  try {
    const payload = await api(
      `/api/comfyui/tasks/${encodeURIComponent(comfyui.activeTaskId)}/events?after=${comfyui.eventSeq}`);
    const events = payload.events || [];
    if (events.length) comfyui.eventSeq = Number(payload.last_seq || comfyui.eventSeq);
    const taskPayload = await api(`/api/comfyui/tasks/${encodeURIComponent(comfyui.activeTaskId)}`);
    const task = taskPayload.task || {};
    const index = comfyui.tasks.findIndex((row) => row.id === task.id);
    if (index >= 0) comfyui.tasks[index] = task;
    else comfyui.tasks.unshift(task);
    renderTasks();
    await renderLatestOutputs();
    if (!isTaskActive(task)) {
      stopPolling();
      comfyui.activeTaskId = "";
      void refreshHealth();
    }
  } catch (_) { /* 下个周期重试 */ }
}

// 卡片指纹：状态/进度/错误/时间任一变化才算「变了」，指纹相同就保留现有 DOM，
// 避免每次轮询整块重建造成闪屏。
function taskFingerprint(row) {
  const progress = row.progress || {};
  return JSON.stringify([
    row.workflow_name || "", row.status || "", row.detail || "", row.error || "",
    Number(progress.done || 0), Number(progress.total || 0), Number(progress.failed || 0),
    (row.result?.outputs || []).length,
    Number(row.created_at || 0), Number(row.finished_at || 0),
    Boolean(row.can_retry), Boolean(row.can_cancel),
    comfyui.deleting.has(String(row.id || "")) ? "deleting" : "",
  ]);
}

function renderTasks() {
  const list = $("#comfyuiTaskList");
  if (!list) return;
  const rows = comfyui.tasks.slice(0, 12);
  if (!rows.length) {
    if (!list.dataset.empty) {
      list.textContent = "";
      list.dataset.empty = "1";
      const empty = document.createElement("p");
      empty.className = "comfyui-empty";
      empty.textContent = "还没有任务。选好工作流和参数后点「开始生成」。";
      list.append(empty);
    }
    return;
  }
  delete list.dataset.empty;
  const existing = new Map();
  for (const node of Array.from(list.children)) {
    if (node.dataset?.taskId) existing.set(node.dataset.taskId, node);
    else node.remove(); // 空态占位
  }
  const wanted = [];
  for (const row of rows) {
    const id = String(row.id || "");
    const fingerprint = taskFingerprint(row);
    let card = existing.get(id);
    if (!card || card.dataset.fingerprint !== fingerprint) {
      // 先摘掉旧卡片再放新的：map 引用被替换后旧节点不再被任何清理步骤覆盖。
      card?.remove();
      card = renderTaskCard(row);
      card.dataset.taskId = id;
      card.dataset.fingerprint = fingerprint;
      existing.set(id, card);
    }
    wanted.push(id);
  }
  const wantedSet = new Set(wanted);
  for (const [id, node] of existing) {
    if (!wantedSet.has(id)) node.remove();
  }
  // 只在顺序真的变化时才重排：无变化轮询不触碰 DOM。
  const currentOrder = Array.from(list.children).map((node) => node.dataset.taskId || "");
  if (currentOrder.join("|") !== wanted.join("|")) {
    for (const id of wanted) list.append(existing.get(id));
  }
}

function renderTaskCard(row) {
  const card = document.createElement("div");
  card.className = `comfyui-task-card status-${row.status}`;
  card.dataset.taskId = row.id;
  const head = document.createElement("div");
  head.className = "comfyui-task-head";
  const name = document.createElement("strong");
  name.textContent = row.workflow_name || "任务";
  const badge = document.createElement("span");
  badge.className = "comfyui-task-status";
  badge.textContent = statusText(row);
  head.append(name, badge);
  const meta = document.createElement("div");
  meta.className = "comfyui-task-meta";
  meta.textContent = taskMetaText(row);
  card.append(head, meta);
  if (row.error) {
    const errorLine = document.createElement("p");
    errorLine.className = "comfyui-task-error";
    errorLine.textContent = row.error;
    card.append(errorLine);
  }
  if (row.detail && !row.error) {
    const detail = document.createElement("p");
    detail.className = "comfyui-task-detail";
    detail.textContent = row.detail;
    card.append(detail);
  }
  const actions = document.createElement("div");
  actions.className = "comfyui-task-actions";
  if (row.status === "queued" || row.status === "running") {
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "control-button tiny";
    cancel.dataset.comfyuiCancel = row.id;
    cancel.textContent = "取消";
    actions.append(cancel);
  } else if (row.status === "failed" || row.status === "cancelled" || row.status === "interrupted") {
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "control-button tiny";
    retry.dataset.comfyuiRetry = row.id;
    retry.textContent = "重试续跑";
    actions.append(retry);
  }
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "control-button tiny";
  remove.dataset.comfyuiDeleteTask = row.id;
  if (comfyui.deleting.has(String(row.id || ""))) {
    remove.disabled = true;
    remove.textContent = "正在删除…";
  } else {
    remove.textContent = "删除";
  }
  actions.append(remove);
  card.append(actions);
  return card;
}

function statusText(row) {
  const map = {
    queued: "排队中",
    running: "生成中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
    interrupted: "已中断",
  };
  return map[row.status] || row.status || "";
}

// 时间显示口径：当天只报时分，跨天补上日期；时间戳缺失不显示。
function formatTime(ms) {
  const stamp = Number(ms || 0);
  if (!stamp) return "";
  const date = new Date(stamp);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (value) => String(value).padStart(2, "0");
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  if (date.toDateString() === new Date().toDateString()) return clock;
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${clock}`;
}

function taskMetaText(row) {
  const progress = row.progress || {};
  const done = Number(progress.done || 0);
  const total = Number(progress.total || row.shots || 0);
  const failed = Number(progress.failed || 0);
  const pieces = [];
  const created = formatTime(row.created_at);
  if (created) pieces.push(`创建 ${created}`);
  pieces.push(`${done}/${total} 段`);
  if (failed) pieces.push(`失败 ${failed} 段`);
  const outputs = (row.result?.outputs || []).length;
  if (outputs) pieces.push(`${outputs} 个产物`);
  // 完成/失败/取消/中断都算结束：结束时刻标出这一轮什么时候停的。
  const finished = formatTime(row.finished_at);
  if (finished && row.status !== "queued" && row.status !== "running") {
    pieces.push(`结束 ${finished}`);
  }
  return pieces.join(" · ");
}

async function onTaskListClick(event) {
  const cancel = event.target.closest("[data-comfyui-cancel]");
  if (cancel) {
    try {
      await api(`/api/comfyui/tasks/${encodeURIComponent(cancel.dataset.comfyuiCancel)}/cancel`,
        { method: "POST", body: {} });
      await refreshTasks();
    } catch (error) {
      toast(`取消失败：${error.message}`);
    }
    return;
  }
  const retry = event.target.closest("[data-comfyui-retry]");
  if (retry) {
    try {
      const payload = await api(
        `/api/comfyui/tasks/${encodeURIComponent(retry.dataset.comfyuiRetry)}/retry`,
        { method: "POST", body: {} });
      comfyui.activeTaskId = String(payload.task?.id || "");
      comfyui.eventSeq = 0;
      await refreshTasks();
      startPolling();
    } catch (error) {
      toast(`重试失败：${error.message}`);
    }
    return;
  }
  const remove = event.target.closest("[data-comfyui-delete-task]");
  if (remove) {
    await deleteTaskFlow(remove.dataset.comfyuiDeleteTask, remove);
  }
}

async function deleteTaskFlow(taskId, button) {
  const id = String(taskId || "");
  if (!id || comfyui.deleting.has(id)) return;
  const row = comfyui.tasks.find((item) => String(item.id) === id);
  const active = row ? isTaskActive(row) : false;
  const confirmText = active
    ? "这条任务还在排队或生成中。会先取消它，等它停下来再删除任务和已保存的产物文件。继续吗？"
    : "删除这条任务和它已保存的产物文件？";
  if (!window.confirm(confirmText)) return;
  comfyui.deleting.add(id);
  if (button) {
    button.disabled = true;
    button.textContent = "正在删除…";
  }
  try {
    const payload = await api("/api/comfyui/tasks", {
      method: "DELETE",
      body: { task_ids: [id], cancel_active: true },
    });
    comfyui.deleting.delete(id);
    const failed = Array.isArray(payload.failed) ? payload.failed : [];
    if (failed.length) {
      toast(`没有删成：${failed[0]?.reason || "任务没能停下来"}`);
    } else {
      toast(active ? "已取消并删除这条任务" : "已删除这条任务");
    }
    if (comfyui.activeTaskId === id) {
      comfyui.activeTaskId = "";
      stopPolling();
    }
    await refreshTasks();
  } catch (error) {
    comfyui.deleting.delete(id);
    toast(`删除失败：${error.message}`);
    await refreshTasks();
  }
}

// 请求 ComfyUI 卸载已加载模型，释放显存/内存。ComfyUI 不打断执行中的任务：
// 还有任务在跑时它会在队列空下来才卸载，文案要把「稍后生效」讲清楚。
async function freeModels() {
  const button = $("#comfyuiFreeModels");
  if (button) button.disabled = true;
  try {
    const payload = await api("/api/comfyui/free", { method: "POST", body: {} });
    toast(payload?.pending
      ? "已请求释放模型：还有任务在执行，ComfyUI 会在任务结束后卸载"
      : "已请求 ComfyUI 释放模型，显存和内存会随之释放");
  } catch (error) {
    toast(`释放失败：${error.message}`);
  } finally {
    if (button) button.disabled = false;
  }
}

// 在运行 Cat Chat 的电脑上打开产物根目录（手机端会被服务端拒绝）。
async function openOutputsFolder() {
  const button = $("#comfyuiOpenOutputs");
  if (button) button.disabled = true;
  try {
    const payload = await api("/api/comfyui/outputs/open", { method: "POST", body: {} });
    toast(payload?.path ? `已打开产物文件夹：${payload.path}` : "已打开产物文件夹");
  } catch (error) {
    toast(`无法打开产物文件夹：${error.message}`);
  } finally {
    if (button) button.disabled = false;
  }
}

async function clearFinishedTasks() {
  if (!window.confirm("删除全部已结束的任务和它们的产物文件？")) return;
  try {
    const payload = await api("/api/comfyui/tasks", { method: "DELETE", body: {} });
    const parts = [`已删除 ${payload.deleted || 0} 条任务`];
    if (payload.skipped?.length) parts.push(`${payload.skipped.length} 条正在执行，已跳过`);
    const failed = Array.isArray(payload.failed) ? payload.failed : [];
    if (failed.length) parts.push(`${failed.length} 条删除失败：${failed[0]?.reason || ""}`);
    toast(parts.join("；"));
    await refreshTasks();
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

// ---- 产物 ----

async function renderLatestOutputs() {
  const grid = $("#comfyuiOutputGrid");
  if (!grid) return;
  // 每个产物各自记着它的任务 id：不同任务的产物用各自的文件地址，
  // 同一任务的多张图逐项全展示（上限沿用 24 张）。
  const items = [];
  for (const row of comfyui.tasks) {
    // output_files 带本地绝对路径；老任务没有这个字段时退回任务 id + 文件名的直链。
    const outputs = row.result?.outputs || [];
    const files = Array.isArray(row.output_files) && row.output_files.length
      ? row.output_files
      : outputs.map((name) => ({ name, path: "" }));
    for (const file of files) {
      if (items.length >= 24) break;
      const name = String(file?.name || "");
      if (!row.id || !name) continue;
      items.push({ taskId: String(row.id), name, path: String(file?.path || "") });
    }
  }
  const wanted = [];
  const seen = new Set();
  for (const item of items) {
    const key = `${item.taskId}\u0000${item.name}`;
    if (seen.has(key)) continue;
    seen.add(key);
    wanted.push({ key, item });
  }
  if (!wanted.length) {
    if (!grid.dataset.empty) {
      grid.textContent = "";
      grid.dataset.empty = "1";
      const empty = document.createElement("p");
      empty.className = "comfyui-empty";
      empty.textContent = "还没有生成结果。提交任务后，出好的图会出现在这里。";
      grid.append(empty);
    }
    return;
  }
  delete grid.dataset.empty;
  const existing = new Map();
  for (const node of Array.from(grid.children)) {
    if (node.dataset?.outputKey) existing.set(node.dataset.outputKey, node);
    else node.remove(); // 空态占位
  }
  for (const { key, item } of wanted) {
    if (!existing.has(key)) {
      const card = renderOutputCard(item.taskId, item.name, item.path);
      card.dataset.outputKey = key;
      existing.set(key, card);
    }
  }
  for (const [key, node] of existing) {
    if (!seen.has(key)) node.remove();
  }
  // 只在产物集合或顺序变化时才重排：无变化轮询不触碰媒体元素（图片不重载）。
  const wantedOrder = wanted.map((entry) => entry.key);
  const currentOrder = Array.from(grid.children).map((node) => node.dataset.outputKey || "");
  if (currentOrder.join("|") !== wantedOrder.join("|")) {
    for (const key of wantedOrder) grid.append(existing.get(key));
  }
}

// paint 由 CSS 的 .comfyui-output-save svg 单点声明（同 .file-action 口径）。
const OUTPUT_SAVE_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v10"></path><path d="M8 11l4 4 4-4"></path><path d="M5 19h14"></path></svg>';

// 产物地址与聊天媒体卡片同一口径：走 /api/file（URL 自带口令 + 真实路径）。
// 只拼 /api/comfyui/tasks/<id>/files/<name> 的话，手机/局域网访问一律 401 成破图，
// 桌面的原生「另存为」也拿不到 path 参数（见 03-media 的 saveLocalFile）。
function outputFileUrl(taskId, name, path) {
  const local = String(path || "");
  if (local) return localFileUrl(local);
  const direct = `/api/comfyui/tasks/${encodeURIComponent(taskId)}/files/${encodeURIComponent(name)}`;
  return state.token ? `${direct}?token=${encodeURIComponent(state.token)}` : direct;
}

// 产物卡与聊天媒体卡同一套类型判定与展示语义（计划 §6）：图片缩略+灯箱、
// 视频/音频就地播放、其他类型文件 chip。mediaKind 走 /api/bootstrap.media_exts
// 的公共名单，不再各写一份扩展名正则（.bmp/.svg 曾因名单漂移彻底不可见）。
function renderOutputCard(taskId, name, path) {
  const url = outputFileUrl(taskId, name, path);
  const card = document.createElement("figure");
  card.className = "comfyui-output";
  const kind = mediaKind(String(path || url), name);
  if (kind === "image") {
    const img = document.createElement("img");
    // 造梦机产物没有侧车缩略图（attachmentThumbUrl 会推导 <stem>_thumb.webp 而
    // 404 破图），直接用原图地址，靠 loading=lazy 控制加载压力。
    img.src = url;
    img.alt = name;
    img.loading = "lazy";
    img.dataset.largeUrl = url;
    img.dataset.comfyuiOutputName = name;
    card.append(img);
  } else if (kind === "video") {
    const video = document.createElement("video");
    video.src = url;
    video.controls = true;
    video.playsInline = true;
    video.preload = "metadata";
    card.append(video);
  } else if (kind === "audio") {
    // 音频产物此前退化为下载链接；现在与聊天媒体卡一致，就地播放。
    const audio = document.createElement("audio");
    audio.src = url;
    audio.controls = true;
    audio.preload = "metadata";
    card.append(audio);
  } else {
    const link = document.createElement("a");
    link.href = url;
    link.textContent = name;
    link.download = name;
    card.append(link);
  }
  const caption = document.createElement("figcaption");
  const label = document.createElement("span");
  label.className = "comfyui-output-name";
  label.textContent = name;
  label.title = name;
  // 手机上点图只是开大图，保存要另找入口；这里给每张产物一个直接的下载按钮。
  const save = document.createElement("button");
  save.type = "button";
  save.className = "comfyui-output-save";
  save.dataset.comfyuiOutputSave = url;
  save.dataset.comfyuiOutputName = name;
  save.title = `下载 ${name}`;
  save.setAttribute("aria-label", `下载 ${name}`);
  save.innerHTML = OUTPUT_SAVE_ICON;
  caption.append(label, save);
  card.append(caption);
  return card;
}

function onOutputGridClick(event) {
  const save = event.target.closest("button[data-comfyui-output-save]");
  if (save) {
    if (!saveLocalFile(save.dataset.comfyuiOutputSave || "", save.dataset.comfyuiOutputName || "")) {
      toast("这个产物没有可下载的地址");
    }
    return;
  }
  // 图片放大走 03-media 的 document 级 data-large-url 委托（与聊天媒体卡同一入口），
  // 这里不再自己开一次灯箱，避免双触发。
  const link = event.target.closest("a[href]");
  if (link && isPywebview()) {
    void saveLocalFile(link.href, link.textContent || "output.bin");
  }
}

// ============================================================
// 分镜队列（拆书匠「分镜生图」跨页交接）：逐条填进正向提示词，不做剪贴板中转。
// 队列只存内存：刷新页面即清空，产物不在这里长期保存。
// ============================================================

function onStoryboardHandoff(event) {
  const raw = Array.isArray(event.detail?.items) ? event.detail.items : [];
  const items = raw
    .map((item, index) => ({
      no: Number(item?.no) || index + 1,
      head: String(item?.head || ""),
      prompt: String(item?.prompt || "").trim(),
    }))
    .filter((item) => item.prompt);
  if (!items.length) return;
  comfyui.storyboardQueue = items;
  renderStoryboardQueue();
  if (currentStudioPage() !== "comfyui") setStudioPage("comfyui");
  toast(`已带入 ${items.length} 条分镜提示词；选好工作流后逐条「填入」出图`);
}

function renderStoryboardQueue() {
  const box = $("#comfyuiStoryboard");
  const list = $("#comfyuiStoryboardList");
  if (!box || !list) return;
  const items = comfyui.storyboardQueue;
  box.hidden = !items.length;
  list.replaceChildren(...items.map((item, index) => {
    const row = document.createElement("div");
    row.className = "comfyui-storyboard-item";
    row.classList.toggle("filled", Boolean(item.filled));
    const fill = document.createElement("button");
    fill.type = "button";
    fill.className = "control-button tiny";
    fill.dataset.comfyuiStoryboardFill = String(index);
    fill.textContent = item.filled ? "再填入" : "填入";
    fill.title = "把这条提示词写进当前工作流的正向提示词框";
    const copy = document.createElement("span");
    copy.className = "comfyui-storyboard-copy";
    const head = document.createElement("strong");
    head.textContent = `第 ${item.no} 场${item.head ? ` · ${item.head}` : ""}`;
    const small = document.createElement("small");
    small.textContent = item.prompt;
    copy.append(head, small);
    row.append(fill, copy);
    return row;
  }));
}

function fillStoryboardPrompt(index) {
  const item = comfyui.storyboardQueue[index];
  if (!item) return;
  const slot = (comfyui.detail?.params || []).find((row) => row?.role === "positive_prompt");
  if (!comfyui.detail || !slot) {
    toast("先在左侧选择一个工作流，再填入分镜提示词");
    return;
  }
  const key = `${slot.node_id}|${slot.field_name}`;
  const area = document.querySelector(`[data-comfyui-slot="${CSS.escape(key)}"]`);
  if (!area) {
    toast("这个工作流的主表单里没有正向提示词框；用「编辑参数」把它加进来再填");
    return;
  }
  area.value = item.prompt;
  area.dispatchEvent(new Event("input", { bubbles: true }));
  item.filled = true;
  renderStoryboardQueue();
  toast(`已填入第 ${item.no} 场的提示词，确认参数后点「开始生成」`);
}
// ============================================================
// 批量生成（2026-11-05 拍板方案 A）：一批 = 方案列表 × 条目表，
// 每条目 = 一次现有提交（同款校验/上传/轮询/续跑）。
// 零猜测：写入位与素材口由用户在映射区指认（按工作流指纹记住），
// 服务端只套地址与校验，不扫描、不派生、不猜时长。
// ============================================================

const batch = {
  view: "draft",            // draft = 填单；run = 跑批进度
  draft: null,              // { title, unload_after, schemes: [scheme] }
  runId: "",                // 跑批视图正在看的批次 id
  runData: null,            // 该批次的详情（轮询刷新）
  runTimer: null,
  runElapsedTimer: null,
  runStartedAt: 0,
  histories: [],            // 历史批次（下拉）
  historiesLoaded: false,
  converting: new Set(),    // 转换中的条目（schemeId:itemIndex）
  missingItems: new Set(),  // 自检没过的条目（schemeId:itemIndex），标红拦提交
  blockedMessage: "",
  convert: { template: "", provider: "", model: "" },
};

// ---- 映射记忆（按工作流指纹；结构 = 三个写入位 + 素材口顺序）----

function batchMappingMemoryKey(fingerprint) {
  return `cfBatchMapping:${fingerprint}`;
}

function rememberBatchMapping(fingerprint, mapping) {
  if (!fingerprint || !mapping) return;
  try {
    localStorage.setItem(batchMappingMemoryKey(fingerprint), JSON.stringify({
      prompt_slot: mapping.prompt_slot || null,
      duration_slot: mapping.duration_slot || null,
      resolution_slot: mapping.resolution_slot || null,
      media_ports: Array.isArray(mapping.media_ports) ? mapping.media_ports : [],
    }));
  } catch (_) { /* 隐私模式忽略 */ }
}

function recallBatchMapping(fingerprint) {
  try {
    const raw = localStorage.getItem(batchMappingMemoryKey(fingerprint));
    const parsed = raw ? JSON.parse(raw) : null;
    if (parsed && typeof parsed === "object") {
      return {
        prompt_slot: parsed.prompt_slot || null,
        duration_slot: parsed.duration_slot || null,
        resolution_slot: parsed.resolution_slot || null,
        media_ports: Array.isArray(parsed.media_ports) ? parsed.media_ports : [],
      };
    }
  } catch (_) { /* 坏记忆当没有 */ }
  return null;
}

// ---- 方案 / 条目的构造与工具 ----

function batchMediaCandidates(detail) {
  return (detail?.params || [])
    .map((slot) => ({ ...slot, media: mediaKindOfSlot(slot) }))
    .filter((slot) => MEDIA_GROUP_ORDER.includes(slot.media));
}

function batchSlotByAddress(detail, slot) {
  if (!slot) return null;
  return (detail?.params || []).find((s) =>
    String(s.node_id) === String(slot.node_id) && String(s.field_name) === String(slot.field_name)) || null;
}

function batchSlotAddressText(detail, slot) {
  const row = batchSlotByAddress(detail, slot);
  if (!row) return `${slot.node_id} · ${slot.field_name}`;
  return `${row.node_title || row.class_type || slot.node_id} · ${slot.field_name}`;
}

function batchSlotSavedValue(detail, slot) {
  if (!slot) return "";
  return String((((detail?.prompt || {})[slot.node_id] || {}).inputs || {})[slot.field_name] ?? "");
}

function batchMappingFromWorkflow(detail) {
  // 预选建议（2026-11-05 拍板）：唯一语义正文槽 / 唯一标签命中的时长、分辨率槽；
  // 只是映射区里预先选中的值，用户随时改、随时清。素材口默认勾上必填槽。
  const mapping = { prompt_slot: null, duration_slot: null, resolution_slot: null, media_ports: [] };
  const slots = detail?.params || [];
  const positive = slots.filter((slot) => slot.role === "positive_prompt");
  const multiline = slots.filter((slot) => slot.field_type === "text" && slot.control === "multiline");
  const preselect = positive.length === 1 ? positive[0] : multiline.length === 1 ? multiline[0] : null;
  if (preselect) mapping.prompt_slot = { node_id: String(preselect.node_id), field_name: String(preselect.field_name) };
  const durationHits = slots.filter((slot) => slot.field_type === "number"
    && /时长|duration/i.test(`${slot.node_title || ""} ${slot.label || ""}`));
  if (durationHits.length === 1) {
    mapping.duration_slot = { node_id: String(durationHits[0].node_id), field_name: String(durationHits[0].field_name) };
  }
  const resolutionHits = slots.filter((slot) => (slot.field_type === "number"
      || (slot.field_type === "text" && (slot.candidates || []).length))
    && /分辨率|画幅|width|height|resolution|size/i.test(`${slot.node_title || ""} ${slot.label || ""}`));
  if (resolutionHits.length === 1) {
    mapping.resolution_slot = { node_id: String(resolutionHits[0].node_id), field_name: String(resolutionHits[0].field_name) };
  }
  mapping.media_ports = batchMediaCandidates(detail).map((slot) => ({
    node_id: String(slot.node_id),
    field_name: String(slot.field_name),
    checked: slot.media_optional === false,
  }));
  return mapping;
}

function newBatchItem(index, patch = {}) {
  return {
    name: `条目 ${index}`,
    script: "",
    prompt: "",
    duration: "",
    resolution: "",
    shots: 1,
    files: {},
    ...patch,
  };
}

function ensureBatchDraft() {
  if (batch.draft) return batch.draft;
  batch.draft = { title: "", unload_after: false, schemes: [] };
  addBatchScheme();
  return batch.draft;
}

function addBatchScheme(cloneFrom = null) {
  const scheme = {
    id: `bs${Date.now().toString(36)}${Math.floor(Math.random() * 1e4)}`,
    workflow_id: cloneFrom?.workflow_id || "",
    detail: cloneFrom?.detail || null,
    loading: false,
    mapping: cloneFrom ? JSON.parse(JSON.stringify(cloneFrom.mapping)) : null,
    fixed: cloneFrom ? JSON.parse(JSON.stringify(cloneFrom.fixed)) : {},
    items: cloneFrom ? cloneFrom.items.map((item) => ({
      ...JSON.parse(JSON.stringify(item)),
      name: `${item.name} 副本`,
    })) : [],
  };
  batch.draft.schemes.push(scheme);
  if (!cloneFrom && scheme.workflow_id) void loadBatchWorkflowDetail(scheme);
  return scheme;
}

async function loadBatchWorkflowDetail(scheme, workflowId) {
  const id = workflowId ?? scheme.workflow_id;
  scheme.workflow_id = id;
  scheme.detail = null;
  scheme.mapping = null;
  scheme.fixed = {};
  if (!scheme.items.length) scheme.items = [newBatchItem(1)];
  batch.blockedMessage = "";
  renderBatch();
  if (!id) return;
  scheme.loading = true;
  renderBatch();
  try {
    const payload = await api(`/api/comfyui/workflows/${encodeURIComponent(id)}`);
    scheme.detail = payload.workflow || null;
    scheme.loading = false;
    const fingerprint = scheme.detail?.workflow_fingerprint || scheme.detail?.fingerprint || "";
    const recalled = recallBatchMapping(fingerprint);
    scheme.mapping = recalled || batchMappingFromWorkflow(scheme.detail);
    // 记忆里的槽位可能已不在工作流里（版本变了）：逐项验证，失效的按未映射处理。
    for (const key of ["prompt_slot", "duration_slot", "resolution_slot"]) {
      const slot = scheme.mapping[key];
      if (slot && !batchSlotByAddress(scheme.detail, slot)) scheme.mapping[key] = null;
    }
    scheme.mapping.media_ports = (scheme.mapping.media_ports || []).filter((port) =>
      batchMediaCandidates(scheme.detail).some((slot) =>
        String(slot.node_id) === String(port.node_id) && String(slot.field_name) === String(port.field_name)));
    renderBatch();
  } catch (error) {
    scheme.loading = false;
    batch.blockedMessage = `工作流读取失败：${error.message}`;
    renderBatch();
  }
}

// ---- 填单视图渲染 ----

function renderBatch() {
  const host = $("#comfyuiBatchBody");
  if (!host) return;
  const root = $("#comfyuiBatch");
  if (root) root.hidden = false;
  host.innerHTML = batch.view === "run" ? batchRunMarkup() : batchDraftMarkup();
  if (batch.view === "run") {
    startBatchRunElapsed();
    return;
  }
  // 转换条是条件渲染的：重绘后补模板与模型下拉（选中值存在 batch.convert 里）。
  if (batch.convert.provider) void batchLoadConvertModels();
  void batchLoadCustomTemplates();
}

function batchDraftMarkup() {
  const draft = ensureBatchDraft();
  const parts = [];
  parts.push(`<div class="cbm-batch-head">
    <span class="cbm-field">批次名 <input type="text" data-batch-field="title" maxlength="120" value="${escapeText(draft.title)}" placeholder="例：短剧第1集"></span>
    <label class="cbm-switch"><input type="checkbox" data-batch-field="unload_after" ${draft.unload_after ? "checked" : ""}><span class="cbm-track"></span>每条出完就释放模型</label>
    <button type="button" class="cbm-btn" data-batch-action="import-storyboard">${comfyui.storyboardQueue.length ? `从分镜队列导入（${comfyui.storyboardQueue.length} 条）` : "从分镜队列导入"}</button>
    <span class="cbm-field cbm-history">查看以前的批次
      <select data-batch-select="history"><option value="">选择批次…</option>${batch.histories.map((row) =>
        `<option value="${escapeText(row.id)}">${escapeText(row.title || row.id)} · ${row.done_count ?? 0}/${row.item_count ?? 0} 完成</option>`).join("")}</select>
    </span>
  </div>`);
  if (batch.blockedMessage) parts.push(`<p class="cbm-alert">${escapeText(batch.blockedMessage)}</p>`);
  const hasScript = draft.schemes.some((scheme) => (scheme.items || []).some((item) => String(item.script || "").trim()));
  if (hasScript) parts.push(batchConvertBarMarkup());
  draft.schemes.forEach((scheme, index) => parts.push(batchSchemeMarkup(scheme, index)));
  parts.push(`<button type="button" class="cbm-add-scheme" data-batch-action="add-scheme">＋ 添加方案（换一个工作流，或同一工作流换模型配方）</button>`);
  const totals = batchTotals(draft);
  parts.push(`<div class="cbm-submit-bar">
    <button type="button" class="cbm-btn primary" data-batch-action="submit" ${batch.missingItems.size ? "disabled" : ""}>开始批量生成</button>
    <span class="cbm-note">${totals.note}</span>
  </div>`);
  if (batch.missingItems.size) {
    parts.push(`<p class="cbm-alert">有 ${batch.missingItems.size} 条条目没填完（表里标红）：补齐或删掉再提交，其他条目不受影响。</p>`);
  }
  return parts.join("");
}

function batchTotals(draft) {
  let count = 0;
  for (const scheme of draft.schemes) {
    for (const item of scheme.items || []) {
      count += Math.max(1, Math.floor(Number(item.shots) || 1));
    }
  }
  const ready = draft.schemes.filter((scheme) => scheme.workflow_id && scheme.detail).length;
  return {
    count,
    note: count
      ? `共 ${count} 条（${draft.schemes.length} 个方案，其中 ${ready} 个已选好工作流），逐条排队执行，一条失败不影响其他条。`
      : "还没有条目：选好工作流后添加条目，或从分镜队列导入。",
  };
}

function batchConvertBarMarkup() {
  const profiles = state.bootstrap?.model_profiles || state.bootstrap?.providers || [];
  return `<div class="cbm-convert-bar">
    <span class="cbm-convert-title">剧本 → 提示词</span>
    <select data-batch-field="convertTemplate" aria-label="转换模板">
      <option value="">内置：按 MiniMax H3 格式改写</option>
      <optgroup label="提词库自定义模板" data-batch-custom-templates></optgroup>
    </select>
    <select data-batch-field="convertProvider" aria-label="转换用的模型供应商">
      <option value="">请选择供应商…</option>
      ${profiles.map((profile) => `<option value="${escapeText(String(profile?.model_key || ""))}"${String(profile?.model_key || "") === batch.convert.provider ? " selected" : ""}>${escapeText(String(profile?.name || profile?.model_key || ""))}</option>`).join("")}
    </select>
    <select data-batch-field="convertModel" aria-label="转换用的模型"><option>${batch.convert.provider ? "加载模型中…" : "请先选择供应商"}</option></select>
    <button type="button" class="cbm-btn" data-batch-action="convert-all">全部转换</button>
    <span class="cbm-convert-note">填了剧本、提示词空着的条目会逐条转换，写完可手改；转换走模型，可能产生费用。</span>
  </div>`;
}

// ---- 方案卡渲染 ----

function batchSchemeMarkup(scheme, schemeIndex) {
  const classes = ["cbm-scheme"];
  if (scheme.collapsed) classes.push("collapsed");
  const head = `<div class="cbm-scheme-head">
    <span class="cbm-chip">方案 ${schemeIndex + 1}</span>
    <select data-batch-select="workflow" data-scheme="${scheme.id}" aria-label="这个方案用哪份工作流">
      <option value="">选择工作流…</option>
      ${comfyui.workflows.map((row) => `<option value="${escapeText(row.id)}"${row.id === scheme.workflow_id ? " selected" : ""}>${escapeText(row.name || row.id)}</option>`).join("")}
    </select>
    ${scheme.loading ? '<span class="cbm-chip plain">正在读取参数…</span>' : ""}
    <span class="cbm-scheme-actions">
      <button type="button" class="cbm-btn tiny" data-batch-action="toggle-scheme" data-scheme="${scheme.id}">${scheme.collapsed ? "展开" : "收起"}</button>
      <button type="button" class="cbm-btn tiny" data-batch-action="copy-scheme" data-scheme="${scheme.id}">复制方案</button>
      <button type="button" class="cbm-btn tiny" data-batch-action="remove-scheme" data-scheme="${scheme.id}">删除</button>
    </span>
  </div>`;
  if (scheme.collapsed) {
    const summary = scheme.items.map((item) => escapeText(item.name)).join(" · ");
    return `<div class="${classes.join(" ")}">${head}<div class="cbm-collapsed-row">${scheme.items.length} 条条目：${summary || "还没有条目"}</div></div>`;
  }
  if (!scheme.workflow_id || !scheme.detail || !scheme.mapping) {
    return `<div class="${classes.join(" ")}">${head}<div class="cbm-scheme-body">
      <p class="cbm-hint">选工作流后在这里指认：正文 / 时长 / 分辨率写进哪个参数、哪些装载槽做素材口。</p>
    </div></div>`;
  }
  const body = `<div class="cbm-scheme-body">
    ${batchMappingAreaMarkup(scheme)}
    ${batchFixedMarkup(scheme)}
    ${batchItemsMarkup(scheme, schemeIndex)}
    <p class="cbm-hint">剧本填了、提示词空着的，点「转换」让模型写提示词，写完可手改。素材格留空＝这条不用这个口（必填的口留空会在提交时拦下）。没勾选的素材口不出现在条目里，生成时按工作流保存值执行。</p>
  </div>`;
  return `<div class="${classes.join(" ")}">${head}${body}</div>`;
}

// ---- 映射区：三个写入位下拉 + 装载槽（勾选 / 排序 / 每口可换节点）----

function batchMappingAreaMarkup(scheme) {
  const detail = scheme.detail;
  const mapping = scheme.mapping;
  const slots = detail?.params || [];
  const slotKeyOf = (slot) => (slot ? `${slot.node_id}|${slot.field_name}` : "");
  const savedValue = (slot) => {
    const value = batchSlotSavedValue(detail, slot);
    return value !== "" ? `当前保存值：${value}` : "当前保存值：空";
  };
  const pointSelect = (point, label, emptyLabel, candidates) => {
    const slot = mapping[`${point}_slot`];
    const key = slotKeyOf(slot);
    return `<div class="cbm-map-slot"><label>${label}</label>
      <select data-batch-select="mapping" data-scheme="${scheme.id}" data-point="${point}">
        <option value="">${emptyLabel}</option>
        ${candidates.map((row) => `<option value="${escapeText(slotKeyOf(row))}"${slotKeyOf(row) === key ? " selected" : ""}>${escapeText(batchSlotAddressText(detail, row))}</option>`).join("")}
      </select></div>`;
  };
  const textCandidates = slots.filter((slot) => slot.field_type === "text");
  const semantic = textCandidates.filter((slot) => slot.role === "positive_prompt" || slot.control === "multiline");
  const promptPool = semantic.length ? semantic : textCandidates;
  const rank = (slot) => (slot.role === "positive_prompt" ? 0 : slot.control === "multiline" ? 1 : 2);
  promptPool.sort((a, b) => rank(a) - rank(b));
  const numberSlots = slots.filter((slot) => slot.field_type === "number");
  const resolutionSlots = [...numberSlots, ...textCandidates.filter((slot) => (slot.candidates || []).length)];
  const durationSlot = batchSlotByAddress(detail, mapping.duration_slot);
  const resolutionSlot = batchSlotByAddress(detail, mapping.resolution_slot);
  const portRows = batchPortsMarkup(scheme);
  return `<div class="cbm-mapping">
    ${pointSelect("prompt", "剧本/提示词写进", "不注入正文（按工作流原配置出片）", promptPool)}
    ${pointSelect("duration", "时长写进", "不逐条改时长", numberSlots)}
    ${pointSelect("resolution", "分辨率写进", "不逐条改分辨率", resolutionSlots)}
    <p class="cbm-map-hint">先把「东西往哪放」指认好：没指认的参数按工作流保存值执行，不会出现在条目里。${durationSlot ? `时长 · ${savedValue(durationSlot)} · 写入 ${escapeText(batchSlotAddressText(detail, durationSlot))}` : "时长未映射：不会替你猜哪个参数控制时长"}；${resolutionSlot ? `分辨率 · ${savedValue(resolutionSlot)}` : "分辨率未映射"}。映射按这份工作流记住，下次打开还是这样。</p>
  </div>
  <p class="cbm-media-title">素材口：勾选启用，对应哪个节点用下拉换（列的是这份工作流里全部的图/音/视频口），↑↓ 定编号。</p>
  ${portRows}`;
}

function batchPortsMarkup(scheme) {
  const detail = scheme.detail;
  const candidates = batchMediaCandidates(detail);
  const ports = scheme.mapping.media_ports || [];
  const kindNames = { image: "图", video: "视频", audio: "音" };
  const kindFull = { image: "图片", video: "视频", audio: "音频" };
  const counts = { image: 0, video: 0, audio: 0 };
  const numbers = ports.map((port) => {
    if (!port.checked) return "—";
    const slot = batchSlotByAddress(detail, port);
    counts[slot?.media || "image"] = (counts[slot?.media || "image"] || 0) + 1;
    return `${kindNames[slot?.media || "image"]} ${counts[slot?.media || "image"]}`;
  });
  const checkedCount = ports.filter((port) => port.checked).length;
  const rows = ports.map((port, index) => {
    const slot = batchSlotByAddress(detail, port);
    const sameKind = candidates.filter((row) => row.media === (slot?.media || "image"));
    return `<div class="cbm-media-slot${port.checked ? "" : " unchecked"}">
      <input type="checkbox" data-batch-port-check="${index}" data-scheme="${scheme.id}" ${port.checked ? "checked" : ""} aria-label="启用这个素材口">
      <span class="cbm-no">${numbers[index]}</span>
      <select class="cbm-nodesel" data-batch-select="port-node" data-scheme="${scheme.id}" data-port="${index}" aria-label="这个素材口对应哪个节点">
        ${sameKind.map((row) => `<option value="${escapeText(slotKeyOf(row))}"${slot && slotKeyOf(row) === slotKeyOf(slot) ? " selected" : ""}>${escapeText(batchSlotAddressText(detail, row))}</option>`).join("")}
      </select>
      <span class="cbm-kw">${port.checked ? `${kindFull[slot?.media || "image"] || "媒体"} · ${slot?.media_optional === false ? "必填" : "可选，留空就跳过"}` : "未勾选 · 不出现在条目里"}</span>
      <button type="button" class="cbm-move" data-batch-action="port-up" data-scheme="${scheme.id}" data-port="${index}" aria-label="上移" ${index === 0 ? "disabled" : ""}>↑</button>
      <button type="button" class="cbm-move" data-batch-action="port-down" data-scheme="${scheme.id}" data-port="${index}" aria-label="下移" ${index === ports.length - 1 ? "disabled" : ""}>↓</button>
    </div>`;
  }).join("");
  return `<div class="cbm-media-slots">
      ${rows || '<p class="cbm-hint">这份工作流没有装载槽（LoadImage / LoadVideo / LoadAudio 等）。</p>'}
    </div>
    <p class="cbm-map-hint">已启用 ${checkedCount}/${ports.length} 个口；编号按这里的顺序算成 图1 / 图2 / 音1…，条目表按口出列。</p>`;
}

function slotKeyOf(slot) {
  return `${slot.node_id}|${slot.field_name}`;
}

// ---- 固定参数折叠区 ----

function batchFixedMarkup(scheme) {
  const detail = scheme.detail;
  const mapping = scheme.mapping;
  const skipKeys = new Set();
  for (const key of ["prompt_slot", "duration_slot", "resolution_slot"]) {
    const slot = batchSlotByAddress(detail, mapping?.[key]);
    if (slot) skipKeys.add(slotKeyOf(slot));
  }
  const editable = (detail?.params || []).filter((slot) =>
    slot.control !== "media" && !MEDIA_FIELD_TYPES.has(slot.field_type) && !skipKeys.has(slotKeyOf(slot)));
  if (!editable.length) return "";
  const rows = editable.map((slot) => {
    const key = slotKeyOf(slot);
    const value = scheme.fixed[key] ?? batchSlotSavedValue(detail, slot);
    let control = "";
    if (slot.field_type === "bool") {
      control = `<select data-batch-fixed="${escapeText(key)}" data-scheme="${scheme.id}">
        <option value="true"${String(value) === "true" ? " selected" : ""}>开</option>
        <option value="false"${String(value) !== "true" ? " selected" : ""}>关</option>
      </select>`;
    } else if ((slot.candidates || []).length) {
      control = `<select data-batch-fixed="${escapeText(key)}" data-scheme="${scheme.id}">
        ${(slot.candidates || []).map((candidate) => `<option value="${escapeText(String(candidate))}"${String(value) === String(candidate) ? " selected" : ""}>${escapeText(String(candidate))}</option>`).join("")}
      </select>`;
    } else {
      control = `<input type="${slot.field_type === "number" || slot.field_type === "seed" ? "number" : "text"}" step="any"
        data-batch-fixed="${escapeText(key)}" data-scheme="${scheme.id}" value="${escapeText(value)}">`;
    }
    const label = slot.label || `${slot.node_id} · ${slot.field_name}`;
    return `<div><label>${escapeText(label)}${slot.field_type === "seed" ? "（留空 = 每次随机）" : ""}</label>${control}</div>`;
  }).join("");
  const applied = Object.keys(scheme.fixed).length
    ? Object.keys(scheme.fixed).map((key) => {
        const slot = (detail?.params || []).find((row) => slotKeyOf(row) === key);
        const label = slot?.label || key;
        const value = scheme.fixed[key];
        return `<span class="cbm-kv">${escapeText(label)} ${escapeText(typeof value === "boolean" ? (value ? "开" : "关") : String(value))}</span>`;
      }).join("")
    : '<span>其余按工作流保存值</span>';
  return `<div class="cbm-fixed-bar">
      固定参数（本方案每条相同）：${applied}
      <button type="button" class="cbm-btn tiny" data-batch-action="toggle-fixed" data-scheme="${scheme.id}">展开固定参数</button>
    </div>
    <div class="cbm-fixed-detail${scheme.fixedOpen ? " open" : ""}">
      ${rows}
      <p class="cbm-map-hint">同一工作流想换模型配方，不用改这份方案——「复制方案」再来一份，这里改成另一套即可。</p>
    </div>`;
}

// ---- 条目表 ----

function batchItemsMarkup(scheme, schemeIndex) {
  const ports = (scheme.mapping?.media_ports || []).filter((port) => port.checked);
  const portHeaders = ports.map((port, portIndex) => {
    const slot = batchSlotByAddress(scheme.detail, port);
    const kind = slot?.media || "image";
    const counts = ports.slice(0, portIndex + 1).filter((row) =>
      (batchSlotByAddress(scheme.detail, row)?.media || "image") === kind).length;
    const kindNames = { image: "图", video: "视频", audio: "音" };
    return `<th>${kindNames[kind]} ${counts}（${escapeText(batchSlotAddressText(scheme.detail, slot))}）</th>`;
  }).join("");
  const rows = scheme.items.map((item, itemIndex) =>
    batchItemRowMarkup(scheme, schemeIndex, item, itemIndex, ports)).join("");
  const resolutionControl = (item) => {
    const slot = batchSlotByAddress(scheme.detail, scheme.mapping?.resolution_slot);
    if (slot && (slot.candidates || []).length) {
      return `<select data-batch-item="resolution" data-scheme="${scheme.id}" data-item="${itemIndex}">
        <option value="">按工作流</option>
        ${slot.candidates.map((candidate) => `<option value="${escapeText(String(candidate))}"${String(item.resolution) === String(candidate) ? " selected" : ""}>${escapeText(String(candidate))}</option>`).join("")}
      </select>`;
    }
    return `<input type="${slot?.field_type === "number" ? "number" : "text"}" step="any" placeholder="按工作流"
      data-batch-item="resolution" data-scheme="${scheme.id}" data-item="${itemIndex}" value="${escapeText(item.resolution)}">`;
  };
  return `<div class="cbm-items-wrap"><table class="cbm-items">
    <thead><tr><th>名称</th>${portHeaders}<th>剧本</th><th>提示词</th><th>时长</th><th>分辨率</th><th>条数</th><th>操作</th></tr></thead>
    <tbody>${rows || `<tr><td colspan="${8 + ports.length}" class="cbm-hint">还没有条目。</td></tr>`}</tbody>
    <tfoot><tr><td colspan="${8 + ports.length}"><button type="button" class="cbm-add-item" data-batch-action="add-item" data-scheme="${scheme.id}">＋ 添加一条</button></td></tr></tfoot>
  </table></div>`;
}

function batchItemRowMarkup(scheme, schemeIndex, item, itemIndex, ports) {
  const missing = batch.missingItems.has(`${scheme.id}:${itemIndex}`);
  const converting = batch.converting.has(`${scheme.id}:${itemIndex}`);
  const tiles = ports.map((port, portIndex) => {
    const slot = batchSlotByAddress(scheme.detail, port);
    const kind = slot?.media || "image";
    const record = item.files[String(portIndex)];
    if (record) {
      const thumb = kind === "image"
        ? `<img src="${escapeText(attachmentThumbUrl(record))}" alt="" loading="lazy">`
        : `<span class="cbm-tile-ico">${kind === "video" ? "▶" : "♪"}</span>`;
      return `<td><span class="cbm-mtile" data-batch-tile data-scheme="${scheme.id}" data-item="${itemIndex}" data-port="${portIndex}" data-kind="${kind}" title="点选 / 拖拽可换">
        <span class="cbm-thumb ${kind}">${thumb}</span>
        <span class="cbm-mname">${escapeText(record.name || "")}<button type="button" class="cbm-tile-clear" data-batch-action="clear-file" data-scheme="${scheme.id}" data-item="${itemIndex}" data-port="${portIndex}" aria-label="清除这个文件">✕</button></span>
      </span></td>`;
    }
    const required = slot?.media_optional === false;
    return `<td><span class="cbm-empty-slot${required && missing ? " is-missing" : ""}" data-batch-tile data-scheme="${scheme.id}" data-item="${itemIndex}" data-port="${portIndex}" data-kind="${kind}">${required ? "必填 · 点选/拖入" : "留空 · 这条跳过"}</span></td>`;
  }).join("");
  const promptCell = `<td>
    <textarea class="cbm-script" rows="3" placeholder="最终交给工作流的提示词" data-batch-item="prompt" data-scheme="${scheme.id}" data-item="${itemIndex}">${escapeText(item.prompt)}</textarea>
    ${item.script && converting ? '<button type="button" class="cbm-convert" disabled>转换中…</button>' : ""}
  </td>`;
  const durationControl = `<input type="number" min="1" step="any" style="width:76px" placeholder="按工作流"
    data-batch-item="duration" data-scheme="${scheme.id}" data-item="${itemIndex}" value="${escapeText(item.duration)}">`;
  return `<tr${missing ? ' class="is-missing"' : ""}>
    <td><input type="text" class="cbm-name" maxlength="120" data-batch-item="name" data-scheme="${scheme.id}" data-item="${itemIndex}" value="${escapeText(item.name)}"></td>
    ${tiles}
    <td><textarea class="cbm-script" rows="3" placeholder="这场的内容要求（可转换成提示词）" data-batch-item="script" data-scheme="${scheme.id}" data-item="${itemIndex}">${escapeText(item.script)}</textarea></td>
    ${promptCell}
    <td>${durationControl}</td>
    <td>${resolutionControl(item)}</td>
    <td><input type="number" min="1" max="200" style="width:56px" data-batch-item="shots" data-scheme="${scheme.id}" data-item="${itemIndex}" value="${escapeText(String(item.shots))}"></td>
    <td class="cbm-row-act">
      <button type="button" class="cbm-btn tiny" data-batch-action="convert-item" data-scheme="${scheme.id}" data-item="${itemIndex}" ${item.script && !item.prompt && !converting ? "" : "disabled"}>转换</button>
      <button type="button" class="cbm-btn tiny" data-batch-action="copy-item" data-scheme="${scheme.id}" data-item="${itemIndex}">复制</button>
      <button type="button" class="cbm-btn tiny" data-batch-action="remove-item" data-scheme="${scheme.id}" data-item="${itemIndex}">删</button>
    </td>
  </tr>`;
}

// ---- 批量交互（容器级委托）----

function batchSchemeOf(id) {
  return (batch.draft?.schemes || []).find((scheme) => scheme.id === id) || null;
}

function onBatchClick(event) {
  const target = event.target.closest("[data-batch-action]");
  const tile = event.target.closest("[data-batch-tile]");
  if (!target && tile) {
    // 素材格点选：唤起系统选择器（tile 内动态建 file input）。
    event.preventDefault();
    batchPickFile(tile);
    return;
  }
  if (!target) {
    if (event.target.closest("[data-batch-tile-clear]")) return;
    return;
  }
  const action = target.dataset.batchAction;
  const scheme = batchSchemeOf(target.dataset.scheme || "");
  const itemIndex = target.dataset.item !== undefined ? Number(target.dataset.item) : -1;
  event.preventDefault();
  if (action === "add-scheme") {
    addBatchScheme();
    renderBatch();
  } else if (action === "copy-scheme" && scheme) {
    addBatchScheme(scheme);
    renderBatch();
  } else if (action === "remove-scheme" && scheme) {
    if ((batch.draft?.schemes.length || 0) <= 1) { toast("至少保留一个方案"); return; }
    batch.draft.schemes = batch.draft.schemes.filter((row) => row.id !== scheme.id);
    renderBatch();
  } else if (action === "toggle-scheme" && scheme) {
    scheme.collapsed = !scheme.collapsed;
    renderBatch();
  } else if (action === "toggle-fixed" && scheme) {
    scheme.fixedOpen = !scheme.fixedOpen;
    renderBatch();
  } else if (action === "add-item" && scheme) {
    scheme.items.push(newBatchItem(scheme.items.length + 1));
    renderBatch();
  } else if (action === "copy-item" && scheme && itemIndex >= 0) {
    const copy = JSON.parse(JSON.stringify(scheme.items[itemIndex]));
    copy.name = `${copy.name} 副本`;
    scheme.items.splice(itemIndex + 1, 0, copy);
    renderBatch();
  } else if (action === "remove-item" && scheme && itemIndex >= 0) {
    scheme.items.splice(itemIndex, 1);
    batch.recheckMissing();
    renderBatch();
  } else if (action === "clear-file" && scheme && itemIndex >= 0) {
    event.stopPropagation();
    const item = scheme.items[itemIndex];
    delete item.files[String(target.dataset.port)];
    renderBatch();
  } else if (action === "import-storyboard") {
    importBatchStoryboard();
  } else if (action === "convert-item" && scheme && itemIndex >= 0) {
    void batchConvert(scheme, itemIndex);
  } else if (action === "convert-all") {
    void batchConvertAll();
  } else if (action === "submit") {
    void submitBatch();
  } else if (action === "back-to-draft") {
    batch.view = "draft";
    stopBatchPolling();
    renderBatch();
  } else if (action === "switch-draft") {
    // 跑批视图「回填单改下一批」：草稿保留，直接回去改。
    batch.view = "draft";
    stopBatchPolling();
    renderBatch();
  } else if (action === "open-task-outputs" && target.dataset.taskId) {
    void openBatchTaskOutputs(String(target.dataset.taskId));
  } else if (action === "retry-item" && target.dataset.taskId) {
    void retryBatchItem(String(target.dataset.taskId));
  }
}

function onBatchChange(event) {
  const target = event.target;
  const scheme = batchSchemeOf(target.dataset.scheme || "");
  if (target.matches('[data-batch-field="title"]')) {
    ensureBatchDraft().title = String(target.value || "");
    return;
  }
  if (target.matches('[data-batch-field="unload_after"]')) {
    ensureBatchDraft().unload_after = target.checked;
    return;
  }
  if (target.matches('[data-batch-field="convertTemplate"]')) {
    batch.convert.template = String(target.value || "");
    return;
  }
  if (target.matches('[data-batch-field="convertProvider"]')) {
    batch.convert.provider = String(target.value || "");
    void batchLoadConvertModels();
    return;
  }
  if (target.matches('[data-batch-field="convertModel"]')) {
    batch.convert.model = String(target.value || "");
    return;
  }
  if (target.matches('[data-batch-select="history"]')) {
    const id = String(target.value || "");
    if (id) { batch.runId = id; batch.runData = null; batch.view = "run"; startBatchPolling(); }
    return;
  }
  if (!scheme) return;
  if (target.matches('[data-batch-select="workflow"]')) {
    void loadBatchWorkflowDetail(scheme, String(target.value || ""));
    return;
  }
  if (target.matches('[data-batch-select="mapping"]')) {
    const point = String(target.dataset.point || "");
    const value = String(target.value || "");
    const [nodeId, ...rest] = value.split("|");
    scheme.mapping[`${point}_slot`] = value ? { node_id: nodeId, field_name: rest.join("|") } : null;
    batchRememberMappingQuietly(scheme);
    renderBatch();
    return;
  }
  if (target.matches('[data-batch-select="port-node"]')) {
    const portIndex = Number(target.dataset.port);
    const value = String(target.value || "");
    const [nodeId, ...rest] = value.split("|");
    const port = (scheme.mapping.media_ports || [])[portIndex];
    if (port && value) {
      port.node_id = nodeId;
      port.field_name = rest.join("|");
      // 换节点后条目里已放的文件还在旧节点名下：清掉这些口的文件，明明白白。
      const ordinal = batchPortOrdinal(scheme, portIndex);
      for (const item of scheme.items) delete item.files[String(ordinal)];
      batchRememberMappingQuietly(scheme);
renderBatch();
    }
    return;
  }
  const checkbox = target.matches("[data-batch-port-check]");
  if (checkbox) {
    const portIndex = Number(target.dataset.batchPortCheck);
    const port = (scheme.mapping.media_ports || [])[portIndex];
    if (port) {
      port.checked = target.checked;
      if (!target.checked) {
        const ordinal = batchPortOrdinal(scheme, portIndex);
        for (const item of scheme.items) delete item.files[String(ordinal)];
      }
      batchRememberMappingQuietly(scheme);
      renderBatch();
    }
  }
}

function onBatchInput(event) {
  const target = event.target;
  const scheme = batchSchemeOf(target.dataset.scheme || "");
  const itemField = target.dataset.batchItem;
  if (!scheme || !itemField) return;
  const itemIndex = Number(target.dataset.item);
  const item = scheme.items[itemIndex];
  if (!item) return;
  if (itemField === "shots") item.shots = Math.max(1, Math.floor(Number(target.value) || 1));
  else item[itemField] = String(target.value ?? "");
}

// 素材口在条目文件字典里的键 = 它在已勾选口里的序号（0 起）。
function batchPortOrdinal(scheme, portIndex) {
  const ports = scheme.mapping?.media_ports || [];
  let ordinal = 0;
  for (let index = 0; index < ports.length && index < portIndex; index += 1) {
    if (ports[index].checked) ordinal += 1;
  }
  return ports[portIndex]?.checked ? ordinal : -1;
}

function batchRememberMappingQuietly(scheme) {
  const fingerprint = scheme.detail?.workflow_fingerprint || scheme.detail?.fingerprint || "";
  rememberBatchMapping(fingerprint, scheme.mapping);
}

// ---- 批量素材上传（复用单次生成的受管上传链路）----

function batchPickFile(tile) {
  const scheme = batchSchemeOf(tile.dataset.scheme || "");
  if (!scheme) return;
  const portIndex = Number(tile.dataset.port);
  const slot = batchSlotByAddress(scheme.detail, (scheme.mapping?.media_ports || [])[portIndex]);
  const kind = tile.dataset.kind || slot?.media || "image";
  const input = document.createElement("input");
  input.type = "file";
  input.accept = MEDIA_ACCEPT[kind] || "";
  input.addEventListener("change", () => {
    const file = input.files?.[0];
    if (file) void batchUploadTiles([{ tile, file }]);
  });
  input.click();
}

function onBatchDragOver(event) {
  const tile = event.target.closest?.("[data-batch-tile]");
  if (!tile || !dragHasFiles(event)) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
  tile.classList.add("dragover");
}

function onBatchDragLeave(event) {
  const tile = event.target.closest?.("[data-batch-tile]");
  if (!tile || (event.relatedTarget && tile.contains(event.relatedTarget))) return;
  tile.classList.remove("dragover");
}

function onBatchDrop(event) {
  const tile = event.target.closest?.("[data-batch-tile]");
  if (!tile || !dragHasFiles(event)) return;
  event.preventDefault();
  event.stopPropagation();
  tile.classList.remove("dragover");
  const scheme = batchSchemeOf(tile.dataset.scheme || "");
  if (!scheme) return;
  const files = Array.from(event.dataTransfer?.files || []);
  if (!files.length) return;
  if (dropHasDirectory(event)) { toast("媒体输入只收单个文件，不支持拖文件夹"); return; }
  const kind = tile.dataset.kind || "image";
  const bad = files.find((file) => !fileMatchesMediaKind(file, kind));
  if (bad) { toastMediaKindMismatch(bad, kind); return; }
  if (files.length > 1) toast(`一次拖了 ${files.length} 个文件，只有第一个进入这个素材格`);
  void batchUploadTiles([{ tile, file: files[0] }]);
}

async function batchUploadTiles(pairs) {
  if (comfyui.uploadingSlot) { toast("上一个文件还在上传，请稍候"); return; }
  comfyui.uploadingSlot = "batch";
  let saved = 0;
  try {
    for (const { tile, file } of pairs) {
      const scheme = batchSchemeOf(tile.dataset.scheme || "");
      const itemIndex = Number(tile.dataset.item);
      const item = scheme?.items[itemIndex];
      if (!scheme || !item) continue;
      const form = new FormData();
      form.append("file", file, file.name);
      try {
        const payload = await api("/api/comfyui/media", { method: "POST", body: form });
        const ordinal = batchPortOrdinal(scheme, Number(tile.dataset.port));
        if (ordinal >= 0) {
          item.files[String(ordinal)] = {
            path: String(payload.path || ""),
            name: String(payload.name || file.name),
            thumb_path: String(payload.thumb_path || ""),
          };
          saved += 1;
        }
      } catch (error) {
        toast(`上传失败：${error.message}`);
      }
    }
  } finally {
    comfyui.uploadingSlot = "";
    batch.recheckMissing();
    renderBatch();
  }
  if (saved) toast(`已上传 ${saved} 个文件`);
}

// ---- 剧本转换（POST /api/studio/generate + 轮询既有任务）----

const BATCH_CONVERT_INTRO = "你是视频提示词工程师。把下面的剧本改写成一支视频的生成提示词：写成一段连贯的英文或中文描述，覆盖画面主体、动作节奏、镜头运动、氛围与声音要点；不要输出解释、标题或分点列表，只输出提示词正文。\n\n剧本：\n";

async function batchLoadConvertModels() {
  const select = document.querySelector('[data-batch-field="convertModel"]');
  if (!select) return;
  const providerKey = batch.convert.provider;
  if (!providerKey) {
    select.replaceChildren(new Option("请先选择供应商", ""));
    select.disabled = true;
    return;
  }
  if (!(state.providerModelCatalogs[providerKey] || []).length) {
    try {
      const result = await api("/api/providers/models", { method: "POST", body: { model_key: providerKey } });
      if (result?.error) throw new Error(result.error);
      state.providerModelCatalogs[providerKey] = Array.isArray(result?.models) ? result.models : [];
    } catch (_) {
      state.providerModelCatalogs[providerKey] = [];
    }
  }
  const models = state.providerModelCatalogs[providerKey] || [];
  select.replaceChildren(new Option("请选择模型…", ""), ...models.map((entry) => {
    const id = typeof entry === "object" ? (entry.id ?? entry.model ?? entry.name) : entry;
    return new Option(entry?.name || id, id);
  }));
  select.disabled = !models.length;
}

async function batchLoadCustomTemplates() {
  const group = document.querySelector('[data-batch-custom-templates]');
  if (!group || group.children.length) return;
  try {
    const { items } = await api("/api/studio/custom");
    group.replaceChildren(...(items || []).filter((item) => item?.id)
      .map((item) => new Option(String(item.title || item.id), String(item.id))));
  } catch (_) { /* 读取失败仍可用内置转换 */ }
}

function batchItemReference(scheme, itemIndex) {
  return `${scheme.id}:${itemIndex}`;
}

async function batchConvert(scheme, itemIndex, silent = false) {
  const item = scheme.items[itemIndex];
  const script = String(item?.script || "").trim();
  if (!item || !script || String(item.prompt || "").trim()) return true;
  const key = batchItemReference(scheme, itemIndex);
  if (batch.converting.has(key)) return true;
  if (!batch.convert.provider || !batch.convert.model) {
    if (!silent) toast("转换前先在「剧本 → 提示词」一栏选好供应商和模型");
    return false;
  }
  batch.converting.add(key);
  renderBatch();
  try {
    const customId = batch.convert.template;
    // studio 生成契约：custom_id 有值 → user_request 填进提词库模板；没有 →
    // user_request 本身就是全文（内置引导 + 剧本）。
    const body = {
      model_key: batch.convert.provider,
      model_name: batch.convert.model,
      user_request: customId ? script : `${BATCH_CONVERT_INTRO}${script}`,
    };
    if (customId) body.custom_id = customId;
    const created = await api("/api/studio/generate", { method: "POST", body });
    const taskId = String(created?.task?.id || "");
    if (!taskId) throw new Error(created?.error || "转换任务创建失败");
    const result = await batchWaitStudioTask(taskId);
    item.prompt = String(result || "").trim();
    return true;
  } catch (error) {
    batch.blockedMessage = "";
    toast(`这条转换失败：${error.message}`);
    return false;
  } finally {
    batch.converting.delete(key);
    renderBatch();
  }
}

async function batchConvertAll() {
  const draft = ensureBatchDraft();
  const targets = [];
  draft.schemes.forEach((scheme) => {
    (scheme.items || []).forEach((item, itemIndex) => {
      if (String(item.script || "").trim() && !String(item.prompt || "").trim()
          && !batch.converting.has(batchItemReference(scheme, itemIndex))) {
        targets.push([scheme, itemIndex]);
      }
    });
  });
  if (!targets.length) { toast("没有要转换的条目：先填剧本，或已全部转换过"); return; }
  if (!batch.convert.provider || !batch.convert.model) { toast("先在「剧本 → 提示词」一栏选好供应商和模型"); return; }
  batch.blockedMessage = "";
  for (const [scheme, itemIndex] of targets) {
    const ok = await batchConvert(scheme, itemIndex, true);
    if (!ok) break;
  }
  toast("转换完成；提示词可以逐条手改");
}

async function batchWaitStudioTask(taskId) {
  for (let attempt = 0; attempt < 600; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    const payload = await api(`/api/studio/tasks/${encodeURIComponent(taskId)}`);
    const task = payload?.task || payload;
    const status = String(task?.status || "");
    if (status === "done") return String(task?.result ?? "");
    if (["failed", "cancelled"].includes(status)) {
      throw new Error(String(task?.error || "转换任务没有完成"));
    }
  }
  throw new Error("转换超时，可稍后重试");
}

// ---- 自检与提交 ----

batch.recheckMissing = function recheckMissing() {
  batch.missingItems.clear();
  const draft = batch.draft;
  if (!draft) return;
  draft.schemes.forEach((scheme) => {
    if (!scheme.detail || !scheme.mapping) return;
    const ports = (scheme.mapping.media_ports || []).filter((port) => port.checked);
    (scheme.items || []).forEach((item, itemIndex) => {
      for (let portIndex = 0; portIndex < ports.length; portIndex += 1) {
        const slot = batchSlotByAddress(scheme.detail, ports[portIndex]);
        if (slot?.media_optional !== false) continue;
        const ordinal = batchPortOrdinal(scheme, portIndex);
        if (!item.files[String(ordinal)]?.path) {
          batch.missingItems.add(`${scheme.id}:${itemIndex}`);
          return;
        }
      }
    });
  });
};

async function submitBatch() {
  batch.recheckMissing();
  const draft = ensureBatchDraft();
  if (batch.missingItems.size) {
    batch.blockedMessage = "";
    renderBatch();
    toast("有标红的条目还没填完必填素材，补齐或删掉再提交");
    return;
  }
  const schemes = [];
  for (const scheme of draft.schemes) {
    if (!scheme.workflow_id || !scheme.detail || !scheme.mapping) continue;
    const detail = scheme.detail;
    const savedOf = (slot) => batchSlotSavedValue(detail, slot);
    const fixedOverrides = [];
    for (const [key, raw] of Object.entries(scheme.fixed || {})) {
      const [nodeId, ...rest] = key.split("|");
      const slot = batchSlotByAddress(detail, { node_id: nodeId, field_name: rest.join("|") });
      if (!slot) continue;
      if (slot.field_type === "bool") {
        const value = String(raw) === "true";
        if (typeof savedOf(slot) === "boolean" && savedOf(slot) === value) continue;
        fixedOverrides.push({ node_id: nodeId, field_name: slot.field_name, value });
      } else if (slot.field_type === "number" || slot.field_type === "seed") {
        const value = Number(raw);
        if (!Number.isFinite(value) || value === savedOf(slot)) continue;
        fixedOverrides.push({ node_id: nodeId, field_name: slot.field_name, value: slot.field_type === "seed" || Number.isInteger(value) ? Math.round(value) : value });
      } else {
        const value = String(raw);
        if (value === savedOf(slot)) continue;
        fixedOverrides.push({ node_id: nodeId, field_name: slot.field_name, value });
      }
    }
    const ports = (scheme.mapping.media_ports || []).filter((port) => port.checked);
    const items = scheme.items.map((item) => {
      const mediaInputs = [];
      ports.forEach((port, portIndex) => {
        const record = item.files[String(portIndex)];
        if (!record?.path) return;
        mediaInputs.push({ node_id: String(port.node_id), field_name: String(port.field_name), path: record.path, media: batchSlotByAddress(detail, port)?.media || "image" });
      });
      const duration = String(item.duration ?? "").trim();
      const resolution = String(item.resolution ?? "").trim();
      return {
        name: String(item.name || "").trim() || `条目 ${scheme.items.indexOf(item) + 1}`,
        prompt: String(item.prompt || "").trim(),
        duration_seconds: duration ? Number(duration) : null,
        resolution,
        shots: Math.max(1, Math.floor(Number(item.shots) || 1)),
        media_inputs: mediaInputs,
      };
    });
    schemes.push({
      workflow_id: scheme.workflow_id,
      overrides: fixedOverrides,
      mapping: {
        prompt_slot: scheme.mapping.prompt_slot,
        duration_slot: scheme.mapping.duration_slot,
        resolution_slot: scheme.mapping.resolution_slot,
      },
      items,
    });
  }
  if (!schemes.length) { toast("还没有可选的方案：每个方案要先选好工作流"); return; }
  try {
    const payload = await api("/api/comfyui/batches", {
      method: "POST",
      body: { title: draft.title, unload_after: draft.unload_after, schemes },
    });
    batch.runId = String(payload.batch?.id || "");
    batch.runData = payload.batch || null;
    batch.runStartedAt = Date.now();
    batch.missingItems.clear();
    batch.view = "run";
    renderBatch();
    startBatchPolling();
    toast("批次已提交，正在逐条执行");
  } catch (error) {
    toast(`提交失败：${error.message}`);
  }
}

// ---- 分镜队列导入 ----

function importBatchStoryboard() {
  const items = (comfyui.storyboardQueue || []).filter((item) => item.prompt);
  if (!items.length) { toast("分镜队列是空的：从拆书匠发一份「分镜生图」过来"); return; }
  const scheme = ensureBatchDraft().schemes[0];
  if (!scheme.detail || !scheme.mapping) { toast("先给方案 1 选好工作流再导入分镜"); return; }
  const start = scheme.items.length;
  items.forEach((row, index) => {
    scheme.items.push(newBatchItem(start + index + 1, {
      name: row.head ? String(row.head).slice(0, 120) : `第 ${row.no} 镜`,
      script: "",
      prompt: String(row.prompt || ""),
    }));
  });
  renderBatch();
  toast(`已把 ${items.length} 条分镜提示词导进方案 ${1} 的条目表`);
}

// ---- 跑批视图 ----

const BATCH_STATUS_BADGES = {
  queued: ["排队中", "queue"],
  running: ["生成中", "run"],
  completed: ["已完成", "done"],
  failed: ["失败", "fail"],
  cancelled: ["已取消", "fail"],
  interrupted: ["已中断", "fail"],
  missing: ["任务缺失", "fail"],
};

function batchRunMarkup() {
  const data = batch.runData;
  if (!data) return '<div class="cbm-run-head"><span class="cbm-run-title">正在读取批次…</span></div>';
  const items = data.items || [];
  const done = items.filter((item) => item.status === "completed").length;
  const total = Math.max(items.length, 1);
  const active = items.some((item) => ["queued", "running"].includes(String(item.status)));
  const percent = Math.round((done / total) * 100);
  const cards = items.map((item) => batchRunCardMarkup(item)).join("");
  return `<div class="cbm-run-head">
      <span class="cbm-run-title">${escapeText(data.title || "未命名批次")}</span>
      <span class="cbm-badge ${active ? "run" : "done"}">${active ? `正在生成 ${done}/${items.length}` : done === items.length ? `${items.length} 条全部完成` : `${done}/${items.length} 完成`}</span>
      <div class="cbm-progress" role="progressbar" aria-label="批次进度" aria-valuenow="${percent}" aria-valuemin="0" aria-valuemax="100"><div style="width:${percent}%"></div></div>
      <span class="cbm-run-meta" id="comfyuiBatchElapsed"></span>
      <button type="button" class="cbm-btn tiny" data-batch-action="switch-draft">回填单改下一批</button>
    </div>
    <div class="cbm-cards">${cards}</div>
    <p class="cbm-run-note">失败的条目点「重试」＝从没完成的段继续，不会重复提交已完成的段；其他条目照常跑。</p>`;
}

function batchRunCardMarkup(item) {
  const [label, cls] = BATCH_STATUS_BADGES[String(item.status || "")] || [String(item.status || "—"), "queue"];
  const outputs = item.outputs || [];
  const first = outputs[0] || "";
  const taskRef = String(item.task_id || "");
  const kind = mediaKind(first, first) === "video" ? "video" : "image";
  let thumb;
  if (item.status === "completed" && first) {
    const url = outputFileUrl(taskRef, first, "");
    thumb = kind === "image"
      ? `<img src="${escapeText(url)}" alt="" loading="lazy" data-large-url="${escapeText(url)}">`
      : '<span class="cbm-card-play">▶</span>';
  } else if (String(item.status) === "running") {
    thumb = `<span class="cbm-card-skel-text">第 ${(Number(item.done) || 0) + 1}/${item.total || 1} 段生成中</span>`;
  } else {
    thumb = `<span class="cbm-card-skel-text">${item.status === "queued" ? "等前面的条目出完" : "—"}</span>`;
  }
  const foot = [];
  if (item.status === "completed") {
    foot.push(`<span>${outputs.length > 1 ? `${outputs.length} 个产物` : "已完成"}</span>`);
    foot.push(`<button type="button" class="cbm-btn tiny" data-batch-action="open-task-outputs" data-task-id="${escapeText(taskRef)}">产物</button>`);
  } else if (item.error) {
    foot.push(`<span class="cbm-err">${escapeText(String(item.error))}</span>`);
    foot.push('<button type="button" class="cbm-btn tiny" data-batch-action="retry-item" data-task-id="' + escapeText(taskRef) + '">重试</button>');
  } else if (item.status === "running" || item.status === "queued") {
    foot.push(`<span>${item.done || 0}/${item.total || 1} 段</span>`);
  }
  return `<div class="cbm-card">
    <div class="cbm-card-head"><strong>${escapeText(item.name || `条目 ${item.index}`)}</strong><span class="cbm-badge ${cls}">${label}</span></div>
    <div class="cbm-card-thumb${item.status === "completed" && first && kind === "image" ? "" : " skel"}">${thumb}</div>
    <div class="cbm-card-foot">${foot.join("")}</div>
  </div>`;
}

function startBatchRunElapsed() {
  if (batch.runElapsedTimer) return;
  const update = () => {
    const host = $("#comfyuiBatchElapsed");
    if (!host) return;
    const seconds = Math.max(0, Math.round((Date.now() - batch.runStartedAt) / 1000));
    host.textContent = `已用 ${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  };
  update();
  batch.runElapsedTimer = setInterval(update, 1000);
}

function startBatchPolling() {
  stopBatchPolling();
  const tick = async () => {
    if (!batch.runId || batch.view !== "run") return;
    try {
      const payload = await api(`/api/comfyui/batches/${encodeURIComponent(batch.runId)}`);
      batch.runData = payload.batch || batch.runData;
      renderBatch();
      const items = batch.runData?.items || [];
      if (!items.some((item) => ["queued", "running"].includes(String(item.status)))) {
        stopBatchPolling();
        void loadBatchHistories();
      }
    } catch (_) { /* 单次轮询失败不终止跑批监视 */ }
  };
  batch.runTimer = setInterval(() => void tick(), 2200);
  void tick();
}

function stopBatchPolling() {
  if (batch.runTimer) { clearInterval(batch.runTimer); batch.runTimer = null; }
  if (batch.runElapsedTimer) { clearInterval(batch.runElapsedTimer); batch.runElapsedTimer = null; }
}

async function loadBatchHistories() {
  try {
    const payload = await api("/api/comfyui/batches");
    batch.histories = (payload.batches || []).slice(0, 50);
    batch.historiesLoaded = true;
    if (batch.view === "draft") renderBatch();
  } catch (_) { /* 历史读不到不挡填单 */ }
}

async function openBatchTaskOutputs(taskId) {
  setComfyuiPane("tasks");
  comfyui.activeTaskId = taskId;
  comfyui.eventSeq = 0;
  comfyui.tasks = [];
  await refreshTasks();
  startPolling();
}

async function retryBatchItem(taskId) {
  try {
    await api(`/api/comfyui/tasks/${encodeURIComponent(taskId)}/retry`, { method: "POST", body: {} });
    toast("已重新排队：从没完成的段继续");
    batch.runData = null;
    if (batch.view === "run" && batch.runId) startBatchPolling();
  } catch (error) {
    toast(`重试失败：${error.message}`);
  }
}

function enterBatchPane() {
  ensureBatchDraft();
  void loadBatchHistories();
  void batchLoadCustomTemplates();
  batch.recheckMissing();
  renderBatch();
}
