// ============================================================
// 04-messages.js —— 拆分自 public/app.js 第 1212-1501 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, api, draggedFileCache, emptyStateElement, escapeHtml, notifyComposerChanged, state, toast } from "./01-core.js";
import { markdown } from "./02-markdown.js";
import { activityMarkup, closeImageLightbox, fileChangesSummaryMarkup, fileUrl, mediaKind, mediaMarkup, mediaTruncatedNotice, reasoningMarkup, remainingAttachments, skillMarkup, sourcesMarkup, toolMarkup, truncationNotice, updateContextComposerLock, updateContextUsage, updateSendButtonState, uploadedFileMarkup, usageMarkup } from "./03-media.js";
import { openConversation, syncCurrentConversation } from "./08-conversations.js";
import { renderPendingFiles } from "./10-upload.js";
import { hideChoiceButtons, sendMessage, showChoiceButtons } from "./12-chat-input.js";
import { hideSkillPopup, renderInputMirror, renderUserContent, resizeTextarea, updateSkillPopup } from "./13-skill-refs.js";
import { hideFilePopup } from "./16-file-refs.js";
// 当前会话所用 Agent 的自定义头像 URL（没有则空串 → 回退到默认的「AI」圆标）。
// 与 currentAgentFixedSkillIds 同口径：会话绑定的 Agent 优先，失效时回退默认 Agent。
export function currentAgentAvatarUrl() {
  const agents = state.bootstrap?.agents || [];
  const conversation = state.conversations.find((item) => item.id === state.conversationId);
  let agent = agents.find((item) => item.id === String(conversation?.agent_id || ''));
  if (!agent) agent = agents.find((item) => item.id === String(state.bootstrap?.default_agent_id || ''));
  const file = String(agent?.avatar || '');
  return file ? `/api/agents/avatar/${encodeURIComponent(file)}` : '';
}

// 内置默认种子模板（设置页留空时回退用它）；占位符：{handoff_path} / {task_count} / {task_list}
export const DEFAULT_CONTEXT_RESET_SEED = [
  '上一段会话已交接，交接文档：{handoff_path}',
  '请先读取该交接文档再继续。',
  '[后台任务] 当前仍有 {task_count} 个任务在运行：',
  '{task_list}',
].join('\n');

// 按分割线标记渲染「新会话」种子消息：没有后台任务时，含占位符的整行自动去掉。
export function contextResetSeedText(info = {}) {
  const template = String(state.bootstrap?.settings?.context_reset_seed_template || '').trim()
    || DEFAULT_CONTEXT_RESET_SEED;
  const tasks = Array.isArray(info.tasks) ? info.tasks : [];
  const taskList = tasks.map((task) => {
    const kind = String(task.kind || '');
    const status = String(task.status || '');
    const suffix = kind || status ? `（${kind}${kind && status ? '，' : ''}${status}）` : '';
    return `- ${String(task.id || '')}${suffix}${task.title ? `：${task.title}` : ''}`;
  }).join('\n');
  return template.split('\n')
    .filter((line) => !((line.includes('{task_count}') || line.includes('{task_list}')) && !tasks.length))
    .map((line) => line
      .replaceAll('{handoff_path}', String(info.handoff_path || ''))
      .replaceAll('{task_count}', String(tasks.length))
      .replaceAll('{task_list}', taskList))
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

// 把种子消息填进输入框——**不自动发送**，由用户确认/编辑后点发送。
export function fillContextResetSeed(info = {}) {
  const text = contextResetSeedText(info);
  const input = $('#messageInput');
  if (!input || !text) return false;
  input.value = text;
  notifyComposerChanged(input);
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  return true;
}

// 「新会话」分割条：标在**某条消息**的 metadata 上（`session_start`），渲染在该消息正下方。
// 语义 = 此线以上的消息不再进入模型上下文，线以下的消息仍在上下文里；聊天记录一条不删。
// 兼容遗留形态：早期版本用独立的 role=session 标记行，这里照旧渲染成同款分隔条。
export function sessionDividerElement(message) {
  const legacyRow = message.role === 'session';
  const info = (message.metadata || {}).session_start || {};
  const at = Number(info.at || message.created_at || 0);
  const time = at ? new Date(at).toLocaleString('zh-CN', { hour12: false }) : '';
  const source = String(info.source || 'manual') === 'tool' ? '模型重置' : '手动';
  const handoff = String(info.handoff_path || '');
  const seedButton = message.id && (handoff || String(info.source || '') === 'tool')
    ? `<button type="button" class="session-divider-seed" data-fill-reset-seed="${escapeHtml(message.id)}" title="把「新会话」种子消息填进输入框（可编辑后再发送）">填入种子消息</button>`
    : '';
  const row = document.createElement('article');
  row.className = 'message-row session-divider';
  row.dataset.messageId = message.id || '';
  row.dataset.sessionDivider = message.id || '';
  // 注意：这里是 DOM 属性赋值（不是 innerHTML），不能 escapeHtml——转义后的 &quot; 会被
  // dataset 原样读出，JSON.parse 直接失败（实测：种子消息里路径变空）。
  row.dataset.resetSeedInfo = JSON.stringify(info);
  row.innerHTML = `
    <div class="session-divider-bar" title="此线以上的消息不再进入模型上下文；下方消息仍保留在上下文中（聊天记录全部保留）">
      <span class="session-divider-line" aria-hidden="true"></span>
      <span class="session-divider-label">新会话${legacyRow ? '开始' : ''} · ${escapeHtml(source)}${time ? ` · ${escapeHtml(time)}` : ''}</span>
      <span class="session-divider-line" aria-hidden="true"></span>
      ${seedButton}
      ${message.id ? '<button type="button" class="session-divider-cancel" data-cancel-session-start title="撤销这条分割线：此线以上的消息重新进入模型上下文">撤销</button>' : ''}
    </div>
    <div class="session-divider-hint">此线以上不再进入模型上下文${handoff ? ` · 交接文档：${escapeHtml(handoff)}` : ''}</div>`;
  return row;
}

// 该消息下方是否要跟一条分割线（遗留标记行由 messageElement 直接渲染，不在此列）。
export function sessionDividerAfter(message) {
  if (!message || message.role === 'session') return null;
  return (message.metadata || {}).session_start ? sessionDividerElement(message) : null;
}

// 就地替换一条消息，并按需在其下方补上分割线（终态事件渲染路径复用）。
export function replaceWithMessage(target, message, temporary = false) {
  const element = messageElement(message, temporary);
  target.replaceWith(element);
  const divider = sessionDividerAfter(message);
  if (divider) element.insertAdjacentElement('afterend', divider);
  return element;
}

export function messageElement(message, temporary = false) {
  if (message.role === 'session') return sessionDividerElement(message);
  const row = document.createElement('article');
  row.className = `message-row ${message.role}`;
  row.dataset.messageId = message.id || '';
  const metadata = message.metadata || {};
  row.__messageMetadata = metadata;
  if (Array.isArray(metadata.attachments)) {
    metadata.attachments.forEach((attachment) => {
      const source = attachment.source || attachment.path;
      if (source && mediaKind(source, attachment.name) === 'image') preloadDraggedFile(source, attachment.name);
    });
  }
  if (message.role === 'user') {
    // 「编辑」（破坏性：截断这条提问及其之后）在前，「分支」（非破坏性）居中，「删除」最后——
    // 编辑最常用，删除破坏性最强（因此排在末尾，且带分级确认框）。
    const actions = message.id
      ? '<div class="message-actions">'
        + '<button data-edit-message title="编辑这条提问并从这里重新发送（其后的消息会被删除）">编辑</button>'
        + '<button data-branch-message title="从这条消息分支到新会话继续">分支</button>'
        + '<button data-delete-message title="删除这条提问（可只删这一条，或连同 AI 回复整轮删除；10 秒内可撤销）">删除</button>'
        + '</div>'
      : '';
    row.innerHTML = `<div class="message-body">${renderUserContent(metadata.display_content || message.content)}${uploadedFileMarkup(metadata.attachments)}${actions}</div>`;
  } else {
    const abortedBadge = metadata.aborted
      ? '<span class="aborted-badge">已中止</span>'
      : metadata.partial ? '<span class="aborted-badge">未完成</span>' : '';
    const activity = Array.isArray(metadata.activity) ? metadata.activity : [];
    const activityHtml = activity.length ? activityMarkup(activity) : '';
    const activityHasProse = activity.some((item) => item && item.type === 'prose');
    const reasoningToolHtml = activityHtml || (reasoningMarkup(metadata.reasoning) + toolMarkup(metadata.tool_runs));
    // 当 activity 已内嵌正文（prose 条目）时，正文按时间交错展示，不再在末尾重复渲染；
    // 末尾的 answer-content 仅保留用于复制/检索（隐藏），避免与时间线重复。
    const hideBottomContent = activityHasProse && !temporary;
    // 媒体就地内嵌在工具调用处（toolRunMarkup 自带）；末尾网格只渲染"没有就地归属"的
    // 附件——新消息的附件都在 run.media 里（集合命中→不重复渲染），旧会话没有 run.media
    // （集合为空→末尾网格照旧），两条渲染路径互不打架。
    const bottomAttachments = remainingAttachments(metadata);
    // 自定义头像：用该 Agent 的会话把默认「AI」圆标换成上传的图片（已中心裁切成正方形）。
    const avatarUrl = currentAgentAvatarUrl();
    const avatarHtml = avatarUrl
      ? `<img class="message-avatar message-avatar-img" src="${escapeHtml(avatarUrl)}" alt="">`
      : '<div class="message-avatar">AI</div>';
    // 「新会话」分割线入口：紧挨「复制」右侧（只有已落库的完整回复才有 id，流式临时气泡不给）。
    const sessionButton = (!temporary && message.id)
      ? `<button data-session-start-after="${escapeHtml(message.id)}" title="在这条回复之后划一条分割线：此线以上的消息不再进入模型上下文（下方消息仍在上下文中，聊天记录全部保留）">新会话</button>`
      : '';
    // 「重新生成」入口：夹在「复制」与「新会话」之间（操作区顺序 复制 → 重新生成 → 新会话）。
    // 显示条件与「新会话」同口径（!temporary && message.id），因此「已中止 / 未完成」的回复同样有按钮
    // ——那正是最常见的真实场景。
    const regenerateButton = (!temporary && message.id)
      ? `<button data-regenerate-message="${escapeHtml(message.id)}" title="用同一条提问重新生成这条回复（前面的对话历史保持不变）">重新生成</button>`
      : '';
    // 「删除」夹在「重新生成」与「新会话」之间（操作区顺序 复制 → 重新生成 → 删除 → 新会话）。
    const deleteButton = (!temporary && message.id)
      ? `<button data-delete-message title="删除这条回复（保留提问；10 秒内可撤销）">删除</button>`
      : '';
    row.innerHTML = `
      ${avatarHtml}
      <div class="message-card">
        <div class="message-body">
          ${skillMarkup(metadata.skills)}
          ${hideBottomContent ? abortedBadge : ''}
          ${reasoningToolHtml}
          ${temporary ? '<div class="run-activity activity">正在准备</div>' : ''}
          <div class="answer-content" data-raw="" ${hideBottomContent ? 'style="display:none"' : ''}>${temporary ? '' : abortedBadge + markdown(message.content)}</div>
          ${temporary ? '' : truncationNotice(metadata.truncated)}
          ${temporary ? '' : sourcesMarkup(metadata.sources)}
          ${mediaMarkup(bottomAttachments)}
          ${bottomAttachments.length ? mediaTruncatedNotice(metadata.attachments_truncated) : ''}
          ${temporary ? '' : fileChangesSummaryMarkup(metadata.files)}
          ${temporary ? '' : usageMarkup({ ...(metadata.usage || {}), performance: metadata.performance || metadata.usage?.performance }, message.created_at)}
          ${temporary ? '' : `<div class="message-actions"><button data-copy-message>复制</button>${regenerateButton}${deleteButton}${sessionButton}</div>`}
        </div>
      </div>`;
  }
  return row;
}

export function preloadDraggedFile(source, name = '') {
  const url = new URL(fileUrl(source), location.href).href;
  if (draggedFileCache.has(url)) return;
  fetch(url).then((response) => response.ok ? response.blob() : Promise.reject(new Error('image fetch failed')))
    .then((blob) => draggedFileCache.set(url, new File([blob], name || 'image' + (blob.type ? '.' + blob.type.split('/')[1] : ''), { type: blob.type })))
    .catch(() => {});
}

// 编辑时把完整的底部 composer-wrap 原节点移动到用户气泡中。这样输入框、@/Skill
// 弹层、附件列表、粘贴/拖放上传和发送按钮都继续使用原有事件绑定。
// 同一时刻最多一个编辑框，退出前必须先把原节点归位。
//
// resume = true 是「重渲染前的归位」：只把节点搬回底部，**保留 stash 与占位锚点**（渲染后要按
// `[data-message-id]` 复挂回新生成的气泡），既不还原草稿也不清 stash。原因见 renderMessages：
// replaceChildren 会连坐销毁搬进气泡的 composer-wrap，所以必须先归位、再渲染、最后复挂。
function restoreInlineComposer({ restoreDraft = false, resume = false } = {}) {
  const stash = state.editComposerStash;
  const wrap = document.querySelector('.composer-wrap.is-inline-edit') || stash?.wrap;
  if (!stash || !wrap) return;
  const parent = stash.parent;
  if (parent) {
    if (stash.nextSibling && stash.nextSibling.parentNode === parent) parent.insertBefore(wrap, stash.nextSibling);
    else parent.appendChild(wrap);
  }
  wrap.hidden = false;
  wrap.classList.remove('is-inline-edit');
  wrap.querySelector('.edit-composer-header')?.remove();
  if (resume) return;
  if (stash.placeholder && stash.placeholder.parentNode) stash.placeholder.remove();
  if (restoreDraft) {
    state.pendingFiles = (stash.files || []).map((file) => ({ ...file }));
    const input = $('#messageInput');
    if (input) input.value = String(stash.text || '');
    renderPendingFiles();
    resizeTextarea();
    renderInputMirror();
    updateSkillPopup();
    notifyComposerChanged(input);
  }
  state.editComposerStash = null;
}

function applyEditingState(row) {
  const editing = Boolean(row);
  if (!editing) {
    const previousRow = state.editComposerStash?.row;
    restoreInlineComposer();
    previousRow?.classList.remove('is-editing');
  }
  state.editingMessageId = editing ? String(row.dataset.messageId || '') : '';
  row?.classList.toggle('is-editing', editing);
  document.body.classList.toggle('is-editing-message', editing);
  updateContextComposerLock(state.chatBusy);
}

// 找到当前移动到消息气泡里的完整输入区。
function activeEditPair() {
  const textarea = $('#messageInput');
  const row = textarea ? textarea.closest('.message-row') : null;
  return state.editingMessageId && textarea && row ? { textarea, row } : null;
}

// 确认编辑：先保存编辑态附件，再把 composer 归位，随后交给共用重发。
function submitEdit(row, textarea) {
  const value = String(textarea?.value ?? '');
  hideSkillPopup();
  hideFilePopup();
  const attachments = (state.pendingFiles || []).filter((file) => file && file.path)
    .map(({ name, path, size, thumb_path }) => ({ name, path, size, thumb_path }));
  restoreInlineComposer();
  row.classList.remove('is-editing');
  applyEditingState(null);
  confirmEditMessage(row, value, attachments);
}

// 取消编辑：恢复进入编辑前的底部草稿与附件，再重新渲染当前会话。
export function cancelActiveEdit() {
  const row = state.editComposerStash?.row;
  hideSkillPopup();
  hideFilePopup();
  restoreInlineComposer({ restoreDraft: true });
  row?.classList.remove('is-editing');
  applyEditingState(null);
  if (state.conversationId) openConversation(state.conversationId);
}

// 底部「发送」按钮在编辑态下的回调：不发新消息，改为确认上面的编辑框。
export function confirmActiveEdit() {
  const pair = activeEditPair();
  if (!pair) {
    applyEditingState(null);
    return false;
  }
  submitEdit(pair.row, pair.textarea);
  return true;
}

// 把 composer-wrap 原节点挂进 row 的气泡并回填文字/附件。「首次进入编辑」与「重渲染后复挂」
// 共用此处，避免两条路径逻辑漂移（复挂漏一步就会出现"输入框在、附件没了/按钮没变"半残状态）。
// wrap 必须由调用方传入：进入编辑后底部还留着一个 `composer-wrap edit-placeholder` 空锚点，
// 且它被插在真 composer **前面**，再查一次 `.composer-wrap` 会抓到那个空 div（派生的坑）。
// 复挂时 stash 必须还在：把它的 row 指向新生成的那一行，取消/确认才认得出新气泡。
function mountEditComposer(row, { wrap, text = '', files = [] } = {}) {
  const body = row?.querySelector('.message-body');
  const textarea = $('#messageInput');
  if (!body || !wrap || !textarea) return false;
  body.replaceChildren();
  const header = document.createElement('div');
  header.className = 'edit-composer-header';
  header.innerHTML = '<span>正在编辑此消息</span><button type="button" class="control-button" data-edit-cancel>取消</button>';
  body.append(wrap, header);
  wrap.classList.add('is-inline-edit');
  state.pendingFiles = (files || []).map((file) => ({ ...file }));
  textarea.value = String(text ?? '');
  if (state.editComposerStash) state.editComposerStash.row = row;
  renderPendingFiles();
  applyEditingState(row);
  resizeTextarea();
  renderInputMirror();
  notifyComposerChanged(textarea);
  header.querySelector('[data-edit-cancel]').addEventListener('click', cancelActiveEdit);
  return true;
}

export function startEditMessage(row) {
  if (!row) return;
  // #composerForm：移动真实表单节点，不复制它，确保既有事件监听与 @/附件行为全部保留。
  if (state.editingMessageId) cancelActiveEdit();
  const body = row.querySelector('.message-body');
  // 排除 edit-placeholder 占位锚点（它也是 .composer-wrap，且在文档顺序里排在前面）。
  const wrap = document.querySelector('.composer-wrap:not(.edit-placeholder)');
  if (!body || !wrap) return;
  // 提取纯文本内容（不含附件标记）。带 /ref 引用的消息优先回填原始 display_content（含引用的原文），
  // 否则退到 DOM 文本/rawContent。
  const displayContent = row.__messageMetadata?.display_content;
  const textContent = body.childNodes[0]?.textContent ?? body.textContent;
  const currentText = (displayContent !== undefined && displayContent !== '')
    ? displayContent
    : (row.dataset.rawContent || textContent.trim());
  const attachments = row.__messageMetadata?.attachments || [];
  const placeholder = document.createElement('div');
  placeholder.className = 'composer-wrap edit-placeholder';
  placeholder.hidden = true;
  wrap.parentNode?.insertBefore(placeholder, wrap);
  state.editComposerStash = {
    wrap,
    row,
    placeholder,
    parent: wrap.parentNode,
    nextSibling: wrap.nextSibling,
    text: $('#messageInput')?.value || '',
    files: (state.pendingFiles || []).map((file) => ({ ...file })),
  };
  row.dataset.rawContent = currentText;
  const files = attachments.map((file) => ({ ...file, existingAttachment: true }));
  if (!mountEditComposer(row, { wrap, text: currentText, files })) return;
  const textarea = $('#messageInput');
  textarea.focus();
  textarea.setSelectionRange(textarea.value.length, textarea.value.length);
}

// 编辑被强制结束（被编辑的消息没了 / 换了会话）：把「进入编辑前暂存的底部草稿」还回底部输入框。
// 这是与旧行为的关键差别——以前是静默丢弃，用户改到一半的内容直接消失。
function exitEditWithDraft() {
  if (!state.editingMessageId && !state.editComposerStash) return;
  const stash = state.editComposerStash;
  const row = stash?.row;
  const hadDraft = Boolean(String(stash?.text || '').trim()) || Boolean((stash?.files || []).length);
  hideSkillPopup();
  hideFilePopup();
  restoreInlineComposer({ restoreDraft: true });
  row?.classList.remove('is-editing');
  applyEditingState(null);
  toast(hadDraft ? '编辑已取消，草稿已还原到底部输入框' : '编辑已取消');
}

// 重渲染前的编辑态分流。返回 null（本次不复挂）或复挂快照。
//   · 同会话刷新（轮询 syncCurrentConversation / 重新打开当前会话）且被编辑消息仍在
//     → 只归位、保 stash，等渲染后把 composer 复挂回新气泡：编辑框、改过的字、附件全都在。
//   · 被编辑消息已不存在（被截断/删除）或换了会话 → 退出编辑并还原草稿（exitEditWithDraft）。
// **顺序是硬要求**：replaceChildren 会连坐销毁搬进气泡的 composer-wrap，必须先归位再渲染。
function prepareEditRerender(list, switched) {
  if (!state.editingMessageId) return null;
  const stash = state.editComposerStash;
  const textarea = $('#messageInput');
  const messageId = state.editingMessageId;
  const stillThere = !switched && list.some((message) => String(message?.id || '') === messageId);
  if (stillThere && stash && textarea) {
    const snapshot = {
      messageId,
      text: String(textarea.value ?? ''),
      files: (state.pendingFiles || []).map((file) => ({ ...file })),
      selection: [textarea.selectionStart, textarea.selectionEnd],
      focused: document.activeElement === textarea,
    };
    restoreInlineComposer({ resume: true });
    return snapshot;
  }
  exitEditWithDraft();
  return null;
}

// 渲染后把 composer 复挂回（可能是新生成的）那条消息行。
// 懒加载窗口里没有它时先向前扩窗口；实在拿不到就按「退出编辑」兜底——草稿仍不会丢。
function resumeEditComposer(snapshot) {
  if (!snapshot) return;
  const container = $('#messages');
  const findRow = () => {
    const rows = container ? container.querySelectorAll('.message-row[data-message-id]') : [];
    for (const row of rows) if (row.dataset.messageId === snapshot.messageId) return row;
    return null;
  };
  let row = findRow();
  if (!row) {
    const index = (state.messages || []).findIndex((message) => String(message?.id || '') === snapshot.messageId);
    if (index >= 0 && ensureMessageRendered(index)) row = findRow();
  }
  if (!row || !state.editComposerStash) {
    exitEditWithDraft();
    return;
  }
  if (!mountEditComposer(row, { wrap: state.editComposerStash.wrap, text: snapshot.text, files: snapshot.files })) {
    exitEditWithDraft();
    return;
  }
  const textarea = $('#messageInput');
  if (textarea && snapshot.focused) {
    textarea.focus();
    try { textarea.setSelectionRange(snapshot.selection[0], snapshot.selection[1]); } catch (_) { /* 选区越界时忽略 */ }
  }
}

// 共用重发：截断到某条用户消息、恢复它的附件、回到输入框并发送。
// 「编辑后确认」与「重新生成」截断位置完全相同（都落在那条 user 消息上），只是文本不同，因此共用此处：
//   - 编辑 → text 用编辑框里的新内容
//   - 重新生成 → text 用该提问的原文（原样重发，不改字）
// 截断走 /api/messages/edit：它只接受 role == "user" 的 id，随即删除该消息及其之后所有消息，并回传原附件。
// 放行口径与 sendChatMessage/submit_chat 一致：**文字与可用附件至少有一个**——纯附件轮次
// （只发文件/图片、不写字）同样可以重发；只传空文字又无附件才拒绝。
export async function resendFromUserMessage(userMessageId, text, { successText = '已从该消息重开，编辑点之前的上下文将复用缓存', errorPrefix = '重发', attachments = [], attachmentsOverride = null } = {}) {
  // 编辑/重发最终仍走同一条输入管线：notifyComposerChanged(input) 会刷新镜像、引用弹层与发送状态。
  const content = String(text ?? '').trim();
  const knownAttachments = Array.isArray(attachments) ? attachments : [];
  if (!content && !knownAttachments.length) {
    toast('内容不能为空');
    return false;
  }
  const conversationId = state.conversationId;
  if (!userMessageId || !conversationId) return false;
  try {
    const result = await api('/api/messages/edit', {
      method: 'POST',
      body: { conversation_id: conversationId, message_id: userMessageId },
    });
    toast(successText);
    // 恢复原消息的附件，供重发使用。字段口径与「分支」一致（含 thumb_path）：
    // 缺 thumb_path 时渲染层会退化成推导 `<path>_thumb.webp`，一旦缩略图不在同目录就 404。
    const restoredAttachments = Array.isArray(attachmentsOverride) ? attachmentsOverride : (result.attachments || []);
    state.pendingFiles = restoredAttachments.map((f) => ({
      name: f.name,
      path: f.path,
      size: f.size,
      thumb_path: f.thumb_path,
    }));
    renderPendingFiles();
    // 截断后重新渲染会话（被截断的消息已从历史消失）
    await openConversation(conversationId);
    // 填入内容并重发（纯附件轮次 text 为空，sendChatMessage 只靠 pendingFiles 放行）
    const input = $('#messageInput');
    input.value = content;
    resizeTextarea();
    renderInputMirror();
    updateSkillPopup();
    notifyComposerChanged(input);
    await sendMessage();
    return true;
  } catch (error) {
    toast(`${errorPrefix}失败：${error.message}`);
    if (state.conversationId) openConversation(state.conversationId);
    return false;
  }
}

export async function confirmEditMessage(row, newText, editedAttachments = null) {
  const text = newText.trim();
  const messageId = row.dataset.messageId;
  if (!messageId || !state.conversationId) return;
  const attachments = Array.isArray(editedAttachments)
    ? editedAttachments
    : ((row.__messageMetadata || {}).attachments || []);
  // 空文字只在同时也没有附件时才拒绝（纯附件轮次同样可编辑重发）。
  await resendFromUserMessage(messageId, text, {
    successText: '已从该消息重开，编辑点之前的上下文将复用缓存',
    errorPrefix: '编辑',
    attachments,
    attachmentsOverride: editedAttachments,
  });
}

// 「重新生成」：对某条已落库的 AI 回复，用它的上一条用户提问原样重发（不改字）。
// 截断点与「编辑」相同——都从那条 user 消息处截断，因此反复点击不会累积重复提问，
// 且 U1..A(N-1) 字节级不动 → 前缀缓存照常命中（这是重发最省钱的理由）。
export async function regenerateMessage(assistantMessageId) {
  if (state.chatRunId || state.abortController) {
    toast('请先等待当前回答结束或停止后再重新生成');
    return;
  }
  if (!state.conversationId) return;
  const messages = state.messages || [];
  const index = messages.findIndex((m) => String(m.id) === String(assistantMessageId));
  if (index < 0) {
    toast('重新生成失败：消息不存在');
    return;
  }
  // 往前找最近一条用户消息（session 分割线等中间消息自动跳过）。
  // 从 state.messages 而非 DOM 推导：懒加载只渲染窗口内的消息，DOM 里未必有那条提问。
  let userMessage = null;
  for (let i = index - 1; i >= 0; i -= 1) {
    if (messages[i] && messages[i].role === 'user') { userMessage = messages[i]; break; }
  }
  if (!userMessage || !userMessage.id) {
    toast('重新生成失败：找不到对应的提问');
    return;
  }
  const metadata = userMessage.metadata || {};
  // 原文取 display_content（用户原样，含 /ref）；content 是剥离引用、解析过 @ 的模型可见版，
  // 用错会把引用悄悄弄丢。
  const text = String(metadata.display_content || userMessage.content || '');
  // 这条回复之后还有多少条消息（截断它们时会一并删除），以及输入框是否已有草稿。
  const trailing = messages.slice(index + 1).length;
  const input = $('#messageInput');
  const hasDraft = Boolean(input && String(input.value || '').trim());
  if (trailing > 0 || hasDraft) {
    const lines = ['重新生成这条回复？', '', '前面的对话历史保持不变。'];
    if (trailing > 0) lines.splice(2, 0, `这条回复之后还有 ${trailing} 条消息，会一并删除。`);
    if (hasDraft) lines.push('', '输入框里的草稿会被替换成这条提问。');
    if (!window.confirm(lines.join('\n'))) return;
  }
  await resendFromUserMessage(userMessage.id, text, {
    successText: '正在重新生成（复用前缀缓存）',
    errorPrefix: '重新生成',
    attachments: metadata.attachments || [],
  });
}

// 从某条 user 消息分支：新开一个会话，复制分支点之前的历史，并把分支消息预填进输入框。
// 非破坏性（原会话保留）；运行中不显示分支按钮（见 CSS .conversation-running），此处兜底拦截。
export async function branchMessage(row) {
  if (state.chatRunId || state.abortController) {
    toast('请先等待当前任务结束或停止后再分支');
    return;
  }
  const messageId = row?.dataset.messageId;
  const sourceId = state.conversationId;
  if (!messageId || !sourceId) {
    toast('分支失败：消息或会话不存在');
    return;
  }
  try {
    const result = await api(`/api/conversations/${sourceId}/branch`, {
      method: 'POST',
      body: { message_id: messageId },
    });
    const newConversation = result.conversation || {};
    const branch = result.branch_message || {};
    // 让新会话进入侧栏列表
    const index = state.conversations.findIndex((c) => c.id === newConversation.id);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...newConversation };
    else state.conversations.unshift(newConversation);
    // 切到新会话（复用 openConversation 的完整装载逻辑）
    await openConversation(newConversation.id);
    // 预填分支消息内容（原样，含 /ref），并恢复其附件为待上传（走现有输入逻辑）
    hideSkillPopup();
    const input = $('#messageInput');
    input.value = branch.display_content || branch.content || '';
    resizeTextarea();
    renderInputMirror();
    state.pendingFiles = (branch.attachments || []).map((f) => ({ name: f.name, path: f.path, size: f.size, thumb_path: f.thumb_path }));
    renderPendingFiles();
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    toast('已从该消息分支到新会话');
  } catch (error) {
    toast(`分支失败：${error.message}`);
  }
}

// ---- 删除单条消息 / 整轮 + 撤销 ----
// 语义：删除 = 从模型上下文剔除 + 界面移除。缓存结论（确认框按此分级提示）：
//   · 删除点**之前**的内容逐字节不变 → 前缀缓存照常命中；
//   · 之后的内容一次性重缓存（与「分割线换前缀」同模式，比「编辑」丢 tail 便宜）；
//   · 删尾部零成本；撤销按原字节插回后旧缓存可重新命中（快照带原始 metadata 文本）。
// 确认框刻意不用 window.confirm：它给不出「只删这一条 / 整轮删除」两个粒度，
// 而且原生弹窗无法截图留证（前端变更要求附截图）。
const UNDO_WINDOW_MS = 10000;
let deleteUndoState = null;   // { conversationId, removed, timer } —— 只存内存，刷新即失效

function askDeleteScope({ isUser, notes }) {
  const dialog = $('#messageDeleteDialog');
  if (!dialog) return Promise.resolve('');
  const list = $('#messageDeleteNotes');
  const singleButton = $('#messageDeleteSingle');
  const turnButton = $('#messageDeleteTurn');
  const cancelButton = $('#messageDeleteCancel');
  if (list) list.innerHTML = (notes || []).map((line) => `<li>${escapeHtml(line)}</li>`).join('');
  // assistant 消息只有「删这一条」一种粒度：删掉提问比删掉回答危险得多，不在此开放。
  // 注意两个按钮**不能都藏**（曾写成 singleButton.hidden = !isUser，结果回复一条都删不掉）：
  // 「仅删这一条」对 user / assistant 都是合法动作，「整轮」才只对 user 开放。
  if (singleButton) singleButton.hidden = false;
  if (turnButton) turnButton.hidden = !isUser;
  return new Promise((resolve) => {
    let settled = false;
    const cleanup = () => {
      singleButton?.removeEventListener('click', onSingle);
      turnButton?.removeEventListener('click', onTurn);
      cancelButton?.removeEventListener('click', onCancel);
      dialog.removeEventListener('close', onCancel);
    };
    const done = (value) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (dialog.open && typeof dialog.close === 'function') dialog.close();
      resolve(value);
    };
    function onSingle() { done('single'); }
    function onTurn() { done('turn'); }
    function onCancel() { done(''); }
    singleButton?.addEventListener('click', onSingle);
    turnButton?.addEventListener('click', onTurn);
    cancelButton?.addEventListener('click', onCancel);
    dialog.addEventListener('close', onCancel);
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
  });
}

function showDeleteUndo(conversationId, removed, mode) {
  const bar = $('#undoBar');
  if (!bar || !removed.length) return;
  hideDeleteUndo();
  const text = $('#undoBarText');
  if (text) {
    text.textContent = mode === 'turn'
      ? `已删除整轮（${removed.length} 条消息）`
      : '已删除 1 条消息';
  }
  deleteUndoState = {
    conversationId,
    removed,
    timer: window.setTimeout(() => hideDeleteUndo(), UNDO_WINDOW_MS),
  };
  bar.hidden = false;
  // 重排一次再显示，确保每次都能重播淡入与进度条动画（连删两条时尤其明显）。
  void bar.offsetWidth;
  bar.classList.add('show');
}

function hideDeleteUndo() {
  const bar = $('#undoBar');
  if (deleteUndoState?.timer) window.clearTimeout(deleteUndoState.timer);
  deleteUndoState = null;
  if (!bar) return;
  bar.classList.remove('show');
  window.setTimeout(() => { if (!deleteUndoState) bar.hidden = true; }, 220);
}

// 撤销：按快照把消息原样插回（后端逐字节还原 id/content/metadata/created_at）。
export async function undoLastDelete() {
  const pending = deleteUndoState;
  if (!pending) return;
  hideDeleteUndo();
  try {
    await api('/api/messages/restore', {
      method: 'POST',
      body: { conversation_id: pending.conversationId, messages: pending.removed },
    });
    if (state.conversationId === pending.conversationId) await syncCurrentConversation();
    toast('已恢复删除的消息');
  } catch (error) {
    toast(`撤销失败：${error.message}`);
  }
}

export async function deleteMessageFlow(row) {
  if (state.chatRunId || state.abortController) {
    toast('请先等待当前回答结束或停止后再删除');
    return;
  }
  const conversationId = state.conversationId;
  const messageId = row?.dataset.messageId || '';
  if (!conversationId || !messageId) return;
  const messages = state.messages || [];
  const index = messages.findIndex((message) => String(message?.id || '') === messageId);
  if (index < 0) {
    toast('删除失败：消息不存在');
    return;
  }
  const message = messages[index];
  const isUser = message.role === 'user';
  const metadata = message.metadata || {};
  const trailing = messages.slice(index + 1).length;
  const hasImages = Array.isArray(metadata.attachments) && metadata.attachments.some(
    (item) => item && mediaKind(item.source || item.path, item.name) === 'image'
  );
  const notes = [
    isUser
      ? '整轮删除会同时移除这条提问与其后紧随的 AI 回复；「仅删这一条」只移除提问。'
      : '这条回复会从对话与模型上下文中移除，它的提问会保留。',
  ];
  if (trailing > 0) {
    notes.push(`该消息之后还有 ${trailing} 条消息，删除后下一轮对话会重建一次缓存（一次性成本）。`);
    if (hasImages) notes.push('图片会随后续消息重新发送。');
  } else {
    notes.push('这条消息在末尾：删除不会影响任何已有缓存。');
  }
  if (metadata.session_start) {
    notes.push('这条消息带有「新会话」分割线：删除后它之前的内容会重新进入模型上下文。');
  }
  const mode = await askDeleteScope({ isUser, notes });
  if (!mode) return;
  try {
    const result = await api('/api/messages/delete', {
      method: 'POST',
      body: { conversation_id: conversationId, message_id: messageId, mode },
    });
    const removed = Array.isArray(result.removed) ? result.removed : [];
    if (state.conversationId === conversationId) await syncCurrentConversation();
    showDeleteUndo(conversationId, removed, result.mode || mode);
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

// 在某条 AI 回复之后落一条「新会话」分割线：此线以上的消息不再进入模型上下文。
export async function startNewSession(afterMessageId) {
  if (state.abortController || state.chatRunId) {
    toast('请先等待当前回答结束或停止后再划分割线');
    return;
  }
  const conversationId = state.conversationId;
  if (!conversationId || !afterMessageId) {
    toast('请先打开一个对话');
    return;
  }
  if (!window.confirm('在这条回复之后划一条分割线？\n\n此线以上的消息不再进入模型上下文；线以下的消息仍保留在上下文中。聊天记录全部保留，可随时撤销。')) return;
  try {
    await api(`/api/conversations/${conversationId}/session_start`, {
      method: 'POST',
      body: { after_message_id: afterMessageId, source: 'manual' },
    });
    await syncCurrentConversation();
    toast('已划出分割线：此线以上的内容不再进入模型上下文，上下文占用已清零，可直接继续');
  } catch (error) {
    toast(`划分割线失败：${error.message}`);
  }
}

// 撤销分割线：此线以上的消息重新进入模型上下文。
export async function cancelSessionStart(messageId) {
  if (!messageId) return;
  if (!window.confirm('撤销这条分割线？\n\n此线以上的消息会重新进入模型请求。')) return;
  try {
    await api(`/api/session_start/${encodeURIComponent(messageId)}`, { method: 'DELETE' });
    await syncCurrentConversation();
    toast('已撤销分割线');
  } catch (error) {
    toast(`撤销失败：${error.message}`);
  }
}

export let stickToBottom = true;

// ESM 下 import 绑定只读：跨文件写入经 setter（读点保持直接引用不变）。
export function setStickToBottom(value) { stickToBottom = value; }

export function isNearBottom(threshold = 80) {
  const messages = $('#messages');
  if (!messages) return true;
  return (messages.scrollHeight - messages.scrollTop - messages.clientHeight) < threshold;
}

// 默认滚动：只在用户仍停留在底部（跟随）时才自动滚到最新内容；
// 用户滚轮上滑阅读历史时，后续任何 delta/工具事件都不再把页面强行拉回底部。
// 一律经 withInstantScroll 瞬时定位：流式期间这里每帧都会被调到，动画化的平滑滚动
// 会让容器长期处于"追赶最新内容"的状态（体感上的持续晃动）。
export function scrollToBottom() {
  if (!stickToBottom) return;
  const messages = $('#messages');
  if (messages) withInstantScroll(messages, () => { messages.scrollTop = messages.scrollHeight; });
}

// 强制滚到底部：用于确实需要展示最新内容的地方（渲染后一次性定位）。
export function forceScrollToBottom() {
  const messages = $('#messages');
  if (messages) withInstantScroll(messages, () => { messages.scrollTop = messages.scrollHeight; });
}

// 流式 markdown 渲染的自适应节流。
// 每次渲染都是「整段 raw 重跑 markdown + 整体 innerHTML」，单次成本随回复长度线性增长，
// 而事件频率大致恒定（token 速率不变）⇒ 总成本随长度呈 O(n²)，长回复后半段明显掉帧。
// 所以按当前累积长度拉长节流间隔：短回复保持 40ms（跟手），每累积 4000 字 +50ms，
// 240ms 封顶（间隔再大就肉眼可见"一段一段蹦"，得不偿失）。
export const STREAMING_RENDER_MIN_MS = 40;
const STREAMING_RENDER_STEP_CHARS = 4000;
const STREAMING_RENDER_STEP_MS = 50;
const STREAMING_RENDER_MAX_MS = 240;

export function streamingRenderDelay(raw) {
  const length = String(raw || '').length;
  const steps = Math.floor(length / STREAMING_RENDER_STEP_CHARS);
  return Math.min(STREAMING_RENDER_MAX_MS, STREAMING_RENDER_MIN_MS + steps * STREAMING_RENDER_STEP_MS);
}

export function scheduleStreamingMarkdown(element, raw) {
  if (!element) return;
  element.dataset.raw = raw;
  if (element.dataset.renderScheduled === '1') return;
  element.dataset.renderScheduled = '1';
  window.setTimeout(() => {
    element.dataset.renderScheduled = '0';
    element.innerHTML = markdown(element.dataset.raw || '');
    scrollToBottom();
  }, streamingRenderDelay(raw));
}

// 把“中途正文”作为独立兄弟块插到 answer 之前，按时间顺序与思考块/工具块交错显示。
// 只有当 answer 的前一个兄弟元素已经是流式正文块时才复用；一旦中间插入了工具/思考块，
// 之后的新正文会生成新的独立块，从而保持“思考→正文→工具→思考→正文…”的顺序，
// 而不是把所有正文统一累积到末尾的 answer-content。
export function getStreamingProseSegment(row, answer) {
  if (!answer) return null;
  const prev = answer.previousElementSibling;
  if (prev && prev.classList && prev.classList.contains('stream-prose')) {
    return prev;
  }
  const seg = document.createElement('div');
  seg.className = 'stream-prose';
  answer.before(seg);
  return seg;
}

// 首个工具出现时，把之前累计在底部（answer-content）的正文移到内联的正文块，
// 让它紧跟在该工具之前，与思考块/后续工具按时间交错，而不是停在末尾。
export function moveBottomProseInline(row, answer) {
  if (!answer) return;
  const bottomRaw = answer.dataset.raw || '';
  if (!bottomRaw.trim()) return;
  const seg = getStreamingProseSegment(row, answer);
  if (!seg) return;
  seg.dataset.raw = bottomRaw;
  seg.innerHTML = markdown(bottomRaw);
  answer.dataset.raw = '';
  answer.replaceChildren();
}

export function firstTurnCardMarkup(firstTurn) {
  const systemText = String((firstTurn && (firstTurn.system || firstTurn.prompt)) || '');
  if (!systemText) return '';
  const tools = Array.isArray(firstTurn.tools) ? firstTurn.tools : [];
  const skills = Array.isArray(firstTurn.skills) ? firstTurn.skills : [];
  const options = firstTurn.options || {};
  const toolChips = tools.map((tool) => `<span class="ft-chip ft-tool-chip" title="${escapeHtml(tool.description || '')}">${escapeHtml(tool.name || '')}</span>`).join('');
  const skillChips = skills.map((skill) => `<span class="ft-chip">${escapeHtml(skill.name || skill.id || '')}</span>`).join('');
  const optionText = [
    options.temperature != null ? `temperature ${options.temperature}` : '',
    options.max_tokens != null ? `max_tokens ${options.max_tokens}` : '',
    options.context_size != null ? `context ${options.context_size}` : '',
    options.reasoning_effort ? `reasoning ${options.reasoning_effort}` : '',
  ].filter(Boolean).join(' · ');
  return `<details class="first-turn-card">
    <summary>首次请求上下文 · ${escapeHtml(firstTurn.agent_name || 'Agent')} · ${escapeHtml(firstTurn.model_key || '')} · 工具 ${tools.length} 个</summary>
    <div class="ft-body">
      <div class="ft-meta">${skillChips ? `技能：${skillChips}` : '技能：无'}</div>
      <div class="ft-tools">${toolChips || '<span class="ft-note">（未启用工具）</span>'}</div>
      ${optionText ? `<div class="ft-options">生成参数：${escapeHtml(optionText)}</div>` : ''}
      <details class="ft-section">
        <summary>系统提示词（发送给模型的 system 原文）</summary>
        <pre class="ft-prompt">${escapeHtml(systemText)}</pre>
      </details>
      ${tools.length ? `<details class="ft-section">
        <summary>工具定义（${tools.length} 个，JSON）</summary>
        <pre class="ft-prompt">${escapeHtml(JSON.stringify(tools, null, 2))}</pre>
      </details>` : ''}
    </div>
  </details>`;
}

// 「首次请求上下文」折叠卡的就地刷新（终态事件后调用）：数据源与 openConversation 一致
// （GET /api/conversations/{id}/first_turn）。只增删/替换折叠卡节点、不整页重渲染，
// 避免打断已完成消息行的 DOM；renderMessages 也复用之，保证两条渲染路径同序
// （empty → 折叠卡 → 消息）。
export async function refreshFirstTurnCard(conversationId = state.conversationId) {
  if (!conversationId || conversationId !== state.conversationId) return;
  let firstTurn = null;
  try {
    const data = await api(`/api/conversations/${conversationId}/first_turn`);
    const hasSystem = data && typeof data === 'object' && Boolean(data.system || data.prompt);
    firstTurn = hasSystem ? data : null;
  } catch (_) {
    firstTurn = null; // 老会话无此数据 / 接口瞬时失败：静默（与 openConversation 同策略）
  }
  if (conversationId !== state.conversationId) return; // 拉取期间已切换会话：丢弃
  state.firstTurnInfo = firstTurn;
  upsertFirstTurnCard();
}

function upsertFirstTurnCard() {
  const container = $('#messages');
  if (!container) return;
  const existing = container.querySelector('.first-turn-card');
  const markup = state.firstTurnInfo ? firstTurnCardMarkup(state.firstTurnInfo) : '';
  if (!markup) {
    if (existing) existing.remove();
    return;
  }
  const template = document.createElement('template');
  template.innerHTML = markup.trim();
  const card = template.content.firstElementChild;
  if (!card) {
    if (existing) existing.remove();
    return;
  }
  if (existing) existing.replaceWith(card);
  else {
    const empty = $('#emptyState');
    if (empty && empty.parentNode === container) empty.insertAdjacentElement('afterend', card);
    else container.prepend(card);
  }
}

/* ---------- 消息列表懒加载（窗口恒以「轮」为边界） ---------- */

// 打开会话默认渲染最近 N 轮；向上滚动时每次再往前渲染 N 轮。
// 窗口必须以「轮」为边界：一条 AI 回复上的「新会话」分割线属于该轮，
// 不能出现"分割线在窗口内、它的锚点消息在窗口外"这种拆散（用户特别提醒过）。
const LAZY_TURNS_INITIAL = 10;
const LAZY_TURNS_STEP = 10;
const LAZY_TOP_TRIGGER = 240;
// 渲染/程序化滚动会连带触发 scroll 事件：这段时间内不把 scroll 当成"用户滚到顶"，
// 否则打开会话（内容刚填进去、scrollTop 还在 0 附近）就会立刻多渲染一段。
let lazySuppressUntil = 0;

// 轮起点 = 每条 user 消息的下标（首条不是 user 时把 0 也算一个起点）。
function turnStartIndexes(messages) {
  const starts = [];
  (messages || []).forEach((message, index) => {
    if (message?.role === 'user') starts.push(index);
  });
  if (!starts.length || starts[0] !== 0) starts.unshift(0);
  return starts;
}

function clampTurnStart(messages, wanted) {
  let result = 0;
  for (const start of turnStartIndexes(messages)) {
    if (start <= wanted) result = start;
    else break;
  }
  return result;
}

function initialRenderStart(messages) {
  const starts = turnStartIndexes(messages);
  return starts[Math.max(0, starts.length - LAZY_TURNS_INITIAL)];
}

// 一段消息的 DOM 片段（消息 + 紧跟其后的「新会话」分割线，保证两者同进同出）。
function messageRangeFragment(messages, start, end) {
  const fragment = document.createDocumentFragment();
  for (let index = start; index < end; index += 1) {
    const message = messages[index];
    if (!message) continue;
    fragment.append(messageElement(message));
    const divider = sessionDividerAfter(message);
    if (divider) fragment.append(divider);
  }
  markChoicePreviewAnsweredState(fragment, messages);
  return fragment;
}

/**
 * 给正文里的「选择题」静态块标记已答/未答：该 assistant 消息之后出现过 user 消息即为已答。
 * 判定基于完整 messages（懒加载只渲染窗口，索引仍取自全量列表，滚动补渲染时标记不会漂移）。
 */
function markChoicePreviewAnsweredState(fragment, messages) {
  const answered = new Set();
  let seenUser = false;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (!message) continue;
    if (message.role === 'user') { seenUser = true; continue; }
    if (seenUser && message.role === 'assistant') answered.add(String(message.id || ''));
  }
  if (!answered.size) return;
  fragment.querySelectorAll('.choice-preview').forEach((preview) => {
    const row = preview.closest('.message-row[data-message-id]');
    if (row && answered.has(String(row.dataset.messageId || ''))) preview.classList.add('is-answered');
  });
}

// 程序化定位/补偿必须瞬时生效：不能依赖容器的 scroll-behavior（那是全局契约，
// 而这里是"这一刻必须立刻到位"）。即使将来有人把 `smooth` 加回 CSS，经这里写入的
// scrollTop 也不会被动画化——读回值准确，也不会在动画期间连发 scroll 事件干扰懒加载判定。
function withInstantScroll(container, mutate) {
  const previous = container.style.scrollBehavior;
  container.style.scrollBehavior = 'auto';
  try {
    mutate();
  } finally {
    container.style.scrollBehavior = previous;
  }
}

// 向上扩展渲染窗口（预渲染视界外的一段历史），并保持视口内容不跳动。
export function extendRenderedWindow() {
  const container = $('#messages');
  const messages = state.messages || [];
  const start = Number(state.renderStart) || 0;
  if (!container || start <= 0) return false;
  const starts = turnStartIndexes(messages);
  let position = starts.indexOf(start);
  if (position <= 0) position = starts.length;
  const nextStart = starts[Math.max(0, position - LAZY_TURNS_STEP)];
  if (nextStart >= start) return false;
  const beforeHeight = container.scrollHeight;
  const anchor = container.querySelector('.message-row[data-message-id]');
  const fragment = messageRangeFragment(messages, nextStart, start);
  if (anchor) anchor.before(fragment);
  else container.append(fragment);
  state.renderStart = nextStart;
  // 补进来的高度加回 scrollTop：用户正在看的那条消息仍停在原来的位置（瞬时，不动画）。
  withInstantScroll(container, () => {
    container.scrollTop += container.scrollHeight - beforeHeight;
  });
  scheduleTurnRail();
  return true;
}

// 把某条消息所在轮次渲染出来（刻度轨点击未渲染的轮次时用）。
function ensureMessageRendered(messageIndex) {
  let guard = 0;
  while ((Number(state.renderStart) || 0) > messageIndex && guard < 200) {
    if (!extendRenderedWindow()) break;
    guard += 1;
  }
  return (Number(state.renderStart) || 0) <= messageIndex;
}

// 全文搜索命中跳转：把某条消息滚到视口垂直中心并高亮 2 秒（只加 class，不改结构）。
// 定位算法与 scrollToTurn 同口径——不用 scrollIntoView：它会尽量贴边，于是被高亮的那条
// 落在视口最上/最下沿，用户看不出自己到底跳到了哪一条。懒加载窗口里没有它时先向前扩窗口。
export function revealMessage(messageId) {
  const container = $('#messages');
  const wanted = String(messageId || '');
  if (!container || !wanted) return false;
  const messages = state.messages || [];
  const index = messages.findIndex((message) => String(message?.id || '') === wanted);
  if (index < 0) return false;
  if (!ensureMessageRendered(index)) return false;
  let row = null;
  for (const candidate of container.querySelectorAll('.message-row[data-message-id]')) {
    if (candidate.dataset.messageId === wanted) { row = candidate; break; }
  }
  if (!row) return false;
  // 用户是主动跳到历史位置的：关掉"贴底跟随"，否则流式回答的新增量会立刻把他拽回底部。
  setStickToBottom(false);
  const base = container.getBoundingClientRect().top - container.scrollTop;
  const rect = row.getBoundingClientRect();
  const top = rect.top - base - Math.max(0, (container.clientHeight - rect.height) / 2);
  container.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
  row.classList.remove('message-located');
  void row.offsetWidth;   // 连点同一条命中时强制重播动画
  row.classList.add('message-located');
  window.setTimeout(() => row.classList.remove('message-located'), 2000);
  return true;
}

export function renderMessages(messages) {
  const container = $('#messages');
  const empty = emptyStateElement;
  closeImageLightbox();
  const list = Array.isArray(messages) ? messages : [];
  // 切换会话 → 窗口重置为「最近 N 轮」；同一会话刷新（轮询/保存后）→ 保留当前窗口与滚动位置，
  // 否则用户正在往上翻历史时一次轮询就会把他拽回底部。
  const switched = state.messagesConversationId !== String(state.conversationId || '');
  // 编辑态分流：以前这里会无条件把编辑态退掉，于是轮询/后台任务写消息引发的一次同会话刷新
  // 就会静默吃掉编辑现场（文字掉回底部、附件丢失、按钮从「重新发送」变回「发送」）。
  // 现在同会话且被编辑消息还在 → 保现场复挂；只有消息真没了或换了会话才退出（且必还草稿）。
  const editResume = prepareEditRerender(list, switched);
  const keepScroll = !switched && !stickToBottom;
  const previousScrollTop = container.scrollTop;
  const wantedStart = switched ? initialRenderStart(list) : clampTurnStart(list, Number(state.renderStart) || 0);
  state.messages = list;
  state.messagesConversationId = String(state.conversationId || '');
  state.renderStart = list.length ? Math.min(wantedStart, list.length - 1) : 0;
  // 渲染/程序化滚动产生的 scroll 事件不算"用户滚到顶"：这段时间内不触发预渲染。
  lazySuppressUntil = Date.now() + 400;
  // 诊断日志：定位"消息消失"是数据为空还是渲染崩溃
  console.log('[naiba] renderMessages 调用, 消息数=', list.length,
    '渲染起点=', state.renderStart,
    'conversationId=', state.conversationId,
    'roles=', list.map((m) => m.role).join(','));
  // 流式中的 run 行必须活过这次重渲染：它就是「正在进行的那条助手回复」，正文此刻只在
  // DOM 里（run 结束才落库）。此前它被 replaceChildren 连根拔掉，而重渲染后又没人把它挂回来，
  // 于是正在流式输出的回复从屏幕上凭空消失，且此后一直不回来（只有等 run 结束才会重新出现）。
  // 典型触发：后台任务跑着时点任务面板里的某一行 —— 那一步会对当前会话做整体重渲染。
  // 判据收紧到「确有活动流 + 行还挂在消息区 + 属于当前会话 + 库里还没有这轮的**助手**消息」，
  // 避免把别的会话的行误搬过来，也避免在流刚结束、库里已有终稿时多出一条重复气泡。
  // 注意必须限定 role==='assistant'：这一轮的**用户**消息 metadata 里也带同一个 run_id。
  const liveRunRow = (
    state.abortController
    && state.runRow?.isConnected
    && String(state.runConversationId || '') === String(state.conversationId || '')
    && !list.some((item) => item?.role === 'assistant'
      && String((item?.metadata || {}).run_id || '') === String(state.chatRunId || ''))
  ) ? state.runRow : null;
  try {
    container.replaceChildren();
    // 始终保留 empty 在容器中，仅切换 hidden；否则它会被移出 DOM，
    // 导致后续 sendMessage 中 $('#emptyState') 为 null 而崩溃
    empty.hidden = list.length > 0;
    container.append(empty);
    // 首轮上下文折叠卡：固定在最顶部（第一条消息上方），展示第一轮发送给模型的
    // 系统提示词与工具集（默认折叠）。
    upsertFirstTurnCard();
    if (list.length) {
      // 懒加载：只渲染 [renderStart, 末尾) 这一段（窗口恒以「轮」为边界，分割线不会与锚点分离）
      container.append(messageRangeFragment(list, state.renderStart, list.length));
      if (keepScroll) {
        withInstantScroll(container, () => { container.scrollTop = previousScrollTop; });
      } else {
        // 先瞬时定位到底部（smooth 会让几百条消息"慢慢滑"），再在下一帧布局稳定后确认；
        // 用户若已上滑（stickToBottom=false）则不再抢滚动。
        withInstantScroll(container, () => { container.scrollTop = container.scrollHeight; });
        requestAnimationFrame(() => scrollToBottom());
      }
    }
    // 把活动流的那条助手气泡挂回末尾（append 会把同一个节点搬过来，流式内容与滚动状态都保住）。
    if (liveRunRow) container.append(liveRunRow);
    const choiceMessage = pendingChoiceMessage(list);
    const choices = choiceMessage?.metadata?.choices || [];
    const choiceGroups = choiceMessage?.metadata?.choice_groups || [];
    if ((Array.isArray(choiceGroups) && choiceGroups.length) || (Array.isArray(choices) && choices.length)) {
      // 选择面板的临时选择以「会话 + 来源消息」为键存在内存里：历史重渲染只是重新挂载
      // 面板，已答的题与草稿不会被清掉；换会话再切回来同样保留（整页刷新才从首题重来）。
      showChoiceButtons(choices, choiceGroups, {
        messageId: choiceMessage?.id,
        conversationId: state.conversationId,
      });
    }
    else hideChoiceButtons();
    updateContextUsage(list);
  } catch (error) {
    console.error('[naiba] renderMessages 渲染崩溃:', error, '消息数=', list.length);
  }
  // 复挂必须在 replaceChildren 之后：新生成的气泡才有 [data-message-id] 可挂。
  resumeEditComposer(editResume);
}

/**
 * 取当前该展示的选项面板来源消息：从末尾向前扫描。
 * - 遇到 user 消息 → 该组选项已被回答（或本就没有未回答的选项），返回 null；
 * - 遇到「带选项的 assistant」→ 返回它；
 * - 遇到「不带选项的 assistant」（followup 轮次 / 后台任务回执 / 错误重试）→ 继续向前找：
 *   用户还没回复过的那组选项，不能因为后面又追加了一条 AI 消息就整块消失。
 */
export function pendingChoiceMessage(messages) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.role === 'user') return null;
    if (message?.role !== 'assistant') continue;
    const choices = message?.metadata?.choices;
    const groups = message?.metadata?.choice_groups;
    if ((Array.isArray(groups) && groups.length) || (Array.isArray(choices) && choices.length)) {
      return message;
    }
  }
  return null;
}

/* ---------- 对话刻度轨（右侧）：每个用户轮次一条横条 ---------- */

const TURN_RAIL_MAX = 30;          // 同时最多显示 30 条（以视口中心为基准的滑动窗口）
const TURN_TIP_USER_CHARS = 160;   // 概要里用户消息最多保留的字符数（再多交给 CSS 省略号）
const TURN_TIP_REPLY_CHARS = 320;

let turnRailTurns = [];            // [{ anchor, user, reply }]
let turnRailActive = -1;
let turnRailWindow = { start: -1, end: -1 };
let turnRailFrame = 0;
let turnRailBound = false;
// 手机端顶栏「轮次」下拉：刻度轨的等价出口，共用上面这份 turnRailTurns / turnRailActive。
let turnJumpSuppressUntil = 0;   // 下拉跳转后的平滑滚动期间，别被"中途轮次"改写选中项

// 消息数据的文本预览（懒加载下未渲染的轮次没有 DOM，刻度轨概要只能从数据取）。
function messagePreviewText(message, limit) {
  const raw = String((message?.metadata || {}).display_content ?? message?.content ?? '');
  const text = raw.replace(/\s+/g, ' ').trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

// 一个「用户轮次」= 一条用户消息 + 它之后（下一条用户消息之前）的助手回复。
// **从 state.messages 收集**（不是从 DOM）：懒加载只渲染窗口内的消息，刻度轨仍要覆盖全部轮次；
// 已渲染的轮次顺带记下锚点元素，供偏移计算与点击跳转使用。
function collectTurns() {
  const messages = state.messages || [];
  const anchors = new Map();
  document.querySelectorAll('#messages .message-row[data-message-id]').forEach((row) => {
    if (row.dataset.messageId) anchors.set(row.dataset.messageId, row);
  });
  const turns = [];
  messages.forEach((message, messageIndex) => {
    if (!message) return;
    if (message.role === 'user') {
      turns.push({
        messageId: String(message.id || ''),
        messageIndex,
        anchor: anchors.get(String(message.id || '')) || null,
        user: messagePreviewText(message, TURN_TIP_USER_CHARS),
        reply: '',
      });
      return;
    }
    if (!turns.length || message.role !== 'assistant') return;
    const reply = messagePreviewText(message, TURN_TIP_REPLY_CHARS);
    if (reply) turns[turns.length - 1].reply = reply;  // 一轮多条助手消息时取最后一条有正文的
  });
  return turns;
}

function turnRailOffsets(container) {
  const base = container.getBoundingClientRect().top - container.scrollTop;
  const offsets = turnRailTurns.map((turn) => (turn.anchor
    ? Math.round(turn.anchor.getBoundingClientRect().top - base)
    : null));
  // 未渲染的轮次没有锚点：按相邻已渲染轮次向上/向下均摊估算。
  // 只用于判定"视口中心在哪一轮"，不参与任何布局。
  const firstKnown = offsets.findIndex((value) => value !== null);
  if (firstKnown < 0) return offsets.map((_, index) => index * 120);
  for (let index = firstKnown - 1; index >= 0; index -= 1) {
    offsets[index] = offsets[index + 1] - 120;
  }
  for (let index = firstKnown + 1; index < offsets.length; index += 1) {
    if (offsets[index] === null) offsets[index] = offsets[index - 1] + 120;
  }
  return offsets;
}

// 视口中心落在哪一轮的垂直范围内，就高亮哪一条。
function turnRailActiveIndex(container, offsets) {
  if (!turnRailTurns.length) return -1;
  const center = container.scrollTop + container.clientHeight / 2;
  let active = 0;
  for (let index = 0; index < offsets.length; index += 1) {
    if (offsets[index] <= center) active = index;
    else break;
  }
  return active;
}

function renderTurnRail() {
  const rail = $('#turnRail');
  const container = $('#messages');
  if (!rail || !container) return;
  turnRailTurns = collectTurns();
  if (turnRailTurns.length < 2) {
    rail.hidden = true;
    turnRailWindow = { start: -1, end: -1 };
    turnRailActive = -1;
    hideTurnTip();
    hideTurnJump();
    return;
  }
  const offsets = turnRailOffsets(container);
  const active = turnRailActiveIndex(container, offsets);
  // 手机端下拉与刻度轨共用同一份轮次模型、同一个 active：不另算一遍，也不会两处漂移。
  renderTurnJump(turnRailTurns, active);
  const total = turnRailTurns.length;
  const span = Math.min(TURN_RAIL_MAX, total);
  const start = Math.max(0, Math.min(active - Math.floor(span / 2), total - span));
  const end = start + span;
  rail.hidden = false;
  if (start === turnRailWindow.start && end === turnRailWindow.end) {
    // 窗口没变：只挪高亮，不重写 DOM（教训 §九.37：每帧重写是迟滞主因）
    if (active !== turnRailActive) {
      turnRailActive = active;
      rail.querySelectorAll('.turn-tick').forEach((tick) => {
        tick.classList.toggle('active', Number(tick.dataset.turnIndex) === active);
      });
    }
    return;
  }
  turnRailWindow = { start, end };
  turnRailActive = active;
  rail.replaceChildren();
  for (let index = start; index < end; index += 1) {
    // 固定尺寸的透明块 = 判定区；可见的线是块里的内层元素（线变长变粗不影响块尺寸）
    const tick = document.createElement('button');
    tick.type = 'button';
    tick.className = 'turn-tick';
    tick.dataset.turnIndex = String(index);
    // 懒加载后"第 N 轮"与 DOM 行不再一一对应，带上该轮用户消息 id 便于定位/断言。
    tick.dataset.turnMessageId = String(turnRailTurns[index]?.messageId || '');
    tick.setAttribute('aria-label', `第 ${index + 1} 轮对话`);
    const line = document.createElement('span');
    line.className = 'turn-tick-line';
    tick.append(line);
    if (index === active) tick.classList.add('active');
    rail.append(tick);
  }
}

function scheduleTurnRail() {
  if (turnRailFrame) return;
  turnRailFrame = requestAnimationFrame(() => {
    turnRailFrame = 0;
    try {
      renderTurnRail();
    } catch (error) {
      // 刻度轨是辅助显示，坏掉不能影响消息渲染——但必须留痕，不静默吞掉。
      console.error('[naiba] 对话刻度轨渲染失败:', error);
    }
  });
}

function ensureTurnTip() {
  let tip = $('#turnTip');
  if (tip) return tip;
  tip = document.createElement('div');
  tip.id = 'turnTip';
  tip.className = 'turn-tip';
  tip.setAttribute('role', 'tooltip');
  tip.hidden = true;
  document.body.append(tip);
  return tip;
}

function showTurnTip(tick) {
  const index = Number(tick.dataset.turnIndex);
  const turn = turnRailTurns[index];
  if (!turn) return;
  const tip = ensureTurnTip();
  tip.innerHTML = `
    <div class="turn-tip-index">第 ${index + 1} 轮</div>
    <div class="turn-tip-user">${escapeHtml(turn.user || '（无文字，仅附件）')}</div>
    <div class="turn-tip-reply">${escapeHtml(turn.reply || '（暂无回复）')}</div>`;
  tip.hidden = false;
  const rect = tick.getBoundingClientRect();
  const left = Math.max(8, rect.left - tip.offsetWidth - 10);
  const top = Math.max(8, Math.min(
    rect.top + rect.height / 2 - tip.offsetHeight / 2,
    window.innerHeight - tip.offsetHeight - 8,
  ));
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

function hideTurnTip() {
  const tip = $('#turnTip');
  if (tip) tip.hidden = true;
}

/* ---------- 手机端顶栏「轮次」下拉（桌面刻度轨的等价形态） ---------- */

function turnJumpSelect() {
  return $('#turnJumpSelect');
}

function turnJumpLabel(turn, index) {
  return `第 ${index + 1} 轮 · ${turn?.user || '（无文字，仅附件）'}`;
}

function hideTurnJump() {
  const select = turnJumpSelect();
  if (!select) return;
  select.hidden = true;
  select.replaceChildren();
}

// 只在「总轮数」变化时重建 <option>：轮数没变只就地刷新文案（流式回复/编辑重渲染都会
// 反复触发刻度轨重建，每帧重写 DOM 是迟滞主因，教训 §九.37）。
function renderTurnJump(turns, active) {
  const select = turnJumpSelect();
  if (!select || !turns || turns.length < 2) return;
  select.hidden = false;
  if (select.options.length !== turns.length) {
    select.replaceChildren();
    turns.forEach((turn, index) => {
      const option = document.createElement('option');
      option.value = String(index);
      option.textContent = turnJumpLabel(turn, index);
      select.append(option);
    });
  } else {
    turns.forEach((turn, index) => {
      const option = select.options[index];
      const label = turnJumpLabel(turn, index);
      if (option && option.textContent !== label) option.textContent = label;
    });
  }
  syncTurnJump(active);
}

// 只挪选中项，不碰 options；跳转后的平滑滚动期间不回写（否则选中项会被中途轮次抢走）。
function syncTurnJump(active) {
  const select = turnJumpSelect();
  if (!select || select.hidden || Date.now() < turnJumpSuppressUntil) return;
  const value = String(Math.max(0, Number(active) || 0));
  if (select.value !== value) select.value = value;
}

// 用户从下拉里选轮次 → 复用刻度轨的 scrollToTurn（内含懒加载"先扩窗口再重收集锚点"），
// 不自己算 scrollTop（教训 §九.56）。
function applyTurnJump(value) {
  const index = Number(value);
  if (!Number.isFinite(index)) return;
  turnJumpSuppressUntil = Date.now() + 700;
  scrollToTurn(index);
  // 动画结束后再同步一次：中途被抑制，落点必须与选中项一致。
  window.setTimeout(() => {
    turnJumpSuppressUntil = 0;
    scheduleTurnRail();
  }, 720);
}

function scrollToTurn(index) {
  let turn = turnRailTurns[index];
  const container = $('#messages');
  if (!turn || !container) return;
  // 懒加载：点到的轮次可能还没渲染 → 先把窗口扩到它，再重新收集锚点。
  if (!turn.anchor) {
    if (!ensureMessageRendered(turn.messageIndex)) return;
    turnRailTurns = collectTurns();
    turn = turnRailTurns[index];
    if (!turn?.anchor) return;
  }
  // 把该轮的用户消息滚到视口垂直中心：这样"视口中心所在轮次"正好是点中的那一条，
  // 跳转后高亮不会跑到隔壁（否则跳转即高亮漂移，用户会以为点错了）。
  const base = container.getBoundingClientRect().top - container.scrollTop;
  const rowRect = turn.anchor.getBoundingClientRect();
  const top = rowRect.top - base - Math.max(0, (container.clientHeight - rowRect.height) / 2);
  container.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
}

export function initTurnRail() {
  const rail = $('#turnRail');
  const container = $('#messages');
  if (!rail || !container || turnRailBound) return;
  turnRailBound = true;
  // 手机端轮次下拉（桌面被 .mobile-only 隐藏）：选中即跳到该轮。
  const jumpSelect = turnJumpSelect();
  if (jumpSelect) jumpSelect.addEventListener('change', () => applyTurnJump(jumpSelect.value));
  container.addEventListener('scroll', scheduleTurnRail, { passive: true });
  // 懒加载：滚到接近顶部就往前预渲染一段（窗口按「轮」扩展，分割线不会与锚点分离）。
  container.addEventListener('scroll', () => {
    if (Date.now() < lazySuppressUntil) return;      // 渲染/程序化滚动，不算用户操作
    if (stickToBottom) return;                        // 仍在底部附近：没在翻历史
    if ((Number(state.renderStart) || 0) > 0 && container.scrollTop <= LAZY_TOP_TRIGGER) {
      extendRenderedWindow();
    }
  }, { passive: true });
  window.addEventListener('resize', scheduleTurnRail);
  window.addEventListener('scroll', hideTurnTip, true);
  rail.addEventListener('click', (event) => {
    const tick = event.target.closest('.turn-tick');
    if (tick) scrollToTurn(Number(tick.dataset.turnIndex));
  });
  rail.addEventListener('mouseover', (event) => {
    const tick = event.target.closest('.turn-tick');
    if (tick) showTurnTip(tick);
  });
  // 用 mouseleave（整条轨道）而不是每根线的 mouseout：块之间切换时不会闪。
  rail.addEventListener('mouseleave', hideTurnTip);
  rail.addEventListener('focusin', (event) => {
    const tick = event.target.closest('.turn-tick');
    if (tick) showTurnTip(tick);
  });
  rail.addEventListener('focusout', hideTurnTip);
  // 消息区结构变化（整轮渲染 / 流式追加 / 编辑重渲染）时重建刻度
  new MutationObserver(scheduleTurnRail).observe(container, { childList: true });
  scheduleTurnRail();
}


