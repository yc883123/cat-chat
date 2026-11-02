// ============================================================
// 28-super-agent.js —— 任务总管控制面
// ============================================================

import { $, api, escapeHtml, setAppView, state } from "./01-core.js";
import { closeSidebar, sidebarDesktop } from "./14-file-panel.js";
import { closeStudioView, openStudioView, setStudioPage } from "./22-studio.js";
import { openConversation } from "./08-conversations.js";

const viewState = {
  open: false, selectedId: "", threads: [], detail: null, nodes: [], events: [], artifacts: [], approvals: [],
  eventCursor: 0, eventsThreadId: "", eventsInitialized: false,
  loading: false, error: "", actionMessage: "", syncedAt: 0, filter: "all", resultRequest: 0,
};

const createModelState = { requestId: 0, loading: false, submitting: false };
const CREATE_MODEL_HINT = "模型来自全局供应商设置，默认使用全局默认模型，没有独立 API 设置。";
const createState = { resourceToken: 0, bookToken: 0, loading: false, bookLoading: false, books: [], book: null, chapters: [], parts: [], conversations: [], jobs: [], fingerprint: "", requestId: "" };
const mixedDraft = [];
const plannedChapterDependencies = new Map();
const actionRequests = new Map();
const actionInFlight = new Set();
const confirmationNotices = new Map();
let refreshTimer = null;
function scheduleRefresh() {
  if (refreshTimer) clearTimeout(refreshTimer);
  refreshTimer = setTimeout(async () => {
    refreshTimer = null;
    if (state.appView !== "super-agent") return;
    try { if (!viewState.loading && !createModelState.submitting && document.visibilityState !== "hidden") await loadThreads(); }
    catch (error) { viewState.error = error.message || "刷新任务失败"; renderSyncStatus(); }
    finally { if (state.appView === "super-agent") scheduleRefresh(); }
  }, 4000);
}
const CHAINS = { writer: ["setup", "outline", "chapter_plan", "chapter_content"], novel: ["import", "elements", "outline", "screenplay", "review"] };
const SINGLE_STAGES = { writer: { setup_all: "全部设定", outline_all: "全部大纲", split: "拆章", chapter: "单章正文" }, novel: { elements: "提取要素", outline: "改编大纲", script: "生成剧本" } };
function newRequestId() { return `super-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`}`; }
function createError(message) { const box = $("#superAgentCreateError"); if (box) box.textContent = message; }
function domainName(domain) { return ({ writer: "织书台", novel: "拆书匠", chat: "聊天", job: "后台任务", subagent: "子助手" })[domain] || domain; }
function chapterNumber(row) { return Number(row?.i ?? row?.no ?? row?.chapter_no ?? 0); }

const STATUS_LABELS = {
  draft: "草稿", active: "进行中", waiting_user: "等待确认", paused: "已暂停", cancelled: "已取消",
  completed: "已完成", failed: "失败", needs_recovery: "待恢复", archived: "已归档", pending: "待处理",
  ready: "就绪", running: "运行中", stopping: "停止中", succeeded: "已完成", blocked: "已阻塞", interrupted: "已中断",
  not_executed: "未执行", unavailable: "不可用", unknown: "状态未知",
};
const RESULT_COMPATIBILITY_LABELS = { current: "当前结果", legacy_unverified: "旧版结果，未经执行结论核验", incomplete: "正文不完整", pending: "尚未生成最终结果", missing: "结果正文不可用", unavailable: "结果不可用", unknown_outcome: "执行结论无法确认" };
const STAGE_LABELS = {
  setup: "设定", import: "导入", elements: "设定 / 要素", outline: "大纲", chapter_plan: "拆章", chapter_content: "正文", screenplay: "剧本", review: "复核",
  chat: "聊天任务", job: "后台任务", subagent: "子助手任务", setup_all: "全部设定", outline_all: "全部大纲", split: "拆章", chapter: "单章正文", script: "生成剧本",
};
const EVENT_LABELS = { thread_created: "任务已创建", thread_updated: "任务已更新", thread_status_changed: "任务状态更新", node_created: "步骤已安排", node_updated: "步骤已更新", node_status_changed: "步骤状态更新", run_started: "开始执行", run_completed: "执行完成", run_failed: "执行失败", run_interrupted: "执行已中断", approval_requested: "需要你的确认", approval_resolved: "确认已处理", artifact_created: "新产物已保存", artifact_registered: "新产物已保存", control_requested: "收到任务操作", control_completed: "任务操作已处理" };
const STAGE_STATUSES = new Set(["blocked", "ready", "running", "waiting_user", "failed", "succeeded", "cancelled", "interrupted", "pending", "stopping"]);

function statusLabel(value) { return STATUS_LABELS[String(value || "")] || String(value || "未知"); }
function stageLabel(value, fallback = "阶段") {
  const key = String(value || "").trim();
  return text(STAGE_LABELS[key] || key || fallback);
}
function nodeDisplayName(node) {
  if (node?.executor_type === "supervised_chat") return "自动检查并继续";
  return STAGE_LABELS[node?.stage_key] || STAGE_LABELS[node?.domain_ref?.stage] || STAGE_LABELS[node?.kind] || "任务步骤";
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
  const threads = filteredThreads();
  if (!viewState.selectedId || !threads.some((item) => item.thread_id === viewState.selectedId)) {
    viewState.selectedId = threads[0]?.thread_id || "";
    viewState.detail = null; viewState.nodes = []; renderEmptyDetail();
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
    if (viewState.selectedId !== threadId) return;
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

function filteredThreads() {
  return viewState.threads.filter((thread) => viewState.filter === "waiting" ? thread.status === "waiting_user" : viewState.filter === "completed" ? thread.status === "completed" : true);
}

function renderThreadList() {
  const list = $("#superAgentThreadList"); if (!list) return;
  const threads = filteredThreads();
  $("#superAgentThreadCount").textContent = String(threads.length);
  $("#superAgentThreadEmpty").hidden = threads.length > 0;
  $("#superAgentThreadEmpty").textContent = viewState.threads.length ? "没有符合筛选条件的任务。" : "还没有任务，先新建一个吧。";
  $("#superAgentFilters")?.querySelectorAll("[data-super-filter]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.superFilter === viewState.filter)));
  $("#superAgentFilters")?.querySelectorAll("[data-super-filter]").forEach((button) => {
    const filter = button.dataset.superFilter;
    const count = viewState.threads.filter((thread) => filter === "waiting" ? thread.status === "waiting_user" : filter === "completed" ? thread.status === "completed" : true).length;
    button.textContent = `${({ all: "全部", waiting: "待确认", completed: "已完成" })[filter]} ${count}`;
  });
  list.innerHTML = threads.map((thread) => `
    <button class="super-agent-thread ${thread.thread_id === viewState.selectedId ? "active" : ""}" type="button" data-super-thread="${text(thread.thread_id)}">
      <strong><i class="super-agent-task-icon" aria-hidden="true">▤</i>${text(thread.title, "未命名任务")}</strong><span class="super-agent-task-state" data-status="${text(thread.status)}"><i aria-hidden="true"></i>${text(statusLabel(thread.status))}</span><small>${text(thread.goal, "暂无目标")}</small>
    </button>`).join("");
}

function renderEmptyDetail() {
  $("#superAgentDetailEmpty").hidden = false; $("#superAgentDetail").hidden = true;
  $("#superAgentDetail").dataset.threadId = "";
  $("#superAgentApprovalList").innerHTML = '<p class="super-agent-muted">暂无审批项。</p>';
  $("#superAgentArtifactList").innerHTML = '<p class="super-agent-muted">暂无产物引用。</p>';
  if ($("#superAgentNodeList")) $("#superAgentNodeList").innerHTML = '<p class="super-agent-muted">暂无步骤。</p>';
  $("#superAgentChatConfirmationList").innerHTML = "";
  $("#superAgentNextStep").innerHTML = '<p class="super-agent-muted">选择任务后查看下一步。</p>';
  $("#superAgentConversationLinks").innerHTML = "";
  $("#superAgentSupervision").hidden = true;
}

const SUPERVISION_PHASES = {
  pending: "等待开始", ready: "等待开始", running: "正在执行", executing: "正在执行", reviewing: "正在检查结果",
  review_pending: "等待检查", awaiting_review: "等待检查", needs_user: "等待你处理", waiting_user: "等待你处理",
  dispatch_ready: "准备自动补做", dispatching: "正在发送任务", waiting_approval: "等待你确认工具操作", reviewed: "已检查",
  paused: "暂停自动续办", stopping: "正在停止", cancelled: "已取消", done: "目标完成", completed: "目标完成",
  blocked: "未完成，等待处理", failed: "自动续办失败", needs_recovery: "需要确认恢复", limit_reached: "未完成，已达到自动续办上限",
  waiting_background: "等待本轮后台任务完成",
};
function supervisionPhase(supervision) {
  const phase = String(supervision.phase || "pending");
  if (["running", "executing"].includes(phase)) return `执行第 ${Number(supervision.round || 1)}/${Number(supervision.max_rounds || 1)} 轮`;
  return SUPERVISION_PHASES[phase] || "等待任务更新";
}
function renderSupervision(detail) {
  const host = $("#superAgentSupervision");
  const supervision = detail.supervision;
  host.hidden = !supervision;
  if (!supervision) { host.innerHTML = ""; return; }
  const criteria = supervision.criteria || supervision.completion_criteria || [];
  const rounds = Array.isArray(supervision.rounds) ? supervision.rounds : [];
  const usage = supervision.token_usage;
  const usageText = usage == null ? "未知" : typeof usage === "number" ? `${usage} tokens` : usage.total_tokens != null ? `${Number(usage.total_tokens)} tokens` : usage.known_tokens > 0 ? `已知 ${Number(usage.known_tokens)} tokens，部分用量未知` : "未知";
  const elapsed = supervision.review_started_at ? Math.max(0, Math.floor((Date.now() - Number(supervision.review_started_at)) / 1000)) : 0;
  const resultLabel = (value) => ({ passed: "通过", pass: "通过", failed: "未通过", fail: "未通过", unknown: "无法确认", unverifiable: "无法确认" })[value] || "无法确认";
  const criterionLabel = (item) => {
    const criterion = item.criterion ?? item.text ?? item.criterion_id ?? item.index;
    return Number.isInteger(criterion) ? criteria[criterion] || `标准 ${criterion + 1}` : criterion;
  };
  host.innerHTML = `<div class="super-agent-section-title"><h2>自动检查与续办</h2><strong data-supervision-phase="${text(supervision.phase)}">${text(supervisionPhase(supervision))}</strong></div>
    <p class="super-agent-muted">已执行 ${Number(supervision.round || 0)} / ${Number(supervision.max_rounds || 1)} 轮 · 剩余补做 ${Number(supervision.remaining_continuations ?? 0)} 次 · 已检查 ${Number(supervision.review_count || 0)} 次</p>
    <p class="super-agent-muted">截止时间：${text(formatTime(supervision.deadline_at))} · Token 用量：${text(usageText)}</p>
    ${supervision.review_attempt_count != null ? `<p class="super-agent-muted">审查调用 ${Number(supervision.review_attempt_count)} 次 · 内部重试 ${supervision.review_retry_count == null ? "未知" : Number(supervision.review_retry_count)} 次${usage && typeof usage === "object" ? ` · 执行 tokens：${usage.execution_tokens ?? "未知"} · 审查 tokens：${usage.review_tokens ?? "未知"}` : ""}</p>` : ""}
    ${supervision.phase === "reviewing" ? `<p class="super-agent-muted">检查开始于 ${text(formatTime(supervision.review_started_at))}，已等待约 ${elapsed} 秒。</p>` : ""}
    ${["paused", "needs_recovery"].includes(supervision.phase) ? '<p class="super-agent-muted">自动检查和新轮次已暂停；在途执行仍可能完成。继续前会重新核对权限、截止时间和剩余次数。</p>' : ""}
    ${supervision.error ? `<p class="super-agent-error">${text(supervision.error)}</p>` : ""}
    ${supervision.can_accept_completion === true ? '<p class="super-agent-muted">无法确认的标准需要你核对。确认完成会结束自动续办，并保留模型检查结论。</p><button type="button" class="control-button tiny" data-super-accept-completion>我已核对，确认完成</button>' : ""}
    ${supervision.completion_source === "user" ? '<p class="super-agent-user-completion">用户确认完成（原检查结论已保留）</p>' : ""}
    <div><strong>完成标准</strong><ol class="super-agent-criteria">${criteria.map((item) => `<li>${text(typeof item === "object" ? item.text || item.criterion : item)}</li>`).join("")}</ol></div>
    <div class="super-agent-review-rounds">${rounds.map((round) => {
      const review = round.review || {};
      const results = Array.isArray(review.criteria_results) ? review.criteria_results : [];
      return `<details class="super-agent-review-round" ${round === rounds.at(-1) ? "open" : ""}><summary>第 ${Number(round.round || 1)} 轮 · ${text(review.summary || SUPERVISION_PHASES[round.phase] || "等待执行结果")}</summary><div>
        ${review.decision ? `<p>检查结论：${text(({ done: "目标完成", continue: "继续补做", needs_user: "等待你处理", blocked: "无法继续" })[review.decision], "无法确认")}</p>` : ""}
        ${results.length ? `<ul>${results.map((item) => `<li><strong>${text(resultLabel(item.status || item.result))}</strong> · ${text(criterionLabel(item))}${item.judgment === "model" || item.judgment_type === "model" || item.evidence_type === "model" ? "（模型判断）" : ""}${Array.isArray(item.evidence_refs) && item.evidence_refs.length ? `<small>依据：${item.evidence_refs.map((ref) => refText(ref)).join("、")}</small>` : ""}</li>`).join("")}</ul>` : ""}
        ${Array.isArray(review.missing_items) && review.missing_items.length ? `<p>仍缺少：${review.missing_items.map((item) => refText(item)).join("；")}</p>` : ""}
        ${review.progress_summary ? `<p>本轮进展：${text(review.progress_summary)}</p>` : ""}
        ${review.next_instruction ? `<div class="super-agent-next-instruction"><strong>自动补做指令</strong><p>${text(review.next_instruction)}</p></div>` : ""}
        ${review.user_question ? `<p class="super-agent-user-question">需要你处理：${text(review.user_question)}</p>` : ""}
        ${round.run_id ? '<small>本轮执行记录已保留，可回到关联会话查看完整对话。</small>' : ""}</div></details>`;
    }).join("") || '<p class="super-agent-muted">开始后在这里保留每轮结果与检查依据。</p>'}</div>`;
}

function renderOverview(detail, nodes, currentNodeId) {
  const completed = nodes.filter((node) => node.status === "succeeded").length;
  const current = nodes.find((node) => node.node_id === currentNodeId) || nodes.find((node) => !["succeeded", "cancelled"].includes(node.status));
  $("#superAgentProgressText").textContent = nodes.length ? `${completed} / ${nodes.length} 步已完成` : "暂无步骤";
  const progress = $("#superAgentProgress"); progress.hidden = !nodes.length; progress.max = nodes.length || 1; progress.value = completed;
  $("#superAgentCurrentStage").textContent = current ? nodeDisplayName(current) : detail.status === "completed" ? "全部完成" : "暂无进行中的步骤";
  $("#superAgentCurrentHint").textContent = current ? statusLabel(current.status) : "";
  if (detail.supervision) {
    $("#superAgentCurrentStage").textContent = supervisionPhase(detail.supervision);
    $("#superAgentCurrentHint").textContent = "每轮结束后按完成标准检查";
    $("#superAgentProgress").hidden = true;
    $("#superAgentProgressText").textContent = `已执行 ${Number(detail.supervision.round || 0)} 轮 · 已检查 ${Number(detail.supervision.review_count || 0)} 次`;
  }
  $("#superAgentUpdatedAt").textContent = formatTime(detail.updated_at || viewState.events.at(-1)?.created_at);
  const next = nodes.find((node) => ["ready", "pending", "blocked"].includes(node.status) && node.node_id !== currentNodeId);
  const confirmations = Array.isArray(detail.chat_confirmations) ? detail.chat_confirmations : [];
  const pending = confirmations.some((item) => item.status === "pending") || viewState.approvals.some((item) => item.status === "pending");
  const settled = ["completed", "cancelled", "failed", "archived"].includes(detail.status);
  const settledTitle = detail.status === "completed" ? detail.supervision ? "目标完成" : "全部步骤已完成" : detail.status === "cancelled" ? "任务已取消" : detail.status === "failed" ? "需要你处理" : "任务已结束";
  const nextHint = settled ? detail.status === "failed" ? "本次执行未完成，请查看结果与原因，再决定是否重新执行。" : "本任务已结束，结果和执行记录已保留。" : pending ? "先处理右侧待确认事项，再查看任务是否继续。" : detail.status === "paused" ? "任务已暂停，继续后再处理后续步骤。" : next ? "按任务设置推进，也可在步骤中手动开始。" : current ? "当前步骤结束后会更新结果。" : "没有待执行步骤。";
  $("#superAgentNextStep").innerHTML = `<div class="super-agent-ref"><strong>${settled ? settledTitle : next ? text(nodeDisplayName(next)) : current ? "等待当前步骤结束" : "暂无下一步"}</strong><p>${text(nextHint)}</p></div>`;
  const notice = confirmationNotices.get(viewState.selectedId);
  $("#superAgentChatConfirmationList").innerHTML = confirmations.map((item) => {
    const subagent = item.executor_kind === "subagent";
    const status = notice?.confirmId === item.confirm_id && notice?.runId === item.run_id ? "expired" : item.status;
    const pending = status === "pending";
    return `<article class="super-agent-ref super-agent-chat-confirmation" data-confirm-id="${text(item.confirm_id)}" data-confirm-status="${text(status)}"><strong>${status === "processing" ? "已确认，工具正在执行" : pending ? "等待你确认" : "确认已失效，请同步任务"}</strong><p>${text(item.tool)} · ${text(item.reason, "请核对本次操作。")}</p>
      ${subagent ? `<p>${text(item.scope, "仅限当前执行")} · 来源：${text(item.action_source)}</p>${item.parameters ? `<pre class="super-agent-confirm-parameters">${text(JSON.stringify(item.parameters, null, 2))}</pre>` : ""}${pending ? `<div class="super-agent-node-actions"><button type="button" class="control-button tiny" data-super-tool-decision="confirm" data-confirm-id="${text(item.confirm_id)}" data-run-id="${text(item.run_id)}">允许执行</button><button type="button" class="control-button tiny" data-super-tool-decision="reject" data-confirm-id="${text(item.confirm_id)}" data-run-id="${text(item.run_id)}">拒绝</button></div>` : ""}` : `<button type="button" class="control-button tiny" data-super-conversation="${text(item.conversation_id)}">${item.status === "processing" ? "回到聊天查看" : "回到聊天确认"}</button>`}</article>`;
  }).join("");
  if (notice && !notice.dismissed) $("#superAgentChatConfirmationList").insertAdjacentHTML("beforeend", `<article class="super-agent-ref" data-confirm-status="expired"><p>${text(notice.message)}</p><button type="button" class="control-button tiny" data-super-dismiss-confirmation>知道了</button></article>`);
}

function renderDetail() {
  const detail = viewState.detail; if (!detail) return renderEmptyDetail();
  $("#superAgentDetailEmpty").hidden = true; $("#superAgentDetail").hidden = false;
  $("#superAgentDetail").dataset.threadId = detail.thread_id || viewState.selectedId;
  $("#superAgentTitle").textContent = detail.title || "未命名任务";
  $("#superAgentCrumbTitle").textContent = detail.title || "未命名任务";
  $("#superAgentGoal").textContent = detail.goal || "暂无目标";
  const latestReview = detail.supervision?.rounds?.filter((round) => round.review?.summary).at(-1)?.review;
  const decision = latestReview?.summary || detail.summary || "";
  $("#superAgentSummary").textContent = decision;
  $("#superAgentDecisionSection").hidden = !decision;
  $("#superAgentDecisionAt").textContent = formatTime(detail.updated_at);
  const status = $("#superAgentStatus"); status.textContent = statusLabel(detail.status); status.dataset.status = detail.status || "";
  const terminal = ["completed", "cancelled", "failed", "archived"].includes(detail.status);
  $("#superAgentThreadActions")?.querySelectorAll("[data-super-thread-action]").forEach((button) => {
    button.disabled = terminal || (button.dataset.superThreadAction === "pause" && !["active", "waiting_user"].includes(detail.status));
    if (detail.supervision && button.dataset.superThreadAction === "pause") button.textContent = "暂停自动续办";
    if (detail.supervision && button.dataset.superThreadAction === "continue") button.textContent = "继续自动续办";
    if (!detail.supervision && button.dataset.superThreadAction === "pause") button.textContent = "暂停任务";
    if (!detail.supervision && button.dataset.superThreadAction === "continue") button.textContent = "继续任务";
  });
  $("#superAgentConversationLinks").innerHTML = (detail.linked_conversation_ids || []).map((id) => {
    const title = state.conversations?.find((row) => row.id === id)?.title;
    return `<button type="button" class="control-button tiny" data-super-conversation="${text(id)}">${title ? `回到关联会话 · ${text(title)}` : "查看关联会话"}</button>`;
  }).join("");
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
  renderOverview(detail, nodes, currentNodeId);
  renderSupervision(detail);
  $("#superAgentChainMeta").textContent = `${nodes.length} 个步骤`;
  $("#superAgentNodeList").innerHTML = nodes.length ? nodes.map((node, index) => {
    const nodeId = String(node.node_id || ""); const nodeStatus = String(node.status || "pending");
    const active = nodeId === currentNodeId;
    const approvals = approvalByNode.get(nodeId) || []; const artifacts = artifactByNode.get(nodeId) || [];
    return `<article class="super-agent-stage ${active ? "is-current" : ""} status-${statusClass(nodeStatus)}" data-node-id="${text(nodeId)}">
      <div class="super-agent-stage-rail"><span class="super-agent-stage-index">${index + 1}</span>${index < nodes.length - 1 ? "<i aria-hidden=\"true\"></i>" : ""}</div>
      <div class="super-agent-stage-body"><div class="super-agent-stage-head"><div><strong>${text(nodeDisplayName(node))}</strong></div><span class="super-agent-status" data-status="${text(nodeStatus)}">${text(statusLabel(nodeStatus))}</span></div>
      <p class="super-agent-stage-meta">第 ${index + 1} 步${(node.dependencies || []).length ? ` · 等待 ${(node.dependencies || []).length} 个前置步骤完成` : ""}${Number(node.attempt) > 0 ? ` · 第 ${Number(node.attempt) + 1} 次执行` : ""}</p>
      ${active ? '<span class="super-agent-current-badge">当前步骤</span>' : ""}
      ${node.domain_ref ? `<p class="super-agent-stage-meta">${text(domainName(node.domain_ref.domain))} · ${stageLabel(node.domain_ref.stage, "作品任务")}</p><button type="button" class="control-button tiny" data-super-return-domain="${text(node.domain_ref.domain)}" data-super-book-id="${text(node.domain_ref.book_id)}">回到工作台</button>` : ""}
      ${approvals.length ? `<div class="super-agent-linked"><b>审批</b>${approvals.map((item) => `<span>${text(item.action, "待确认操作")} · ${text(statusLabel(item.status))}</span>`).join("")}</div>` : ""}
      ${artifacts.length ? `<div class="super-agent-linked"><b>产物</b>${artifacts.map((item) => `<span>${refText(item.title || item.path_or_ref, item.kind)}</span>`).join("")}</div>` : ""}
      ${node.error ? `<small class="super-agent-error">${text(node.error)}</small>` : ""}
      <div class="super-agent-node-actions">${node.capabilities?.start ? `<button type="button" class="control-button tiny" data-super-action="start" data-super-node="${text(nodeId)}">开始</button>` : ""}${!['succeeded','failed','cancelled'].includes(nodeStatus) ? `<button type="button" class="control-button tiny" data-super-action="sync" data-super-node="${text(nodeId)}">同步</button>` : ""}${node.capabilities?.cancel ? `<button type="button" class="control-button tiny" data-super-action="cancel" data-super-node="${text(nodeId)}">取消</button>` : ""}
      ${node.capabilities?.retry ? `<button type="button" class="control-button tiny" data-super-action="retry" data-super-node="${text(nodeId)}" title="重新调用模型和工具，可能重复已发生的外部操作">重试（重新执行）</button>` : ""}
      ${node.capabilities?.resume ? `<button type="button" class="control-button tiny" data-super-action="resume" data-super-node="${text(nodeId)}">从检查点继续</button>` : ""}
      ${node.executor_id && ["chat_run", "subagent", "job", "supervised_chat"].includes(node.executor_type) ? `<button type="button" class="control-button tiny" data-super-result="${text(nodeId)}">查看本次结果</button>` : ""}</div>
      </div></article>`;
  }).join("") : '<p class="super-agent-muted">暂无步骤。</p>';
  $("#superAgentEventList").innerHTML = viewState.events.length ? [...viewState.events].reverse().map((event) => `
    <article class="super-agent-event"><div><strong>${text(EVENT_LABELS[event.event_type], "任务活动")}</strong><time>${text(formatTime(event.created_at))}</time></div><p>${text(event.summary?.message || event.summary?.description || (event.summary?.status ? statusLabel(event.summary.status) : "状态已更新"))}</p></article>`).join("") : '<p class="super-agent-muted">暂无活动。</p>';
  $("#superAgentApprovalList").innerHTML = viewState.approvals.length ? viewState.approvals.map((item) => `
    <article class="super-agent-ref"><strong>${text(item.action, "待确认操作")}</strong><span>${text(statusLabel(item.status))}</span><p>步骤：${text(nodeDisplayName(nodeById.get(String(item.node_id))))}</p><p>${text(item.description)}</p>${item.status === 'pending' ? `<div class="super-agent-node-actions"><button type="button" class="control-button tiny" data-super-approval="approved" data-super-approval-id="${text(item.approval_id)}">批准</button><button type="button" class="control-button tiny" data-super-approval="rejected" data-super-approval-id="${text(item.approval_id)}">拒绝</button></div>` : ""}</article>`).join("") : (detail.chat_confirmations?.length ? "" : '<p class="super-agent-muted">暂无待确认事项。</p>');
  $("#superAgentArtifactList").innerHTML = viewState.artifacts.length ? viewState.artifacts.map((item) => `
    <article class="super-agent-ref"><strong>${text(item.title, "任务产物")}</strong><p>步骤：${text(nodeDisplayName(nodeById.get(String(item.node_id))))}</p><p>${text(item.summary)}</p><code>${text(item.path_or_ref)}</code></article>`).join("") : '<p class="super-agent-muted">暂无产物。</p>';
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
  scheduleRefresh();
}
export function closeSuperAgentView() { if (refreshTimer) clearTimeout(refreshTimer); refreshTimer = null; if (state.appView === "super-agent") setView("chat"); }

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
  renderCreateModelSummary();
}

function renderCreateModelSummary() {
  const summary = $("#superAgentCreateModelSummary");
  if (summary) summary.textContent = $("#superAgentCreateModel")?.selectedOptions[0]?.textContent || "跟随全局默认";
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

function renderCreateMode() {
  const domain = $("#superAgentCreateDomain").value;
  const isDomain = Boolean(CHAINS[domain]);
  const chain = isDomain && $("#superAgentCreateMode").value === "chain";
  const stage = $("#superAgentCreateStage").value;
  $("#superAgentCreateDomainFields").hidden = !isDomain;
  $("#superAgentCreateConversationFields").hidden = isDomain;
  const supervised = domain === "chat" && $("#superAgentCreateExecutionMode").value === "supervised";
  $("#superAgentCreateChatMode").hidden = domain !== "chat";
  $("#superAgentCreateSupervisionFields").hidden = !supervised;
  $("#superAgentCreateCriteria").required = supervised;
  $("#superAgentCreateMixedOptions").hidden = false;
  if (supervised && mixedDraft.length) $("#superAgentCreateMixedOptions").open = true;
  $("#superAgentCreateAddStep").disabled = supervised;
  $("#superAgentCreateStageWrap").hidden = !isDomain || chain;
  $("#superAgentCreateChainPreview").hidden = !chain;
  $("#superAgentCreateChainPreview").innerHTML = (CHAINS[domain] || []).map((stage) => `<li>${stageLabel(stage)}</li>`).join("");
  $("#superAgentCreateWriterRange").hidden = domain !== "writer" || (!chain && !["split", "chapter"].includes(stage));
  $("#superAgentCreateWriterFields").hidden = domain !== "writer" || (!chain && !["outline_all", "split"].includes(stage));
  for (const id of ["Part", "From", "Count"]) $("#superAgentCreate" + id + "Wrap").hidden = !chain && stage !== "split";
  $("#superAgentCreateChapterWrap").hidden = !chain && stage !== "chapter";
  $("#superAgentCreateNovelRange").hidden = domain !== "novel";
  $("#superAgentCreateAutoAdvanceWrap").hidden = !chain;
  $("#superAgentCreateGoal").required = ["chat", "subagent"].includes(domain);
  $("#superAgentCreateGoalHint").textContent = domain === "chat" ? "开始时会把这段内容发送到所选会话。" : domain === "subagent" ? "子助手会按这段要求处理，沿用所选会话的权限。" : domain === "job" ? "可选：备注这项后台任务的目标。" : "可选：补充要求；作品内容沿用工作台中已保存的版本。";
  $("#superAgentCreateModelOptions").hidden = domain === "job";
  $("#superAgentCreateJobWrap").hidden = domain !== "job";
  $("#superAgentCreateAutostart").disabled = domain === "job" && !mixedDraft.length;
  $("#superAgentCreateRuntimeHint").textContent = supervised && mixedDraft.length ? `已保留 ${mixedDraft.length} 个步骤。自动检查目前只能独立创建；请切换为单次执行继续添加，或移除下面的步骤。` : domain === "job" ? "关联仅跟踪已有任务，不会重新运行。整套任务的立即开始选择仍保留。" : domain === "subagent" ? "使用所选会话的工作区与权限，新建独立子任务；不会自动读取原会话聊天记录，请把要求写完整。结果保存在独立任务中，可在总管查看。" : "开始后会向所选会话发送消息，沿用聊天历史；结果为本次执行的回复。只创建任务时不会发送。";
  renderCreateRangeHint();
}

function updateCreateParts() {
  createState.parts = $("#superAgentCreateParts").value.split(/\r?\n/).map((title) => title.trim()).filter(Boolean).map((title, index) => ({ no: index + 1, title }));
  const select = $("#superAgentCreatePart"); const previous = select.value;
  select.replaceChildren();
  createState.parts.forEach((row) => select.append(new Option(`第 ${row.no} 部 · ${row.title}`, String(row.no))));
  if ([...select.options].some((option) => option.value === previous)) select.value = previous;
  renderCreateRangeHint();
}

function renderCreateRangeHint() {
  const domain = $("#superAgentCreateDomain").value;
  const hint = $("#superAgentCreateRangeHint");
  if (domain === "writer") {
    const from = Number($("#superAgentCreateFrom").value);
    const count = Number($("#superAgentCreateCount").value);
    const chapter = Number($("#superAgentCreateChapter").value);
    const chain = $("#superAgentCreateMode").value === "chain";
    const stage = $("#superAgentCreateStage").value;
    const range = chain || stage === "split" ? `拆章范围：第 ${from}–${from + count - 1} 章。` : "";
    const target = chain || stage === "chapter" ? `正文仅生成第 ${chapter} 章。` : "";
    hint.textContent = `按作品已保存的创意生成。${range}${target}已有内容会保留为历史版本。`;
  } else if (domain === "novel") {
    hint.textContent = "使用所选范围内已导入的原文；完成后需要你确认复核。请先在拆书匠导入原文。";
  } else hint.textContent = "";
}

async function loadCreateBook() {
  const token = ++createState.bookToken;
  const domain = $("#superAgentCreateDomain").value;
  const bookId = $("#superAgentCreateBookId").value;
  createState.book = null; createState.chapters = []; createState.parts = [];
  if (!CHAINS[domain] || !bookId) return;
  createState.bookLoading = true;
  $("#superAgentCreateBookHint").textContent = "正在读取作品与章节…";
  try {
    const result = await api(`/api/${domain}/books/${encodeURIComponent(bookId)}`);
    if (token !== createState.bookToken || domain !== $("#superAgentCreateDomain").value) return;
    createState.book = result.book;
    createState.chapters = (result.chapters || []).filter((row) => chapterNumber(row) > 0).sort((a, b) => chapterNumber(a) - chapterNumber(b));
    createState.parts = Array.isArray(result.outline?.parts) ? result.outline.parts : [];
    for (const id of ["superAgentCreateChapterStart", "superAgentCreateChapterEnd"]) {
      const select = $(`#${id}`); select.replaceChildren();
      createState.chapters.forEach((row) => select.append(new Option(`第 ${chapterNumber(row)} 章 · ${row.title || "未命名"}`, String(chapterNumber(row)))));
    }
    $("#superAgentCreateChapterEnd").value = String(chapterNumber(createState.chapters.at(-1)) || "");
    $("#superAgentCreateParts").value = createState.parts.length ? createState.parts.map((row) => row.title || "").join("\n") : "第一部：起点\n第二部：转折";
    updateCreateParts();
    $("#superAgentCreateBookHint").textContent = `《${result.book?.title || "未命名作品"}》 · ${createState.chapters.length} 章${result.broken?.length ? `，${result.broken.length} 章读取异常，请先修复` : ""}`;
    if (domain === "writer") {
      $("#superAgentCreateChapter").value = String(chapterNumber(createState.chapters[0]) || 1);
      const nextChapter = (chapterNumber(createState.chapters.at(-1)) || 0) + 1;
      $("#superAgentCreateFrom").value = String(nextChapter);
      if ($("#superAgentCreateMode").value === "chain") $("#superAgentCreateChapter").value = String(nextChapter);
    }
    renderCreateRangeHint();
  } catch (error) { if (token === createState.bookToken) { createError(error.message || "作品读取失败"); $("#superAgentCreateBookHint").textContent = "读取失败，请刷新作品后重试。"; } }
  finally { if (token === createState.bookToken) createState.bookLoading = false; }
}

function renderCreateJobs() {
  const select = $("#superAgentCreateJob"); select.replaceChildren();
  const conversationId = $("#superAgentCreateConversation").value;
  createState.jobs.filter((job) => job.conversation_id === conversationId).forEach((job) => select.append(new Option(`${job.message || job.label || "后台任务"} · ${statusLabel(job.status)}`, job.id)));
  if (!select.options.length) select.append(new Option("此会话暂无可关联后台任务", ""));
}

async function loadCreateResources({ bookId = "", conversationId = "" } = {}) {
  const token = ++createState.resourceToken;
  ++createState.bookToken;
  createState.book = null; createState.bookLoading = false; createState.parts = []; createState.chapters = [];
  const domain = $("#superAgentCreateDomain").value;
  const stage = $("#superAgentCreateStage"); stage.replaceChildren();
  Object.entries(SINGLE_STAGES[domain] || {}).forEach(([key, label]) => stage.append(new Option(label, key)));
  createState.loading = true; clearCreateError(); renderCreateMode();
  const select = $(CHAINS[domain] ? "#superAgentCreateBookId" : "#superAgentCreateConversation");
  select.replaceChildren(new Option("正在读取…", "")); select.disabled = true;
  try {
    if (CHAINS[domain]) {
      const result = await api(`/api/${domain}/books`);
      if (token !== createState.resourceToken) return;
      createState.books = Array.isArray(result.books) ? result.books : [];
      select.replaceChildren();
      createState.books.forEach((book) => select.append(new Option(book.title || "未命名作品", book.id)));
      if (bookId && createState.books.some((book) => book.id === bookId)) select.value = bookId;
      if (!select.options.length) select.append(new Option("暂无作品，请先到工作台新建 / 导入", ""));
      $("#superAgentCreateBookHint").textContent = select.value ? "" : "暂无作品。织书台先保存故事创意和部标题；拆书匠先导入原文章节。";
      await loadCreateBook();
    } else {
      const [conversations, jobs] = await Promise.all([api("/api/conversations"), domain === "job" ? api("/api/tasks?jobs_only=1") : Promise.resolve({ tasks: [] })]);
      if (token !== createState.resourceToken) return;
      createState.conversations = conversations.conversations || []; createState.jobs = jobs.tasks || [];
      select.replaceChildren();
      createState.conversations.forEach((conversation) => select.append(new Option(conversation.title || "未命名会话", conversation.id)));
      if (!select.options.length) select.append(new Option("暂无会话，请先在聊天页新建", ""));
      const preferredConversation = conversationId || state.conversationId;
      if (createState.conversations.some((row) => row.id === preferredConversation)) select.value = preferredConversation;
      renderCreateJobs();
    }
  } catch (error) { if (token === createState.resourceToken) { select.replaceChildren(new Option("读取失败，请重试", "")); createError(error.message || "读取失败"); } }
  finally { if (token === createState.resourceToken) { createState.loading = false; select.disabled = false; } }
}

async function openCreateDomainDialog(prefill = {}) {
  const dialog = $("#superAgentCreateDialog");
  if (!dialog || typeof dialog.showModal !== "function") return;
  // Failed submissions keep their request ID while the payload is unchanged.
  clearCreateError();
  const providerKey = renderCreateProviders($("#superAgentCreateProvider")?.value || "");
  void loadCreateModels(providerKey, createPreferredModel(providerKey));
  if (["writer", "novel", "chat", "job", "subagent"].includes(prefill.domain)) $("#superAgentCreateDomain").value = prefill.domain;
  else if (prefill.conversationId) $("#superAgentCreateDomain").value = "chat";
  if (!dialog.open) dialog.showModal();
  await loadCreateResources(prefill);
}

function renderMixedDraft() {
  $("#superAgentCreateDraftStatus").textContent = mixedDraft.length ? `已保留 ${mixedDraft.length} 个步骤，可继续添加或逐项移除。` : "";
  $("#superAgentCreateSteps").innerHTML = mixedDraft.map((node, index) => {
    const request = node.start_request || {};
    const range = node.stage === "split" ? `第 ${Number(request.part_no)} 部，第 ${Number(request.from_no)}–${Number(request.from_no) + Number(request.want_count) - 1} 章` : node.stage === "chapter" ? `第 ${Number(request.chapter_no)} 章` : request.chapter_ids?.length ? `原文第 ${request.chapter_ids[0]}–${request.chapter_ids.at(-1)} 章` : "";
    return `<li><span>${text(node.title)} · ${text(SINGLE_STAGES[node.domain]?.[node.stage] || domainName(node.domain))}${range ? ` · ${text(range)}` : ""}${node.model_name ? ` · ${text(node.model_name)}` : ""}${plannedChapterDependencies.has(node.key) ? " · 等待前序拆章生成" : ""}</span><button type="button" class="control-button tiny" data-super-remove-step="${index}">移除</button></li>`;
  }).join("");
  $("#superAgentCreateMixedSubmit").disabled = !mixedDraft.length || createModelState.submitting;
  $("#superAgentCreateAutostart").disabled = $("#superAgentCreateDomain").value === "job" && !mixedDraft.length;
}

async function submitCreateBody(body, { mixed = false } = {}) {
  const fingerprint = JSON.stringify(body);
  if (fingerprint !== createState.fingerprint || !createState.requestId) { createState.fingerprint = fingerprint; createState.requestId = newRequestId(); }
  body.request_id = createState.requestId; body.idempotency_key = createState.requestId;
  if (body.chain || mixed) body.chain_instance_id = createState.requestId;
  createModelState.submitting = true;
  const submit = $("#superAgentCreateSubmit");
  if (submit) { submit.disabled = true; submit.textContent = "正在创建…"; }
  renderMixedDraft();
  try {
    const result = await api("/api/super-agent/threads", { method: "POST", body });
    const threadId = result.thread?.thread_id || result.thread_id;
    if (!threadId) throw new Error("服务端未返回新任务编号，请保持表单并重试以确认创建结果。");
    viewState.selectedId = threadId;
    viewState.detail = null; viewState.nodes = []; renderEmptyDetail();
    $("#superAgentCreateDialog")?.close();
    createState.fingerprint = ""; createState.requestId = "";
    if (mixed) { mixedDraft.splice(0); plannedChapterDependencies.clear(); }
    viewState.filter = "all";
    await loadThreads();
  } catch (error) { createError(error.message || "创建任务失败"); }
  finally { createModelState.submitting = false; if (submit) { submit.disabled = false; submit.textContent = "创建任务"; } renderMixedDraft(); }
}

async function submitMixedDraft() {
  if (createModelState.submitting || !mixedDraft.length) return;
  clearCreateError();
  const invalid = mixedDraft.findIndex((node, index) => plannedChapterDependencies.has(node.key) && !draftProvidesChapter(node.book_id, Number(node.start_request?.chapter_no), index));
  if (invalid >= 0) return createError(`第 ${invalid + 1} 步的正文依赖前序拆章，请恢复同作品且覆盖目标章的拆章步骤，或移除此正文步骤。`);
  await submitCreateBody({ domain: "mixed", title: $("#superAgentCreateName").value.trim() || "多步骤任务", goal: $("#superAgentCreateGoal").value.trim(), autostart: $("#superAgentCreateAutostart").checked, auto_advance: $("#superAgentCreateMixedAutoAdvance").checked, nodes: mixedDraft.map((node) => ({ ...node })) }, { mixed: true });
}

function draftProvidesChapter(bookId, chapter, before = mixedDraft.length) {
  return mixedDraft.slice(0, before).some((node) => node.domain === "writer" && node.book_id === bookId && node.stage === "split" && chapter >= Number(node.start_request?.from_no) && chapter < Number(node.start_request?.from_no) + Number(node.start_request?.want_count));
}

async function createDomainThread(event, { addStep = false } = {}) {
  event?.preventDefault();
  if (createModelState.submitting) return;
  const domain = String($("#superAgentCreateDomain")?.value || "writer").trim().toLowerCase();
  const bookId = String($("#superAgentCreateBookId")?.value || "").trim();
  const stage = String($("#superAgentCreateStage")?.value || "").trim();
  const isDomain = Boolean(CHAINS[domain]);
  const chain = isDomain && $("#superAgentCreateMode").value === "chain";
  if (addStep && chain) return createError("当前“做到哪一步”为“从设定到成稿”，包含多个阶段。请在上方改为“只做一个步骤”，选择具体阶段后再添加。本次尚未添加步骤。");
  if (createState.loading || createState.bookLoading) return createError("正在读取作品或会话，请稍候。");
  if (isDomain && (!bookId || !createState.book)) return createError("请先选择有效作品；没有作品时可到工作台新建 / 导入。");
  const providerKey = String($("#superAgentCreateProvider")?.value || "").trim();
  const modelName = String($("#superAgentCreateModel")?.value || "").trim();
  const provider = createProviderProfiles().find((item) => String(item?.model_key || "") === providerKey);
  const providerId = String(provider?.id || "").trim();
  const errorBox = $("#superAgentCreateError");
  if (errorBox) errorBox.textContent = "";
  if (domain !== "job" && (!providerKey || !modelName)) {
    $("#superAgentCreateModelOptions").open = true;
    if (errorBox) errorBox.textContent = !providerKey
      ? "请先在设置中配置并选择供应商。"
      : !modelName
        ? "请选择一个可用模型。"
        : "请选择有效供应商和模型。";
    return;
  }
  const catalog = state.providerModelCatalogs[providerKey] || [];
  if (domain !== "job" && (createModelState.loading || !catalog.some((entry) => String(typeof entry === "object" ? (entry?.id ?? entry?.model ?? entry?.name ?? "") : entry).trim() === modelName))) {
    if (errorBox) errorBox.textContent = "请先重新检测并选择一个可用模型。";
    return;
  }
  const selection = { provider_id: providerId, model_key: providerKey, model_name: modelName };
  const conversationId = $("#superAgentCreateConversation").value;
  const goal = $("#superAgentCreateGoal").value.trim();
  const title = $("#superAgentCreateName").value.trim() || `${domainName(domain)} · ${isDomain ? createState.book?.title || "作品" : createState.conversations.find((row) => row.id === conversationId)?.title || "任务"}`;
  const body = { domain, title, goal, autostart: domain !== "job" && $("#superAgentCreateAutostart").checked, auto_advance: chain && $("#superAgentCreateAutoAdvance").checked };
  if (domain === "chat" && $("#superAgentCreateExecutionMode").value === "supervised") {
    const criteria = $("#superAgentCreateCriteria").value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
    const maxContinuations = Number($("#superAgentCreateContinuations").value);
    const windowMinutes = Number($("#superAgentCreateWindow").value);
    if (!criteria.length) return createError("请填写至少一条完成标准，才能自动检查并继续。");
    if (criteria.length > 20 || criteria.some((item) => item.length > 1000)) return createError("完成标准最多 20 条，每条不超过 1000 字。");
    if (!Number.isInteger(maxContinuations) || maxContinuations < 1 || maxContinuations > 5) return createError("最多自动补做次数须为 1–5 次。");
    if (!Number.isInteger(windowMinutes) || windowMinutes < 1 || windowMinutes > 120) return createError("运行窗口须为 1–120 分钟。");
    if (addStep || mixedDraft.length) return createError("自动检查任务须独立创建，请先移除已添加的步骤。");
    body.execution_mode = "supervised";
    body.supervision = { completion_criteria: criteria, max_continuations: maxContinuations, window_minutes: windowMinutes };
  }
  if (domain !== "job") Object.assign(body, selection);
  let startRequest = {};
  if (domain === "writer") {
    const from = Number($("#superAgentCreateFrom").value);
    const count = Number($("#superAgentCreateCount").value);
    const chapter = Number($("#superAgentCreateChapter").value);
    const partNo = Number($("#superAgentCreatePart").value);
    const splitNeeded = chain || stage === "split";
    const chapterNeeded = chain || stage === "chapter";
    if ((chain || stage === "outline_all") && (!createState.parts.length || createState.parts.length > 20 || createState.parts.some((row) => !String(row.title || "").trim() || row.title.length > 80))) return createError("请填写 1–20 部标题，每部标题不超过 80 字。");
    if (splitNeeded && (!Number.isInteger(from) || from < 1 || !Number.isInteger(count) || count < 1 || count > 999 || !partNo)) return createError("请选择拆章所属部，起始章号为正整数，拆章数量须为 1–999。");
    if (chapterNeeded && (!Number.isInteger(chapter) || chapter < 1)) return createError("正文目标章须为正整数。");
    if (!chain && stage === "chapter" && !createState.chapters.some((row) => chapterNumber(row) === chapter) && !(addStep && draftProvidesChapter(bookId, chapter))) return createError("正文目标章不在作品中，请先到工作台拆章，或先添加同作品且覆盖目标章的拆章步骤。");
    if (chain && (chapter < from || chapter >= from + count)) return createError("要生成正文的章节须位于本次拆章范围内。");
    const requests = {
      setup: { extra_note: goal },
      outline: { part_plan: createState.parts.map((row, index) => ({ no: Number(row.no || index + 1), title: row.title })), extra_note: goal },
      chapter_plan: { part_no: partNo, from_no: from, want_count: count, extra_note: goal },
      chapter_content: { chapter_no: chapter, extra_note: goal },
    };
    if (chain) body.stage_requests = requests;
    else startRequest = { ...selection, kind: stage, ...(requests[({ setup_all: "setup", outline_all: "outline", split: "chapter_plan", chapter: "chapter_content" })[stage]] || {}) };
  } else if (domain === "novel") {
    const first = Number($("#superAgentCreateChapterStart").value);
    const last = Number($("#superAgentCreateChapterEnd").value);
    const chapterIds = createState.chapters.map(chapterNumber).filter((number) => number >= first && number <= last);
    if (!first || !last || first > last || !chapterIds.length) return createError("请选择已导入原文的有效起止章节。");
    startRequest = { ...selection, book_id: bookId, stage, chapter_ids: chapterIds };
    if (chain) body.stage_requests = Object.fromEntries(CHAINS.novel.map((key) => [key, { chapter_ids: chapterIds }]));
  } else {
    if (!conversationId) return createError("请选择有效会话；没有会话时请先在聊天页新建。");
    body.conversation_id = conversationId;
    if (domain === "chat" || domain === "subagent") {
      if (!goal) return createError("请先写下你想完成什么。");
      startRequest = domain === "chat" ? { message: goal, model_key: providerKey, model_name: modelName } : { instruction: goal };
    } else {
      const jobId = $("#superAgentCreateJob").value;
      if (!createState.jobs.some((job) => job.id === jobId && job.conversation_id === conversationId)) return createError("请选择此会话已有的后台任务。");
      body.job_id = jobId;
    }
  }
  if (isDomain) body.book_id = bookId;
  if (chain) { body.chain = `${domain}_v1`; body.chain_version = 1; }
  else if (isDomain) body.stage = stage;
  if (!chain && domain !== "job") Object.assign(body, { start_request: startRequest });
  if (addStep) {
    if (chain) return createError("添加到步骤清单前，请先选择“只做一个步骤”。");
    if (mixedDraft.length >= 20) return createError("一套任务最多加入 20 个步骤。");
    delete body.autostart; delete body.auto_advance;
    const key = newRequestId();
    mixedDraft.push({ key, ...body });
    if (domain === "writer" && stage === "chapter" && !createState.chapters.some((row) => chapterNumber(row) === Number(startRequest.chapter_no))) plannedChapterDependencies.set(key, true);
    renderMixedDraft();
    $("#superAgentCreateDraftStatus").textContent = `已添加第 ${mixedDraft.length} 步：${SINGLE_STAGES[domain]?.[stage] || domainName(domain)}${plannedChapterDependencies.has(key) ? "，等待前序拆章生成目标章节。" : "。"}`;
    return;
  }
  if (mixedDraft.length) return createError("已有待创建步骤，请用「创建整套任务」，或移除步骤后创建当前任务。");
  await submitCreateBody(body);
}

async function runNodeAction(action, nodeId = "") {
  if (!viewState.selectedId || viewState.detail?.thread_id !== viewState.selectedId) return;
  const threadId = viewState.selectedId;
  const key = `${threadId}:${nodeId}:${action}`;
  if (actionInFlight.has(key)) return;
  if (action === "retry" && !window.confirm("重试会重新调用模型和工具，可能重复已经发生的外部操作。确认重新执行？")) return;
  if (action === "accept_completion" && !window.confirm("请确认你已核对完成标准与结果。确认后将结束自动续办，模型原检查结论会保留。")) return;
  if (action === "cancel" && !window.confirm(nodeId ? "确认取消这个步骤？正在执行的内容会收到停止请求。" : "确认取消整个任务及其后续步骤？已产出的内容不会删除。")) return;
  actionInFlight.add(key);
  if (!actionRequests.has(key)) actionRequests.set(key, newRequestId());
  viewState.error = "";
  viewState.actionMessage = "正在处理…"; renderSyncStatus();
  try {
    await api(`/api/super-agent/threads/${encodeURIComponent(threadId)}/actions`, {
      method: "POST", body: { action, node_id: nodeId, request_id: actionRequests.get(key), ...(action === "accept_completion" ? { completion_source: "user" } : {}) },
    });
    actionRequests.delete(key);
    if (viewState.selectedId === threadId) await loadDetail(threadId);
  } catch (error) {
    if (error?.payload?.submission === "not_submitted") actionRequests.delete(key);
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
  } finally { actionInFlight.delete(key); }
}

async function showNodeResult(nodeId) {
  const threadId = viewState.selectedId;
  if (!threadId || !nodeId) return;
  const requestId = ++viewState.resultRequest;
  const dialog = $("#superAgentResultDialog");
  const host = $("#superAgentResultBody");
  host.textContent = "正在读取本次执行结果…";
  if (!dialog.open) dialog.showModal();
  try {
    const payload = await api(`/api/super-agent/threads/${encodeURIComponent(threadId)}/results?node_id=${encodeURIComponent(nodeId)}`);
    if (requestId !== viewState.resultRequest) return;
    host.innerHTML = (payload.results || []).map((result) => `<article class="super-agent-result"><h3>第 ${Number(result.attempt || 0) + 1} 次执行 · ${text(statusLabel(result.status))}</h3>${result.summary ? `<p>${text(result.summary)}</p>` : ""}<pre>${text(result.text, "本次执行尚无可展示正文。")}</pre>${result.compatibility ? `<p class="super-agent-muted">${text(RESULT_COMPATIBILITY_LABELS[result.compatibility], "执行记录需要进一步核对")}</p>` : ""}${result.output_refs?.length ? `<p>结果引用：${result.output_refs.map((ref) => refText(ref)).join("、")}</p>` : ""}</article>`).join("") || '<p>暂无可展示的执行结果。</p>';
  } catch (error) { if (requestId === viewState.resultRequest) host.textContent = `结果读取失败：${error.message}`; }
}

async function resolveToolConfirmation(button) {
  const threadId = viewState.selectedId;
  const confirmId = button.dataset.confirmId;
  const runId = button.dataset.runId;
  const key = `confirmation:${runId}:${confirmId}`;
  if (actionInFlight.has(key)) return;
  actionInFlight.add(key);
  const card = button.closest("[data-confirm-status]");
  card.querySelectorAll("button").forEach((item) => { item.disabled = true; });
  const status = card.querySelector("strong");
  status.textContent = "正在提交确认…";
  try {
    const response = await api(`/api/tool/${button.dataset.superToolDecision}`, { method: "POST", body: { run_id: runId, confirm_id: confirmId } });
    status.textContent = button.dataset.superToolDecision === "reject" ? "已拒绝，本次操作不会执行" : response.success ? "已确认，工具正在执行" : `确认未受理：${response.result || "请同步任务"}`;
    if (viewState.selectedId === threadId) await loadDetail(threadId);
  } catch (error) {
    status.textContent = `确认已失效或未受理：${error.message}。请同步任务查看当前状态。`;
    confirmationNotices.set(threadId, { confirmId, runId, message: status.textContent });
    if (viewState.selectedId === threadId) {
      const item = viewState.detail?.chat_confirmations?.find((row) => row.confirm_id === confirmId && row.run_id === runId);
      if (item) item.status = "expired";
      viewState.actionMessage = status.textContent; renderSyncStatus(); renderDetail();
    }
  } finally { actionInFlight.delete(key); }
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
  $("#superAgentResultClose")?.addEventListener("click", () => $("#superAgentResultDialog").close());
  $("#superAgentSupervision")?.addEventListener("click", (event) => { if (event.target.closest("[data-super-accept-completion]")) void runNodeAction("accept_completion"); });
  $("#superAgentChatConfirmationList")?.addEventListener("click", (event) => {
    if (event.target.closest("[data-super-dismiss-confirmation]")) { const notice = confirmationNotices.get(viewState.selectedId); if (notice) notice.dismissed = true; renderDetail(); return; }
    const button = event.target.closest("[data-super-tool-decision]"); if (button) void resolveToolConfirmation(button);
  });
  $("#superAgentAppNav")?.addEventListener("click", (event) => {
    const next = event.target.closest("[data-super-view]")?.dataset.superView;
    if (next === "chat") $("#openChatMode")?.click();
    else if (next === "studio") $("#openStudio")?.click();
  });
  $("#superAgentFilters")?.addEventListener("click", (event) => {
    const filter = event.target.closest("[data-super-filter]")?.dataset.superFilter;
    if (!["all", "waiting", "completed"].includes(filter)) return;
    viewState.filter = filter;
    const threads = filteredThreads();
    if (!threads.some((thread) => thread.thread_id === viewState.selectedId)) viewState.selectedId = threads[0]?.thread_id || "";
    renderThreadList();
    if (viewState.selectedId) void loadDetail(viewState.selectedId); else renderEmptyDetail();
  });
  $("#superAgentCreate")?.addEventListener("click", () => openCreateDomainDialog());
  $("#superAgentCreateForm")?.addEventListener("submit", (event) => {
    if (event.submitter?.value === "cancel") return;
    void createDomainThread(event);
  });
  $("#superAgentView")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-super-conversation]");
    if (!button) return;
    closeSuperAgentView();
    void openConversation(button.dataset.superConversation).catch((error) => { viewState.error = error.message || "打开会话失败"; renderSyncStatus(); void openSuperAgentView(); });
  });
  $("#superAgentCreateDomain")?.addEventListener("change", () => void loadCreateResources());
  $("#superAgentCreateMode")?.addEventListener("change", renderCreateMode);
  $("#superAgentCreateExecutionMode")?.addEventListener("change", renderCreateMode);
  $("#superAgentCreateStage")?.addEventListener("change", renderCreateMode);
  $("#superAgentCreateParts")?.addEventListener("input", updateCreateParts);
  $("#superAgentCreateAddStep")?.addEventListener("click", () => void createDomainThread(null, { addStep: true }));
  $("#superAgentCreateMixedSubmit")?.addEventListener("click", () => void submitMixedDraft());
  $("#superAgentCreateSteps")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-super-remove-step]");
    if (!button || createModelState.submitting) return;
    const removed = mixedDraft.splice(Number(button.dataset.superRemoveStep), 1)[0];
    if (removed) plannedChapterDependencies.delete(removed.key);
    renderMixedDraft();
  });
  $("#superAgentCreateBookId")?.addEventListener("change", () => void loadCreateBook());
  $("#superAgentCreateBooksRefresh")?.addEventListener("click", () => void loadCreateResources());
  $("#superAgentCreateWorkbench")?.addEventListener("click", () => {
    $("#superAgentCreateDialog")?.close();
    void returnToDomain({ dataset: { superReturnDomain: $("#superAgentCreateDomain").value, superBookId: $("#superAgentCreateBookId").value } });
  });
  $("#superAgentCreateConversation")?.addEventListener("change", renderCreateJobs);
  $("#superAgentCreateForm")?.addEventListener("input", renderCreateRangeHint);
  $("#superAgentThreadActions")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-super-thread-action]");
    if (button) void runNodeAction(button.dataset.superThreadAction);
  });
  $("#superAgentCreateProvider")?.addEventListener("change", (event) => {
    clearCreateError();
    void loadCreateModels(String(event.target.value || ""));
  });
  $("#superAgentCreateModel")?.addEventListener("change", () => { clearCreateError(); renderCreateModelSummary(); });
  $("#superAgentCreateModelRefresh")?.addEventListener("click", () => {
    void loadCreateModels(String($("#superAgentCreateProvider")?.value || ""), "", { force: true });
  });
  $("#superAgentCreateForm")?.addEventListener("input", clearCreateError);
  $("#openTaskManager")?.addEventListener("click", () => { $("#tasksDialog")?.close(); void openSuperAgentView(); });
  $("#superAgentRefresh")?.addEventListener("click", () => void openSuperAgentView());
  $("#superAgentThreadList")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-super-thread]"); if (!button) return;
    const threadId = button.dataset.superThread || "";
    if (threadId !== viewState.selectedId) { viewState.detail = null; viewState.nodes = []; renderEmptyDetail(); }
    viewState.selectedId = threadId; renderThreadList(); void loadDetail(threadId);
  });
  $("#superAgentNodeList")?.addEventListener("click", (event) => {
    const result = event.target.closest("[data-super-result]");
    if (result) void showNodeResult(result.dataset.superResult);
    const action = event.target.closest("[data-super-action]");
    if (action) void runNodeAction(action.dataset.superAction || "status", action.dataset.superNode || "");
    const back = event.target.closest("[data-super-return-domain]");
    if (back) void returnToDomain(back);
  });
  $("#superAgentApprovalList")?.addEventListener("click", (event) => { const button = event.target.closest("[data-super-approval]"); if (!button) return; void resolveApproval(button.dataset.superApproval || "", button.dataset.superApprovalId || ""); });
}
