// ============================================================
// 12-chat-input.js —— 拆分自 public/app.js 第 5238-6068 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, $$, api, escapeHtml, state, toast } from "./01-core.js";
import { markdown } from "./02-markdown.js";
import { setContextUsage, toolMediaMarkup, updateContextComposerLock, updateContextUsage, usageMarkup } from "./03-media.js";
import { fillContextResetSeed, getStreamingProseSegment, messageElement, moveBottomProseInline, refreshFirstTurnCard, replaceWithMessage, scheduleStreamingMarkdown, scrollToBottom } from "./04-messages.js";
import { loadTasks, renderPermissionModeSwitch } from "./06-tasks-plans.js";
import { updateUnloadModelButton } from "./07-models-agents.js";
import { createConversation, openConversation } from "./08-conversations.js";
import { uploadFiles } from "./10-upload.js";
import { SKILL_INSTALL_PRESET, SKILL_SCRIPT_RULES, clearElapsedStatus, clearRunReconnectTimers, clearVisionProgress, collapseToolReasoningBlock, createStreamingReasoningBlock, detachRunConnection, sendChatMessage, setConnectionState, settleReconnectStatus, showElapsedStatus, stopRunWatchdog } from "./11-run-stream.js";
import { insertTextAtCursor, renderInputMirror, resizeTextarea, updateSkillPopup } from "./13-skill-refs.js";
import { normalizeChoiceGroups } from "./17-choice-groups.js";
import { freezeQueuedInterjections, handleInterjectionConsumedEvent, handleUserGuidanceEvent, queuedInterjectionStats } from "./18-interjections.js";
export async function startSkillInstall() {
  if (state.chatRunId || state.abortController) {
    toast('请先等待当前任务结束或停止后再安装 Skill');
    return;
  }
  if (!state.conversationId) await createConversation();
  const cid = state.conversationId;
  try {
    const result = await api(`/api/conversations/${cid}/tools`, {
      method: 'POST',
      body: { tools: ['install_skill', 'unpack_skill_archive', 'read_file', 'edit_file', 'write_file'] },
    });
    toast(`已启用 Skill 安装工具${result.added?.length ? `（新增 ${result.added.length} 个）` : ''}`);
  } catch (error) {
    toast(`启用安装工具失败：${error.message}`);
    return;
  }
  sendMessage(SKILL_INSTALL_PRESET);
}

// ---- 自定义指令（开始新对话页的卡片）：内置预设与用户条目同在 config.starter_prompts，
// 因此两者都可编辑、可删除；删除内置预设后可用「恢复默认预设」补回。 ----
// 图标名 → SVG 内容（内置预设的 icon 字段随条目存在用户 config 里）。
const STARTER_ICONS = {
  list: '<path d="M4 6h16M4 12h16M4 18h10"></path>',
  sparkle: '<circle cx="12" cy="12" r="3"></circle><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"></path>',
  link: '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"></path><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"></path>',
  wrench: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18v3h3l6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-2.4 2.6-2.6Z"></path>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"></path>',
  clock: '<circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 3"></path>',
  plus: '<path d="M12 5v14M5 12h14"></path>',
};
const STARTER_DEFAULT_DESC = '自定义指令';

function starterIconSvg(name) {
  const inner = STARTER_ICONS[String(name || '')] || STARTER_ICONS.plus;
  return `<svg viewBox="0 0 24 24" aria-hidden="true">${inner}</svg>`;
}

export async function loadStarterPrompts() {
  try {
    const r = await api('/api/starter-prompts');
    state.customPrompts = Array.isArray(r.prompts) ? r.prompts : [];
    state.missingStarterPresets = Number(r.missing_presets || 0);
    renderStarterPrompts();
  } catch (error) {
    state.customPrompts = [];
    state.missingStarterPresets = 0;
  }
}

export async function restoreStarterPresets() {
  try {
    const r = await api('/api/starter-prompts/restore', { method: 'POST', body: {} });
    state.customPrompts = Array.isArray(r.prompts) ? r.prompts : [];
    state.missingStarterPresets = 0;
    renderStarterPrompts();
    toast('已恢复默认预设');
  } catch (error) {
    toast(`恢复默认预设失败：${error.message}`);
  }
}

export function renderStarterPrompts() {
  const grid = document.querySelector('.starter-grid');
  const addBtn = $('#starterAddBtn');
  if (!grid || !addBtn) return;
  grid.querySelectorAll('.custom-starter').forEach((el) => el.remove());
  // 卡片插在静态动作按钮（安装/编辑 Skill）之前，保持"预设 → 动作 → 自定义指令"的原顺序。
  const anchor = grid.querySelector('.starter-action') || addBtn;
  state.customPrompts.forEach((p, i) => {
    if (!p || !p.text) return;
    const wrap = document.createElement('div');
    wrap.className = 'custom-starter';
    const main = document.createElement('button');
    main.type = 'button';
    main.title = `点击复用：${p.title || '自定义指令'}`;
    main.innerHTML = `<span class="starter-icon">${starterIconSvg(p.icon)}</span><span class="starter-title">${escapeHtml(p.title || '自定义指令')}</span><span class="starter-desc">${escapeHtml(p.desc || STARTER_DEFAULT_DESC)}</span>`;
    main.addEventListener('click', () => sendMessage(p.text));
    const edit = document.createElement('button');
    edit.type = 'button';
    edit.className = 'starter-edit';
    edit.title = '编辑此指令';
    edit.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4L19.5 8.5a2.12 2.12 0 0 0-3-3L5 17l-1 4Z"></path><path d="M13.5 6.5l3 3"></path></svg>';
    edit.addEventListener('click', (e) => { e.stopPropagation(); openStarterPromptDialog(i, 'starter'); });
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'starter-del';
    del.title = '删除此指令';
    del.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg>';
    del.addEventListener('click', (e) => { e.stopPropagation(); removeStarterPrompt(i); });
    wrap.appendChild(main);
    wrap.appendChild(edit);
    wrap.appendChild(del);
    grid.insertBefore(wrap, anchor);
  });
  const restoreBtn = $('#starterRestoreBtn');
  if (restoreBtn) restoreBtn.hidden = !(state.missingStarterPresets > 0);
}

export function openStarterPromptDialog(index = -1, target = 'starter') {
  const quick = target === 'quick';
  state.editingPromptTarget = quick ? 'quick' : 'starter';
  state.editingStarterPrompt = index;
  const list = quick ? quickPanelState.items : state.customPrompts;
  // 按原始序号查找（快捷消息列表按权重排序后，数组下标不等于 index）。
  const p = (index >= 0
    ? list.find((item) => Number(item?.index ?? list.indexOf(item)) === Number(index))
    : null) || {};
  const heading = $('#starterPromptHeading');
  const hint = $('#starterPromptHint');
  const titleField = $('#starterPromptTitleField');
  const textLabel = $('#starterPromptTextLabel');
  if (heading) heading.textContent = index >= 0
    ? (quick ? '编辑快捷消息' : '编辑自定义指令')
    : (quick ? '添加快捷消息' : '添加自定义指令');
  if (hint) hint.textContent = quick
    ? '保存后可在会话输入区「快捷消息」面板中点击插入'
    : '保存后可在「开始新对话」页快速复用';
  // 快捷消息只有正文，不设标题。
  if (titleField) titleField.hidden = quick;
  if (textLabel) textLabel.textContent = quick ? '正文' : '指令内容';
  $('#starterPromptTitle').value = quick ? '' : (p.title || '');
  $('#starterPromptText').value = p.text || '';
  $('#starterPromptDialog').showModal();
  (quick ? $('#starterPromptText') : $('#starterPromptTitle')).focus();
}

export async function saveStarterPrompt() {
  const title = $('#starterPromptTitle').value;
  const text = $('#starterPromptText').value;
  if (!text.trim()) { toast('内容不能为空'); return; }
  const editing = state.editingStarterPrompt;
  const quick = state.editingPromptTarget === 'quick';
  const base = quick ? '/api/quick-messages' : '/api/starter-prompts';
  try {
    const url = editing >= 0 ? `${base}/${editing}` : base;
    const body = quick ? { text } : { title, text };
    const r = await api(url, { method: 'POST', body });
    if (quick) {
      quickPanelState.items = r.messages || [];
      state.editingStarterPrompt = -1;
      renderQuickMessages();
    } else {
      state.customPrompts = r.prompts || [];
      state.missingStarterPresets = Number(r.missing_presets || 0);
      state.editingStarterPrompt = -1;
      renderStarterPrompts();
    }
    $('#starterPromptDialog').close();
    toast(quick
      ? (editing >= 0 ? '已更新快捷消息' : '已添加快捷消息')
      : (editing >= 0 ? '已更新自定义指令' : '已保存自定义指令'));
  } catch (error) {
    toast(`保存失败：${error.message}`);
  }
}

export async function removeStarterPrompt(index) {
  try {
    const r = await api(`/api/starter-prompts/${index}`, { method: 'DELETE' });
    state.customPrompts = r.prompts || [];
    state.missingStarterPresets = Number(r.missing_presets || 0);
    renderStarterPrompts();
    toast('已删除自定义指令');
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

// ---- 快捷消息面板（独立列表：不复用「开始新对话」页的自定义指令）----
// 排序由后端 GET /api/quick-messages?sort=usage 给出：min(次数,50)×2 + 新鲜度加分，
// 并列取新增时间倒序——新条目有曝光窗口，高频条目稳定靠前。
export const quickPanelState = { open: false, items: [], loading: false };

const QUICK_EDIT_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4L19.5 8.5a2.12 2.12 0 0 0-3-3L5 17l-1 4Z"></path><path d="M13.5 6.5l3 3"></path></svg>';
const QUICK_DELETE_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg>';

function renderQuickMessages() {
  const list = $('#quickMessageList');
  if (!list) return;
  if (quickPanelState.loading) {
    // 每次打开都重新取：先占位，避免闪出上一轮的旧顺序。
    list.innerHTML = '<div class="quick-msg-empty">正在读取快捷消息…</div>';
    return;
  }
  if (!quickPanelState.items.length) {
    list.innerHTML = '<div class="quick-msg-empty">还没有快捷消息，点右上角「＋ 新建」添加</div>';
    return;
  }
  list.innerHTML = quickPanelState.items.map((item) => {
    // 快捷消息只有正文：首行作标题行、其余行折叠成一行预览（都截断）。
    const lines = String(item.text || '').split('\n').map((line) => line.trim()).filter(Boolean);
    const headline = (lines[0] || '').slice(0, 60);
    const preview = lines.slice(1).join(' ').slice(0, 80);
    return `<div class="quick-msg-item" role="menuitem" tabindex="-1" data-quick-index="${item.index}" title="点击插入到输入框">
      <div class="quick-msg-main">
        <span class="quick-msg-title"><b>${escapeHtml(headline)}</b>${item.preset_id ? '<em class="quick-msg-tag" title="内置预设：可编辑、可删除，改动与删除都不会被版本更新覆盖">默认</em>' : ''}</span>
        ${preview ? `<small>${escapeHtml(preview)}</small>` : ''}
      </div>
      <button type="button" class="quick-msg-action" data-quick-edit="${item.index}" title="编辑" aria-label="编辑">${QUICK_EDIT_SVG}</button>
      <button type="button" class="quick-msg-action" data-quick-delete="${item.index}" title="删除" aria-label="删除">${QUICK_DELETE_SVG}</button>
    </div>`;
  }).join('');
}

async function loadQuickMessages() {
  quickPanelState.loading = true;
  renderQuickMessages();
  try {
    const result = await api('/api/quick-messages?sort=usage');
    quickPanelState.items = Array.isArray(result.messages) ? result.messages : [];
  } catch (error) {
    quickPanelState.items = [];
    toast(`读取快捷消息失败：${error.message}`);
  } finally {
    quickPanelState.loading = false;
  }
}

export async function removeQuickMessage(index) {
  try {
    const result = await api(`/api/quick-messages/${index}`, { method: 'DELETE' });
    quickPanelState.items = result.messages || [];
    renderQuickMessages();
    toast('已删除快捷消息');
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

export function closeQuickMessagePanel() {
  quickPanelState.open = false;
  quickPanelState.loading = false;
  const panel = $('#quickMessagePanel');
  const button = $('#quickMessageButton');
  if (panel) panel.hidden = true;
  button?.setAttribute('aria-expanded', 'false');
}

// 面板固定定位：右对齐按钮、优先向上展开，贴边时夹在视口内（不依赖 composer 的 overflow）。
export function positionQuickMessagePanel() {
  const panel = $('#quickMessagePanel');
  const button = $('#quickMessageButton');
  if (!panel || !button || panel.hidden) return;
  const rect = button.getBoundingClientRect();
  const width = panel.offsetWidth;
  const height = panel.offsetHeight;
  const margin = 8;
  let top = rect.top - height - 6;
  if (top < margin) top = Math.min(rect.bottom + 6, window.innerHeight - height - margin);
  const left = Math.max(margin, Math.min(rect.right - width, window.innerWidth - width - margin));
  panel.style.top = `${Math.max(margin, top)}px`;
  panel.style.left = `${left}px`;
}

export async function toggleQuickMessagePanel() {
  if (quickPanelState.open) {
    closeQuickMessagePanel();
    return;
  }
  quickPanelState.open = true;
  const panel = $('#quickMessagePanel');
  const button = $('#quickMessageButton');
  if (panel) panel.hidden = false;
  button?.setAttribute('aria-expanded', 'true');
  // 每次打开都重新取：排序随使用次数与新增时间变化。
  await loadQuickMessages();
  if (!quickPanelState.open) return;
  renderQuickMessages();
  positionQuickMessagePanel();
}

// 点击插入到输入框（不自动发送），并记一次使用（失败只记日志，不阻断插入）。
export function insertQuickMessage(index) {
  const entry = quickPanelState.items.find((item) => Number(item.index) === Number(index));
  if (!entry) return;
  insertTextAtCursor(entry.text);
  closeQuickMessagePanel();
  $('#messageInput')?.focus();
  api(`/api/quick-messages/${entry.index}/use`, { method: 'POST', body: {} }).catch((error) => {
    console.debug('[naiba] 记录快捷消息使用失败:', error.message);
  });
}

export function handleQuickMessagePanelClick(event) {
  const editButton = event.target.closest('[data-quick-edit]');
  if (editButton) {
    event.stopPropagation();
    openStarterPromptDialog(Number(editButton.dataset.quickEdit), 'quick');
    return;
  }
  const deleteButton = event.target.closest('[data-quick-delete]');
  if (deleteButton) {
    event.stopPropagation();
    removeQuickMessage(Number(deleteButton.dataset.quickDelete));
    return;
  }
  if (event.target.closest('#quickMessageAdd')) {
    event.stopPropagation();
    openStarterPromptDialog(-1, 'quick');
    return;
  }
  const item = event.target.closest('[data-quick-index]');
  if (!item) return;
  insertQuickMessage(Number(item.dataset.quickIndex));
}

export const SKILL_EDIT_PRESET =
  '用户希望编辑本应用内一个已安装的 Skill。本会话已为你启用 inspect_installed_skill（以及读取/编辑/写入文件）工具。'
  + '请按以下流程执行，并【先等待用户指定要编辑哪个 Skill】：\n'
  + '1. 等待用户给出目标 Skill（支持名称或 id）。\n'
  + '2. 调用 inspect_installed_skill{skill: <名称或id>} 拿到该 Skill 的 path（SKILL.md）与 root（所在目录）。\n'
  + '3. 用 read_file 读取 SKILL.md 及其相关脚本/资源，向用户概述当前内容。\n'
  + '4. 按用户要求，用 edit_file/write_file 修改 SKILL.md、描述、脚本等；修改前可先与用户确认改动点，改完说明改了什么。\n'
  + '5. 若涉及附带脚本：按下面的脚本规范整理——脚本统一放到该 Skill 目录的 scripts/ 子目录，读写文件显式 UTF-8；'
  + '发现旧脚本裸用 open()（冻结版下会按 GBK 读写、中文乱码）时，顺手改成 encoding="utf-8" 并告知用户。\n'
  + '6. 提醒用户：改动会持久化到该 Skill 文件；切换/重开会话或重新引用（/技能名）后生效。\n'
  + '7. 若用户给的 Skill 不存在（inspect_installed_skill 返回未找到），向用户说明可用的 Skill，不要凭空编造。\n\n'
  + SKILL_SCRIPT_RULES;

export async function startSkillEdit() {
  if (state.chatRunId || state.abortController) {
    toast('请先等待当前任务结束或停止后再编辑 Skill');
    return;
  }
  if (!state.conversationId) await createConversation();
  const cid = state.conversationId;
  try {
    const result = await api(`/api/conversations/${cid}/tools`, {
      method: 'POST',
      body: { tools: ['inspect_installed_skill', 'read_file', 'edit_file', 'write_file'] },
    });
    toast(`已启用编辑 Skill 工具${result.added?.length ? `（新增 ${result.added.length} 个）` : ''}`);
  } catch (error) {
    toast(`启用编辑工具失败：${error.message}`);
    return;
  }
  sendMessage(SKILL_EDIT_PRESET);
}

export async function sendMessage(textOverride = '') {
  await sendChatMessage(textOverride);
}

// 思考强度等级的中文名（按钮下方标签 + 菜单高亮共用同一份，避免两处各写一套）。
export const REASONING_LABELS = {
  auto: '自动',
  off: '关闭',
  low: '低',
  medium: '中',
  high: '高',
};

export function updateDeepReasoningButton() {
  const btn = $('#deepReasoningButton');
  if (!btn) return;
  const disabled = Boolean(state.chatRunId || state.abortController);
  btn.disabled = disabled;
  const effort = state.reasoningEffort || (state.deepReasoningEnabled ? 'medium' : 'auto');
  const auto = effort === 'auto';
  const active = auto || effort !== 'off';
  const label = REASONING_LABELS[effort] || REASONING_LABELS.auto;
  btn.classList.toggle('active', active);
  btn.dataset.reasoningEffort = effort;
  btn.setAttribute('aria-pressed', String(active));
  btn.title = auto ? '思考强度：跟随 API（自动）' : `思考强度：${label}`;
  const labelEl = $('#reasoningLabel');
  if (labelEl) labelEl.textContent = `思考 ${label}`;
  // 菜单高亮当前等级（点开就能看到"现在用的是哪一档"）
  document.querySelectorAll('#reasoningMenu [data-reasoning-effort]').forEach((item) => {
    const isCurrent = item.dataset.reasoningEffort === effort;
    item.classList.toggle('is-active', isCurrent);
    item.setAttribute('aria-checked', String(isCurrent));
  });
}

// 点击思考按钮只做一件事：弹开/收起强度列表（不再顺带切换等级——用户实测反馈）。
export function toggleDeepReasoning() {
  if (state.chatRunId || state.abortController) return;
  const menu = $('#reasoningMenu');
  if (!menu) return;
  const open = menu.hidden;
  // 弹层挂到 body 并 fixed 定位：避免被 .composer-wrap 的 overflow 裁剪（教训 §九.27）。
  if (open && menu.parentElement !== document.body) document.body.appendChild(menu);
  menu.hidden = !open;
  $('#deepReasoningButton')?.setAttribute('aria-expanded', String(open));
  if (open) {
    updateDeepReasoningButton();
    positionReasoningMenu();
  }
}

// 强度列表的定位（fixed + 按按钮对齐，夹在视口内；优先向上展开，贴顶则下翻）。
export function positionReasoningMenu() {
  const menu = $('#reasoningMenu');
  const btn = $('#deepReasoningButton');
  if (!menu || !btn || menu.hidden) return;
  const rect = btn.getBoundingClientRect();
  const menuRect = menu.getBoundingClientRect();
  const edge = 12;
  const left = Math.min(
    Math.max(edge, rect.right - menuRect.width),
    Math.max(edge, window.innerWidth - menuRect.width - edge),
  );
  let top = rect.top - menuRect.height - 8;
  if (top < edge) {
    top = Math.min(rect.bottom + 8, Math.max(edge, window.innerHeight - menuRect.height - edge));
  }
  menu.style.left = `${Math.round(left)}px`;
  menu.style.top = `${Math.round(top)}px`;
}

export function closeReasoningMenu() {
  const menu = $('#reasoningMenu');
  if (!menu || menu.hidden) return;
  menu.hidden = true;
  $('#deepReasoningButton')?.setAttribute('aria-expanded', 'false');
}

// ---- 审批模式上拉框（与思考强度列表同套路：挂 body + fixed 定位 + 夹在视口内）----
// 收起态只有一个触发按钮；点开才在按钮上方弹出选项列表（原为平铺四段，占位又拥挤）。
export const permissionMenuState = { open: false };

export function togglePermissionModeMenu() {
  const menu = $('#permissionModeMenu');
  const button = $('#permissionModeButton');
  if (!menu) return;
  if (permissionMenuState.open) { closePermissionModeMenu(); return; }
  if (button?.disabled) return;
  // 弹层挂到 body 并 fixed 定位：避免被 .composer-wrap 的 overflow 裁剪（教训 §九.27）。
  if (menu.parentElement !== document.body) document.body.appendChild(menu);
  permissionMenuState.open = true;
  menu.hidden = false;
  button?.setAttribute('aria-expanded', 'true');
  // 打开前按会话数据刷新一次当前档高亮，避免沿用上一轮的旧高亮。
  renderPermissionModeSwitch();
  positionPermissionModeMenu();
}

// 审批列表定位（fixed + 右对齐按钮、夹在视口内；优先向上展开，贴顶则下翻）。
export function positionPermissionModeMenu() {
  const menu = $('#permissionModeMenu');
  const button = $('#permissionModeButton');
  if (!menu || !button || menu.hidden) return;
  const rect = button.getBoundingClientRect();
  const menuRect = menu.getBoundingClientRect();
  const edge = 12;
  const left = Math.min(
    Math.max(edge, rect.right - menuRect.width),
    Math.max(edge, window.innerWidth - menuRect.width - edge),
  );
  let top = rect.top - menuRect.height - 8;
  if (top < edge) {
    top = Math.min(rect.bottom + 8, Math.max(edge, window.innerHeight - menuRect.height - edge));
  }
  menu.style.left = `${Math.round(left)}px`;
  menu.style.top = `${Math.round(top)}px`;
}

export function closePermissionModeMenu() {
  permissionMenuState.open = false;
  const menu = $('#permissionModeMenu');
  if (menu) menu.hidden = true;
  $('#permissionModeButton')?.setAttribute('aria-expanded', 'false');
}

// 选择一档思考强度（菜单项点击）。思考强度是**会话级**设置：没有会话时先建会话再保存。
export async function setReasoningEffort(effort) {
  if (!REASONING_LABELS[effort]) return;
  if (state.chatRunId || state.abortController) return;
  if (!state.conversationId) await createConversation();
  if (!state.conversationId) return;
  const previous = state.reasoningEffort || 'auto';
  const previousEnabled = Boolean(state.deepReasoningEnabled);
  state.reasoningEffort = effort;
  state.deepReasoningEnabled = effort !== 'off';
  updateDeepReasoningButton();
  closeReasoningMenu();
  try {
    const updated = await api(`/api/conversations/${state.conversationId}/settings`, {
      method: 'POST',
      body: { reasoning_effort: effort, deep_reasoning_enabled: state.deepReasoningEnabled },
    });
    const index = state.conversations.findIndex((item) => item.id === state.conversationId);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...updated };
    toast(effort === 'auto' ? '思考强度：跟随 API（自动）' : `思考强度：${REASONING_LABELS[effort]}`);
  } catch (error) {
    state.reasoningEffort = previous;
    state.deepReasoningEnabled = previousEnabled;
    updateDeepReasoningButton();
    toast(`思考设置保存失败：${error.message}`);
  }
}

export function markdownFilePreview(text) {
  return markdown(text, false);
}

export async function handlePasteImage(event) {
  const items = (event.clipboardData && event.clipboardData.items) || [];
  const imageFiles = [];
  for (const item of items) {
    if (item.type && item.type.startsWith('image/') && item.kind === 'file') {
      const file = item.getAsFile();
      if (file) imageFiles.push(file);
    }
  }
  if (!imageFiles.length) return;
  event.preventDefault();
  if (!state.conversationId) await createConversation();
  await uploadFiles(imageFiles);
  toast('已粘贴图片，可发送');
}

// ---- Run 事件流渲染：类型路由表（阶段 3：巨型 if-else → type→handler 表）----
// 约定：handler 返回 false 表示“本事件不触发滚动”（原 debug_cache/reasoning_delta
// 空内容分支的 return 语义）；其余事件按原实现在分派后统一滚动到底部。
const CHAT_EVENT_HANDLERS = {
  debug_cache: handleDebugCacheEvent,
  run_started: handleRunStartedEvent,
  status: handleStatusEvent,
  skills: handleSkillsEvent,
  skill_warning: handleSkillWarningEvent,
  tools_available: handleToolsAvailableEvent,
  delta: handleDeltaEvent,
  reasoning_start: handleReasoningStartEvent,
  reasoning_delta: handleReasoningDeltaEvent,
  reasoning_end: handleReasoningEndEvent,
  reasoning: handleReasoningEvent,
  tool_start: handleToolStartEvent,
  tool_progress: handleToolProgressEvent,
  tool_result: handleToolResultEvent,
  tool_confirm: handleToolConfirmEvent,
  choice: handleChoiceEvent,
  // 插话（插话队列面板的实时反馈；实现见 18-interjections.js）
  user_guidance: handleUserGuidanceEvent,
  interjection_consumed: handleInterjectionConsumedEvent,
  cancelled: handleCancelledEvent,
  run_failed: handleRunFailedEvent,
  context_full: handleContextFullEvent,
  done: handleDoneEvent,
  error: handleErrorEvent,
  usage: handleUsageEvent,
};

export function handleChatEvent(event, row, conversationId = state.conversationId, runId = state.chatRunId) {
  if (conversationId !== state.conversationId) return;
  if (state.cancelRequested || state.cancelledRunIds.has(String(event.run_id || runId || ''))) return;
  // 重连已恢复：任何真实事件到达即撤销「重连中…」滞留。挂在这里（而不是各个 handler 里）
  // 是唯一不漏的挂法——reasoning_delta / reasoning_end / reasoning / tool_progress /
  // tool_result / usage 六个 handler 本来都不清计时，逐个补调用必漏；未知类型也一样算
  // 「流活着」。只清 'reconnect' 来源：「正在思考 · 已等待 X 秒」是设计特性（见 settleReconnectStatus）。
  settleReconnectStatus();
  const answer = row.querySelector('.answer-content');
  const activity = row.querySelector('.run-activity');
  const setActivity = (content, html = false) => {
    if (!activity) return;
    activity.hidden = !content;
    if (html) activity.innerHTML = content;
    else activity.textContent = content || '';
  };
  const collapseReasoning = () => {
    row.querySelectorAll('.reasoning-block').forEach((block) => { block.open = false; });
  };
  const handler = CHAT_EVENT_HANDLERS[String(event.type || '')];
  if (!handler) {
    // 未知类型：可观测（console）而非静默；原实现未知事件同样直接落到末尾滚动。
    console.debug('[naiba] 未识别的运行事件类型:', String(event.type || ''));
    scrollToBottom();
    return;
  }
  if (handler(event, { row, answer, setActivity, collapseReasoning, conversationId, runId }) !== false) {
    scrollToBottom();
  }
}

function handleDebugCacheEvent(event) {
  // 缓存诊断（NAIBA_DEBUG_CACHE=1 时由后端推送）：逐条 [索引:角色:字节数:哈希]
  console.groupCollapsed(`[CACHE] ${event.label || ''}`);
  (event.lines || []).forEach((line) => console.log(line));
  console.groupEnd();
  window.__CACHE_DEBUG__ ??= [];
  window.__CACHE_DEBUG__.push({ label: event.label || '', lines: event.lines || [] });
  return false;
}

function handleRunStartedEvent(event, { row, conversationId }) {
  state.chatRunId = String(event.run_id || '');
  state.runConversationId = conversationId;
  row.dataset.runId = state.chatRunId;
}

function handleStatusEvent(event, { setActivity }) {
  clearVisionProgress();
  const statusMessage = String(event.message || '');
  setActivity(statusMessage);
  // 思考等待计时：显示 “正在思考 … · 已等待 X 秒”，收到进展事件即清除。
  // 走统一入口并标记来源 'status'——与重连计时区分，避免重连恢复后状态栏被
  // 一个已失效的「重连中…」独占（清计时的判据见 11-run-stream.js settleReconnectStatus）。
  showElapsedStatus(statusMessage || '正在思考', 'status');
}

function handleSkillsEvent(event, { setActivity }) {
  const user = (event.skills || []).filter((s) => s?.source !== 'auto');
  const auto = (event.skills || []).filter((s) => s?.source === 'auto');
  const parts = [];
  if (user.length) parts.push(`已启用 ${user.map((s) => s?.name).join('、')}`);
  if (auto.length) parts.push(`已自动匹配 ${auto.map((s) => s?.name).join('、')}`);
  setActivity(parts.join('；'));
}

function handleSkillWarningEvent(event, { setActivity }) {
  const warning = String(event.message || '本次引用的技能体积较大，已完整注入但可能影响响应速度');
  toast(warning);
  setActivity(warning);
}

function handleToolsAvailableEvent() {
  // Tool schemas are runtime state, not user-facing message content.
  // Keep tool execution/result details available without dumping the full
  // capability list into every response.
}

function handleDeltaEvent(event, { row, answer, setActivity }) {
  clearElapsedStatus();
  setActivity('');
  const content = String(event.content || '');
  if (row.dataset.sawTool === 'true') {
    // 已出现工具：中途正文插入事件流（与思考/工具块按时间交错），不再全部堆到底部。
    const seg = getStreamingProseSegment(row, answer);
    if (seg) scheduleStreamingMarkdown(seg, (seg.dataset.raw || '') + content);
  } else {
    // 尚无工具：正文即整段回复，累积到底部（避免正文跑到思考前面的倒序）。
    const current = answer.dataset.raw || '';
    const next = current + content;
    answer.dataset.raw = next;
    scheduleStreamingMarkdown(answer, next);
  }
}

function handleReasoningStartEvent(event, { row }) {
  clearElapsedStatus();
  state.streamingReasoningBlock = null;
  row.querySelectorAll('.reasoning-block[data-active="true"]').forEach((block) => {
    block.dataset.active = 'false';
    if (!(block.querySelector('.reasoning-content')?.dataset.raw || '').trim()) block.remove();
  });
}

// ---- 流式渲染的模型请求轮次断点：每次新请求（usage 事件边界后）的第一个块左侧画「·」 ----
// row.dataset.requestsDone = 已完成的请求数（usage 事件更新）；requestsMarked = 已标记起点数。
function markRequestStart(row, block) {
  if (!row || !block) return;
  const done = Number(row.dataset.requestsDone || 0);
  const marked = Number(row.dataset.requestsMarked || 0);
  if (marked < done + 1) {
    row.dataset.requestsMarked = String(marked + 1);
    block.classList.add('request-start');
  }
}

function handleReasoningDeltaEvent(event, { row, answer }) {
  if (!String(event.content || '').trim()) return false;
  let block = row.querySelector('.reasoning-block[data-active="true"]');
  if (!block) {
    block = createStreamingReasoningBlock(answer);
    markRequestStart(row, block);
  }
  state.streamingReasoningBlock = block;
  const content = block.querySelector('.reasoning-content');
  scheduleStreamingMarkdown(content, (content.dataset.raw || '') + String(event.content || ''));
  row.dataset.reasoningStreamed = 'true';
}

function handleReasoningEndEvent() {
  // 工具 vs 正式的分类不在此处做（正文 delta 无法可靠区分：模型可能在工具前
  // 先叙说一句）。这里保持展开；接下来若 tool_start 到来，由 collapseToolReasoningBlock
  // 坍缩成单行；若一直无 tool_start（正式回复）则保持展开。
  const block = state.streamingReasoningBlock;
  if (block) {
    block.dataset.active = 'false';
    const content = block.querySelector('.reasoning-content');
    if (!(content?.dataset.raw || '').trim()) block.remove();
  }
}

function handleReasoningEvent(event, { row, answer }) {
  if (row.dataset.reasoningStreamed) return;
  // 实时显示推理内容到可折叠块
  let block = row.querySelector('.reasoning-block');
  if (!block) {
    block = document.createElement('details');
    block.className = 'reasoning-block';
    block.open = true;
    block.innerHTML = '<summary>思考过程</summary><div class="reasoning-content"></div>';
    answer.before(block);
  }
  const content = block.querySelector('.reasoning-content');
  content.innerHTML = markdown((content.dataset.raw || '') + (content.dataset.raw ? '\n\n---\n\n' : '') + event.content);
  content.dataset.raw = (content.dataset.raw || '') + (content.dataset.raw ? '\n\n---\n\n' : '') + event.content;
}

// ---- 工具实时输出（tool_progress）：卡片内滚动输出区 + 运行计时 ----
// 后端已按时间片节流，这里再做两件事：限制保留行数、只在用户贴着底部时自动跟随
// （否则用户往上翻看命令输出时，每一行新内容都会把他拽回底部）。
const TOOL_LIVE_MAX_LINES = 200;
// 卡片 DOM → 计时器 id。用 Map 而不是往 dataset 里写 timer id：dataset 会被整体
// 序列化进快照/调试输出，塞一个不透明的整数进去只会让现场更难读。
const toolLiveTimers = new Map();

function toolLiveElapsed(card) {
  const startedAt = Number(card.dataset.startTs || 0);
  return startedAt ? Math.max(0, Math.round((Date.now() - startedAt) / 1000)) : 0;
}

function startToolLiveTimer(card) {
  const tick = () => {
    const summary = card.querySelector('summary');
    if (!summary) return;
    const reason = card.dataset.reason ? ` · ${card.dataset.reason}` : '';
    summary.textContent = `Running · ${card.dataset.tool || ''}${reason} · 已运行 ${toolLiveElapsed(card)}s`;
  };
  tick();
  toolLiveTimers.set(card, setInterval(tick, 1000));
}

function stopToolLiveTimer(card) {
  const timer = toolLiveTimers.get(card);
  if (timer === undefined) return toolLiveElapsed(card);
  clearInterval(timer);
  toolLiveTimers.delete(card);
  return toolLiveElapsed(card);
}

function findToolCard(row, event) {
  const cards = row.querySelectorAll('.tool-run');
  if (!cards.length) return null;
  const raw = event.seq;
  const seq = raw === undefined || raw === null ? '' : String(raw);
  if (seq) {
    // 优先按实例序号定位：一轮里并行调用多个同名工具时，只有序号能唯一确定是哪一张卡
    //（“最后一个 .tool-run”会把结果贴到别的卡片上）。
    for (let i = cards.length - 1; i >= 0; i -= 1) {
      if (cards[i].dataset.seq === seq && cards[i].dataset.state === 'running') return cards[i];
    }
    for (let i = cards.length - 1; i >= 0; i -= 1) {
      if (cards[i].dataset.seq === seq) return cards[i];
    }
  }
  for (let i = cards.length - 1; i >= 0; i -= 1) {
    if (cards[i].dataset.state === 'running') return cards[i];
  }
  return cards[cards.length - 1];
}

function handleToolProgressEvent(event, { row }) {
  const card = findToolCard(row, event);
  if (!card) return false;
  const line = String(event.line || '');
  if (!line) return false;
  let live = card.querySelector('.tool-live');
  if (!live) {
    live = document.createElement('div');
    live.className = 'tool-live';
    const anchor = card.querySelector('pre');
    if (anchor) anchor.after(live);
    else card.appendChild(live);
  }
  live.hidden = false;
  // 贴底判定必须在插入之前算：插入之后 scrollHeight 已经变了。
  const pinned = live.scrollTop + live.clientHeight >= live.scrollHeight - 24;
  const item = document.createElement('div');
  item.className = 'tool-live-line';
  item.textContent = line;
  live.appendChild(item);
  while (live.childElementCount > TOOL_LIVE_MAX_LINES) live.removeChild(live.firstElementChild);
  if (pinned) live.scrollTop = live.scrollHeight;
  // 返回 false：进度事件不触发整页滚动——命令刷屏时把页面一直拽到底，用户没法看别处。
  return false;
}

function handleToolStartEvent(event, { row, answer }) {
  clearElapsedStatus();
  // 首个工具出现：把之前累计在底部的正文移到内联块（紧跟该工具前），并切换为“有工具”模式。
  if (row.dataset.sawTool !== 'true') {
    moveBottomProseInline(row, answer);
    row.dataset.sawTool = 'true';
  }
  collapseToolReasoningBlock();
  // 每次工具调用作为一个独立兄弟节点插到 answer 之前，与思考块按时间顺序交错摆放，
  // 而不是全部塞进同一个 .tool-stack（那样会把所有工具挤在一起，破坏与思考块的交错）。
  const details = document.createElement('details');
  details.className = 'tool-run';
  details.open = true;
  // 实例序号 + 原始工具名/原因：tool_progress / tool_result 靠它们回到正确的卡片，
  // 终态摘要也要复用（不再依赖渲染时的字符串，避免与后续事件拼出来的文本不一致）。
  details.dataset.seq = event.seq === undefined || event.seq === null ? '' : String(event.seq);
  details.dataset.tool = String(event.tool || '');
  details.dataset.reason = String(event.reason || '');
  details.dataset.state = 'running';
  details.dataset.startTs = String(Date.now());
  const toolArguments = typeof event.arguments === 'string'
    ? event.arguments
    : JSON.stringify(event.arguments || {}, null, 2);
  // 摘要文本由计时器写入（含「已运行 Ns」），这里只留空壳 + 参数 + 实时输出区。
  details.innerHTML = `<summary></summary><pre>${escapeHtml(toolArguments)}</pre><div class="tool-live" hidden></div>`;
  markRequestStart(row, details);
  answer.before(details);
  startToolLiveTimer(details);
  // 让新插入的工具块始终位于末尾（紧贴 answer），从而保持时间顺序。
  scrollToBottom();
}

function handleToolResultEvent(event, { row }) {
  const card = findToolCard(row, event);
  if (!card) return;
  const seconds = stopToolLiveTimer(card);
  card.dataset.state = event.success ? 'completed' : 'failed';
  const summary = card.querySelector('summary');
  if (summary) {
    const reason = card.dataset.reason ? ` · ${card.dataset.reason}` : '';
    const tool = String(event.tool || card.dataset.tool || '');
    summary.textContent = `${event.success ? 'Completed' : 'Failed'} · ${tool}${reason} · ${seconds}s`;
  }
  const live = card.querySelector('.tool-live');
  // 实时输出区留在卡片内（卡片折叠，展开仍能看到这次命令真实打了什么），空则不留空块。
  if (live && !live.childElementCount) live.hidden = true;
  const toolArguments = typeof event.arguments === 'string'
    ? event.arguments
    : JSON.stringify(event.arguments || {}, null, 2);
  const pre = card.querySelector('pre') || document.createElement('pre');
  pre.textContent = `${toolArguments}\n\n${String(event.result || '')}`;
  if (!pre.parentNode) card.appendChild(pre);
  card.open = false;
  // 媒体就地出现在本次工具块下方（流式期间即可见，不必等整轮结束）。
  // 工具块本身保持折叠；终态 done 会用 messageElement 全量重渲染，toolRunMarkup
  // 从同一份 run.media 重建同位置的媒体块，位置与内容不变。
  const markup = toolMediaMarkup(event);
  const next = card.nextElementSibling;
  if (next && next.classList && next.classList.contains('tool-media')) {
    if (markup) next.outerHTML = markup;
    else next.remove();
  } else if (markup) {
    card.insertAdjacentHTML('afterend', markup);
  }
}

function handleToolConfirmEvent(event, { row, answer, runId }) {
  clearElapsedStatus();
  if (row.dataset.sawTool !== 'true') {
    moveBottomProseInline(row, answer);
    row.dataset.sawTool = 'true';
  }
  // 同样保留已输出的正式回复，避免在等待确认时被吞掉。
  const confirmId = event.confirm_id;
  const toolName = event.tool_name;
  const toolDesc = event.tool_desc;
  const toolArguments = typeof event.arguments === 'string'
    ? event.arguments
    : JSON.stringify(event.arguments || {}, null, 2);
  const confirmMarkup = `
    <div class="tool-confirm" data-confirm-id="${escapeHtml(confirmId)}">
      <div class="tool-confirm-header">
        <span class="tool-confirm-icon">⚠️</span>
        <span class="tool-confirm-title">需要确认</span>
      </div>
      <div class="tool-confirm-body">
        <div class="tool-confirm-tool">工具：${escapeHtml(toolName)}</div>
        <div class="tool-confirm-desc">${escapeHtml(toolDesc)}</div>
        ${toolArguments ? `<div class="tool-confirm-args"><pre>${escapeHtml(toolArguments)}</pre></div>` : ''}
      </div>
      <div class="tool-confirm-actions">
        <button class="tool-confirm-btn tool-confirm-reject" data-confirm-id="${escapeHtml(confirmId)}" data-run-id="${escapeHtml(event.run_id || runId)}">拒绝</button>
        <button class="tool-confirm-btn tool-confirm-approve" data-confirm-id="${escapeHtml(confirmId)}" data-run-id="${escapeHtml(event.run_id || runId)}">允许执行</button>
      </div>
    </div>`;
  answer.insertAdjacentHTML('beforebegin', confirmMarkup);
  scrollToBottom();
}

function handleChoiceEvent(event) {
  // AI回复包含可选项，显示选择面板。带上来源消息 id（后端 choice 事件已带 message_id），
  // 面板据此绑定「会话 + 来源消息」，避免同会话多条提问互相串题。
  showChoiceButtons(event.choices, event.choice_groups, {
    messageId: event.message_id,
    conversationId: state.conversationId,
  });
}

function handleCancelledEvent(event, { row, answer, setActivity, conversationId }) {
  clearElapsedStatus();
  clearVisionProgress();
  setActivity('');
  if (event.aborted_message) {
    // 取消时后端已把累积内容持久化为"已中止"assistant 消息，直接用其渲染，保留已展示的思考与工具。
    try {
      const cancelledRow = replaceWithMessage(row, event.aborted_message);
      updateContextUsage(null, event.aborted_message);
    } catch (error) {
      console.error('[naiba] cancelled 事件渲染崩溃:', error, 'message=', event.aborted_message);
    }
  } else {
    // 没有 aborted_message（例如 forced-cancel 未及时重建）：绝不能清空已展示的中途输出，
    // 只在真正无任何内容时才显示占位提示；否则会抹掉 AI 已输出的回复。
    const hasContent = Boolean((answer.dataset.raw || '').trim())
      || row.querySelector('.reasoning-block, .tool-run, .stream-prose, .tool-confirm');
    if (!hasContent) {
      answer.innerHTML = `<p>${escapeHtml(event.message || '任务已取消')}</p>`;
    }
    setActivity(event.message || '任务已取消');
  }
  // 首轮上下文折叠卡：终态事件前后端已把 first_turn 落盘，即时拉取展示（首轮取消同样有上下文可看）。
  if (conversationId) void refreshFirstTurnCard(conversationId);
  // 队列随 Run 一起冻结：残留插话只剩「取回输入框 / 删除」两个出口（本版无自动 follow-up）。
  freezeQueuedInterjections();
}

function handleRunFailedEvent(event, { answer, setActivity }) {
  clearElapsedStatus();
  clearVisionProgress();
  setActivity('');
  // 工具协议解析失败：只展示可读错误，不显示原始 XML/JSON 或命令参数。
  answer.innerHTML = `<p>执行失败：${escapeHtml(event.error || '任务执行失败')}</p>`;
  $('#runtimeStatus').textContent = '执行失败';
  // 失败同样意味着一轮结束：队列不再有消费者，冻结成「已停止」（同时撤掉引导按钮）。
  freezeQueuedInterjections();
}

function handleContextFullEvent() {
  // 上下文已达上限：后端已阻止本次请求，立即锁定输入并提示新建对话。
  state.contextAtCeiling = true;
  updateContextComposerLock(Boolean(state.chatBusy));
}

function handleUsageEvent(event, { row, answer }) {
  // 用量框常驻流式消息尾端：首次请求完成时挂载，之后每完成一次请求原位更新
  // （命中率为最后一次请求口径，与终态 metadata.usage 同构）。
  row.dataset.requestsDone = String(Math.max(
    Number(row.dataset.requestsDone || 0),
    Number(event.usage?.requests || 0),
  ));
  let box = answer.nextElementSibling;
  if (!box || !(box.classList && box.classList.contains('usage-stream'))) {
    box = document.createElement('div');
    box.className = 'usage-stream';
    answer.insertAdjacentElement('afterend', box);
  }
  box.innerHTML = usageMarkup(event.usage || {});
  // 上下文圆环随每次模型请求即时刷新（不再等整轮结束）：usage 事件本就是
  // 「最后一次请求」口径（含 context_tokens），与终态 metadata.usage 同构。
  setContextUsage(event.usage || null);
}

function handleDoneEvent(event, { row, answer, collapseReasoning, conversationId }) {
  clearElapsedStatus();
  clearVisionProgress();
  collapseReasoning();
  if (event.message) {
    try {
      const completedRow = replaceWithMessage(row, event.message);
      updateContextUsage(null, event.message);
      // 模型调用 reset_context 成功：把「新会话」种子消息填进输入框（不自动发送，
      // 用户可编辑后再点发送；刷新后仍可从分割线上的「填入种子消息」按钮找回）。
      const resetInfo = event.message.metadata?.session_start;
      if (resetInfo && String(resetInfo.source || '') === 'tool') {
        if (fillContextResetSeed(resetInfo)) {
          toast('模型已交接并重置上下文：种子消息已填入输入框，确认后发送即可继续');
        }
      }
      const metadata = event.message.metadata || {};
      if ((Array.isArray(metadata.choice_groups) && metadata.choice_groups.length)
        || (Array.isArray(metadata.choices) && metadata.choices.length)) {
        // 与 choice 事件同口径：同一个来源消息 → 同一份临时选择（重复事件只更新数据）。
        showChoiceButtons(metadata.choices, metadata.choice_groups, {
          messageId: event.message.id,
          conversationId: state.conversationId,
        });
      }
    } catch (error) {
      console.error('[naiba] done 事件渲染崩溃:', error, 'message=', event.message);
    }
  } else {
    answer.innerHTML = '<p>计划执行完成</p>';
  }
  $('#runtimeStatus').textContent = '就绪';
  // 共存守卫（与「选择」面板）：本轮结束时队列里还有未引导的插话——**只提示不消费**。
  // 旧版在这里自动 claim 队列开一个 follow-up run，若终态带选择题，刚弹出的选择面板会
  // 被新 run 立刻锁死、用户点选项又变成新插话，两个意图互相打架（本版明确不做）。
  // 顺序要紧：先取统计（此时还是 pending，才能报出「还有 N 条未引导」），再冻结面板
  // ——冻结后残留行转「已停止」，只剩「取回输入框 / 删除」两个出口。
  const leftover = queuedInterjectionStats();
  if (leftover.pending) {
    toast(`本轮已结束，还有 ${leftover.pending} 条插话未引导：点「取回输入框」后作为新消息发送，或删除`);
  }
  freezeQueuedInterjections();
  // 首轮上下文折叠卡：后端在终态事件落盘之前已抢先写入 first_turn（见 run/chat.py），
  // done 后立即拉取展示，不必等下次 openConversation / 同步轮询。
  if (conversationId) void refreshFirstTurnCard(conversationId);
}

function handleErrorEvent(event, { row, answer, collapseReasoning, conversationId }) {
  clearElapsedStatus();
  clearVisionProgress();
  collapseReasoning();
  if (event.partial_message) {
    // 失败时后端已把累积内容持久化为 partial assistant 消息，直接用其渲染，
    // 保留已展示的思考/正文/工具，避免 HTTP 500 后内容被覆盖丢失。
    try {
      const partialRow = replaceWithMessage(row, event.partial_message);
      updateContextUsage(null, event.partial_message);
    } catch (error) {
      console.error('[naiba] error 事件渲染崩溃:', error, 'message=', event.partial_message);
    }
  } else {
    // 没有 partial_message：绝不能清空已展示的中途输出，
    // 只在真正无任何内容时才显示错误占位；否则会抹掉 AI 已输出的回复。
    const hasContent = Boolean((answer.dataset.raw || '').trim())
      || row.querySelector('.reasoning-block, .tool-run, .stream-prose, .tool-confirm');
    if (hasContent) {
      const errNode = document.createElement('p');
      errNode.className = 'run-error';
      errNode.textContent = `执行失败：${event.message || '任务执行失败'}`;
      answer.appendChild(errNode);
    } else {
      answer.innerHTML = `<p>执行失败：${escapeHtml(event.message || '任务执行失败')}</p>`;
    }
  }
  $('#runtimeStatus').textContent = '执行失败';
  // 失败轮次同样可能是首轮：first_turn 已由收尾路径落盘，即时拉取展示。
  if (conversationId) void refreshFirstTurnCard(conversationId);
}

/* ---------- 交互选择面板：单选 / 多选 / 多组 ----------
 * 后端只提供数据（消息 metadata.choices / metadata.choice_groups），面板只做「收集答案 →
 * 直接作为一轮消息发送」（2026-09-16 起不再经输入框中转：长答案平铺在输入框里太占地方）。
 * 输入框里已有的草稿会拼在答案前面一起发出（口径见 sendChatMessage 的 textOverride 合并）。
 * 状态以「会话 + 来源消息」为键存在前端内存里：重渲染 / 重复事件 / 会话切换都不清空
 * （切回来还能接着答）；整页刷新即从首题重来，不落任何半成品。实时 choice 事件、done 事件
 * metadata、历史渲染三个入口统一汇入 showChoiceButtons（选择状态的唯一同步入口）。
 */
const choiceSelections = new Map();  // key → { groups, answers, index, collapsed, done }
const CHOICE_MEMORY_MAX = 24;        // 内存里最多保留多少条临时选择（按插入序淘汰最旧）
let activeChoiceKey = '';
let choicePendingSubmit = null;      // 提交中的面板（失败可回退，见 rollbackChoiceSubmit）
let pendingChoiceCustomFocus = -1;   // 刚展开「自定义回复」的题号：下次渲染后把焦点落进输入框
let choiceSubmitting = false;        // 「发送」防连点：一轮发送流程结束前不再受理

function choiceKeyOf(conversationId, messageId) {
  const conversation = String(conversationId ?? '');
  const message = String(messageId || '');
  return `${conversation}::${message || 'legacy'}`;
}

function pruneChoiceSelections() {
  while (choiceSelections.size > CHOICE_MEMORY_MAX) {
    const oldest = choiceSelections.keys().next().value;
    if (oldest === activeChoiceKey) break;
    choiceSelections.delete(oldest);
  }
}

/** 前端侧规范化：与后端 normalize_choice_groups 同口径，实现见 17-choice-groups.js。 */

function choicePicked(entry, index) {
  const picked = entry.answers[index];
  return Array.isArray(picked) ? picked : [];
}

/** 该题的自定义回复（与预设选项并存；空串表示未填）。 */
function choiceCustom(entry, index) {
  const value = Array.isArray(entry.customs) ? entry.customs[index] : '';
  return String(value ?? '').trim();
}

/** 该题是否已有答案：预设选项或自定义回复任一非空。 */
function choiceHasAnswer(entry, index) {
  return Boolean(choicePicked(entry, index).length || choiceCustom(entry, index));
}

/** 该题是否被显式跳过（跳过题不参与"未答"拦截，也不写进答案块）。 */
function choiceSkipped(entry, index) {
  return Boolean(Array.isArray(entry.skipped) ? entry.skipped[index] : false);
}

/** 该题是否已处理（答了或明确跳过）——「下一题 / 完成」的门槛。 */
function choiceResolved(entry, index) {
  return choiceHasAnswer(entry, index) || choiceSkipped(entry, index);
}

/** 该题的有效答案项 = 预设选项 + 自定义回复（自定义永远作为额外一项，单选也并存）。 */
function choiceAnswerItems(entry, index) {
  const items = [...choicePicked(entry, index)];
  const custom = choiceCustom(entry, index);
  if (custom) items.push(custom);
  return items;
}

/** 提示文案（已选数量含自定义项；跳过题有独立提示）。 */
function choiceHintText(entry, index) {
  const picked = choicePicked(entry, index).length;
  const custom = choiceCustom(entry, index) ? 1 : 0;
  const total = picked + custom;
  const multi = entry.groups[index]?.mode === 'multi';
  if (total) return `已选 ${total} 项`;
  if (choiceSkipped(entry, index)) return '已跳过本题';
  return multi ? '可多选，勾选后进入下一题（也可自己写）' : '请选择一项、写自定义回复，或点「跳过」';
}

/** 答案块：一行一题（`视觉：写实`），多选同行用「、」连接；自定义回复作为该题最后一项。 */
function choiceAnswerBlock(entry) {
  return entry.groups
    .map((group, index) => {
      const text = choiceAnswerItems(entry, index).join('、');
      if (!text) return '';
      if (!group.prompt) return text;
      // 题目自带冒号结尾（"请选择下一步："）时不重复追加分隔符
      const separator = /[：:]$/.test(group.prompt) ? '' : '：';
      return `${group.prompt}${separator}${text}`;
    })
    .filter(Boolean)
    .join('\n');
}

function syncChoicePanelLock(busy = Boolean(state.abortController)) {
  const host = $('#choiceButtons');
  if (!host) return;
  const entry = choiceSelections.get(activeChoiceKey);
  const hasAnswer = entry ? choiceResolved(entry, entry.index) : false;
  host.classList.toggle('is-busy', Boolean(busy));
  $$('#choiceButtons button').forEach((button) => {
    const nav = button.dataset.choiceNav;
    if (nav === 'collapse') return;            // 折叠是纯显示操作，运行中也允许
    if (nav === 'next' || nav === 'done') button.disabled = Boolean(busy) || !hasAnswer;
    else button.disabled = Boolean(busy);      // 选项卡片 / 上一题 / 自定义
  });
}

/**
 * 就地刷新当前题的选中态 / 已选摘要 / 提示与主按钮禁用（不重建 DOM）。
 * 自定义输入框正在打字时点选项、或边打边看已选摘要，都不该被打断——重建会丢焦点。
 */
function syncChoiceSelectionUi(entry) {
  const host = $('#choiceButtons');
  if (!host) return;
  const index = entry.index;
  const picked = choicePicked(entry, index);
  const items = choiceAnswerItems(entry, index);
  host.querySelectorAll('.choice-btn').forEach((button) => {
    const selected = picked.includes(button.dataset.choiceValue || '');
    button.classList.toggle('is-selected', selected);
    button.setAttribute('aria-checked', String(selected));
  });
  const hint = host.querySelector('.choice-hint');
  if (hint) hint.textContent = choiceHintText(entry, index);
  const go = host.querySelector('.choice-nav-primary');
  if (go) go.disabled = Boolean(state.abortController) || !choiceResolved(entry, index);
}

/** 跳过/取消跳过当前题：跳过即前进（与单选点选项一致），最后一题停在原地等「完成」。 */
function toggleChoiceSkip(entry, index) {
  if (state.abortController) return;           // 运行中不接受操作
  if (!Array.isArray(entry.skipped)) entry.skipped = [];
  entry.skipped[index] = !choiceSkipped(entry, index);
  if (entry.skipped[index] && index < entry.groups.length - 1) {
    entry.index = index + 1;
    renderChoicePanel();
    scrollToBottom();
    return;
  }
  renderChoicePanel();
}

function onChoiceOptionClick(entry, choice) {
  if (state.abortController) return;           // 运行中不接受答题
  const group = entry.groups[entry.index];
  const picked = choicePicked(entry, entry.index);
  if (group.mode === 'multi') {
    entry.answers[entry.index] = picked.includes(choice)
      ? picked.filter((item) => item !== choice)
      : [...picked, choice];
    syncChoiceSelectionUi(entry);
    return;
  }
  // 再点一次已选中的选项 = 取消该题选择（停在原题，不进入下一题；此前是"选中了取消不掉"）
  if (picked.length === 1 && picked[0] === choice) {
    entry.answers[entry.index] = [];
    syncChoiceSelectionUi(entry);
    return;
  }
  entry.answers[entry.index] = [choice];
  // 单选点击即进入下一题；最后一题停在原地，等用户统一点「完成」。
  if (entry.index < entry.groups.length - 1) {
    entry.index += 1;
    renderChoicePanel();
    scrollToBottom();
    return;
  }
  syncChoiceSelectionUi(entry);
}

/** 收尾：把答案直接作为一轮消息发送（失败时面板连答案一起还给用户，见 rollbackChoiceSubmit）。 */
function completeChoicePanel() {
  const entry = choiceSelections.get(activeChoiceKey);
  if (!entry || choiceSubmitting) return;      // 防连点：一轮发送流程结束前不再受理
  const unanswered = entry.groups.findIndex((_, index) => !choiceResolved(entry, index));
  if (unanswered >= 0) {                       // 每题要么作答、要么显式跳过
    entry.index = unanswered;
    renderChoicePanel();
    toast(`第 ${unanswered + 1} 题还没有选择（也可以点「跳过」）`);
    return;
  }
  const block = choiceAnswerBlock(entry);
  // 全部跳过时答案块为空：发一条明确的跳过说明，而不是空白消息
  const text = block || '（已跳过全部选择题）';
  entry.collapsed = false;
  choiceSubmitting = true;
  // 面板不在这里提前隐藏：sendChatMessage 的守卫（模型未选 / 回复进行中 / 上下文提醒）都在
  // beginChoiceSubmit 之前 return，被拦下时面板与答案保持原样，用户可修正后重试。
  Promise.resolve(sendMessage(text)).finally(() => { choiceSubmitting = false; });
}

function renderChoicePanel() {
  const entry = choiceSelections.get(activeChoiceKey);
  const composerWrap = $('.composer-wrap');
  const composer = $('#composerForm');
  if (!entry || !composerWrap || !composer) return;
  let host = $('#choiceButtons');
  if (!host) {
    host = document.createElement('div');
    host.className = 'choice-buttons';
    host.id = 'choiceButtons';
    host.setAttribute('role', 'group');
    composerWrap.insertBefore(host, composer);
  }
  const group = entry.groups[entry.index];
  const picked = choicePicked(entry, entry.index);
  const multi = group.mode === 'multi';
  const last = entry.index === entry.groups.length - 1;
  const busy = Boolean(state.abortController);
  host.dataset.choiceKey = activeChoiceKey;
  host.classList.toggle('is-collapsed', entry.collapsed);
  host.classList.toggle('is-busy', busy);
  host.setAttribute('aria-label', group.prompt || `第 ${entry.index + 1} 组选项`);
  host.replaceChildren();

  const header = document.createElement('div');
  header.className = 'choice-header';
  const collapse = document.createElement('button');
  collapse.type = 'button';
  collapse.className = 'choice-collapse';
  collapse.dataset.choiceNav = 'collapse';
  collapse.textContent = entry.collapsed ? '▸' : '▾';
  collapse.title = entry.collapsed ? '展开选项' : '折叠选项（已选保留）';
  collapse.setAttribute('aria-expanded', String(!entry.collapsed));
  collapse.setAttribute('aria-label', collapse.title);
  collapse.addEventListener('click', () => {
    entry.collapsed = !entry.collapsed;        // 折叠只改显示，已选照旧保留
    renderChoicePanel();
  });
  header.appendChild(collapse);

  const prompt = document.createElement('strong');
  prompt.className = 'choice-prompt';
  prompt.textContent = group.prompt || '请选择';
  header.appendChild(prompt);

  const mode = document.createElement('span');
  mode.className = `choice-mode-badge is-${multi ? 'multi' : 'single'}`;
  mode.textContent = multi ? '多选' : '单选';
  header.appendChild(mode);

  if (choiceSkipped(entry, entry.index)) {
    const skipBadge = document.createElement('span');
    skipBadge.className = 'choice-mode-badge is-skip';
    skipBadge.textContent = '已跳过';
    header.appendChild(skipBadge);
  }

  if (entry.groups.length > 1) {
    const progress = document.createElement('span');
    progress.className = 'choice-progress';
    progress.textContent = `${entry.index + 1}/${entry.groups.length}`;
    header.appendChild(progress);
  }
  host.appendChild(header);

  const body = document.createElement('div');
  body.className = 'choice-body';
  body.hidden = entry.collapsed;

  const options = document.createElement('div');
  options.className = 'choice-options';
  group.choices.forEach((choice) => {
    const selected = picked.includes(choice);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'choice-btn';
    btn.classList.toggle('is-selected', selected);
    btn.dataset.choiceValue = choice;
    btn.setAttribute('role', multi ? 'checkbox' : 'radio');
    btn.setAttribute('aria-checked', String(selected));
    btn.textContent = choice;
    btn.disabled = busy;
    if (multi) {
      const box = document.createElement('span');
      box.className = 'choice-check';
      box.setAttribute('aria-hidden', 'true');
      btn.prepend(box);
    }
    btn.addEventListener('click', () => onChoiceOptionClick(entry, choice));
    options.appendChild(btn);
  });
  body.appendChild(options);

  // 「自定义回复」：与预设选项并存的一项；就地展开输入框（展开态存 entry.customOpen，按题号对齐）
  const customBlock = document.createElement('div');
  customBlock.className = 'choice-custom';
  const customValue = Array.isArray(entry.customs) ? String(entry.customs[entry.index] ?? '') : '';
  if (!(entry.customOpen && entry.customOpen[entry.index])) {
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'choice-custom-toggle';
    toggle.dataset.choiceNav = 'custom';
    toggle.textContent = customValue.trim() ? `自定义：${customValue.trim()}（点击修改）` : '＋ 自定义回复…';
    toggle.disabled = busy;
    toggle.addEventListener('click', () => {
      entry.customOpen[entry.index] = true;
      pendingChoiceCustomFocus = entry.index;   // 重建后把焦点落回输入框
      renderChoicePanel();
    });
    customBlock.appendChild(toggle);
  } else {
    const input = document.createElement('textarea');
    input.className = 'choice-custom-input';
    input.rows = 2;
    input.placeholder = '输入你自己的回复（与上面选项并存，随「完成」一起填入输入框）';
    input.value = customValue;
    input.disabled = busy;
    input.addEventListener('input', () => {
      entry.customs[entry.index] = input.value;
      syncChoiceSelectionUi(entry);
    });
    customBlock.appendChild(input);
    if (customValue.trim()) {
      const clear = document.createElement('button');
      clear.type = 'button';
      clear.className = 'choice-custom-clear';
      clear.dataset.choiceNav = 'custom';
      clear.textContent = '清除自定义回复';
      clear.disabled = busy;
      clear.addEventListener('click', () => {
        entry.customs[entry.index] = '';
        entry.customOpen[entry.index] = false;
        renderChoicePanel();
      });
      customBlock.appendChild(clear);
    }
  }
  body.appendChild(customBlock);

  // 不再渲染「已选：…」摘要：选项选中态 + 自定义输入框已表达全部答案，摘要与按钮高亮重复。
  host.appendChild(body);

  const actions = document.createElement('div');
  actions.className = 'choice-actions';
  actions.hidden = entry.collapsed;
  if (entry.index > 0) {
    const back = document.createElement('button');
    back.type = 'button';
    back.className = 'choice-nav';
    back.dataset.choiceNav = 'prev';
    back.textContent = '上一题';
    back.disabled = busy;
    back.addEventListener('click', () => {
      entry.index -= 1;                        // 返回修改，已选保留
      renderChoicePanel();
    });
    actions.appendChild(back);
  }
  const skip = document.createElement('button');
  skip.type = 'button';
  skip.className = 'choice-nav';
  skip.dataset.choiceNav = 'skip';
  skip.textContent = choiceSkipped(entry, entry.index) ? '取消跳过' : '跳过';
  skip.title = choiceSkipped(entry, entry.index) ? '恢复本题（需要作答）' : '跳过本题（不写进发送内容）';
  skip.disabled = busy;
  skip.addEventListener('click', () => toggleChoiceSkip(entry, entry.index));
  actions.appendChild(skip);
  const hint = document.createElement('span');
  hint.className = 'choice-hint';
  hint.textContent = choiceHintText(entry, entry.index);
  actions.appendChild(hint);

  const go = document.createElement('button');
  go.type = 'button';
  go.className = 'choice-nav choice-nav-primary';
  go.dataset.choiceNav = last ? 'done' : 'next';
  go.textContent = last ? '完成' : '下一题';
  go.title = last
    ? '点「完成」后直接发送（输入框里已有的草稿会拼在答案前面）'
    : '进入下一题';
  go.disabled = busy || !choiceResolved(entry, entry.index);
  go.addEventListener('click', () => {
    if (last) {
      completeChoicePanel();
      return;
    }
    entry.index += 1;
    renderChoicePanel();
  });
  actions.appendChild(go);
  host.appendChild(actions);

  // 刚点开「自定义回复」：把焦点落进输入框（光标置尾），省得用户再点一次
  if (pendingChoiceCustomFocus === entry.index) {
    pendingChoiceCustomFocus = -1;
    const input = host.querySelector('.choice-custom-input');
    if (input) {
      input.focus();
      const end = input.value.length;
      try { input.setSelectionRange(end, end); } catch (_) { /* 个别输入类型不支持选区 */ }
    }
  }
}

export function showChoiceButtons(choices, choiceGroups = [], context = {}) {
  const groups = normalizeChoiceGroups(choices, choiceGroups);
  if (!groups.length) {
    hideChoiceButtons();
    return;
  }
  const key = choiceKeyOf(context.conversationId ?? state.conversationId, context.messageId);
  const known = choiceSelections.get(key);
  if (known?.done) return;                     // 已完成（答案已在输入框里）：重复事件/重渲染不再弹
  if (known) {
    // 同一来源重复同步：只更新数据，不重建已选状态（撤回多选后事件重放也不会被清空）。
    known.groups = groups;
    known.answers = groups.map((group, index) => choicePicked(known, index).filter((c) => group.choices.includes(c)));
    // 自定义回复与展开态按题号对齐：题目数变化时不越界、不串题。
    known.customs = groups.map((_, index) => String((known.customs || [])[index] ?? ''));
    known.customOpen = groups.map((_, index) => Boolean((known.customOpen || [])[index]));
    known.skipped = groups.map((_, index) => Boolean((known.skipped || [])[index]));
    if (known.index >= groups.length) known.index = groups.length - 1;
    if (known.index < 0) known.index = 0;
  } else {
    choiceSelections.set(key, { groups, answers: [], customs: [], customOpen: [], skipped: [], index: 0, collapsed: false, done: false });
    pruneChoiceSelections();
  }
  activeChoiceKey = key;
  renderChoicePanel();
  scrollToBottom();
}

/** 只移除 DOM，不清内存：会话切换后切回来仍能接着答（整页刷新才真正丢弃）。 */
export function hideChoiceButtons() {
  const existing = $('#choiceButtons');
  if (existing) existing.remove();
}

/** 提交路径①：本轮已带上面板的答案发出，先只隐藏不销毁（失败可回退）。 */
export function beginChoiceSubmit() {
  const entry = choiceSelections.get(activeChoiceKey);
  hideChoiceButtons();
  if (!entry) return false;
  choicePendingSubmit = { key: activeChoiceKey };
  return true;
}

/** 提交路径②：请求被受理 → 旧面板与其临时选择一起失效。 */
export function commitChoiceSubmit() {
  choicePendingSubmit = null;
  const prefix = `${String(state.conversationId ?? '')}::`;
  [...choiceSelections.keys()].forEach((key) => {
    if (key.startsWith(prefix)) choiceSelections.delete(key);
  });
}

/** 提交路径③：请求被拒（网络错误 / 409）→ 面板连答案一起还给用户。 */
export function rollbackChoiceSubmit() {
  const pending = choicePendingSubmit;
  choicePendingSubmit = null;
  if (!pending) return;
  activeChoiceKey = pending.key;
  if (choiceSelections.has(pending.key)) renderChoicePanel();
}



export async function approveTool(confirmId, runId = state.chatRunId) {
  try {
    const confirmEl = document.querySelector(`[data-confirm-id="${confirmId}"]`);
    if (confirmEl) {
      confirmEl.querySelector('.tool-confirm-actions').innerHTML = '<div class="tool-confirm-status">正在执行...</div>';
    }
    const response = await api('/api/tool/confirm', {
      method: 'POST', body: { run_id: runId, confirm_id: confirmId },
    });
    if (confirmEl) {
      confirmEl.querySelector('.tool-confirm-status').textContent = response.success ? '已执行' : `执行失败：${response.result}`;
    }
  } catch (error) {
    toast(`确认失败：${error.message}`);
  }
}

export async function rejectTool(confirmId, runId = state.chatRunId) {
  try {
    const confirmEl = document.querySelector(`[data-confirm-id="${confirmId}"]`);
    if (confirmEl) {
      confirmEl.querySelector('.tool-confirm-actions').innerHTML = '<div class="tool-confirm-status">已拒绝</div>';
    }
    const response = await api('/api/tool/reject', {
      method: 'POST', body: { run_id: runId, confirm_id: confirmId },
    });
    if (confirmEl) confirmEl.querySelector('.tool-confirm-status').textContent = response.result || '已拒绝';
  } catch (error) {
    toast(`拒绝失败：${error.message}`);
  }
}

// 确认卡按钮事件委托：模块（ESM）作用域函数不能经内联 onclick 访问（全局查找 ReferenceError），
// 统一在模块内委托分派（import 绑定只读红线：不做跨文件赋值）。
document.addEventListener('click', (event) => {
  const button = event.target.closest('.tool-confirm-btn');
  if (!button || !button.dataset.confirmId) return;
  event.preventDefault();
  const confirmId = button.dataset.confirmId;
  const runId = button.dataset.runId || state.chatRunId;
  if (button.classList.contains('tool-confirm-reject')) {
    rejectTool(confirmId, runId);
  } else if (button.classList.contains('tool-confirm-approve')) {
    approveTool(confirmId, runId);
  }
});

// 用量"请求明细"展开/收起（与确认卡同模式：data-* + 模块内委托）。
document.addEventListener('click', (event) => {
  const button = event.target.closest('[data-usage-toggle]');
  if (!button) return;
  const scope = button.closest('.usage-stream, .message-body');
  const box = scope && scope.querySelector('.usage-requests');
  if (!box) return;
  box.hidden = !box.hidden;
  button.setAttribute('aria-expanded', String(!box.hidden));
  const arrow = button.querySelector('.usage-toggle-arrow');
  if (arrow) arrow.textContent = box.hidden ? '▸' : '▾';
});

export function setBusy(busy) {
  state.chatBusy = busy;
  const mc = $('#messages');
  if (mc) mc.classList.toggle('conversation-running', busy);
  const sendBtn = $('#sendButton');
  sendBtn.classList.toggle('is-stop', busy);
  sendBtn.innerHTML = state.cancelRequested
    ? '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"></circle><circle cx="12" cy="12" r="3"></circle></svg>'
    : (busy
      ? '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2"></rect></svg>'
      : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7"></path></svg>');
  // 选择面板：运行中禁用答题，结束时按「是否已选」恢复（下一题/完成要求每题至少选一项，
  // 不能一律 enable；单一写入点见 syncChoicePanelLock）。
  syncChoicePanelLock(busy);
  // 输入框的 disabled/placeholder 与发送按钮一律由 updateContextComposerLock /
  // updateSendButtonState 单点维护（含「上下文已满」与「正在编辑消息」两种锁定原因），
  // 此处只负责图标与停止态样式——两个写入点会互相覆盖，实测踩过。
  updateContextComposerLock(busy);
  updateDeepReasoningButton();
  updateUnloadModelButton();
  if (!busy && $('#runtimeStatus').textContent !== '执行失败') $('#runtimeStatus').textContent = '就绪';
}

export async function cancelCurrentRun() {
  if (state.cancelRequested) return;
  const runId = String(state.chatRunId || '');
  const conversationId = String(state.runConversationId || state.conversationId || '');
  if (!runId && !conversationId) return;
  state.cancelRequested = true;
  state.cancelConversationId = conversationId;
  if (runId) state.cancelledRunIds.add(runId);
  state.checkRunEligible = false;
  clearRunReconnectTimers();
  detachRunConnection();
  state.runReconnectAt = 0;
  state.runAttempt = 0;
  state.runRecovering = false;
  setConnectionState('connected');
  setBusy(true);
  $('#runtimeStatus').textContent = '正在停止';

  let terminalConfirmed = false;
  let failureMessage = '';
  try {
    let result = null;
    for (const delay of [0, 100, 250, 500, 1000]) {
      if (delay) await new Promise((resolve) => window.setTimeout(resolve, delay));
      try {
        result = await api('/api/chat/cancel', {
          method: 'POST', body: { run_id: runId, conversation_id: conversationId },
        });
        break;
      } catch (error) {
        // The initial /api/chat request and this fallback request are served by
        // different threads.  Before run_started there is a tiny window where
        // the conversation Run has not been committed yet.
        if (runId || error.status !== 404 || delay === 1000) throw error;
      }
    }
    const resolvedRunId = String(result?.run?.id || runId || '');
    if (resolvedRunId) state.cancelledRunIds.add(resolvedRunId);
    for (let attempt = 0; attempt < 100; attempt += 1) {
      const active = await api(`/api/runs?conversation_id=${encodeURIComponent(conversationId)}&active_only=1`);
      if (!(active.runs || []).length) {
        terminalConfirmed = true;
        break;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 250));
    }
    if (!terminalConfirmed) throw new Error('服务端尚未确认任务已停止');
    await loadTasks();
    if (state.conversationId === conversationId) await openConversation(conversationId);
  } catch (error) {
    console.debug('[naiba] cancel chat failed:', error.message);
    failureMessage = error.message;
  } finally {
    // 无论成功与否都必须把界面放回可操作状态：本地模型正在 prefill 时后端可能
    // 迟迟确认不了停止，而界面一旦停留在「回复进行中」，输入框与「分支 / 重新
    // 生成 / 新会话」三个救援入口会被永久挡住，用户只能重启应用（2026-09-14）。
    // 解锁后若后端其实仍在运行，再次发送会被 ACTIVE_RUN 拒绝并给出明确提示，
    // 比界面永久冻结可诊断得多。
    if (state.cancelConversationId === conversationId) {
      state.chatRunId = '';
      state.runConversationId = '';
      state.runSequence = 0;
      state.runRow = null;
      state.cancelRequested = false;
      state.cancelConversationId = '';
      state.checkRunEligible = false;
      setBusy(false);
    }
    if (failureMessage) toast(`停止任务失败：${failureMessage}`);
  }
}

// 显式刷新页面（内嵌 pywebview 无法 F5 时的退路，浏览器同样可用）。
// URL 中的 token 与对话/工作区状态由后端持久化，reload 后可恢复。
export function reloadPage() {
  if (state.runWatchdogTimer) stopRunWatchdog();
  if (state.elapsedTimer) clearElapsedStatus();
  window.location.reload();
}

// ============================================================
// Skill 快速引用（/ 索引 + 蓝色高亮 + 顶栏点击插入 + 发送解析）
// ============================================================
