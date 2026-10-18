// ============================================================
// 21-aux-chat.js —— 辅助对话（Auxiliary Chat，计划-辅助对话.md）
// ------------------------------------------------------------
// 主对话旁的轻量并行聊天：标签页随手开随手关、不进会话列表/搜索/统计，
// 后端只读锁死（工具白名单 + 只读执行器，前端无任何权限开关）。
//
// 消息渲染直接复用主对话的 messageElement()（04-messages.js）：辅助对话与主对话
// 是同一套 `.message-row / .message-card` 皮，工具卡、用量、媒体全部同源。
// 供应商与模型是**辅助对话自己的**：存在 aux 会话行上，发送时只读这里的值，
// 顶栏/输入区的供应商变化不再影响它（只在新建那一刻继承一次初始值）。
// ============================================================

import { $, api, copyText, escapeHtml, state, toast } from "./01-core.js";
import { markdown } from "./02-markdown.js";
import { messageElement } from "./04-messages.js";
// 点「跟随」标题跳回主对话。08 不 import 21，不构成新的循环依赖。
import { openConversation } from "./08-conversations.js";
// 面板宽度 / 开合状态只有一份实现（14-file-panel.js）。14 不 import 21，无循环依赖。
import { applyFilePanelOpenClass, filePanelState, filePanelWidthPx, updateFileTabsButton } from "./14-file-panel.js";

// 辅助对话自己的状态：tabs 是会话行（id/title/parentId/供应商/模型），activeId 是当前标签。
// messages 按会话 id 缓存（切换标签不丢已渲染内容）；sending 表示当前是否有 run 在流式。
export const auxChatState = {
  tabs: [],            // [{ id, title, parentId, providerKey, modelName, messages, loaded }]
  activeId: '',
  group: 'files',      // dock 顶层分组记忆：'files' | 'aux'
  sending: false,      // 当前会话是否有 run 在流式（辅助对话同时只有一个）
};

const AUX_GROUP_KEY = 'naibaChatDockGroup';
// 已经尝试拉过目录的供应商：目录为空（供应商 200 但没模型）时不重复请求。
const auxCatalogFetched = new Set();

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

function tabFromRow(row, known) {
  const id = String(row.id || '');
  return {
    id,
    title: String(row.title || '辅助对话'),
    // 「跟随」的主对话 id：只用于显示与切换联动，不改变上下文隔离。
    parentId: String(row.parent_id || ''),
    // 辅助对话自己的供应商（model_key）与模型名，存在 aux 会话行上。
    providerKey: String(row.model_key || ''),
    modelName: String(row.model_name || ''),
    messages: known?.messages || [],
    loaded: Boolean(known?.loaded),
  };
}

async function refreshAuxChats() {
  const result = await api('/api/aux-chats');
  const rows = Array.isArray(result?.conversations) ? result.conversations : [];
  const known = new Map(auxChatState.tabs.map((tab) => [tab.id, tab]));
  auxChatState.tabs = rows.map((row) => tabFromRow(row, known.get(String(row.id)))).filter((tab) => tab.id);
  syncAuxToConversation({ render: false });
  // 只有在没有「当前主对话」可跟随时才退到第一个标签；否则保持 sync 的空态决定
  // （当前主对话没有辅助窗 → 空态 + 「＋」，而不是随便挂一个别的对话的辅助窗）。
  if (!auxChatState.activeId && !String(state.conversationId || '')) {
    auxChatState.activeId = auxChatState.tabs[0]?.id || '';
  }
  updateAuxTabsButton();
  renderAuxChat();
  const tab = activeTab();
  if (tab) void loadAuxMessages(tab);
}

export async function createAuxChat() {
  try {
    // 新建时继承一次主对话当前的选择作为初始值；此后两边互不影响。
    const providerKey = String(selectedProviderKey() || '');
    const modelName = String($('#composerModelSelect')?.value || '').trim();
    const result = await api('/api/aux-chats', {
      method: 'POST',
      body: {
        parent_id: String(state.conversationId || ''),
        model_key: providerKey,
        model_name: modelName,
      },
    });
    const conversation = result?.conversation || result;
    if (!conversation?.id) throw new Error(result?.error || '创建失败');
    const tab = tabFromRow(conversation, null);
    tab.messages = [];
    tab.loaded = true;
    auxChatState.tabs.unshift(tab);
    auxChatState.activeId = tab.id;
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
  if (auxChatState.activeId === id) {
    auxChatState.activeId = '';
    syncAuxToConversation({ render: false });
    if (!auxChatState.activeId && !String(state.conversationId || '')) {
      auxChatState.activeId = auxChatState.tabs[0]?.id || '';
    }
  }
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
  // 复用文件面板的开合链路（同一个 dock、同一宽度）；不自动开文件标签。
  // 状态必须写回 filePanelState 再交给 applyFilePanelOpenClass()：以前这里直接 add class，
  // 于是 filePanelState.open 仍是 false，两个「重开」按钮的显隐判断（都读这个值）全部失效。
  if (!filePanelState.open) {
    filePanelState.open = true;
    shell.style.setProperty('--file-panel-w', `${filePanelWidthPx()}px`);
  }
  applyFilePanelOpenClass();
  updateFileTabsButton();
  updateAuxTabsButton();
  renderAuxChat();
  const tab = activeTab();
  if (tab) void loadAuxMessages(tab);
}

export function closeAuxPanel() {
  // 与 closeFilePanel 同语义：收起整个 dock（两组一起收），分组记忆保留。
  if (!filePanelState.open) return;
  filePanelState.open = false;
  applyFilePanelOpenClass();
  // 收起后两个分组各自的重开入口要立刻回来，否则「文件 N / 辅助 N」都不见了。
  updateFileTabsButton();
  updateAuxTabsButton();
}

// 面板宽度不再在这里维护第二份实现：本文件曾复制过一份 filePanelWidthPx()，
// 但把钳制逻辑（min/max）丢了，于是「记忆值」可以是窗口一半以上、默认值固定 380 不随窗口收，
// 主区被吃干净后顶栏只能挤成一团（3.3.2 用户报障的直接触发点）。统一走 14-file-panel.js。

// ---- 跟随主对话 -------------------------------------------------------------

// 切主对话时把辅助窗切到「属于这个主对话」的那个标签；没有就进空态。
// render=false 用于列表刚刷新时（调用方紧接着自己会渲染）。
export function syncAuxToConversation({ render = true } = {}) {
  const parentId = String(state.conversationId || '');
  if (!parentId) return;
  const match = auxChatState.tabs.find((tab) => tab.parentId === parentId);
  if (match) {
    if (auxChatState.activeId !== match.id) {
      auxChatState.activeId = match.id;
      if (render) {
        renderAuxChat();
        void loadAuxMessages(match);
      }
    }
    return;
  }
  // 当前主对话没有辅助窗：面板显示空态（空态的「＋」创建时会自动绑定当前主对话）。
  if (auxChatState.activeId) {
    auxChatState.activeId = '';
    if (render) renderAuxChat();
  }
}

function renderAuxFollow() {
  const row = $('#auxFollowRow');
  const target = $('#auxFollowTarget');
  if (!row || !target) return;
  const tab = activeTab();
  if (!tab || !tab.parentId) {
    row.hidden = true;
    target.textContent = '';
    return;
  }
  const parent = state.conversations.find((item) => item.id === tab.parentId);
  row.hidden = false;
  if (parent) {
    target.textContent = String(parent.title || '未命名对话');
    target.disabled = false;
    target.classList.remove('is-missing');
    target.title = '回到这个主对话';
  } else {
    target.textContent = '（原对话已删）';
    target.disabled = true;
    target.classList.add('is-missing');
    target.title = '这个辅助对话原本跟随的主对话已被删除';
  }
}

// ---- 渲染 -------------------------------------------------------------------

function updateAuxTabsButton() {
  const button = $('#openAuxChats');
  if (!button) return;
  // ⚠️ 不要在这里加 `|| auxChatState.tabs.length === 0` 之类的门槛（3.3.0~3.3.2 就是这么错的）：
  // 顶栏这个按钮是**全新用户唯一的辅助对话入口**——新建辅助对话的 ＋ 在面板里，
  // 面板又只能靠这个按钮打开，要求「先有标签」等于死锁，全新用户永远看不到辅助对话。
  // 所以只在「面板已经开着」时隐藏（那会儿面板里自己就有 ＋ 和空态按钮，不需要重复入口）。
  // 这与文件按钮的口径不同：文件按钮背后有「点消息里的文件」另一条入口，辅助没有。
  button.hidden = filePanelState.open;
  const count = $('#auxChatsCount');
  if (count) {
    // 0 时显示「＋」（引导新建），>0 显示数量；两者都不是裸 0。
    count.textContent = auxChatState.tabs.length ? String(auxChatState.tabs.length) : '＋';
  }
}

export function renderAuxChat() {
  renderAuxTabs();
  renderAuxFollow();
  renderAuxMessages();
  renderAuxProviderSelect();
  renderAuxModelSelect();
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

// 一条消息 → 主对话同款 DOM。id 一律置空：编辑/分支/删除/重新生成/新会话都是
// 主对话的概念，在辅助窗里会误伤（messageElement 按 !temporary && message.id 显示它们）。
function auxMessageRow(message, index) {
  const element = messageElement({ ...message, id: '' }, false);
  const runs = Array.isArray(message.metadata?.tool_runs) ? message.metadata.tool_runs : [];
  // 流式期间的占位 run 还没有结果：toolRunMarkup 只认 success，会显示成「已执行」，
  // 这里按 pending 改写成「执行中」。run 结束后回填的真实消息不带 pending，自动恢复。
  if (runs.some((run) => run?.pending)) {
    element.querySelectorAll('.tool-run').forEach((card, runIndex) => {
      const run = runs[runIndex];
      if (!run?.pending) return;
      const summary = card.querySelector('summary');
      if (summary) summary.textContent = `执行中 · ${String(run.tool || '')}`;
    });
  }
  const answer = element.querySelector('.answer-content');
  if (message.role === 'assistant' && message.pending && !message.content && !runs.length && answer) {
    answer.innerHTML = '<span class="aux-thinking">思考中…</span>';
  }
  if (message.role === 'assistant' && !message.pending && message.content) {
    const actions = element.querySelector('.message-actions');
    if (actions) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'aux-send-to-main';
      button.dataset.auxToMain = String(index);
      button.title = '把这段结论复制进主对话输入框';
      button.textContent = '发送到主任务';
      actions.append(button);
    }
  }
  return element;
}

function auxEmptyState() {
  // 空态必须有**可点的入口**：面板里除了这是一个状态说明，还得能直接开一个。
  // （此前只有一行提示文字，加上顶栏按钮的门槛，全新用户等于进不来。）
  return [
    '<div class="aux-chat-banner">辅助对话与主对话相互独立：只能读取和查询，不能修改文件；内容不会保留，关掉标签就清掉。</div>',
    '<div class="aux-empty">',
    '<button type="button" class="aux-empty-start" data-aux-new>＋ 开一个辅助对话</button>',
    '<p class="aux-empty-hint">随手问一句就行。它和左边的对话互不打扰，主任务跑着也不受影响。</p>',
    '</div>',
  ].join('');
}

function renderAuxMessages() {
  const body = $('#auxChatMessages');
  if (!body) return;
  const tab = activeTab();
  if (!tab) {
    body.innerHTML = auxEmptyState();
    return;
  }
  const fragment = document.createDocumentFragment();
  tab.messages.forEach((message, index) => fragment.append(auxMessageRow(message, index)));
  body.replaceChildren(fragment);
  body.scrollTop = body.scrollHeight;
}

// 流式期间只更新「最后那条待完成回复」，不整表重绘（否则滚动位置和展开的工具卡都会丢）。
function updatePendingRow(tab, reply, index) {
  const body = $('#auxChatMessages');
  const row = body?.querySelectorAll('.message-row')?.[index];
  if (!row) { renderAuxMessages(); return; }
  const runs = Array.isArray(reply.metadata?.tool_runs) ? reply.metadata.tool_runs : [];
  // 结构签名必须带上 pending：tool_result 会就地替换同一条 run（数量不变），
  // 只看数量的话「执行中」会一直挂着，直到终态回填才消失。
  const signature = runs.map((run) => `${run?.tool || ''}:${run?.pending ? 1 : 0}`).join('|');
  if (signature !== (reply.__renderedRuns || '')) {
    // 工具卡是 <details>：结构变了就整行替换，不能只改正文。
    reply.__renderedRuns = signature;
    const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 60;
    row.replaceWith(auxMessageRow(reply, index));
    if (atBottom) body.scrollTop = body.scrollHeight;
    return;
  }
  const answer = row.querySelector('.answer-content');
  if (answer) {
    answer.innerHTML = reply.content
      ? markdown(reply.content)
      : '<span class="aux-thinking">思考中…</span>';
  }
}

// ---- 供应商 / 模型（辅助对话自己的选择） ------------------------------------

function providerProfiles() {
  // restoreAuxChats() 在启动链里跑得比 bootstrap 早：state.bootstrap 可能还是 null。
  return state.bootstrap?.model_profiles || state.bootstrap?.providers || [];
}

function selectedProviderKey() {
  // 主对话当前的供应商：只在新建辅助对话时作为初始值读取一次。
  return String($('#modelSelect')?.value || '').trim();
}

function auxProviderKey(tab) {
  if (tab?.providerKey) return tab.providerKey;
  // 存量辅助会话没有自己的供应商：用默认模型，**不读主对话当前选择**——
  // 读主对话就又把两边绑在一起了（用户明确要求互不影响）。
  return String(state.bootstrap?.default_model_key || '');
}

function auxCatalog(providerKey) {
  return state.providerModelCatalogs[providerKey] || [];
}

async function ensureAuxCatalog(providerKey, { force = false } = {}) {
  if (!providerKey) return [];
  if (!force && auxCatalog(providerKey).length) return auxCatalog(providerKey);
  // 目录确实为空时也要只拉一次：renderAuxModelSelect 会在拉取完成后重绘，
  // 若这里不记「已尝试」，重绘又触发拉取就成了无限请求。
  if (!force && auxCatalogFetched.has(providerKey)) return auxCatalog(providerKey);
  auxCatalogFetched.add(providerKey);
  try {
    const result = await api('/api/providers/models', { method: 'POST', body: { model_key: providerKey } });
    state.providerModelCatalogs[providerKey] = Array.isArray(result.models) ? result.models : [];
  } catch (error) {
    if (force) toast(`无法获取模型目录：${error.message}`);
    else console.debug('[naiba] 辅助对话获取模型目录失败:', error.message);
    state.providerModelCatalogs[providerKey] = state.providerModelCatalogs[providerKey] || [];
  }
  return state.providerModelCatalogs[providerKey];
}

function renderAuxProviderSelect() {
  const select = $('#auxProviderSelect');
  if (!select) return;
  const tab = activeTab();
  const profiles = providerProfiles();
  select.replaceChildren();
  if (!tab) {
    // 空态：还没有辅助对话，供应商/模型没有归属，给占位并禁用（发送时会自动新建）。
    const option = document.createElement('option');
    option.value = '';
    option.textContent = '供应商';
    select.append(option);
    select.disabled = true;
    return;
  }
  if (!profiles.length) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = '未配置 API';
    select.append(option);
    select.disabled = true;
    return;
  }
  select.disabled = false;
  const online = profiles.filter((profile) => (profile.kind || 'online') === 'online');
  const local = profiles.filter((profile) => profile.kind === 'local');
  const group = (label, list) => {
    if (!list.length) return;
    const optgroup = document.createElement('optgroup');
    optgroup.label = label;
    list.forEach((profile) => {
      const option = document.createElement('option');
      option.value = String(profile.model_key || '');
      option.textContent = String(profile.name || profile.model_key || '');
      optgroup.append(option);
    });
    select.append(optgroup);
  };
  group('在线 API', online);
  group('本地模型', local);
  const effective = auxProviderKey(tab);
  if (effective && ![...select.options].some((option) => option.value === effective)) {
    const option = document.createElement('option');
    option.value = effective;
    option.textContent = effective;
    select.append(option);
  }
  select.value = effective;
}

function renderAuxModelSelect() {
  const select = $('#auxModelSelect');
  if (!select) return;
  const tab = activeTab();
  const providerKey = tab ? auxProviderKey(tab) : '';
  select.replaceChildren();
  if (!tab) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = '模型';
    select.append(option);
    select.disabled = true;
    return;
  }
  const catalog = auxCatalog(providerKey);
  const desired = String(tab.modelName || '').trim();
  catalog.forEach((model) => {
    const id = String(model?.id || '').trim();
    if (!id) return;
    const option = document.createElement('option');
    option.value = id;
    option.textContent = model?.name || id;
    select.append(option);
  });
  if (desired && ![...select.options].some((option) => option.value === desired)) {
    // 已保存但目录里暂时没有（供应商换了 / 模型下架）：仍然显示，避免静默清空。
    const option = document.createElement('option');
    option.value = desired;
    option.textContent = `已保存：${desired}`;
    select.append(option);
  }
  if (!select.options.length) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = providerKey ? '该供应商暂无模型' : '请先选择供应商';
    select.append(option);
  } else if (!desired) {
    // 有目录但还没选：给一个明确的占位项，避免「看着像选中了第一项、实际没选」。
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = '选择模型';
    select.prepend(placeholder);
  }
  select.value = desired && [...select.options].some((option) => option.value === desired) ? desired : '';
  select.disabled = !providerKey || !select.options.length;
  if (providerKey && !catalog.length) {
    // 只有真的拿到模型才重绘：目录为空时再重绘会回到这里，形成无限请求/微任务循环。
    void ensureAuxCatalog(providerKey).then((models) => {
      if (models.length && auxChatState.activeId === tab.id) renderAuxModelSelect();
    });
  }
}

async function persistAuxSelection(tab) {
  if (!tab?.id) return;
  try {
    await api(`/api/conversations/${encodeURIComponent(tab.id)}/settings`, {
      method: 'POST',
      body: { model_key: tab.providerKey, model_name: tab.modelName },
    });
  } catch (error) {
    toast(`保存辅助对话模型失败：${error.message}`);
  }
}

async function handleAuxProviderChange() {
  const tab = activeTab();
  const select = $('#auxProviderSelect');
  if (!tab || !select) return;
  tab.providerKey = String(select.value || '').trim();
  // 换供应商后旧模型名不再适用：清空，等用户在新目录里选。
  tab.modelName = '';
  renderAuxModelSelect();
  const models = await ensureAuxCatalog(tab.providerKey, { force: true });
  // 只有一个模型时直接选中，省掉「选了供应商还要再点一次」的死路。
  if (!tab.modelName && models.length === 1) tab.modelName = String(models[0]?.id || '');
  renderAuxModelSelect();
  await persistAuxSelection(tab);
}

async function handleAuxModelChange() {
  const tab = activeTab();
  const select = $('#auxModelSelect');
  if (!tab || !select) return;
  tab.modelName = String(select.value || '').trim();
  await persistAuxSelection(tab);
}

// ---- 拉取真实消息（工具卡 / 用量 / 媒体全部来自落库结果） ---------------------

async function loadAuxMessages(tab) {
  if (!tab || tab.loaded || auxChatState.sending) return;
  try {
    const conversation = await api(`/api/conversations/${encodeURIComponent(tab.id)}`);
    if (!conversation || conversation.error) return;
    tab.messages = Array.isArray(conversation.messages) ? conversation.messages : [];
    tab.loaded = true;
    if (String(conversation.parent_id || '') !== tab.parentId) {
      tab.parentId = String(conversation.parent_id || '');
    }
    if (auxChatState.activeId === tab.id) {
      renderAuxFollow();
      renderAuxMessages();
    }
  } catch (error) {
    console.debug('[naiba] 辅助对话读取消息失败:', error.message);
  }
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
  const providerKey = auxProviderKey(tab);
  const modelName = String(tab.modelName || '').trim();
  if (!providerKey || !modelName) {
    toast('请先在辅助对话底部选择供应商和模型');
    return;
  }
  input.value = '';
  autoGrowAuxInput();
  tab.messages.push({ role: 'user', content: text });
  const reply = { role: 'assistant', content: '', pending: true, metadata: { tool_runs: [] } };
  reply.__renderedRuns = '';
  tab.messages.push(reply);
  renderAuxChat();
  await streamAuxRun(tab, reply, text, providerKey, modelName);
}

async function streamAuxRun(tab, reply, text, providerKey, modelName) {
  auxChatState.sending = true;
  setAuxBusy(true);
  const controller = new AbortController();
  const replyIndex = tab.messages.indexOf(reply);
  try {
    // 模型口径只取辅助对话自己的选择：顶栏/输入区的供应商变化不影响这里。
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${state.token}` },
      body: JSON.stringify({
        conversation_id: tab.id,
        message: text,
        model_key: providerKey,
        model_name: modelName,
      }),
      signal: controller.signal,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    // 轻量 NDJSON 消费：正文 / 工具事件 / 终态。工具事件落进 metadata.tool_runs，
    // 与主对话同形（toolMarkup 直接渲染），所以流式期间就能看见工具卡。
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let failure = '';
    const handleEvent = (event) => {
      if (!event || typeof event !== 'object') return;
      if (event.type === 'heartbeat') return;
      if (event.type === 'delta' && typeof event.content === 'string') {
        reply.content += event.content;
        updatePendingRow(tab, reply, replyIndex);
      } else if (event.type === 'tool_start') {
        // 只登记「正在调用」；真正的结果由 tool_result 补齐（与落库结构同源）。
        reply.metadata.tool_runs = [
          ...(reply.metadata.tool_runs || []),
          {
            tool: String(event.tool || ''),
            reason: String(event.reason || ''),
            arguments: event.arguments || {},
            success: true,
            result: '执行中…',
            pending: true,
          },
        ];
        updatePendingRow(tab, reply, replyIndex);
      } else if (event.type === 'tool_result') {
        const runs = reply.metadata.tool_runs || [];
        const index = runs.findIndex((run) => run.pending && run.tool === String(event.tool || ''));
        const finished = { ...event };
        delete finished.type;
        if (index >= 0) runs[index] = finished;
        else runs.push(finished);
        reply.metadata.tool_runs = runs;
        updatePendingRow(tab, reply, replyIndex);
      } else if (event.type === 'error' || event.type === 'run_failed') {
        failure = String(event.message || event.error || '回答失败');
      } else if (event.type === 'cancelled') {
        failure = '已取消';
      }
    };
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) {
        if (!line.trim()) continue;
        try { handleEvent(JSON.parse(line)); } catch (_) { /* 非 JSON 行忽略 */ }
      }
      if (done) break;
    }
    if (buffer.trim()) {
      try { handleEvent(JSON.parse(buffer)); } catch (_) { /* 尾行不完整忽略 */ }
    }
    reply.pending = false;
    if (failure && !reply.content) reply.content = `（${failure}）`;
    else if (failure) reply.content += `\n\n（${failure}）`;
    // run 已落库：拉一次真实消息，工具卡 / 用量 / 媒体与主对话完全同源。
    // 只在服务端确实有这一轮（条数不少于本地）时才整体替换：run 失败/被取消、
    // 或后端没落库时，本地已渲染的提问与回复不能被一次空响应抹掉。
    if (!failure || reply.content) {
      try {
        const conversation = await api(`/api/conversations/${encodeURIComponent(tab.id)}`);
        const serverMessages = Array.isArray(conversation?.messages) ? conversation.messages : [];
        if (serverMessages.length >= tab.messages.length) {
          tab.messages = serverMessages;
          tab.loaded = true;
        }
      } catch (error) {
        console.debug('[naiba] 辅助对话回填消息失败:', error.message);
      }
    }
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
    setAuxBusy(false);
  }
}

function setAuxBusy(busy) {
  const send = $('#auxChatSend');
  if (!send) return;
  // 发送键是主对话同款图标按钮：只切可用态，不写 textContent（会抹掉 svg）。
  send.disabled = busy;
  send.title = busy ? '回答中' : '发送';
  send.setAttribute('aria-label', busy ? '回答中' : '发送');
}

export function autoGrowAuxInput() {
  const input = $('#auxChatInput');
  if (!input) return;
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
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
      const tab = activeTab();
      if (tab) void loadAuxMessages(tab);
      $('#auxChatInput')?.focus();
    }
  });
  $('#closeAuxPanel')?.addEventListener('click', () => closeAuxPanel());
  $('#auxChatSend')?.addEventListener('click', () => { void sendAuxMessage(); });
  $('#auxProviderSelect')?.addEventListener('change', () => { void handleAuxProviderChange(); });
  $('#auxModelSelect')?.addEventListener('change', () => { void handleAuxModelChange(); });
  // 主对话切会话 → 辅助窗跟着切到属于它的那个标签（08 只广播事件，不 import 本模块）。
  document.addEventListener('naiba:conversation-changed', () => syncAuxToConversation());
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
  // 面板开合 / 收起（文件分组点 ✕、Esc、切会话都会改）→ 辅助按钮跟着显示或隐藏。
  // 事件由 14-file-panel.js 的 applyFilePanelOpenClass() 广播，避免两个模块互相 import。
  document.addEventListener('naiba:file-panel-toggle', () => updateAuxTabsButton());
  $('#openAuxChats')?.addEventListener('click', () => openAuxPanel());
  // 「跟随」标题：点回那个主对话。
  $('#auxFollowTarget')?.addEventListener('click', () => {
    const tab = activeTab();
    if (!tab?.parentId) return;
    void openConversation(tab.parentId);
  });
  $('#auxChatMessages')?.addEventListener('click', async (event) => {
    // 空态里的「＋ 开一个辅助对话」按钮（与标签栏那个 ＋ 同一个动作）
    if (event.target.closest('[data-aux-new]')) { void createAuxChat(); return; }
    const copyButton = event.target.closest('[data-copy-message]');
    if (copyButton) {
      const text = copyButton.closest('.message-body')?.querySelector('.answer-content')?.textContent || '';
      try {
        await copyText(text.trim());
        toast('已复制回复');
      } catch (error) {
        toast(`复制失败：${error.message}`);
      }
      return;
    }
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
  try {
    await refreshAuxChats();
  } catch (_) {
    auxChatState.tabs = [];
    renderAuxChat();
  }
}
