// ============================================================
// 24-comfyui.js —— 造梦机（工作台第三个功能页）
// 只 import 01-core / 03-media / 22-studio；轮询经 onStudioPageChange 暂停/恢复
// （离开页面就停，回来由服务端状态校准，与打标器同一套约定）。
// 与聊天域零耦合：只调 /api/comfyui/*。
// ============================================================

import { $, api, localFileUrl, state, toast } from "./01-core.js";
import { attachmentThumbUrl, collectMediaPreviewItems, isPywebview, mediaKind, saveLocalFile } from "./03-media.js";
import { currentStudioPage, isStudioOpen, onStudioPageChange, setStudioPage } from "./22-studio.js";
import { BATCH_H3_BASE_TEMPLATE, BATCH_H3_REFERENCE_TEMPLATE, buildBatchConvertRequest } from "./24a-comfyui-convert.js";
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
  mediaSkipped: new Set(),   // 手动 ⊘ 跳过的媒体槽 key；独立于 mediaSelection，切工作流清空
  rerun: null,               // 完成态任务再次生成草稿 {sourceTaskId, workflowId, unavailable...}
  rerunLoading: "",          // 正在读取快照的任务 id（防重复点）
  drafts: new Map(),       // 主表单用户已填的值 {工作流id|node_id|field_name: value}；重绘与刷新都保留，点「默认」才清
  uploadingSlot: "",       // 正在上传的媒体槽 key（防并发点选；批量拖拽时是各 key 拼接，仍为真值即锁住）
  mediaGroupsCollapsed: {}, // 媒体分组卡折叠状态 {kind: true}；只存内存，不落盘、不进 visible_params
  storyboardQueue: [],    // 拆书匠「分镜生图」发来的提示词；只存内存，刷新即清
  resultsSegment: "single", // 结果页两档：single = 单次任务+产物；batch = 批次列表
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
    return "提示词按任务输入处理，不写进副本；请在主表单或批量条目里填";
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

function escapeAttribute(value) {
  return escapeText(value).replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function openSaveAsDialog() {
  if (!comfyui.detail || workflowSaveBusy) return;
  if (comfyui.rerun) {
    toast("再次生成使用原任务快照，不新增或修改工作流库条目");
    return;
  }
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
  if (comfyui.rerun) {
    toast("再次生成使用原任务快照，不新增或修改工作流库条目");
    return;
  }
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
    toast(`已另存副本（写入 ${overrides.length} 项改动${skipped}），已切到副本；没有开始执行。`);
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
  // 结果页两档切换 + 批次列表（点一批进跑批视图）。
  $("#comfyuiResultsSeg")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-comfyui-results-seg]");
    if (btn) setComfyuiResultsSegment(btn.dataset.comfyuiResultsSeg);
  });
  $("#comfyuiBatchList")?.addEventListener("click", (event) => {
    const edit = event.target.closest("[data-batch-edit]");
    if (edit) { void openBatchForEdit(String(edit.dataset.batchEdit || "")); return; }
    const row = event.target.closest("[data-batch-open]");
    if (row) openBatchHistory(String(row.dataset.batchOpen || ""));
  });
  for (const type of ["input", "change"]) $("#comfyuiParamForm")?.addEventListener(type, () => updateDraftHint());
  if (comfyui.unsubscribePage) comfyui.unsubscribePage();
  comfyui.unsubscribePage = onStudioPageChange((page, detail = {}) => {
    if (page === "comfyui" && !detail.closed && isStudioOpen()) {
      void enterPage();
    } else {
      // 离开造梦机页：再次生成草稿不跨页面保留。
      if (comfyui.rerun) dropRerunDraft();
      stopPolling();
      stopBackgroundRefresh();
      stopBatchPolling();
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
    if (event.target.closest("[data-comfyui-rerun-cancel]")) cancelRerunDraft();
    else if (event.target.closest("[data-comfyui-edit-params]")) openParamsDialog();
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
  // 批量生成档：填单与跑批渲染进 #comfyuiBatchBody；结果页批次详情渲染进
  // #comfyuiBatchDetail。两个宿主共用同一套容器级委托。
  for (const selector of ["#comfyuiBatchBody", "#comfyuiBatchDetail"]) {
    const batchHost = $(selector);
    if (!batchHost) continue;
    batchHost.addEventListener("click", onBatchClick);
    // 跑批卡片里的产物卡与单次结果同一套节点：下载按钮也得接同一条委托。
    batchHost.addEventListener("click", onOutputGridClick);
    batchHost.addEventListener("change", onBatchChange);
    batchHost.addEventListener("input", onBatchInput);
    batchHost.addEventListener("dragover", onBatchDragOver);
    batchHost.addEventListener("dragleave", onBatchDragLeave);
    batchHost.addEventListener("drop", onBatchDrop);
  }
  // 分镜队列：拆书匠跨页交接，只进批量生成。
  document.addEventListener("naiba:storyboard-handoff", onStoryboardHandoff);
}

async function enterPage() {
  void refreshHealth();
  void refreshWorkflows();
  void refreshTasks();
  void loadModelCandidates();
  void loadBatchHistories();
  renderParamForm();
  scheduleBackgroundRefresh();
  if (batchPaneVisible()) enterBatchPane();
  else if (batch.resultsDetailId && comfyuiPaneName() === "tasks") {
    // 回到造梦机页时结果详情还在：按持久时间重算耗时并恢复监视。
    syncResultsBatchDetail();
    startBatchPolling();
  }
}

function comfyuiPageVisible() {
  return isStudioOpen() && currentStudioPage() === "comfyui";
}

function comfyuiPaneName() {
  return String($("#studioPageComfyui")?.dataset.comfyuiPane || "");
}

function batchPaneVisible() {
  return comfyuiPageVisible() && comfyuiPaneName() === "batch";
}

// 跑批监视的可见口径：批量页的跑批视图，或结果页「批量」档里打开的批次详情。
function batchRunVisible() {
  if (batchPaneVisible() && batch.view === "run") return true;
  return comfyuiPageVisible() && comfyuiPaneName() === "tasks"
    && comfyui.resultsSegment === "batch" && Boolean(batch.resultsDetailId);
}

function onPaneClick(event) {
  const btn = event.target.closest("[data-comfyui-pane]");
  if (!btn) return;
  setComfyuiPane(btn.dataset.comfyuiPane);
}

// 工作流 / 单次生成 / 批量生成 / 结果四档在所有宽度互斥整页（2026-11-06 结果页
// 改造随三栏布局一起退役了旧的页面宽度判档机制：档位切换不再依赖 ResizeObserver，
// data-comfyui-pane 是唯一的显隐真源）。

function setComfyuiPane(pane) {
  const root = $("#studioPageComfyui");
  if (!root) return;
  // 离开生成表单就丢弃再次生成草稿；旧任务快照不跨档位复用到别的表单。
  if (pane !== "run" && comfyui.rerun) dropRerunDraft();
  root.dataset.comfyuiPane = pane;
  document.querySelectorAll("#comfyuiPanes [data-comfyui-pane]").forEach((node) => {
    const active = node.dataset.comfyuiPane === pane;
    node.classList.toggle("active", active);
    node.setAttribute("aria-pressed", String(active));
  });
  // 页签和主表单的「批量生成」按钮共用入口，首次进入也要初始化填单。
  if (pane === "batch") {
    enterBatchPane();
  } else if (pane === "tasks" && batch.resultsDetailId) {
    // 结果页批次详情是独立视图：回到结果档时按持久时间重算，不重新起算。
    syncResultsBatchDetail();
    startBatchPolling();
  } else {
    stopBatchPolling();
  }
}

// 结果页两档（单次 / 批量）共用一个页签：由页面根上的 data-results-segment 驱动
// CSS 显隐，与 data-comfyui-pane 同一套口径。
function setComfyuiResultsSegment(seg) {
  comfyui.resultsSegment = seg === "batch" ? "batch" : "single";
  const root = $("#studioPageComfyui");
  if (root) root.dataset.resultsSegment = comfyui.resultsSegment;
  document.querySelectorAll("#comfyuiResultsSeg [data-comfyui-results-seg]").forEach((node) => {
    const active = node.dataset.comfyuiResultsSeg === comfyui.resultsSegment;
    node.classList.toggle("active", active);
    node.setAttribute("aria-pressed", String(active));
  });
  if (comfyui.resultsSegment === "batch") {
    if (batch.resultsDetailId) syncResultsBatchDetail();
    else {
      stopBatchPolling();
      void loadBatchHistories();
    }
  } else if (!batchPaneVisible()) {
    stopBatchPolling();
  }
}

// 批量条目也是普通任务（会进 /api/comfyui/tasks），但结果页按来源分档：
// 带 batch_id 的行只在「批量」档出现，单次档的任务列表和产物网格不再混排。
function isBatchTaskRow(row) {
  return Boolean(row && row.batch_id);
}

function scheduleBackgroundRefresh() {
  stopBackgroundRefresh();
  comfyui.refreshTimer = setInterval(() => {
    if (!comfyuiPageVisible()) return;
    // 轻量保活：任务列表轮询走 pollActiveTask；这里只补工作流/连接的偶发变化，
    // 顺带刷新批次列表（本地文件读取，进行中的批次在「批量」档实时推进）。
    void refreshTasks(true);
    void loadBatchHistories();
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
    // 四档互斥整页：选好工作流直接跳到单次生成档填参数。
    setComfyuiPane("run");
  }
}

async function selectWorkflow(id) {
  // 手动选工作流即离开再次生成草稿：旧任务快照不能串到这份表单里。
  if (comfyui.rerun) resetRerunState();
  comfyui.selectedId = id;
  comfyui.mediaSelection.clear();
  comfyui.mediaSkipped.clear();
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
  edit.textContent = "设置高级参数";
  edit.title = "选择要在主表单显示的参数，并调整它们的先后顺序";
  if (comfyui.rerun) {
    // 再次生成只读原任务快照：显示集属于工作流库，不能在快照表单里修改。
    edit.disabled = true;
    edit.style.display = "none";
    edit.title = "再次生成使用原任务快照，不修改工作流库";
  }
  actions.append(reset, edit);
  head.append(heading, actions);
  form.append(head);
  if (comfyui.rerun) {
    // 来源提示与取消入口：让用户知道这份表单来自哪条已完成任务，而不是当前工作流库。
    const banner = document.createElement("div");
    banner.className = "comfyui-rerun-banner";
    const text = document.createElement("p");
    const sourceName = comfyui.rerun.workflowName || comfyui.detail.name || "原任务";
    const stamp = comfyui.rerun.finishedAt || comfyui.rerun.createdAt;
    const when = stamp
      ? ` · ${comfyui.rerun.finishedAt ? "完成于" : "创建于"} ${formatTime(stamp)}`
      : "";
    text.textContent = `再次生成 · 来源任务「${sourceName}」${when}`;
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "control-button tiny";
    cancel.dataset.comfyuiRerunCancel = "1";
    cancel.textContent = "取消再次生成";
    banner.append(text, cancel);
    form.append(banner);
    const problems = rerunMediaProblems();
    if (problems.length) {
      const alert = document.createElement("p");
      alert.className = "comfyui-rerun-alert";
      alert.textContent = `有 ${problems.length} 个原任务媒体文件已不存在：请重新选择，或点 ⊘ 跳过后再提交。`;
      form.append(alert);
    }
  }
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
// 不打断调参、不弹窗；单次按当前表单值执行，批量按工作流已保存快照出片，
// 调整要么另存进工作流，要么在批量条目里逐项填。
function updateDraftHint() {
  const hint = $("#comfyuiDraftHint");
  if (!hint) return;
  // 再次生成是「原任务快照」不是当前工作流的未保存调整：别把覆盖值误报成脏改动。
  if (comfyui.rerun) {
    hint.hidden = true;
    hint.textContent = "";
    return;
  }
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
    ? `当前有 ${changed} 项未保存调整；「开始生成」按当前填的值执行，批量生成按工作流里已保存的快照执行——要让批量带上这些调整，先另存进工作流，或在批量条目里逐项填。`
    : "";
}

// ---- 编辑参数 ----

function openParamsDialog() {
  if (!comfyui.detail) return;
  if (comfyui.rerun) {
    toast("再次生成使用原任务快照，不修改工作流库的显示参数");
    return;
  }
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
  if (comfyui.rerun) {
    toast("再次生成使用原任务快照，不修改工作流库的显示参数");
    return;
  }
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
  toast(comfyui.rerun
    ? "已还原成原任务快照里的参数值"
    : "已还原成工作流里原本的参数值");
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
const MEDIA_PLAY_ICON = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';

// 判定口径与 renderSlot 的 isMedia 完全一致：control 声明或 field_type 命中即媒体槽；
// kind 取 slot.media（后端显式映射）优先。类型不在三集合里的边缘数据返回 ""，
// 继续走 renderSlot → renderMediaPicker 的老行渲染，不至于丢失控件。
function mediaKindOfSlot(slot) {
  if (slot.control !== "media" && !MEDIA_FIELD_TYPES.has(slot.field_type)) return "";
  const kind = slot.media || slot.field_type;
  return MEDIA_GROUP_ORDER.includes(kind) ? kind : "";
}

// 三档口径唯一实现（分组块与兜底行共用）：手动 ⊘ > 已选新文件 > 沿用原文件 >
// 空值且确认 optional 自动跳过 > 请选择文件。「沿用原文件」优先于 optional——
// 工作流固定了素材时，不能因为槽位可选就把原文件删掉（2026-11-09 口径）。
// full 用于块 title/兜底行的整句；badge 是块上角标的短文案；
// clearTitle 是已选块「清除」按钮的说明。
function mediaSlotStatus(slot, picked, skipped = false, problem = false) {
  const emptyInWorkflow = !String(slot.default ?? "").trim() || String(slot.default).trim() === "None";
  const skipSafe = slot.media_optional === true;
  if (skipped) {
    return {
      badge: "已跳过",
      cls: "skip",
      full: "已跳过：这次不提交这个输入；点 ⊘ 可以恢复",
      clearTitle: "清除选择",
    };
  }
  if (problem || picked?.available === false) {
    return {
      badge: "文件失效",
      cls: "fail",
      full: "原任务的媒体文件已不存在：请重新选择，或点 ⊘ 跳过",
      clearTitle: "清除选择",
    };
  }
  if (picked) {
    return {
      badge: "已选",
      cls: "ok",
      full: `已选：${picked.name}`,
      clearTitle: emptyInWorkflow
        ? "清除选择：回到工作流里的空值"
        : "清除选择：生成时按工作流原本的文件执行",
    };
  }
  if (emptyInWorkflow && skipSafe) return { badge: "未选即跳过", cls: "", full: "未选即跳过" };
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
  const pickedCount = slots.filter((slot) => {
    const picked = comfyui.mediaSelection.get(slotKey(slot));
    return Boolean(picked?.path) && picked.available !== false;
  }).length;
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

// ⊘ 手动跳过：独立于已选文件；再次点击取消，恢复此前原文件/已选文件状态。
// 只有这条路径不受 optional 限制——它表达的是用户明确不要这个输入。
function mediaSkipButton(key, skipped) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "t-skip" + (skipped ? " active" : "");
  button.textContent = "⊘";
  const label = skipped ? "取消跳过，恢复此前状态" : "跳过这个输入";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.dataset.comfyuiMediaSkip = key;
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    toggleMediaSkip(key);
  });
  return button;
}

function toggleMediaSkip(key) {
  if (comfyui.mediaSkipped.has(key)) comfyui.mediaSkipped.delete(key);
  else comfyui.mediaSkipped.add(key);
  rerenderParamForm();
}

function renderMediaTile(slot, key, kind) {
  const picked = comfyui.mediaSelection.get(key) || null;
  const skipped = comfyui.mediaSkipped.has(key);
  // 再次生成的原任务文件可能已被清理：失效态必须显式提示，不能退回工作流原值。
  const problem = Boolean(comfyui.rerun?.unavailable?.has(key)) || picked?.available === false;
  const status = mediaSlotStatus(slot, picked, skipped, problem);
  // 「沿用原文件」的槽按实块渲染（占位图标 + 原文件名），不是虚线空块（§3.4）。
  const keepOriginal = !skipped && !picked && !problem && status.cls === "keep";
  const tile = document.createElement("div");
  tile.className = "comfyui-media-tile"
    + (kind === "audio" ? " audio" : kind === "video" ? " video" : "")
    + (skipped ? " skipped" : "")
    + (problem ? " problem" : "");
  if (!picked && !keepOriginal && !skipped && !problem) tile.classList.add("empty");
  tile.dataset.comfyuiMediaTile = key;
  tile.dataset.comfyuiMediaSkipped = skipped ? "1" : "";
  tile.dataset.comfyuiMediaProblem = problem ? "1" : "";
  tile.title = `${slot.label || ""}\n${status.full}`;
  const file = document.createElement("input");
  file.type = "file";
  file.accept = MEDIA_ACCEPT[kind] || "";
  file.hidden = true;
  file.addEventListener("change", () => void onMediaPicked(slot, key, file));

  const badge = document.createElement("span");
  badge.className = "t-status" + (status.cls ? ` ${status.cls}` : "");
  badge.textContent = status.badge;
  const skip = mediaSkipButton(key, skipped);

  if (picked || keepOriginal || skipped || problem) {
    const thumb = document.createElement("div");
    thumb.className = "thumb";
    const actions = document.createElement("div");
    actions.className = "t-actions";
    const replace = document.createElement("button");
    replace.type = "button";
    replace.textContent = "⇄";
    replace.title = problem ? "重新选择文件（原文件已失效）" : picked ? "替换这个文件" : skipped
      ? "选择文件替换并取消跳过" : `选择文件替换（当前沿用 ${slot.default}）`;
    replace.setAttribute("aria-label", replace.title);
    replace.addEventListener("click", (event) => {
      event.stopPropagation();
      file.click();
    });
    actions.append(replace);
    if (picked && picked.available !== false) {
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
    if (picked && picked.available !== false) {
      const record = { path: picked.path, thumb_path: picked.thumb_path || "", name: picked.name || "" };
      tile.dataset.comfyuiPreviewUrl = localFileUrl(record.path);
      tile.dataset.comfyuiPreviewKind = kind;
      tile.dataset.comfyuiPreviewName = record.name;
      tile.dataset.comfyuiPreviewKey = JSON.stringify(["input", key]);
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
          openMediaPreviewOverlay(localFileUrl(record.path), "video", tile);
        });
      } else {
        const icon = document.createElement("span");
        icon.className = "ph-icon";
        icon.innerHTML = MEDIA_PLAY_ICON;
        thumb.append(icon);
        tile.addEventListener("click", (event) => {
          if (event.target.closest(".t-actions")) return;
          openMediaPreviewOverlay(localFileUrl(record.path), "audio", tile);
        });
      }
    } else {
      const icon = document.createElement("span");
      icon.className = "ph-icon";
      icon.innerHTML = MEDIA_GROUP_ICONS[kind];
      thumb.append(icon);
      if (!skipped) {
        tile.addEventListener("click", (event) => {
          if (event.target.closest(".t-actions") || event.target.closest(".t-skip")) return;
          file.click();
        });
      }
    }
    const name = document.createElement("div");
    name.className = "t-name";
    name.textContent = picked
      ? (picked.available === false ? `${picked.name || "文件"}（已失效）` : picked.name)
      : problem ? "原文件已失效" : skipped
        ? "已跳过这个输入" : String(slot.default ?? "");
    name.title = name.textContent;
    const port = document.createElement("div");
    port.className = "t-port";
    port.textContent = mediaPortText(slot);
    tile.append(thumb, badge, skip, actions, name, port);
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
    tile.append(badge, skip, wrap);
    if (kind !== "audio") {
      const port = document.createElement("div");
      port.className = "t-port";
      port.textContent = mediaPortText(slot);
      tile.append(port);
    }
    tile.addEventListener("click", (event) => {
      if (event.target.closest(".t-skip")) return;
      file.click();
    });
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

// 视频/音频放大播放浮层：同组前后切换；关闭或换项必须停下并释放旧播放器。
let closeMediaPreviewOverlay = null;
function openMediaPreviewOverlay(url, kind, sourceEl = null) {
  closeMediaPreviewOverlay?.();
  const overlay = document.createElement("div");
  overlay.className = "comfyui-media-preview-overlay";
  overlay.tabIndex = -1;
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-label", kind === "video" ? "视频预览" : "音频预览");
  const items = collectMediaPreviewItems(sourceEl, kind);
  const selectedKey = String(sourceEl?.closest?.('[data-comfyui-preview-key]')?.dataset?.comfyuiPreviewKey || url);
  let index = items.findIndex((item) => item.key === selectedKey || item.url === url);
  if (index < 0) { items.splice(0, items.length, { url, kind, name: "" }); index = 0; }
  else items[index] = { ...items[index], url };
  let media = null;
  const stopMedia = () => {
    if (!media) return;
    media.pause();
    media.removeAttribute("src");
    media.load();
    media.remove();
    media = null;
  };
  const counter = document.createElement("div");
  counter.className = "comfyui-media-preview-counter";
  counter.dataset.mediaPreviewCounter = "";
  counter.setAttribute("aria-live", "polite");
  const previous = document.createElement("button");
  const next = document.createElement("button");
  for (const [button, delta, label, icon] of [[previous, -1, "上一项（←）", "‹"], [next, 1, "下一项（→）", "›"]]) {
    button.type = "button";
    button.className = `comfyui-media-preview-nav ${delta < 0 ? "previous" : "next"}`;
    button.dataset.mediaPreviewStep = String(delta);
    button.setAttribute("aria-label", label);
    button.title = label;
    button.textContent = icon;
    button.hidden = items.length < 2;
    button.addEventListener("click", () => step(delta));
  }
  const render = () => {
    stopMedia();
    const item = items[index];
    media = document.createElement(kind === "video" ? "video" : "audio");
    media.src = item.url;
    media.controls = true;
    media.autoplay = true;
    if (kind === "video") media.playsInline = true;
    counter.textContent = `${index + 1} / ${items.length}${item.name ? ` · ${item.name}` : ""}`;
    overlay.prepend(media);
  };
  const step = (delta) => {
    if (items.length < 2) return;
    index = (index + delta + items.length) % items.length;
    render();
  };
  const close = document.createElement("button");
  close.type = "button";
  close.className = "comfyui-media-preview-close";
  close.dataset.mediaPreviewClose = "";
  close.textContent = "✕";
  close.title = "关闭";
  close.setAttribute("aria-label", "关闭预览");
  let suppressClick = false;
  const shutdown = () => {
    stopMedia();
    overlay.remove();
    document.removeEventListener("keydown", onKey, true);
    if (closeMediaPreviewOverlay === shutdown) closeMediaPreviewOverlay = null;
    if (sourceEl?.isConnected) sourceEl.focus?.();
  };
  const onKey = (event) => {
    if (!["Escape", "ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (event.key === "Escape") shutdown();
    else step(event.key === "ArrowLeft" ? -1 : 1);
  };
  close.addEventListener("click", shutdown);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay && !suppressClick) shutdown();
  });
  let swipe = null;
  overlay.addEventListener("touchstart", (event) => {
    if (event.touches.length !== 1 || event.target.closest?.("button")) { swipe = null; return; }
    swipe = { x: event.touches[0].clientX, y: event.touches[0].clientY };
  }, { passive: true });
  overlay.addEventListener("touchend", (event) => {
    if (!swipe || event.touches.length) { swipe = null; return; }
    const touch = event.changedTouches?.[0];
    const dx = touch ? touch.clientX - swipe.x : 0;
    const dy = touch ? touch.clientY - swipe.y : 0;
    swipe = null;
    if (Math.abs(dx) >= 45 && Math.abs(dx) > Math.abs(dy) * 1.2) {
      suppressClick = true;
      step(dx < 0 ? 1 : -1);
      window.setTimeout(() => { suppressClick = false; }, 400);
    }
  }, { passive: true });
  document.addEventListener("keydown", onKey, true);
  closeMediaPreviewOverlay = shutdown;
  overlay.append(previous, next, counter, close);
  render();
  document.body.append(overlay);
  overlay.focus();
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
  // 手动 ⊘ 的槽是用户明确不要这个输入，不参与批量填充。
  const empties = slots.filter((slot) => !comfyui.mediaSelection.get(slotKey(slot))?.path
    && !comfyui.mediaSkipped.has(slotKey(slot)));
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
        // 选文件即表达「这次用这个文件」：取消该槽此前的 ⊘ 跳过，并解除失效标记。
        comfyui.mediaSkipped.delete(key);
        comfyui.rerun?.unavailable?.delete(key);
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
  const skipped = comfyui.mediaSkipped.has(key);
  const problem = Boolean(comfyui.rerun?.unavailable?.has(key)) || picked?.available === false;
  const preview = document.createElement("div");
  preview.className = "comfyui-media-preview";
  if (picked && picked.available !== false) {
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
  const status = mediaSlotStatus(slot, picked, skipped, problem);
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
  // 兜底行也要有手动 ⊘：与分组块同一套状态，避免两条渲染路径分叉。
  const skipButton = document.createElement("button");
  skipButton.type = "button";
  skipButton.className = "control-button tiny" + (skipped ? " active" : "");
  skipButton.textContent = "⊘";
  const skipLabel = skipped ? "取消跳过，恢复此前状态" : "跳过这个输入";
  skipButton.title = skipLabel;
  skipButton.setAttribute("aria-label", skipLabel);
  skipButton.addEventListener("click", () => toggleMediaSkip(key));
  actions.append(skipButton);
  if (picked && picked.available !== false) {
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
// 未选的槽不提交：原值非空的沿用原文件；空值且确认 optional 的由执行端自动跳过。
// 手动 ⊘ 的槽即使选过文件也不提交——跳过是用户明确的最高优先级。
function collectMediaInputs() {
  const items = [];
  for (const slot of comfyui.detail?.params || []) {
    if ((slot.control || "") !== "media" && !MEDIA_FIELD_TYPES.has(slot.field_type)) continue;
    const key = slotKey(slot);
    if (comfyui.mediaSkipped.has(key)) continue;
    const picked = comfyui.mediaSelection.get(key);
    if (picked?.path) {
      items.push({ node_id: slot.node_id, field_name: slot.field_name, path: picked.path });
    }
  }
  return items;
}

// 手动 ⊘ 的媒体槽地址：独立于 media_inputs 随任务提交，执行端强制移除下游连接。
function collectMediaSkips() {
  const items = [];
  for (const slot of comfyui.detail?.params || []) {
    if ((slot.control || "") !== "media" && !MEDIA_FIELD_TYPES.has(slot.field_type)) continue;
    if (!comfyui.mediaSkipped.has(slotKey(slot))) continue;
    items.push({ node_id: slot.node_id, field_name: slot.field_name });
  }
  return items;
}

// ---- 再次生成：完成态任务冻结快照 → 独立表单草稿 ----

function clearDraftsForWorkflow(workflowId) {
  const prefix = `${workflowId || ""}|`;
  for (const key of [...comfyui.drafts.keys()]) {
    if (key.startsWith(prefix)) comfyui.drafts.delete(key);
  }
}

// 原任务媒体文件失效的槽位：必须重新选择或手动 ⊘，不能静默退回工作流原值。
function rerunMediaProblems() {
  const unavailable = comfyui.rerun?.unavailable;
  if (!unavailable) return [];
  const problems = [];
  for (const key of unavailable) {
    if (comfyui.mediaSkipped.has(key)) continue;
    const picked = comfyui.mediaSelection.get(key);
    if (picked?.path && picked.available !== false) continue;
    problems.push(String(key));
  }
  return problems;
}

// 再次生成期间隐藏「用当前配置另存工作流」：计划要求不改/不新增工作流库条目。
function setRerunSaveAsHidden(hidden) {
  const saveAs = $("#comfyuiSaveCurrentAs");
  if (!saveAs) return;
  saveAs.hidden = hidden;
  saveAs.disabled = hidden;
  saveAs.style.display = hidden ? "none" : "";
}

function resetRerunState() {
  const rerun = comfyui.rerun;
  if (!rerun) return;
  clearDraftsForWorkflow(rerun.workflowId);
  comfyui.rerun = null;
  setRerunSaveAsHidden(false);
  comfyui.mediaSelection.clear();
  comfyui.mediaSkipped.clear();
  const shots = $("#comfyuiShots");
  if (shots) shots.value = "1";
  const unload = $("#comfyuiUnloadAfter");
  if (unload) unload.checked = false;
}

// 离开生成表单 / 切换工作流：丢弃再次生成草稿，别把旧任务串到别的表单。
function dropRerunDraft() {
  if (!comfyui.rerun) return;
  resetRerunState();
  comfyui.detail = null;
  comfyui.selectedId = "";
  renderWorkflowList();
  renderParamForm();
}

function cancelRerunDraft() {
  if (!comfyui.rerun) return;
  dropRerunDraft();
  setComfyuiResultsSegment("single");
  setComfyuiPane("tasks");
}

function buildRerunDetail(snapshot) {
  if (!snapshot || typeof snapshot !== "object") {
    throw new Error("再次生成快照为空");
  }
  if (!String(snapshot.workflow_id || "")
      || !snapshot.prompt || typeof snapshot.prompt !== "object"
      || !Array.isArray(snapshot.params) || !Array.isArray(snapshot.overrides)
      || !Array.isArray(snapshot.media_inputs) || !Array.isArray(snapshot.media_skip)) {
    throw new Error("再次生成快照不完整，原任务未改动");
  }
  const params = snapshot.params.map((slot) =>
    (slot && typeof slot === "object") ? { ...slot } : null);
  if (params.some((slot) => !slot || !slot.node_id || !slot.field_name)) {
    throw new Error("任务快照的参数清单不完整，无法再次生成");
  }
  const slotKeys = new Set(params.map((slot) => slotKey(slot)));
  const overrides = new Map();
  for (const row of snapshot.overrides) {
    if (!row?.node_id || !row?.field_name) {
      throw new Error("任务快照的参数覆盖不完整，无法再次生成");
    }
    const key = `${row.node_id}|${row.field_name}`;
    if (!slotKeys.has(key)) {
      throw new Error("任务快照的参数覆盖找不到对应槽位，无法再次生成");
    }
    overrides.set(key, row.value);
  }
  // 原任务覆盖值变成表单当前值；没覆盖的槽位继续用冻结 prompt 的原值。
  for (const slot of params) {
    const key = slotKey(slot);
    if (overrides.has(key)) slot.default = overrides.get(key);
  }
  const mediaKeys = new Set(params.filter((slot) => mediaKindOfSlot(slot)).map((slot) => slotKey(slot)));
  const mediaSelection = new Map();
  const unavailable = new Set();
  for (const row of snapshot.media_inputs) {
    if (!row?.node_id || !row?.field_name) {
      throw new Error("任务快照的媒体输入不完整，无法再次生成");
    }
    const key = `${row.node_id}|${row.field_name}`;
    if (!mediaKeys.has(key)) {
      throw new Error("任务快照的媒体输入找不到对应媒体槽，无法再次生成");
    }
    const available = row.available !== false && Boolean(row.path);
    mediaSelection.set(key, {
      path: String(row.path || ""),
      name: String(row.name || ""),
      // 任务快照没有侧车缩略图：图片直接读原文件，别再推导 _thumb 造成破图。
      thumb_path: row.media === "image" ? String(row.path || "") : "",
      available,
    });
    if (!available) unavailable.add(key);
  }
  const skipped = new Set();
  for (const row of snapshot.media_skip) {
    if (!row?.node_id || !row?.field_name) {
      throw new Error("任务快照的媒体跳过不完整，无法再次生成");
    }
    const key = `${row.node_id}|${row.field_name}`;
    if (!mediaKeys.has(key)) {
      throw new Error("任务快照的媒体跳过找不到对应媒体槽，无法再次生成");
    }
    skipped.add(key);
  }
  const visible = defaultVisibleKeys(params);
  for (const key of overrides.keys()) visible.add(key);
  for (const key of mediaKeys) visible.add(key);
  return {
    detail: {
      id: String(snapshot.workflow_id),
      name: String(snapshot.workflow_name || "原任务工作流"),
      prompt: snapshot.prompt,
      params,
      visible_params: [...visible].map((key) => {
        const [nodeId, ...rest] = key.split("|");
        return { node_id: nodeId, field_name: rest.join("|") };
      }),
    },
    mediaSelection,
    skipped,
    unavailable,
  };
}

async function openRerunSnapshot(taskId) {
  const id = String(taskId || "");
  if (!id || comfyui.rerunLoading) return;
  comfyui.rerunLoading = id;
  try {
    const payload = await api(`/api/comfyui/tasks/${encodeURIComponent(id)}/rerun-snapshot`);
    const snapshot = payload?.snapshot;
    if (!snapshot || String(snapshot.source_task_id || "") !== id) {
      throw new Error("服务端没有返回这条任务的再次生成快照");
    }
    const built = buildRerunDetail(snapshot);
    // 全部构建成功后才替换表单状态：失败时原任务和当前表单都不受影响。
    clearDraftsForWorkflow(String(snapshot.workflow_id));
    comfyui.detail = built.detail;
    comfyui.selectedId = String(snapshot.workflow_id);
    comfyui.mediaSelection = built.mediaSelection;
    comfyui.mediaSkipped = built.skipped;
    comfyui.rerun = {
      sourceTaskId: id,
      workflowId: String(snapshot.workflow_id),
      workflowName: String(snapshot.workflow_name || ""),
      createdAt: Number(snapshot.created_at) || 0,
      finishedAt: Number(snapshot.finished_at) || 0,
      unavailable: built.unavailable,
    };
    setRerunSaveAsHidden(true);
    const shots = $("#comfyuiShots");
    if (shots) shots.value = String(Math.max(1, Math.min(200, Math.floor(Number(snapshot.shots) || 1))));
    const unload = $("#comfyuiUnloadAfter");
    if (unload) unload.checked = Boolean(snapshot.unload_after);
    renderWorkflowList();
    renderParamForm();
    setComfyuiPane("run");
    toast(`已载入「${comfyui.rerun.workflowName || built.detail.name}」的完成快照，可直接提交或修改`);
  } catch (error) {
    toast(`再次生成不可用：${error.message}`);
  } finally {
    comfyui.rerunLoading = "";
  }
}

async function submitTask() {
  if (comfyui.submitting || !comfyui.detail) return;
  const rerunId = String(comfyui.rerun?.sourceTaskId || "");
  if (rerunId) {
    const problems = rerunMediaProblems();
    if (problems.length) {
      // 原任务媒体文件失效：不能静默退回工作流原值，必须重新选择或手动 ⊘。
      toast(`有 ${problems.length} 个原任务媒体文件已不存在：请重新选择，或点 ⊘ 跳过后再提交`);
      renderParamForm();
      return;
    }
  }
  const shotsInput = $("#comfyuiShots");
  let shots = 1;
  try {
    shots = Math.max(1, Math.min(200, Math.floor(Number(shotsInput?.value || 1))));
  } catch (_) { /* 回落默认 */ }
  comfyui.submitting = true;
  const button = $("#comfyuiSubmit");
  if (button) button.disabled = true;
  try {
    const body = {
      overrides: collectOverrides(),
      media_inputs: collectMediaInputs(),
      media_skip: collectMediaSkips(),
      shots,
      // 「释放模型」是单次专属开关：任务收尾请求 ComfyUI 卸载；批量走
      // batch.draft.unload_after，两条路径互不读写对方状态。
      unload_after: Boolean($("#comfyuiUnloadAfter")?.checked),
    };
    // 再次生成只用原任务冻结快照做基底；普通提交才带当前工作流 id。
    if (!rerunId) body.workflow_id = comfyui.detail.id;
    const endpoint = rerunId
      ? `/api/comfyui/tasks/${encodeURIComponent(rerunId)}/rerun`
      : "/api/comfyui/tasks";
    const payload = await api(endpoint, { method: "POST", body });
    // 仅在任务受理成功后复位开关；失败保留勾选，用户重试不丢选择。
    const unloadToggle = $("#comfyuiUnloadAfter");
    if (unloadToggle) unloadToggle.checked = false;
    if (rerunId) {
      // 新任务已经创建：再次生成草稿完成使命，原任务记录不受影响。
      resetRerunState();
      comfyui.detail = null;
      comfyui.selectedId = "";
      renderWorkflowList();
      renderParamForm();
    }
    comfyui.activeTaskId = String(payload.task?.id || "");
    comfyui.eventSeq = 0;
    comfyui.tasks = [];
    renderTasks();
    await refreshTasks();
    startPolling();
    // 四档互斥整页：提交后跳到结果档盯进度。
    setComfyuiPane("tasks");
    toast(rerunId ? "已按原任务快照提交新任务，正在排队执行" : "已提交，正在排队执行");
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
  if (!comfyuiPageVisible()) return;
  comfyui.pollTimer = setInterval(() => { void pollActiveTask(); }, POLL_MS);
}

function stopPolling() {
  if (comfyui.pollTimer) {
    clearInterval(comfyui.pollTimer);
    comfyui.pollTimer = null;
  }
}

async function pollActiveTask() {
  if (!comfyuiPageVisible()) {
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

// ============================================================
// 提取 ComfyUI 进度条（顶栏全局单条，定点更新）
// ============================================================

// runner 单 worker 串行，同一时刻至多一个 prompt 在采样；单次任务与批量当前
// 条目各回一份 step，取 updated_at 最新的那份。形状非法一律视为无数据（不渲染）。
function normalizeStep(step) {
  if (!step || typeof step !== "object") return null;
  const value = Number(step.value);
  const max = Number(step.max);
  if (!Number.isFinite(value) || !Number.isFinite(max) || max <= 0 || value > max) return null;
  return {
    value,
    max,
    rate: typeof step.rate === "string" ? step.rate.trim() : "",
    updatedAt: Number(step.updated_at) || 0,
  };
}

function activeComfyStep() {
  let best = null;
  for (const row of comfyui.tasks || []) {
    if (String(row.status || "") !== "running") continue;
    const step = normalizeStep(row.step);
    if (step && (!best || step.updatedAt >= best.updatedAt)) best = step;
  }
  for (const item of batch.runData?.items || []) {
    if (String(item.status || "") !== "running") continue;
    const step = normalizeStep(item.step);
    if (step && (!best || step.updatedAt >= best.updatedAt)) best = step;
  }
  return best;
}

// 唯一写手：只定点写宽度和标签文本；终态/降级整块隐藏，不留 100% 定格。
// 不进 taskFingerprint / batchRunSignature：步进刷新太频繁，进了会整卡重建闪屏。
function updateStepMeter() {
  const meter = document.getElementById("comfyuiStepMeter");
  if (!meter) return;
  const step = activeComfyStep();
  if (!step) {
    meter.hidden = true;
    return;
  }
  const pct = Math.max(0, Math.min(100, Math.round((step.value / step.max) * 100)));
  meter.hidden = false;
  const fill = meter.querySelector(".comfyui-step-meter-fill");
  if (fill) fill.style.width = `${pct}%`;
  const pctNode = meter.querySelector(".comfyui-step-meter-pct");
  if (pctNode) pctNode.textContent = `${pct}%`;
  const detail = meter.querySelector(".comfyui-step-meter-detail");
  if (detail) {
    detail.textContent = ` (${step.value}/${step.max})${step.rate ? ` ${step.rate}` : ""}`;
  }
}

function renderTasks() {
  const list = $("#comfyuiTaskList");
  if (!list) return;
  updateStepMeter();
  // 批量条目去了结果页「批量」档的批次列表，这里只留单次任务。
  const rows = comfyui.tasks.filter((row) => !isBatchTaskRow(row)).slice(0, 12);
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
  } else if (row.status === "completed" && !isBatchTaskRow(row)) {
    // 完成态单次任务：只加这一个入口，不改失败任务的「重试续跑」语义。
    const rerun = document.createElement("button");
    rerun.type = "button";
    rerun.className = "control-button tiny";
    rerun.dataset.comfyuiRerun = row.id;
    rerun.textContent = "再次生成";
    actions.append(rerun);
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
  // 重试过的任务显示最近一轮开始时刻（retry_count 由重试入口累加）：
  // 创建时间是首次提交的，重试后仍是旧值。排队中不显示：runner 还没
  // 重写 started_at，那还是上一轮的开始时刻。
  const retried = Number(row.retry_count || 0) > 0;
  const started = formatTime(row.started_at);
  const created = formatTime(row.created_at);
  if (retried && started && row.status !== "queued") pieces.push(`开始 ${started}`);
  else if (created) pieces.push(`创建 ${created}`);
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
  const rerun = event.target.closest("[data-comfyui-rerun]");
  if (rerun) {
    await openRerunSnapshot(rerun.dataset.comfyuiRerun);
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
  if (!window.confirm("删除全部已结束的单次任务和它们的产物文件？批量产物请到「批量」档里按批次删。")) return;
  try {
    // exclude_batch：单次档看不见批量条目，就不能在这里把它们一起删掉。
    const payload = await api("/api/comfyui/tasks", {
      method: "DELETE",
      body: { exclude_batch: true },
    });
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
    if (isBatchTaskRow(row)) continue; // 批量产物在「批量」档按批次看
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
    // 视频卡进入视野才读元数据，首帧当封面；逐卡观察，新卡在后续轮询里补挂。
    batchPreviewVideoPreload(existing.get(key));
  }
  for (const [key, node] of existing) {
    if (!seen.has(key)) node.remove();
  }
  // 只在产物集合或顺序变化时才重排：无变化轮询不触碰媒体元素（图片不重载）。
  const wantedOrder = wanted.map((entry) => entry.key);
  const currentOrder = Array.from(grid.children).map((node) => node.dataset.outputKey || "");
  if (currentOrder.join("|") !== wantedOrder.join("|")) {
    let next = grid.firstElementChild;
    for (const key of wantedOrder) {
      const node = existing.get(key);
      if (node !== next) grid.insertBefore(node, next);
      next = node.nextElementSibling;
    }
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
  card.dataset.comfyuiPreviewUrl = url;
  card.dataset.comfyuiPreviewKind = kind;
  card.dataset.comfyuiPreviewName = name;
  card.dataset.comfyuiPreviewKey = JSON.stringify([String(taskId), String(name)]);
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
    // 批量结果可同时有 24 段视频，隐藏的结果栏也会创建媒体节点。
    // 由用户点击后再请求，避免打开批量页便并发读取所有视频及反复取消 Range。
    video.preload = "none";
    card.append(video);
  } else if (kind === "audio") {
    // 音频产物此前退化为下载链接；现在与聊天媒体卡一致，就地播放。
    const audio = document.createElement("audio");
    audio.src = url;
    audio.controls = true;
    audio.preload = "none";
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
  if (kind === "video" || kind === "audio") {
    const preview = document.createElement("button");
    preview.type = "button";
    preview.className = "comfyui-output-preview";
    preview.dataset.mediaPreviewOpen = "";
    preview.textContent = "放大预览";
    preview.setAttribute("aria-label", `放大预览 ${name}`);
    preview.addEventListener("click", (event) => {
      event.stopPropagation();
      card.querySelector(kind)?.pause();
      openMediaPreviewOverlay(url, kind, card);
    });
    card.append(preview);
  }
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
// 分镜队列（拆书匠「分镜生图」跨页交接）：只进批量生成，导入成条目表的提示词。
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
  if (currentStudioPage() !== "comfyui") setStudioPage("comfyui");
  // 直接落到批量档：分镜提示词只走批量这一条路。
  setComfyuiPane("batch");
  toast(`已带入 ${items.length} 条分镜提示词：在批量生成页点「从分镜队列导入」`);
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
  draftSourceId: "",        // 当前填单是从哪一批回填的（同一批再点编辑不用重复拉）
  runId: "",                // 跑批视图正在看的批次 id
  runData: null,            // 该批次的详情（轮询刷新）
  runSignature: "",         // 上一次渲染用的条目状态摘要；轮询没变化就不重绘，别打断视频播放
  runTimer: null,
  runEpoch: 0,
  runPoll: null,
  runElapsedTimer: null,
  resultsDetailId: "",      // 结果页「批量」档里打开的批次 id；空 = 显示批次列表
  histories: [],            // 历史批次（结果页「批量」档的批次列表）
  deletingItems: new Set(), // 正在删除产物的条目任务 id
  deletingBatches: new Set(), // 同一批的破坏性操作互斥，切换批次后仍保留请求归属
  selectionBatchId: "",
  selectionMode: false,
  selectedItems: new Set(),
  editEpoch: 0,             // 编辑历史批次只允许最新请求写回，迟到响应不得覆盖人工输入
  converting: new Set(),    // 转换中的原条目对象；删行或重排不会把忙碌态移给其他条目
  missingItems: new Set(),  // 自检没过的条目（schemeId:itemIndex），标红拦提交
  blockedMessage: "",
  convert: { template: "", provider: "", model: "" },
  templateRows: null,       // {custom:[], catalog:[]}；一次拉取后随每次重绘同步重建选项
  templateLoading: null,
};

// 跑批卡片里视频预览的视野观察器：只给进入视野的卡片读元数据，避免整批并发。
let batchPreviewObserver = null;

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
      extra_points: Array.isArray(mapping.extra_points) ? mapping.extra_points : [],
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
        extra_points: (Array.isArray(parsed.extra_points) ? parsed.extra_points : []).filter(
          (point) => point?.kind && point?.slot),
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
  // 与单次生成共用扫描标签，保留节点号、下游端口及同名目标的区分信息。
  return row.label || `${row.node_id} · ${row.node_title || row.class_type || "节点"} · ${row.field_name}`;
}

function batchSlotSavedValue(detail, slot) {
  if (!slot) return "";
  return String((((detail?.prompt || {})[slot.node_id] || {}).inputs || {})[slot.field_name] ?? "");
}

/** 工作流里这个媒体槽是否已经固定了原文件（非空且不是 None）。 */
function mediaSlotOriginalNonEmpty(slot) {
  const value = String(slot?.default ?? "").trim();
  return Boolean(value) && value !== "None";
}

/** 这个条目的某个素材口是否被手动 ⊘ 跳过（按地址，换节点/重排都不串口）。 */
function itemSkipsPort(item, port) {
  return Boolean(item?.skips?.[slotKeyOf(port)]);
}

function toggleBatchItemSkip(scheme, itemIndex, portOrdinal) {
  const item = scheme?.items?.[itemIndex];
  const port = (scheme?.mapping?.media_ports || []).filter((entry) => entry.checked)[portOrdinal];
  if (!item || !port) return;
  const key = slotKeyOf(port);
  item.skips = item.skips || {};
  if (item.skips[key]) delete item.skips[key];
  else item.skips[key] = true;
  batch.recheckMissing();
  renderBatch();
}

function batchMappingFromWorkflow(detail) {
  // 预选建议（2026-11-05 拍板）：唯一语义正文槽 / 唯一标签命中的时长、分辨率槽；
  // 只是映射区里预先选中的值，用户随时改、随时清。素材口默认勾上必填槽。
  const mapping = {
    prompt_slot: null, duration_slot: null, resolution_slot: null,
    extra_points: [], media_ports: [],
  };
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
    // 手动 ⊘ 跳过的素材口地址 {"节点|字段": true}；独立于 files，按地址跟随素材口。
    skips: {},
    extras: {},
    ...patch,
  };
}

// 「＋增加指认」扩展写入位：第二段提示词 / 第二时长 / 第二分辨率 / 备用槽。
// 只有点了加号才出现指认行；值都存 item.extras["节点|字段"]，随条目提交。
const EXTRA_KIND_LABELS = {
  prompt: "第二段提示词",
  duration: "第二时长",
  resolution: "第二分辨率",
  spare: "备用槽",
};

function extraPointsOf(scheme) {
  // 展示口径：点过加号的行都算，哪怕还没选参数。
  return (scheme?.mapping?.extra_points || []).filter((point) => point?.kind);
}

/** 生效口径：只有已选定参数的指认行才上条目列、进提交与转换。 */
function activeExtraPoints(scheme) {
  return extraPointsOf(scheme).filter((point) => point?.slot);
}

function extraKeyOf(point) {
  return `${point.slot.node_id}|${point.slot.field_name}`;
}

function newExtraId() {
  return `bx${Date.now().toString(36)}${Math.floor(Math.random() * 1e4)}`;
}

/** 每类指认可以选的参数池：备用槽给所有可改参数，其余和主写入位同一口径。 */
function extraSlotPool(detail, kind) {
  const slots = detail?.params || [];
  if (kind === "prompt") {
    return promptSlotPool(slots);
  }
  if (kind === "duration") {
    return slots.filter((slot) => slot.field_type === "number");
  }
  if (kind === "resolution") {
    return [...slots.filter((slot) => slot.field_type === "number"),
      ...slots.filter((slot) => slot.field_type === "text" && (slot.candidates || []).length)];
  }
  // 备用槽：任何非媒体参数都行（表达式节点、开关、LoRA 清单一类都在内）。
  return slots.filter((slot) => slot.control !== "media" && !MEDIA_FIELD_TYPES.has(slot.field_type));
}

function promptSlotPool(slots) {
  const textCandidates = slots.filter((slot) => slot.field_type === "text");
  const semantic = textCandidates.filter((slot) => slot.role === "positive_prompt" || slot.control === "multiline");
  const pool = semantic.length ? semantic : textCandidates;
  const rank = (slot) => (slot.role === "positive_prompt" ? 0 : slot.control === "multiline" ? 1 : 2);
  pool.sort((a, b) => rank(a) - rank(b));
  return pool;
}

/** 该条目的某个指认值；参数还没被指认时是空串。 */
function itemExtraValue(item, key) {
  if (!key) return "";
  return String((item?.extras || {})[key] ?? "");
}

/** 完全没动过的条目：默认名、没剧本/提示词/时长/分辨率、没挂素材、条数还是 1。 */
function batchItemIsBlank(item) {
  if (!item) return true;
  const name = String(item.name || "").trim();
  if (name && !/^条目\s*\d+$/.test(name)) return false;
  if (String(item.script || "").trim()) return false;
  if (String(item.prompt || "").trim()) return false;
  if (String(item.duration ?? "").trim()) return false;
  if (String(item.resolution ?? "").trim()) return false;
  if (Math.max(1, Math.floor(Number(item.shots) || 1)) !== 1) return false;
  if (Object.values(item.extras || {}).some((value) => String(value ?? "").trim())) return false;
  return !Object.values(item.files || {}).some((ref) => ref?.path);
}

function batchSchemeId() {
  return `bs${Date.now().toString(36)}${Math.floor(Math.random() * 1e4)}`;
}

function ensureBatchDraft() {
  if (batch.draft) return batch.draft;
  batch.draft = { title: "", unload_after: false, schemes: [] };
  batch.draftSourceId = "";
  addBatchScheme();
  return batch.draft;
}

function addBatchScheme(cloneFrom = null) {
  const scheme = {
    id: batchSchemeId(),
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
  const request = {};
  scheme.detailRequest = request;
  const current = () => batchSchemeOf(scheme.id) === scheme && scheme.detailRequest === request
    && scheme.workflow_id === id;
  // 同一份工作流重读（例如回填时详情没拉到）保留固定参数；真换工作流才清空。
  const keepFixed = scheme.workflow_id === id ? JSON.parse(JSON.stringify(scheme.fixed || {})) : {};
  scheme.workflow_id = id;
  scheme.detail = null;
  scheme.mapping = null;
  scheme.fixed = keepFixed;
  scheme.loading = false;
  if (!scheme.items.length) scheme.items = [newBatchItem(1)];
  batch.blockedMessage = "";
  renderBatch();
  if (!id) return;
  scheme.loading = true;
  renderBatch();
  try {
    const payload = await api(`/api/comfyui/workflows/${encodeURIComponent(id)}`);
    if (!current()) return;
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
    // 记忆里的指认行同样逐项验证：槽位没了的整行丢掉，姐妹指认的类型照旧。
    scheme.mapping.extra_points = (scheme.mapping.extra_points || []).filter((point) =>
      EXTRA_KIND_LABELS[point?.kind]
      && batchSlotByAddress(scheme.detail, point.slot)
      // 专用指认的类型跟着参数走：工作流版本变了类型对不上就按未指认处理。
      && (point.kind === "spare"
        || extraSlotPool(scheme.detail, point.kind).some((slot) =>
          slotKeyOf(slot) === slotKeyOf(point.slot))));
    scheme.mapping.media_ports = (scheme.mapping.media_ports || []).filter((port) =>
      batchMediaCandidates(scheme.detail).some((slot) =>
        String(slot.node_id) === String(port.node_id) && String(slot.field_name) === String(port.field_name)));
    renderBatch();
  } catch (error) {
    if (!current()) return;
    scheme.loading = false;
    batch.blockedMessage = `工作流读取失败：${error.message}`;
    renderBatch();
  }
}

// ---- 填单视图渲染 ----

function renderBatch() {
  // 结果页批次详情是独立宿主：从批次列表点开时留在结果区，不跳回「批量生成」填单页。
  if (batch.resultsDetailId && comfyuiPaneName() === "tasks") {
    renderResultsBatchDetail();
    return;
  }
  const host = $("#comfyuiBatchBody");
  if (!host) return;
  const root = $("#comfyuiBatch");
  if (root) root.hidden = false;
  // 整段重绘前先断开旧卡片的视野观察，避免旧的 detached 节点一直挂着。
  if (batch.view === "run") batchPreviewObserver?.disconnect();
  host.innerHTML = batch.view === "run" ? batchRunMarkup() : batchDraftMarkup();
  if (batch.view === "run") {
    startBatchRunElapsed();
    mountBatchItemPreviews();
    return;
  }
  // 转换条是条件渲染的：重绘后补模板与模型下拉（选中值存在 batch.convert 里）。
  if (batch.convert.provider) void batchLoadConvertModels();
  void batchLoadCustomTemplates();
  updateStepMeter();
}

function renderResultsBatchDetail() {
  const host = $("#comfyuiBatchDetail");
  if (!host) return;
  // 结果区详情天生是跑批视图：不渲染填单表单。
  batch.view = "run";
  batchPreviewObserver?.disconnect();
  host.innerHTML = batchRunMarkup();
  startBatchRunElapsed();
  mountBatchItemPreviews();
  updateStepMeter();
}

// 结果页批次列表 / 详情互斥显隐由页面根上的 data-batch-detail 驱动（与
// data-results-segment 同一套口径）；打开或返回时都要同步。
function syncResultsBatchDetail() {
  const root = $("#studioPageComfyui");
  if (root) root.dataset.batchDetail = batch.resultsDetailId ? "1" : "0";
  if (batch.resultsDetailId && comfyuiPaneName() === "tasks" && comfyui.resultsSegment === "batch") {
    renderResultsBatchDetail();
  }
}

function batchDraftMarkup() {
  const draft = ensureBatchDraft();
  const parts = [];
  parts.push(`<div class="cbm-batch-head">
    <span class="cbm-field">批次名 <input type="text" data-batch-field="title" maxlength="120" value="${escapeText(draft.title)}" placeholder="例：短剧第1集"></span>
    <label class="cbm-switch"><input type="checkbox" data-batch-field="unload_after" ${draft.unload_after ? "checked" : ""}><span class="cbm-track"></span>每条出完就释放模型</label>
    <button type="button" class="cbm-btn" data-batch-action="import-storyboard">${comfyui.storyboardQueue.length ? `从分镜队列导入（${comfyui.storyboardQueue.length} 条）` : "从分镜队列导入"}</button>
    <button type="button" class="cbm-btn" data-batch-action="open-history">查看以前的批次</button>
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
  let blank = 0;
  for (const scheme of draft.schemes) {
    for (const item of scheme.items || []) {
      if (batchItemIsBlank(item)) { blank += 1; continue; }
      count += Math.max(1, Math.floor(Number(item.shots) || 1));
    }
  }
  const ready = draft.schemes.filter((scheme) => scheme.workflow_id && scheme.detail).length;
  const blankNote = blank ? `；${blank} 条空行不会提交` : "";
  return {
    count,
    note: count
      ? `共 ${count} 条（${draft.schemes.length} 个方案，其中 ${ready} 个已选好工作流），逐条排队执行，一条失败不影响其他条${blankNote}。`
      : blank
        ? `只有 ${blank} 条空行，不会提交也不会计数；先填剧本或提示词，或从分镜队列导入。`
        : "还没有条目：选好工作流后添加条目，或从分镜队列导入。",
  };
}

function batchConvertBarMarkup() {
  const profiles = state.bootstrap?.model_profiles || state.bootstrap?.providers || [];
  return `<div class="cbm-convert-bar">
    <span class="cbm-convert-title">剧本 → 提示词</span>
    <select data-batch-field="convertTemplate" aria-label="转换模板">
      <option value=""${batch.convert.template ? "" : " selected"}>MiniMax H3：自动（按工作流素材槽位选基础/参考）</option>
      <option value="${BATCH_H3_BASE_TEMPLATE}"${batch.convert.template === BATCH_H3_BASE_TEMPLATE ? " selected" : ""}>MiniMax H3：强制基础（文生/首帧/首尾帧/尾帧）</option>
      <option value="${BATCH_H3_REFERENCE_TEMPLATE}"${batch.convert.template === BATCH_H3_REFERENCE_TEMPLATE ? " selected" : ""}>MiniMax H3：参考生成 / 编辑</option>
      <optgroup label="提词库模板" data-batch-custom-templates></optgroup>
    </select>
    <select data-batch-field="convertProvider" aria-label="转换用的模型供应商">
      <option value="">请选择供应商…</option>
      ${profiles.map((profile) => `<option value="${escapeText(String(profile?.model_key || ""))}"${String(profile?.model_key || "") === batch.convert.provider ? " selected" : ""}>${escapeText(String(profile?.name || profile?.model_key || ""))}</option>`).join("")}
    </select>
    <select data-batch-field="convertModel" aria-label="转换用的模型"><option>${batch.convert.provider ? "加载模型中…" : "请先选择供应商"}</option></select>
    <button type="button" class="cbm-btn" data-batch-action="convert-all">全部转换</button>
    <span class="cbm-convert-note">填了剧本、提示词空着的条目会逐条转换，写完可手改；转换走模型，可能产生费用。自动口径：普通参考图用参考六字段，首尾帧用基础三字段。</span>
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
  const warning = scheme.restoreWarning
    ? `<p class="cbm-alert">${escapeText(scheme.restoreWarning)}</p>` : "";
  if (!scheme.workflow_id || !scheme.detail || !scheme.mapping) {
    const names = scheme.items.map((item) => escapeText(item.name)).filter(Boolean).join(" · ");
    return `<div class="${classes.join(" ")}">${head}<div class="cbm-scheme-body">
      ${warning}
      <p class="cbm-hint">选工作流后在这里指认：正文 / 时长 / 分辨率写进哪个参数、哪些装载槽做素材口。</p>
      ${names ? `<p class="cbm-hint">条目：${names}</p>` : ""}
    </div></div>`;
  }
  const body = `<div class="cbm-scheme-body">
    ${warning}
    ${batchMappingAreaMarkup(scheme)}
    ${batchFixedMarkup(scheme)}
    ${batchItemsMarkup(scheme, schemeIndex)}
    <p class="cbm-hint">剧本填了、提示词空着的，点「转换」让模型写提示词，写完可手改。素材格没选文件时：工作流已有原文件就沿用原文件（即使口是可选的），空值且确认可选才自动跳过；必填的口留空会在提交时拦下，也可以点 ⊘ 手动强制跳过这个输入。没勾选的素材口不出现在条目里，生成时按工作流保存值执行。</p>
  </div>`;
  return `<div class="${classes.join(" ")}">${head}${body}</div>`;
}

// ---- 映射区：三个写入位下拉 + 装载槽（勾选 / 排序 / 每口可换节点）----

function batchMappingAreaMarkup(scheme) {
  const detail = scheme.detail;
  const mapping = scheme.mapping;
  const slots = detail?.params || [];
  const slotKeyOfLocal = (slot) => (slot ? `${slot.node_id}|${slot.field_name}` : "");
  const savedValue = (slot) => {
    const value = batchSlotSavedValue(detail, slot);
    return value !== "" ? `当前保存值：${value}` : "当前保存值：空";
  };
  const pointSelect = (point, label, emptyLabel, candidates) => {
    const slot = mapping[`${point}_slot`];
    const key = slotKeyOfLocal(slot);
    return `<div class="cbm-map-slot"><label>${label}</label>
      <select data-batch-select="mapping" data-scheme="${scheme.id}" data-point="${point}">
        <option value="">${emptyLabel}</option>
        ${candidates.map((row) => `<option value="${escapeText(slotKeyOfLocal(row))}"${slotKeyOfLocal(row) === key ? " selected" : ""}>${escapeText(batchSlotAddressText(detail, row))}</option>`).join("")}
      </select></div>`;
  };
  const promptPool = promptSlotPool(slots);
  const numberSlots = slots.filter((slot) => slot.field_type === "number");
  const resolutionSlots = [...numberSlots, ...slots.filter((slot) => slot.field_type === "text" && (slot.candidates || []).length)];
  const durationSlot = batchSlotByAddress(detail, mapping.duration_slot);
  const resolutionSlot = batchSlotByAddress(detail, mapping.resolution_slot);
  const portRows = batchPortsMarkup(scheme);
  const extras = extraPointsOf(scheme);
  const extraRows = extras.map((point) => {
    const pairs = extraSlotPool(detail, point.kind);
    // 当前槽位不在池里（工作流改过）也要保留显示，别让下拉静默改值。
    const chosen = batchSlotByAddress(detail, point.slot);
    const pool = chosen && !pairs.some((slot) => slotKeyOfLocal(slot) === slotKeyOfLocal(chosen))
      ? [chosen, ...pairs] : pairs;
    const usedKinds = extras.filter((row) => row !== point).map((row) => row.kind);
    const kindOptions = Object.entries(EXTRA_KIND_LABELS).map(([kind, label]) =>
      `<option value="${kind}"${point.kind === kind ? " selected" : ""}${usedKinds.includes(kind) && kind !== "spare" ? " disabled" : ""}>${escapeText(label)}</option>`).join("");
    return `<div class="cbm-extra-row">
      <select class="cbm-extra-kind" data-batch-extra-kind data-scheme="${scheme.id}" data-extra="${escapeText(String(point.id || ""))}" aria-label="这行指认的类型">${kindOptions}</select>
      <select class="cbm-extra-slot" data-batch-extra-slot data-scheme="${scheme.id}" data-extra="${escapeText(String(point.id || ""))}" aria-label="这行指认写进哪个参数">
        <option value="">选一个参数…</option>
        ${pool.map((row) => `<option value="${escapeText(slotKeyOfLocal(row))}"${slotKeyOfLocal(row) === slotKeyOfLocal(point.slot) ? " selected" : ""}>${escapeText(batchSlotAddressText(detail, row))}</option>`).join("")}
      </select>
      <button type="button" class="cbm-btn tiny" data-batch-action="remove-extra" data-scheme="${scheme.id}" data-extra="${escapeText(String(point.id || ""))}">删除指认</button>
    </div>`;
  }).join("");
  return `<div class="cbm-mapping">
    ${pointSelect("prompt", "剧本/提示词写进", "不注入正文（按工作流原配置出片）", promptPool)}
    ${pointSelect("duration", "时长写进", "不逐条改时长", numberSlots)}
    ${pointSelect("resolution", "分辨率写进", "不逐条改分辨率", resolutionSlots)}
    <p class="cbm-map-hint">先把「东西往哪放」指认好：没指认的参数按工作流保存值执行，不会出现在条目里。${durationSlot ? `时长 · ${savedValue(durationSlot)} · 写入 ${escapeText(batchSlotAddressText(detail, durationSlot))}` : "时长未映射：不会替你猜哪个参数控制时长"}；${resolutionSlot ? `分辨率 · ${savedValue(resolutionSlot)}` : "分辨率未映射"}。映射按这份工作流记住，下次打开还是这样。要逐条控制两个以上参数（比如第二段提示词），点下面的「＋增加指认」。</p>
  </div>
  ${extras.length || detail ? `<div class="cbm-extras">${extraRows || '<p class="cbm-hint">还没有增加指认：点了加号才会出现一行。</p>'}
    <button type="button" class="cbm-btn tiny" data-batch-action="add-extra" data-scheme="${scheme.id}" ${detail ? "" : "disabled"}>＋ 增加指认</button>
  </div>` : ""}
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
    // 三态口径与单次生成一致：原文件非空沿用；空值 optional 自动跳过；否则要选文件。
    const keptOriginal = mediaSlotOriginalNonEmpty(slot);
    const note = keptOriginal
      ? "沿用原文件"
      : slot?.media_optional === false
        ? "必填"
        : slot?.media_optional === true
          ? "可选，留空自动跳过"
          : "需选择文件";
    return `<div class="cbm-media-slot${port.checked ? "" : " unchecked"}">
      <input type="checkbox" data-batch-port-check="${index}" data-scheme="${scheme.id}" ${port.checked ? "checked" : ""} aria-label="启用这个素材口">
      <span class="cbm-no">${numbers[index]}</span>
      <select class="cbm-nodesel" data-batch-select="port-node" data-scheme="${scheme.id}" data-port="${index}" aria-label="这个素材口对应哪个节点">
        ${sameKind.map((row) => `<option value="${escapeText(slotKeyOf(row))}"${slot && slotKeyOf(row) === slotKeyOf(slot) ? " selected" : ""}>${escapeText(batchSlotAddressText(detail, row))}</option>`).join("")}
      </select>
      <span class="cbm-kw">${port.checked ? `${kindFull[slot?.media || "image"] || "媒体"} · ${note}` : "未勾选 · 不出现在条目里"}</span>
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
  // 指认行选中的槽位走条目列，不再重复出现在固定参数里。
  for (const point of activeExtraPoints(scheme)) {
    skipKeys.add(slotKeyOf(point.slot));
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
      const choices = (slot.candidates || []).map((candidate) => String(candidate));
      const current = String(value);
      const extra = current && !choices.includes(current)
        ? `<option value="${escapeText(current)}" selected>${escapeText(current)}（原值）</option>`
        : "";
      control = `<select data-batch-fixed="${escapeText(key)}" data-scheme="${scheme.id}">
        ${extra}
        ${(slot.candidates || []).map((candidate) => `<option value="${escapeText(String(candidate))}"${String(value) === String(candidate) ? " selected" : ""}>${escapeText(String(candidate))}</option>`).join("")}
      </select>`;
    } else {
      control = `<input type="${slot.field_type === "number" || slot.field_type === "seed" ? "number" : "text"}" step="any"
        data-batch-fixed="${escapeText(key)}" data-scheme="${scheme.id}" value="${escapeText(value)}">`;
    }
    const label = slot.label || `${slot.node_id} · ${slot.field_name}`;
    return `<div class="cbm-fixed-field"><label>${escapeText(label)}${slot.field_type === "seed" ? "（留空 = 每次随机）" : ""}</label>${control}</div>`;
  }).join("");
  return `<div class="cbm-fixed-bar" data-fixed-bar="${escapeText(scheme.id)}">
      固定参数（本方案每条相同）：<span class="cbm-fixed-summary" data-fixed-summary>${batchFixedAppliedMarkup(scheme)}</span>
      <button type="button" class="cbm-btn tiny" data-batch-action="toggle-fixed" data-scheme="${scheme.id}">展开固定参数</button>
    </div>
    <div class="cbm-fixed-detail${scheme.fixedOpen ? " open" : ""}">
      ${rows}
      <p class="cbm-map-hint">同一工作流想换模型配方，不用改这份方案——「复制方案」再来一份，这里改成另一套即可。</p>
    </div>`;
}

/** 固定参数栏的已改值摘要；编辑时原地刷新，不重绘整块表单。 */
function batchFixedAppliedMarkup(scheme) {
  const keys = Object.keys(scheme.fixed || {});
  if (!keys.length) return '<span>其余按工作流保存值</span>';
  return keys.map((key) => {
    const slot = (scheme.detail?.params || []).find((row) => slotKeyOf(row) === key);
    const label = slot?.label || key;
    const value = scheme.fixed[key];
    return `<span class="cbm-kv">${escapeText(label)} ${escapeText(typeof value === "boolean" ? (value ? "开" : "关") : String(value))}</span>`;
  }).join("");
}

/** 固定参数控件 → scheme.fixed。数字/种子清空视为还原工作流值；布尔存真布尔。 */
function batchSetFixedValue(scheme, input) {
  const key = String(input?.dataset?.batchFixed || "");
  if (!scheme || !key) return;
  const slot = (scheme.detail?.params || []).find((row) => slotKeyOf(row) === key);
  const raw = String(input.value ?? "");
  if ((slot?.field_type === "number" || slot?.field_type === "seed") && !raw.trim()) {
    delete scheme.fixed[key];
  } else if (slot?.field_type === "bool") {
    scheme.fixed[key] = raw === "true";
  } else {
    scheme.fixed[key] = raw;
  }
  batchRefreshFixedBar(scheme);
}

function batchRefreshFixedBar(scheme) {
  const bars = document.querySelectorAll("#comfyuiBatchBody .cbm-fixed-bar[data-fixed-bar]");
  const host = Array.from(bars).find((node) => String(node.dataset.fixedBar) === String(scheme.id));
  const summary = host?.querySelector("[data-fixed-summary]");
  if (summary) summary.innerHTML = batchFixedAppliedMarkup(scheme);
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
  // 指认列跟在同类主列后面：第二段提示词接提示词，第二时长接时长，其余排条数前。
  const extras = activeExtraPoints(scheme);
  const kindExtras = (kind) => extras.filter((point) => point.kind === kind);
  const extrasMarkup = (kind) => kindExtras(kind).map((point) =>
    `<th title="${escapeText(batchSlotAddressText(scheme.detail, point.slot))}">${escapeText(EXTRA_KIND_LABELS[point.kind] || "备用槽")}</th>`).join("");
  const rows = scheme.items.map((item, itemIndex) =>
    batchItemRowMarkup(scheme, item, itemIndex, ports)).join("");
  const columnCount = 7 + kindExtras("prompt").length + kindExtras("duration").length
    + extras.filter((point) => point.kind === "resolution" && point.slot).length
    + extras.filter((point) => point.kind === "spare" && point.slot).length + ports.length;
  return `<div class="cbm-items-wrap"><table class="cbm-items">
    <thead><tr><th>名称</th>${portHeaders}<th>剧本</th><th>提示词<button type="button" class="cbm-th-clear" data-batch-action="clear-prompts" data-scheme="${scheme.id}" title="清空这个方案所有条目的提示词（含指认出的第二段），剧本栏不动">清空</button></th>${extrasMarkup("prompt")}<th>时长</th>${extrasMarkup("duration")}<th>分辨率</th>${extrasMarkup("resolution")}${extrasMarkup("spare")}<th>条数</th><th>操作</th></tr></thead>
    <tbody>${rows || `<tr><td colspan="${columnCount}" class="cbm-hint">还没有条目。</td></tr>`}</tbody>
    <tfoot><tr><td colspan="${columnCount}"><button type="button" class="cbm-add-item" data-batch-action="add-item" data-scheme="${scheme.id}">＋ 添加一条</button></td></tr></tfoot>
  </table></div>`;
}

// 指认列的一个单元格：按指认类型出控件（第二段提示词是叙述文本，备用槽跟参数类型走）。
function batchExtraCellMarkup(scheme, item, itemIndex, point) {
  const detail = scheme.detail;
  const slot = batchSlotByAddress(detail, point.slot);
  const key = extraKeyOf(point);
  const value = itemExtraValue(item, key);
  const bind = `<td data-batch-extra="${escapeText(String(point.id || ""))}">`;
  if (point.kind === "prompt") {
    return `${bind}<textarea class="cbm-extra-prompt" rows="2" placeholder="第二段交给工作流的提示词" data-batch-item="extras" data-extra-key="${escapeText(key)}" data-scheme="${scheme.id}" data-item="${itemIndex}">${escapeText(value)}</textarea></td>`;
  }
  if (point.kind === "duration") {
    return `${bind}<input type="number" min="0" step="any" style="width:76px" placeholder="按工作流" data-batch-item="extras" data-extra-key="${escapeText(key)}" data-scheme="${scheme.id}" data-item="${itemIndex}" value="${escapeText(value)}"></td>`;
  }
  if (point.kind === "resolution" && (slot?.candidates || []).length) {
    const choices = (slot.candidates || []).map((candidate) => String(candidate));
    const extra = value && !choices.includes(value)
      ? `<option value="${escapeText(value)}" selected>${escapeText(value)}（原值）</option>` : "";
    return `${bind}<select data-batch-item="extras" data-extra-key="${escapeText(key)}" data-scheme="${scheme.id}" data-item="${itemIndex}"><option value="">按工作流</option>${extra}${choices.map((choice) => `<option value="${escapeText(choice)}"${choice === value ? " selected" : ""}>${escapeText(choice)}</option>`).join("")}</select></td>`;
  }
  if (slot?.field_type === "bool") {
    return `${bind}<select data-batch-item="extras" data-extra-key="${escapeText(key)}" data-scheme="${scheme.id}" data-item="${itemIndex}"><option value="">按工作流</option><option value="true"${String(value) === "true" ? " selected" : ""}>开</option><option value="false"${String(value) === "false" ? " selected" : ""}>关</option></select></td>`;
  }
  if ((slot?.candidates || []).length) {
    const choices = (slot.candidates || []).map((candidate) => String(candidate));
    const extra = value && !choices.includes(value)
      ? `<option value="${escapeText(value)}" selected>${escapeText(value)}（原值）</option>` : "";
    return `${bind}<select data-batch-item="extras" data-extra-key="${escapeText(key)}" data-scheme="${scheme.id}" data-item="${itemIndex}">${extra}${choices.map((choice) => `<option value="${escapeText(choice)}"${choice === value ? " selected" : ""}>${escapeText(choice)}</option>`).join("")}</select></td>`;
  }
  const numberish = slot?.field_type === "number" || slot?.field_type === "seed";
  const multiline = slot?.control === "multiline";
  const control = multiline
    ? `<textarea class="cbm-extra-prompt" rows="2" data-batch-item="extras" data-extra-key="${escapeText(key)}" data-scheme="${scheme.id}" data-item="${itemIndex}">${escapeText(value)}</textarea>`
    : `<input type="${numberish ? "number" : "text"}" step="any"${numberish ? ' style="width:96px"' : ""} placeholder="按工作流" data-batch-item="extras" data-extra-key="${escapeText(key)}" data-scheme="${scheme.id}" data-item="${itemIndex}" value="${escapeText(value)}">`;
  return `${bind}${control}</td>`;
}

/** 该条目转换真正会带几张图：已勾选的图片口里挂了原图的那几只；>6 张后端直接拒绝。 */
function batchItemConversionImageCount(scheme, item) {
  const ports = (scheme.mapping?.media_ports || []).filter((port) => port.checked);
  let count = 0;
  ports.forEach((port, portIndex) => {
    const slot = batchSlotByAddress(scheme.detail, port);
    if ((slot?.media || "image") !== "image") return;
    if (itemSkipsPort(item, port)) return;
    if (item?.files?.[String(portIndex)]?.path) count += 1;
  });
  return count;
}

function batchConversionImageHint(scheme, item) {
  const count = batchItemConversionImageCount(scheme, item);
  if (count > 6) return { text: `${count} 图 · 超上限`, over: true };
  if (count > 0) return { text: `带 ${count} 图`, over: false };
  return { text: "无图", over: false };
}

function batchItemRowMarkup(scheme, item, itemIndex, ports) {
  const missing = batch.missingItems.has(`${scheme.id}:${itemIndex}`);
  const converting = batch.converting.has(item);
  // 转换条件：有剧本可转，且（还出不来正文，或还没指认出/没填到第二段）。
  const promptExtra = activeExtraPoints(scheme).find((point) => point.kind === "prompt");
  const promptFilled = Boolean(String(item.prompt || "").trim());
  const prompt2Filled = Boolean(promptExtra && itemExtraValue(item, extraKeyOf(promptExtra)).trim());
  const convertReady = Boolean(String(item.script || "").trim())
    && (!promptFilled || (Boolean(promptExtra) && !prompt2Filled))
    && !converting;
  const imageHint = batchConversionImageHint(scheme, item);
  const title = batchItemConversionImageCount(scheme, item) > 6
    ? "超过 6 张图的转换会被拒绝：减少本条启用的图片素材口" : "转换实际发给视觉模型的图片数（视频/音频不发送）";
  // 分辨率控件需要当前条目的索引，必须与条目行处于同一作用域。
  const resolutionControl = () => {
    const slot = batchSlotByAddress(scheme.detail, scheme.mapping?.resolution_slot);
    if (slot && (slot.candidates || []).length) {
      const choices = slot.candidates.map((candidate) => String(candidate));
      // 回填的旧值可能已不在工作流的候选里：保留它并标出来，不让下拉静默改值。
      const legacy = String(item.resolution || "").trim();
      const extra = legacy && !choices.includes(legacy)
        ? `<option value="${escapeText(legacy)}" selected>${escapeText(legacy)}（原值）</option>`
        : "";
      return `<select data-batch-item="resolution" data-scheme="${scheme.id}" data-item="${itemIndex}">
        <option value="">按工作流</option>
        ${extra}
        ${slot.candidates.map((candidate) => `<option value="${escapeText(String(candidate))}"${String(item.resolution) === String(candidate) ? " selected" : ""}>${escapeText(String(candidate))}</option>`).join("")}
      </select>`;
    }
    return `<input type="${slot?.field_type === "number" ? "number" : "text"}" step="any" placeholder="按工作流"
      data-batch-item="resolution" data-scheme="${scheme.id}" data-item="${itemIndex}" value="${escapeText(item.resolution)}">`;
  };
  const tiles = ports.map((port, portIndex) => {
    const slot = batchSlotByAddress(scheme.detail, port);
    const kind = slot?.media || "image";
    const record = item.files[String(portIndex)];
    const skipped = itemSkipsPort(item, port);
    const skipLabel = skipped ? "取消跳过，恢复此前状态" : "跳过这个输入";
    const skipButton = `<button type="button" class="cbm-tile-skip${skipped ? " active" : ""}" data-batch-action="toggle-skip" data-scheme="${scheme.id}" data-item="${itemIndex}" data-port="${portIndex}" title="${skipLabel}" aria-label="${skipLabel}">⊘</button>`;
    if (skipped) {
      // 跳过态不再画媒体缩略图：一个 ⊘ 开关 + 说明 +（有文件时）清除入口，
      // 避免「紫色媒体块里一个 ⊘、旁边又一个 ⊘」的双图标歧义。
      const subText = record
        ? `原选择：${record.name}`
        : mediaSlotOriginalNonEmpty(slot)
          ? `恢复后沿用：${slot.default}` : "本次不提交这个素材口";
      return `<td><span class="cbm-mtile is-skipped cbm-skip-tile" data-batch-tile data-batch-skipped="1" data-scheme="${scheme.id}" data-item="${itemIndex}" data-port="${portIndex}" data-kind="${kind}" title="已跳过这个输入；点 ⊘ 恢复">
        ${skipButton}
        <span class="cbm-skip-text"><span class="cbm-skip-title">已跳过这个输入</span><span class="cbm-skip-sub" title="${escapeText(subText)}">${escapeText(subText)}</span></span>
        ${record ? `<button type="button" class="cbm-tile-clear" data-batch-action="clear-file" data-scheme="${scheme.id}" data-item="${itemIndex}" data-port="${portIndex}" aria-label="清除这个文件">✕</button>` : ""}
      </span></td>`;
    }
    if (record) {
      const thumb = kind === "image"
        ? `<img src="${escapeText(attachmentThumbUrl(record))}" alt="" loading="lazy">`
        : `<span class="cbm-tile-ico">${kind === "video" ? "▶" : "♪"}</span>`;
      return `<td><span class="cbm-mtile" data-batch-tile data-batch-skipped="" data-scheme="${scheme.id}" data-item="${itemIndex}" data-port="${portIndex}" data-kind="${kind}" title="点选 / 拖拽可换；⊘ 跳过这个输入">
        <span class="cbm-thumb ${kind}">${thumb}</span>
        <span class="cbm-mname">${escapeText(record.name || "")}<span class="cbm-tile-actions"><button type="button" class="cbm-tile-clear" data-batch-action="clear-file" data-scheme="${scheme.id}" data-item="${itemIndex}" data-port="${portIndex}" aria-label="清除这个文件">✕</button>${skipButton}</span></span>
      </span></td>`;
    }
    // 没选新文件：原文件非空沿用（即使 optional）；空值且确认 optional 自动跳过；
    // 必填/无法确认的保持选择文件提示——用户仍可手动 ⊘ 强制跳过。
    const keptOriginal = mediaSlotOriginalNonEmpty(slot);
    const autoSkip = !keptOriginal && slot?.media_optional === true;
    const required = !keptOriginal && slot?.media_optional === false;
    const labelText = keptOriginal ? "沿用原文件"
      : autoSkip ? "留空 · 自动跳过" : required ? "必填 · 点选/拖入" : "点选/拖入";
    return `<td><span class="cbm-empty-slot${required && missing ? " is-missing" : ""}${keptOriginal ? " is-kept" : ""}" data-batch-tile data-batch-skipped="" data-scheme="${scheme.id}" data-item="${itemIndex}" data-port="${portIndex}" data-kind="${kind}" title="点选 / 拖入换素材；⊘ 跳过这个输入"><span class="cbm-slot-label">${labelText}</span>${skipButton}</span></td>`;
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
    ${activeExtraPoints(scheme).filter((point) => point.kind === "prompt").map((point) => batchExtraCellMarkup(scheme, item, itemIndex, point)).join("")}
    <td>${durationControl}</td>
    ${activeExtraPoints(scheme).filter((point) => point.kind === "duration").map((point) => batchExtraCellMarkup(scheme, item, itemIndex, point)).join("")}
    <td>${resolutionControl()}</td>
    ${activeExtraPoints(scheme).filter((point) => (point.kind === "resolution" || point.kind === "spare")).map((point) => batchExtraCellMarkup(scheme, item, itemIndex, point)).join("")}
    <td><input type="number" min="1" max="200" style="width:56px" data-batch-item="shots" data-scheme="${scheme.id}" data-item="${itemIndex}" value="${escapeText(String(item.shots))}"></td>
    <td class="cbm-row-act">
      <span class="cbm-convert-hint${imageHint.over ? " over" : ""}" title="${title}">${escapeText(imageHint.text)}</span>
      <button type="button" class="cbm-btn tiny" data-batch-action="convert-item" data-scheme="${scheme.id}" data-item="${itemIndex}" ${convertReady ? "" : "disabled"}>转换</button>
      <button type="button" class="cbm-btn tiny" data-batch-action="copy-item" data-scheme="${scheme.id}" data-item="${itemIndex}">复制</button>
      <button type="button" class="cbm-btn tiny" data-batch-action="remove-item" data-scheme="${scheme.id}" data-item="${itemIndex}">删</button>
    </td>
  </tr>`;
}

// ---- 批量交互（容器级委托）----

function batchSchemeOf(id) {
  return (batch.draft?.schemes || []).find((scheme) => scheme.id === id) || null;
}

// 提示词列头的一键清除：只清「最终提示词」和指认出的第二段，剧本栏保留。
function clearBatchPrompts(scheme) {
  const promptExtras = activeExtraPoints(scheme).filter((point) => point.kind === "prompt");
  const touched = (scheme?.items || []).filter((item) =>
    String(item.prompt || "").trim()
    || promptExtras.some((point) => itemExtraValue(item, extraKeyOf(point)).trim()));
  if (!touched.length) { toast("提示词已经是空的"); return; }
  if (!window.confirm(`清空这个方案的全部 ${touched.length} 条提示词？剧本栏不动。`)) return;
  for (const item of scheme.items || []) {
    item.prompt = "";
    for (const point of promptExtras) delete item.extras?.[extraKeyOf(point)];
  }
  renderBatch();
  toast(`已清空 ${touched.length} 条提示词`);
}

function onBatchClick(event) {
  const target = event.target.closest("[data-batch-action]");
  const tile = event.target.closest("[data-batch-tile]");
  if (!target && tile) {
    // 已跳过的素材格不响应点选（避免误触又弹文件框）；点 ⊘ 恢复后再选。
    if (tile.dataset.batchSkipped === "1") return;
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
  if (action === "select-items") {
    syncBatchSelection(batch.runData);
    batch.selectionMode = true;
    renderBatch();
  } else if (action === "select-all-items") {
    syncBatchSelection(batch.runData);
    batch.selectedItems = new Set(batchDeletableItems().map((item) => String(item.task_id)));
    renderBatch();
  } else if (action === "cancel-selection") {
    batch.selectionMode = false;
    batch.selectedItems.clear();
    renderBatch();
  } else if (action === "delete-selected-items") {
    void deleteSelectedBatchItems();
  } else if (action === "add-scheme") {
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
  } else if (action === "clear-prompts" && scheme) {
    clearBatchPrompts(scheme);
  } else if (action === "add-extra" && scheme) {
    // ＋增加指认：点了才出现一行；类型先给备用槽，用户再选参数。
    if (!scheme.detail) { toast("先选好工作流再增加指认"); return; }
    scheme.mapping.extra_points = (scheme.mapping.extra_points || []).concat(
      [{ id: newExtraId(), kind: "spare", slot: null }]);
    renderBatch();
  } else if (action === "remove-extra" && scheme) {
    const pointId = String(target.dataset.extra || "");
    const points = scheme.mapping.extra_points || [];
    const leaving = points.find((point) => String(point.id) === pointId);
    scheme.mapping.extra_points = points.filter((point) => String(point.id) !== pointId);
    if (leaving?.slot) {
      const key = extraKeyOf(leaving);
      for (const item of scheme.items) delete item.extras?.[key];
    }
    batchRememberMappingQuietly(scheme);
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
  } else if ((action === "port-up" || action === "port-down") && scheme) {
    const index = Number(target.dataset.port);
    const next = index + (action === "port-up" ? -1 : 1);
    const ports = scheme.mapping?.media_ports || [];
    if (index >= 0 && index < ports.length && next >= 0 && next < ports.length) {
      changeBatchMediaPorts(scheme, () => { [ports[index], ports[next]] = [ports[next], ports[index]]; });
    }
  } else if (action === "clear-file" && scheme && itemIndex >= 0) {
    event.stopPropagation();
    const item = scheme.items[itemIndex];
    delete item.files[String(target.dataset.port)];
    renderBatch();
  } else if (action === "toggle-skip" && scheme && itemIndex >= 0) {
    // ⊘ 手动强制跳过 / 取消跳过：不改 files，也不受 optional 与必填声明限制。
    event.stopPropagation();
    toggleBatchItemSkip(scheme, itemIndex, Number(target.dataset.port));
  } else if (action === "import-storyboard") {
    importBatchStoryboard();
  } else if (action === "open-history") {
    // 历史统一在「结果 → 批量」：这里只负责导航，不在填单区再放一份下拉。
    setComfyuiResultsSegment("batch");
    setComfyuiPane("tasks");
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
  } else if (action === "edit-batch") {
    // 历史批次也走这里：先把这一批的条目回填到填单区，再渲染。
    void editCurrentBatch();
  } else if (action === "clear-batch-outputs") {
    void clearBatchOutputs();
  } else if (action === "delete-batch") {
    void deleteCurrentBatch();
  } else if (action === "delete-item" && target.dataset.taskId) {
    void deleteBatchItem(String(target.dataset.taskId), target);
  } else if (action === "reveal-item-folder" && target.dataset.taskId) {
    // 条目产物操作 = 打开这条任务自己的文件夹，不展开产物列表、不开灯箱。
    void revealBatchItemFolder(String(target.dataset.taskId));
  } else if (action === "back-to-batch-list") {
    // 结果页详情返回列表：只退视图、停刷新，不重建任何持久时间。
    batch.resultsDetailId = "";
    batch.runSignature = "";
    stopBatchPolling();
    syncResultsBatchDetail();
    void loadBatchHistories();
  } else if (action === "cancel-item" && target.dataset.taskId) {
    void cancelBatchItem(String(target.dataset.taskId));
  } else if (action === "retry-item" && target.dataset.taskId) {
    void retryBatchItem(String(target.dataset.taskId));
  }
}

function onBatchChange(event) {
  const target = event.target;
  if (target.matches("[data-batch-task-check]")) {
    syncBatchSelection(batch.runData);
    const id = String(target.dataset.batchTaskCheck || "");
    if (!batch.deletingBatches.has(batch.runId) && batchDeletableItems().some((item) => String(item.task_id) === id)) {
      if (target.checked) batch.selectedItems.add(id);
      else batch.selectedItems.delete(id);
      renderBatch();
    }
    return;
  }
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
    batch.convert.model = "";
    void batchLoadConvertModels();
    return;
  }
  if (target.matches('[data-batch-field="convertModel"]')) {
    batch.convert.model = String(target.value || "");
    return;
  }
  if (!scheme) return;
  if (target.matches("[data-batch-fixed]")) {
    batchSetFixedValue(scheme, target);
    return;
  }
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
  if (target.matches("[data-batch-extra-kind]")) {
    const row = (scheme.mapping.extra_points || []).find(
      (item) => String(item?.id) === String(target.dataset.extra || ""));
    if (row) {
      const oldKey = row.slot ? extraKeyOf(row) : "";
      row.kind = String(target.value || "spare");
      // 池子换了：旧槽位不在新池里就按未指认处理，条目里的旧补充值也不留。
      if (row.slot && !extraSlotPool(scheme.detail, row.kind).some(
          (slot) => slotKeyOf(slot) === slotKeyOf(row.slot))) {
        row.slot = null;
        for (const item of scheme.items) delete item.extras?.[oldKey];
      }
      batchRememberMappingQuietly(scheme);
      renderBatch();
    }
    return;
  }
  if (target.matches("[data-batch-extra-slot]")) {
    const row = (scheme.mapping.extra_points || []).find(
      (item) => String(item?.id) === String(target.dataset.extra || ""));
    if (row) {
      const oldKey = row.slot ? extraKeyOf(row) : "";
      const value = String(target.value || "");
      const [nodeId, ...rest] = value.split("|");
      row.slot = value ? { node_id: nodeId, field_name: rest.join("|") } : null;
      if (oldKey && (!row.slot || extraKeyOf(row) !== oldKey)) {
        for (const item of scheme.items) delete item.extras?.[oldKey];
      }
      batchRememberMappingQuietly(scheme);
      renderBatch();
    }
    return;
  }
  if (target.matches('[data-batch-select="port-node"]')) {
    const portIndex = Number(target.dataset.port);
    const value = String(target.value || "");
    const [nodeId, ...rest] = value.split("|");
    const port = (scheme.mapping.media_ports || [])[portIndex];
    if (port && value) {
      changeBatchMediaPorts(scheme, () => {
        port.node_id = nodeId;
        port.field_name = rest.join("|");
      });
    }
    return;
  }
  const checkbox = target.matches("[data-batch-port-check]");
  if (checkbox) {
    const portIndex = Number(target.dataset.batchPortCheck);
    const port = (scheme.mapping.media_ports || [])[portIndex];
    if (port) {
      changeBatchMediaPorts(scheme, () => { port.checked = target.checked; });
    }
  }
}

function changeBatchMediaPorts(scheme, change) {
  const before = (scheme.mapping?.media_ports || []).filter((port) => port.checked);
  const records = new Map(scheme.items.map((item) => [item, new Map(before.map((port, ordinal) =>
    [port, { address: slotKeyOf(port), file: item.files?.[String(ordinal)] }]))]));
  change();
  const after = (scheme.mapping?.media_ports || []).filter((port) => port.checked);
  const keptKeys = new Set(after.map((port) => slotKeyOf(port)));
  for (const item of scheme.items) {
    const files = {};
    after.forEach((port, ordinal) => {
      const previous = records.get(item)?.get(port);
      // 排序只改编号；禁用或换节点只移除对应口，其余文件仍跟随原素材口。
      if (previous?.file && previous.address === slotKeyOf(port)) files[String(ordinal)] = previous.file;
    });
    item.files = files;
    // 跳过按地址保存：仍启用的口保留跳过，禁用/换节点留下的失效地址清掉。
    item.skips = item.skips || {};
    for (const key of Object.keys(item.skips)) {
      if (!keptKeys.has(key)) delete item.skips[key];
    }
  }
  batch.recheckMissing();
  batchRememberMappingQuietly(scheme);
  renderBatch();
}

function onBatchInput(event) {
  const target = event.target;
  const scheme = batchSchemeOf(target.dataset.scheme || "");
  if (target.matches("[data-batch-fixed]")) {
    if (scheme) batchSetFixedValue(scheme, target);
    return;
  }
  const itemField = target.dataset.batchItem;
  if (!scheme || !itemField) return;
  const itemIndex = Number(target.dataset.item);
  const item = scheme.items[itemIndex];
  if (!item) return;
  if (itemField === "shots") item.shots = Math.max(1, Math.floor(Number(target.value) || 1));
  else if (itemField === "extras") {
    const key = String(target.dataset.extraKey || "");
    if (!key) return;
    item.extras = item.extras || {};
    item.extras[key] = String(target.value ?? "");
  } else item[itemField] = String(target.value ?? "");
  if (itemField === "script" || itemField === "prompt" || itemField === "extras") {
    const promptExtra = activeExtraPoints(scheme).find((point) => point.kind === "prompt");
    const promptFilled = Boolean(String(item.prompt || "").trim());
    const prompt2Filled = Boolean(promptExtra && itemExtraValue(item, extraKeyOf(promptExtra)).trim());
    const convertButton = target.closest("tr")?.querySelector('[data-batch-action="convert-item"]');
    if (convertButton) convertButton.disabled = !String(item.script || "").trim()
      || (promptFilled && !(promptExtra && !prompt2Filled))
      || batch.converting.has(batchItemReference(scheme, itemIndex));
    const host = $("#comfyuiBatchBody");
    const bar = host?.querySelector(".cbm-convert-bar");
    const hasScript = batch.draft.schemes.some((row) => row.items.some((entry) => String(entry.script || "").trim()));
    if (!hasScript) bar?.remove();
    else if (!bar) {
      // 输入时只补转换条，保留正在编辑的 textarea 与光标。
      host?.querySelector(".cbm-batch-head")?.insertAdjacentHTML("afterend", batchConvertBarMarkup());
      if (batch.convert.provider) void batchLoadConvertModels();
      void batchLoadCustomTemplates();
    }
  }
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
  const port = (scheme.mapping?.media_ports || []).filter((entry) => entry.checked)[portIndex];
  const item = scheme.items[Number(tile.dataset.item)];
  const detail = scheme.detail;
  const address = port ? slotKeyOf(port) : "";
  const slot = batchSlotByAddress(detail, port);
  const kind = tile.dataset.kind || slot?.media || "image";
  const input = document.createElement("input");
  input.type = "file";
  input.accept = MEDIA_ACCEPT[kind] || "";
  input.addEventListener("change", () => {
    const file = input.files?.[0];
    if (file) void batchUploadTiles([{ tile, file, scheme, item, port, detail, address }]);
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
  // 全部目标在首个 await 前固定；旧 tile 的序号不能在后续上传时重新指向另一行/口。
  const queued = pairs.map((pair) => {
    const { tile } = pair;
    const scheme = pair.scheme || batchSchemeOf(tile.dataset.scheme || "");
    const item = pair.item || scheme?.items[Number(tile.dataset.item)];
    const port = pair.port || (scheme?.mapping?.media_ports || []).filter((entry) => entry.checked)[Number(tile.dataset.port)];
    return { ...pair, scheme, item, port, detail: pair.detail || scheme?.detail,
      address: pair.address || (port ? slotKeyOf(port) : "") };
  });
  let saved = 0;
  try {
    for (const { file, scheme, item, port, detail, address } of queued) {
      const current = () => scheme && batchSchemeOf(scheme.id) === scheme && scheme.items.includes(item)
        && scheme.detail === detail && port?.checked && slotKeyOf(port) === address
        && (scheme.mapping?.media_ports || []).includes(port);
      if (!current()) continue;
      const form = new FormData();
      form.append("file", file, file.name);
      try {
        const payload = await api("/api/comfyui/media", { method: "POST", body: form });
        const ordinal = (scheme.mapping?.media_ports || []).filter((entry) => entry.checked).indexOf(port);
        if (current() && ordinal >= 0) {
          item.files[String(ordinal)] = {
            path: String(payload.path || ""),
            name: String(payload.name || file.name),
            thumb_path: String(payload.thumb_path || ""),
          };
          // 换上新文件即恢复这个口：取消此前的手动 ⊘。
          item.skips = item.skips || {};
          delete item.skips[address];
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

async function batchLoadConvertModels() {
  const select = document.querySelector('[data-batch-field="convertModel"]');
  if (!select) return;
  const providerKey = batch.convert.provider;
  if (!providerKey) {
    select.replaceChildren(new Option("请先选择供应商", ""));
    select.disabled = true;
    return;
  }
  select.replaceChildren(new Option("加载模型中…", ""));
  select.disabled = true;
  if (!(state.providerModelCatalogs[providerKey] || []).length) {
    try {
      const result = await api("/api/providers/models", { method: "POST", body: { model_key: providerKey } });
      if (result?.error) throw new Error(result.error);
      state.providerModelCatalogs[providerKey] = Array.isArray(result?.models) ? result.models : [];
    } catch (_) {
      state.providerModelCatalogs[providerKey] = [];
    }
  }
  if (batch.convert.provider !== providerKey || !select.isConnected) return;
  const models = state.providerModelCatalogs[providerKey] || [];
  const validIds = models.map((entry) => String(typeof entry === "object" ? (entry.id ?? entry.model ?? entry.name) : entry));
  if (!validIds.includes(batch.convert.model)) batch.convert.model = "";
  select.replaceChildren(new Option("请选择模型…", ""), ...models.map((entry) => {
    const id = typeof entry === "object" ? (entry.id ?? entry.model ?? entry.name) : entry;
    return new Option(entry?.name || id, id);
  }));
  select.disabled = !models.length;
  select.value = batch.convert.model;
}

// 提词库内置模板按模块分组；值加 catalog: 前缀，请求里还原成 item_id。
// 模块顺序与显示名跟提词库分类一致，避免同一个模块两套叫法。
const BATCH_TEMPLATE_MODULES = [
  ["vs", "文生图"], ["h3", "视频"], ["vi", "反推"], ["ec", "电商"],
  ["global", "通用"], ["script", "剧本"], ["game", "游戏原画"],
];

function batchFillTemplateOptions() {
  const select = document.querySelector('[data-batch-field="convertTemplate"]');
  const group = select?.querySelector("[data-batch-custom-templates]");
  if (!group || !batch.templateRows) return;
  const nodes = [];
  const customRows = batch.templateRows.custom || [];
  if (customRows.length) {
    const customGroup = document.createElement("optgroup");
    customGroup.label = "提词库自定义模板";
    customGroup.append(...customRows.map((item) =>
      new Option(String(item.title || item.id), String(item.id))));
    nodes.push(customGroup);
  }
  for (const [module, label] of BATCH_TEMPLATE_MODULES) {
    const rows = (batch.templateRows.catalog || []).filter((item) => item.module === module);
    if (!rows.length) continue;
    const moduleGroup = document.createElement("optgroup");
    moduleGroup.label = `提词库 · ${label}`;
    moduleGroup.append(...rows.map((item) =>
      new Option(String(item.title || item.id), `catalog:${item.id}`)));
    nodes.push(moduleGroup);
  }
  if (nodes.length) group.replaceWith(...nodes);
  else group.remove();
  select.value = batch.convert.template;
}

async function batchLoadCustomTemplates() {
  if (!document.querySelector('[data-batch-custom-templates]')) return;
  if (!batch.templateRows) {
    if (!batch.templateLoading) {
      batch.templateLoading = Promise.all([
        api("/api/studio/custom").catch(() => ({ items: [] })),
        // 模板与规则都能当转换写法；词条与示例不是指令，列进来只会把下拉拖长。
        api("/api/studio/catalog/items?type=template").catch(() => ({ items: [] })),
        api("/api/studio/catalog/items?type=rule").catch(() => ({ items: [] })),
      ]).then(([custom, templates, rules]) => {
        const seen = new Set();
        const catalog = [...(templates?.items || []), ...(rules?.items || [])].filter((item) => {
          // 两条 H3 内置档已有专用选项，别在下拉里重复出现。
          if (!item?.id || seen.has(item.id)
              || item.id === "h3-007-h3-i2va" || item.id === "h3-014-h3-ref2va") return false;
          seen.add(item.id);
          return true;
        });
        batch.templateRows = {
          custom: (custom?.items || []).filter((item) => item?.id),
          catalog,
        };
      }).catch(() => { batch.templateRows = { custom: [], catalog: [] }; });
    }
    await batch.templateLoading;
  }
  batchFillTemplateOptions();
}

function batchItemReference(scheme, itemIndex) {
  return scheme.items[itemIndex];
}

function batchConversionSnapshot(scheme, item) {
  return JSON.stringify({ workflow: scheme.workflow_id, script: item.script, prompt: item.prompt,
    extras: item.extras || {}, files: item.files, skips: item.skips || {},
    duration: item.duration, resolution: item.resolution,
    mapping: scheme.mapping, fixed: scheme.fixed, convert: batch.convert });
}

// 转换输出按 ---- 分行拆成两段（第二段写入位的输出协议）；没拆开就当作整段。
function splitBatchPromptSegments(text) {
  const lines = String(text || "").replace(/\r\n/g, "\n").trim().split("\n");
  const index = lines.findIndex((line) => /^\s*-{4,}\s*$/.test(line));
  if (index < 1 || index >= lines.length - 1) return null;
  const first = lines.slice(0, index).join("\n").trim();
  const second = lines.slice(index + 1).join("\n").trim();
  return first && second ? [first, second] : null;
}

async function batchConvert(scheme, itemIndex, silent = false) {
  const item = scheme.items[itemIndex];
  const script = String(item?.script || "").trim();
  const promptExtra = activeExtraPoints(scheme).find((point) => point.kind === "prompt");
  const prompt2Key = promptExtra ? extraKeyOf(promptExtra) : "";
  if (!item || !script
      || (String(item.prompt || "").trim()
          && (!promptExtra || itemExtraValue(item, prompt2Key).trim()))) return true;
  const key = batchItemReference(scheme, itemIndex);
  if (batch.converting.has(key)) return true;
  if (!batch.convert.provider || !batch.convert.model) {
    if (!silent) toast("转换前先在「剧本 → 提示词」一栏选好供应商和模型");
    return false;
  }
  batch.converting.add(key);
  renderBatch();
  try {
    const detail = scheme.detail;
    const snapshot = batchConversionSnapshot(scheme, item);
    const current = () => batchSchemeOf(scheme.id) === scheme && scheme.detail === detail
      && scheme.items.includes(item) && batchConversionSnapshot(scheme, item) === snapshot;
    const body = buildBatchConvertRequest(scheme.detail, scheme.mapping, item, batch.convert, scheme.fixed);
    const created = await api("/api/studio/generate", { method: "POST", body });
    const taskId = String(created?.task?.id || "");
    if (!taskId) throw new Error(created?.error || "转换任务创建失败");
    const result = await batchWaitStudioTask(taskId, current);
    if (!current()) {
      toast("条目、素材或转换设置已改变，本次结果未写入；请重新转换");
      return false;
    }
    const prompt = String(result || "").trim();
    if (!prompt) throw new Error("转换模型没有返回提示词正文");
    const parts = prompt2Key ? splitBatchPromptSegments(prompt) : null;
    if (parts) {
      item.prompt = parts[0];
      item.extras = item.extras || {};
      item.extras[prompt2Key] = parts[1];
    } else {
      // 模型没按协议分段：整段进主列，第二列留空让用户手填。
      item.prompt = prompt;
      if (prompt2Key && !silent) toast("转换没有按 ---- 给出第二段，可以直接手填第二列");
    }
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
    const promptExtra = activeExtraPoints(scheme).find((point) => point.kind === "prompt");
    const prompt2Key = promptExtra ? extraKeyOf(promptExtra) : "";
    (scheme.items || []).forEach((item, itemIndex) => {
      const done = Boolean(String(item.prompt || "").trim()
        && (!promptExtra || itemExtraValue(item, prompt2Key).trim()));
      if (String(item.script || "").trim() && !done
          && !batch.converting.has(batchItemReference(scheme, itemIndex))) {
        targets.push([scheme, item]);
      }
    });
  });
  if (!targets.length) { toast("没有要转换的条目：先填剧本，或已全部转换过"); return; }
  if (!batch.convert.provider || !batch.convert.model) { toast("先在「剧本 → 提示词」一栏选好供应商和模型"); return; }
  batch.blockedMessage = "";
  for (const [scheme, item] of targets) {
    if (batchSchemeOf(scheme.id) !== scheme) return;
    const itemIndex = scheme.items.indexOf(item);
    if (itemIndex < 0) continue;
    const ok = await batchConvert(scheme, itemIndex, true);
    if (!ok) return;
  }
  toast("转换完成；提示词可以逐条手改");
}

async function batchWaitStudioTask(taskId, current = () => true) {
  for (let attempt = 0; attempt < 600; attempt += 1) {
    if (!current()) return null;
    await new Promise((resolve) => setTimeout(resolve, 1500));
    if (!current()) return null;
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
      // 空行不会提交，别让它因为必填素材口把整批拦下。
      if (batchItemIsBlank(item)) return;
      for (let portIndex = 0; portIndex < ports.length; portIndex += 1) {
        const port = ports[portIndex];
        // 手动 ⊘ = 用户明确不要这个输入：不因必填声明在前端拦下。
        if (itemSkipsPort(item, port)) continue;
        const slot = batchSlotByAddress(scheme.detail, port);
        // 工作流已有原文件：没选新文件也合法（固定素材优先于 optional/必填提示）。
        if (mediaSlotOriginalNonEmpty(slot)) continue;
        if (slot?.media_optional !== false) continue;
        if (!item.files[String(portIndex)]?.path) {
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
  let skippedItems = 0;
  let skippedSchemes = 0;
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
    // 完全没动过的行不提交：空提示词会退回工作流原配置，白跑一条。
    const readyItems = (scheme.items || []).filter((item) => !batchItemIsBlank(item));
    skippedItems += scheme.items.length - readyItems.length;
    if (!readyItems.length) { skippedSchemes += 1; continue; }
    const items = readyItems.map((item) => {
      const mediaInputs = [];
      const mediaSkip = [];
      ports.forEach((port, portIndex) => {
        // 手动 ⊘ 优先于已选文件：跳过是用户明确的最高优先级。
        if (itemSkipsPort(item, port)) {
          mediaSkip.push({
            node_id: String(port.node_id),
            field_name: String(port.field_name),
          });
          return;
        }
        const record = item.files[String(portIndex)];
        if (!record?.path) return;
        mediaInputs.push({ node_id: String(port.node_id), field_name: String(port.field_name), path: record.path, media: batchSlotByAddress(detail, port)?.media || "image" });
      });
      const duration = String(item.duration ?? "").trim();
      const resolution = String(item.resolution ?? "").trim();
      // 只提交已指认槽位的补充值：指认行还没选参数就不提交那一行。
      const extras = {};
      for (const point of activeExtraPoints(scheme)) {
        const value = itemExtraValue(item, extraKeyOf(point));
        if (value) extras[extraKeyOf(point)] = value;
      }
      return {
        name: String(item.name || "").trim() || `条目 ${scheme.items.indexOf(item) + 1}`,
        script: String(item.script || "").trim(),
        prompt: String(item.prompt || "").trim(),
        duration_seconds: duration ? Number(duration) : null,
        resolution,
        extras,
        shots: Math.max(1, Math.floor(Number(item.shots) || 1)),
        media_inputs: mediaInputs,
        media_skip: mediaSkip,
      };
    });
    schemes.push({
      workflow_id: scheme.workflow_id,
      overrides: fixedOverrides,
      mapping: {
        prompt_slot: scheme.mapping.prompt_slot,
        duration_slot: scheme.mapping.duration_slot,
        resolution_slot: scheme.mapping.resolution_slot,
        extra_points: activeExtraPoints(scheme).map((point) => ({
          id: String(point.id || ""),
          kind: String(point.kind || ""),
          slot: { node_id: String(point.slot.node_id), field_name: String(point.slot.field_name) },
        })),
        // 素材口顺序与勾选一并提交：批次快照原样存下来，「编辑这一批」才能
        // 把每个文件放回原来的口，而不是靠猜。
        media_ports: (scheme.mapping.media_ports || []).map((port) => ({
          node_id: String(port.node_id),
          field_name: String(port.field_name),
          checked: Boolean(port.checked),
        })),
      },
      items,
    });
  }
  if (!schemes.length) {
    toast(skippedItems || skippedSchemes
      ? "没有可提交的条目：空行不会提交，先填剧本或提示词，或给条目挂素材"
      : "还没有可选的方案：每个方案要先选好工作流");
    return;
  }
  try {
    const payload = await api("/api/comfyui/batches", {
      method: "POST",
      body: { title: draft.title, unload_after: draft.unload_after, schemes },
    });
    batch.runId = String(payload.batch?.id || "");
    batch.runData = payload.batch || null;
    batch.runSignature = batchRunSignature(batch.runData);
    // 这一批就是当前填单提交出来的：再点「编辑这一批」不用重新拉快照。
    batch.draftSourceId = batch.runId;
    // 回填提示只属于来源批次；内容已经重新提交，别让警告跟着新批次走。
    for (const scheme of draft.schemes) delete scheme.restoreWarning;
    batch.missingItems.clear();
    // 新一批开始：结果页若还停在旧详情，切回批次列表，避免看到上一批。
    batch.resultsDetailId = "";
    syncResultsBatchDetail();
    batch.view = "run";
    renderBatch();
    startBatchPolling();
    void loadBatchHistories();
    const skipped = [skippedItems ? `忽略 ${skippedItems} 条空行` : "",
      skippedSchemes ? `跳过 ${skippedSchemes} 个空方案` : ""].filter(Boolean).join("，");
    toast(skipped ? `批次已提交（${skipped}），正在逐条执行` : "批次已提交，正在逐条执行");
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
  // 默认空行不占任务：导入前先把完全没动过的行清掉，只删空行不碰用户填过的。
  const before = scheme.items.length;
  scheme.items = scheme.items.filter((item) => !batchItemIsBlank(item));
  const removed = before - scheme.items.length;
  const start = scheme.items.length;
  items.forEach((row, index) => {
    scheme.items.push(newBatchItem(start + index + 1, {
      name: row.head ? String(row.head).slice(0, 120) : `第 ${row.no} 镜`,
      script: "",
      prompt: String(row.prompt || ""),
    }));
  });
  renderBatch();
  toast(`已把 ${items.length} 条分镜提示词导进方案 1 的条目表${removed ? `，清掉 ${removed} 条空行` : ""}`);
}

// ---- 跑批视图 ----

const BATCH_STATUS_BADGES = {
  queued: ["排队中", "queue"],
  running: ["生成中", "run"],
  completed: ["已完成", "done"],
  failed: ["失败", "fail"],
  cancelled: ["已取消", "fail"],
  interrupted: ["已中断", "fail"],
  // 任务记录被删（用户清过产物）：不是执行失败，别再用红字吓人。
  missing: ["产物已删除", "queue"],
};

function batchDeletableItems(data = batch.runData) {
  return (data?.items || []).filter((item) => item?.task_id && String(item.status) !== "missing");
}

function syncBatchSelection(data) {
  const id = String(data?.id || batch.runId || "");
  if (batch.selectionBatchId !== id) {
    batch.selectionBatchId = id;
    batch.selectionMode = false;
    batch.selectedItems.clear();
  }
  const allowed = new Set(batchDeletableItems(data).map((item) => String(item.task_id)));
  for (const selected of batch.selectedItems) if (!allowed.has(selected)) batch.selectedItems.delete(selected);
}

function batchRunMarkup() {
  const data = batch.runData;
  if (!data) return '<div class="cbm-run-head"><span class="cbm-run-title">正在读取批次…</span></div>';
  syncBatchSelection(data);
  const items = data.items || [];
  const done = items.filter((item) => item.status === "completed").length;
  const missing = items.filter((item) => item.status === "missing").length;
  const failed = items.filter((item) =>
    ["failed", "cancelled", "interrupted"].includes(String(item.status))).length;
  const total = Math.max(items.length, 1);
  const active = items.some((item) => ["queued", "running"].includes(String(item.status)));
  const percent = Math.round((done / total) * 100);
  const cards = items.map((item) => batchRunCardMarkup(item)).join("");
  const badgeLabel = active
    ? `正在生成 ${done}/${items.length}`
    : done === items.length
      ? `${items.length} 条全部完成`
      : missing
        ? `${done}/${items.length} 完成 · ${missing} 条产物已删`
        : `${done}/${items.length} 完成`;
  const badgeClass = active ? "run" : done === items.length ? "done" : failed ? "fail" : "queue";
  const canClear = batchDeletableItems(data).length > 0;
  const busy = batch.deletingBatches.has(String(batch.runId || data.id || ""));
  const disabled = busy ? "disabled" : "";
  const actions = [
    `<button type="button" class="cbm-btn cbm-manage-btn primary" data-batch-action="edit-batch" ${disabled}>编辑这一批</button>`,
    canClear
      ? `<button type="button" class="cbm-btn cbm-manage-btn danger" data-batch-action="clear-batch-outputs" ${disabled}>删除本批全部产物</button>`
      : "",
    `<button type="button" class="cbm-btn cbm-manage-btn danger" data-batch-action="delete-batch" ${disabled}>删除整个批次</button>`,
  ].join("");
  const selection = batch.selectionMode
    ? `<strong data-batch-selected-count>已选 ${batch.selectedItems.size} 条</strong>
       <button type="button" class="cbm-btn cbm-manage-btn" data-batch-action="select-all-items" ${disabled}>全选可删除条目</button>
       <button type="button" class="cbm-btn cbm-manage-btn" data-batch-action="cancel-selection" ${disabled}>取消选择</button>
       <button type="button" class="cbm-btn cbm-manage-btn danger" data-batch-action="delete-selected-items" ${disabled || !batch.selectedItems.size ? "disabled" : ""}>${busy ? "删除中…" : `删除所选（${batch.selectedItems.size}）`}</button>`
    : `<button type="button" class="cbm-btn cbm-manage-btn" data-batch-action="select-items" ${disabled || !canClear ? "disabled" : ""}>选择条目</button><span>勾选后可一次删除多个条目的产物</span>`;
  // 结果页打开的详情要有明确返回入口；批量页的当前跑批视图不需要。
  const back = batch.resultsDetailId && comfyuiPaneName() === "tasks"
    ? `<button type="button" class="cbm-btn tiny cbm-back-to-list" data-batch-action="back-to-batch-list">← 返回批次列表</button>`
    : "";
  return `<div class="cbm-run-head">
      ${back}
      <span class="cbm-run-title">${escapeText(data.title || "未命名批次")}</span>
      <span class="cbm-badge ${badgeClass}">${escapeText(badgeLabel)}</span>
      <div class="cbm-progress" role="progressbar" aria-label="批次进度" aria-valuenow="${percent}" aria-valuemin="0" aria-valuemax="100"><div style="width:${percent}%"></div></div>
      <span class="cbm-run-meta" data-batch-elapsed></span>
      <span class="cbm-run-actions">${actions}</span>
    </div>
    <div class="cbm-selection-toolbar" data-batch-selection-toolbar aria-label="批次条目管理">${selection}
      <p>删除产物会同时移除对应任务记录，保留批次内容，可编辑后重新生成；正在运行的条目会先取消。删除整个批次还会移除批次记录。</p>
    </div>
    <div class="cbm-cards">${cards}</div>
    <p class="cbm-run-note">失败的条目点「重试」＝从没完成的段继续；产物删掉了也能点「编辑这一批」把工作流、条目和映射回填，改完重新提交会生成新批次。</p>`;
}

function batchRunCardMarkup(item) {
  const status = String(item.status || "");
  const [label, cls] = BATCH_STATUS_BADGES[status] || [status || "—", "queue"];
  const outputs = item.outputs || [];
  const first = outputs[0] || "";
  const taskRef = String(item.task_id || "");
  // 完成态预览不再自己画缩略图：挂一个占位，渲染后塞单次结果同款产物卡，
  // 图片/视频/音频的尺寸、灯箱和播放器全部与单次结果一致。
  let preview = "";
  if (status === "completed") {
    preview = first
      ? `<div class="cbm-card-preview" data-batch-preview-task="${escapeAttribute(taskRef)}" data-batch-preview-name="${escapeAttribute(first)}" data-comfyui-preview-key="${escapeAttribute(JSON.stringify([taskRef, String(first)]))}" data-comfyui-preview-url="${escapeAttribute(outputFileUrl(taskRef, first))}" data-comfyui-preview-kind="${escapeAttribute(mediaKind(first, first))}" data-comfyui-preview-name="${escapeAttribute(first)}"></div>`
      : '<div class="cbm-card-thumb skel"><span class="cbm-card-skel-text">已完成，没有产物</span></div>';
  } else {
    let thumb;
    if (status === "running") {
      thumb = `<span class="cbm-card-skel-text">第 ${(Number(item.done) || 0) + 1}/${item.total || 1} 段生成中</span>`;
    } else if (status === "missing") {
      thumb = '<span class="cbm-card-skel-text">任务和产物已删除</span>';
    } else {
      thumb = `<span class="cbm-card-skel-text">${status === "queued" ? "等前面的条目出完" : "—"}</span>`;
    }
    preview = `<div class="cbm-card-thumb skel">${thumb}</div>`;
  }
  const deleting = taskRef && batch.deletingItems.has(taskRef);
  const busy = deleting || batch.deletingBatches.has(String(batch.runId || ""));
  const deletable = taskRef && status !== "missing";
  const deleteButton = deletable
    ? `<button type="button" class="cbm-btn cbm-delete-product danger" data-batch-action="delete-item" data-task-id="${escapeText(taskRef)}" ${busy ? "disabled" : ""}>${deleting ? "删除中…" : "删除产物"}</button>`
    : "";
  const foot = [];
  if (status === "completed") {
    foot.push(`<span>${outputs.length > 1 ? `${outputs.length} 个产物` : outputs.length ? "已完成" : "无产物"}</span>`);
    if (first) {
      // 条目产物操作 = 打开这一条的产物文件夹，不再就地展开产物列表；
      // 媒体预览仍留在卡片缩略图上，两种动作分开。
      foot.push(`<button type="button" class="cbm-btn tiny" data-batch-action="reveal-item-folder" data-task-id="${escapeText(taskRef)}">打开文件夹</button>`);
    }
  } else if (status === "missing") {
    // 记录已经不在：重试必然 404，入口统一到「编辑这一批」重新提交。
    foot.push('<span>产物已删除</span>');
  } else if (item.error) {
    foot.push(`<span class="cbm-err">${escapeText(String(item.error))}</span>`);
    if (taskRef) {
      foot.push('<button type="button" class="cbm-btn tiny" data-batch-action="retry-item" data-task-id="' + escapeText(taskRef) + '">重试</button>');
    }
  } else if (["failed", "cancelled", "interrupted"].includes(status)) {
    foot.push(`<span>${item.failed || 0} 段失败</span>`);
    if (taskRef) {
      foot.push('<button type="button" class="cbm-btn tiny" data-batch-action="retry-item" data-task-id="' + escapeText(taskRef) + '">重试</button>');
    }
  } else if (status === "running" || status === "queued") {
    // 批量条目不再出现在单次结果页的任务列表，取消入口就留在条目卡上。
    foot.push(`<span>${item.done || 0}/${item.total || 1} 段</span>`);
    foot.push(`<button type="button" class="cbm-btn tiny" data-batch-action="cancel-item" data-task-id="${escapeText(taskRef)}">取消</button>`);
  }
  const selection = batch.selectionMode && deletable
    ? `<label class="cbm-card-select"><input type="checkbox" data-batch-task-check="${escapeText(taskRef)}" ${batch.selectedItems.has(taskRef) ? "checked" : ""} ${busy ? "disabled" : ""}><span>选择此条目</span></label>` : "";
  return `<div class="cbm-card${batch.selectedItems.has(taskRef) ? " is-selected" : ""}" data-batch-task-id="${escapeText(taskRef)}">
    <div class="cbm-card-head">${selection}<strong>${escapeText(item.name || `条目 ${item.index}`)}</strong><span class="cbm-badge ${cls}">${label}</span></div>
    ${preview}
    <div class="cbm-card-foot">${foot.join("")}</div>
    ${deleteButton ? `<div class="cbm-card-actions">${deleteButton}</div>` : ""}
  </div>`;
}

// 完成态卡片的预览：用单次结果同一张产物卡（renderOutputCard），图片能开灯箱、
// 视频有播放器、音频能直接播；这样批量卡片和单次结果的媒体展示不会再分叉。
function batchPreviewVideoPreload(host) {
  const load = (target) => target.querySelectorAll?.("video").forEach((video) => {
    if (video.preload === "none") video.preload = "metadata";
  });
  if (typeof IntersectionObserver === "undefined") { load(host); return; }
  if (!batchPreviewObserver) {
    batchPreviewObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        load(entry.target);
        batchPreviewObserver.unobserve(entry.target);
      }
    }, { rootMargin: "240px" });
  }
  batchPreviewObserver.observe(host);
}

function mountBatchItemPreviews() {
  document.querySelectorAll("#comfyuiBatchBody [data-batch-preview-task], #comfyuiBatchDetail [data-batch-preview-task]").forEach((host) => {
    const taskId = String(host.dataset.batchPreviewTask || "");
    const name = String(host.dataset.batchPreviewName || "");
    host.textContent = "";
    if (!taskId || !name) return;
    host.append(renderOutputCard(taskId, name, ""));
    // 单次结果为了省带宽用 preload=none；批量卡片只预览第一条，等卡片进入
    // 视野再读元数据，按竖屏/横屏的真实比例显示，也不并发拉整批视频。
    batchPreviewVideoPreload(host);
    const video = host.querySelector("video");
    if (!video) return;
    // 视频中间给一个真正的播放入口：点一下就开始，不要求用户去点控制条。
    const figure = host.querySelector(".comfyui-output") || host;
    const play = document.createElement("button");
    play.type = "button";
    play.className = "cbm-preview-play";
    play.setAttribute("aria-label", "播放视频");
    play.textContent = "▶";
    play.addEventListener("click", (event) => {
      event.stopPropagation();
      play.remove();
      void video.play();
    });
    video.addEventListener("play", () => play.remove(), { once: true });
    figure.append(play);
  });
}

// 条目「打开文件夹」：定位这一条自己的产物目录（不是产物根目录，也不展开产物列表）。
async function revealBatchItemFolder(taskId) {
  const id = String(taskId || "");
  if (!id) return;
  try {
    await api(`/api/comfyui/tasks/${encodeURIComponent(id)}/outputs/open`, {
      method: "POST", body: {},
    });
    toast("已在电脑上打开这个条目的产物文件夹");
  } catch (error) {
    // 手机端 / 目录不存在等由服务端给出明确原因，这里照实转达。
    toast(`打开文件夹失败：${error.message}`);
  }
}

async function cancelBatchItem(taskId) {
  const batchId = String(batch.runId || "");
  try {
    await api(`/api/comfyui/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST", body: {} });
    toast("已取消这一条，后面的条目照常跑");
    if (batch.runId === batchId && batch.view === "run") {
      batch.runData = null;
      batch.runSignature = "";
      stopBatchPolling();
      renderBatch();
      startBatchPolling();
    }
    void loadBatchHistories();
  } catch (error) {
    toast(`取消失败：${error.message}`);
  }
}

function formatBatchElapsed(ms) {
  const seconds = Math.max(0, Math.round(Number(ms) / 1000));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

// 耗时文案只由持久时间决定：完成态固定，运行态按 started_at 起算；
// 没有 started_at 的老记录显示「耗时未知」，不按打开页面的时刻伪造。
function batchElapsedText(data, now = Date.now()) {
  if (!data) return "";
  const started = Number(data.started_at) || 0;
  const finished = Number(data.finished_at) || 0;
  const active = Boolean(data.active);
  if (!started) return active ? "等待开始" : "耗时未知";
  if (finished) return `耗时 ${formatBatchElapsed(finished - started)}`;
  if (!active) return "耗时未知";
  return `已用 ${formatBatchElapsed(now - started)}`;
}

function batchElapsedHost() {
  const root = batch.resultsDetailId && comfyuiPaneName() === "tasks"
    ? $("#comfyuiBatchDetail") : $("#comfyuiBatchBody");
  return root?.querySelector?.("[data-batch-elapsed]") || null;
}

// 批次耗时只认服务端持久时间：开始 = 最早任务的 started_at，结束 = 全部终态后的
// 最晚 finished_at。离开详情只停刷新，不重置起点；老记录缺时间就显示「耗时未知」。
function startBatchRunElapsed() {
  if (batch.runElapsedTimer) {
    clearInterval(batch.runElapsedTimer);
    batch.runElapsedTimer = null;
  }
  const update = () => {
    const host = batchElapsedHost();
    if (!host) return;
    host.textContent = batchElapsedText(batch.runData);
  };
  update();
  const data = batch.runData;
  if (data?.active && Number(data?.started_at) > 0) {
    batch.runElapsedTimer = setInterval(update, 1000);
  }
}

// 跑批轮询的重绘口径：条目状态没变化就不整段重绘，避免正在播放的视频被
// innerHTML 重建打断；用户自己的展开/删除动作仍由对应 handler 直接重绘。
function batchRunSignature(data) {
  return JSON.stringify({
    title: String(data?.title || ""),
    // 开始/结束时刻也进签名：任务刚开始（还没有状态变化）时要让耗时立刻从
    // 服务端时间起算，而不是停在「等待开始」。
    started_at: Number(data?.started_at) || 0,
    finished_at: Number(data?.finished_at) || 0,
    active: Boolean(data?.active),
    items: (data?.items || []).map((item) => [
      String(item?.task_id || ""),
      String(item?.status || ""),
      Number(item?.done) || 0,
      Number(item?.total) || 0,
      Number(item?.failed) || 0,
      (item?.outputs || []).map(String),
      String(item?.error || ""),
    ]),
  });
}

function startBatchPolling() {
  const runId = batch.runId;
  // 结果页批次详情也是跑批视图：两种宿主都算可见，不能因为不在批量档就不刷新。
  if (!runId || !batchRunVisible()) return;
  if (batch.runPoll?.runId === runId) return;
  stopBatchPolling();
  const poll = { runId, epoch: batch.runEpoch };
  batch.runPoll = poll;
  const current = () => batch.runPoll === poll && batch.runEpoch === poll.epoch
    && batch.runId === runId && batchRunVisible();
  const tick = async () => {
    if (!current()) return;
    try {
      const payload = await api(`/api/comfyui/batches/${encodeURIComponent(runId)}`);
      if (!current() || (payload.batch?.id && payload.batch.id !== runId)) return;
      const next = payload.batch || batch.runData;
      const signature = batchRunSignature(next);
      const changed = signature !== batch.runSignature;
      batch.runData = next;
      batch.runSignature = signature;
      // 步级进度不进签名：每轮 tick 都定点刷新顶栏条，签名不变也要刷。
      updateStepMeter();
      if (changed) renderBatch();
      const items = batch.runData?.items || [];
      if (!items.some((item) => ["queued", "running"].includes(String(item.status)))) {
        stopBatchPolling();
        void loadBatchHistories();
      }
    } catch (_) { /* 单次轮询失败不终止跑批监视 */ }
    finally {
      // 串行安排下一次；旧响应既不能写入，也不能清除新一代轮询。
      if (current()) batch.runTimer = setTimeout(() => void tick(), 2200);
    }
  };
  void tick();
}

function stopBatchPolling() {
  batch.runEpoch += 1;
  batch.runPoll = null;
  if (batch.runTimer) { clearTimeout(batch.runTimer); batch.runTimer = null; }
  if (batch.runElapsedTimer) { clearInterval(batch.runElapsedTimer); batch.runElapsedTimer = null; }
}

async function loadBatchHistories() {
  try {
    const payload = await api("/api/comfyui/batches");
    batch.histories = (payload.batches || []).slice(0, 50);
    renderBatchHistoryList();
  } catch (_) { /* 历史读不到不挡填单 */ }
}

// ---- 批次列表（结果页「批量」档） ----

// 徽标口径与跑批视图一致：有在跑的报进度，全完成报完成，否则报完成数与失败数。
// 产物被删（missing）单独计数，不算失败——它只是记录没了，不是生成失败。
function batchHistoryBadge(row) {
  const items = row.items || [];
  const total = Number(row.item_count ?? items.length) || 0;
  const done = Number(row.done_count ?? 0) || 0;
  const deleted = items.filter((item) => String(item?.status) === "missing").length;
  const failed = items.filter((item) =>
    ["failed", "cancelled", "interrupted"].includes(String(item?.status))).length;
  if (items.some((item) => ["queued", "running"].includes(String(item?.status)))) return ["run", `正在生成 ${done}/${total}`];
  if (total && done === total) return ["done", `${total} 条全部完成`];
  if (failed) return ["fail", `${done}/${total} 完成 · ${failed} 失败`];
  if (deleted) return ["queue", `${done}/${total} 完成 · ${deleted} 条产物已删`];
  return ["queue", `${done}/${total} 完成`];
}

function renderBatchHistoryList() {
  const host = $("#comfyuiBatchList");
  if (!host) return;
  if (!batch.histories.length) {
    host.innerHTML = '<p class="comfyui-empty">还没有批次。到「批量生成」提交一批，这里就会留下记录。</p>';
    return;
  }
  host.innerHTML = batch.histories.map((row) => {
    const [cls, label] = batchHistoryBadge(row);
    const total = Number(row.item_count ?? 0) || 0;
    const percent = total ? Math.min(100, Math.round(((Number(row.done_count ?? 0) || 0) / total) * 100)) : 0;
    const created = formatTime(row.created_at);
    const id = escapeText(String(row.id || ""));
    return `<div class="comfyui-batch-row-wrap">
      <button type="button" class="comfyui-batch-row${cls === "fail" ? " is-failed" : ""}" data-batch-open="${id}">
        <span class="comfyui-batch-row-top"><strong>${escapeText(row.title || "未命名批次")}</strong><span class="cbm-badge ${cls}">${escapeText(label)}</span></span>
        <span class="comfyui-batch-progress"><span style="width:${percent}%"></span></span>
        <span class="comfyui-batch-meta">${total} 条目${created ? ` · ${escapeText(created)} 提交` : ""}</span>
      </button>
      <button type="button" class="comfyui-batch-row-edit" data-batch-edit="${id}" title="回到填单区修改这一批的内容">编辑</button>
    </div>`;
  }).join("");
}

// 从批次列表进详情：留在结果区「批量」档，不切回批量生成填单页。
// 耗时由服务端任务时间计算（打开详情不重新起算）；详情先置空由轮询拉取。
function openBatchHistory(id) {
  if (!id) return;
  batch.runId = String(id);
  batch.runData = null;
  batch.runSignature = "";
  batch.view = "run";
  batch.resultsDetailId = String(id);
  setComfyuiResultsSegment("batch");
  syncResultsBatchDetail();
  setComfyuiPane("tasks");
}

// ---- 从批次回填填单区 ----

function batchBasename(path) {
  return String(path || "").split(/[\\/]/).pop() || "";
}

function batchItemFromSnapshot(item, index) {
  const extras = {};
  for (const [key, value] of Object.entries(item?.extras || {})) {
    const text = String(value ?? "").trim();
    if (text) extras[String(key)] = text;
  }
  // 快照里的手动 ⊘ 按地址放回条目；素材口顺序重排也不串口。
  const skips = {};
  for (const input of item?.media_skip || []) {
    if (input?.node_id && input?.field_name) skips[slotKeyOf(input)] = true;
  }
  return newBatchItem(index + 1, {
    name: String(item?.name || `条目 ${index + 1}`),
    script: String(item?.script || ""),
    prompt: String(item?.prompt || ""),
    duration: item?.duration_seconds === null || item?.duration_seconds === undefined
      ? "" : String(item.duration_seconds),
    resolution: String(item?.resolution || ""),
    shots: Math.max(1, Math.floor(Number(item?.shots) || 1)),
    extras,
    files: {},
    skips,
  });
}

// 文件按「已勾选素材口的序号」存（与填单区一致）；快照里只有口地址，这里配回去。
function batchItemFiles(mediaInputs, checkedPorts) {
  const files = {};
  for (const input of mediaInputs || []) {
    const path = String(input?.path || "");
    if (!path) continue;
    const portIndex = checkedPorts.findIndex((port) => slotKeyOf(port) === slotKeyOf(input));
    if (portIndex < 0) continue;
    files[String(portIndex)] = {
      path,
      name: String(input?.name || batchBasename(path)),
      // 快照没有缩略图路径：图片直接读原文件，视频/音频只显示图标。
      thumb_path: input?.media === "image" ? path : "",
    };
  }
  return files;
}

function batchMappingFromSnapshot(snapshotMapping, detail, mediaInputs) {
  const mapping = {
    prompt_slot: null, duration_slot: null, resolution_slot: null,
    extra_points: [], media_ports: [],
  };
  for (const key of ["prompt_slot", "duration_slot", "resolution_slot"]) {
    const slot = snapshotMapping?.[key];
    if (slot && batchSlotByAddress(detail, slot)) {
      mapping[key] = { node_id: String(slot.node_id), field_name: String(slot.field_name) };
    }
  }
  // 指认行原样回填：槽位还在就保留；类型/槽位对不上就丢（跟主映射同一口径）。
  const seenIds = new Set();
  for (const point of snapshotMapping?.extra_points || []) {
    if (!point?.kind || !point?.slot) continue;
    const slot = batchSlotByAddress(detail, point.slot);
    if (!slot) continue;
    let id = String(point.id || "");
    while (!id || seenIds.has(id)) id = newExtraId();
    seenIds.add(id);
    mapping.extra_points.push({ id, kind: String(point.kind), slot: { node_id: slot.node_id, field_name: slot.field_name } });
  }
  const candidates = batchMediaCandidates(detail);
  const validKeys = new Set(candidates.map((slot) => slotKeyOf(slot)));
  const usedKeys = new Set((mediaInputs || []).map((input) => slotKeyOf(input)));
  const ports = [];
  const seen = new Set();
  for (const port of snapshotMapping?.media_ports || []) {
    const key = slotKeyOf(port);
    if (!validKeys.has(key) || seen.has(key)) continue;
    seen.add(key);
    ports.push({
      node_id: String(port.node_id),
      field_name: String(port.field_name),
      // 有文件的口必须勾上，否则条目里的文件会落到不存在的列。
      checked: Boolean(port.checked) || usedKeys.has(key),
    });
  }
  // 快照里没记到的媒体槽补在末尾（不勾）：工作流后来加了新装载槽也能在这里看到。
  for (const slot of candidates) {
    const key = slotKeyOf(slot);
    if (seen.has(key)) continue;
    seen.add(key);
    ports.push({
      node_id: String(slot.node_id),
      field_name: String(slot.field_name),
      checked: usedKeys.has(key),
    });
  }
  mapping.media_ports = ports;
  return mapping;
}

// 新批次：快照原样回填（不猜字段）。
function batchSchemeFromRestore(snapshot, detail) {
  const scheme = {
    id: batchSchemeId(),
    workflow_id: String(snapshot?.workflow_id || ""),
    detail: detail || null,
    loading: false,
    mapping: null,
    fixed: {},
    items: [],
  };
  for (const override of snapshot?.overrides || []) {
    if (!override?.node_id || !override?.field_name) continue;
    scheme.fixed[slotKeyOf(override)] = override.value;
  }
  const snapshotItems = Array.isArray(snapshot?.items) ? snapshot.items : [];
  if (!detail) {
    scheme.items = snapshotItems.map((item, index) => batchItemFromSnapshot(item, index));
    scheme.restoreWarning = `工作流「${snapshot?.workflow_name || snapshot?.workflow_id || "未知"}」已不存在，请重新选择工作流后再核对条目。`;
    if (!scheme.items.length) scheme.items = [newBatchItem(1)];
    return scheme;
  }
  const allMedia = snapshotItems.flatMap((item) => item.media_inputs || []);
  scheme.mapping = batchMappingFromSnapshot(snapshot?.mapping, detail, allMedia);
  const checkedPorts = scheme.mapping.media_ports.filter((port) => port.checked);
  scheme.items = snapshotItems.map((item, index) => {
    const row = batchItemFromSnapshot(item, index);
    row.files = batchItemFiles(item.media_inputs, checkedPorts);
    return row;
  });
  const lostMapping = ["prompt_slot", "duration_slot", "resolution_slot"]
    .some((key) => snapshot?.mapping?.[key] && !scheme.mapping[key]);
  // 指认行对不上就先丢（batchMappingFromSnapshot 已丢），条目里的补充值也清掉，
  // 免得提交时撞「给未指认参数填了值」的整单拒绝。
  const knownExtraKeys = new Set(activeExtraPoints(scheme).map((point) => extraKeyOf(point)));
  let lostExtras = false;
  for (const item of scheme.items) {
    const kept = {};
    for (const [key, value] of Object.entries(item.extras || {})) {
      if (knownExtraKeys.has(key)) kept[key] = value;
      else lostExtras = true;
    }
    item.extras = kept;
  }
  const lostMedia = allMedia.some((input) =>
    !checkedPorts.some((port) => slotKeyOf(port) === slotKeyOf(input)));
  if (lostMapping || lostMedia || lostExtras) {
    scheme.restoreWarning = "这份工作流改过：部分映射或素材口已经对不上，标出来的条目提交前要重新指认。";
  }
  if (!scheme.items.length) scheme.items = [newBatchItem(1)];
  return scheme;
}

function batchLegacyValue(overrides, slot, kind) {
  if (!slot) return { ok: false, value: "" };
  const key = slotKeyOf(slot);
  const row = (overrides || []).find((entry) => slotKeyOf(entry) === key);
  if (!row) return { ok: false, value: "" };
  const value = row.value;
  if (kind === "text") {
    return typeof value === "string" ? { ok: true, value } : { ok: false, value: "" };
  }
  if (kind === "number") {
    const number = Number(value);
    return value !== "" && value !== null && Number.isFinite(number)
      ? { ok: true, value: number } : { ok: false, value: "" };
  }
  if (value === null || value === undefined || String(value).trim() === "") {
    return { ok: false, value: "" };
  }
  return { ok: true, value: String(value) };
}

// 旧批次（没有 schemes 快照）：从任务输入尽量捞回字段。映射按当前工作流的
// 预选规则重新识别，所以回填后要让用户核对——这是兜底，不是权威数据。
function batchSchemeFromLegacy(group, detail) {
  const scheme = {
    id: batchSchemeId(),
    workflow_id: String(group.workflowId || ""),
    detail: detail || null,
    loading: false,
    mapping: null,
    fixed: {},
    items: [],
    restoreWarning: "这一批是旧版提交的：映射按当前工作流重新识别，提交前请核对正文/时长/分辨率写对了没有。",
  };
  if (!detail) {
    scheme.restoreWarning = group.workflowId
      ? `工作流「${group.workflowName || group.workflowId}」已不存在，只恢复了条目名，请重新选择工作流。`
      : "这一批没有记录工作流，只恢复了条目名，请重新选择工作流。";
    scheme.items = group.rows.map(({ row }, index) => newBatchItem(index + 1, {
      name: String(row?.name || `条目 ${index + 1}`),
    }));
    if (!scheme.items.length) scheme.items = [newBatchItem(1)];
    return scheme;
  }
  scheme.mapping = batchMappingFromWorkflow(detail);
  const mappedKeys = new Set(["prompt_slot", "duration_slot", "resolution_slot"]
    .map((key) => batchSlotByAddress(detail, scheme.mapping[key]))
    .filter(Boolean)
    .map((slot) => slotKeyOf(slot)));
  const resolutionSlot = batchSlotByAddress(detail, scheme.mapping.resolution_slot);
  const resolutionChoices = new Set((resolutionSlot?.candidates || []).map(String));
  const allMedia = group.rows.flatMap(({ restore }) => restore?.media_inputs || []);
  for (const port of scheme.mapping.media_ports) {
    if (allMedia.some((input) => slotKeyOf(input) === slotKeyOf(port))) port.checked = true;
  }
  const checkedPorts = scheme.mapping.media_ports.filter((port) => port.checked);
  const fixedMaps = [];
  group.rows.forEach(({ row, restore }, index) => {
    const overrides = restore?.overrides || [];
    const item = newBatchItem(index + 1, {
      name: String(row?.name || `条目 ${index + 1}`),
      shots: Math.max(1, Math.floor(Number(restore?.shots) || 1)),
    });
    const promptValue = batchLegacyValue(overrides, scheme.mapping.prompt_slot, "text");
    if (promptValue.ok) item.prompt = promptValue.value;
    const durationValue = batchLegacyValue(overrides, scheme.mapping.duration_slot, "number");
    if (durationValue.ok) item.duration = String(durationValue.value);
    const resolutionValue = batchLegacyValue(overrides, scheme.mapping.resolution_slot, "resolution");
    if (resolutionValue.ok && (!resolutionChoices.size || resolutionChoices.has(resolutionValue.value))) {
      item.resolution = resolutionValue.value;
    }
    item.files = batchItemFiles(restore?.media_inputs, checkedPorts);
    item.skips = {};
    for (const input of restore?.media_skip || []) {
      if (input?.node_id && input?.field_name) item.skips[slotKeyOf(input)] = true;
    }
    scheme.items.push(item);
    const extras = new Map();
    for (const override of overrides) {
      if (!override?.node_id || !override?.field_name) continue;
      if (mappedKeys.has(slotKeyOf(override))) continue;
      extras.set(slotKeyOf(override), override.value);
    }
    fixedMaps.push(extras);
  });
  if (!scheme.items.length) scheme.items = [newBatchItem(1)];
  // 只有每条都相同、且不属于三个映射位的参数才当固定参数；否则宁可丢掉也不乱套。
  for (const [key, value] of fixedMaps[0] || []) {
    if (fixedMaps.every((map) => map.has(key) && JSON.stringify(map.get(key)) === JSON.stringify(value))) {
      scheme.fixed[key] = value;
    }
  }
  return scheme;
}

async function batchFetchWorkflow(workflowId) {
  if (!workflowId) return null;
  try {
    const payload = await api(`/api/comfyui/workflows/${encodeURIComponent(workflowId)}`);
    return payload.workflow || null;
  } catch (_) {
    return null;
  }
}

async function batchDraftFromDetail(detail) {
  const schemes = [];
  const snapshots = Array.isArray(detail?.schemes) ? detail.schemes : [];
  if (snapshots.length) {
    const details = await Promise.all(snapshots.map((snapshot) =>
      batchFetchWorkflow(String(snapshot?.workflow_id || ""))));
    snapshots.forEach((snapshot, index) => {
      schemes.push(batchSchemeFromRestore(snapshot, details[index]));
    });
  } else {
    // 老批次：按工作流把条目分组；任务还在的能多恢复一些字段。
    const groups = new Map();
    for (const row of detail?.items || []) {
      const restore = row?.restore || {};
      const workflowId = String(restore.workflow_id || row?.workflow_id || "");
      const key = workflowId || "__unknown__";
      if (!groups.has(key)) {
        groups.set(key, {
          workflowId,
          workflowName: String(restore.workflow_name || row?.workflow_name || ""),
          rows: [],
        });
      }
      groups.get(key).rows.push({ row, restore });
    }
    const groupRows = Array.from(groups.values());
    const details = await Promise.all(groupRows.map((group) =>
      batchFetchWorkflow(group.workflowId)));
    groupRows.forEach((group, index) => {
      schemes.push(batchSchemeFromLegacy(group, details[index]));
    });
  }
  if (!schemes.length) {
    schemes.push({
      id: batchSchemeId(), workflow_id: "", detail: null, loading: false,
      mapping: null, fixed: {}, items: [newBatchItem(1)],
    });
  }
  return {
    title: String(detail?.title || ""),
    unload_after: Boolean(detail?.unload_after),
    schemes,
  };
}

function batchDraftHasContent(draft) {
  return Boolean((draft?.schemes || []).some((scheme) => (scheme.items || []).some((item) =>
    String(item?.prompt || "").trim()
    || String(item?.script || "").trim()
    || String(item?.duration || "").trim()
    || String(item?.resolution || "").trim()
    || Object.values(item?.extras || {}).some((value) => String(value ?? "").trim())
    || Object.keys(item?.skips || {}).length
    || Object.keys(item?.files || {}).length)));
}

function batchEditSnapshot() {
  const draft = batch.draft;
  return JSON.stringify({
    title: draft?.title, unload_after: draft?.unload_after, convert: batch.convert,
    schemes: (draft?.schemes || []).map((scheme) => ({
      id: scheme.id, workflow_id: scheme.workflow_id, mapping: scheme.mapping,
      fixed: scheme.fixed, items: scheme.items,
    })),
    // 文本框尚未 blur 时模型可能尚未回写（例如批次标题），也必须保护人工输入。
    fields: Array.from(document.querySelectorAll("#comfyuiBatchBody [data-batch-field], #comfyuiBatchBody [data-batch-item], #comfyuiBatchBody [data-batch-fixed]")).map((node) => [node.dataset, node.value, node.checked]),
  });
}

async function editCurrentBatch(batchIdOverride = "") {
  const epoch = ++batch.editEpoch;
  const batchId = String(batchIdOverride || batch.runId || batch.runData?.id || "");
  if (!batchId) return;
  if (batch.draftSourceId === batchId && batch.draft) {
    batch.runId = batchId;
    batch.view = "draft";
    // 进入填单即离开结果详情；再回结果页看到的是批次列表。
    batch.resultsDetailId = "";
    syncResultsBatchDetail();
    stopBatchPolling();
    setComfyuiPane("batch");
    renderBatch();
    return;
  }
  const originalDraft = batch.draft;
  const originalSnapshot = batchEditSnapshot();
  const originalRunId = batch.runId;
  const originalView = batch.view;
  const current = () => epoch === batch.editEpoch && batch.draft === originalDraft
    && batch.runId === originalRunId && batch.view === originalView
    && batchEditSnapshot() === originalSnapshot;
  const stale = () => {
    if (epoch === batch.editEpoch) toast("填单或当前批次已改变，本次历史内容未覆盖；需要时请重新点编辑");
  };
  let detail = null;
  try {
    const payload = await api(
      `/api/comfyui/batches/${encodeURIComponent(batchId)}?with_schemes=1`);
    detail = payload.batch || null;
  } catch (error) {
    if (epoch === batch.editEpoch) toast(`读取批次失败：${error.message}`);
    return;
  }
  if (!current()) { stale(); return; }
  if (!detail) { toast("批次不存在或已被删除"); return; }
  const active = (detail.items || []).some((item) =>
    ["queued", "running"].includes(String(item?.status)));
  if (active && !window.confirm("这一批还有条目在生成。现在编辑并重新提交会另起一批，原来的继续跑。继续吗？")) {
    return;
  }
  if (batch.draft && batch.draftSourceId !== batchId && batchDraftHasContent(batch.draft)
      && !window.confirm("填单区已经有内容，编辑这一批会把它替换掉。继续吗？")) {
    return;
  }
  const draft = await batchDraftFromDetail(detail);
  if (!current()) { stale(); return; }
  batch.draft = draft;
  batch.draftSourceId = batchId;
  batch.runId = batchId;
  batch.runData = detail;
  batch.runSignature = batchRunSignature(detail);
  batch.view = "draft";
  batch.resultsDetailId = "";
  syncResultsBatchDetail();
  batch.missingItems.clear();
  batch.blockedMessage = "";
  stopBatchPolling();
  setComfyuiPane("batch");
  renderBatch();
  toast((detail.schemes || []).length
    ? "已回到填单：改完重新提交会生成新批次"
    : "已回到填单：这一批是旧版提交的，内容可能不全，提交前请核对");
}

async function openBatchForEdit(id) {
  const batchId = String(id || "");
  if (!batchId) return;
  await editCurrentBatch(batchId);
}

async function deleteBatchItem(taskId) {
  const id = String(taskId || "");
  if (id) await deleteBatchTaskIds([id]);
}

async function deleteSelectedBatchItems() {
  syncBatchSelection(batch.runData);
  await deleteBatchTaskIds(Array.from(batch.selectedItems));
}

function batchDeleteResult(payload, requested) {
  if (!Number.isInteger(payload?.deleted) || payload.deleted < 0 || payload.deleted > requested
      || !Array.isArray(payload.failed) || !Array.isArray(payload.skipped)
      || payload.deleted + payload.failed.length + payload.skipped.length > requested) {
    throw new Error("删除响应不完整，请刷新批次确认结果后再试");
  }
  return { deleted: payload.deleted, failed: payload.failed, skipped: payload.skipped };
}

async function deleteBatchTaskIds(requestedIds) {
  const batchId = String(batch.runId || "");
  if (!batchId || batch.deletingBatches.has(batchId)) return;
  const rows = batchDeletableItems();
  const allowed = new Set(rows.map((item) => String(item.task_id)));
  const ids = [...new Set(requestedIds.map(String))].filter((id) => allowed.has(id));
  if (!ids.length) { toast("先选择要删除的条目"); return; }
  const active = rows.filter((item) => ids.includes(String(item.task_id)) && ["running", "queued"].includes(String(item.status))).length;
  if (!window.confirm(`删除所选 ${ids.length} 个条目的全部产物和任务记录？${active ? `其中 ${active} 条正在运行或排队，会先取消。` : ""}\n批次内容保留，可点「编辑这一批」重新生成；产物删除后无法恢复。`)) return;
  const current = () => batch.runId === batchId && batch.view === "run";
  batch.deletingBatches.add(batchId);
  ids.forEach((id) => batch.deletingItems.add(id));
  renderBatch();
  let deleted = 0;
  const failed = [], skipped = [], doneIds = new Set();
  try {
    // 删除接口每次最多 200 个 id，按块提交并保留逐块结果，不能把后半批悄悄漏掉。
    for (let offset = 0; offset < ids.length; offset += 200) {
      const chunk = ids.slice(offset, offset + 200);
      const result = batchDeleteResult(await api("/api/comfyui/tasks", {
        method: "DELETE", body: { task_ids: chunk, cancel_active: true },
      }), chunk.length);
      deleted += result.deleted;
      failed.push(...result.failed);
      skipped.push(...result.skipped);
      const kept = new Set([...result.failed.map((row) => String(row?.id || "")), ...result.skipped.map(String)]);
      const removed = chunk.filter((id) => !kept.has(id));
      if (removed.length === result.deleted) removed.forEach((id) => doneIds.add(id));
    }
    if (failed.length || skipped.length) {
      toast(`已删除 ${deleted} 条，${failed.length + skipped.length} 条未删除：${failed[0]?.reason || "任务仍在运行，请稍后重试"}`);
    } else if (deleted === ids.length) toast(`已删除所选 ${deleted} 个条目的产物，批次内容已保留`);
    else toast(`本次删除 ${deleted} 条，其余记录可能已被移除，已重新读取批次状态`);
  } catch (error) {
    toast(`${deleted ? `已有 ${deleted} 条删除成功；` : ""}删除未全部完成：${error.message}`);
  } finally {
    ids.forEach((id) => batch.deletingItems.delete(id));
    batch.deletingBatches.delete(batchId);
    if (current()) {
      for (const id of doneIds) {
        batch.selectedItems.delete(id);
      }
      batch.runData = null;
      batch.runSignature = "";
      stopBatchPolling();
      startBatchPolling();
      renderBatch();
    }
    void loadBatchHistories();
  }
}

async function clearBatchOutputs() {
  const ids = batchDeletableItems().map((item) => String(item.task_id));
  if (!ids.length) {
    toast("这一批没有可删除的产物");
    return;
  }
  await deleteBatchTaskIds(ids);
}

async function deleteCurrentBatch() {
  const batchId = String(batch.runId || "");
  if (!batchId || batch.view !== "run" || batch.deletingBatches.has(batchId)) return;
  const originalDraft = batch.draft;
  const originalSnapshot = batchEditSnapshot();
  const editEpoch = batch.editEpoch;
  const current = () => batch.runId === batchId && batch.view === "run"
    && batch.draft === originalDraft && batch.editEpoch === editEpoch
    && batchEditSnapshot() === originalSnapshot;
  batch.deletingBatches.add(batchId);
  renderBatch();
  let deleteStarted = false;
  try {
    let detail = batch.runData;
    if (!detail || String(detail.id || "") !== batchId || !Array.isArray(detail.items)) {
      toast("正在读取批次详情，确认删除范围后继续");
      const response = await api(`/api/comfyui/batches/${encodeURIComponent(batchId)}`);
      if (!current()) { toast("批次或填单已改变，本次删除未执行"); return; }
      detail = response.batch;
      if (!detail || String(detail.id || "") !== batchId || !Array.isArray(detail.items)) {
        throw new Error("批次详情不可用，请刷新后重试");
      }
      batch.runData = detail;
      batch.runSignature = batchRunSignature(detail);
      renderBatch();
    }
    if (!current()) { toast("批次或填单已改变，本次删除未执行"); return; }
    const count = detail.items.length;
    if (!window.confirm(`删除整个批次「${detail.title || "未命名批次"}」及其 ${count} 个条目的全部产物和任务记录？\n正在运行的条目会先取消；批次记录也会移除，无法从历史重新编辑。删除后无法恢复。`)) return;
    if (!current()) { toast("批次或填单已改变，本次删除未执行"); return; }
    // 确认后让旧轮询失效，不能在整批删除后重新挂回刚删掉的内容。
    stopBatchPolling();
    deleteStarted = true;
    const payload = await api(`/api/comfyui/batches/${encodeURIComponent(batchId)}`, {
      method: "DELETE",
      body: { delete_outputs: true },
    });
    const result = batchDeleteResult(payload, count);
    if (payload.ok !== true || result.failed.length || result.skipped.length) {
      throw new Error(result.failed[0]?.reason || "批次未完整删除，请刷新后确认");
    }
    if (batch.runId === batchId && batch.view === "run") {
      stopBatchPolling();
      batch.runId = "";
      batch.runData = null;
      batch.runSignature = "";
      batch.selectedItems.clear();
      batch.selectionMode = false;
      if (batch.draftSourceId === batchId) batch.draftSourceId = "";
      batch.view = "draft";
      batch.resultsDetailId = "";
      syncResultsBatchDetail();
      setComfyuiResultsSegment("batch");
      setComfyuiPane("tasks");
    }
    void loadBatchHistories();
    toast(`已删除整个批次及 ${result.deleted} 条任务记录`);
  } catch (error) {
    toast(`删除批次失败：${error.message}`);
  } finally {
    batch.deletingBatches.delete(batchId);
    if (batch.runId === batchId && batch.view === "run") {
      // 后端整批删除可能先删掉部分任务再报错；重新读取，不能留下已删除的旧缩略图。
      if (deleteStarted) {
        batch.runData = null;
        batch.runSignature = "";
        stopBatchPolling();
        startBatchPolling();
      }
      renderBatch();
    }
  }
}

async function retryBatchItem(taskId) {
  const batchId = String(batch.runId || "");
  try {
    await api(`/api/comfyui/tasks/${encodeURIComponent(taskId)}/retry`, { method: "POST", body: {} });
    toast("已重新排队：从没完成的段继续");
    if (batch.runId === batchId && batch.view === "run") {
      batch.runData = null;
      batch.runSignature = "";
      stopBatchPolling();
      renderBatch();
      startBatchPolling();
    }
    void loadBatchHistories();
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
  // 离开批量页会暂停监视；返回时立即校准，避免运行卡停在离开前的状态。
  if (batch.view === "run" && batch.runId) startBatchPolling();
}
