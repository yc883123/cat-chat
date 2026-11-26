// 对话顶部状态胶囊栏（左：任务 + GOAL，右：子代理）与详情浮层/底抽屉。
// 只依赖 01-core / 06-tasks-plans / 31-goals，避免与 08/12/15 形成循环引用。
import { $, escapeHtml, state } from './01-core.js';
import { activeTaskStatuses, taskDisplayTitle, taskStatusLabel } from './06-tasks-plans.js';
import { goalStatusInfo } from './31-goals.js';

const PILL_IDS = {
  todo: '#todoStatusPill',
  goal: '#goalStatusPill',
  subagent: '#subagentStatusPill',
};
const PANE_IDS = {
  todo: '#todoStatusPane',
  goal: '#goalStatusPane',
  subagent: '#subagentStatusPane',
};
const PANE_TITLES = {
  todo: '任务清单',
  goal: 'GOAL 状态',
  subagent: '子代理进度',
};

let openKind = '';
let statusResizeObserver = null;
let onOpenTaskCb = null;
let onCancelTaskCb = null;
let onCloseOtherMenusCb = null;

function isMobileSheet() {
  return window.innerWidth <= 760;
}

function currentTodos() {
  const cid = String(state.conversationId || '');
  if (!cid) return [];
  const rows = state.conversationTodos?.[cid];
  return Array.isArray(rows) ? rows.filter((item) => item && String(item.content || '').trim()) : [];
}

function isSubagentFailed(task) {
  const status = String(task?.status || '');
  if (status === 'failed' || status === 'interrupted') return true;
  const outcome = task?.result?.execution_outcome;
  return Boolean(outcome && outcome.status === 'failed');
}

function currentSubagents(goalInfo) {
  const cid = String(state.conversationId || '');
  if (!cid) return [];
  const all = (state.tasks || []).filter(
    (task) => task && task.kind === 'subagent' && String(task.conversation_id || '') === cid,
  );
  if (!all.length) return [];
  const goal = goalInfo?.visible ? goalInfo.goal : null;
  if (goal?.id) {
    const goalId = String(goal.id);
    const goalRunId = String(goal.run_id || '');
    const matched = all.filter((task) => {
      const tGoal = String(task.goal_id || '');
      const tParent = String(task.parent_job_id || '');
      return (tGoal && tGoal === goalId)
        || (goalRunId && tParent === goalRunId)
        || activeTaskStatuses.has(task.status);
    });
    if (matched.length) {
      return [...matched].sort((a, b) => Number(a.created_at || 0) - Number(b.created_at || 0));
    }
  }
  const active = all.filter((task) => activeTaskStatuses.has(task.status));
  if (active.length) {
    const activeParents = new Set(active.map((task) => String(task.parent_job_id || '')).filter(Boolean));
    const batch = activeParents.size
      ? all.filter((task) => activeParents.has(String(task.parent_job_id || '')) || activeTaskStatuses.has(task.status))
      : active;
    return [...batch].sort((a, b) => Number(a.created_at || 0) - Number(b.created_at || 0));
  }
  const activeRunId = String(state.chatRunId || '');
  const lastAssistant = [...(state.messages || [])].reverse().find((msg) => msg && msg.role === 'assistant');
  const lastRunId = activeRunId || String(lastAssistant?.metadata?.run_id || '');
  if (!lastRunId) return [];
  const matched = all.filter((task) => String(task.parent_job_id || '') === lastRunId);
  return [...matched].sort((a, b) => Number(a.created_at || 0) - Number(b.created_at || 0));
}

function pillIconMarkup(kind, tone, spinning) {
  if (spinning) {
    return '<span class="chat-status-spinner" aria-hidden="true"></span>';
  }
  if (tone === 'done') {
    return '<span class="chat-status-dot is-done" aria-hidden="true"></span>';
  }
  if (tone === 'error') {
    return '<span class="chat-status-dot is-error" aria-hidden="true"></span>';
  }
  if (tone === 'warning') {
    return '<span class="chat-status-dot is-warning" aria-hidden="true"></span>';
  }
  if (tone === 'cancelled') {
    return '<span class="chat-status-dot is-cancelled" aria-hidden="true"></span>';
  }
  return `<span class="chat-status-dot is-${kind}" aria-hidden="true"></span>`;
}

function paintPill(el, { visible, text, tone = 'neutral', spinning = false, title = '', kind = '' }) {
  if (!el) return;
  el.hidden = !visible;
  if (!visible) {
    el.setAttribute('aria-expanded', 'false');
    return;
  }
  el.dataset.tone = tone;
  el.dataset.spinning = spinning ? '1' : '0';
  el.title = title || text;
  const html = `${pillIconMarkup(kind, tone, spinning)}<span class="chat-status-pill-text">${escapeHtml(text)}</span>`;
  if (el.dataset.renderKey !== html) {
    el.dataset.renderKey = html;
    el.innerHTML = html;
  }
  el.setAttribute('aria-expanded', openKind === kind ? 'true' : 'false');
}

function renderTodoPane(todos) {
  const pane = $(PANE_IDS.todo);
  if (!pane) return;
  if (!todos.length) {
    pane.innerHTML = '<div class="chat-status-empty">当前对话暂无任务清单</div>';
    return;
  }
  const done = todos.filter((item) => item.status === 'completed').length;
  const active = todos.find((item) => item.status === 'in_progress');
  const rows = todos.map((item) => {
    const status = String(item.status || 'pending');
    const mark = status === 'completed' ? '✓' : (status === 'in_progress' ? '●' : '○');
    const label = status === 'completed' ? '已完成' : (status === 'in_progress' ? '进行中' : '待处理');
    return `<li class="chat-status-todo-item is-${escapeHtml(status)}">
      <span class="chat-status-todo-mark" aria-label="${label}">${mark}</span>
      <span class="chat-status-todo-text">${escapeHtml(String(item.content || ''))}</span>
    </li>`;
  }).join('');
  pane.innerHTML = `<div class="chat-status-summary">已完成 <strong>${done}/${todos.length}</strong> 项${active ? ` · 当前：${escapeHtml(String(active.content || '').slice(0, 60))}` : ''}</div>
    <ul class="chat-status-todo-list">${rows}</ul>`;
}

function renderSubagentPane(items) {
  const pane = $(PANE_IDS.subagent);
  if (!pane) return;
  if (!items.length) {
    pane.innerHTML = '<div class="chat-status-empty">当前没有子代理任务</div>';
    return;
  }
  const done = items.filter((item) => item.status === 'completed' && !isSubagentFailed(item)).length;
  const failed = items.filter((item) => isSubagentFailed(item)).length;
  const running = items.filter((item) => activeTaskStatuses.has(item.status)).length;
  const rows = items.map((task) => {
    const active = activeTaskStatuses.has(task.status);
    const isFail = isSubagentFailed(task);
    const tone = active ? 'running' : (isFail ? 'error' : (task.status === 'completed' ? 'done' : 'cancelled'));
    const statusText = isFail && task.status === 'completed' ? '执行未完成' : taskStatusLabel(task.status);
    const step = String(task.current_step || task.error || task.result?.error || task.result?.execution_outcome?.summary || '').trim();
    const tid = escapeHtml(String(task.id || ''));
    return `<div class="chat-status-subagent-item is-${tone}">
      <div class="chat-status-subagent-head">
        <span class="chat-status-subagent-title">${escapeHtml(taskDisplayTitle(task))}</span>
        <span class="chat-status-badge is-${tone}">${escapeHtml(statusText)}</span>
      </div>
      ${step ? `<div class="chat-status-subagent-step">${escapeHtml(step.slice(0, 180))}</div>` : ''}
      <div class="chat-status-subagent-actions">
        <button class="control-button" type="button" data-status-open-task="${tid}">查看详情</button>
        ${active ? `<button class="control-button" type="button" data-status-cancel-task="${tid}">停止</button>` : ''}
      </div>
    </div>`;
  }).join('');
  pane.innerHTML = `<div class="chat-status-summary">共 <strong>${items.length}</strong> 个子代理 · 已完成 ${done}${running ? ` · 执行中 ${running}` : ''}${failed ? ` · <span class="is-error-text">失败 ${failed}</span>` : ''}</div>
    <div class="chat-status-subagent-list">${rows}</div>`;
}

export function syncChatStatusBarHeight() {
  const bar = $('#chatStatusBar');
  const main = document.querySelector('.chat-main');
  if (!bar || !main) return;
  const visible = !bar.hidden && (!state.appView || state.appView === 'chat')
    && document.body.dataset.studio !== 'on' && document.body.dataset.appView !== 'super-agent';
  main.classList.toggle('has-status-bar', visible);
  if (visible) {
    const height = Math.ceil(bar.getBoundingClientRect().height || bar.offsetHeight || 42);
    main.style.setProperty('--chat-status-bar-h', `${Math.max(36, height)}px`);
  } else {
    main.style.removeProperty('--chat-status-bar-h');
  }
}

export function positionChatStatusPopover() {
  const popover = $('#chatStatusPopover');
  if (!popover || popover.hidden || !openKind) return;
  if (isMobileSheet()) {
    popover.style.left = '';
    popover.style.top = '';
    popover.style.right = '';
    popover.style.bottom = '';
    popover.classList.add('is-sheet');
    return;
  }
  popover.classList.remove('is-sheet');
  const anchor = $(PILL_IDS[openKind]);
  if (!anchor || anchor.hidden) return;
  const rect = anchor.getBoundingClientRect();
  const popRect = popover.getBoundingClientRect();
  const edge = 12;
  const width = popRect.width || 380;
  const height = popRect.height || 260;
  let left = openKind === 'subagent' ? rect.right - width : rect.left;
  left = Math.min(Math.max(edge, left), Math.max(edge, window.innerWidth - width - edge));
  let top = rect.bottom + 8;
  if (top + height > window.innerHeight - edge) {
    top = Math.max(edge, window.innerHeight - height - edge);
  }
  popover.style.left = `${Math.round(left)}px`;
  popover.style.top = `${Math.round(top)}px`;
}

export function closeChatStatusPopover({ restoreFocus = false } = {}) {
  const previousKind = openKind;
  openKind = '';
  const popover = $('#chatStatusPopover');
  const backdrop = $('#chatStatusBackdrop');
  if (popover) popover.hidden = true;
  if (backdrop) backdrop.hidden = true;
  for (const selector of Object.values(PILL_IDS)) {
    $(selector)?.setAttribute('aria-expanded', 'false');
  }
  for (const selector of Object.values(PANE_IDS)) {
    const pane = $(selector);
    if (pane) pane.hidden = true;
  }
  if (restoreFocus && previousKind) {
    const trigger = $(PILL_IDS[previousKind]);
    if (trigger && !trigger.hidden) trigger.focus({ preventScroll: true });
  }
}

export function openChatStatusPopover(kind) {
  if (!PILL_IDS[kind]) return;
  if (openKind === kind) {
    closeChatStatusPopover();
    return;
  }
  const trigger = $(PILL_IDS[kind]);
  if (!trigger || trigger.hidden) return;
  if (typeof onCloseOtherMenusCb === 'function') onCloseOtherMenusCb();
  openKind = kind;
  const popover = $('#chatStatusPopover');
  const backdrop = $('#chatStatusBackdrop');
  if (!popover) return;
  if (backdrop && backdrop.parentElement !== document.body) document.body.appendChild(backdrop);
  if (popover.parentElement !== document.body) document.body.appendChild(popover);
  const title = $('#chatStatusPopoverTitle');
  if (title) title.textContent = PANE_TITLES[kind] || '状态详情';
  for (const [key, selector] of Object.entries(PANE_IDS)) {
    const pane = $(selector);
    if (pane) pane.hidden = key !== kind;
  }
  for (const [key, selector] of Object.entries(PILL_IDS)) {
    $(selector)?.setAttribute('aria-expanded', key === kind ? 'true' : 'false');
  }
  renderChatStatusBar();
  popover.hidden = false;
  if (backdrop) backdrop.hidden = !isMobileSheet();
  positionChatStatusPopover();
}

export function renderChatStatusBar() {
  const bar = $('#chatStatusBar');
  if (!bar) return;
  const inChat = (!state.appView || state.appView === 'chat')
    && document.body.dataset.studio !== 'on'
    && document.body.dataset.appView !== 'super-agent';
  if (!inChat) {
    bar.hidden = true;
    closeChatStatusPopover();
    syncChatStatusBarHeight();
    return;
  }
  const todos = currentTodos();
  const goalInfo = goalStatusInfo();
  const subagents = currentSubagents(goalInfo);

  // 1. 任务胶囊（左）：只有对话或 GOAL 仍在活跃执行时才旋转，空闲/已取消时不假转。
  const todoDone = todos.filter((item) => item.status === 'completed').length;
  const todoInProgress = todos.find((item) => item.status === 'in_progress');
  const todoAllDone = todos.length > 0 && todoDone === todos.length;
  const todoSpinning = Boolean((state.chatBusy || goalInfo.active) && todos.length > 0 && !todoAllDone);
  const todoTone = todoSpinning ? 'running' : (todoAllDone ? 'done' : 'neutral');
  paintPill($(PILL_IDS.todo), {
    visible: todos.length > 0,
    text: `任务 ${todoDone}/${todos.length}`,
    tone: todoTone,
    spinning: todoSpinning,
    title: todoInProgress ? `正在进行：${todoInProgress.content}` : `任务进度 ${todoDone}/${todos.length}`,
    kind: 'todo',
  });
  renderTodoPane(todos);

  // 2. GOAL 胶囊（左）
  paintPill($(PILL_IDS.goal), {
    visible: goalInfo.visible,
    text: goalInfo.pillText,
    tone: goalInfo.pillTone,
    spinning: goalInfo.spinning,
    title: goalInfo.error
      ? `目标状态读取失败：${goalInfo.error}`
      : (goalInfo.goal?.reason || goalInfo.goal?.message || goalInfo.pillText),
    kind: 'goal',
  });

  // 3. 子代理胶囊（右）
  const subTotal = subagents.length;
  const subRunning = subagents.filter((task) => activeTaskStatuses.has(task.status)).length;
  const subFailed = subagents.filter((task) => isSubagentFailed(task)).length;
  const subCancelled = subagents.filter((task) => task.status === 'cancelled').length;
  const subDone = subagents.filter((task) => task.status === 'completed' && !isSubagentFailed(task)).length;
  let subText = `子代理 ${subDone}/${subTotal}`;
  let subTone = 'neutral';
  let subSpinning = false;
  if (subRunning > 0) {
    subSpinning = true;
    subTone = subFailed > 0 ? 'warning' : 'running';
    if (subFailed > 0) subText = `子代理 ${subDone}/${subTotal} · ${subFailed} 失败`;
  } else if (subFailed > 0) {
    subTone = subDone === 0 ? 'error' : 'warning';
    subText = `子代理 ${subDone}/${subTotal} · ${subFailed} 失败`;
  } else if (subCancelled > 0 && subDone < subTotal) {
    subTone = 'cancelled';
    subText = `子代理 ${subDone}/${subTotal} · 已取消`;
  } else if (subTotal > 0 && subDone === subTotal) {
    subTone = 'done';
  }
  paintPill($(PILL_IDS.subagent), {
    visible: subTotal > 0,
    text: subText,
    tone: subTone,
    spinning: subSpinning,
    title: subText,
    kind: 'subagent',
  });
  renderSubagentPane(subagents);

  const anyVisible = todos.length > 0 || goalInfo.visible || subTotal > 0;
  bar.hidden = !anyVisible;
  syncChatStatusBarHeight();

  if (openKind) {
    const activeTrigger = $(PILL_IDS[openKind]);
    if (!activeTrigger || activeTrigger.hidden) {
      closeChatStatusPopover();
    } else {
      positionChatStatusPopover();
    }
  }
}

export function bindChatStatusEvents({ onOpenTask = null, onCancelTask = null, onCloseOtherMenus = null } = {}) {
  onOpenTaskCb = onOpenTask;
  onCancelTaskCb = onCancelTask;
  onCloseOtherMenusCb = onCloseOtherMenus;

  for (const [kind, selector] of Object.entries(PILL_IDS)) {
    $(selector)?.addEventListener('click', (event) => {
      event.stopPropagation();
      openChatStatusPopover(kind);
    });
  }
  $('#chatStatusPopoverClose')?.addEventListener('click', () => closeChatStatusPopover({ restoreFocus: true }));
  $('#chatStatusBackdrop')?.addEventListener('click', () => closeChatStatusPopover());

  $('#subagentStatusPane')?.addEventListener('click', (event) => {
    const openBtn = event.target.closest?.('[data-status-open-task]');
    if (openBtn) {
      const taskId = openBtn.dataset.statusOpenTask;
      closeChatStatusPopover();
      if (typeof onOpenTaskCb === 'function') onOpenTaskCb(taskId);
      return;
    }
    const cancelBtn = event.target.closest?.('[data-status-cancel-task]');
    if (cancelBtn && !cancelBtn.disabled) {
      const taskId = cancelBtn.dataset.statusCancelTask;
      cancelBtn.disabled = true;
      if (typeof onCancelTaskCb === 'function') void onCancelTaskCb(taskId);
    }
  });

  // 浮层内部点击（如 GOAL 卡里的暂停/继续/轮次记录会同步重绘 innerHTML 导致 event.target 脱离 DOM）：
  // 阻断冒泡并用 composedPath 兜底，防止点按钮后把正在操作的浮层误关。
  $('#chatStatusPopover')?.addEventListener('click', (event) => {
    event.stopPropagation();
  });

  document.addEventListener('click', (event) => {
    if (!openKind) return;
    const popover = $('#chatStatusPopover');
    const bar = $('#chatStatusBar');
    const path = typeof event.composedPath === 'function' ? event.composedPath() : [];
    if ((popover && path.includes(popover)) || event.target.closest?.('#chatStatusPopover')) return;
    if ((bar && path.includes(bar)) || event.target.closest?.('#chatStatusBar')) return;
    closeChatStatusPopover();
  });

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !openKind) return;
    event.preventDefault();
    closeChatStatusPopover({ restoreFocus: true });
  });

  window.addEventListener('resize', () => {
    syncChatStatusBarHeight();
    if (openKind) {
      const backdrop = $('#chatStatusBackdrop');
      if (backdrop) backdrop.hidden = !isMobileSheet();
      positionChatStatusPopover();
    }
  });
  window.addEventListener('scroll', positionChatStatusPopover, true);

  document.addEventListener('naiba:conversation-changed', () => {
    closeChatStatusPopover();
    renderChatStatusBar();
  });
  document.addEventListener('naiba:goal-changed', renderChatStatusBar);
  document.addEventListener('naiba:tasks-changed', renderChatStatusBar);
  document.addEventListener('naiba:todos-changed', renderChatStatusBar);
  document.addEventListener('naiba:close-chat-status', () => closeChatStatusPopover());

  const bar = $('#chatStatusBar');
  if (bar && typeof ResizeObserver === 'function' && !statusResizeObserver) {
    statusResizeObserver = new ResizeObserver(() => {
      syncChatStatusBarHeight();
      if (openKind) positionChatStatusPopover();
    });
    statusResizeObserver.observe(bar);
  }
  renderChatStatusBar();
}
