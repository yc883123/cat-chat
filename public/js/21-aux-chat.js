// ============================================================
// 21-aux-chat.js —— 辅助对话（Auxiliary Chat，计划-辅助对话.md）
// ------------------------------------------------------------
// 主对话旁的轻量并行聊天：标签页随手开随手关、不进会话列表/搜索/统计，
// 后端只读锁死（工具白名单 + 只读执行器，前端无任何权限开关）。
// 与主对话唯一共享的是 state.token / api()：消息渲染与流式消费都是独立实现，
// 不碰主对话单例 state（除只读 token），互不串扰（§3.5）。
// ============================================================

import { $, api, escapeHtml, state, toast } from "./01-core.js";
import { markdown } from "./02-markdown.js";
import { populateComposerModels, selectedProvider } from "./07-models-agents.js";

// 辅助对话自己的状态：tabs 是会话行（id/title），activeId 是当前标签。
// messages 按会话 id 缓存（切换标签不丢已渲染内容）；runId 是当前流式 run。
export const auxChatState = {
  tabs: [],            // [{ id, title, messages: [{role, content, pending?}] }]
  activeId: '',
  open: false,         // dock 的辅助分组是否处于当前显示（dock 开合仍由文件面板的开合类驱动）
  group: 'files',      // dock 顶层分组记忆：'files' | 'aux'
  sending: false,      // 当前会话是否有 run 在流式（辅助对话同时只有一个）
  runId: '',
  // 辅助对话的模型选择（'' = 跟随主对话输入区当前选择）：用户在辅助面板里
  // 独立切换后记进 localStorage，此后不再跟着主对话漂。
  modelOverride: '',
};

const AUX_GROUP_KEY = 'naibaChatDockGroup';
const AUX_MODEL_KEY = 'naibaChatAuxModel';

// ---- dock 分组切换（§3.4：文件 | 辅助 共存，共享宽度与开合） --------------------

export function dockGroup() {
  return auxChatState.group;
}

export function applyDockGroup() {
  const filesBody = $('#dockGroupFilesBody');
  const auxBody = $('#dockGroupAuxBody');
  const filesBtn = $('#dockGroupFiles');
  const auxBtn = $('#dockGroupAux');
  if (!filesBody || !auxBody || !filesBtn || !auxBtn) return;
  const aux = auxChatState.group === 'aux';
  filesBody.hidden = aux;
  auxBody.hidden = !aux;
  filesBtn.setAttribute('aria-selected', String(!aux));
  auxBtn.setAttribute('aria-selected', String(aux));
  // 辅助分组激活时，文件分组里的旧高亮不动（各自的 active 状态独立保留）。
  localStorage.setItem(AUX_GROUP_KEY, auxChatState.group);
}

export function setDockGroup(group) {
  auxChatState.group = group === 'aux' ? 'aux' : 'files';
  applyDockGroup();
}

export function restoreDockGroup() {
  const saved = String(localStorage.getItem(AUX_GROUP_KEY) || 'files');
  auxChatState.group = saved === 'aux' ? 'aux' : 'files';
  applyDockGroup();
}

// ---- 会话生命周期 -----------------------------------------------------------

function activeTab() {
  return auxChatState.tabs.find((tab) => tab.id === auxChatState.activeId) || null;
}

async function refreshAuxChats() {
  const result = await api('/api/aux-chats');
  const rows = Array.isArray(result?.conversations) ? result.conversations : [];
  const known = new Map(auxChatState.tabs.map((tab) => [tab.id, tab]));
  auxChatState.tabs = rows.map((row) => ({
    id: String(row.id || ''),
    title: String(row.title || '辅助对话'),
    messages: known.get(String(row.id))?.messages || [],
  })).filter((tab) => tab.id);
  if (!auxChatState.tabs.find((tab) => tab.id === auxChatState.activeId)) {
    auxChatState.activeId = auxChatState.tabs[0]?.id || '';
  }
  updateAuxTabsButton();
  renderAuxChat();
}

export async function createAuxChat() {
  try {
    const result = await api('/api/aux-chats', { method: 'POST', body: {} });
    const conversation = result?.conversation || result;
    if (!conversation?.id) throw new Error(result?.error || '创建失败');
    auxChatState.tabs.unshift({ id: String(conversation.id), title: String(conversation.title || '辅助对话'), messages: [] });
    auxChatState.activeId = String(conversation.id);
    openAuxPanel();
    renderAuxChat();
    updateAuxTabsButton();
    $('#auxChatInput')?.focus();
  } catch (error) {
    toast(`创建辅助对话失败：${error.message}`);
  }
}

export async function closeAuxChat(id, { silent = false } = {}) {
  const tab = auxChatState.tabs.find((item) => item.id === id);
  // 有内容时弹一次确认（§3.5 留存策略）：关闭即删除，不可恢复。
  if (!silent && tab && tab.messages.length) {
    if (!window.confirm('关闭这个辅助对话？里面的内容不会保留。')) return;
  }
  auxChatState.tabs = auxChatState.tabs.filter((item) => item.id !== id);
  if (auxChatState.activeId === id) auxChatState.activeId = auxChatState.tabs[0]?.id || '';
  try {
    await api(`/api/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' });
  } catch (error) {
    // 删除失败不阻断 UI：本地已移除，7 天过期清扫兜底。
    console.warn('辅助对话删除失败（将由过期清扫兜底）', error);
  }
  updateAuxTabsButton();
  renderAuxChat();
}

// ---- 打开/收起面板 -----------------------------------------------------------

export function openAuxPanel() {
  setDockGroup('aux');
  const shell = $('#appShell');
  if (!shell) return;
  if (!shell.classList.contains('file-panel-open')) {
    // 复用文件面板的开合链路（同一个 dock、同一宽度）；不自动开文件标签。
    shell.classList.add('file-panel-open');
    shell.style.setProperty('--file-panel-w', `${filePanelWidthPx()}px`);
  }
  renderAuxChat();
}

export function closeAuxPanel() {
  // 与 closeFilePanel 同语义：收起整个 dock（两组一起收），分组记忆保留。
  const shell = $('#appShell');
  if (shell) shell.classList.remove('file-panel-open');
}

function filePanelWidthPx() {
  const stored = parseFloat(localStorage.getItem('naibaChatFilePanelW') || '');
  if (Number.isFinite(stored) && stored > 0) return Math.round(stored);
  return 380;
}

// ---- 渲染 -------------------------------------------------------------------

function updateAuxTabsButton() {
  const button = $('#openAuxChats');
  if (!button) return;
  button.hidden = auxChatState.tabs.length === 0;
  const count = $('#auxChatsCount');
  if (count) count.textContent = String(auxChatState.tabs.length);
}

export function renderAuxChat() {
  renderAuxTabs();
  renderAuxMessages();
}

function renderAuxTabs() {
  const tabsEl = $('#auxChatTabs');
  if (!tabsEl) return;
  tabsEl.innerHTML = [
    ...auxChatState.tabs.map((tab) => {
      const active = tab.id === auxChatState.activeId;
      return `<span class="file-tab${active ? ' active' : ''}" role="tab" aria-selected="${active}" data-aux-tab="${escapeHtml(tab.id)}" title="${escapeHtml(tab.title)}"><span class="file-tab-name">${escapeHtml(tab.title)}</span><button type="button" class="file-tab-x" data-aux-tab-close="${escapeHtml(tab.id)}" aria-label="关闭 ${escapeHtml(tab.title)}" tabindex="-1"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg></button></span>`;
    }),
    '<button type="button" class="file-tab aux-new-tab" data-aux-new title="新开一个辅助对话" aria-label="新开辅助对话">＋</button>',
  ].join('');
}

function renderAuxMessages() {
  const body = $('#auxChatMessages');
  if (!body) return;
  const tab = activeTab();
  if (!tab) {
    body.innerHTML = [
      '<div class="aux-chat-banner">辅助对话与主对话相互独立：只能读取和查询，不能修改文件；内容不会保留，关掉标签就清掉。</div>',
      '<div class="file-panel-hint">开一个辅助对话，随手问一句。它和左边的对话互不打扰，主任务跑着也不受影响。</div>',
    ].join('');
    return;
  }
  const rows = tab.messages.map((message, index) => {
    const isUser = message.role === 'user';
    const contentHtml = isUser
      ? `<div class="aux-bubble-text">${escapeHtml(message.content)}</div>`
      : `<div class="aux-bubble-text message-body answer-content">${message.pending && !message.content ? '<span class="aux-thinking">思考中…</span>' : markdown(message.content || '')}</div>`;
    const sendToMain = !isUser && message.content && !message.pending
      ? `<button type="button" class="control-button aux-send-to-main" data-aux-to-main="${index}" title="把这段结论复制进主对话输入框">发送到主任务</button>`
      : '';
    return `<div class="aux-row aux-row-${isUser ? 'user' : 'ai'}"><div class="aux-bubble">${contentHtml}</div>${sendToMain}</div>`;
  }).join('');
  const banner = '<div class="aux-chat-banner">此对话与主对话相互独立，只读，内容不会保留。</div>';
  body.innerHTML = banner + rows;
  body.scrollTop = body.scrollHeight;
}

// ---- 发送与轻量流式消费（独立实现，不碰主对话流） -----------------------------

export async function sendAuxMessage() {
  const input = $('#auxChatInput');
  const text = String(input?.value || '').trim();
  if (!text) return;
  let tab = activeTab();
  if (!tab) {
    await createAuxChat();
    tab = activeTab();
    if (!tab) return;
  }
  if (auxChatState.sending) {
    toast('上一条还在回答，等它说完');
    return;
  }
  input.value = '';
  autoGrowAuxInput();
  tab.messages.push({ role: 'user', content: text });
  const reply = { role: 'assistant', content: '', pending: true };
  tab.messages.push(reply);
  renderAuxChat();
  await streamAuxRun(tab, reply, text);
}

async function streamAuxRun(tab, reply, text) {
  auxChatState.sending = true;
  setAuxBusy(true);
  const controller = new AbortController();
  try {
    // 模型口径与主对话发送完全一致：model_key = 顶栏 API 选择，model_name =
    // 输入区模型（辅助面板可独立覆盖，见 auxModelChoice）。后端 submit_chat 两个
    // 字段都收；只带 provider_id 会落进「请先在会话底部选择模型」。
    const conversationId = tab.id;
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${state.token}` },
      body: JSON.stringify({
        conversation_id: conversationId,
        message: text,
        model_key: String($('#modelSelect')?.value || '').trim(),
        model_name: auxModelChoice(),
      }),
      signal: controller.signal,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    // 轻量 NDJSON 消费：只关心 delta / done / error / cancelled，其余事件忽略。
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let failure = '';
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) {
        if (!line.trim()) continue;
        let event;
        try { event = JSON.parse(line); } catch (_) { continue; }
        if (event.type === 'heartbeat') continue;
        if (event.type === 'delta' && typeof event.content === 'string') {
          reply.content += event.content;
          renderAuxMessages();
        } else if (event.type === 'error' || event.type === 'run_failed') {
          failure = String(event.message || event.error || '回答失败');
        } else if (event.type === 'cancelled') {
          failure = '已取消';
        } else if (event.type === 'done') {
          // done 事件：正文已在 delta 里流完；失败标记优先。
        }
      }
      if (done) break;
    }
    if (buffer.trim()) {
      try {
        const event = JSON.parse(buffer);
        if (event.type === 'delta' && typeof event.content === 'string') reply.content += event.content;
        if (event.type === 'error' || event.type === 'run_failed') failure = String(event.message || event.error || '回答失败');
      } catch (_) { /* 尾行不完整忽略 */ }
    }
    reply.pending = false;
    if (failure && !reply.content) reply.content = `（${failure}）`;
    else if (failure) reply.content += `\n\n（${failure}）`;
    renderAuxChat();
  } catch (error) {
    reply.pending = false;
    if (error.name === 'AbortError') {
      reply.content = reply.content || '（已取消）';
    } else {
      reply.content = `（发送失败：${error.message}）`;
      toast(`辅助对话发送失败：${error.message}`);
    }
    renderAuxChat();
  } finally {
    auxChatState.sending = false;
    auxChatState.runId = '';
    setAuxBusy(false);
  }
}

function setAuxBusy(busy) {
  const send = $('#auxChatSend');
  if (send) {
    send.disabled = busy;
    send.textContent = busy ? '回答中' : '发送';
  }
}

export function autoGrowAuxInput() {
  const input = $('#auxChatInput');
  if (!input) return;
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}

// ---- 模型选择（辅助对话可独立换模型；'' = 跟随主对话输入区） -------------------

function auxModelChoice() {
  // 覆盖值只在「当前 API 下目录里真的有这个模型」时生效：主对话换 API 后，
  // 旧 API 的覆盖模型不再发送（避免拿着 A 家的模型名打 B 家的接口）。
  const override = String(auxChatState.modelOverride || '').trim();
  const provider = selectedProvider();
  if (override && provider) {
    const catalog = state.providerModelCatalogs[provider.model_key] || [];
    if (catalog.some((model) => String(model?.id || '').trim() === override)) return override;
    if (!catalog.length) return override; // 目录还没拉到：先照发，让后端给出明确报错
  }
  // 跟随主对话：直接读主输入区的隐藏 select（唯一值来源），不缓存副本——
  // 主对话换模型，辅助面板未覆盖时立刻跟上，不会两处漂移。
  const mainSelect = $('#composerModelSelect');
  return String(mainSelect?.value || '').trim();
}

function renderAuxModelSelect() {
  const select = $('#auxModelSelect');
  const provider = selectedProvider();
  if (!select) return;
  const providerKey = String(provider?.model_key || '');
  const catalog = state.providerModelCatalogs[providerKey] || [];
  const mainChoice = String($('#composerModelSelect')?.value || '').trim();
  const override = String(auxChatState.modelOverride || '').trim();
  const effective = override || mainChoice;
  select.replaceChildren();
  // 头选项表达「跟随主对话」这一状态本身：主对话当前是什么就显示什么。
  const follow = document.createElement('option');
  follow.value = '';
  follow.textContent = mainChoice ? `跟随主对话（${mainChoice}）` : '跟随主对话（未选择）';
  select.append(follow);
  catalog.forEach((model) => {
    const id = String(model?.id || '').trim();
    if (!id) return;
    const option = document.createElement('option');
    option.value = id;
    option.textContent = model?.name || id;
    select.append(option);
  });
  // 目录还没拉到时至少把主对话的当前选择列出来，下拉不至于空到没法换。
  if (!catalog.length && mainChoice) {
    const option = document.createElement('option');
    option.value = mainChoice;
    option.textContent = mainChoice;
    select.append(option);
  }
  const matched = [...select.options].some((option) => option.value === effective);
  select.value = matched ? effective : '';
  if (!catalog.length && providerKey) {
    // 目录为空：借主对话的拉取链路填一次缓存（force=false，命中缓存就不发请求）。
    void populateComposerModels(null, { force: false }).then(renderAuxModelSelect);
  }
}

function handleAuxModelChange() {
  const select = $('#auxModelSelect');
  if (!select) return;
  auxChatState.modelOverride = String(select.value || '').trim();
  if (auxChatState.modelOverride) {
    localStorage.setItem(AUX_MODEL_KEY, auxChatState.modelOverride);
  } else {
    localStorage.removeItem(AUX_MODEL_KEY);
  }
  renderAuxModelSelect();
}

function restoreAuxModelOverride() {
  // 覆盖值跨重载保留；当前 API 的目录拉到后 handleAuxModelChange/render 会自然校验，
  // 不在新 API 目录里的覆盖项发送时会被 auxModelChoice 丢弃。
  auxChatState.modelOverride = String(localStorage.getItem(AUX_MODEL_KEY) || '').trim();
}

// ---- 事件委托（动态 HTML 一律 data-* + 委托，禁止内联 onclick） ---------------

export function bindAuxChatEvents() {
  const tabsEl = $('#auxChatTabs');
  tabsEl?.addEventListener('click', (event) => {
    if (event.target.closest('[data-aux-new]')) { void createAuxChat(); return; }
    const closeBtn = event.target.closest('[data-aux-tab-close]');
    if (closeBtn) { void closeAuxChat(closeBtn.dataset.auxTabClose); return; }
    const tabEl = event.target.closest('[data-aux-tab]');
    if (tabEl) {
      auxChatState.activeId = tabEl.dataset.auxTab;
      renderAuxChat();
      $('#auxChatInput')?.focus();
    }
  });
  $('#closeAuxPanel')?.addEventListener('click', () => closeAuxPanel());
  $('#auxChatSend')?.addEventListener('click', () => { void sendAuxMessage(); });
  $('#auxModelSelect')?.addEventListener('change', handleAuxModelChange);
  // 主对话换 API / 换模型：辅助下拉的头选项与候选列表跟着重算（覆盖值仍保留）。
  $('#modelSelect')?.addEventListener('change', () => setTimeout(renderAuxModelSelect, 0));
  $('#composerModelSelect')?.addEventListener('change', () => setTimeout(renderAuxModelSelect, 0));
  const input = $('#auxChatInput');
  input?.addEventListener('input', autoGrowAuxInput);
  input?.addEventListener('keydown', (event) => {
    // Enter 发送、Shift+Enter 换行（与主输入框一致）。
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      void sendAuxMessage();
    }
  });
  $('#dockGroupAux')?.addEventListener('click', () => { setDockGroup('aux'); renderAuxChat(); });
  $('#dockGroupFiles')?.addEventListener('click', () => setDockGroup('files'));
  $('#openAuxChats')?.addEventListener('click', () => openAuxPanel());
  // 「发送到主任务」：复制进主输入框并聚焦，不自动发送（守住不污染，§2）。
  $('#auxChatMessages')?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-aux-to-main]');
    if (!button) return;
    const tab = activeTab();
    const message = tab?.messages[Number(button.dataset.auxToMain)];
    if (!message?.content) return;
    const mainInput = $('#messageInput');
    if (!mainInput) { toast('找不到主对话输入框'); return; }
    mainInput.value = mainInput.value ? `${mainInput.value}\n\n${message.content}` : message.content;
    mainInput.dispatchEvent(new Event('input', { bubbles: true }));
    mainInput.focus();
    toast('已复制到主对话输入框，确认后再发送');
  });
}

// 页面重载后恢复标签（§3.5）：初始化时调用一次；失败静默（首次启动无 aux 会话是常态）。
export async function restoreAuxChats() {
  restoreAuxModelOverride();
  renderAuxModelSelect();
  try {
    await refreshAuxChats();
  } catch (_) {
    auxChatState.tabs = [];
  }
}
