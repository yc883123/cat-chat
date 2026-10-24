// ============================================================
// 24-comfyui.js —— 造梦机（工作台第三个功能页）
// 只 import 01-core / 03-media / 22-studio；轮询经 onStudioPageChange 暂停/恢复
// （离开页面就停，回来由服务端状态校准，与打标器同一套约定）。
// 与聊天域零耦合：只调 /api/comfyui/*。
// ============================================================

import { $, api, toast } from "./01-core.js";
import { isPywebview, openImageLightbox, saveLocalFile } from "./03-media.js";
import { currentStudioPage, onStudioPageChange } from "./22-studio.js";
const POLL_MS = 1200;
const REFRESH_MS = 5000;
const IMAGE_EXTS = new Set([".png", ".jpg", ".jpeg", ".webp", ".gif"]);
const VIDEO_EXTS = new Set([".mp4", ".webm", ".mov"]);

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
};

// ============================================================
// 入口 / 生命周期
// ============================================================

export function bindComfyuiEvents() {
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
  $("#comfyuiImportCancel")?.addEventListener("click", closeImportDialog);
  $("#comfyuiImportConfirm")?.addEventListener("click", () => void confirmImport());
  $("#comfyuiImportFile")?.addEventListener("change", onImportFile);
  $("#comfyuiSubmit")?.addEventListener("click", () => void submitTask());
  $("#comfyuiClearTasks")?.addEventListener("click", () => void clearFinishedTasks());
  $("#comfyuiWorkflowList")?.addEventListener("click", onWorkflowListClick);
  $("#comfyuiTaskList")?.addEventListener("click", onTaskListClick);
  $("#comfyuiOutputGrid")?.addEventListener("click", onOutputGridClick);
  $("#comfyuiPanes")?.addEventListener("click", onPaneClick);
}

async function enterPage() {
  void refreshHealth();
  void refreshWorkflows();
  void refreshTasks();
  renderParamForm();
  scheduleBackgroundRefresh();
}

function onPaneClick(event) {
  const btn = event.target.closest("[data-comfyui-pane]");
  if (!btn) return;
  setComfyuiPane(btn.dataset.comfyuiPane);
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
    if (window.matchMedia("(max-width: 760px)").matches) setComfyuiPane("run");
  }
}

async function selectWorkflow(id) {
  comfyui.selectedId = id;
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

// 槽位渲染顺序：正向提示词 → 负向提示词 → 图片 → 其余（保持导入扫描顺序）。
const ROLE_ORDER = { positive_prompt: 0, negative_prompt: 1, image: 2 };
const ROLE_LABELS = { positive_prompt: "正向提示词", negative_prompt: "负向提示词" };

function orderedSlots(slots) {
  return slots
    .map((slot, index) => ({ slot, index }))
    .sort((a, b) => {
      const rankA = ROLE_ORDER[a.slot.role] ?? 3;
      const rankB = ROLE_ORDER[b.slot.role] ?? 3;
      return rankA !== rankB ? rankA - rankB : a.index - b.index;
    })
    .map((item) => item.slot);
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
    empty.textContent = "先在左侧选一份工作流，这里会列出它可以调的参数。";
    form.append(empty);
    return;
  }
  const slots = comfyui.detail.params || [];
  const heading = document.createElement("p");
  heading.className = "comfyui-param-heading";
  heading.textContent = comfyui.detail.name || "未命名工作流";
  form.append(heading);
  if (!slots.length) {
    const note = document.createElement("p");
    note.className = "comfyui-empty";
    note.textContent = "这份工作流没有可调的参数，直接点「开始生成」即可。";
    form.append(note);
  }
  for (const slot of orderedSlots(slots)) {
    form.append(renderSlot(slot));
  }
}

function renderSlot(slot) {
  const wrap = document.createElement("label");
  wrap.className = "comfyui-param";
  const title = document.createElement("span");
  title.className = "comfyui-param-label";
  const roleLabel = ROLE_LABELS[slot.role];
  if (roleLabel) {
    // 主提示词槽位：角色名做主标签，原始槽位名退为备注。
    title.textContent = roleLabel;
    title.title = slot.label || "";
  } else {
    title.textContent = slot.label || `${slot.node_id} · ${slot.field_name}`;
  }
  wrap.append(title);
  const key = `${slot.node_id}|${slot.field_name}`;
  wrap.dataset.paramKey = key;
  const type = slot.field_type;
  if (type === "bool") {
    const select = document.createElement("select");
    select.dataset.comfyuiSlot = key;
    for (const [value, text] of [["true", "开"], ["false", "关"]]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      select.append(option);
    }
    select.value = slot.default ? "true" : "false";
    wrap.append(select);
    return wrap;
  }
  if (type === "image") {
    const input = document.createElement("input");
    input.type = "text";
    input.dataset.comfyuiSlot = key;
    input.value = slot.default ?? "";
    input.placeholder = "已在 ComfyUI 输入目录里的图片文件名";
    wrap.append(input);
    return wrap;
  }
  if (roleLabel) {
    // 提示词是多行文本：单行 input 没法正常编辑长提示词。
    const area = document.createElement("textarea");
    area.rows = slot.role === "positive_prompt" ? 5 : 3;
    area.dataset.comfyuiSlot = key;
    area.value = slot.default ?? "";
    area.spellcheck = false;
    wrap.append(area);
    return wrap;
  }
  const input = document.createElement("input");
  input.type = type === "number" ? "number" : "text";
  input.step = "any";
  input.dataset.comfyuiSlot = key;
  if (type === "seed") {
    input.placeholder = "留空 = 每次随机；填数字 = 固定种子";
    input.value = "";
  } else {
    input.value = slot.default ?? "";
  }
  wrap.append(input);
  return wrap;
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
    const paste = $("#comfyuiImportText");
    if (paste && !paste.value.trim()) paste.value = text;
    const name = $("#comfyuiImportName");
    if (name && !name.value.trim()) name.value = file.name.replace(/\.json$/i, "");
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
        shots,
      },
    });
    comfyui.activeTaskId = String(payload.task?.id || "");
    comfyui.eventSeq = 0;
    comfyui.tasks = [];
    renderTasks();
    await refreshTasks();
    startPolling();
    if (window.matchMedia("(max-width: 760px)").matches) setComfyuiPane("tasks");
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

function renderTasks() {
  const list = $("#comfyuiTaskList");
  if (!list) return;
  list.textContent = "";
  if (!comfyui.tasks.length) {
    const empty = document.createElement("p");
    empty.className = "comfyui-empty";
    empty.textContent = "还没有任务。选好工作流和参数后点「开始生成」。";
    list.append(empty);
    return;
  }
  for (const row of comfyui.tasks.slice(0, 12)) {
    list.append(renderTaskCard(row));
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
  remove.textContent = "删除";
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

function taskMetaText(row) {
  const progress = row.progress || {};
  const done = Number(progress.done || 0);
  const total = Number(progress.total || row.shots || 0);
  const failed = Number(progress.failed || 0);
  const pieces = [`${done}/${total} 段`];
  if (failed) pieces.push(`失败 ${failed} 段`);
  const outputs = (row.result?.outputs || []).length;
  if (outputs) pieces.push(`${outputs} 个产物`);
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
    if (!window.confirm("删除这条任务和它已保存的产物文件？")) return;
    try {
      await api("/api/comfyui/tasks", {
        method: "DELETE",
        body: { task_ids: [remove.dataset.comfyuiDeleteTask] },
      });
      if (comfyui.activeTaskId === remove.dataset.comfyuiDeleteTask) {
        comfyui.activeTaskId = "";
        stopPolling();
      }
      await refreshTasks();
    } catch (error) {
      toast(`删除失败：${error.message}`);
    }
  }
}

async function clearFinishedTasks() {
  if (!window.confirm("删除全部已结束的任务和它们的产物文件？")) return;
  try {
    const payload = await api("/api/comfyui/tasks", { method: "DELETE", body: {} });
    toast(`已删除 ${payload.deleted || 0} 条任务${payload.skipped?.length ? `，${payload.skipped.length} 条正在执行被跳过` : ""}`);
    await refreshTasks();
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

// ---- 产物 ----

async function renderLatestOutputs() {
  const grid = $("#comfyuiOutputGrid");
  if (!grid) return;
  const finished = comfyui.tasks.filter((row) => (row.result?.outputs || []).length);
  const names = [];
  let owner = "";
  for (const row of finished) {
    for (const name of row.result.outputs || []) {
      if (names.length >= 24) break;
      names.push(name);
      owner = owner || row.id;
    }
  }
  grid.textContent = "";
  grid.dataset.taskId = owner;
  if (!names.length) {
    const empty = document.createElement("p");
    empty.className = "comfyui-empty";
    empty.textContent = "还没有生成结果。提交任务后，出好的图会出现在这里。";
    grid.append(empty);
    return;
  }
  for (const name of names) {
    grid.append(renderOutputCard(owner, name));
  }
}

function renderOutputCard(taskId, name) {
  const ext = `.${name.split(".").pop()?.toLowerCase() || ""}`;
  const url = `/api/comfyui/tasks/${encodeURIComponent(taskId)}/files/${encodeURIComponent(name)}`;
  const card = document.createElement("figure");
  card.className = "comfyui-output";
  if (IMAGE_EXTS.has(ext)) {
    const img = document.createElement("img");
    img.src = url;
    img.alt = name;
    img.loading = "lazy";
    img.dataset.comfyuiOutputUrl = url;
    img.dataset.comfyuiOutputName = name;
    card.append(img);
  } else if (VIDEO_EXTS.has(ext)) {
    const video = document.createElement("video");
    video.src = url;
    video.controls = true;
    video.preload = "metadata";
    card.append(video);
  } else {
    const link = document.createElement("a");
    link.href = url;
    link.textContent = name;
    link.download = name;
    card.append(link);
  }
  const caption = document.createElement("figcaption");
  caption.textContent = name;
  card.append(caption);
  return card;
}

function onOutputGridClick(event) {
  const img = event.target.closest("img[data-comfyui-output-url]");
  if (img) {
    openImageLightbox(img.dataset.comfyuiOutputUrl);
    return;
  }
  const link = event.target.closest("a[href]");
  if (link && isPywebview()) {
    void saveLocalFile(link.href, link.textContent || "output.bin");
  }
}
