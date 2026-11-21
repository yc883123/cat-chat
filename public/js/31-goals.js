// 主对话 GOAL：服务端状态独立于单轮 Run，视图只读取当前会话的目标。
import { $, api, escapeHtml, randomUuid, state, toast } from './01-core.js';

const STATUS = {
  queued: '排队中', running: '进行中', waiting_tool: '等待后台任务', pausing: '正在暂停', paused: '已暂停',
  needs_user: '需要你处理', blocked: '已停止', awaiting_review: '待验收',
  cancelling: '正在取消', cancelled: '已取消', completed: '已完成',
};
const ACTION_LABEL = { pause: '暂停', resume: '继续目标', cancel: '取消目标', accept: '确认完成' };
const ACTIONS = {
  queued: ['pause', 'cancel'], running: ['pause', 'cancel'], waiting_tool: ['pause', 'cancel'],
  paused: ['resume', 'cancel'], needs_user: ['resume', 'cancel'], blocked: ['cancel'],
  awaiting_review: ['accept', 'cancel'],
};
const STOP_CODES = {
  budget_model: '模型请求额度用尽', budget_tools: '工具动作额度用尽', budget_rounds: '续跑轮次额度用尽',
  deadline: '已到截止时间', no_progress: '多轮无确认进展', configuration: '执行配置已变化',
  source_changed: '引用计划已变化', unknown_write: '存在无法核对的写入', job_missing: '后台任务无法确认',
  job_failed: '后台任务失败', check_failed: '本地验收未通过', error: '本轮运行错误',
};
const POLICY_LABEL = { machine_all: '条件齐全时自动完成', mixed: '机器通过后仍需人工验收', manual: '交付后人工验收' };
const WAIT_KIND_LABEL = { job: '后台任务', goal_check: '本地验收检查' };
const ROUND_STATUS = {
  queued: '排队', running: '进行中', waiting_tool: '等待后台', pausing: '暂停中', paused: '已暂停',
  needs_user: '需处理', blocked: '已停止', awaiting_review: '待验收', cancelling: '取消中',
  cancelled: '已取消', completed: '已完成',
};
const snapshots = new Map();
const readErrors = new Map();
const pendingChecks = new Set();
const drafts = new Map();
const retries = new Map();
const extendTries = new Map();
const rounds = new Map();
let plan = null;
let planError = '';
const writes = new Set();
let viewId = '';
let draftMode = false;
let draftScoped = false;
let readTicket = null;
let timer;

const conversationId = () => String(state.conversationId || '');
const currentGoal = () => snapshots.get(conversationId());
const isActive = (goal) => Boolean(goal && !['cancelled', 'completed'].includes(goal.status));
const isCurrentView = (id) => id === viewId && id === conversationId();

export function goalDraftEnabled() {
  return draftMode && isCurrentView(viewId) && !state.editingMessageId;
}

export function goalIsActive(id = conversationId()) {
  return isActive(snapshots.get(String(id)));
}

export function goalBlocksChat(id = conversationId()) {
  id = String(id || '');
  return goalIsActive(id) || writes.has(id) || pendingChecks.has(id) || readErrors.has(id) || Boolean(id && !snapshots.has(id));
}

export function guardGoalChat() {
  if (!goalBlocksChat()) return false;
  toast(goalIsActive() ? '目标未结束，请先取消目标，或到其他会话继续聊天' : '正在确认目标状态，请稍后重试');
  return true;
}

export function goalCanPause() {
  return !writes.has(conversationId()) && Boolean(ACTIONS[currentGoal()?.status]?.includes('pause'));
}

function paint() {
  const card = $('#goalCard');
  const id = conversationId();
  const goal = currentGoal();
  const error = readErrors.get(id);
  if (card) {
    card.hidden = !goal && !error;
    const key = JSON.stringify([id, goal, error, writes.has(id), rounds.get(String(goal?.id || ''))]);
    if (card.dataset.snapshot !== key) {
      const detailsWasOpen = card.dataset.goalId === String(goal?.id) && card.querySelector('details')?.open;
      const focusedAction = card.contains(document.activeElement) && document.activeElement?.dataset.goalAction;
      // 轮询会重绘整卡：先把未提交的表单值收起来，渲染后原样放回。
      const formValues = {};
      if (typeof card.querySelectorAll === 'function') {
        card.querySelectorAll('[data-goal-field]').forEach((input) => { if (input.value) formValues[input.dataset.goalField] = input.value; });
      }
      card.dataset.snapshot = key;
      card.dataset.goalId = goal?.id || '';
      if (goal) {
        const deadline = goal.deadline_at ? new Date(goal.deadline_at) : null;
        const created = goal.created_at ? new Date(goal.created_at) : null;
        const dateText = date => date && !Number.isNaN(date.getTime()) ? date.toLocaleString() : '';
        const evidence = Array.isArray(goal.evidence)
          ? goal.evidence.map(item => `${item.passed ? '通过' : '未通过'}：${item.path || item.check_id || '目标文件'}${item.sha256 ? `\nSHA-256：${item.sha256}` : ''}`).join('\n') : '';
        const limits = goal.limits || {};
        const used = (keyName) => goal[keyName] != null
          ? (limits[keyName] ? `${goal[keyName]}/${limits[keyName]}` : `${goal[keyName]}`) : '';
        const counts = [
          goal.generation != null ? `第 ${goal.generation}${limits.rounds ? `/${limits.rounds}` : ''} 轮` : '',
          goal.model_requests != null ? `模型请求 ${used('model_requests')} 次` : '',
          goal.tool_actions != null ? `工具动作 ${used('tool_actions')} 次` : '',
        ].filter(Boolean).join(' · ');
        const criteriaTotal = Array.isArray(goal.criteria) ? goal.criteria.length : 0;
        const passedCount = Array.isArray(goal.evidence) ? goal.evidence.filter(item => item.passed).length : 0;
        const waitBinding = goal.wait_binding || {};
        const waitLine = waitBinding.target_id
          ? `等待中：${WAIT_KIND_LABEL[waitBinding.kind] || waitBinding.kind} ${String(waitBinding.target_id).slice(0, 12)}…` : '';
        const sources = Array.isArray(goal.sources) ? goal.sources : [];
        const stages = Array.isArray(goal.stages) ? goal.stages : [];
        const manualItems = Array.isArray(goal.manual_items) ? goal.manual_items : [];
        const extendable = !['cancelled', 'completed', 'cancelling'].includes(goal.status);
        card.innerHTML = `<header><strong>GOAL</strong><span role="status">${STATUS[goal.status] || '状态未知'}</span></header>
          <div class="goal-card-message">${escapeHtml(goal.message || '')}</div>
          ${goal.reason ? `<p class="goal-card-reason">${escapeHtml(goal.reason)}</p>` : ''}
          <div class="goal-card-actions">${(ACTIONS[goal.status] || []).map(action =>
            `<button class="control-button" type="button" data-goal-action="${action}" ${writes.has(id) ? 'disabled' : ''}>${ACTION_LABEL[action]}</button>`).join('')}</div>
          <details${detailsWasOpen ? ' open' : ''}><summary>详情</summary>
            ${counts ? `<p>${escapeHtml(counts)}</p>` : ''}
            ${criteriaTotal ? `<p>已验收 ${passedCount}/${criteriaTotal} 项${manualItems.length ? `，另有 ${manualItems.length} 项人工核对` : ''}</p>` : ''}
            ${goal.stop_code && STOP_CODES[goal.stop_code] ? `<p>停止原因：${escapeHtml(STOP_CODES[goal.stop_code])}</p>` : ''}
            ${POLICY_LABEL[goal.completion_policy] ? `<p>验收方式：${escapeHtml(POLICY_LABEL[goal.completion_policy])}</p>` : ''}
            ${dateText(created) ? `<p>创建时间：${escapeHtml(dateText(created))}</p>` : ''}
            ${dateText(deadline) ? `<p>截止时间：${escapeHtml(dateText(deadline))}</p>` : ''}
            ${goal.run_id ? `<p>Run：${escapeHtml(goal.run_id)}</p>` : ''}
            ${waitLine ? `<p>${escapeHtml(waitLine)}</p>` : ''}
            ${sources.length ? `<p>引用计划：${sources.map(item => escapeHtml(item.name || item.path || '')).join('、')}</p>` : ''}
            ${stages.length ? `<p>阶段：${stages.map(item => escapeHtml(item.title || item.id || '')).filter(Boolean).join(' → ')}</p>` : ''}
            ${evidence ? `<pre>${escapeHtml(evidence)}</pre>` : ''}
            ${goalRoundSection(goal)}
            ${extendable ? goalExtendForm(id) : ''}
          </details>`;
        for (const [name, value] of Object.entries(formValues)) {
          const input = card.querySelector(`[data-goal-field="${name}"]`);
          if (input) input.value = value;
        }
      } else {
        card.replaceChildren();
      }
      if (error) card.insertAdjacentHTML('beforeend', `<p class="goal-card-reason" role="status">目标状态读取失败：${escapeHtml(error)}</p><button class="control-button" type="button" data-goal-action="refresh">重试</button>`);
      if (focusedAction) card.querySelector(`[data-goal-action="${focusedAction}"]`)?.focus({ preventScroll: true });
    }
  }
  document.dispatchEvent(new CustomEvent('naiba:goal-changed'));
}

function goalRoundSection(goal) {
  const state = rounds.get(String(goal.id || ''));
  if (!state) return '<p><button class="control-button" type="button" data-goal-action="rounds-more">查看轮次记录</button></p>';
  const rows = state.items.map(item => {
    const usage = [
      item.model_requests_start != null && item.model_requests_end != null
        ? `模型请求 +${item.model_requests_end - item.model_requests_start}` : '',
      item.tool_actions_start != null && item.tool_actions_end != null
        ? `工具动作 +${item.tool_actions_end - item.tool_actions_start}` : '',
    ].filter(Boolean).join(' · ');
    const started = item.created_at ? new Date(item.created_at).toLocaleString() : '';
    const ended = item.finished_at ? new Date(item.finished_at).toLocaleString() : '';
    return `<li>第 ${item.generation} 轮 · ${escapeHtml(ROUND_STATUS[item.status] || item.status || '')}`
      + `${started ? ` · ${escapeHtml(started)}` : ''}${ended ? ` → ${escapeHtml(ended)}` : ''}`
      + `${usage ? ` · ${escapeHtml(usage)}` : ''}`
      + `${item.reason ? `<br><span class="goal-round-reason">${escapeHtml(String(item.reason).slice(0, 200))}</span>` : ''}</li>`;
  }).join('');
  return `<div class="goal-rounds"><p>轮次记录（新 → 旧）</p><ul>${rows}</ul>`
    + (state.error ? `<p class="goal-card-reason">轮次读取失败：${escapeHtml(state.error)}</p>` : '')
    + (state.has_more ? '<button class="control-button" type="button" data-goal-action="rounds-more">更多轮次</button>' : '')
    + '</div>';
}

function goalExtendForm(id) {
  const field = (name, label) => `<div class="goal-extend-field"><span>${label}</span>`
    + `<input type="number" min="1" step="1" inputmode="numeric" data-goal-field="${name}" aria-label="${label}"></div>`;
  return `<div class="goal-extend">
    <p>追加额度与时限（只增不减；总时限不超过创建后 24 小时）</p>
    <div class="goal-extend-fields">
      ${field('extend-model', '模型请求 +')}
      ${field('extend-tools', '工具动作 +')}
      ${field('extend-rounds', '续跑轮次 +')}
      ${field('extend-minutes', '时限 +分钟')}
    </div>
    <button class="control-button" type="button" data-goal-action="extend-submit" ${writes.has(id) ? 'disabled' : ''}>追加额度或时限</button>
  </div>`;
}

export function setGoalDraftMode(enabled) {
  if (goalBlocksChat() || (enabled && (state.chatBusy || state.chatRunId || state.abortController || state.editingMessageId))) return false;
  draftMode = Boolean(enabled);
  draftScoped ||= draftMode;
  if (!draftMode) clearGoalPlan();
  paint();
  paintPlanSummary();
  return true;
}

export function goalPlan() {
  return plan;
}

export function clearGoalPlan() {
  plan = null;
  planError = '';
  paintPlanSummary();
}

export async function previewGoalPlan() {
  const id = conversationId();
  if (!goalDraftEnabled() || writes.has(id)) return false;
  const sources = state.pendingFiles.filter(file => file.path && !file.folder)
    .map(file => ({ path: file.path, name: file.name }));
  if (!sources.length) { toast('请先添加计划文件附件，再解析计划'); return false; }
  const message = String($('#messageInput')?.value || '');
  beginGoalWrite(id);
  try {
    const result = await api('/api/goals/plan-preview', {
      method: 'POST', body: { conversation_id: id, message, sources }, signal: AbortSignal.timeout(20000),
    });
    if (!isCurrentView(id)) return false;
    plan = result.plan || null;
    planError = plan ? '' : '服务端未返回计划预览';
  } catch (error) {
    if (isCurrentView(id)) { plan = null; planError = error.message; }
  } finally {
    finishGoalWrite(id);
  }
  paintPlanSummary();
  return true;
}

function paintPlanSummary() {
  const host = $('#goalPlanSummary');
  if (!host) return;
  host.hidden = !plan && !planError;
  if (plan) {
    const stages = Array.isArray(plan.stages) ? plan.stages : [];
    const manual = Array.isArray(plan.manual_items) ? plan.manual_items : [];
    const criteriaCount = Array.isArray(plan.criteria) ? plan.criteria.length : 0;
    host.innerHTML = `<strong>计划预览</strong>
      <span>${(plan.sources || []).map(item => escapeHtml(item.name || item.path || '')).filter(Boolean).join('、')}</span>
      <span>机器条件 ${criteriaCount} 项 · 人工核对 ${manual.length} 项 · ${POLICY_LABEL[plan.completion_policy] || '验收方式未定'}</span>
      ${stages.length ? `<span>阶段：${stages.map(item => escapeHtml(item.title || '')).filter(Boolean).join(' → ')}</span>` : ''}
      <button type="button" data-plan-action="clear">移除计划</button>`;
  } else if (planError) {
    host.innerHTML = `<span class="goal-card-reason">计划解析失败：${escapeHtml(planError)}</span>`;
  }
}

export function syncGoalConversation({ created = false } = {}) {
  const id = conversationId();
  let restored = false;
  if (viewId !== id) {
    const previous = viewId;
    viewId = id;
    if (id) pendingChecks.add(id);
    readTicket = null;
    clearGoalPlan();
    // 「发送」或粘贴在空白页隐式建会话：这是同一份草稿第一次取得会话 ID。
    if (previous || !created) {
      const input = $('#messageInput');
      const incoming = drafts.get(id);
      if (input && (draftScoped || incoming)) {
        drafts.set(previous, { mode: draftMode, scoped: draftScoped, text: input.value, files: state.pendingFiles });
        drafts.delete(id);
        input.value = incoming?.text || '';
        state.pendingFiles = incoming?.files || [];
        restored = true;
      }
      draftScoped = Boolean(incoming?.scoped);
      draftMode = Boolean(incoming?.mode);
    }
  }
  if (created) { snapshots.set(id, null); pendingChecks.delete(id); }
  paint();
  void refreshGoal();
  return restored;
}

function storeGoal(id, goal) {
  if (goal === null) { snapshots.set(id, null); return true; }
  if (!goal?.id || String(goal.conversation_id) !== id || !Object.hasOwn(STATUS, goal.status)
      || !Number.isSafeInteger(goal.version)) return false;
  const previous = snapshots.get(id);
  if (previous?.id === goal.id && goal.version < previous.version) return false;
  snapshots.set(id, goal);
  return true;
}

function scheduleGoalPoll() {
  clearTimeout(timer);
  if (!viewId || !isCurrentView(viewId) || document.visibilityState === 'hidden') return;
  timer = setTimeout(refreshGoal, goalIsActive() || readErrors.has(viewId) ? 1800 : 10000);
}

export async function refreshGoal() {
  clearTimeout(timer);
  const id = viewId;
  if (!id || !isCurrentView(id)) { paint(); return; }
  if (document.visibilityState === 'hidden' || writes.has(id) || readTicket || state.appView && state.appView !== 'chat') return;
  const ticket = {};
  readTicket = ticket;
  paint();
  try {
    const result = await api(`/api/goals?conversation_id=${encodeURIComponent(id)}`, { signal: AbortSignal.timeout(8000) });
    if (readTicket !== ticket || !isCurrentView(id)) return;
    if (!storeGoal(id, result.goal)) {
      // 同一个目标的旧版本可以忽略，非法/串会话响应不能解除输入锁。
      if (!(result.goal?.id === snapshots.get(id)?.id && result.goal?.version < snapshots.get(id)?.version)) throw new Error('服务端未返回有效目标状态');
    }
    pendingChecks.delete(id);
    readErrors.delete(id);
  } catch (error) {
    if (readTicket === ticket && isCurrentView(id)) {
      // 旧版本没有该接口时不改变普通聊天；网络故障不能冒充「没有目标」。
      if (error.status === 404 && !isActive(snapshots.get(id))) {
        snapshots.set(id, null);
        pendingChecks.delete(id);
        readErrors.delete(id);
      } else readErrors.set(id, error.message);
    }
  } finally {
    if (readTicket === ticket) {
      readTicket = null;
      paint();
      scheduleGoalPoll();
    }
  }
}

function beginGoalWrite(id) {
  writes.add(id);
  if (isCurrentView(id)) {
    readTicket = null; // 写入前的读取失效，不能覆盖写入结果。
    paint();
  }
}

function finishGoalWrite(id) {
  writes.delete(id);
  if (!isCurrentView(id)) return;
  paint();
  scheduleGoalPoll();
}

export async function submitGoal(payload) {
  const id = String(payload.conversation_id || '');
  if (!id || !String(payload.message || '').trim() || !isCurrentView(id) || !goalDraftEnabled() || goalBlocksChat()) return false;
  const input = $('#messageInput');
  const text = input.value;
  const files = state.pendingFiles;
  const sentFiles = new Set(files);
  const fingerprint = JSON.stringify(payload);
  const prior = retries.get(id);
  const requestId = prior?.fingerprint === fingerprint ? prior.requestId : randomUuid();
  retries.set(id, { fingerprint, requestId });
  beginGoalWrite(id);
  try {
    const { goal } = await api('/api/goals', {
      method: 'POST', body: { ...payload, request_id: requestId }, signal: AbortSignal.timeout(20000),
    });
    if (!goal || !storeGoal(id, goal)) throw new Error('服务端未返回有效目标');
    pendingChecks.delete(id);
    readErrors.delete(id);
    retries.delete(id);
    if (isCurrentView(id)) {
      if (input.value === text) input.value = '';
      state.pendingFiles = state.pendingFiles.filter(file => !sentFiles.has(file));
      draftMode = false;
      draftScoped = false;
      clearGoalPlan();
    } else {
      const saved = drafts.get(id);
      if (saved?.files === files) {
        if (saved.text === text) saved.text = '';
        saved.files = saved.files.filter(file => !sentFiles.has(file));
        saved.mode = saved.scoped = false;
        // 留空槽位仍标明草稿归属；切回时要隔离另一会话尚未发送的文字。
      }
    }
    return true;
  } catch (error) {
    if (isCurrentView(id)) toast(`创建目标失败：${error.message}`);
    return false;
  } finally {
    finishGoalWrite(id);
  }
}

export async function changeGoal(action) {
  const goal = currentGoal();
  const id = conversationId();
  if (!isCurrentView(id) || writes.has(id) || !ACTIONS[goal?.status]?.includes(action)) return false;
  if (action === 'cancel' && !confirm('取消目标？已经产出的文件和记录会保留。')) return false;
  beginGoalWrite(id);
  try {
    const result = await api(`/api/goals/${encodeURIComponent(goal.id)}/${action}`, {
      method: 'POST', body: { version: goal.version }, signal: AbortSignal.timeout(20000),
    });
    if (!result.goal || !storeGoal(id, result.goal)) throw new Error('服务端未返回有效目标状态');
    pendingChecks.delete(id);
    readErrors.delete(id);
    return true;
  } catch (error) {
    if (isCurrentView(id)) toast(`${ACTION_LABEL[action]}失败：${error.message}`);
    return false;
  } finally {
    finishGoalWrite(id);
    if (isCurrentView(id)) void refreshGoal();
  }
}

export async function loadGoalRounds() {
  const goal = currentGoal();
  const id = String(goal?.id || '');
  if (!id || writes.has(conversationId())) return false;
  const state = rounds.get(id) || { items: [], cursor: '', has_more: true, error: '' };
  rounds.set(id, state);
  try {
    const params = new URLSearchParams({ page_size: '10' });
    if (state.cursor) params.set('cursor', state.cursor);
    const result = await api(`/api/goals/${encodeURIComponent(id)}/rounds?${params}`, { signal: AbortSignal.timeout(8000) });
    if (!isCurrentView(conversationId()) || currentGoal()?.id !== id) return false;
    state.items = state.items.concat(Array.isArray(result.rounds) ? result.rounds : []);
    state.cursor = String(result.next_cursor || '');
    state.has_more = Boolean(result.has_more);
    state.error = '';
  } catch (error) {
    state.error = error.message;
  }
  paint();
  return true;
}

export async function submitGoalExtend() {
  const goal = currentGoal();
  const id = conversationId();
  if (!goal || writes.has(id) || ['cancelled', 'completed', 'cancelling'].includes(goal.status)) return false;
  const read = (name) => String(cardInputValue(name) || '').trim();
  const increments = {};
  for (const [fieldName, key] of [['extend-model', 'model_requests'], ['extend-tools', 'tool_actions'], ['extend-rounds', 'rounds']]) {
    const raw = read(fieldName);
    if (!raw) continue;
    const parsed = Number(raw);
    if (!Number.isInteger(parsed) || parsed < 1) { toast('追加额度必须是正整数'); return false; }
    increments[key] = parsed;
  }
  const minutesRaw = read('extend-minutes');
  const extraMinutes = minutesRaw ? Number(minutesRaw) : 0;
  if (minutesRaw && (!Number.isInteger(extraMinutes) || extraMinutes < 1)) { toast('追加时限必须是正整数分钟'); return false; }
  if (!Object.keys(increments).length && !extraMinutes) { toast('请填写要追加的额度或时限'); return false; }
  const fingerprint = JSON.stringify([goal.id, goal.version, increments, extraMinutes]);
  const prior = extendTries.get(goal.id);
  const requestId = prior?.fingerprint === fingerprint ? prior.requestId : randomUuid();
  extendTries.set(goal.id, { fingerprint, requestId });
  beginGoalWrite(id);
  try {
    const result = await api(`/api/goals/${encodeURIComponent(goal.id)}/extend`, {
      method: 'POST', body: { version: goal.version, request_id: requestId, increments, extra_minutes: extraMinutes },
      signal: AbortSignal.timeout(20000),
    });
    if (!result.goal || !storeGoal(id, result.goal)) throw new Error('服务端未返回有效目标状态');
    extendTries.delete(goal.id);
    return true;
  } catch (error) {
    if (isCurrentView(id)) toast(`追加额度失败：${error.message}`);
    return false;
  } finally {
    finishGoalWrite(id);
    if (isCurrentView(id)) void refreshGoal();
  }
}

function cardInputValue(name) {
  const input = $('#goalCard')?.querySelector(`[data-goal-field="${name}"]`);
  return input ? input.value : '';
}
