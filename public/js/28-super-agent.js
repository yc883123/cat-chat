// ============================================================
// 28-super-agent.js —— 任务总管 P2 第一切片（只读 Thread 展示）
// ============================================================

import { $, api, escapeHtml, setAppView, state } from "./01-core.js";
import { closeSidebar, openSidebar, setLeftSidebarCollapsed, sidebarDesktop } from "./14-file-panel.js";
import { closeStudioView } from "./22-studio.js";

const viewState = {
  open: false, selectedId: "", threads: [], detail: null, nodes: [], events: [], artifacts: [], approvals: [],
  loading: false, error: "", syncedAt: 0,
};

const STATUS_LABELS = {
  draft: "草稿", active: "进行中", waiting_user: "等待确认", paused: "已暂停", cancelled: "已取消",
  completed: "已完成", failed: "失败", needs_recovery: "待恢复", archived: "已归档", pending: "待处理",
  ready: "就绪", running: "运行中", stopping: "停止中", succeeded: "已完成", blocked: "已阻塞", interrupted: "已中断",
};

function statusLabel(value) { return STATUS_LABELS[String(value || "")] || String(value || "未知"); }
function text(value, fallback = "—") { const result = String(value ?? "").trim(); return escapeHtml(result || fallback); }
function formatTime(value) {
  const timestamp = Number(value || 0); if (!timestamp) return "—";
  const date = new Date(timestamp); return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
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
    const [thread, nodes, events, artifacts, approvals] = await Promise.all([
      api(`/api/super-agent/threads/${encoded}`), api(`/api/super-agent/threads/${encoded}/nodes`),
      api(`/api/super-agent/threads/${encoded}/events`), api(`/api/super-agent/threads/${encoded}/artifacts`),
      api(`/api/super-agent/threads/${encoded}/approvals`),
    ]);
    viewState.detail = thread.thread || null;
    viewState.nodes = Array.isArray(nodes.nodes) ? nodes.nodes : [];
    viewState.events = Array.isArray(events.events) ? events.events : [];
    viewState.artifacts = Array.isArray(artifacts.artifacts) ? artifacts.artifacts : [];
    viewState.approvals = Array.isArray(approvals.approvals) ? approvals.approvals : [];
    viewState.error = ""; viewState.syncedAt = Date.now(); renderDetail();
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
}

function renderDetail() {
  const detail = viewState.detail; if (!detail) return renderEmptyDetail();
  $("#superAgentDetailEmpty").hidden = true; $("#superAgentDetail").hidden = false;
  $("#superAgentTitle").textContent = detail.title || "未命名任务";
  $("#superAgentGoal").textContent = detail.goal || "暂无目标"; $("#superAgentSummary").textContent = detail.summary || "暂无摘要";
  const status = $("#superAgentStatus"); status.textContent = statusLabel(detail.status); status.dataset.status = detail.status || "";
  $("#superAgentNodeList").innerHTML = viewState.nodes.length ? viewState.nodes.map((node) => `
    <article class="super-agent-node"><div><strong>${text(node.kind, "任务节点")}</strong><span class="super-agent-status" data-status="${text(node.status)}">${text(statusLabel(node.status))}</span></div>
    <p>依赖：${text((node.dependencies || []).join("、"), "无")} · attempt ${text(node.attempt, "0")} · executor ${text(node.executor_type, "未知")}</p>${node.error ? `<small class="super-agent-error">${text(node.error)}</small>` : ""}</article>`).join("") : '<p class="super-agent-muted">暂无节点。</p>';
  $("#superAgentEventList").innerHTML = viewState.events.length ? viewState.events.map((event) => `
    <article class="super-agent-event"><div><strong>${text(event.event_type)}</strong><time>${text(formatTime(event.created_at))}</time></div><p>${text(Object.values(event.summary || {}).join(" · "), "状态已更新")}</p></article>`).join("") : '<p class="super-agent-muted">暂无事件。</p>';
  $("#superAgentApprovalList").innerHTML = viewState.approvals.length ? viewState.approvals.map((item) => `
    <article class="super-agent-ref"><strong>${text(item.action, "待确认操作")}</strong><span>${text(statusLabel(item.status))}</span><p>${text(item.description)}</p></article>`).join("") : '<p class="super-agent-muted">暂无审批项。</p>';
  $("#superAgentArtifactList").innerHTML = viewState.artifacts.length ? viewState.artifacts.map((item) => `
    <article class="super-agent-ref"><strong>${text(item.title, item.kind)}</strong><p>${text(item.summary)}</p><code>${text(item.path_or_ref)}</code></article>`).join("") : '<p class="super-agent-muted">暂无产物引用。</p>';
}

function renderSyncStatus() {
  const element = $("#superAgentSyncStatus"); if (!element) return;
  if (viewState.loading) element.textContent = "正在读取…";
  else if (viewState.error) element.textContent = `读取失败：${viewState.error}`;
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

export function bindSuperAgentEvents() {
  $("#openSuperAgent")?.addEventListener("click", () => void openSuperAgentView());
  $("#openTaskManager")?.addEventListener("click", () => { $("#tasksDialog")?.close(); void openSuperAgentView(); });
  $("#superAgentRefresh")?.addEventListener("click", () => void openSuperAgentView());
  $("#superAgentOpenSidebar")?.addEventListener("click", () => openSidebar());
  $("#superAgentExpandSidebar")?.addEventListener("click", () => setLeftSidebarCollapsed(false));
  $("#superAgentThreadList")?.addEventListener("click", (event) => { const button = event.target.closest("[data-super-thread]"); if (!button) return; viewState.selectedId = button.dataset.superThread || ""; renderThreadList(); void loadDetail(viewState.selectedId); });
}
