// ============================================================
// 28-super-agent.js —— 任务总管控制面
// ============================================================

import { $, api, escapeHtml, setAppView, state } from "./01-core.js";
import { closeSidebar, openSidebar, setLeftSidebarCollapsed, sidebarDesktop } from "./14-file-panel.js";
import { closeStudioView, openStudioView, setStudioPage } from "./22-studio.js";

const viewState = {
  open: false, selectedId: "", threads: [], detail: null, nodes: [], events: [], artifacts: [], approvals: [],
  eventCursor: 0, eventsThreadId: "", eventsInitialized: false,
  loading: false, error: "", actionMessage: "", syncedAt: 0,
};

const createModelState = { requestId: 0, loading: false, submitting: false };
const CREATE_MODEL_HINT = "模型来自全局供应商设置，默认使用全局默认模型，没有独立 API 设置；阶段链创建仅供内部预览。";

const STATUS_LABELS = {
  draft: "草稿", active: "进行中", waiting_user: "等待确认", paused: "已暂停", cancelled: "已取消",
  completed: "已完成", failed: "失败", needs_recovery: "待恢复", archived: "已归档", pending: "待处理",
  ready: "就绪", running: "运行中", stopping: "停止中", succeeded: "已完成", blocked: "已阻塞", interrupted: "已中断",
};
const STAGE_LABELS = {
  setup: "设定", import: "导入", elements: "设定 / 要素", outline: "大纲", chapter_plan: "拆章", chapter_content: "正文", screenplay: "剧本", review: "复核",
};
const STAGE_STATUSES = new Set(["blocked", "ready", "running", "waiting_user", "failed", "succeeded", "cancelled", "interrupted", "pending", "stopping"]);

function statusLabel(value) { return STATUS_LABELS[String(value || "")] || String(value || "未知"); }
function stageLabel(value, fallback = "阶段") {
  const key = String(value || "").trim();
  return text(STAGE_LABELS[key] || key || fallback);
}
function statusClass(value) {
  const status = String(value || "").trim().toLowerCase();
  return STAGE_STATUSES.has(status) ? status : "unknown";
}
function text(value, fallback = "—") {
  const result = String(value ?? "").trim(); return escapeHtml(result || fallback);
}
function refText(value, fallback = "—") {
  if (value && typeof value === "object") {
    return text(value.title || value.path_or_ref || value.ref || value.artifact_id || JSON.stringify(value), fallback);
  }
  return text(value, fallback);
}
function formatTime(value) {
  const timestamp = Number(value || 0); if (!timestamp) return "—";
  const date = new Date(timestamp); return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}
function sortedNodes(nodes) {
  return [...(Array.isArray(nodes) ? nodes : [])].sort((left, right) => {
    const sequence = Number(left.sequence ?? Number.MAX_SAFE_INTEGER) - Number(right.sequence ?? Number.MAX_SAFE_INTEGER);
    return sequence || String(left.node_id || left.kind || "").localeCompare(String(right.node_id || right.kind || ""));
  });
}
function mergeEvents(existing, incoming) {
  const merged = new Map();
  [...(existing || []), ...(incoming || [])].forEach((event) => {
    const key = String(event?.event_id || `sequence:${event?.sequence ?? ""}`);
    if (key !== "sequence:" && !merged.has(key)) merged.set(key, event);
  });
  return [...merged.values()].sort((left, right) => Number(left.sequence || 0) - Number(right.sequence || 0));
}

async function loadThreads() {
  const result = await api("/api/super-agent/threads");
  viewState.threads = Array.isArray(result.threads) ? result.threads : [];
  if (!viewState.selectedId || !viewState.threads.some((item) => item.thread_id === viewState.selectedId)) {
    viewState.selectedId = viewState.threads[0]?.thread_id || "";
  }
  renderThreadList();
  if (viewState.selectedId) await loadDetail(viewState.selectedId); else renderEmptyDetail();
}

async function loadDetail(threadId) {
  if (!threadId) return;
  viewState.loading = true; renderSyncStatus();
  try {
    const encoded = encodeURIComponent(threadId);
    const firstLoad = viewState.eventsThreadId !== threadId || !viewState.eventsInitialized;
    const after = firstLoad ? 0 : viewState.eventCursor;
    const eventRequest = api(`/api/super-agent/threads/${encoded}/events${firstLoad ? "" : `?after=${after}`}`);
    const [thread, nodes, events, artifacts, approvals] = await Promise.all([
      api(`/api/super-agent/threads/${encoded}`), api(`/api/super-agent/threads/${encoded}/nodes`), eventRequest,
      api(`/api/super-agent/threads/${encoded}/artifacts`), api(`/api/super-agent/threads/${encoded}/approvals`),
    ]);
    viewState.detail = thread.thread || null;
    viewState.nodes = Array.isArray(nodes.nodes) ? nodes.nodes : [];
    const incomingEvents = Array.isArray(events.events) ? events.events : [];
    viewState.events = firstLoad ? mergeEvents([], incomingEvents) : mergeEvents(viewState.events, incomingEvents);
    viewState.eventCursor = firstLoad ? Number(events.last_sequence || incomingEvents.at(-1)?.sequence || 0) : Math.max(viewState.eventCursor, Number(events.last_sequence || after || 0));
    viewState.eventsThreadId = threadId; viewState.eventsInitialized = true;
    viewState.artifacts = Array.isArray(artifacts.artifacts) ? artifacts.artifacts : [];
    viewState.approvals = Array.isArray(approvals.approvals) ? approvals.approvals : [];
    viewState.error = ""; viewState.actionMessage = ""; viewState.syncedAt = Date.now(); renderDetail();
  } catch (error) { viewState.error = error.message || "读取任务失败"; renderSyncStatus(); }
  finally { viewState.loading = false; renderSyncStatus(); }
}

function renderThreadList() {
  const list = $("#superAgentThreadList"); if (!list) return;
  $("#superAgentThreadCount").textContent = String(viewState.threads.length);
  $("#superAgentThreadEmpty").hidden = viewState.threads.length > 0;
  list.innerHTML = viewState.threads.map((thread) => `
    <button class="super-agent-thread ${thread.thread_id === viewState.selectedId ? "active" : ""}" type="button" data-super-thread="${text(thread.thread_id)}">
      <strong>${text(thread.title, "未命名任务")}</strong><span>${text(statusLabel(thread.status))}</span><small>${text(thread.goal, "暂无目标")}</small>
    </button>`).join("");
}

function renderEmptyDetail() {
  $("#superAgentDetailEmpty").hidden = false; $("#superAgentDetail").hidden = true;
  $("#superAgentApprovalList").innerHTML = '<p class="super-agent-muted">暂无审批项。</p>';
  $("#superAgentArtifactList").innerHTML = '<p class="super-agent-muted">暂无产物引用。</p>';
  if ($("#superAgentStageChain")) $("#superAgentStageChain").innerHTML = '<p class="super-agent-muted">暂无阶段链。</p>';
}

function renderDetail() {
  const detail = viewState.detail; if (!detail) return renderEmptyDetail();
  $("#superAgentDetailEmpty").hidden = true; $("#superAgentDetail").hidden = false;
  $("#superAgentTitle").textContent = detail.title || "未命名任务";
  $("#superAgentGoal").textContent = detail.goal || "暂无目标"; $("#superAgentSummary").textContent = detail.summary || "暂无摘要";
  const status = $("#superAgentStatus"); status.textContent = statusLabel(detail.status); status.dataset.status = detail.status || "";
  const nodes = sortedNodes(viewState.nodes);
  const nodeById = new Map(nodes.map((node) => [String(node.node_id), node]));
  const currentNodeId = String(detail.current_node_id || nodes.find((node) => ["running", "waiting_user"].includes(String(node.status || "")))?.node_id || "");
  const artifactByNode = new Map();
  viewState.artifacts.forEach((item) => {
    const key = String(item.node_id || ""); if (!artifactByNode.has(key)) artifactByNode.set(key, []); artifactByNode.get(key).push(item);
  });
  const approvalByNode = new Map();
  viewState.approvals.forEach((item) => {
    const key = String(item.node_id || ""); if (!approvalByNode.has(key)) approvalByNode.set(key, []); approvalByNode.get(key).push(item);
  });
  $("#superAgentChainMeta").textContent = detail.chain_version ? `链版本 ${detail.chain_version}` : "兼容模式：传统节点";
  $("#superAgentStageChain").innerHTML = nodes.length ? nodes.map((node, index) => {
    const nodeId = String(node.node_id || ""); const nodeStatus = String(node.status || "pending");
    const active = nodeId === currentNodeId;
    const stageKey = node.stage_key || node.kind || node.node_id;
    const approvals = approvalByNode.get(nodeId) || []; const artifacts = artifactByNode.get(nodeId) || [];
    return `<article class="super-agent-stage ${active ? "is-current" : ""} status-${statusClass(nodeStatus)}" data-node-id="${text(nodeId)}">
      <div class="super-agent-stage-rail"><span class="super-agent-stage-index">${index + 1}</span>${index < nodes.length - 1 ? "<i aria-hidden=\"true\"></i>" : ""}</div>
      <div class="super-agent-stage-body"><div class="super-agent-stage-head"><div><strong>${stageLabel(stageKey, node.kind || "任务节点")}</strong><small>${text(node.kind || stageKey)}</small></div><span class="super-agent-status" data-status="${text(nodeStatus)}">${text(statusLabel(nodeStatus))}</span></div>
      <p class="super-agent-stage-meta">${node.sequence != null ? `阶段 ${text(node.sequence)}` : "传统节点"} · 依赖：${text((node.dependencies || []).join("、"), "无")} · attempt ${text(node.attempt, "0")}</p>
      ${active ? '<span class="super-agent-current-badge">当前节点</span>' : ""}
      ${node.domain_ref ? `<p>领域：${text(node.domain_ref.domain)} · 作品：${text(node.domain_ref.book_id)} · 阶段：${text(node.domain_ref.stage)}</p><button type="button" class="control-button tiny" data-super-return-domain="${text(node.domain_ref.domain)}" data-super-book-id="${text(node.domain_ref.book_id)}">回到工作台</button>` : ""}
      ${approvals.length ? `<div class="super-agent-linked"><b>审批</b>${approvals.map((item) => `<span>${text(item.action, "待确认操作")} · ${text(statusLabel(item.status))}</span>`).join("")}</div>` : ""}
      ${artifacts.length ? `<div class="super-agent-linked"><b>产物</b>${artifacts.map((item) => `<span>${refText(item.title || item.path_or_ref, item.kind)}</span>`).join("")}</div>` : ""}
      ${node.error ? `<small class="super-agent-error">${text(node.error)}</small>` : ""}
      ${!['succeeded','failed','cancelled'].includes(nodeStatus) ? `<div class="super-agent-node-actions"><button type="button" class="control-button tiny" data-super-action="${text(nodeStatus === 'ready' ? 'start' : 'sync')}" data-super-node="${text(nodeId)}">${nodeStatus === 'ready' ? '开始' : '同步'}</button><button type="button" class="control-button tiny" data-super-action="cancel" data-super-node="${text(nodeId)}">取消</button></div>` : ""}
      ${['failed','cancelled','interrupted'].includes(nodeStatus) ? `<button type="button" class="control-button tiny" data-super-action="retry" data-super-node="${text(nodeId)}">重试</button>` : ""}
      </div></article>`;
  }).join("") : '<p class="super-agent-muted">暂无节点。</p>';
  $("#superAgentNodeList").innerHTML = $("#superAgentStageChain").innerHTML;
  $("#superAgentEventList").innerHTML = viewState.events.length ? viewState.events.map((event) => `
    <article class="super-agent-event"><div><strong>${text(event.event_type)}</strong><time>${text(formatTime(event.created_at))}</time></div><p>${text(Object.values(event.summary || {}).join(" · "), "状态已更新")}</p></article>`).join("") : '<p class="super-agent-muted">暂无事件。</p>';
  $("#superAgentApprovalList").innerHTML = viewState.approvals.length ? viewState.approvals.map((item) => `
    <article class="super-agent-ref"><strong>${text(item.action, "待确认操作")}</strong><span>${text(statusLabel(item.status))}</span><p>节点：${text(nodeById.get(String(item.node_id))?.stage_key || nodeById.get(String(item.node_id))?.kind || item.node_id, "未关联")}</p><p>${text(item.description)}</p>${item.status === 'pending' ? `<div class="super-agent-node-actions"><button type="button" class="control-button tiny" data-super-approval="approved" data-super-approval-id="${text(item.approval_id)}">批准</button><button type="button" class="control-button tiny" data-super-approval="rejected" data-super-approval-id="${text(item.approval_id)}">拒绝</button></div>` : ""}</article>`).join("") : '<p class="super-agent-muted">暂无审批项。</p>';
  $("#superAgentArtifactList").innerHTML = viewState.artifacts.length ? viewState.artifacts.map((item) => `
    <article class="super-agent-ref"><strong>${text(item.title, item.kind)}</strong><p>节点：${text(nodeById.get(String(item.node_id))?.stage_key || nodeById.get(String(item.node_id))?.kind || item.node_id, "未关联")}</p><p>${text(item.summary)}</p><code>${text(item.path_or_ref)}</code></article>`).join("") : '<p class="super-agent-muted">暂无产物引用。</p>';
}

function renderSyncStatus() {
  const element = $("#superAgentSyncStatus"); if (!element) return;
  if (viewState.loading) element.textContent = "正在读取…";
  else if (viewState.error) element.textContent = `读取失败：${viewState.error}`;
  else if (viewState.actionMessage) element.textContent = viewState.actionMessage;
  else if (viewState.syncedAt) element.textContent = `已更新 ${formatTime(viewState.syncedAt)}`;
  else element.textContent = "仅显示已关联任务";
  element.classList.toggle("is-error", Boolean(viewState.error));
}

function setView(next) {
  setAppView(next);
}

export async function openSuperAgentView() {
  if (state.appView === "studio") {
    // 只停 Studio 自身轮询，不把任务总管误记为 Chat 偏好。
    closeStudioView();
  }
  setView("super-agent");
  try { await loadThreads(); } catch (error) { viewState.error = error.message || "读取任务失败"; renderSyncStatus(); }
  if (!sidebarDesktop()) closeSidebar();
}
export function closeSuperAgentView() { if (state.appView === "super-agent") setView("chat"); }

function clearCreateError() {
  const errorBox = $("#superAgentCreateError");
  if (errorBox) errorBox.textContent = "";
}

function createProviderProfiles() {
  return Array.isArray(state.bootstrap?.model_profiles)
    ? state.bootstrap.model_profiles
    : (Array.isArray(state.bootstrap?.providers) ? state.bootstrap.providers : []);
}

function renderCreateProviders(preferred = "") {
  const select = $("#superAgentCreateProvider");
  if (!select) return "";
  const profiles = createProviderProfiles();
  select.replaceChildren();
  if (!profiles.length) {
    select.append(new Option("未配置供应商，请先到设置中添加", ""));
    select.disabled = true;
    renderCreateModels("");
    return "";
  }
  for (const profile of profiles) {
    const key = String(profile?.model_key || "").trim();
    if (!key) continue;
    select.append(new Option(String(profile?.name || key), key));
  }
  const defaultKey = String(state.bootstrap?.default_model_key || "");
  const wanted = [preferred, defaultKey, select.options[0]?.value].find((key) =>
    key && [...select.options].some((option) => option.value === key));
  select.value = wanted || "";
  select.disabled = !select.options.length;
  return select.value;
}

function createPreferredModel(providerKey) {
  const profile = createProviderProfiles().find((item) => String(item?.model_key || "") === providerKey);
  return String(profile?.model || "").trim();
}

function renderCreateModels(providerKey, preferred = "") {
  const select = $("#superAgentCreateModel");
  if (!select) return;
  const models = state.providerModelCatalogs[providerKey] || [];
  select.replaceChildren();
  const entries = models.map((entry) => {
    const id = typeof entry === "object" ? (entry.id ?? entry.model ?? entry.name) : entry;
    return { id: String(id || "").trim(), name: String(entry?.name || id || "").trim() };
  }).filter((entry, index, rows) => entry.id && rows.findIndex((row) => row.id === entry.id) === index);
  if (!providerKey) {
    select.append(new Option("请先选择供应商", ""));
    select.disabled = true;
    return;
  }
  if (!entries.length) {
    select.append(new Option("该供应商暂无可用模型，请先在设置中检查配置", ""));
    select.disabled = true;
    return;
  }
  select.append(new Option("请选择模型…", ""));
  for (const entry of entries) select.append(new Option(entry.name || entry.id, entry.id));
  const wanted = preferred || createPreferredModel(providerKey);
  select.value = entries.some((entry) => entry.id === wanted) ? wanted : "";
  select.disabled = false;
}

async function loadCreateModels(providerKey, preferred = "", { force = false } = {}) {
  const select = $("#superAgentCreateModel");
  const requestId = ++createModelState.requestId;
  const hint = $("#superAgentCreateModelHint");
  if (hint) hint.textContent = CREATE_MODEL_HINT;
  createModelState.loading = false;
  renderCreateModels(providerKey, preferred);
  if (!select || !providerKey) return;
  if (!force && (state.providerModelCatalogs[providerKey] || []).length) return;
  createModelState.loading = true;
  select.disabled = true;
  select.replaceChildren(new Option("正在读取模型目录…", ""));
  try {
    const result = await api("/api/providers/models", { method: "POST", body: { model_key: providerKey } });
    if (result?.error) throw new Error(result.error);
    if (requestId !== createModelState.requestId) return;
    state.providerModelCatalogs[providerKey] = Array.isArray(result?.models) ? result.models : [];
    renderCreateModels(providerKey, preferred);
    if (!select.value && !state.providerModelCatalogs[providerKey].length && hint) {
      hint.textContent = "该供应商暂无可用模型，请先在设置中检查配置，再点重新检测模型。";
    }
  } catch (error) {
    if (requestId !== createModelState.requestId) return;
    delete state.providerModelCatalogs[providerKey];
    renderCreateModels(providerKey);
    if (hint) hint.textContent = `模型目录读取失败：${error.message}。请检查配置后重新检测模型。`;
  } finally {
    if (requestId === createModelState.requestId) createModelState.loading = false;
  }
}

function openCreateDomainDialog() {
  const dialog = $("#superAgentCreateDialog");
  if (!dialog || typeof dialog.showModal !== "function") return;
  const domain = $("#superAgentCreateDomain");
  if (domain) domain.value = "writer";
  if ($("#superAgentCreateStage")) $("#superAgentCreateStage").value = "chapter";
  clearCreateError();
  const providerKey = renderCreateProviders();
  void loadCreateModels(providerKey, createPreferredModel(providerKey));
  $("#superAgentCreateBookId")?.focus();
  dialog.showModal();
}

async function createDomainThread(event) {
  event?.preventDefault();
  if (createModelState.submitting) return;
  const domain = String($("#superAgentCreateDomain")?.value || "writer").trim().toLowerCase();
  const bookId = String($("#superAgentCreateBookId")?.value || "").trim();
  const stage = String($("#superAgentCreateStage")?.value || "").trim();
  const providerKey = String($("#superAgentCreateProvider")?.value || "").trim();
  const modelName = String($("#superAgentCreateModel")?.value || "").trim();
  const provider = createProviderProfiles().find((item) => String(item?.model_key || "") === providerKey);
  const providerId = String(provider?.id || "").trim();
  const errorBox = $("#superAgentCreateError");
  if (errorBox) errorBox.textContent = "";
  if (!bookId || !stage || !providerKey || !modelName) {
    if (errorBox) errorBox.textContent = !providerKey
      ? "请先在设置中配置并选择供应商。"
      : !modelName
        ? "请选择一个可用模型。"
        : "请填写作品 ID 和阶段。";
    return;
  }
  const catalog = state.providerModelCatalogs[providerKey] || [];
  if (createModelState.loading || !catalog.some((entry) => String(typeof entry === "object" ? (entry?.id ?? entry?.model ?? entry?.name ?? "") : entry).trim() === modelName)) {
    if (errorBox) errorBox.textContent = "请先重新检测并选择一个可用模型。";
    return;
  }
  createModelState.submitting = true;
  const submit = $("#superAgentCreateSubmit");
  if (submit) submit.disabled = true;
  const startRequest = domain === "writer"
    ? { kind: stage, chapter_no: 1, provider_id: providerId, model_name: modelName, model_key: providerKey }
    : { book_id: bookId, stage, chapter_ids: [], provider_id: providerId, model_name: modelName, model_key: providerKey };
  try {
    await api("/api/super-agent/threads", { method: "POST", body: {
      domain, book_id: bookId, stage, provider_id: providerId, model_key: providerKey, model_name: modelName,
      start_request: startRequest, title: `${domain} · ${stage}`,
    } });
    $("#superAgentCreateDialog")?.close();
    await loadThreads();
  } catch (error) { if (errorBox) errorBox.textContent = error.message || "创建任务失败"; }
  finally { createModelState.submitting = false; if (submit) submit.disabled = false; }
}

async function runNodeAction(action, nodeId) {
  if (!viewState.selectedId || !nodeId) return;
  viewState.error = "";
  viewState.actionMessage = "正在处理…"; renderSyncStatus();
  try {
    await api(`/api/super-agent/threads/${encodeURIComponent(viewState.selectedId)}/actions`, {
      method: "POST", body: { action, node_id: nodeId, request_id: `${action}-${nodeId}-${Date.now()}` },
    });
    await loadDetail(viewState.selectedId);
  } catch (error) {
    const message = String(error?.message || "任务操作失败");
    if (action === "cancel" && (Number(error?.status) === 409 || message.includes("已结束") || message.includes("无需取消"))) {
      viewState.error = "";
      viewState.actionMessage = "任务已结束，无需取消";
      renderSyncStatus();
      await loadDetail(viewState.selectedId);
      viewState.actionMessage = "任务已结束，无需取消";
      renderSyncStatus();
      return;
    }
    viewState.actionMessage = `操作失败：${message}`;
    renderSyncStatus();
  }
}

async function returnToDomain(button) {
  const domain = String(button?.dataset?.superReturnDomain || "");
  if (domain === "writer" || domain === "novel") {
    openStudioView();
    setStudioPage(domain);
    window.dispatchEvent(new CustomEvent("naiba-open-domain", { detail: { domain, bookId: String(button.dataset.superBookId || "") } }));
  }
}

async function resolveApproval(status, approvalId) {
  if (!viewState.selectedId || !approvalId) return;
  viewState.error = "";
  viewState.actionMessage = "正在处理…"; renderSyncStatus();
  try {
    await api(`/api/super-agent/threads/${encodeURIComponent(viewState.selectedId)}/approvals/${encodeURIComponent(approvalId)}`, {
      method: "POST", body: { status, request_id: `${status}-${approvalId}` },
    });
    await loadDetail(viewState.selectedId);
  } catch (error) { viewState.error = error.message || "审批操作失败"; renderSyncStatus(); }
}

export function bindSuperAgentEvents() {
  $("#openSuperAgent")?.addEventListener("click", () => void openSuperAgentView());
  $("#superAgentCreate")?.addEventListener("click", () => openCreateDomainDialog());
  $("#superAgentCreateForm")?.addEventListener("submit", (event) => {
    if (event.submitter?.value === "cancel") return;
    void createDomainThread(event);
  });
  $("#superAgentCreateDomain")?.addEventListener("change", (event) => {
    const stage = $("#superAgentCreateStage");
    if (stage) stage.value = event.target.value === "novel" ? "outline" : "chapter";
    clearCreateError();
  });
  $("#superAgentCreateProvider")?.addEventListener("change", (event) => {
    clearCreateError();
    void loadCreateModels(String(event.target.value || ""));
  });
  $("#superAgentCreateModel")?.addEventListener("change", clearCreateError);
  $("#superAgentCreateModelRefresh")?.addEventListener("click", () => {
    void loadCreateModels(String($("#superAgentCreateProvider")?.value || ""), "", { force: true });
  });
  $("#superAgentCreateForm")?.addEventListener("input", clearCreateError);
  $("#openTaskManager")?.addEventListener("click", () => { $("#tasksDialog")?.close(); void openSuperAgentView(); });
  $("#superAgentRefresh")?.addEventListener("click", () => void openSuperAgentView());
  $("#superAgentOpenSidebar")?.addEventListener("click", () => openSidebar());
  $("#superAgentExpandSidebar")?.addEventListener("click", () => setLeftSidebarCollapsed(false));
  $("#superAgentThreadList")?.addEventListener("click", (event) => { const button = event.target.closest("[data-super-thread]"); if (!button) return; viewState.selectedId = button.dataset.superThread || ""; renderThreadList(); void loadDetail(viewState.selectedId); });
  $("#superAgentStageChain")?.addEventListener("click", (event) => {
    const action = event.target.closest("[data-super-action]");
    if (action) void runNodeAction(action.dataset.superAction || "status", action.dataset.superNode || "");
    const back = event.target.closest("[data-super-return-domain]");
    if (back) void returnToDomain(back);
  });
  $("#superAgentNodeList")?.addEventListener("click", (event) => { const button = event.target.closest("[data-super-action]"); if (!button) return; void runNodeAction(button.dataset.superAction || "status", button.dataset.superNode || ""); });
  $("#superAgentApprovalList")?.addEventListener("click", (event) => { const button = event.target.closest("[data-super-approval]"); if (!button) return; void resolveApproval(button.dataset.superApproval || "", button.dataset.superApprovalId || ""); });
  $("#superAgentNodeList")?.addEventListener("click", (event) => { const button = event.target.closest("[data-super-return-domain]"); if (!button) return; void returnToDomain(button); });
}
