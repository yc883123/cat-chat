// ============================================================
// 06-tasks-plans.js —— 拆分自 public/app.js 第 1606-1965 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, $$, api, escapeHtml, state, toast } from "./01-core.js";
import { markdown } from "./02-markdown.js";
import { fileUrl } from "./03-media.js";
import { renderRunTasks, syncCurrentConversation } from "./08-conversations.js";
import { RUN_RECONNECT_COOLDOWN, clearElapsedStatus, resumeRun, setConnectionState, stopRunWatchdog } from "./11-run-stream.js";
import { cancelCurrentRun, closePermissionModeMenu, setBusy } from "./12-chat-input.js";
export const activeTaskStatuses = new Set(['queued', 'running', 'waiting', 'cancelling']);

export async function loadTasks() {
  if (state.taskPollInFlight || document.visibilityState === 'hidden') return;
  state.taskPollInFlight = true;
  try {
    const result = await api('/api/tasks');
    const previous = new Map(state.tasks.map((task) => [task.id, task.status]));
    state.tasks = result.tasks || [];
    renderRunTasks();
    for (const task of state.tasks) {
      if (previous.has(task.id) && previous.get(task.id) !== task.status && ['completed', 'failed', 'cancelled'].includes(task.status)) {
        if (task.status === 'completed') toast(`${task.agent_name} 的任务已完成`);
        if (task.conversation_id === state.conversationId) syncCurrentConversation();
      }
    }
    await maybeRecoverRunFromPoll();
  } catch (error) {
    console.debug('[naiba] 任务同步失败:', error.message);
  } finally {
    state.taskPollInFlight = false;
  }
}

// 轮询兜底恢复流：仅在处于"等待恢复/重连冷却已过"且前端无活动流时，
// 探测后端是否仍有当前对话的活跃 Run，若有则 resumeRun 拉回流。
// 只会在 runReconnectAt（>0 表示要恢复）且冷却已过时查询，避免每个轮询周期都打 /api/runs。
export async function maybeRecoverRunFromPoll() {
  if (state.cancelRequested) return;
  if (state.abortController) return; // 已有活动流，无需兜底
  if (state.runRecovering) return;
  if (!state.checkRunEligible) return; // 未进入"等恢复"状态就不查询
  if (!state.conversationId) return;
  if (state.runReconnectAt && Date.now() < state.runReconnectAt) return; // 冷却中
  state.runRecovering = true;
  const conversationId = state.conversationId;
  try {
    const result = await api(`/api/runs?conversation_id=${encodeURIComponent(conversationId)}&active_only=1`);
    if (state.conversationId !== conversationId || state.abortController) return;
    // 只认顶层对话 Run（与 /api/runs 的口径一致）：后台子 Job 不占用会话运行位。
    const run = (result.runs || []).find(
      (item) => !String(item.parent_job_id || '').trim()
        && ['chat', 'plan_execute'].includes(String(item.kind || '')),
    );
    if (run && run.id) {
      console.warn('[naiba] 轮询兜底：检测到活跃 Run，恢复流 run=', run.id);
      state.checkRunEligible = false;
      await resumeRun(run);
    }
    // 后端已无活跃 Run：说明任务其实已完成，静默回到就绪，避免无限查询。
    else {
      state.checkRunEligible = false;
      setConnectionState('connected');
      if (state.chatRunId) {
        state.chatRunId = '';
        state.runConversationId = '';
        state.runSequence = 0;
        stopRunWatchdog();
        clearElapsedStatus();
        setBusy(false);
      }
    }
  } catch (error) {
    // 服务仍不可达：保持等待，冷却窗口由 enterReconnectCoolDown 控制
    console.debug('[naiba] 轮询兜底恢复流失败:', error.message);
    if (!state.runReconnectAt) {
      state.runReconnectAt = Date.now() + RUN_RECONNECT_COOLDOWN;
    }
  } finally {
    state.runRecovering = false;
  }
}

export function startTaskSync() {
  if (state.taskPolling) return;
  state.taskPolling = true;
  scheduleTaskSync(1500);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      if (state.taskTimer) window.clearTimeout(state.taskTimer);
      state.taskTimer = null;
    } else {
      scheduleTaskSync(0);
    }
  });
}

export function scheduleTaskSync(delay = null) {
  if (!state.taskPolling || document.visibilityState === 'hidden') return;
  if (state.taskTimer) window.clearTimeout(state.taskTimer);
  const active = state.checkRunEligible || state.tasks.some((task) => activeTaskStatuses.has(task.status));
  const interval = active ? 1500 : 10000;
  state.taskTimer = window.setTimeout(async () => {
    state.taskTimer = null;
    if (document.visibilityState === 'visible') await loadTasks();
    scheduleTaskSync();
  }, delay ?? interval);
}

export function taskStatusLabel(status) {
  return ({ queued: '排队中', running: '运行中', waiting: '等待确认', cancelling: '取消中', completed: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断' })[status] || status;
}

// 审批模式上拉框：收起态只有触发按钮（显示当前档），点开才在按钮上方弹出列表。
export const PERMISSION_MODE_LABELS = { confirm: '确认', auto: '自动', full: '完全' };
const PERMISSION_MODE_TITLES = { confirm: '敏感操作逐次确认', auto: '工作区内自动放行，越界仍确认', full: '不再询问，可访问工作区外' };

export function currentPermissionMode() {
  const conversation = state.conversations.find((item) => item.id === state.conversationId);
  const mode = conversation?.permission_mode || 'auto';
  return ['confirm', 'auto', 'full'].includes(mode) ? mode : 'auto';
}

export function renderPermissionModeSwitch() {
  const mode = currentPermissionMode();
  const enabled = Boolean(state.conversationId);
  // 选项按钮挂在菜单里，而菜单打开时会被移到 body 下（fixed 定位，避免被 composer 的
  // overflow 裁剪）。因此这里必须按菜单 id 查找：用 #permissionModeSwitch 的后代选择器，
  // 菜单一旦挂到 body 就再也选不到按钮，高亮与禁用态会一直停在上一轮的旧值。
  $$('#permissionModeMenu [data-permission-mode]').forEach((button) => {
    const active = button.dataset.permissionMode === mode;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-selected', String(active));
    button.disabled = !enabled;
  });
  // 触发按钮同步当前档：文字、风险配色（data-mode）、悬停提示、禁用态。
  const trigger = $('#permissionModeButton');
  if (trigger) {
    trigger.disabled = !enabled;
    trigger.dataset.mode = mode;
    trigger.title = `当前对话审批模式：${PERMISSION_MODE_TITLES[mode] || ''}`;
  }
  const value = $('#permissionModeValue');
  if (value) value.textContent = PERMISSION_MODE_LABELS[mode] || '';
}

export async function switchPermissionMode(mode) {
  // 上拉框点选即收起：无论后续成败，浮层都不留在屏幕上。
  closePermissionModeMenu();
  if (!state.conversationId || !['confirm', 'auto', 'full'].includes(mode) || mode === currentPermissionMode()) return;
  if (mode === 'full' && !confirm('完全访问会允许此对话的 Agent 无需逐次确认即可操作本机文件、命令、网络和 MCP。确认启用？')) {
    renderPermissionModeSwitch();
    return;
  }
  const conversationId = state.conversationId;
  try {
    const updated = await api(`/api/conversations/${conversationId}/settings`, {
      method: 'POST',
      body: { permission_mode: mode },
    });
    if (state.conversationId !== conversationId) return;
    const index = state.conversations.findIndex((item) => item.id === conversationId);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...updated };
    renderPermissionModeSwitch();
    const names = { confirm: '请求批准', auto: '替我审批', full: '完全访问' };
    toast(`当前对话已切换为“${names[mode]}”`);
  } catch (error) {
    renderPermissionModeSwitch();
    toast(`切换审批模式失败：${error.message}`);
  }
}

// ---- 计划（Plan 模式） ----

export function planStatusLabel(status) {
  return ({ prepare: '准备中', ready: '待确认', building: '执行中', finished: '已完成', failed: '执行失败', cancelled: '已取消' })[status] || status;
}

export async function loadPlans() {
  const requestSeq = ++state.planLoadSeq;
  const conversationId = state.conversationId;
  if (!state.conversationId) {
    state.plans = [];
    renderPlanBar();
    return;
  }
  try {
    const result = await api(`/api/plans?conversation_id=${encodeURIComponent(conversationId)}`);
    // Timers and run completion may overlap; ignore responses for an older state.
    if (requestSeq !== state.planLoadSeq || conversationId !== state.conversationId) return;
    state.plans = result.plans || [];
  } catch (error) {
    if (requestSeq !== state.planLoadSeq || conversationId !== state.conversationId) return;
    console.debug('[naiba] 计划同步失败:', error.message);
  }
  renderPlanBar();
  fillPlanCards();
}

export function activePlan() {
  // Older plans are history and must not restore actions after the newest plan ends.
  const plan = state.plans[0];
  return plan && ['prepare', 'ready', 'building', 'failed', 'cancelled'].includes(plan.status)
    ? plan
    : null;
}

export function renderPlanBar() {
  const bar = $('#planBar');
  if (!bar) return;
  const plan = activePlan();
  if (!plan || !state.conversationId || currentInteractionMode() !== 'plan') {
    bar.hidden = true;
    bar.innerHTML = '';
    return;
  }
  bar.hidden = false;
  const steps = Array.isArray(plan.steps) ? plan.steps : [];
  const done = steps.filter((step) => step.status === 'done').length;
  const planId = escapeHtml(plan.id);
  let text = '';
  let actions = '';
  if (plan.status === 'prepare') {
    text = '计划准备中：正在澄清需求或生成方案，请直接回复我的问题';
    actions = `<button type="button" data-plan-action="cancel" data-plan-id="${planId}">取消计划</button>`;
  } else if (plan.status === 'ready') {
    text = `方案已就绪：《${escapeHtml(plan.title || '实施计划')}》（${steps.length} 步），请审阅`;
    actions = `<button type="button" data-plan-action="keep-planning" data-plan-id="${planId}">Keep planning</button>`
      + `<button type="button" data-plan-action="edit" data-plan-id="${planId}">编辑计划</button>`
      + `<button type="button" class="plan-primary" data-plan-action="execute" data-plan-id="${planId}">Approve</button>`;
  } else if (plan.status === 'building') {
    const current = steps.find((step) => step.status === 'running');
    text = `正在执行《${escapeHtml(plan.title || '实施计划')}》 ${done}/${steps.length} 步`
      + (current ? `：${escapeHtml(current.title)}` : '')
      + (plan.detail?.message ? ` · ${escapeHtml(plan.detail.message)}` : '');
    if (plan.detail?.confirm_id) {
      const confirmId = escapeHtml(plan.detail.confirm_id);
      actions = `<button type="button" data-plan-action="reject" data-confirm-id="${confirmId}">拒绝</button>`
        + `<button type="button" class="plan-primary" data-plan-action="confirm" data-confirm-id="${confirmId}">确认执行</button>`;
    }
    actions += `<button type="button" data-plan-action="cancel" data-plan-id="${planId}">取消</button>`;
  } else if (plan.status === 'failed') {
    text = `《${escapeHtml(plan.title || '实施计划')}》执行失败（${done}/${steps.length} 步已完成）：${escapeHtml(plan.error || '未知错误')}`;
    actions = `<button type="button" data-plan-action="edit" data-plan-id="${planId}">编辑计划</button>`
      + `<button type="button" class="plan-primary" data-plan-action="execute" data-plan-id="${planId}">继续执行</button>`
      + `<button type="button" data-plan-action="cancel" data-plan-id="${planId}">取消</button>`;
  } else if (plan.status === 'cancelled') {
    text = `计划已取消：《${escapeHtml(plan.title || '实施计划')}》（${done}/${steps.length} 步已完成）`;
    actions = `<button type="button" data-plan-action="edit" data-plan-id="${planId}">编辑计划</button>`
      + `<button type="button" class="plan-primary" data-plan-action="execute" data-plan-id="${planId}">继续执行</button>`;
  }
  bar.innerHTML = `<span class="plan-bar-status plan-status-${escapeHtml(plan.status)}">${planStatusLabel(plan.status)}</span>`
    + `<span class="plan-bar-text">${text}</span>`
    + `<span class="plan-bar-actions">${actions}</span>`;
}

export function fillPlanCards() {
  $$('[data-plan-card]').forEach((slot) => {
    const plan = state.plans.find((item) => item.id === slot.dataset.planCard);
    slot.innerHTML = plan ? planCardMarkup(plan) : '';
  });
}

export function planCardMarkup(plan) {
  const steps = Array.isArray(plan.steps) ? plan.steps : [];
  const done = steps.filter((step) => step.status === 'done').length;
  const stepIcons = { pending: '○', running: '◌', done: '✓', failed: '✗' };
  const stepsHtml = steps.length
    ? `<ol class="plan-steps">${steps.map((step) => `
      <li class="plan-step plan-step-${escapeHtml(step.status)}">
        <span class="plan-step-icon">${stepIcons[step.status] || '○'}</span>
        <span class="plan-step-title">${escapeHtml(step.title)}</span>
        ${step.summary ? `<span class="plan-step-summary">${escapeHtml(step.summary)}</span>` : ''}
      </li>`).join('')}</ol>`
    : '';
  const contentHtml = plan.content
    ? `<details class="plan-content"><summary>方案详情</summary><div class="plan-content-body">${markdown(plan.content)}</div></details>`
    : '';
  const archive = plan.archive_path
    ? `<a class="plan-archive" href="${fileUrl(plan.archive_path)}" target="_blank" rel="noreferrer">归档</a>`
    : '';
  return `<div class="plan-card-inner">
    <div class="plan-card-head">
      <span class="plan-card-badge plan-status-${escapeHtml(plan.status)}">${planStatusLabel(plan.status)}</span>
      <b class="plan-card-title">${escapeHtml(plan.title || '实施计划')}</b>
      ${steps.length ? `<span class="plan-card-progress">${done}/${steps.length}</span>` : ''}
      ${archive}
    </div>
    ${stepsHtml}
    ${contentHtml}
    ${plan.error ? `<div class="plan-card-error">${escapeHtml(plan.error)}</div>` : ''}
  </div>`;
}

export async function executePlan(planId) {
  try {
    const run = await api(`/api/plans/${planId}/execute`, {
      method: 'POST',
      body: { web_search_enabled: state.webSearchEnabled },
    });
    const plan = state.plans.find((item) => item.id === planId);
    if (plan) {
      plan.status = 'building';
      plan.detail = { ...(plan.detail || {}), message: '准备执行计划', run_id: run?.id || '' };
      renderPlanBar();
      fillPlanCards();
    }
    if (run?.id && run.conversation_id === state.conversationId) {
      await resumeRun(run);
    }
    const index = state.conversations.findIndex((item) => item.id === state.conversationId);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], interaction_mode: 'craft' };
    state.interactionMode = 'craft';
    localStorage.setItem('naibaChatInteractionMode', 'craft');
    renderModeSwitch();
    toast('计划已开始执行');
  } catch (error) {
    toast(`执行失败：${error.message}`);
  }
  await loadPlans();
}

export async function keepPlanning(planId) {
  try {
    await api(`/api/plans/${planId}/keep-planning`, { method: 'POST', body: {} });
    await loadPlans();
    $('#messageInput').focus();
    toast('已保留计划，继续规划');
  } catch (error) {
    toast(`继续规划失败：${error.message}`);
  }
}

export async function cancelPlan(planId) {
  const plan = state.plans.find((item) => item.id === planId);
  if (plan?.status === 'prepare' && state.chatRunId) {
    await cancelCurrentRun();
  }
  try {
    await api(`/api/plans/${planId}/cancel`, { method: 'POST', body: {} });
  } catch (error) {
    toast(`取消失败：${error.message}`);
  }
  await loadPlans();
}

export async function resolvePlanConfirmation(confirmId, approved) {
  const runId = String(activePlan()?.detail?.run_id || state.chatRunId || '');
  if (!runId) {
    toast('找不到该确认所属的 Run');
    return;
  }
  try {
    await api(approved ? '/api/tool/confirm' : '/api/tool/reject', {
      method: 'POST', body: { run_id: runId, confirm_id: confirmId },
    });
  } catch (error) {
    toast(`处理确认失败：${error.message}`);
  }
  await loadPlans();
}

export function openPlanEditor(planId) {
  const plan = state.plans.find((item) => item.id === planId);
  if (!plan) return;
  state.planEditingId = planId;
  $('#planEditTitle').value = plan.title || '';
  $('#planEditContent').value = plan.content || '';
  $('#planEditError').textContent = '';
  $('#planEditDialog').showModal();
}

export async function savePlanEdit(event) {
  event.preventDefault();
  const planId = state.planEditingId;
  if (!planId) return;
  const button = $('#savePlanEdit');
  button.disabled = true;
  try {
    await api(`/api/plans/${planId}`, {
      method: 'PUT',
      body: { title: $('#planEditTitle').value, content: $('#planEditContent').value },
    });
    $('#planEditDialog').close();
    toast('计划已保存');
    await loadPlans();
  } catch (error) {
    $('#planEditError').textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

