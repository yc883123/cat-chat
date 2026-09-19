// ============================================================
// 18-interjections.js —— 插话（interjection）队列面板
//
// 插话是**运行中的第二输入通道**：模型正在回答时，用户还能继续打字，但打的字不会
// 直接变成新一轮提问（那会被 ACTIVE_RUN 拒绝，也会把两个意图搅在一起），而是先排进
// 队列；用户随时可以把它「引导」进正在跑的那一轮（agent 下一步就取走），或编辑 / 删除。
//
// 与「选择」面板（12-chat-input.js，choice_groups）的共存规则——两者时间窗天然互斥：
//   · 插话队列 = 运行中的输入通道；选择面板 = 运行结束后的输入通道。
//   · 本模块不碰 syncChoicePanelLock / submitChoice，也不把选择答案并进队列。
//   · 不做任何「自动 follow-up」：run 结束时队列里剩的插话只提示、不自动开新一轮
//     （否则终态带选择题时，刚弹出的选择面板会被自动开跑的新 run 立刻锁死）。
//
// 数据来源：`GET 会话消息` 里带 metadata.interjection 的 role=user 行。落库形态见
// naiba/storage/store.py 的插话段；未消费的插话**既不进模型上下文**（core/history.py），
// 也不进消息流（04-messages.js 的可见性过滤），只在本面板里出现。
// ============================================================

import { $, api, escapeHtml, state, toast } from "./01-core.js";
import { updateContextComposerLock } from "./03-media.js";
import { missingAttachmentPaths, renderPendingFiles } from "./10-upload.js";
import { hideSkillPopup, renderInputMirror, resizeTextarea } from "./13-skill-refs.js";
import { hideFilePopup } from "./16-file-refs.js";

let editingInterjectionId = '';   // 正在就地编辑的插话（重渲染时必须保住输入框，见 renderRunGuidance）
let interjectionSubmitting = false;   // 入队请求的互斥标志（连点发送按钮只发一条）
let queuedCache = [];             // 最近一次渲染拿到的队列消息原文（「取回」要连附件一起还原）
// 已在本地判定「这一轮结束了」的 Run id。后端 `_finish` 也会把这些行标成 stopped，但那是
// 事件发出**之后**才落库的：期间若来一次会话轮询，面板会照着旧 metadata 把行翻回「待引导」，
// 用户就会对着一个只会 404 的按钮点。本地记一份，重渲染时以它为准。
const frozenRunIds = new Set();

/** 该消息是否是「尚未被 agent 消费」的插话：不进消息流、由本面板承载。 */
export function isQueuedInterjection(message) {
  const metadata = message?.metadata || {};
  return message?.role === 'user'
    && Boolean(metadata.interjection)
    && !metadata.interjection_consumed;
}

/**
 * 队列行状态：
 * - `guided`  已点「引导」，等 agent 在下一步取走（不可再操作）；
 * - `stopped` 本轮已被取消，队列就地冻结（README 承诺绝不自动发送，只能删除）；
 * - `pending` 待引导（三个动作都可用）。
 */
function interjectionState(message) {
  const metadata = message?.metadata || {};
  if (metadata.interjection_consumed) return 'consumed';
  if (metadata.interjection_stopped) return 'stopped';
  if (metadata.run_id && frozenRunIds.has(String(metadata.run_id))) return 'stopped';
  if (metadata.interjection_guided) return 'guided';
  return 'pending';
}

const INTERJECTION_STATE_TEXT = {
  pending: '待引导',
  guided: '已引导',
  stopped: '已停止',
};

const INTERJECTION_GUIDE_HINT =
  '本轮结束后不会自动发送；点「引导」才会立刻并进当前任务。';

const ICON_EDIT = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4L19.5 8.5a2.12 2.12 0 0 0-3-3L5 17l-1 4Z"></path><path d="M13.5 6.5l3 3"></path></svg>';
const ICON_DELETE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg>';
const ICON_GUIDE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7"></path></svg>';
const ICON_REUSE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 14 4 9l5-5"></path><path d="M4 9h9a7 7 0 0 1 0 14h-4"></path></svg>';

/**
 * 「取回」（填入输入框并移出队列）这条出口是必需的：本版砍掉了自动 follow-up，run 一旦
 * 结束就再没有 Run 可引导——没有它，残留的插话就只能删，用户刚打的字白丢。
 * 已引导（guided）且 Run 还活着的行不给这个出口：它即将被 agent 取走，取回会造成两份。
 */
function reuseButtonHtml(state) {
  if (state === 'guided') return '';
  return `<button type="button" data-interjection-reuse title="取回输入框（把内容放回输入框并移出队列）" aria-label="取回输入框">${ICON_REUSE}</button>`;
}

function interjectionRowFor(messageId) {
  const container = $('#runGuidanceList');
  const id = String(messageId || '');
  if (!container || !id) return null;
  return container.querySelector(`.run-guidance-card[data-message-id="${CSS.escape(id)}"]`);
}

/** 附件数量徽标（队列行不渲染缩略图：一行一句话，缩略图会把面板撑成半屏）。 */
function interjectionAttachmentNote(metadata) {
  const count = Array.isArray(metadata?.attachments) ? metadata.attachments.length : 0;
  return count ? `<span class="run-guidance-badge">${count} 个附件</span>` : '';
}

/**
 * 一条队列行。`state='guided'` 时不留任何动作按钮——已放行的插话只能等 agent 取走
 * （此时移除会让用户以为它丢了）；`stopped` 只留删除，让用户能清理冻结的残留。
 */
export function runGuidanceElement(message) {
  const row = document.createElement('div');
  const metadata = message.metadata || {};
  const status = interjectionState(message);
  row.className = 'run-guidance-card';
  if (status === 'guided') row.classList.add('is-guided');
  if (status === 'stopped') row.classList.add('is-stopped');
  row.dataset.messageId = message.id || '';
  row.dataset.interjectionState = status;
  row.dataset.rawContent = message.content || '';
  // 队列行必须自带所属 Run id：面板的寿命比 Run 长（run 结束后残留行还要能被取回/删除），
  // 此时 state.chatRunId 已经清空，再去问「当前活动 Run」只会得到空串。
  row.dataset.runId = String(metadata.run_id || '');
  const actions = status === 'pending'
    ? `<button type="button" data-interjection-edit title="编辑排队消息" aria-label="编辑排队消息">${ICON_EDIT}</button>`
      + `<button type="button" data-interjection-delete title="删除排队消息" aria-label="删除排队消息">${ICON_DELETE}</button>`
      + `<button type="button" data-interjection-guide title="立即引导当前任务（下一步即生效）" aria-label="立即引导当前任务">${ICON_GUIDE}</button>`
      + reuseButtonHtml(status)
    : (status === 'stopped'
      ? `<button type="button" data-interjection-delete title="删除这条已停止的插话" aria-label="删除这条已停止的插话">${ICON_DELETE}</button>`
        + reuseButtonHtml(status)
      : '');
  row.innerHTML = `<span class="run-guidance-icon" aria-hidden="true">≡</span>`
    + `<span class="run-guidance-badge">${INTERJECTION_STATE_TEXT[status] || ''}</span>`
    + `<span class="run-guidance-preview" title="${escapeHtml(message.content || '')}">${escapeHtml(message.content || '') || '（无正文）'}</span>`
    + interjectionAttachmentNote(metadata)
    + `<span class="run-guidance-actions">${actions}</span>`;
  return row;
}

function syncInterjectionPanelVisibility(container) {
  if (!container) return;
  container.hidden = !container.querySelector('.run-guidance-card');
}

/**
 * 面板抬头文案（唯一出口）：待引导条数变了就得跟着变——冻结后若还写着「N 条待引导」，
 * 用户会对着一个已经点不动的队列找「引导」按钮（实测截图里就是这么错的）。
 */
function refreshGuidanceHead(container, queued) {
  const head = container?.querySelector('.run-guidance-head');
  if (!head) return;
  const pending = queued.filter((message) => interjectionState(message) === 'pending').length;
  head.textContent = pending
    ? `插话队列 · ${pending} 条待引导 · ${INTERJECTION_GUIDE_HINT}`
    : `插话队列 · ${queued.length} 条（无可引导项）`;
}

/**
 * 任何一次**局部**变更（引导 / 删除 / 被 agent 取走）之后的统一收尾：缓存、抬头计数、
 * 整体显隐、引导可用性四处一起对齐。散着写必漏一处——出过的错就是「行已成已停止、
 * 抬头还写着 1 条待引导」。
 */
function settlePanel(container = $('#runGuidanceList')) {
  if (!container) return;
  queuedCache = queuedCache.filter(isQueuedInterjection);
  refreshGuidanceHead(container, queuedCache);
  syncInterjectionPanelVisibility(container);
  syncGuideAvailability(container);
}

/**
 * 用**完整会话消息数组**（含未消费插话）重建队列面板。
 *
 * 为什么不吃 `state.messages`：那份是被过滤过的可见列表（插话不在里面），面板需要的是
 * 原始数组——所以 04-messages.js 在过滤**之前**把原数组交给这里。
 *
 * 就地编辑期间不重建：会话同步轮询会周期性重渲染，重建会连输入框和已打的字一起吃掉
 * （同类事故见维护说明 §九.78 编辑框被重渲染拆掉）。
 */
export function renderRunGuidance(messages) {
  const container = $('#runGuidanceList');
  if (!container) return;
  const queued = (messages || []).filter(isQueuedInterjection);
  queuedCache = queued;
  if (editingInterjectionId) {
    const editingRow = interjectionRowFor(editingInterjectionId);
    if (editingRow && editingRow.querySelector('.run-guidance-input')) {
      // 仍在编辑：只同步整体显隐，不碰行内 DOM
      container.hidden = !queued.length;
      return;
    }
    editingInterjectionId = '';
  }
  container.replaceChildren();
  if (!queued.length) {
    container.hidden = true;
    return;
  }
  const head = document.createElement('div');
  head.className = 'run-guidance-head';
  container.append(head, ...queued.map(runGuidanceElement));
  container.hidden = false;
  refreshGuidanceHead(container, queued);
  syncGuideAvailability(container);
}
/**
 * 「引导」只在有活动 Run 时可点：本版没有自动 follow-up，run 一结束队列就不会再被消费，
 * 此时按钮点下去只会拿到 404。禁用而不是隐藏——用户看得见这条指令还在，也知道去哪取回。
 */
function syncGuideAvailability(container) {
  const live = Boolean(state.chatRunId || state.abortController);
  container.querySelectorAll('[data-interjection-guide]').forEach((button) => {
    button.disabled = !live;
    if (!live) button.title = '本轮已结束：无法再引导，可「取回输入框」或删除';
  });
}

/** 队列面板当前统计（done 事件的「还有 N 条未引导」提示用它，避免自己再数一遍 DOM）。 */
export function queuedInterjectionStats() {
  const container = $('#runGuidanceList');
  if (!container) return { total: 0, pending: 0 };
  // 顺手刷一次可用性：状态栏与队列同属「运行状态」的呈现，run 结束时两边必须一起更新，
  // 否则会出现「状态栏已就绪、引导按钮却还亮着」的错位。
  syncGuideAvailability(container);
  const rows = [...container.querySelectorAll('.run-guidance-card')];
  return {
    total: rows.length,
    pending: rows.filter((row) => row.dataset.interjectionState === 'pending').length,
  };
}

/**
 * Run 结束（完成/失败/取消）→ 队列就地冻结：所有残留行转「已停止」，只留删除与取回。
 *
 * 与后端的 `stop_pending_interjections` 是同一件事的两半：后端改的是库（保证刷新后仍一致），
 * 这里改的是当前 DOM（保证用户不用等下一次轮询就看见真相）。本版没有自动 follow-up，
 * run 一结束队列就没有消费者了——把按钮留着可点，只会让用户对着 404 反复点。
 */
export function freezeQueuedInterjections() {
  const container = $('#runGuidanceList');
  const stats = queuedInterjectionStats();
  if (!container || !stats.total) return stats;
  editingInterjectionId = '';
  const rows = [...container.querySelectorAll('.run-guidance-card')];
  for (const row of rows) if (row.dataset.runId) frozenRunIds.add(row.dataset.runId);
  queuedCache = queuedCache.map((message) => ({
    ...message,
    metadata: { ...(message.metadata || {}), interjection_stopped: true },
  }));
  for (const row of rows) {
    const message = queuedCache.find((item) => String(item.id || '') === row.dataset.messageId);
    if (message) renderInterjectionRowInPlace(row, message);
  }
  // 抬头必须跟着行一起变：冻结后还写着「N 条待引导」，用户会去找已经不存在的引导按钮。
  settlePanel(container);
  return stats;
}

/** 入队后立刻把新行挂上（不等下一次整体渲染），并把面板显示出来。 */
function appendRunGuidance(message) {
  const container = $('#runGuidanceList');
  if (!container) return;
  queuedCache = [...queuedCache, message];
  if (!container.querySelector('.run-guidance-head')) {
    const head = document.createElement('div');
    head.className = 'run-guidance-head';
    container.append(head);
  }
  container.append(runGuidanceElement(message));
  container.hidden = false;
  refreshGuidanceHead(container, queuedCache);
  syncGuideAvailability(container);
}

/** 当前会话的活动 Run id：state.chatRunId 可能在 run_started 之前还是空的，兜底问一次后端。 */
async function resolveActiveRunId(conversationId) {
  if (state.chatRunId) return String(state.chatRunId);
  try {
    const result = await api(`/api/runs?conversation_id=${encodeURIComponent(conversationId)}&active_only=1`);
    const run = (result.runs || []).find((item) => !String(item.parent_job_id || '').trim());
    return String(run?.id || '');
  } catch (_) {
    return '';
  }
}

/**
 * 运行中发送：把输入框（含待发送附件）的内容排进插话队列。
 *
 * 与 sendChatMessage 的差异：不调 /api/chat（那会与进行中的 Run 撞 ACTIVE_RUN），
 * 也不做 /技能 引用解析（插话没有 skill_policy 通道，原文照发，作者/模型都能看见）。
 * 请求被拒时把草稿与附件原样还给用户，不让一次网络抖动吞掉刚打的字。
 */
export async function sendRunInterjection(textOverride = '') {
  const input = $('#messageInput');
  const inputText = String(input?.value || '').trim();
  const buttonText = String(textOverride || '').trim();
  const text = buttonText
    ? (inputText ? `${inputText}\n${buttonText}` : buttonText)
    : inputText;
  const attachments = state.pendingFiles.map(({ name, path, size, thumb_path }) => ({ name, path, size, thumb_path }));
  if (!text && !attachments.length) {
    toast('请输入插话内容（或添加附件）后再加入队列');
    return null;
  }
  if (interjectionSubmitting) return null;
  const conversationId = String(state.conversationId || '');
  if (!conversationId) return null;
  const uploadingFile = state.pendingFiles.find((file) => file.uploading);
  if (uploadingFile) {
    toast(`请等待「${uploadingFile.name}」上传完成${uploadingFile.progress > 0 ? `（${uploadingFile.progress}%）` : ''}`);
    return null;
  }
  interjectionSubmitting = true;
  try {
    const runId = await resolveActiveRunId(conversationId);
    if (!runId) {
      toast('本轮已结束，直接发送即可（无需插话）');
      return null;
    }
    if (attachments.length) {
      const missing = await missingAttachmentPaths(attachments.map((item) => item.path));
      if (missing.length) {
        const missingSet = new Set(missing);
        const names = state.pendingFiles
          .filter((file) => missingSet.has(String(file.path || '')))
          .map((file) => file.name);
        state.pendingFiles = state.pendingFiles.filter((file) => !missingSet.has(String(file.path || '')));
        renderPendingFiles();
        toast(`附件文件已丢失（可能已被缓存清理）：${(names.length ? names : missing).join('、')}。已从待发送列表移除，请重新上传。`);
        return null;
      }
    }
    // 先清空输入区再发请求（与 sendChatMessage 同序）：连点不会把同一条内容排两次。
    state.pendingFiles = [];
    renderPendingFiles();
    if (input) input.value = '';
    resizeTextarea();
    renderInputMirror();
    hideSkillPopup();
    hideFilePopup();
    // 清空是**程序写**的，不会触发 input 事件 → 两个按钮（发送/插话）必须显式刷新一次。
    // 漏了这步，插话按钮会亮在一个空输入框上，再点一次只会得到「请输入插话内容」。
    updateContextComposerLock(state.chatBusy);
    let result;
    try {
      result = await api('/api/chat/interject', {
        method: 'POST',
        body: { conversation_id: conversationId, run_id: runId, message: text, attachments },
      });
    } catch (error) {
      // 回滚：草稿与附件放回用户手上，而不是让一次网络抖动吞掉刚打的内容
      if (input && !String(input.value || '').trim()) {
        input.value = text;
        resizeTextarea();
        renderInputMirror();
      }
      if (!state.pendingFiles.length && attachments.length) {
        state.pendingFiles = attachments.map((item) => ({ ...item }));
        renderPendingFiles();
      }
      updateContextComposerLock(state.chatBusy);   // 同上：程序写回也要刷按钮
      toast(`插话失败：${error.message}`);
      return null;
    }
    const message = result.message || {
      role: 'user',
      content: text,
      metadata: { attachments, interjection: true, interjection_guided: false },
    };
    appendRunGuidance(message);
    toast('已加入插话队列：可编辑 / 删除，或点「引导」立即并进当前任务');
    return message;
  } finally {
    interjectionSubmitting = false;
  }
}

/**
 * 取回：把插话正文与附件放回输入框、并把队列行移出（调 delete）。
 * 这是「run 结束后残留插话」的唯一可用出口（引导已不可用，见 syncGuideAvailability）。
 */
export async function reuseInterjection(messageId) {
  const id = String(messageId || '');
  const row = interjectionRowFor(id);
  if (!row) return false;
  const message = queuedCache.find((item) => String(item.id || '') === id);
  const content = String(message?.content ?? row.dataset.rawContent ?? '');
  const input = $('#messageInput');
  if (input) {
    input.value = String(input.value || '').trim() ? `${input.value}\n${content}` : content;
    resizeTextarea();
    renderInputMirror();
    input.focus();
    try { input.setSelectionRange(input.value.length, input.value.length); } catch (_) { /* 忽略 */ }
  }
  const attachments = Array.isArray(message?.metadata?.attachments) ? message.metadata.attachments : [];
  if (attachments.length) {
    const known = new Set(state.pendingFiles.map((file) => String(file.path || '')));
    state.pendingFiles = [
      ...state.pendingFiles,
      ...attachments.filter((item) => !known.has(String(item?.path || ''))).map((item) => ({ ...item })),
    ];
    renderPendingFiles();
  }
  updateContextComposerLock(state.chatBusy);   // 取回是「现在就能发」的动作，按钮必须立刻跟上
  const removed = id ? await deleteInterjection(id) : false;
  if (!removed) {
    toast('内容已放回输入框，但队列里那条没能删掉（可再点一次删除）');
    return false;
  }
  toast('已取回输入框：确认后点发送即可（队列里那条已移除）');
  return true;
}

/** 队列行所属 Run id（行自带的为准；`fallback` 只用于行还没渲染出来的极端时序）。 */
function rowRunId(row, fallback = '') {
  return String(row?.dataset.runId || fallback || state.chatRunId || '');
}

/** 引导一条插话：agent 下一步即取走（后端同时会撤掉该 Run 上待确认的工具卡）。 */
export async function guideInterjection(messageId) {
  const id = String(messageId || '');
  if (!id || !state.conversationId) return false;
  const row = interjectionRowFor(id);
  const runId = rowRunId(row);
  if (!runId) {
    toast('这条插话已不属于任何进行中的任务，无法引导（可「取回输入框」或删除）');
    return false;
  }
  const button = row?.querySelector('[data-interjection-guide]');
  if (button) button.disabled = true;
  try {
    await api('/api/chat/interject/guide', {
      method: 'POST',
      body: { conversation_id: state.conversationId, run_id: runId, message_id: id },
    });
    markInterjectionGuided(id);
    return true;
  } catch (error) {
    if (button) button.disabled = false;
    toast(`引导失败：${error.message}`);
    return false;
  }
}

/** 依次引导全部「待引导」项（队列顺序 = 落库顺序）。 */
export async function guideAllInterjections() {
  const container = $('#runGuidanceList');
  const ids = [...(container?.querySelectorAll('.run-guidance-card[data-interjection-state="pending"]') || [])]
    .map((row) => row.dataset.messageId)
    .filter(Boolean);
  if (!ids.length) {
    toast('队列里没有待引导的插话');
    return 0;
  }
  let guided = 0;
  for (const id of ids) {
    if (!await guideInterjection(id)) break;
    guided += 1;
  }
  return guided;
}

/** 把队列行切到「已引导」态（引导事件与本地乐观更新共用这一处）。 */
function markInterjectionGuided(messageId) {
  const id = String(messageId);
  const row = interjectionRowFor(id);
  // 缓存的 metadata 一起改：后续任何一次重渲染（含 freezeQueuedInterjections）都得看到
  // 同一份状态，否则「已引导」的行会在重渲染后弹回「待引导」。
  queuedCache = queuedCache.map((message) => (
    String(message.id || '') === id
      ? { ...message, metadata: { ...(message.metadata || {}), interjection_guided: true } }
      : message
  ));
  if (!row) return;
  editingInterjectionId = editingInterjectionId === id ? '' : editingInterjectionId;
  row.classList.add('is-guided');
  row.dataset.interjectionState = 'guided';
  row.querySelector('.run-guidance-input')?.remove();
  row.querySelector('.run-guidance-preview')?.removeAttribute('style');
  const badge = row.querySelector('.run-guidance-badge');
  if (badge) badge.textContent = INTERJECTION_STATE_TEXT.guided;
  row.querySelector('.run-guidance-actions')?.replaceChildren();
  settlePanel();
}

export async function deleteInterjection(messageId) {
  const id = String(messageId || '');
  if (!id || !state.conversationId) return false;
  const row = interjectionRowFor(id);
  const runId = rowRunId(row);
  if (!runId) {
    toast('这条插话已不属于任何任务，无法删除');
    return false;
  }
  try {
    await api('/api/chat/interject/delete', {
      method: 'POST',
      body: { conversation_id: state.conversationId, run_id: runId, message_id: id },
    });
  } catch (error) {
    toast(`删除失败：${error.message}`);
    return false;
  }
  if (editingInterjectionId === id) editingInterjectionId = '';
  queuedCache = queuedCache.filter((item) => String(item.id || '') !== id);
  row?.remove();
  settlePanel();
  return true;
}

/** 就地展开编辑框（不搬走底部输入区，也不进消息流）。 */
function startEditInterjection(row) {
  if (!row || row.querySelector('.run-guidance-input')) return;
  const messageId = row.dataset.messageId || '';
  if (!messageId) return;
  editingInterjectionId = messageId;
  const preview = row.querySelector('.run-guidance-preview');
  const textarea = document.createElement('textarea');
  textarea.className = 'run-guidance-input';
  textarea.rows = 2;
  textarea.value = row.dataset.rawContent || '';
  textarea.placeholder = '修改这条插话（Enter 保存，Esc 取消，Shift+Enter 换行）';
  if (preview) preview.replaceWith(textarea);
  else row.append(textarea);
  const actions = row.querySelector('.run-guidance-actions');
  if (actions) {
    const save = document.createElement('button');
    save.type = 'button';
    save.className = 'run-guidance-save';
    save.dataset.interjectionSave = 'true';
    save.textContent = '保存';
    save.title = '保存修改（不发送）';
    actions.replaceChildren(save);
  }
  textarea.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      editingInterjectionId = '';
      commitInterjectionEdit(messageId, null);
      return;
    }
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      commitInterjectionEdit(messageId, textarea.value);
    }
  });
  textarea.focus();
  const end = textarea.value.length;
  try { textarea.setSelectionRange(end, end); } catch (_) { /* 个别输入类型不支持选区 */ }
}

/**
 * 提交编辑：`next === null` 表示取消（只把行还原，不发请求）。
 * 保存成功后用返回的真消息重建这一行，避免前端自己拼出一份与库里不一致的显示。
 */
async function commitInterjectionEdit(messageId, next) {
  const id = String(messageId || '');
  const row = interjectionRowFor(id);
  if (!row) return;
  editingInterjectionId = '';
  const restore = () => renderInterjectionRowInPlace(row, {
    id,
    role: 'user',
    content: row.dataset.rawContent || '',
    metadata: { interjection: true, interjection_guided: false, run_id: row.dataset.runId || '' },
  });
  if (next === null) {
    restore();
    return;
  }
  const content = String(next || '').trim();
  if (!content) {
    toast('插话内容不能为空');
    startEditInterjection(row);
    return;
  }
  // 刻意不要求「Run 还活着」：run 结束后残留行仍可编辑（改完再「取回输入框」）。
  // 存储层的闸门是「已引导/已消费不给改」——引导过的行正被 agent 取用，就地改写会让
  // 模型看到的与界面显示的错位。
  const runId = rowRunId(row);
  if (!runId) {
    toast('这条插话已不属于任何任务，无法编辑');
    restore();
    return;
  }
  try {
    const result = await api('/api/chat/interject/edit', {
      method: 'POST',
      body: { conversation_id: state.conversationId, run_id: runId, message_id: id, message: content },
    });
    const message = result.message || { id, role: 'user', content, metadata: { interjection: true } };
    queuedCache = queuedCache.map((item) => (String(item.id || '') === id ? message : item));
    renderInterjectionRowInPlace(row, message);
  } catch (error) {
    toast(`编辑失败：${error.message}`);
    restore();
  }
}

function renderInterjectionRowInPlace(row, message) {
  const fresh = runGuidanceElement(message);
  row.replaceWith(fresh);
}

// ---- chat 事件接线（注册进 12-chat-input.js 的 CHAT_EVENT_HANDLERS）----

/** user_guidance：用户点了「引导」→ 该行切「已引导」，并撤掉该 Run 上待确认的工具卡。 */
export function handleUserGuidanceEvent(event, { row, setActivity } = {}) {
  markInterjectionGuided(String(event.message_id || ''));
  // 后端在引导时已 reject 掉待确认的工具调用：界面同步说明，别让用户对着死卡片点。
  row?.querySelectorAll('.tool-confirm').forEach((confirmation) => {
    const actions = confirmation.querySelector('.tool-confirm-actions');
    if (actions) actions.innerHTML = '<div class="tool-confirm-status">新指令已到达，原确认已撤销</div>';
  });
  if (typeof setActivity === 'function') setActivity('已收到引导，准备继续');
  return false;   // 不触发滚动：引导只改输入区上方的队列，与消息区位置无关
}

/** interjection_consumed：agent 已把该插话并进本轮消息 → 从队列面板摘掉。 */
export function handleInterjectionConsumedEvent(event) {
  const id = String(event.message_id || '');
  if (!id) return false;
  if (editingInterjectionId === id) editingInterjectionId = '';
  // 同时从缓存里摘掉：它已被消费，`isQueuedInterjection` 会为 false，缓存与 DOM 不能分家。
  queuedCache = queuedCache.filter((item) => String(item.id || '') !== id);
  interjectionRowFor(id)?.remove();
  settlePanel();
  return false;
}

// 队列面板的点击委托：整块挂在模块内（ESM 作用域函数不能经内联 onclick 访问）。
document.addEventListener('click', (event) => {
  const container = event.target.closest('#runGuidanceList');
  if (!container) return;
  const row = event.target.closest('.run-guidance-card');
  if (!row) return;
  if (event.target.closest('[data-interjection-guide]')) {
    event.preventDefault();
    void guideInterjection(row.dataset.messageId);
    return;
  }
  if (event.target.closest('[data-interjection-delete]')) {
    event.preventDefault();
    void deleteInterjection(row.dataset.messageId);
    return;
  }
  if (event.target.closest('[data-interjection-edit]')) {
    event.preventDefault();
    startEditInterjection(row);
    return;
  }
  if (event.target.closest('[data-interjection-reuse]')) {
    event.preventDefault();
    void reuseInterjection(row.dataset.messageId);
    return;
  }
  if (event.target.closest('[data-interjection-save]')) {
    event.preventDefault();
    const textarea = row.querySelector('.run-guidance-input');
    if (textarea) void commitInterjectionEdit(row.dataset.messageId, textarea.value);
  }
});
