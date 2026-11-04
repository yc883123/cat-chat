// ============================================================
// 28-super-agent.js —— 任务总管控制面
// ============================================================

import { $, api, copyText, escapeHtml, localFileUrl, setAppView, state } from "./01-core.js";
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
    try { if (!viewState.loading && !createModelState.submitting && !recipeState.busy && document.visibilityState !== "hidden") await loadThreads(); }
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

function statusLabel(value) { return STATUS_LABELS[String(value || "")] || ({ reverse_running: "正在生成提示词", generation_running: "正在生成视频", review: "待确认生成视频", needs_review: "需要人工核对", done: "已完成", provided: "提示词已就绪", queued: "排队中", authorized: "已确认，等待执行", partial: "部分未完成", collection_failed: "结果下载失败", collect_failed: "结果下载失败", download_failed: "结果下载失败", invalid: "提示词格式不合格" })[value] || String(value || "未知"); }
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
    const recipeThread = Boolean(viewState.detail?.recipe_batch_id || viewState.nodes.some((node) => node.executor_type === "recipe_batch"));
    if (recipeThread) {
      const response = await api(`/api/super-agent/threads/${encoded}/recipe`);
      if (viewState.selectedId !== threadId) return;
      recipeState.batches.set(threadId, response.batch);
    }
    viewState.error = ""; viewState.actionMessage = ""; viewState.syncedAt = Date.now(); renderDetail();
    renderRecipeBatch(recipeThread ? recipeState.batches.get(threadId) : null);
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
  $("#superAgentApprovalList").innerHTML = "";
  $("#superAgentArtifactList").innerHTML = '<p class="super-agent-muted">暂无产物。</p>';
  if ($("#superAgentNodeList")) $("#superAgentNodeList").innerHTML = '<p class="super-agent-muted">暂无步骤。</p>';
  $("#superAgentChatConfirmationList").innerHTML = "";
  $("#superAgentNextStep").innerHTML = '<p class="super-agent-muted">选择任务后查看下一步。</p>';
  $("#superAgentConversationLinks").innerHTML = "";
  $("#superAgentSupervision").hidden = true;
  if ($("#superAgentRecipeBatch")) $("#superAgentRecipeBatch").hidden = true;
  const focus = $("#superAgentFocus"); if (focus) focus.hidden = true;
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
  $("#superAgentProgressHint").textContent = "仅统计已完成的真实步骤";
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

function artifactKind(item) {
  const kind = String(item?.kind || "").toLowerCase();
  if (["image", "img", "png", "jpg", "jpeg", "webp", "gif"].includes(kind)) return "image";
  if (["video", "mp4", "mov", "webm"].includes(kind)) return "video";
  if (["audio", "mp3", "wav", "flac"].includes(kind)) return "audio";
  return mediaKindFromName(item?.path_or_ref || item?.title || "");
}
function artifactBaseName(ref) {
  const raw = String(ref || "").replaceAll("\\", "/");
  if (raw.includes("://")) return raw;
  return raw.split("/").filter(Boolean).pop() || "";
}
// 单一主操作卡：按当前状态给出唯一的「现在需要你做的事」，不再多处重复按钮与文案。
function renderFocus(detail, nodes, currentNodeId) {
  const focus = $("#superAgentFocus"); if (!focus) return;
  const label = $("#superAgentFocusLabel"), title = $("#superAgentFocusTitle"), body = $("#superAgentFocusText"), note = $("#superAgentFocusNote");
  focus.hidden = false;
  focus.classList.remove("is-running", "is-done", "is-failed");
  const focusActions = $("#superAgentFocusActions"); if (focusActions) focusActions.innerHTML = "";
  note.hidden = true; note.textContent = "";
  const status = detail.status;
  const pendingCount = (Array.isArray(detail.chat_confirmations) ? detail.chat_confirmations : []).filter((item) => item.status === "pending").length
    + viewState.approvals.filter((item) => item.status === "pending").length;
  const current = nodes.find((node) => node.node_id === currentNodeId);
  if (pendingCount) {
    label.textContent = "现在需要你确认";
    title.textContent = `有 ${pendingCount} 项操作等待你确认`;
    body.textContent = "确认后任务才会继续推进；每一项都写明了会执行什么。";
  } else if (["completed", "archived"].includes(status)) {
    focus.classList.add("is-done");
    label.textContent = "任务已完成";
    title.textContent = detail.supervision ? "目标完成" : "全部步骤已完成";
    body.textContent = "结果保留在最近产物里；需要继续处理时，可以复制内容新建任务，不会改动已有结果。";
  } else if (status === "failed") {
    focus.classList.add("is-failed");
    label.textContent = "需要你处理";
    title.textContent = "本次执行未完成";
    body.textContent = "请查看失败原因与结果，再决定是否重试或重新创建。";
  } else if (status === "paused") {
    label.textContent = "已暂停";
    title.textContent = "任务已暂停";
    body.textContent = "继续后才会执行后续步骤；正在执行的内容仍可能完成。";
  } else if (status === "cancelled") {
    focus.classList.add("is-done");
    label.textContent = "任务已取消";
    title.textContent = "任务已取消";
    body.textContent = "已产出的结果会保留；需要同类任务可重新创建。";
  } else if (status === "stopping") {
    label.textContent = "正在停止";
    title.textContent = "正在停止后续步骤";
    body.textContent = "停止完成前请稍候；在途执行仍可能完成。";
  } else {
    focus.classList.add("is-running");
    label.textContent = "正在执行";
    title.textContent = current ? nodeDisplayName(current) : "任务进行中";
    body.textContent = current ? "这一步不需要你操作，完成后这里会更新。" : "按任务设置推进；需要你确认时会在这里提示。";
  }
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
  const stopping = detail.status === "stopping";
  const threadActions = $("#superAgentThreadActions");
  if (threadActions) {
    // 每个状态只保留当前有效的操作；结束/停止中不再渲染一排禁用按钮（A11/A13）。
    threadActions.hidden = terminal || stopping;
    threadActions.querySelectorAll("[data-super-thread-action]").forEach((button) => {
      const action = button.dataset.superThreadAction;
      button.disabled = action === "pause" ? !["active", "waiting_user"].includes(detail.status)
        : action === "continue" ? !(detail.status === "paused" || detail.status === "needs_recovery" || detail.supervision?.phase === "paused")
        : stopping;
      if (detail.supervision && action === "pause") button.textContent = "暂停自动续办";
      if (detail.supervision && action === "continue") button.textContent = "继续自动续办";
      if (!detail.supervision && action === "pause") button.textContent = "暂停任务";
      if (!detail.supervision && action === "continue") button.textContent = "继续任务";
    });
  }
  const controlNote = $("#superAgentControlNote");
  if (controlNote) controlNote.hidden = terminal || stopping;
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
  renderFocus(detail, nodes, currentNodeId);
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
    <article class="super-agent-ref"><strong>${text(item.action, "待确认操作")}</strong><span>${text(statusLabel(item.status))}</span><p>步骤：${text(nodeDisplayName(nodeById.get(String(item.node_id))))}</p><p>${text(item.description)}</p>${item.status === 'pending' ? `<div class="super-agent-node-actions"><button type="button" class="control-button tiny" data-super-approval="approved" data-super-approval-id="${text(item.approval_id)}">批准</button><button type="button" class="control-button tiny" data-super-approval="rejected" data-super-approval-id="${text(item.approval_id)}">拒绝</button></div>` : ""}</article>`).join("") : "";
  $("#superAgentArtifactList").innerHTML = viewState.artifacts.length ? viewState.artifacts.map((item) => {
    const kind = artifactKind(item);
    const ref = String(item.path_or_ref || "");
    const openable = ref && !ref.includes("://");
    return `<article class="super-agent-artifact"><span class="recipe-thumb kind-${kind}" aria-hidden="true">${({ image: "图", video: "影", audio: "音" })[kind] || "文"}</span><span class="artifact-copy"><b>${text(item.title, "任务产物")}</b><small>${text(item.summary || artifactBaseName(ref))}</small></span>${openable ? `<a class="control-button tiny" href="${text(localFileUrl(ref))}" target="_blank" rel="noopener">打开</a>` : ""}</article>`;
  }).join("") : '<p class="super-agent-muted">暂无产物。</p>';
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
  // 链的正文步骤按拆章范围写全部章节（方案A）：不选单章，只展示范围提示。
  $("#superAgentCreateChapterWrap").hidden = chain || stage !== "chapter";
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
    const target = chain
      ? `正文将写完拆章范围内全部 ${count} 章（每章一次模型调用，逐章落盘，可中途取消）。`
      : stage === "chapter" ? `正文仅生成第 ${chapter} 章。` : "";
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
    const chapterNeeded = !chain && stage === "chapter";
    if ((chain || stage === "outline_all") && (!createState.parts.length || createState.parts.length > 20 || createState.parts.some((row) => !String(row.title || "").trim() || row.title.length > 80))) return createError("请填写 1–20 部标题，每部标题不超过 80 字。");
    if (splitNeeded && (!Number.isInteger(from) || from < 1 || !Number.isInteger(count) || count < 1 || count > 999 || !partNo)) return createError("请选择拆章所属部，起始章号为正整数，拆章数量须为 1–999。");
    if (chapterNeeded && (!Number.isInteger(chapter) || chapter < 1)) return createError("正文目标章须为正整数。");
    if (!chain && stage === "chapter" && !createState.chapters.some((row) => chapterNumber(row) === chapter) && !(addStep && draftProvidesChapter(bookId, chapter))) return createError("正文目标章不在作品中，请先到工作台拆章，或先添加同作品且覆盖目标章的拆章步骤。");
    const requests = {
      setup: { extra_note: goal },
      outline: { part_plan: createState.parts.map((row, index) => ({ no: Number(row.no || index + 1), title: row.title })), extra_note: goal },
      chapter_plan: { part_no: partNo, from_no: from, want_count: count, extra_note: goal },
      // 链的正文步骤写完拆章范围内全部章节（方案A，2026-11-04 拍板）：
      // 范围与拆章步同源，逐章落盘、每章一次模型调用、可中途取消。
      chapter_content: chain ? { from_no: from, want_count: count, extra_note: goal } : { chapter_no: chapter, extra_note: goal },
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
  if (!nodeId && recipeState.batches.has(viewState.selectedId)) return recipeAction(action);
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

function resultRefCard(ref) {
  const value = typeof ref === "object" ? String(ref.path_or_ref || ref.ref || ref.artifact_id || ref.title || "") : String(ref || "");
  const kind = mediaKindFromName(value);
  const openable = value && !value.includes("://");
  return `<div class="super-agent-artifact"><span class="recipe-thumb kind-${kind}" aria-hidden="true">${({ image: "图", video: "影", audio: "音" })[kind] || "文"}</span><span class="artifact-copy"><b>${text(artifactBaseName(value), "产物")}</b></span>${openable ? `<a class="control-button tiny" href="${text(localFileUrl(value))}" target="_blank" rel="noopener">打开</a>` : ""}</div>`;
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
    host.innerHTML = (payload.results || []).map((result) => `<article class="super-agent-result"><h3>第 ${Number(result.attempt || 0) + 1} 次执行 · ${text(statusLabel(result.status))}</h3>${result.summary ? `<p>${text(result.summary)}</p>` : ""}<div class="sa-result-text">${text(result.text, "本次执行尚无可展示正文。")}</div>${result.compatibility ? `<p class="super-agent-muted">${text(RESULT_COMPATIBILITY_LABELS[result.compatibility], "执行记录需要进一步核对")}</p>` : ""}${result.output_refs?.length ? `<div class="recipe-review-items">${result.output_refs.map((ref) => resultRefCard(ref)).join("")}</div>` : ""}</article>`).join("") || '<p>暂无可展示的执行结果。</p>';
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
  bindRecipeEvents();
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

// Bounded recipes use native domain tasks, never chat planning or guessed bindings.
const recipeState = {
  batches: new Map(), drafts: new Map(), busy: false, review: null, requests: new Map(),
  restored: false, intent: null, handoff: null,
  createWorkflow: null, createMapping: null, createInput: null, createDraft: null, precheckOk: false, addGroupMedia: {},
};
let recipeEditorBridge = {};
export function setRecipeEditorBridge(bridge) { recipeEditorBridge = bridge || {}; }
const RECIPE_DRAFT_KEY = "naibaChatRecipeProductionDraftV1";
function recipeMediaKind(entry) { return entry?.media || entry?.kind || mediaKindFromName(entry?.name || entry?.path || entry?.source); }
function rememberRecipeDraft() {
  // 创建草稿：保存「选了哪个工作流 + 首组内容 + 用户映射与正文来源」；换工作流后旧值不套用。
  const dialog = $("#superAgentRecipeDialog");
  if (!dialog || !dialog.open) return;
  const draft = { version: 2,
    workflowId: recipeState.createWorkflow?.id || recipeState.createDraft?.workflowId || "",
    workflowFingerprint: recipeState.createWorkflow?.workflow_fingerprint || recipeState.createDraft?.workflowFingerprint || "",
    title: $("#recipeTitle")?.value || recipeState.createDraft?.title || "",
    shots: $("#recipeShots")?.value || recipeState.createDraft?.shots || "",
    prompt: $("#recipeCreatePrompt")?.value || recipeState.createDraft?.prompt || "" };
  if (recipeState.createMapping) draft.mapping = recipeState.createMapping;
  if (recipeState.createInput) { draft.inputMode = recipeState.createInput.inputMode; draft.templateId = recipeState.createInput.templateId; }
  if (draft.workflowId) recipeState.createDraft = draft;
  try { localStorage.setItem(RECIPE_DRAFT_KEY, JSON.stringify(draft)); } catch { /* quota/private mode: live draft remains */ }
}
function restoreRecipeDraft() {
  if (recipeState.restored) return;
  recipeState.restored = true;
  try {
    const draft = JSON.parse(localStorage.getItem(RECIPE_DRAFT_KEY) || "null");
    if (draft?.version !== 2 || !draft.workflowId) return;
    recipeState.createDraft = { workflowId: String(draft.workflowId), workflowFingerprint: String(draft.workflowFingerprint || ""),
      title: String(draft.title || ""), shots: draft.shots || "", prompt: String(draft.prompt || ""),
      ...(draft.mapping && typeof draft.mapping === "object" ? { mapping: draft.mapping } : {}),
      ...(draft.inputMode ? { inputMode: String(draft.inputMode) } : {}),
      ...(draft.templateId ? { templateId: String(draft.templateId) } : {}) };
  } catch { /* malformed/old draft: start safely */ }
}
function recipeDurationSupported(recipe) { return Boolean(recipe?.duration_slot); }
function itemFieldEditable(batch, item, field) {
  if (item.edit_capabilities && Object.hasOwn(item.edit_capabilities, field)) return item.edit_capabilities[field] === true;
  const generated = generationStatus(item);
  const attempted = (item.generate?.attempts || []).length > 0;
  if (!recipeCan(batch, "edit")) return false;
  return field === "included" || (generated === "pending" && !attempted);
}
const RECIPE_PHASES = { draft: "待生成提示词", reverse_running: "正在生成提示词", review: "待确认生成视频", generation_running: "正在生成视频", completed: "全部完成", partial: "部分未完成", paused: "已暂停", cancelled: "已取消", interrupted: "执行已中断", needs_review: "需要人工核对" };
function mediaKindFromName(name) {
  const ext = String(name || "").split(".").pop().toLowerCase();
  if (["mp4", "mov", "webm", "mkv", "avi"].includes(ext)) return "video";
  if (["png", "jpg", "jpeg", "webp", "gif", "bmp", "avif"].includes(ext)) return "image";
  if (["mp3", "wav", "flac", "m4a", "ogg", "aac", "opus"].includes(ext)) return "audio";
  return "doc";
}
function recipeMediaSummary(media) {
  return ["image", "video", "audio"].map((kind) => { const count = (media || []).filter((entry) => recipeMediaKind(entry) === kind).length; return count ? `${count} ${({ image: "张图", video: "段视频", audio: "段音频" })[kind]}` : ""; }).filter(Boolean).join("、") || "无参考素材";
}
function recipeBaseName(ref) {
  return String(ref || "").replaceAll("\\", "/").split("/").filter(Boolean).pop() || "产物";
}
function itemDisplayName(item, index = 0) {
  const raw = String(item?.name || item?.source_path || "").replaceAll("\\", "/");
  const base = raw.split("/").filter(Boolean).pop() || "";
  return /^(group|item)-\d+$/i.test(base) ? "" : base;
}
function recipeCan(batch, action) { return Array.isArray(batch.capabilities) ? batch.capabilities.includes(action) : batch.capabilities?.[action] === true; }
function reverseStatus(item) { return item.reverse_status || item.reverse?.status || "pending"; }
function generationStatus(item) { return item.generation_status || item.generate?.status || "pending"; }
function recipeDraft(batch) {
  if (!recipeState.drafts.has(batch.thread_id)) recipeState.drafts.set(batch.thread_id, { revision: batch.revision, items: {}, dirty: false });
  return recipeState.drafts.get(batch.thread_id);
}
function recipeError(message) { $("#recipeCreateError").textContent = message || ""; }
// 素材缩略图：本地上传图片直接预览；不再显示受管绝对路径。
function recipeMediaMarkup(media) {
  const counts = { image: 0, video: 0, audio: 0 };
  return `<div class="recipe-thumb-strip">${(media || []).map((entry) => {
    const kind = recipeMediaKind(entry), number = ++counts[kind];
    const reference = entry.reference || `<${({ image: "Picture", video: "Video", audio: "Audio" })[kind] || "Media"} ${number}>`;
    const url = text(localFileUrl(entry.path));
    const preview = !entry.path ? "" : kind === "image" ? `<img class="recipe-thumb" src="${url}" loading="lazy" alt="${text(entry.name)}">` : kind === "video" ? `<video class="recipe-media-preview" src="${url}" controls preload="metadata"></video>` : `<audio class="recipe-media-preview" src="${url}" controls preload="metadata"></audio>`;
    return `<figure class="recipe-media-ref">${preview}<figcaption>${text(reference)} · ${text(entry.name || recipeBaseName(entry.path))}${entry.description ? `<small>${text(entry.description)}</small>` : ""}</figcaption></figure>`;
  }).join("")}</div>`;
}
// 产物：后端补 output.type 之前用可辨占位 + 打开/保存链接；不渲染不可用的播放按钮。
function recipeOutputMarkup(output) {
  if (!output?.path) return `<p class="super-agent-error">产物缺少受管路径：${text(output?.name)}</p>`;
  const url = text(localFileUrl(output.path));
  const displayName = recipeBaseName(output.name || output.path);
  const kind = String(output.type || "") || mediaKindFromName(output.name || output.path);
  const mark = ({ image: "图", video: "影", audio: "音" })[kind] || "文";
  const kindClass = kind === "image" ? "kind-image" : kind === "doc" ? "kind-doc" : "kind-video";
  return `<div class="recipe-output"><span class="recipe-thumb ${kindClass}" aria-hidden="true">${mark}</span><a href="${url}" target="_blank" rel="noopener">打开 / 保存 ${text(displayName)}</a></div>`;
}
function recipeRequest(key, body) {
  const fingerprint = JSON.stringify(body);
  const old = recipeState.requests.get(key);
  if (old?.fingerprint === fingerprint) return old.id;
  const id = newRequestId(); recipeState.requests.set(key, { fingerprint, id }); return id;
}
// ---- 新建批量任务：直接选造梦机已保存工作流（2026-11-03 计划 §4.1 入口②）----
// 映射解释权在用户：总管不猜不查。文本写入位候选 = 全部文本槽（正向锚点 /
// 多行框排前面只是排序建议，不是规则）；选哪个由用户定，也可以不注入正文。
function recipePromptSlotCandidates(slots) {
  const all = (slots || []).filter((slot) => slot.field_type === "text");
  const rank = (slot) => (slot.role === "positive_prompt" ? 0 : slot.control === "multiline" ? 1 : 2);
  return [...all].sort((a, b) => rank(a) - rank(b));
}
function recipeDurationSlotCandidate(slots) {
  const hits = (slots || []).filter((slot) => slot.field_type === "number"
    && /时长|duration/i.test(`${slot.node_title || ""} ${slot.label || ""}`));
  return hits.length === 1 ? { node_id: String(hits[0].node_id), field_name: String(hits[0].field_name) } : null;
}
function recipeSlotKey(slot) { return JSON.stringify({ node_id: String(slot.node_id), field_name: String(slot.field_name) }); }
function recipeSlotValue(workflow, slot) {
  if (!slot) return "";
  return String((((workflow?.prompt || {})[slot.node_id] || {}).inputs || {})[slot.field_name] ?? "");
}
// ---- 用户映射（方案）：文本写入位 + 素材入口 + 时长槽，按工作流指纹记住 ----
function recipeMappingMemoryKey(fingerprint) { return `saHandoffMapping:${fingerprint}`; }
function rememberRecipeMapping(fingerprint, mapping) {
  if (!fingerprint || !mapping) return;
  try { localStorage.setItem(recipeMappingMemoryKey(fingerprint), JSON.stringify(mapping)); } catch { /* 隐私模式忽略 */ }
}
function recalledRecipeMapping(fingerprint) {
  try {
    const raw = localStorage.getItem(recipeMappingMemoryKey(fingerprint));
    const parsed = raw ? JSON.parse(raw) : null;
    if (parsed && typeof parsed === "object") {
      return { prompt_slot: parsed.prompt_slot || null, media_slots: Array.isArray(parsed.media_slots) ? parsed.media_slots : [], duration_slot: parsed.duration_slot || null };
    }
  } catch { /* 坏记忆当没有 */ }
  return null;
}
function recipeMappingFromWorkflow(workflow) {
  // 没有记忆时的初稿：唯一文本槽 / 唯一时长槽预选只是省一次点击，用户可改可清。
  const mapping = { prompt_slot: null, media_slots: [], duration_slot: null };
  const slots = workflow?.params || [];
  const positive = slots.filter((slot) => slot.role === "positive_prompt");
  const multiline = slots.filter((slot) => slot.field_type === "text" && slot.control === "multiline");
  const preselect = positive.length === 1 ? positive[0] : multiline.length === 1 ? multiline[0] : null;
  if (preselect) mapping.prompt_slot = { node_id: String(preselect.node_id), field_name: String(preselect.field_name) };
  const duration = recipeDurationSlotCandidate(slots);
  if (duration) mapping.duration_slot = duration;
  return mapping;
}
function recipeMappingAddresses(mapping) {
  return [mapping?.prompt_slot, mapping?.duration_slot, ...(mapping?.media_slots || [])]
    .filter(Boolean).map((slot) => `${slot.node_id}|${slot.field_name}`);
}
// 造梦机已选素材 = 用户已确认的映射：自动收编成素材入口，缺类型的按加载节点事实补。
function adoptWorkflowMediaIntoMapping(mapping, workflow, mediaInputs) {
  const known = new Set((mapping.media_slots || []).map((slot) => `${slot.node_id}|${slot.field_name}`));
  mapping.media_slots = mapping.media_slots || [];
  for (const entry of mediaInputs || []) {
    const key = `${entry.node_id}|${entry.field_name}`;
    if (known.has(key)) continue;
    const param = (workflow?.params || []).find((p) => String(p.node_id) === String(entry.node_id) && p.field_name === entry.field_name);
    const kind = ["image", "video", "audio"].includes(param?.field_type) ? param.field_type : "image";
    mapping.media_slots.push({ node_id: String(entry.node_id), field_name: String(entry.field_name), media: kind, label: String(param?.label || `${entry.node_id} · ${entry.field_name}`) });
    known.add(key);
  }
}
const RECIPE_MEDIA_KIND_LABELS = { image: "图片", video: "视频", audio: "音频" };
const RECIPE_TEMPLATE_PRESETS = [
  { id: "recipe_minimax_h3_r2v_v2", label: "MiniMax H3 · 多媒体参考生视频" },
  { id: "recipe_minimax_h3_i2v_v1", label: "MiniMax H3 · 首帧生视频" },
];
function recipeTemplateLabel(id) {
  return RECIPE_TEMPLATE_PRESETS.find((row) => row.id === id)?.label
    || (id ? "提词库自定义模板" : "未选择");
}
// 参数映射编辑区（交接/直建共用）：文本位、时长槽、素材入口全部用户指定。
function recipeMappingEditorMarkup(prefix, workflow, mapping) {
  const slots = workflow?.params || [];
  const textSlots = recipePromptSlotCandidates(slots);
  const numberSlots = slots.filter((slot) => slot.field_type === "number" || slot.field_type === "seed");
  const mediaCandidates = slots.filter((slot) => ["text", "image", "video", "audio"].includes(slot.field_type));
  const promptKey = mapping.prompt_slot ? recipeSlotKey(mapping.prompt_slot) : "";
  const durationKey = mapping.duration_slot ? recipeSlotKey(mapping.duration_slot) : "";
  const mappedKeys = new Set((mapping.media_slots || []).map((slot) => `${slot.node_id}|${slot.field_name}`));
  const slotLabel = (slot) => slot.label || `${slot.node_id} · ${slot.field_name}`;
  const addCandidates = mediaCandidates.filter((slot) => {
    const key = `${slot.node_id}|${slot.field_name}`;
    return !mappedKeys.has(key) && key !== promptKey && key !== durationKey;
  });
  const option = (slot, selectedKey) => `<option value="${escapeHtml(recipeSlotKey(slot))}"${recipeSlotKey(slot) === selectedKey ? " selected" : ""}>${escapeHtml(slotLabel(slot))}</option>`;
  const mediaRows = (mapping.media_slots || []).map((slot, index) => `<li data-media-index="${index}"><code>${escapeHtml(slotLabel(slot))}</code><select data-map-role="mediaKind" aria-label="素材类型">${["image", "video", "audio"].map((kind) => `<option value="${kind}"${(slot.media || "image") === kind ? " selected" : ""}>${RECIPE_MEDIA_KIND_LABELS[kind]}</option>`).join("")}</select><button type="button" class="control-button tiny" data-map-role="mediaRemove">移除</button></li>`).join("");
  return `<div class="recipe-mapping">
    <label>文本写入位（每组正文写进哪个参数）<select data-map-role="prompt"><option value="">不注入正文（沿用工作流里的原值）</option>${textSlots.map((slot) => option(slot, promptKey)).join("")}</select></label>
    <label>时长槽（批次时长写进哪个参数）<select data-map-role="duration"><option value="">不映射时长（按工作流原配置出片）</option>${numberSlots.map((slot) => option(slot, durationKey)).join("")}</select></label>
    <div class="recipe-mapping-media">
      <span class="recipe-mapping-media-title">素材入口（可选，不必填满；文件在批次里逐组放）</span>
      ${mediaRows ? `<ul class="recipe-mapping-media-list">${mediaRows}</ul>` : '<p class="writer-dialog-hint">还没映射素材入口；不需要素材的工作流可以不加。</p>'}
      <div class="recipe-mapping-media-add">
        <select data-map-role="mediaAddField" aria-label="选择参数">${addCandidates.map((slot) => option(slot, "")).join("")}</select>
        <select data-map-role="mediaAddKind" aria-label="素材类型">${["image", "video", "audio"].map((kind) => `<option value="${kind}">${RECIPE_MEDIA_KIND_LABELS[kind]}</option>`).join("")}</select>
        <button type="button" class="control-button tiny" data-map-role="mediaAdd"${addCandidates.length ? "" : " disabled"}>添加素材入口</button>
      </div>
    </div>
  </div>`;
}
function renderRecipeMappingEditor(prefix, workflow, mapping) {
  const host = $(`#${prefix}Mapping`);
  if (host) host.innerHTML = recipeMappingEditorMarkup(prefix, workflow, mapping || {});
}
function bindRecipeMappingEditor(prefix, getMapping, onChange) {
  const host = $(`#${prefix}Mapping`);
  if (!host || host.dataset.bound) return;
  host.dataset.bound = "1";
  const workflow = () => recipeState.handoff?.intent?.workflow || recipeState.createWorkflow;
  host.addEventListener("change", (event) => {
    const mapping = getMapping(); if (!mapping) return;
    const role = event.target.closest("[data-map-role]")?.dataset.mapRole;
    if (role === "prompt" || role === "duration") {
      const key = event.target.value || "";
      mapping[role === "prompt" ? "prompt_slot" : "duration_slot"] = key ? JSON.parse(key) : null;
      onChange();
    } else if (role === "mediaKind") {
      const index = Number(event.target.closest("li")?.dataset.mediaIndex);
      if (Number.isInteger(index) && mapping.media_slots[index]) { mapping.media_slots[index].media = event.target.value; onChange(); }
    }
  });
  host.addEventListener("click", (event) => {
    const mapping = getMapping(); if (!mapping) return;
    const remove = event.target.closest("[data-map-role='mediaRemove']");
    if (remove) {
      const index = Number(remove.closest("li")?.dataset.mediaIndex);
      if (Number.isInteger(index)) { mapping.media_slots.splice(index, 1); onChange(); }
      return;
    }
    if (event.target.closest("[data-map-role='mediaAdd']")) {
      const fieldKey = host.querySelector("[data-map-role='mediaAddField']")?.value || "";
      const kind = host.querySelector("[data-map-role='mediaAddKind']")?.value || "image";
      if (!fieldKey) return;
      const slot = JSON.parse(fieldKey);
      const param = (workflow()?.params || []).find((p) => String(p.node_id) === String(slot.node_id) && p.field_name === slot.field_name);
      mapping.media_slots.push({ node_id: String(slot.node_id), field_name: String(slot.field_name), media: kind, label: String(param?.label || `${slot.node_id} · ${slot.field_name}`) });
      onChange();
    }
  });
}
// 正文来源（直接 / 反推）+ 反推模板（H3 两种预设 + 提词库自定义），交接/直建共用。
function recipeInputModeMarkup() {
  return `<div class="sa-sec-head"><h3>每组正文怎么来</h3><span>直接用现成文本，或先反推</span></div>
    <div class="super-agent-mode-cards" role="radiogroup" aria-label="正文来源">
      <button type="button" class="super-agent-mode-card active" data-input-mode="prompt" role="radio" aria-checked="true"><span class="super-agent-mode-radio" aria-hidden="true"></span><span class="super-agent-mode-top">直接用现成文本</span><p>填好的正文就是最终提示词，不反推；哪组想反推进批次后单独改。</p></button>
      <button type="button" class="super-agent-mode-card" data-input-mode="script" role="radio" aria-checked="false"><span class="super-agent-mode-radio" aria-hidden="true"></span><span class="super-agent-mode-top">先反推生成提示词</span><p>只填每组的内容要求，确认后按模板反推；填了正文的组仍直接用原文。</p></button>
    </div>`;
}
function renderRecipeInputMode(prefix, input) {
  const host = $(`#${prefix}InputMode`); if (!host) return;
  host.querySelectorAll("[data-input-mode]").forEach((card) => {
    const active = card.dataset.inputMode === input.inputMode;
    card.classList.toggle("active", active);
    card.setAttribute("aria-checked", String(active));
  });
  const row = $(`#${prefix}TemplateRow`);
  if (row) row.hidden = input.inputMode !== "script";
  if (input.inputMode === "script") void fillRecipeCustomTemplates(`#${prefix}Template`);
}
async function fillRecipeCustomTemplates(selector) {
  const select = $(selector); if (!select) return;
  if (!recipeState.customTemplates) {
    recipeState.customTemplates = [];
    try {
      const { items } = await api("/api/studio/custom");
      recipeState.customTemplates = (items || []).filter((item) => item?.id).map((item) => ({ id: String(item.id), title: String(item.title || item.id) }));
    } catch { /* 读取失败仍可用 H3 预设 */ }
  }
  try {
    const group = select.querySelector("optgroup[data-custom]");
    if (group && !group.children.length) group.replaceChildren(...recipeState.customTemplates.map((row) => new Option(row.title, row.id)));
  } catch { /* 桩环境无 Option/DOM，忽略 */ }
}
async function openRecipeDialog() {
  restoreRecipeDraft();
  recipeError("");
  const dialog = $("#superAgentRecipeDialog"); if (!dialog.open) dialog.showModal();
  try {
    const result = await api("/api/comfyui/workflows");
    const workflows = result.workflows || [];
    const select = $("#recipeWorkflowSelect");
    select.replaceChildren(new Option("请选择造梦机已保存的工作流", ""), ...workflows.map((row) => new Option(row.name || row.id, row.id)));
    const wanted = recipeState.createDraft?.workflowId;
    if (wanted && workflows.some((row) => row.id === wanted)) select.value = wanted;
    await loadCreateWorkflowDetail();
  } catch (error) { recipeError(`工作流读取失败：${error.message}`); }
}
async function loadCreateWorkflowDetail() {
  const id = $("#recipeWorkflowSelect").value;
  const hint = $("#recipeWorkflowHint");
  recipeState.createWorkflow = null;
  recipeState.createMapping = { prompt_slot: null, media_slots: [], duration_slot: null };
  $("#recipeDurationField").hidden = true;
  if (!id) { hint.textContent = "选工作流后自动带入它的参数与保存值；不要求先建方案。"; return; }
  hint.textContent = "正在读取工作流参数…";
  try {
    const result = await api(`/api/comfyui/workflows/${encodeURIComponent(id)}`);
    const workflow = result.workflow;
    recipeState.createWorkflow = workflow;
    const fingerprint = workflow.workflow_fingerprint || workflow.id || "";
    const draft = recipeState.createDraft;
    // 用户映射：优先恢复本次草稿，其次记住的方案，最后给一份可改的初稿。
    const mapping = draft?.workflowId === id && draft.mapping ? draft.mapping : (recalledRecipeMapping(fingerprint) || recipeMappingFromWorkflow(workflow));
    adoptWorkflowMediaIntoMapping(mapping, workflow, []);
    recipeState.createMapping = mapping;
    recipeState.createInput = { inputMode: draft?.workflowId === id && draft.inputMode === "script" ? "script" : "prompt",
      templateId: draft?.templateId || RECIPE_TEMPLATE_PRESETS[0].id };
    renderRecipeMappingEditor("recipeCreate", workflow, mapping);
    renderRecipeInputMode("recipeCreate", recipeState.createInput);
    const promptField = $("#recipeCreatePrompt");
    if (document.activeElement !== promptField) promptField.value = draft?.workflowId === id && draft.prompt ? draft.prompt : recipeSlotValue(workflow, mapping.prompt_slot);
    const duration = mapping.duration_slot;
    $("#recipeDurationField").hidden = !duration;
    if (duration) {
      const value = Number(recipeSlotValue(workflow, duration));
      if (Number.isFinite(value) && value > 0) $("#recipeDuration").value = String(Math.round(value));
    }
    if (draft?.workflowId === id && draft.title) $("#recipeTitle").value = draft.title;
    if (draft?.workflowId === id && draft.shots) $("#recipeShots").value = String(draft.shots);
    hint.textContent = `已带入「${workflow.name || id}」的保存值；文本写入位、素材入口、时长槽在参数映射里指定，这次的选择会记住，下次自动带入。`;
    rememberRecipeDraft();
  } catch (error) { hint.textContent = `工作流参数读取失败：${error.message}`; }
}
// ---- 高级参数：一份模型同时供展示和提交 ----
// 原图值来自冻结的 recipe.workflow，方案值来自 recipe.overrides，本批次值来自控件。
// 只收数值 / 布尔 / seed；提示词、素材和时长由方案自己管理，不在这里重复。
function recipeBoundAddresses(recipe) {
  return new Set([recipe?.prompt_slot, recipe?.duration_slot, ...(recipe?.media_slots || [])]
    .filter(Boolean).map((slot) => `${slot.node_id}|${slot.field_name}`));
}
// 草稿按「方案 ID + 版本 + 工作流指纹」隔离：换方案、重新读取或另存副本后，
// 旧地址不会套到新方案的同名字段上。
function recipeParamIdentity(recipe) {
  const fingerprint = String(recipe?.workflow_fingerprint || "");
  return `${recipe?.id || ""}@${recipe?.version || 1}${fingerprint ? `#${fingerprint.slice(0, 12)}` : ""}`;
}
function recipeOverrideKey(recipe, nodeId, field) {
  return `override:${recipeParamIdentity(recipe)}::${nodeId}|${field}`;
}
function recipeParameterKind(field, value) {
  if (typeof value === "boolean") return "bool";
  if (typeof value === "number" && Number.isFinite(value)) return field === "seed" || field === "noise_seed" ? "seed" : "number";
  return "";
}
function recipeParameterRows(recipe) {
  if (!recipe || typeof recipe.workflow !== "object" || !recipe.workflow) return [];
  const bound = recipeBoundAddresses(recipe);
  const recipeValues = new Map((recipe.overrides || []).map((entry) => [`${entry.node_id}|${entry.field_name}`, entry.value]));
  const rows = [];
  for (const [nodeId, node] of Object.entries(recipe.workflow)) {
    if (!node || typeof node.inputs !== "object" || !node.inputs) continue;
    const title = String(node._meta?.title || node.class_type || "");
    for (const [field, value] of Object.entries(node.inputs)) {
      if (Array.isArray(value)) continue;
      const address = `${nodeId}|${field}`;
      if (bound.has(address)) continue;
      const kind = recipeParameterKind(field, value);
      if (!kind) continue;
      rows.push({
        address, node_id: String(nodeId), field_name: field, field_type: kind,
        original: value, recipe_value: recipeValues.has(address) ? recipeValues.get(address) : undefined,
        // 原图是整数就要求精确整数；seed 一律按整数处理。
        integer: kind === "seed" || (typeof value === "number" && Number.isInteger(value)),
        node_title: title,
        label: `${field}（节点 ${nodeId}${title ? ` · ${title}` : ""}）`,
      });
    }
  }
  return rows;
}
// 旧的调用点只关心「有哪些数值槽」；统一走同一份模型，避免两套逻辑漂移。
function recipeNumericSlots(recipe) {
  return recipeParameterRows(recipe).map((row) => ({
    node_id: row.node_id, field_name: row.field_name,
    value: row.recipe_value ?? row.original,
  }));
}
function recipeParamValueText(value) {
  if (value === undefined) return "同原图";
  if (typeof value === "boolean") return value ? "开" : "关";
  return String(value);
}
// 提交用的覆盖：方案默认 + 本批次填写值，经过同一份参数行校验。
function collectRecipeOverrides(recipe) {
  const result = new Map((recipe?.overrides || []).map((entry) => [`${entry.node_id}|${entry.field_name}`, { node_id: String(entry.node_id), field_name: entry.field_name, value: entry.value }]));
  const rows = new Map(recipeParameterRows(recipe).map((row) => [row.address, row]));
  const host = $("#recipeEffectiveControls");
  for (const input of host ? host.querySelectorAll("[data-recipe-override-node]") : []) {
    const address = `${input.dataset.recipeOverrideNode}|${input.dataset.recipeOverrideField}`;
    const row = rows.get(address);
    if (!row) throw new Error(`节点 ${input.dataset.recipeOverrideNode} · ${input.dataset.recipeOverrideField} 不在当前方案的可覆盖参数里，请重新打开方案核对。`);
    if (input.value === "") continue;
    result.set(address, { node_id: row.node_id, field_name: row.field_name, value: recipeOverrideValue(row, input.value) });
  }
  return [...result.values()];
}
function recipeOverrideValue(row, raw) {
  const where = `节点 ${row.node_id} · ${row.field_name}`;
  if (row.field_type === "bool") {
    if (raw !== "true" && raw !== "false") throw new Error(`${where}：请选择开或关`);
    return raw === "true";
  }
  const value = Number(raw);
  if (!Number.isFinite(value)) throw new Error(`${where}：请填有限数字`);
  if (row.integer && !Number.isInteger(value)) throw new Error(`${where}：这个参数是整数，不能填小数`);
  if (Math.abs(value) > 2 ** 53 - 1) throw new Error(`${where}：超出可精确确认的整数范围`);
  if (row.field_type === "seed" && value < 0) throw new Error(`${where}：固定种子不能是负数；想随机就留空`);
  return value;
}
// 批次页展示的是「原始工作流图 + 待应用覆盖」：服务端返回的 overrides 才是已受理的
// 覆盖，未打补丁的 effective_workflow.prompt 不能当成最终执行图。
function recipeAppliedOverridesMarkup(batch) {
  const applied = Array.isArray(batch?.overrides) ? batch.overrides : [];
  if (!applied.length) return '<p class="super-agent-muted">高级参数：全部沿工作流原图值，没有额外覆盖。</p>';
  const rows = applied.map((entry) => `${text(entry.field_name)}（节点 ${text(entry.node_id)}）= ${text(recipeParamValueText(entry.value))}`);
  return `<p class="super-agent-muted">高级参数（原始工作流图 + 待应用覆盖）：${rows.join(" · ")}</p><p class="super-agent-muted">提示词、素材和时长在执行阶段写入，预览图不等于最终执行图。</p>`;
}
// 创建批量草稿：工作流直建，映射按用户在「参数映射」里指定的为准，不要求先注册方案。
async function submitRecipeCreate(event) {
  event.preventDefault(); if (recipeState.busy) return;
  try {
    const workflow = recipeState.createWorkflow;
    if (!workflow?.id) throw new Error("请选择造梦机已保存的工作流。");
    const shots = Number($("#recipeShots").value);
    if (!Number.isInteger(shots) || shots < 1 || shots > 200) throw new Error("每组生成几条须为 1–200 整数。");
    const mapping = recipeState.createMapping || {};
    const input = recipeState.createInput || {};
    const inputMode = input.inputMode === "script" ? "script" : "prompt";
    const templateId = String(input.templateId || "");
    if (inputMode === "script" && !templateId) throw new Error("反推需要选择模板：H3 两种预设或提词库模板。");
    const prompt = String($("#recipeCreatePrompt").value || "").trim();
    if (prompt.length > 32000) throw new Error("提示词超过长度上限。");
    if (inputMode === "prompt" && prompt.trim() && !mapping.prompt_slot) throw new Error("已填写正文但没有指定文本写入位；请在参数映射里选一个，或清空正文。");
    const durationSlot = mapping.duration_slot || null;
    const durationSeconds = durationSlot ? Number($("#recipeDuration")?.value) : NaN;
    const body = { source: "workflow", workflow_id: workflow.id, workflow_fingerprint: workflow.workflow_fingerprint || workflow.fingerprint || "",
      title: $("#recipeTitle").value.trim() || workflow.name || "批量任务", shots, prompt,
      prompt_slot: mapping.prompt_slot || null,
      media_slots: (mapping.media_slots || []).map((slot) => ({ node_id: String(slot.node_id), field_name: String(slot.field_name), media: slot.media || "image", scope: "item", required: false })),
      ...(durationSlot ? { duration_slot: durationSlot } : {}),
      ...(durationSlot && Number.isFinite(durationSeconds) && durationSeconds > 0 ? { duration_seconds: Math.round(durationSeconds) } : {}),
      ...(inputMode === "script" ? (RECIPE_TEMPLATE_PRESETS.some((row) => row.id === templateId)
        ? { input_mode: "script", template_id: templateId }
        : { input_mode: "script", custom_id: templateId }) : {}) };
    body.request_id = recipeRequest("create", body);
    recipeState.busy = true; $("#recipeCreateSubmit").disabled = true;
    const response = await api("/api/super-agent/recipe-batches", { method: "POST", body });
    if (!response.thread_id || !response.batch) throw new Error("服务端未返回批次，请保持清单并重试确认创建结果。");
    recipeState.requests.delete("create");
    recipeState.createDraft = null; rememberRecipeDraft();
    recipeState.batches.set(response.thread_id, response.batch);
    viewState.selectedId = response.thread_id; viewState.filter = "all";
    $("#superAgentRecipeDialog").close(); await openSuperAgentView();
  } catch (error) { recipeError(error.message || "创建批次失败"); }
  finally { recipeState.busy = false; $("#recipeCreateSubmit").disabled = false; }
}

function recipePrimaryAction(batch) {
  const status = batch.status;
  const included = (batch.items || []).filter((item) => item.included !== false);
  // 与后端 _authorize 口径一致：失败阶段必须逐组补跑，不能整体确认；空提示词不进入生成确认。
  const failedStates = ["failed", "cancelled", "interrupted", "needs_review", "partial", "collection_failed", "collect_failed", "download_failed"];
  const pendingReverse = included.filter((item) => reverseStatus(item) === "pending").length;
  const pendingGenerate = included.filter((item) => generationStatus(item) === "pending").length;
  const failedReverse = included.some((item) => failedStates.includes(reverseStatus(item)));
  const failedGenerate = included.some((item) => failedStates.includes(generationStatus(item)));
  const missingPrompts = included.filter((item) => !["done", "provided"].includes(reverseStatus(item)) && !String(item.prompt || "").trim()).length;
  if (["draft", "review", "partial", "needs_review"].includes(status)) {
    if (pendingReverse && !failedReverse && recipeCan(batch, "confirm_reverse")) return { action: "confirm_reverse", label: "预览并生成提示词" };
    if (pendingGenerate && !failedGenerate && !missingPrompts && recipeCan(batch, "confirm_generate")) {
      return { action: "confirm_generate", label: `确认生成 ${pendingGenerate * Number(batch.shots)} 条视频` };
    }
  }
  if (["paused", "interrupted"].includes(status) && recipeCan(batch, "continue")) return { action: "continue", label: "继续" };
  return null;
}
function recipePhaseLabel(batch) {
  if (batch.status === "draft") {
    const included = (batch.items || []).filter((item) => item.included !== false);
    if (!included.length) return "待纳入项目";
    if (batch.input_mode === "prompt") return included.every((item) => String(item.prompt || "").trim()) ? "待确认生成视频" : "待补齐提示词";
  }
  return RECIPE_PHASES[batch.status] || batch.status;
}
function recipeNextCopy(batch) {
  const included = (batch.items || []).filter((item) => item.included !== false);
  const completed = included.filter((item) => generationStatus(item) === "done").length;
  const prompts = included.filter((item) => ["done", "provided"].includes(reverseStatus(item)) || String(item.prompt || "").trim()).length;
  const outputs = included.reduce((sum, item) => sum + (item.outputs?.length || 0), 0);
  switch (batch.status) {
    case "draft": {
      if (!included.length) return { title: "待纳入项目", hint: "请先纳入至少一组内容，再核对并确认下一步。" };
      if (batch.input_mode === "prompt") {
        if (included.some((item) => !String(item.prompt || "").trim())) return { title: "待补齐提示词", hint: "请填写每个纳入项目的完整视频提示词；本批次不执行提词阶段。" };
        const primary = recipePrimaryAction(batch);
        return { title: primary?.action === "confirm_generate" ? primary.label : "提示词已就绪，待核对", hint: "已使用你提供的完整提示词；核对后确认生成视频，可能产生费用。本批次不执行提词阶段。" };
      }
      return { title: "核对素材，生成提示词", hint: `已纳入 ${included.length} 组；确认后向提词模型发送请求，生成可编辑的提示词。` };
    }
    case "reverse_running": return { title: "正在生成提示词", hint: "完成后需要你核对，再确认生成视频。" };
    case "review": return prompts < included.length
      ? { title: "还有提示词未就绪", hint: `${prompts} / ${included.length} 组提示词就绪；请补齐后再确认生成。` }
      : { title: `确认生成 ${included.length * Number(batch.shots)} 条视频`, hint: "提示词已就绪；确认后提交生成，可能产生费用。" };
    case "needs_review": return { title: "有项目需要核对", hint: "请查看各组说明，处理后继续。" };
    case "partial": return { title: "部分未完成", hint: `已完成 ${completed} 组；未完成项目可逐组重试或重新下载。` };
    case "generation_running": return { title: "正在生成视频", hint: "每条完成后会自动收集结果，不需要你操作。" };
    case "paused": return { title: "已暂停", hint: "继续后从当前进度接着执行。" };
    case "interrupted": return { title: "执行已中断", hint: "核对执行记录后选择继续，或新建批次。" };
    case "completed": return { title: "全部完成", hint: `${outputs} 个结果已收齐，可在对应组下打开或保存。` };
    case "cancelled": return { title: "批次已取消", hint: "已生成的结果仍保留。" };
    default: return { title: "等待当前任务完成", hint: "完成后这里会更新。" };
  }
}
function recipeFocusCopy(batch) {
  const status = batch.status;
  const included = (batch.items || []).filter((item) => item.included !== false);
  const pendingGenerate = included.filter((item) => generationStatus(item) === "pending").length;
  const completed = included.filter((item) => generationStatus(item) === "done").length;
  if (["draft", "review", "needs_review", "partial"].includes(status)) {
    const primary = recipePrimaryAction(batch);
    if (primary?.action === "confirm_reverse") return { tone: "", label: "现在需要你确认", title: "核对素材，生成提示词", text: `共 ${included.length} 组素材 / 文本；确认后将向提词模型发送请求（可能产生费用），生成可编辑的提示词。确认前不会调用模型。` };
    if (primary?.action === "confirm_generate") return { tone: "", label: "现在需要你确认", title: `确认生成 ${pendingGenerate * Number(batch.shots)} 条视频`, text: "提示词就绪后才会进入这一步；确认后提交生成，可能产生费用，不会重新生成提示词。" };
    if (primary?.action === "continue") return { tone: "", label: "已暂停", title: "已暂停后续任务", text: "继续后从当前进度接着执行；在途任务仍可能完成。" };
    return { tone: "is-failed", label: "需要你处理", title: "部分项目未完成", text: "请在下方清单里按组重试或补齐提示词；已成功的结果不会重复执行。" };
  }
  if (status === "reverse_running") return { tone: "is-running", label: "正在执行", title: "正在生成提示词", text: "不需要你操作；完成后这里会提示下一步。" };
  if (status === "generation_running") return { tone: "is-running", label: "正在执行", title: "正在生成视频", text: `已完成 ${completed} / ${included.length} 组；可随时暂停后续任务。` };
  if (status === "paused") return { tone: "", label: "已暂停", title: "已暂停后续任务", text: "继续后从当前进度接着执行；在途任务仍可能完成。" };
  if (status === "interrupted") return { tone: "", label: "执行已中断", title: "执行已中断", text: "核对记录后选择继续，或新建批次。" };
  if (status === "completed") return { tone: "is-done", label: "任务已完成", title: `全部 ${included.length} 组已完成`, text: "结果在下方逐组保留，可打开或保存；需要继续处理可复制为新任务。" };
  if (status === "cancelled") return { tone: "is-done", label: "已取消", title: "批次已取消", text: "已生成的结果仍保留在对应组下。" };
  return { tone: "is-running", label: "进行中", title: "等待任务更新", text: "完成后这里会提示下一步。" };
}
function renderRecipeBatch(batch) {
  const host = $("#superAgentRecipeBatch"); if (!host) return;
  host.hidden = !batch; if (!batch) return;
  if (host.dataset.batchId !== batch.id) {
    host.dataset.batchId = batch.id;
    host.innerHTML = '<div class="recipe-batch-status" aria-live="polite"></div><p id="recipeBatchError" class="super-agent-error" role="alert"></p><div class="recipe-batch-actions"></div><p class="recipe-dirty-notice" role="status"></p><div class="recipe-batch-editors"></div><div class="recipe-batch-results"></div>';
  }
  const draft = recipeDraft(batch); const items = batch.items || [];
  const includedItems = items.filter((item) => item.included !== false);
  const readyItems = includedItems.filter((item) => ["done", "provided"].includes(reverseStatus(item)) || String(item.prompt || "").trim());
  const completedItems = includedItems.filter((item) => generationStatus(item) === "done");
  const editorWasEmpty = !host.querySelector(".recipe-batch-editors").children.length;
  const phase = recipePhaseLabel(batch);
  host.querySelector(".recipe-batch-status").innerHTML = `<h2>${text(batch.recipe?.name, "视频方案")}</h2><span class="super-agent-status" data-status="${text(batch.status)}">${text(phase)}</span><p>${includedItems.length ? `已纳入 ${includedItems.length} 组 · 提示词就绪 ${readyItems.length} 组 · 计划生成 ${Number(batch.counts?.generation_requests ?? includedItems.length * batch.shots)} 条视频` : "还没有纳入的图片组"}</p><p>${recipeDurationSupported(batch.recipe) ? `请求时长 ${Number(batch.duration_seconds)} 秒 · 实际长度按帧对齐，以最终结果为准。` : "时长沿用工作流配置；本方案没有时长映射，不额外改写时长。"}</p>${recipeAppliedOverridesMarkup(batch)}${batch.error ? `<p class="super-agent-error">${text(batch.error)}</p>` : ""}`;
  // 每个状态只给一个当前主操作（在主操作卡中），这里只保留次级操作。
  const primary = recipePrimaryAction(batch);
  const secondary = [];
  if (recipeCan(batch, "edit")) secondary.push(["edit", "保存修改（不执行）"]);
  if (recipeCan(batch, "pause")) secondary.push(["pause", "暂停后续任务"]);
  if (recipeCan(batch, "continue") && !primary) secondary.push(["continue", "继续"]);
  if (recipeCan(batch, "cancel")) secondary.push(["cancel", "取消批次"]);
  host.querySelector(".recipe-batch-actions").innerHTML = '<button type="button" class="control-button tiny" data-recipe-clone>复制为新任务（不执行）</button>' + secondary.map(([action, label]) => `<button type="button" class="control-button tiny" data-recipe-action="${action}" ${recipeState.busy ? "disabled" : ""}>${label}</button>`).join("");
  if (draft.dirty) host.querySelector(".recipe-batch-actions").insertAdjacentHTML("beforeend", '<button type="button" class="control-button tiny" data-recipe-discard>放弃本地草稿，读取最新版本</button>');
  host.querySelector(".recipe-dirty-notice").textContent = draft.dirty
    ? "有未保存修改；确认前会先保存，不会覆盖你的编辑。"
    : ["draft", "review", "needs_review", "partial"].includes(batch.status) ? "修改后需重新确认；保存不会执行模型。" : "";
  const editors = host.querySelector(".recipe-batch-editors");
  // Keep live DOM and cursor while dirty/focused, even when 4-second polling updates statuses.
  if (editorWasEmpty || (!draft.dirty && !editors.contains(document.activeElement))) {
    if (!draft.dirty) { draft.revision = batch.revision; draft.baseline = Object.fromEntries(items.map((item) => [item.id, { requirements: item.requirements || "", prompt: item.prompt || "", included: item.included !== false }])); }
    editors.innerHTML = items.map((savedItem, index) => {
      const item = { ...savedItem, ...(draft.items[savedItem.id] || {}) };
      const locked = !recipeCan(batch, "edit") || generationStatus(item) === "done";
      const label = itemDisplayName(item, index);
      // 逐组审核、逐组确认（计划 §4.3）：哪组准备好就先确认哪组，不等其他组。
      const groupConfirm = recipeCan(batch, "confirm_generate") && item.included !== false
        && generationStatus(item) === "pending" && !item.generate?.attempts?.length
        && Boolean(String(item.prompt || "").trim());
      return `<article class="recipe-item" data-recipe-item-id="${text(item.id)}"><div class="recipe-item-head"><div><h3>第 ${index + 1} 组</h3><span class="recipe-item-status" data-status="${text(generationStatus(item))}">${text(statusLabel(generationStatus(item)))}</span><span class="recipe-item-sub">${text(label ? `${label} · ` : "")}${recipeMediaSummary(item.media)}</span></div><label class="recipe-check"><input type="checkbox" data-recipe-edit="included" ${item.included !== false ? "checked" : ""} ${itemFieldEditable(batch, savedItem, "included") ? "" : "disabled"}>纳入本次</label></div>${recipeMediaMarkup([...(item.media || []), ...(batch.shared_media || [])])}${recipeCan(batch, "edit") && generationStatus(item) !== "done" ? recipeGroupMediaEditor(batch, item.media) : ""}<label>${batch.input_mode === "prompt" ? "内容备注（本批次不执行提词阶段）" : "剧本 / 内容要求（修改后需重新生成提示词）"}<textarea rows="3" data-recipe-edit="requirements" ${itemFieldEditable(batch, savedItem, "requirements") ? "" : "disabled"}>${escapeHtml(item.requirements || "")}</textarea></label><label>最终提示词<textarea rows="5" data-recipe-edit="prompt" ${itemFieldEditable(batch, savedItem, "prompt") ? "" : "disabled"}>${escapeHtml(item.prompt || "")}</textarea></label>${item.reverse?.attempts?.at(-1)?.validation?.errors?.length ? `<p class="super-agent-error">提示词格式问题：${item.reverse.attempts.at(-1).validation.errors.map((error) => text(error)).join("；")}</p>` : ""}${groupConfirm ? `<div class="recipe-group-actions"><button type="button" class="control-button tiny primary" data-recipe-action="confirm_generate" data-recipe-retry-id="${text(item.id)}" ${recipeState.busy ? "disabled" : ""}>预检本组 · 确认生成</button><span class="writer-dialog-hint">确认生成 ≠ 确认反推；其他组仍可编辑。</span></div>` : ""}<div class="recipe-item-results" data-recipe-results-id="${text(item.id)}"></div></article>`;
    }).join("") + (recipeCan(batch, "edit") ? recipeAddGroupCard(batch, items.length) : "");
  }
  host.querySelector(".recipe-batch-results").innerHTML = "";
  for (const item of items) {
    const reverse = reverseStatus(item), generated = generationStatus(item);
    const excluded = item.included === false;
    const retryReverse = !excluded && ["failed", "invalid", "needs_review", "interrupted", "cancelled", "partial"].includes(reverse);
    const retryCollect = !excluded && (["collection_failed", "collect_failed", "download_failed"].includes(generated) || item.capabilities?.retry_collect === true || item.capabilities?.includes?.("retry_collect") === true);
    const retryGenerate = !excluded && ["failed", "interrupted", "needs_review", "partial", "cancelled"].includes(generated) && !retryCollect;
    const failure = item.error || item.reverse?.error || item.generate?.error || item.reverse?.attempts?.at(-1)?.error || item.generate?.attempts?.at(-1)?.error;
    const resultHost = [...editors.querySelectorAll("[data-recipe-results-id]")].find((element) => element.dataset.recipeResultsId === item.id);
    if (resultHost) resultHost.innerHTML = `<p>提示词：${text(statusLabel(reverse))} · 视频：${text(statusLabel(generated))}${excluded ? " · 已排除" : ""}</p>${failure ? `<p class="super-agent-error">${text(failure)}</p>` : ""}<div class="recipe-retry-actions">${[[retryReverse && recipeCan(batch, "retry_reverse"), "retry_reverse", "重试本组提示词"], [retryGenerate && recipeCan(batch, "retry_generate"), "retry_generate", "重试本组视频（保留提示词）"], [retryCollect && recipeCan(batch, "retry_collect"), "retry_collect", "重新下载已有结果（不会重新生成）"]].filter(([show]) => show).map(([, action, label]) => `<button class="control-button tiny" type="button" data-recipe-action="${action}" data-recipe-retry-id="${text(item.id)}">${label}</button>`).join("")}</div><div class="recipe-outputs">${(item.outputs || []).map(recipeOutputMarkup).join("")}</div>`;
  }
  $("#superAgentProgressText").textContent = includedItems.length ? `${completedItems.length} / ${includedItems.length} 组完成` : "暂无内容";
  $("#superAgentProgress").hidden = !includedItems.length;
  $("#superAgentProgress").max = includedItems.length || 1; $("#superAgentProgress").value = completedItems.length;
  $("#superAgentProgressHint").textContent = "仅统计纳入清单且产物完整的项目";
  $("#superAgentCurrentStage").textContent = phase;
  $("#superAgentCurrentHint").textContent = includedItems.length ? `提示词就绪 ${readyItems.length} 组 · 计划生成 ${Number(batch.counts?.generation_requests ?? 0)} 条` : "";
  const nextCopy = recipeNextCopy(batch);
  $("#superAgentNextStep").innerHTML = `<div class="super-agent-ref"><strong>${text(nextCopy.title)}</strong><p>${text(nextCopy.hint)}</p></div>`;
  $("#superAgentChatConfirmationList").innerHTML = "";
  $("#superAgentApprovalList").innerHTML = "";
  $("#superAgentArtifactList").innerHTML = items.some((item) => item.outputs?.length)
    ? `<p class="super-agent-muted">已收集 ${items.reduce((sum, item) => sum + (item.outputs?.length || 0), 0)} 个结果，在对应组下打开或保存。</p>`
    : '<p class="super-agent-muted">尚未收集到结果；生成成功后会显示在对应组下。</p>';
  $("#superAgentConversationLinks").innerHTML = '<p class="super-agent-muted">暂无相关入口。</p>';
  $("#superAgentChainMeta").textContent = "按方案固定流程执行";
  $("#superAgentNodeList").innerHTML = `<p class="super-agent-muted">${text(phase)} · 执行记录与结果在上方逐组清单中保留。</p>`;
  const focusCopy = recipeFocusCopy(batch);
  const focus = $("#superAgentFocus");
  if (focus) {
    focus.hidden = false;
    focus.classList.remove("is-running", "is-done", "is-failed");
    if (focusCopy.tone) focus.classList.add(focusCopy.tone);
    $("#superAgentFocusLabel").textContent = focusCopy.label;
    $("#superAgentFocusTitle").textContent = focusCopy.title;
    $("#superAgentFocusText").textContent = focusCopy.text;
    $("#superAgentFocusActions").innerHTML = primary
      ? `<button type="button" class="control-button primary" data-recipe-action="${primary.action}" ${recipeState.busy ? "disabled" : ""}>${text(primary.label)}</button>`
      : "";
    const note = $("#superAgentFocusNote");
    note.hidden = true;
    note.textContent = "";
  }
  const threadActions = $("#superAgentThreadActions");
  const actionsVisible = recipeCan(batch, "pause") || recipeCan(batch, "continue") || recipeCan(batch, "cancel");
  if (threadActions) {
    threadActions.hidden = !actionsVisible;
    threadActions.querySelectorAll("[data-super-thread-action]").forEach((button) => { button.disabled = !recipeCan(batch, button.dataset.superThreadAction); });
  }
  const controlNote = $("#superAgentControlNote");
  if (controlNote) controlNote.hidden = !actionsVisible;
  $("#superAgentNodeList")?.querySelectorAll("[data-super-action]").forEach((button) => { button.hidden = true; });
}
async function sendRecipeAction(batch, action, extra = {}) {
  const body = { action, revision: batch.revision, ...(batch.revision_hash ? { revision_hash: batch.revision_hash } : {}), ...extra };
  const key = `${batch.thread_id}:${action}`; body.request_id = recipeRequest(key, body);
  const result = await api(`/api/super-agent/threads/${encodeURIComponent(batch.thread_id)}/recipe/actions`, { method: "POST", body });
  if (!result.batch) throw new Error("服务端未返回更新批次，请同步状态后重试。");
  recipeState.requests.delete(key); recipeState.batches.set(batch.thread_id, result.batch); return result.batch;
}
async function saveRecipeDraft(batch) {
  const draft = recipeDraft(batch); if (!draft.dirty) return batch;
  if (draft.revision !== batch.revision) {
    const conflict = Object.entries(draft.items).some(([id, patch]) => {
      const current = batch.items.find((item) => item.id === id);
      return !current || Object.keys(patch).some((field) => current[field] !== draft.baseline?.[id]?.[field]);
    });
    if (conflict) throw new Error("同一字段的服务器版本已变化，已保留草稿。请复制需要的修改，放弃草稿后重新核对。");
  }
  const saved = await sendRecipeAction(batch, "edit", { items: draft.items });
  recipeState.drafts.set(batch.thread_id, { revision: saved.revision, items: {}, dirty: false });
  return saved;
}
function recipeReviewItems(batch, action, itemIds) {
  const reverse = action === "confirm_reverse" || action === "retry_reverse";
  return (batch.items || []).filter((item) => item.included !== false && (!itemIds || itemIds.includes(item.id)) && (reverse ? !["done", "provided"].includes(reverseStatus(item)) : generationStatus(item) !== "done"));
}
function recipeEffectiveWorkflow(item) {
  const workflow = item.effective_workflow;
  return workflow && { id: workflow.id, fingerprint: workflow.fingerprint, graph: workflow.prompt || workflow.graph };
}
function assertRecipeReviewSnapshot(batch, action, itemIds) {
  if (action.includes("reverse") || action === "retry_collect") return;
  for (const item of recipeReviewItems(batch, action, itemIds)) {
    const effective = recipeEffectiveWorkflow(item);
    if (!effective?.id || !effective.fingerprint || !effective.graph || typeof effective.graph !== "object" || Array.isArray(effective.graph)) throw new Error(`第 ${itemDisplayName(item) || "当前"} 组的生成设置还未同步到本页，请刷新后重新核对，暂不能确认生成。`);
  }
}
function recipeTargetLabel(batch) {
  const raw = String(batch?.target?.url || batch?.target || "");
  let host = raw;
  try { host = new URL(raw).hostname; } catch { /* 非 URL 时原样显示主机名 */ }
  return ["127.0.0.1", "localhost", "::1", "[::1]"].includes(host) ? "本机 ComfyUI" : host || "生成服务";
}
function recipeReviewMediaMarkup(item, shared) {
  const media = [...(item.media || []), ...(shared || [])];
  return media.length ? recipeMediaMarkup(media) : '<span class="writer-dialog-hint">纯文本 / 无参考媒体</span>';
}
// ---- 组素材上传（映射了素材入口的方案，逐槽放文件）----
const RECIPE_MEDIA_ACCEPT = { image: "image/*", video: "video/*", audio: "audio/*" };
function recipeGroupMediaEditor(batch, media, { forAdd = false } = {}) {
  const slots = (batch.recipe?.media_slots || []).filter((slot) => (slot.scope || "item") === "item");
  if (!slots.length) return "";
  const rows = slots.map((slot) => {
    const current = (media || []).find((m) => m.role === slot.role);
    const kindLabel = RECIPE_MEDIA_KIND_LABELS[slot.media || "image"] || "图片";
    return `<div class="recipe-group-media-row"><span class="recipe-group-media-label">${text(slot.label || slot.role)}<i>${kindLabel}</i></span>${current ? `<span class="recipe-group-media-file">${text(current.name || "")}</span>` : '<span class="recipe-group-media-file muted">未放文件</span>'}${forAdd || !current ? "" : `<button type="button" class="control-button tiny" data-recipe-media-clear="${text(slot.role)}"${recipeState.busy ? " disabled" : ""}>清掉</button>`}<label class="control-button tiny">选${kindLabel}<input type="file" hidden accept="${RECIPE_MEDIA_ACCEPT[slot.media || "image"] || "image/*"}" data-recipe-media-upload="${text(slot.role)}"${recipeState.busy ? " disabled" : ""}></label></div>`;
  }).join("");
  return `<div class="recipe-group-media"><span class="recipe-group-media-title">素材（入口按方案映射；${batch.input_mode === "prompt" ? "换素材不动已填正文" : "换素材后该组会要求重新反推"}）</span>${rows}</div>`;
}
async function recipeUploadMediaFile(file) {
  const form = new FormData();
  form.append("file", file, file.name);
  return api("/api/comfyui/media", { method: "POST", body: form });
}
async function recipeSetItemMedia(batch, itemId, role, ref) {
  const savedItem = (batch.items || []).find((row) => row.id === itemId) || {};
  const media = (savedItem.media || []).filter((m) => m.role !== role);
  if (ref) media.push({ role, path: String(ref.path || ""), name: String(ref.name || "") });
  const updated = await sendRecipeAction(batch, "edit", { items: { [itemId]: { media } } });
  if (viewState.selectedId === batch.thread_id) renderRecipeBatch(updated);
}
async function recipeSubmitAddGroup(batch) {
  const card = document.querySelector('#superAgentRecipeBatch [data-recipe-add-card]');
  const field = card?.querySelector("[data-recipe-add]");
  const value = String(field?.value || "").trim();
  const media = Object.entries(recipeState.addGroupMedia || {}).map(([role, ref]) => ({ role, path: String(ref.path || ""), name: String(ref.name || "") }));
  const count = (batch.items || []).length;
  const row = { source_path: `added/group-${count + 1}`, name: `组 ${String(count + 1).padStart(2, "0")}`, media };
  if (batch.input_mode === "prompt") row.prompt = value; else row.requirements = value;
  try {
    const updated = await sendRecipeAction(batch, "edit", { add_items: [row] });
    recipeState.addGroupMedia = [];
    recipeState.batches.set(batch.thread_id, updated);
    if (viewState.selectedId === batch.thread_id) renderRecipeBatch(updated);
    toast(`已添加第 ${count + 1} 组`);
  } catch (error) { $("#recipeBatchError").textContent = `添加失败：${error.message}`; }
}
function recipeAddGroupCard(batch, count) {
  const isPrompt = batch.input_mode === "prompt";
  return `<article class="recipe-item recipe-item-add" data-recipe-add-card><div class="recipe-item-head"><div><h3>＋ 添加一组</h3><span class="recipe-item-sub">新组和其他组一样逐个处理；不会触发任何生成</span></div></div>${recipeGroupMediaEditor(batch, Object.entries(recipeState.addGroupMedia || {}).map(([role, ref]) => ({ role, name: ref.name })), { forAdd: true })}<label>${isPrompt ? "本组正文（最终提示词，可留空进组后补）" : "本组要求（交给反推模板；可留空进组后补）"}<textarea rows="3" data-recipe-add placeholder="${isPrompt ? "直接填这一组的最终提示词" : "写这一组要发生什么"}"></textarea></label><div class="recipe-group-actions"><button type="button" class="control-button tiny primary" data-recipe-add-submit ${recipeState.busy ? "disabled" : ""}>添加第 ${count + 1} 组</button><span class="writer-dialog-hint">素材先选好再点添加；进组后还能逐项改。</span></div></article>`;
}
// 就绪检查前置：缺项在确认页直接可见，不再走到最后一步才被拒（A05）。
function recipeReadinessList(batch, action, items) {
  const reverse = action === "confirm_reverse" || action === "retry_reverse";
  const rows = [];
  const missingMedia = items.filter((item) => (batch.recipe?.media_slots || []).some((slot) => slot.required && ![...(item.media || []), ...(batch.shared_media || [])].some((entry) => entry.role === slot.role)));
  rows.push({ ok: !missingMedia.length, label: missingMedia.length ? `${missingMedia.length} 组缺少方案要求的素材` : "素材按当前方案要求已核对；可选参考不必填满" });
  if (items.some((item) => [...(item.media || []), ...(batch.shared_media || [])].some((entry) => ["video", "audio"].includes(recipeMediaKind(entry))))) rows.push({ ok: true, label: "提词模型仅使用音视频文字描述，不观看 / 收听；参考视频仅用画面，音轨请另加音频" });
  if (reverse) {
    const missingScript = items.filter((item) => !String(item.requirements || "").trim()).length;
    rows.push(missingScript
      ? { ok: false, label: `${missingScript} 组还没有剧本正文；生成提示词需要剧本` }
      : { ok: true, label: `${items.length} 组剧本已填写` });
  } else if (action !== "retry_collect") {
    const missingPrompt = items.filter((item) => !String(item.prompt || "").trim()).length;
    rows.push(missingPrompt
      ? { ok: false, label: `${missingPrompt} 组提示词为空，确认会被拦下` }
      : { ok: true, label: `${items.length} 条提示词已就绪` });
  } else {
    rows.push({ ok: true, label: "只重新下载已有结果，不会重新生成" });
  }
  if (action.includes("generate")) {
    const missingSnapshot = items.filter((item) => { const effective = recipeEffectiveWorkflow(item); return !effective?.id || !effective.fingerprint || !effective.graph; }).length;
    rows.push(missingSnapshot
      ? { ok: false, label: `${missingSnapshot} 组生成设置未同步，请刷新后重新核对` }
      : { ok: true, label: "生成设置已同步到本页" });
  }
  rows.push({ ok: true, label: "已成功的结果不会重复执行" });
  return `<div class="sa-sec-head" style="margin-top:18px"><h3>就绪检查</h3><span>确认前自动核对</span></div><div class="recipe-check-list" style="margin-top:10px">${rows.map((row) => `<div class="recipe-check-item${row.ok ? "" : " warn"}"><i>${row.ok ? "✓" : "!"}</i><span>${text(row.label)}</span></div>`).join("")}</div>`;
}
// 审核确认页：普通用户视角——素材、提示词、数量与费用；不再展示 JSON、绝对路径、指纹与模板原文。
function recipeReviewMarkup(batch, action, itemIds) {
  const reverse = action === "confirm_reverse" || action === "retry_reverse";
  const collect = action === "retry_collect";
  const items = recipeReviewItems(batch, action, itemIds);
  const shots = Number(batch.shots);
  const count = reverse || collect ? items.length : items.length * shots;
  const excluded = (batch.items || []).filter((item) => item.included === false);
  const authorize = reverse
    ? `本次将向提词模型发送 ${count} 项请求（可能产生费用）；只生成提示词，不制作视频。`
    : collect
      ? "只重新下载已经生成的结果，不会重新生成，也不会改写提示词。"
      : `本次将提交 ${count} 条视频生成（可能产生费用）；不会重新生成提示词，也不会改写你已填好的内容。`;
  return `<div class="recipe-review-summary"><span class="recipe-review-kv">方案 <b>${text(batch.recipe?.name, "视频方案")}</b></span><span class="recipe-review-kv">本次涉及 <b>${items.length} 组</b></span>${reverse || collect ? "" : `<span class="recipe-review-kv">每组 <b>${shots} 条</b></span><span class="recipe-review-kv">时长 <b>${recipeDurationSupported(batch.recipe) ? `${Number(batch.duration_seconds)} 秒` : "沿用工作流"}</b></span>`}<span class="recipe-review-kv">运行位置 <b>${text(recipeTargetLabel(batch))}</b></span></div><p class="writer-dialog-hint">已成功的结果不会重复执行。${excluded.length ? `已明确排除：${excluded.map((item) => text(itemDisplayName(item) || item.source_path)).join("、")}。` : ""}</p><p><strong>${authorize}</strong></p>${reverse ? `<p class="writer-dialog-hint">提词模型：${text(batch.model?.model_name || batch.model?.model_key, "未选择")}</p>` : ""}${items.length ? `<div class="recipe-review-items">${items.map((item, index) => `<article class="recipe-review-card"><div class="recipe-review-media">${recipeReviewMediaMarkup(item, batch.shared_media)}</div><div class="recipe-review-body"><h3>${text(itemDisplayName(item) || `第 ${index + 1} 组`)}</h3>${reverse ? `<p class="writer-dialog-hint">剧本 / 内容要求：${text(item.requirements, "（未填写）")}</p>` : `<div class="recipe-review-prompt">${escapeHtml(item.prompt || "")}</div>`}<p class="writer-dialog-hint">素材 ${recipeMediaSummary([...(item.media || []), ...(batch.shared_media || [])])}${reverse || collect ? "" : ` · 生成 ${shots} 条`}</p></div></article>`).join("")}</div>` : '<p class="writer-dialog-hint">所选范围没有需要执行的项目。</p>'}${recipeReadinessList(batch, action, items)}`;
}
async function recipeAction(action, itemIds = null) {
  const threadId = viewState.selectedId; let batch = recipeState.batches.get(threadId);
  if (!batch || recipeState.busy || !recipeCan(batch, action)) return;
  recipeState.busy = true;
  try {
    if (action === "cancel" && !window.confirm("取消只停止还没开始的任务；正在执行的可能仍会完成。确认取消本批次？")) return;
    if (!["pause", "continue", "cancel"].includes(action)) batch = await saveRecipeDraft(batch);
    renderRecipeBatch(batch);
    if (["confirm_reverse", "confirm_generate", "retry_reverse", "retry_generate", "retry_collect"].includes(action)) {
      recipeState.review = { batch, action, itemIds, snapshotOk: true };
      const reviewItems = recipeReviewItems(batch, action, itemIds);
      const reverse = action.includes("reverse");
      const count = reverse || action === "retry_collect" ? reviewItems.length : reviewItems.length * Number(batch.shots);
      $("#recipeReviewTitle").textContent = reverse ? "确认生成提示词" : action === "retry_collect" ? "重新下载已有结果" : "生成前预检 · 确认生成";
      $("#recipeReviewConfirm").textContent = reverse ? `确认生成 ${count} 组提示词` : action === "retry_collect" ? "重新下载" : `确认生成 ${count} 条视频`;
      $("#recipeReviewHint").textContent = reverse
        ? "确认后向提词模型发送请求；只生成提示词，不制作视频。"
        : action === "retry_collect"
          ? "只重新下载已有结果，不会重新生成。"
          : "预检通过后才提交视频生成；缺项会在这一步之前拦下。";
      $("#recipeReviewError").textContent = ""; $("#recipeReviewBody").innerHTML = recipeReviewMarkup(batch, action, itemIds);
      try { assertRecipeReviewSnapshot(batch, action, itemIds); }
      catch (error) { $("#recipeReviewError").textContent = error.message; recipeState.review.snapshotOk = false; }
      // 生成前预检（计划 §5.3）：冻结目标四项检查通过才允许确认；反推不探测 ComfyUI。
      recipeState.precheckOk = !["confirm_generate", "retry_generate", "retry_collect"].includes(action);
      $("#recipeReviewConfirm").disabled = !recipeState.precheckOk || recipeState.review.snapshotOk === false;
      const precheckHost = $("#recipeReviewPrecheck");
      if (precheckHost) { precheckHost.hidden = recipeState.precheckOk; if (recipeState.precheckOk) precheckHost.innerHTML = ""; }
      $("#recipeReviewDialog").showModal();
      if (!recipeState.precheckOk) await runReviewPrecheck(batch, action, reviewItems);
      return;
    }
    if (action !== "edit") batch = await sendRecipeAction(batch, action);
    $("#recipeBatchError").textContent = "";
    renderRecipeBatch(batch);
  } catch (error) { const box = $("#recipeBatchError"); if (box) box.textContent = `操作未完成：${error.message}`; }
  finally { recipeState.busy = false; if (viewState.selectedId === threadId) renderRecipeBatch(recipeState.batches.get(threadId)); }
}
// 生成前预检（计划 §5.3）：只读探测本批冻结目标与就绪项；结果不伪造全绿。
function recipePrecheckMarkup(report) {
  const rows = (report.checks || []).map((check) => `<div class="recipe-check-item${check.ok ? "" : " warn"}"><i>${check.ok ? "✓" : "!"}</i><span><b>${text(check.label)}</b>${check.detail ? ` · ${text(check.detail)}` : ""}</span></div>`).join("");
  let host = report.target?.url || "生成目标";
  try { host = new URL(report.target?.url || "").host || host; } catch { /* 原样显示 */ }
  const time = report.checked_at ? new Date(report.checked_at).toLocaleTimeString() : "";
  return `<div class="recipe-precheck"><p class="recipe-precheck-target">本组冻结目标：<b>${text(host)}</b> · ${text(time)} 检测 · 一次「在线」不代表长期可用</p><div class="recipe-check-list">${rows}</div><p class="writer-dialog-hint">预检不保证显存充足、模型已安装或付费余额足够；执行器仍保留上传、图校验与提交前的最后校验。检查后掉线会如实区分「确定未提交」与「提交结果未知」。</p>${report.ok ? "" : '<button type="button" class="control-button tiny" data-recipe-precheck-retry>重新检测</button>'}</div>`;
}
async function runReviewPrecheck(batch, action, items) {
  const host = $("#recipeReviewPrecheck");
  if (!host) { recipeState.precheckOk = true; return; }
  host.hidden = false;
  host.innerHTML = '<p class="writer-dialog-hint">正在预检本组冻结目标（实际目标可达 / 工作流与输入 / 媒体 / 提示词引用）…</p>';
  try {
    const report = await api(`/api/super-agent/threads/${encodeURIComponent(batch.thread_id)}/recipe/precheck`, { method: "POST", body: {} });
    host.innerHTML = recipePrecheckMarkup(report);
    recipeState.precheckOk = Boolean(report.ok);
  } catch (error) {
    host.innerHTML = `<p class="super-agent-error">预检不可用：${text(error.message)}。预检通过前不能确认生成。</p><button type="button" class="control-button tiny" data-recipe-precheck-retry>重新检测</button>`;
    recipeState.precheckOk = false;
  }
  $("#recipeReviewConfirm").disabled = !recipeState.precheckOk || recipeState.review?.snapshotOk === false;
}
async function confirmRecipeReview() {
  const review = recipeState.review; if (!review || recipeState.busy) return;
  recipeState.busy = true; $("#recipeReviewConfirm").disabled = true;
  try {
    if (recipeDraft(review.batch).dirty) throw new Error("审核打开后仍有修改，请返回保存并重新审核。");
    if (!["confirm_reverse", "retry_reverse"].includes(review.action) && !recipeState.precheckOk) throw new Error("生成前预检未通过；请处理检查项或重新检测后再确认。");
    assertRecipeReviewSnapshot(review.batch, review.action, review.itemIds);
    const batch = await sendRecipeAction(review.batch, review.action, review.itemIds ? { item_ids: review.itemIds } : {});
    $("#recipeReviewDialog").close(); recipeState.review = null;
    const errorBox = $("#recipeBatchError"); if (errorBox) errorBox.textContent = "";
    if (viewState.selectedId === batch.thread_id) renderRecipeBatch(batch);
  } catch (error) { $("#recipeReviewError").textContent = `确认未受理：${error.message}。请同步批次后重新核对，不会自动授权新版本。`; }
  finally { recipeState.busy = false; $("#recipeReviewConfirm").disabled = false; if (viewState.selectedId === review.batch.thread_id) renderRecipeBatch(recipeState.batches.get(viewState.selectedId)); }
}

// 值全部在总管批次里定：不再有「未保存调整带成参数覆盖」的通道；造梦机里的
// 临时调整要么另存进工作流，要么在总管批次页逐项填。
// ---- 造梦机交接：工作流快照 + 用户映射 → 批量草稿 ----
// 值全部在总管批次里定：交接只带工作流快照与映射（文本位/素材入口/时长槽），
// 不再有「已保存值 vs 未保存调整」二选一；映射按工作流指纹记住，下次自动带入。
export async function openRecipeWorkflowIntent(intent) {
  const workflow = intent?.workflow;
  if (!workflow) return;
  recipeError("");
  const slots = recipePromptSlotCandidates(workflow.params || []);
  const fingerprint = workflow.workflow_fingerprint || workflow.fingerprint || "";
  const mapping = recalledRecipeMapping(fingerprint) || recipeMappingFromWorkflow(workflow);
  adoptWorkflowMediaIntoMapping(mapping, workflow, intent.media_inputs || []);
  recipeState.handoff = { intent, slots, mapping, fingerprint, inputMode: "prompt", templateId: RECIPE_TEMPLATE_PRESETS[0].id };
  $("#superAgentHandoffError").textContent = "";
  $("#superAgentHandoffWorkflow").textContent = `当前工作流：${workflow.name || workflow.id}`;
  renderRecipeMappingEditor("superAgentHandoff", workflow, mapping);
  renderRecipeInputMode("superAgentHandoff", recipeState.handoff);
  renderHandoffSummary();
  syncHandoffDialog();
  $("#superAgentHandoffDialog").showModal();
}
// 交接摘要随参数映射与正文来源刷新：多文本槽工作流显示的必须是映射槽位的正文；
// 时长 / 分辨率亮出实际带入的保存值，不能只说「沿用槽位」。
function recipeHandoffDurationText(handoff) {
  const workflow = handoff.intent.workflow;
  const slot = handoff.mapping?.duration_slot;
  if (!slot) return "未映射时长槽；按工作流原配置出片，不额外改写时长";
  const value = recipeSlotValue(workflow, slot);
  const seconds = Number(value);
  const shown = value !== "" && Number.isFinite(seconds) ? `${seconds} 秒` : "未填（将按 15 秒执行）";
  return `${shown} · 沿用工作流保存值（进批次后可改）`;
}
function recipeHandoffResolutionText(handoff) {
  // 分辨率 / 画幅没有统一槽位：width/height 走 role，H3 这类工作流用
  // 「解析度選擇器」（aspect_ratio / megapixels）——按字段名与节点标题一起认，
  // 只做展示不影响提交；都没有就如实说按工作流原配置出片。
  const workflow = handoff.intent.workflow;
  const slots = (workflow.params || []).filter((slot) => ["number", "text"].includes(slot.field_type)
    && (slot.role === "resolution"
      || ["width", "height", "aspect_ratio", "megapixels"].includes(slot.field_name)
      || /解析度|分辨率/.test(String(slot.node_title || ""))));
  if (!slots.length) return "工作流未暴露分辨率 / 画幅参数；按工作流原配置出片";
  const parts = slots.slice(0, 4).map((slot) => {
    const value = recipeSlotValue(workflow, slot);
    const name = slot.field_name === "height" ? "高" : slot.field_name === "width" ? "宽" : slot.field_name;
    return `${name} ${value !== "" ? value : "未填"}`;
  });
  return `${parts.join(" · ")}${slots.length > 4 ? `（等共 ${slots.length} 项）` : ""} · 沿用工作流保存值`;
}
function renderHandoffSummary() {
  const handoff = recipeState.handoff; if (!handoff) return;
  const workflow = handoff.intent.workflow;
  const mapping = handoff.mapping || {};
  const selected = mapping.prompt_slot;
  const savedPrompt = recipeSlotValue(workflow, selected);
  const media = handoff.intent.media_inputs || [];
  const mediaCounts = { image: 0, video: 0, audio: 0 };
  for (const entry of media) mediaCounts[(workflow.params || []).find((param) => String(param.node_id) === String(entry.node_id) && param.field_name === entry.field_name)?.field_type || "image"] += 1;
  const mediaFiles = media.length ? Object.entries(mediaCounts).filter(([, count]) => count).map(([kind, count]) => `${count} ${({ image: "张图", video: "段视频", audio: "段音频" })[kind]}`).join("、") : "";
  const mediaText = (mapping.media_slots || []).length
    ? `已映射 ${(mapping.media_slots || []).length} 个素材入口${mediaFiles ? `，本次带 ${mediaFiles}` : "，文件进批次后逐组放"}`
    : "未映射素材入口（不需要素材可留空）";
  const promptText = selected
    ? `${mapping.prompt_slot.node_id} / ${mapping.prompt_slot.field_name}${String(savedPrompt || "").length ? `（${String(savedPrompt).length} 字）` : "（空，进批次后补）"}`
    : "未映射（正文沿用工作流里的原值）";
  const sourceText = handoff.inputMode === "script" ? `反推 · ${recipeTemplateLabel(handoff.templateId)}` : "直接用现成正文（不反推）";
  $("#superAgentHandoffSummary").innerHTML = `<div class="recipe-check-list"><div class="recipe-check-item"><i>✓</i><span><b>工作流</b> · ${text(workflow.name || workflow.id)}（按这份快照出片）</span></div><div class="recipe-check-item"><i>✓</i><span><b>正文来源</b> · ${text(sourceText)}</span></div><div class="recipe-check-item"><i>✓</i><span><b>提示词</b> · ${text(promptText)}</span></div><div class="recipe-check-item"><i>✓</i><span><b>素材</b> · ${text(mediaText)}</span></div><div class="recipe-check-item"><i>✓</i><span><b>时长</b> · ${text(recipeHandoffDurationText(handoff))}</span></div><div class="recipe-check-item"><i>✓</i><span><b>分辨率 / 画幅</b> · ${text(recipeHandoffResolutionText(handoff))}</span></div><div class="recipe-check-item"><i>✓</i><span><b>生成条数</b> · 首组 ${text(String(Number(handoff.intent.shots) || 1))} 条（沿用造梦机「张数」）</span></div></div>`;
}
function syncHandoffDialog() {
  const handoff = recipeState.handoff; if (!handoff) return;
  $("#superAgentHandoffConfirm").disabled = false;
  $("#superAgentHandoffError").textContent = "";
}
async function confirmWorkflowHandoff() {
  const handoff = recipeState.handoff;
  if (!handoff || recipeState.busy) return;
  const workflow = handoff.intent.workflow;
  const mapping = handoff.mapping || {};
  try {
    // 正文取工作流快照里映射槽位的保存值；参数/素材/时长的最终值都在总管批次里定。
    const prompt = recipeSlotValue(workflow, mapping.prompt_slot);
    if (handoff.inputMode === "script" && !handoff.templateId) throw new Error("反推需要选择模板：H3 两种预设或提词库模板。");
    // 这次确认的映射按工作流指纹记住：下次交接同一工作流自动带入，可改。
    rememberRecipeMapping(handoff.fingerprint, mapping);
    let durationSeconds;
    if (mapping.duration_slot) {
      const value = Number(recipeSlotValue(workflow, mapping.duration_slot));
      if (Number.isFinite(value) && value > 0) durationSeconds = Math.round(value);
    }
    const templateId = String(handoff.templateId || "");
    const body = { source: "workflow", workflow_id: workflow.id, workflow_fingerprint: handoff.fingerprint,
      title: `${workflow.name || "工作流"} · 批量`, shots: Number(handoff.intent.shots) || 1,
      prompt: String(prompt),
      prompt_slot: mapping.prompt_slot || null,
      media_slots: (mapping.media_slots || []).map((slot) => ({ node_id: String(slot.node_id), field_name: String(slot.field_name), media: slot.media || "image", scope: "item", required: false })),
      ...(mapping.duration_slot ? { duration_slot: mapping.duration_slot } : {}),
      ...(durationSeconds ? { duration_seconds: durationSeconds } : {}),
      ...(handoff.inputMode === "script" ? (RECIPE_TEMPLATE_PRESETS.some((row) => row.id === templateId)
        ? { input_mode: "script", template_id: templateId }
        : { input_mode: "script", custom_id: templateId }) : {}),
      media_inputs: handoff.intent.media_inputs || [] };
    body.request_id = recipeRequest(`handoff:${workflow.id}`, body);
    recipeState.busy = true; $("#superAgentHandoffConfirm").disabled = true;
    const response = await api("/api/super-agent/recipe-batches", { method: "POST", body });
    if (!response.thread_id || !response.batch) throw new Error("服务端未返回批次，请保持工作流打开并重试。");
    recipeState.requests.delete(`handoff:${workflow.id}`);
    recipeState.handoff = null;
    recipeState.batches.set(response.thread_id, response.batch);
    $("#superAgentHandoffDialog").close();
    viewState.selectedId = response.thread_id; viewState.filter = "all";
    await openSuperAgentView();
  } catch (error) { $("#superAgentHandoffError").textContent = error.message || "交接失败"; }
  finally { recipeState.busy = false; if (recipeState.handoff) $("#superAgentHandoffConfirm").disabled = false; }
}
async function cloneRecipeBatch() {
  try {
    const result = await api(`/api/super-agent/threads/${encodeURIComponent(viewState.selectedId)}/recipe/clone-draft`);
    const draft = result.draft; if (!draft) throw new Error("未返回可复制草稿");
    if (!draft.workflow?.id) throw new Error("这个批次来自旧方案流程，不能按工作流直建复制；请新建批量任务重新选择工作流。原批次与结果保留不变。");
    const first = (draft.items || [])[0] || {};
    recipeState.createDraft = { workflowId: draft.workflow.id, workflowFingerprint: draft.workflow.workflow_fingerprint || "",
      title: `${draft.title || "视频批次"} · 副本`, shots: draft.shots || 1, prompt: String(first.prompt || "") };
    await openRecipeDialog();
    if ((draft.items || []).length > 1) recipeError(`原批次有 ${draft.items.length} 组；当前带入第 1 组，其余组创建后在批次里逐组添加。`);
  } catch (error) { $("#recipeBatchError").textContent = `复制失败：${error.message}`; }
}
function bindRecipeEvents() {
  $("#superAgentRecipeCreate")?.addEventListener("click", () => void openRecipeDialog());
  $("#superAgentFocus")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-recipe-action]");
    if (button) void recipeAction(button.dataset.recipeAction, button.dataset.recipeRetryId ? [button.dataset.recipeRetryId] : null);
  });
  $("#recipeWorkflowSelect")?.addEventListener("change", () => void loadCreateWorkflowDetail());
  // 参数映射（直建）：改动即刷新并进草稿；交接侧同样绑定。
  bindRecipeMappingEditor("recipeCreate", () => recipeState.createMapping, () => { renderRecipeMappingEditor("recipeCreate", recipeState.createWorkflow, recipeState.createMapping); rememberRecipeDraft(); });
  $("#recipeCreateInputMode")?.addEventListener("click", (event) => {
    const card = event.target.closest("[data-input-mode]");
    if (!card || !recipeState.createInput) return;
    recipeState.createInput.inputMode = card.dataset.inputMode;
    renderRecipeInputMode("recipeCreate", recipeState.createInput);
    rememberRecipeDraft();
  });
  $("#recipeCreateTemplate")?.addEventListener("change", () => {
    if (!recipeState.createInput) return;
    recipeState.createInput.templateId = $("#recipeCreateTemplate").value;
    rememberRecipeDraft();
  });
  for (const type of ["input", "change"]) $("#superAgentRecipeForm")?.addEventListener(type, () => rememberRecipeDraft());
  window.addEventListener("beforeunload", rememberRecipeDraft);
  $("#superAgentRecipeForm")?.addEventListener("submit", (event) => void submitRecipeCreate(event));
  $("#superAgentRecipeDialog")?.addEventListener("click", (event) => { if (event.target.closest("[data-recipe-close]")) $("#superAgentRecipeDialog").close(); });
  // 参数映射（交接）+ 正文来源切换：改动即刷新摘要。
  bindRecipeMappingEditor("superAgentHandoff", () => recipeState.handoff?.mapping, () => {
    if (!recipeState.handoff) return;
    renderRecipeMappingEditor("superAgentHandoff", recipeState.handoff.intent.workflow, recipeState.handoff.mapping);
    renderHandoffSummary();
  });
  $("#superAgentHandoffInputMode")?.addEventListener("click", (event) => {
    const card = event.target.closest("[data-input-mode]");
    if (!card || !recipeState.handoff) return;
    recipeState.handoff.inputMode = card.dataset.inputMode;
    renderRecipeInputMode("superAgentHandoff", recipeState.handoff);
    renderHandoffSummary();
  });
  $("#superAgentHandoffTemplate")?.addEventListener("change", () => {
    if (!recipeState.handoff) return;
    recipeState.handoff.templateId = $("#superAgentHandoffTemplate").value;
    renderHandoffSummary();
  });
  // 交接：确认 + 关闭（参数来源二选一已移除——值全部在总管批次里定）。
  $("#superAgentHandoffConfirm")?.addEventListener("click", () => void confirmWorkflowHandoff());
  $("#superAgentHandoffDialog")?.addEventListener("click", (event) => { if (event.target.closest("[data-handoff-close]")) { $("#superAgentHandoffDialog").close(); recipeState.handoff = null; } });
  $("#superAgentRecipeBatch")?.addEventListener("change", (event) => {
    const upload = event.target.closest("[data-recipe-media-upload]");
    if (upload) {
      const file = upload.files?.[0];
      upload.value = "";
      if (!file || recipeState.busy) return;
      const batch = recipeState.batches.get(viewState.selectedId); if (!batch) return;
      const role = upload.dataset.recipeMediaUpload;
      const addCard = upload.closest("[data-recipe-add-card]");
      void (async () => {
        try {
          recipeState.busy = true;
          const ref = await recipeUploadMediaFile(file);
          if (addCard) {
            recipeState.addGroupMedia[role] = { path: String(ref.path || ""), name: String(ref.name || file.name) };
            renderRecipeBatch(batch);
          } else {
            const itemId = upload.closest("[data-recipe-item-id]")?.dataset.recipeItemId;
            if (itemId) await recipeSetItemMedia(batch, itemId, role, ref);
          }
        } catch (error) { toast(`素材上传失败：${error.message}`); }
        finally { recipeState.busy = false; }
      })();
      return;
    }
    const field = event.target.dataset.recipeEdit; if (!field || event.target.disabled) return;
    const batch = recipeState.batches.get(viewState.selectedId), id = event.target.closest("[data-recipe-item-id]")?.dataset.recipeItemId; if (!batch || !id) return;
    const draft = recipeDraft(batch);
    draft.items[id] = { ...(draft.items[id] || {}), [field]: field === "included" ? event.target.checked : event.target.value }; draft.dirty = true;
    $("#superAgentRecipeBatch .recipe-dirty-notice").textContent = "有未保存修改；轮询不会覆盖编辑，确认前会先保存。";
  });
  $("#superAgentRecipeBatch")?.addEventListener("click", (event) => {
    if (event.target.closest("[data-recipe-clone]")) { void cloneRecipeBatch(); return; }
    if (event.target.closest("[data-recipe-discard]")) { if (window.confirm("确认放弃当前未保存的本地编辑？")) { recipeState.drafts.delete(viewState.selectedId); $("#superAgentRecipeBatch .recipe-batch-editors").innerHTML = ""; renderRecipeBatch(recipeState.batches.get(viewState.selectedId)); } return; }
    const clear = event.target.closest("[data-recipe-media-clear]");
    if (clear && !recipeState.busy) {
      const batch = recipeState.batches.get(viewState.selectedId), itemId = clear.closest("[data-recipe-item-id]")?.dataset.recipeItemId;
      if (batch && itemId) void recipeSetItemMedia(batch, itemId, clear.dataset.recipeMediaClear, null);
      return;
    }
    if (event.target.closest("[data-recipe-add-submit]")) {
      const batch = recipeState.batches.get(viewState.selectedId);
      if (batch && !recipeState.busy) void recipeSubmitAddGroup(batch);
      return;
    }
    const button = event.target.closest("[data-recipe-action]"); if (button) void recipeAction(button.dataset.recipeAction, button.dataset.recipeRetryId ? [button.dataset.recipeRetryId] : null);
  });
  $("#recipeReviewCancel")?.addEventListener("click", () => { $("#recipeReviewDialog").close(); recipeState.review = null; });
  $("#recipeReviewConfirm")?.addEventListener("click", () => void confirmRecipeReview());
  $("#recipeReviewPrecheck")?.addEventListener("click", (event) => {
    if (!event.target.closest("[data-recipe-precheck-retry]")) return;
    const review = recipeState.review; if (!review) return;
    void runReviewPrecheck(review.batch, review.action, recipeReviewItems(review.batch, review.action, review.itemIds));
  });
}
